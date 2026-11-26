"""A-plane coordinator: plan the full execution pipeline for one task.

The coordinator answers one question before anything runs:
  "Given this task, which route executes it, which route reviews it,
   how hard should it work, and when is a review mandatory?"

It wraps policy.assess + policy.recommend and adds:
- Review gating  : difficulty drives none / advisory / blocking
- Reviewer route : always a different agent family than the executor
- Effort         : bumped for hard tasks, reduced for trivial ones
- A-plane bundle : ephemeral context for executor→reviewer handoff

This module has no I/O side effects. It never reads the DB, never writes
files, never dispatches a run. The caller (runner.py) owns that.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import NamedTuple

from . import policy, routes

# ---------------------------------------------------------------------------
# Review policy
# ---------------------------------------------------------------------------

#: difficulty threshold at which a review becomes non-optional
BLOCKING_THRESHOLD = 4

#: difficulty threshold at which advisory review is added
ADVISORY_THRESHOLD = 3

#: For high-difficulty tasks, prefer stronger/more capable reviewers
#: For low-difficulty tasks, prefer cheaper/faster reviewers
#: These are agent preferences, not route names (routes are resolved dynamically)
_HIGH_REVIEW_AGENT_PREFERENCE = ("claude", "codex", "dsh", "opencode-gemini", "opencode-glm")
_LOW_REVIEW_AGENT_PREFERENCE = ("dsh", "codex", "opencode-glm", "claude", "opencode-gemini")

# Agent families: agents in the same family share the same provider ecosystem.
# A reviewer must come from a *different* family than the executor so it brings
# an independent read. Family assignments are coarse-grained on purpose.
_AGENT_FAMILIES: dict[str, str] = {
    "claude": "anthropic",
    "minimax": "anthropic",   # same protocol/CLI as claude
    "codex": "openai",
    "dsh": "deepseek",
    "opencode-glm": "glm",
    "opencode-gemini": "gemini",
}


def _family(route: str) -> str:
    """Return the model family for a route.

    Resolves the route to get its agent, then maps agent to family.
    This ensures cross-family guarantee works for all routes, not just
    the hardcoded legacy ones.

    If the route doesn't resolve or agent is unknown, returns the route
    name as a fallback (treating each unknown route as its own family).
    """
    resolved = routes.resolve(route)
    if resolved is None:
        return route  # Unknown route, treat as its own family
    agent = resolved.agent
    return _AGENT_FAMILIES.get(agent, agent)


def _routes_by_agent_preference(agent_preference: tuple[str, ...]) -> list[str]:
    """Return present routes sorted by agent preference order.

    Routes with agents earlier in the preference list come first.
    Within same agent group, routes appear in present_routes() order.
    Agents not in the preference list are appended as fallback.
    """
    present = policy.present_routes()
    # Group routes by their agent
    by_agent: dict[str, list[str]] = {}
    for route_name in present:
        resolved = routes.resolve(route_name)
        if resolved is None:
            continue
        agent = resolved.agent
        if agent not in by_agent:
            by_agent[agent] = []
        by_agent[agent].append(route_name)

    # Build ordered list following agent preference
    result = []
    for agent in agent_preference:
        if agent in by_agent:
            result.extend(by_agent[agent])
    # Append any agents not in preference (as fallback)
    for agent, routes_list in by_agent.items():
        if agent not in agent_preference:
            result.extend(routes_list)
    return result


def _pick_reviewer(executor: str, difficulty: int) -> str | None:
    """Choose a reviewer route that is from a different family than executor.

    Returns None when no suitable reviewer is available (graceful degradation).
    Hard tasks prefer a stronger reviewer (high_preference list); easy ones
    prefer a cheap one.

    Uses dynamic route discovery from present_routes() instead of hardcoded
    route names, so new routes are automatically available as reviewers.
    """
    executor_family = _family(executor)
    agent_pref = _HIGH_REVIEW_AGENT_PREFERENCE if difficulty >= BLOCKING_THRESHOLD else _LOW_REVIEW_AGENT_PREFERENCE
    candidates = _routes_by_agent_preference(agent_pref)

    # First pass: find different family
    for candidate in candidates:
        if candidate == executor:
            continue
        if _family(candidate) != executor_family:
            return candidate

    # Second pass: same family fallback — pick any that isn't the executor
    for candidate in candidates:
        if candidate != executor:
            return candidate

    return None


def _review_kind(difficulty: int, needs: list[str]) -> str:
    """'none' | 'advisory' | 'blocking'."""
    if difficulty >= BLOCKING_THRESHOLD:
        return "blocking"
    if difficulty >= ADVISORY_THRESHOLD or "reasoning" in needs:
        return "advisory"
    return "none"


# ---------------------------------------------------------------------------
# CoordinatorPlan
# ---------------------------------------------------------------------------


@dataclass
class CoordinatorPlan:
    # Execution
    executor_route: str
    effort: str                 # "low" | "medium" | "high" | "xhigh" | "default"
    # Review
    reviewer_route: str | None  # None → skip review
    review_kind: str            # "none" | "advisory" | "blocking"
    # Evidence
    difficulty: int
    kind: str
    needs: list[str] = field(default_factory=list)
    confidence: float = 0.5
    basis: str = "rules-only"
    rationale: str = ""
    # Raw outputs for debugging / tracing
    assessment: dict = field(default_factory=dict)
    scoring: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# plan()
# ---------------------------------------------------------------------------


def plan(
    task_text: str,
    context: str = "",
    cwd: str | None = None,          # noqa: ARG001  reserved for future locality scoring
    *,
    force_review: bool = False,       # e.g. --verify flag
    skip_review: bool = False,        # e.g. --no-review flag
) -> CoordinatorPlan:
    """Full coordinator plan: executor, reviewer, effort, review policy.

    Args:
        task_text:    The task goal/brief text.
        context:      Any extra context (cwd path, previous notes, etc.).
        cwd:          Working directory, reserved for future locality scoring.
        force_review: Treat review_kind as at least "advisory" even for easy tasks.
        skip_review:  Override all gating and produce reviewer_route=None.

    The plan is intentionally simple and purely derived from task content
    and route availability. It does not check the DB, does not read files.
    """
    text = (task_text or "").strip()
    if not text:
        return CoordinatorPlan(
            executor_route="claude",
            effort="default",
            reviewer_route=None,
            review_kind="none",
            difficulty=1,
            kind="code",
            rationale="empty task text — using defaults",
        )

    assessment = policy.assess(text, context=context, review=True)
    recommendation = policy.recommend(assessment)

    difficulty = assessment["difficulty"]
    kind = assessment.get("kind") or "code"
    needs = list(assessment.get("needs") or [])
    executor_route = str(recommendation.get("route") or "").strip()
    if not executor_route:
        raise ValueError("recommend() returned no route")
    effort = str(recommendation.get("effort") or "default").strip() or "default"
    basis = assessment.get("basis") or "rules-only"
    confidence = assessment.get("confidence") or 0.5

    # Review gating
    if skip_review:
        review_kind = "none"
        reviewer_route = None
    else:
        review_kind = _review_kind(difficulty, needs)
        if force_review and review_kind == "none":
            review_kind = "advisory"
        reviewer_route = (
            _pick_reviewer(executor_route, difficulty)
            if review_kind != "none"
            else None
        )

    rationale = _build_rationale(
        difficulty, kind, needs, executor_route, effort,
        reviewer_route, review_kind, basis, recommendation,
    )

    return CoordinatorPlan(
        executor_route=executor_route,
        effort=effort,
        reviewer_route=reviewer_route,
        review_kind=review_kind,
        difficulty=difficulty,
        kind=kind,
        needs=needs,
        confidence=confidence,
        basis=basis,
        rationale=rationale,
        assessment=assessment,
        scoring=recommendation.get("scoring") or {},
    )


def _build_rationale(
    difficulty: int,
    kind: str,
    needs: list[str],
    executor: str,
    effort: str,
    reviewer: str | None,
    review_kind: str,
    basis: str,
    recommendation: dict,
) -> str:
    need_text = ", ".join(needs) if needs else "无特殊能力"
    r = (
        f"难度 {difficulty}/5，类型 {kind}，需要 {need_text}。"
        f"执行路由 {executor}（effort {effort}），基于 {basis}。"
        f"置信度 {recommendation.get('confidence', 0):.2f}。"
    )
    if reviewer:
        r += f" 审查路由 {reviewer}（{review_kind}）。"
    else:
        r += " 跳过审查。"
    r += f" {recommendation.get('rationale') or ''}"
    return r.strip()
