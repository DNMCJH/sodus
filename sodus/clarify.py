"""L1 Requirements Intake: clarify ambiguous tasks before dispatch.

When the coordinator flags needs_clarification=True (blank signals + hard
difficulty, or broad scope keywords), this module runs a structured
clarification round using the coordinator route (Sonnet 5.5).

The model's job here is NOT to solve the task. It is to:
  1. Identify what is under-specified (scope, success criteria, constraints).
  2. Produce 2-4 targeted clarification questions.
  3. Draft a preliminary spec from whatever IS known.
  4. Draft acceptance criteria that can be verified programmatically.

Output is a ClarifyResult that the caller (runner.py / CLI) persists as
refined_spec.md alongside the run, and optionally injects into HANDOFF.md
as a D-xxx decision draft.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import cortex

# ---------------------------------------------------------------------------
# Prompt design
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are the requirements intake layer for a personal AI task orchestration system.
Your role is strictly scoped: analyse the given task description and assessment,
identify what is under-specified, and produce a structured clarification output.

You are NOT solving the task. You are preparing it for safe dispatch to an
executor model. A bad spec wastes expensive compute; an ambiguous scope leads
to multi-hour runs that miss the point.

You must output exactly one JSON object with no markdown fences and no prose
before or after. The schema is:

{
  "questions": [
    {
      "q": "...",
      "why": "...",
      "options": ["...", "..."]
    }
  ],
  "draft_spec": "...",
  "acceptance_draft": ["...", "..."],
  "scope_risk": "low",
  "estimated_effort": "1h"
}

Field rules:
- questions: array of 2-4 objects, each with string fields q, why, and an
  options array of 2-3 strings. Most important question first. Keep each q
  under 60 characters.
- draft_spec: string. Refined task spec using what IS known. Mark each gap
  you assumed with [ASSUMED] in the text.
- acceptance_draft: array of 2-5 strings. Each must be verifiable: a file
  exists, a test passes, a command exits 0, a count matches, etc.
- scope_risk: exactly one of "low", "medium", or "high".
- estimated_effort: rough wall-clock, one of "1h", "4h", "1d", "3d", "1w+".

Output only the JSON object. No commentary, no code fences, no trailing text.
"""


def _user_message(task_text: str, assessment: dict) -> str:
    difficulty = assessment.get("difficulty", "?")
    kind = assessment.get("kind", "unknown")
    signals = assessment.get("signals") or []
    rules_conf = (assessment.get("rules") or {}).get("confidence", assessment.get("confidence", 0))
    composite_conf = assessment.get("confidence", 0)
    basis = assessment.get("basis", "unknown")
    needs = assessment.get("needs") or []

    # Describe the actual reason for clarification (not a generic message)
    if not signals:
        trigger_reason = "Rules engine found no structural signals in the task text."
    else:
        trigger_reason = (
            f"Task contains broad scope keywords despite having signals: {', '.join(signals)}."
        )

    return (
        f"Task: {task_text}\n\n"
        f"Assessment:\n"
        f"  difficulty: {difficulty}/5\n"
        f"  kind: {kind}\n"
        f"  signals: {signals or 'none'}\n"
        f"  needs: {', '.join(needs) or 'none'}\n"
        f"  rules_confidence: {rules_conf:.2f}  composite_confidence: {composite_conf:.2f}\n"
        f"  basis: {basis}\n\n"
        f"Trigger reason: {trigger_reason}\n"
        "Identify what is under-specified and produce the clarification JSON."
    )


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ClarifyQuestion:
    q: str
    why: str
    options: list[str] = field(default_factory=list)


