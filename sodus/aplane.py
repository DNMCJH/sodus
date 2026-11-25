"""A-plane ephemeral handoff context.

An APlaneCtx is a short-lived inter-agent bundle that the coordinator
creates after execution and passes to the reviewer.  It is *not* a
persistent project record (that is HANDOFF.md); it lives only for the
duration of one run and is stored under ~/.sodus/runs/<run_id>/.

The reviewer uses it to focus its reading:
- What did the coordinator decide and why?
- What acceptance criteria must pass?
- What did verification show?
- Which files actually changed?

Nothing in this module dispatches or writes to the DB.  The caller owns
those steps.  This module only serialises and deserialises the bundle.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config


@dataclass
class APlaneCtx:
    """Ephemeral context for one executor→reviewer handoff."""

    run_id: str
    task_id: str
    task_title: str
    task_goal: str
    acceptance: str

    # Coordinator decisions
    executor_route: str
    reviewer_route: str | None
    review_kind: str            # "none" | "advisory" | "blocking"
    coordinator_rationale: str
    difficulty: int
    kind: str

    # Post-execution evidence
    verify_state: str = "none"  # "ok" | "red" | "none"
    verify_summary: str = ""    # one-line test/check output summary
    changed_files: list[str] = field(default_factory=list)
    diff_stat: str = ""         # `git diff --stat` output (truncated)

    # Housekeeping
    created_at: float = field(default_factory=time.time)
    schema_version: int = 1

    # ---- serialisation ---------------------------------------------------

    def to_json(self) -> dict:
        return asdict(self)

    def to_prompt_fragment(self, max_diff_lines: int = 40) -> str:
        """Compact text fragment for inclusion in a reviewer's prompt."""
        lines: list[str] = [
            f"## A-plane 协调上下文（run {self.run_id}）",
            f"任务：{self.task_title}",
            f"目标：{self.task_goal}",
            f"验收：{self.acceptance}",
            f"执行路由：{self.executor_route}（难度 {self.difficulty}/5，类型 {self.kind}）",
            f"审查类型：{self.review_kind}",
            f"协调理由：{self.coordinator_rationale}",
            f"验证结果：{self.verify_state}"
            + (f" — {self.verify_summary}" if self.verify_summary else ""),
        ]
        if self.diff_stat:
            diff_lines = self.diff_stat.splitlines()[:max_diff_lines]
            lines.append("变更统计：")
            lines.extend(f"  {ln}" for ln in diff_lines)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


def _ctx_path(run_id: str) -> Path:
    return config.HOME / "runs" / run_id / "aplane_ctx.json"


def save(ctx: APlaneCtx) -> Path:
    """Persist an APlaneCtx to disk.  Returns the file path."""
    path = _ctx_path(ctx.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(ctx.to_json(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def load(run_id: str) -> APlaneCtx | None:
    """Load an APlaneCtx from disk, or None if it does not exist."""
    path = _ctx_path(run_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # Tolerate unknown / missing keys so old bundles still parse
    known = {f.name for f in APlaneCtx.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    filtered = {k: v for k, v in data.items() if k in known}
    try:
        return APlaneCtx(**filtered)
    except TypeError:
        return None


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build(
    run_id: str,
    task: dict,
    coordinator_plan,          # coordinator.CoordinatorPlan
    *,
    verify_state: str = "none",
    verify_summary: str = "",
    changed_files: list[str] | None = None,
    diff_stat: str = "",
) -> APlaneCtx:
    """Build an APlaneCtx from a completed execution.

    Args:
        run_id:           Run ID.
        task:             Task dict from db.get_task().
        coordinator_plan: The CoordinatorPlan that drove this execution.
        verify_state:     "ok" | "red" | "none".
        verify_summary:   Short summary of test/verify output.
        changed_files:    List of paths changed by the execution.
        diff_stat:        `git diff --stat` output (may be empty).
    """
    return APlaneCtx(
        run_id=run_id,
        task_id=str(task.get("id") or ""),
        task_title=str(task.get("title") or ""),
        task_goal=str(task.get("goal") or task.get("title") or ""),
        acceptance=str(task.get("acceptance") or ""),
        executor_route=coordinator_plan.executor_route,
        reviewer_route=coordinator_plan.reviewer_route,
        review_kind=coordinator_plan.review_kind,
        coordinator_rationale=coordinator_plan.rationale,
        difficulty=coordinator_plan.difficulty,
        kind=coordinator_plan.kind,
        verify_state=verify_state,
        verify_summary=verify_summary,
        changed_files=list(changed_files or []),
        diff_stat=diff_stat,
    )
