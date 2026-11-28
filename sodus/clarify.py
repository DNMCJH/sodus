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

You must output exactly one JSON object (no markdown fences, no prose before or
after). Schema:

{
  "questions": [          // 2-4 targeted questions, most important first
    {
      "q": "...",         // the question (Chinese or English matching input)
      "why": "...",       // what ambiguity this resolves
      "options": ["..."]  // 2-3 concrete options the user can pick, or []
    }
  ],
  "draft_spec": "...",    // refined task spec using what IS known; fill gaps
                          // with the most conservative reasonable assumption,
                          // mark each assumption with [ASSUMED]
  "acceptance_draft": [   // 2-5 verifiable acceptance criteria
    "..."                 // each one must be checkable: a file exists, a test
                          // passes, a command exits 0, a count matches, etc.
  ],
  "scope_risk": "low|medium|high",  // how much the ambiguity could inflate cost
  "estimated_effort": "1h|4h|1d|3d|1w+"  // rough wall-clock, not compute
}

Be concise. The questions field is the most important — the user reads these
and answers before anything runs. Keep each question under 60 characters.
"""


def _user_message(task_text: str, assessment: dict) -> str:
    difficulty = assessment.get("difficulty", "?")
    kind = assessment.get("kind", "unknown")
    signals = assessment.get("signals") or []
    confidence = assessment.get("confidence", 0)
    basis = assessment.get("basis", "unknown")
    needs = assessment.get("needs") or []

    signal_summary = (
        "none — rules could not find any structural signals"
        if not signals
        else ", ".join(signals)
    )
    return (
        f"Task: {task_text}\n\n"
        f"Assessment:\n"
        f"  difficulty: {difficulty}/5\n"
        f"  kind: {kind}\n"
        f"  signals: {signal_summary}\n"
        f"  needs: {', '.join(needs) or 'none'}\n"
        f"  confidence: {confidence:.2f} (basis: {basis})\n\n"
        "The rule engine had low confidence due to minimal structural signals. "
        "Identify what is under-specified and produce the clarification output."
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
    max_tokens: int = 1024,
) -> ClarifyResult:
    """Run one clarification pass and return a structured ClarifyResult.

    Fails open: if the cortex call fails or the JSON is unparseable, returns
    a ClarifyResult(ok=False) so the caller can proceed without blocking.
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
    parsed = _parse(text)
    if parsed is None:
        return ClarifyResult(
            ok=False,
            unavailable="JSON parse failed",
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
        for item in (parsed.get("questions") or [])
        if item.get("q")
    ]

    return ClarifyResult(
        ok=True,
        questions=questions,
        draft_spec=str(parsed.get("draft_spec") or ""),
        acceptance_draft=[str(c) for c in (parsed.get("acceptance_draft") or [])],
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
