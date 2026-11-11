"""Codex sessions: one rollout JSONL per thread, filed by date.

    <CODEX_HOME>/sessions/YYYY/MM/DD/rollout-<iso>-<thread-uuid>.jsonl

The first line is `session_meta` and carries both the thread id and the cwd, so
a scan can take the id without parsing the rest.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import config
from . import model


def roots() -> list[tuple[Path, str, str]]:
    out: list[tuple[Path, str, str]] = []
    base = config.HOME / "profiles"
    if base.is_dir():
        for profile_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            codex_dir = profile_dir / "codex"
            if not codex_dir.is_dir():
                continue
            for home in sorted(p for p in codex_dir.iterdir() if p.is_dir()):
                sessions = home / "sessions"
                if sessions.is_dir():
                    out.append((sessions, profile_dir.name, home.name))
    user = Path.home() / ".codex" / "sessions"
    if user.is_dir():
        out.append((user, "", ""))
    return out


def _rows(path: Path, limit: int = 0) -> list[dict]:
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
        try:
            from datetime import datetime

            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _text_of(content) -> str:
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") in ("input_text", "output_text", "text"):
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts).strip()


def messages_of(session: model.Session, include_reasoning: bool = False) -> list[dict]:
    out: list[dict] = []
    for row in _rows(session.path):
        if row.get("type") != "response_item":
            continue
        payload = row.get("payload") or {}
        if payload.get("type") != "message":
            continue
        role = payload.get("role")
        ts = _ts(row.get("timestamp"))
        text = _text_of(payload.get("content"))
        if not text:
            continue
        if role == "user":
            out.append({"role": model.USER, "ts": ts, "text": text})
        elif role == "assistant":
            out.append({"role": model.ASSISTANT, "ts": ts, "text": text})
        elif role == "developer" and include_reasoning:
            out.append({"role": model.REASONING, "ts": ts, "text": text})
    return out


def scan() -> list[model.Session]:
    found: list[model.Session] = []
    for sessions, profile, provider in roots():
        for path in sessions.rglob("rollout-*.jsonl"):
            rows = _rows(path, limit=120)
            if not rows:
                continue
            meta = next(
                (r.get("payload") or {} for r in rows if r.get("type") == "session_meta"), {}
            )
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            session_id = str(meta.get("session_id") or meta.get("id") or path.stem)
            times = [_ts(r.get("timestamp")) for r in rows]
            times = [t for t in times if t > 0]
            first = next(
                (
                    _text_of((r.get("payload") or {}).get("content"))
                    for r in rows
                    if r.get("type") == "response_item"
                    and (r.get("payload") or {}).get("role") == "user"
                ),
                "",
            )
            found.append(
                model.Session(
                    vendor="codex",
                    id=session_id,
                    cwd=str(meta.get("cwd") or ""),
                    title=model.title_of(first),
                    created_at=min(times) if times else _ts(meta.get("timestamp")) or mtime,
                    updated_at=mtime,
                    path=path,
                    events=len(rows),
                    profile=profile,
                    provider=provider,
                )
            )
    return found
