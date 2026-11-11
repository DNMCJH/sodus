"""Claude Code sessions: one JSONL per conversation.

    <CLAUDE_CONFIG_DIR>/projects/<slug>/<session-uuid>.jsonl

The slug encodes the working directory (`A--VScode-Code-Projects-x`), but the
authoritative cwd is in the first line that carries one, so this never guesses
from a lossy encoding.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .. import config
from . import model

FILE_SUFFIX = ".jsonl"


def roots() -> list[tuple[Path, str, str]]:
    """(projects dir, profile, provider) for every Claude config home we own.

    Each combination keeps its own store, so the same session id can exist in
    two homes; carrying the origin with the path is what lets `resume` pick the
    endpoint that actually holds the history.
    """
    out: list[tuple[Path, str, str]] = []
    base = config.HOME / "profiles"
    if base.is_dir():
        for profile_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            claude_dir = profile_dir / "claude"
            if not claude_dir.is_dir():
                continue
            for home in sorted(p for p in claude_dir.iterdir() if p.is_dir()):
                projects = home / "projects"
                if projects.is_dir():
                    out.append((projects, profile_dir.name, home.name))
    user = Path.home() / ".claude" / "projects"
    if user.is_dir():
        out.append((user, "", ""))
    return out


def _rows(path: Path, limit: int = 0) -> list[dict]:
    """Parse a session file; `limit` reads only the head (meta lives there)."""
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for index, line in enumerate(f):
                if limit and index >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def _ts(value) -> float:
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 1e11 else 1)
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        try:
            from datetime import datetime

            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _cwd(rows: list[dict], slug: str) -> str:
    for row in rows:
        if isinstance(row.get("cwd"), str) and row["cwd"]:
            return row["cwd"]
        snap = (row.get("attachment") or {}).get("snapshot") or {}
        if isinstance(snap.get("workingDirectory"), str) and snap["workingDirectory"]:
            return snap["workingDirectory"]
    return slug


def _text_of(content) -> str:
    if isinstance(content, str):
        return content.strip()
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts).strip()


def messages_of(session: model.Session, include_reasoning: bool = False) -> list[dict]:
    out: list[dict] = []
    for row in _rows(session.path):
        if row.get("isSidechain"):
            continue  # a subagent's private thread, not this conversation
        kind = row.get("type")
        message = row.get("message") or {}
        ts = _ts(row.get("timestamp"))
        if kind == "user":
            text = _text_of(message.get("content"))
            if text:
                out.append({"role": model.USER, "ts": ts, "text": text})
        elif kind == "assistant":
            content = message.get("content") or []
            text = _text_of(content)
            if text:
                out.append({"role": model.ASSISTANT, "ts": ts, "text": text})
            if include_reasoning:
                reasoning = "\n".join(
                    b.get("thinking", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "thinking"
                ).strip()
                if reasoning:
                    out.append({"role": model.REASONING, "ts": ts, "text": reasoning})
    return out


def scan() -> list[model.Session]:
    found: list[model.Session] = []
    for projects, profile, provider in roots():
        for path in projects.rglob(f"*{FILE_SUFFIX}"):
            if path.parent.name == "subagents":
                continue
            # The head carries the cwd, the session id and the first turn; the
            # tail only carries more turns. Scanning a 1.4 MB rollout for a
            # title is what made the list take seven seconds.
            rows = _rows(path, limit=80)
            if not rows:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            session_id = path.stem
            summary = ""
            for row in rows:
                if row.get("type") == "summary" and row.get("summary"):
                    summary = str(row["summary"])
                    break
            msgs = self_messages(rows)
            title = summary or model.title_of(
                next((m["text"] for m in msgs if m["role"] == model.USER), "")
            )
            times = [_ts(r.get("timestamp")) for r in rows]
            times = [t for t in times if t > 0]
            found.append(
                model.Session(
                    vendor="claude",
                    id=session_id,
                    cwd=_cwd(rows, path.parent.name),
                    title=title,
                    created_at=min(times) if times else mtime,
                    updated_at=mtime,
                    path=path,
                    events=len(rows),
                    profile=profile,
                    provider=provider,
                )
            )
    return found


def self_messages(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        if row.get("isSidechain"):
            continue
        if row.get("type") not in ("user", "assistant"):
            continue
        text = _text_of((row.get("message") or {}).get("content"))
        if text:
            out.append({"role": row["type"], "ts": _ts(row.get("timestamp")), "text": text})
    return out


def sessions_dir(profile: str, provider: str) -> Path:
    return config.HOME / "profiles" / profile / "claude" / provider / "projects"


def ago(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"
