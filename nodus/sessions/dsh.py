"""DSH sessions: an append-only zstd JSONL stream per conversation.

    <DSH_HOME>/sessions/<project-slug>/<session-id>/session.v4.jsonl.zstd

Read-only, deliberately: DSH has no cross-session API, and injecting events
into an append-only store would corrupt the GUI's live state. Handing work over
is done by emitting a brief the next session can consume.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import zstandard

from . import model

FILENAME = "session.v4.jsonl.zstd"


def default_root() -> Path:
    env = os.environ.get("DSH_SESSIONS")
    if env:
        return Path(env).expanduser()
    home = os.environ.get("DSH_HOME")
    if home:
        return Path(home).expanduser() / "sessions"
    return Path.home() / ".dsh" / "sessions"


ROOT = default_root()


def _load(path: Path) -> list[dict]:
    with path.open("rb") as f:
        raw = zstandard.ZstdDecompressor().stream_reader(f).read()
    rows = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _text_of(content) -> str:
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts).strip()


def _meta(rows: list[dict]) -> dict:
    head = next((r for r in rows if r.get("type") == "session"), {})
    title = ""
    for row in reversed(rows):
        if row.get("type") == "session/title":
            title = (row.get("data") or {}).get("title", "")
            break
    times = [r.get("time") for r in rows if isinstance(r.get("time"), (int, float))]
    return {
        "id": head.get("id", ""),
        "cwd": head.get("cwd", ""),
        "created_at": (head.get("createdAt") or 0) / 1000,
        "updated_at": (max(times) if times else 0) / 1000,
        "title": title,
        "rows": len(rows),
    }


def messages(rows: list[dict], include_reasoning: bool = False) -> list[dict]:
    """User and assistant prose, in order. Tool traffic and reminders dropped."""
    out: list[dict] = []
    for row in rows:
        kind = row.get("type")
        data = row.get("data") or {}
        ts = (row.get("time") or 0) / 1000
        if kind == "user/message":
            if (data.get("source") or {}).get("kind") != "user":
                continue
            text = _text_of(data.get("content"))
            if text:
                out.append({"role": model.USER, "ts": ts, "text": text})
        elif kind == "assistant/message":
            content = (data.get("message") or {}).get("content") or []
            text = _text_of(content)
            if text:
                out.append({"role": model.ASSISTANT, "ts": ts, "text": text})
            if include_reasoning:
                reasoning = "\n".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "reasoning"
                ).strip()
                if reasoning:
                    out.append({"role": model.REASONING, "ts": ts, "text": reasoning})
    return out


def files() -> list[tuple[Path, float]]:
    if not ROOT.exists():
        return []
    out = []
    for path in ROOT.rglob(FILENAME):
        try:
            out.append((path, path.stat().st_mtime))
        except OSError:
            continue
    out.sort(key=lambda item: item[1], reverse=True)
    return out


def scan() -> list[model.Session]:
    found: list[model.Session] = []
    for path, mtime in files():
        try:
            rows = _load(path)
        except Exception:  # noqa: BLE001 - one corrupt session must not hide the rest
            continue
        meta = _meta(rows)
        title = meta["title"]
        if not title:
            first = next(
                (m["text"] for m in messages(rows) if m["role"] == model.USER), ""
            )
            title = model.title_of(first)
        found.append(
            model.Session(
                vendor="dsh",
                id=meta["id"] or path.parent.name,
                cwd=meta["cwd"],
                title=title,
                created_at=meta["created_at"],
                updated_at=meta["updated_at"] or mtime,
                path=path,
                events=meta["rows"],
            )
        )
    return found


def messages_of(session: model.Session, include_reasoning: bool = False) -> list[dict]:
    return messages(_load(session.path), include_reasoning)
