"""One session, whichever vendor wrote it.

A session is the unit a person means when they say "the conversation where we
decided X". Every agent CLI keeps its own store in its own format; this is the
shape the rest of Nodus reads, so `nodus sessions` is one list and not three.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Role names used by every adapter. `tool` is a normalised tool call line and
#: is dropped unless asked for: it is evidence, not conversation.
USER = "user"
ASSISTANT = "assistant"
REASONING = "reasoning"
TOOL = "tool"


@dataclass(frozen=True)
class Session:
    vendor: str
    id: str
    cwd: str
    title: str
    created_at: float
    updated_at: float
    path: Path
    events: int = 0
    #: which configured endpoint it was written through ("" when it predates
    #: profiles or came from the user's own CLI config)
    profile: str = ""
    provider: str = ""
    #: the Nodus run that produced it, when there is one
    run_id: str = ""

    @property
    def source(self) -> str:
        return "run" if self.run_id else "user"

    @property
    def origin(self) -> str:
        if self.provider:
            return f"{self.vendor}@{self.provider}"
        return f"{self.vendor}?"

    def as_row(self) -> dict:
        return {
            "id": self.id,
            "vendor": self.vendor,
            "cwd": self.cwd,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "path": str(self.path),
            "events": self.events,
            "profile": self.profile,
            "provider": self.provider,
            "run_id": self.run_id,
            "source": self.source,
        }


def first_line(text: str, limit: int = 80) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        return line[:limit]
    return ""


#: Lines a session's first user turn often starts with, which say nothing
#: about what the conversation was for.
NOISE_PREFIXES = (
    "你是 Sodus 里负责执行任务的 agent",
    # Prompts written before the 0.5.0 rename. Old sessions still open with it;
    # their titles must not turn into the template line either.
    "你是 Nodus 里负责执行任务的 agent",
    "<environment_context>",
    "<user_instructions>",
    "# AGENTS.md",
)


def title_of(text: str, limit: int = 80) -> str:
    """A label that describes the conversation, not the harness around it.

    A Sodus run's first user turn is a template whose first line is identical
    in every run; the task itself is under `## 任务`. Showing the template line
    makes every row look the same, which is worse than no title at all.
    """
    body = text or ""
    for marker in ("## 任务", "## Task"):
        if marker in body:
            tail = body.split(marker, 1)[1]
            line = first_line(tail, limit)
            if line and not line.startswith("#"):
                return line
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if any(line.startswith(prefix) for prefix in NOISE_PREFIXES):
            continue
        return line[:limit]
    return ""