@dataclass
class ClarifyResult:
    ok: bool
    questions: list[ClarifyQuestion] = field(default_factory=list)
    draft_spec: str = ""
    acceptance_draft: list[str] = field(default_factory=list)
    scope_risk: str = "medium"
    estimated_effort: str = "unknown"
    # Meta
    model: str = ""
    latency_ms: int = 0
    unavailable: str = ""   # non-empty when ok=False
    raw: str = ""           # raw LLM text, for debugging

    def to_markdown(self, task_text: str = "") -> str:
        """Render as refined_spec.md content."""
        lines = ["# Clarification Required\n"]
        if task_text:
            lines += [f"**Original task:** {task_text}\n"]
        lines += [
            f"**Scope risk:** {self.scope_risk}  ",
            f"**Estimated effort:** {self.estimated_effort}\n",
            "## Clarification Questions\n",
        ]
        for i, q in enumerate(self.questions, 1):
            lines.append(f"### Q{i}: {q.q}")
            lines.append(f"*Why this matters:* {q.why}")
            if q.options:
                lines.append("Options:")
                for opt in q.options:
                    lines.append(f"- [ ] {opt}")
            lines.append("")
        lines += [
            "## Draft Spec\n",
            self.draft_spec or "_Not generated._",
            "",
            "## Acceptance Criteria Draft\n",
        ]
        for criterion in self.acceptance_draft:
            lines.append(f"- [ ] {criterion}")
        lines += [
            "",
            f"---",
            f"*Generated by cortex/{self.model} in {self.latency_ms}ms*",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ClarifyResult":
        questions = [
            ClarifyQuestion(
                q=item.get("q", ""),
                why=item.get("why", ""),
                options=list(item.get("options") or []),
            )
            for item in (data.get("questions") or [])
        ]
        return cls(
            ok=bool(data.get("ok", False)),
            questions=questions,
            draft_spec=str(data.get("draft_spec") or ""),
            acceptance_draft=[str(c) for c in (data.get("acceptance_draft") or [])],
            scope_risk=str(data.get("scope_risk") or "medium"),
            estimated_effort=str(data.get("estimated_effort") or "unknown"),
            model=str(data.get("model") or ""),
            latency_ms=int(data.get("latency_ms") or 0),
            unavailable=str(data.get("unavailable") or ""),
            raw=str(data.get("raw") or ""),
        )


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _validate(parsed: dict) -> str | None:
    """Return error message if parsed JSON fails schema checks, else None."""
    qs = parsed.get("questions")
    if not isinstance(qs, list):
        return f"questions must be a list, got {type(qs).__name__}"
    for i, item in enumerate(qs):
        if not isinstance(item, dict):
            return f"questions[{i}] must be a dict, got {type(item).__name__}"
        if not isinstance(item.get("q"), str) or not item["q"].strip():
            return f"questions[{i}].q must be a non-empty string"
    ac = parsed.get("acceptance_draft")
    if ac is not None and not isinstance(ac, list):
        return f"acceptance_draft must be a list, got {type(ac).__name__}"
    if isinstance(ac, list):
        for i, item in enumerate(ac):
            if not isinstance(item, str):
                return f"acceptance_draft[{i}] must be a string, got {type(item).__name__}"
    scope_risk = parsed.get("scope_risk")
    if scope_risk is not None and scope_risk not in ("low", "medium", "high"):
        parsed["scope_risk"] = "medium"
    return None


def _parse(text: str) -> dict | None:
    """Extract the JSON object from cortex output (strips accidental fences)."""
    # Strip markdown fences if present
    cleaned = re.sub(r"^```[a-z]*\n?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    # Find outermost JSON object
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1
    if start == -1 or end == 0:
        return None
    try:
        return json.loads(cleaned[start:end])
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(
    task_text: str,
    assessment: dict,
    *,
    route: str = "coordinator",
    max_tokens: int = 2048,
) -> ClarifyResult:
    """Run one clarification pass and return a structured ClarifyResult.

    Fails open: if the cortex call fails, JSON is unparseable, or schema
    validation fails, returns a ClarifyResult(ok=False) so the caller can
    proceed without blocking or changing the route decision.
    """
    user_msg = _user_message(task_text, assessment)
    result = cortex.ask(
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
        route=route,
        max_tokens=max_tokens,
    )

    if not result["ok"]:
        return ClarifyResult(
            ok=False,
            unavailable=result.get("unavailable", "unknown error"),
            latency_ms=result.get("latency_ms", 0),
        )

    text = result.get("text", "")
    # Check for truncation before parsing
    stop_reason = result.get("stop_reason", "")
    truncated = stop_reason == "max_tokens"

    parsed = _parse(text)
    if parsed is None:
        reason = "JSON parse failed (truncated output)" if truncated else "JSON parse failed"
        return ClarifyResult(
            ok=False,
            unavailable=reason,
            model=result.get("model", ""),
            latency_ms=result.get("latency_ms", 0),
            raw=text[:500],
        )

    # Schema validation — bad structure must not propagate as ok=True
    validation_error = _validate(parsed)
    if validation_error:
        return ClarifyResult(
            ok=False,
            unavailable=f"schema validation: {validation_error}",
            model=result.get("model", ""),
            latency_ms=result.get("latency_ms", 0),
            raw=text[:500],
        )

    questions = [
        ClarifyQuestion(
            q=str(item.get("q", "")),
            why=str(item.get("why", "")),
            options=[str(o) for o in (item.get("options") or [])],
        )
        for item in (parsed.get("questions") or [])[:4]  # cap at 4
        if isinstance(item, dict) and item.get("q")
    ]

    acceptance = parsed.get("acceptance_draft") or []
    return ClarifyResult(
        ok=True,
        questions=questions,
        draft_spec=str(parsed.get("draft_spec") or ""),
        acceptance_draft=[str(c) for c in acceptance if isinstance(c, str)],
        scope_risk=str(parsed.get("scope_risk") or "medium"),
        estimated_effort=str(parsed.get("estimated_effort") or "unknown"),
        model=result.get("model", ""),
        latency_ms=result.get("latency_ms", 0),
        raw=text,
    )


def save(result: ClarifyResult, task_text: str, dest: Path) -> Path:
    """Write refined_spec.md to dest directory. Returns the written path."""
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "refined_spec.md"
    path.write_text(result.to_markdown(task_text), encoding="utf-8")
    return path
