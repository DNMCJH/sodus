"""Read DSH chat sessions from disk.

DSH stores each session as an append-only zstd-compressed JSONL stream:

    $DSH_HOME/sessions/<project-slug>/<session-id>/session.v4.jsonl.zstd

There is no cross-session API in DSH's CLI (it only boots profiles), so reading
the store is the only way for Nodus to know what another conversation decided.
This module is deliberately read-only: it never writes, recompresses, or
injects events into a session. Handing work over is done by emitting a brief
that the next session (or the human) can consume.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path

import zstandard

from . import config

FILE_NAME = "session.v4.jsonl.zstd"
URL_RE = re.compile(r"https?://[^\s)\]）>\"']+")
WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"'<>|]+")


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


def _text_of(content) -> str:
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts).strip()


def messages(rows: list[dict], include_reasoning: bool = False) -> list[dict]:
    """User and assistant prose, in order. Tool traffic and system reminders dropped."""
    out: list[dict] = []
    for row in rows:
        kind = row.get("type")
        data = row.get("data") or {}
        ts = (row.get("time") or 0) / 1000
        if kind == "user/message":
            source = (data.get("source") or {}).get("kind")
            if source != "user":
                continue
            text = _text_of(data.get("content"))
            if text:
                out.append({"role": "user", "ts": ts, "text": text})
        elif kind == "assistant/message":
            content = (data.get("message") or {}).get("content") or []
            text = _text_of(content)
            if text:
                out.append({"role": "assistant", "ts": ts, "text": text})
            if include_reasoning:
                reasoning = "\n".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "reasoning"
                ).strip()
                if reasoning:
                    out.append({"role": "reasoning", "ts": ts, "text": reasoning})
    return out


def session_files() -> list[dict]:
    """All session files, newest first."""
    found = []
    if not ROOT.exists():
        return found
    for path in ROOT.rglob(FILE_NAME):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        found.append(
            {
                "id": path.parent.name,
                "path": path,
                "project": path.parent.parent.name,
                "mtime": mtime,
            }
        )
    found.sort(key=lambda item: item["mtime"], reverse=True)
    return found


def list_sessions(limit: int = 30, query: str = "") -> list[dict]:
    out = []
    needle = query.lower()
    for item in session_files():
        if len(out) >= limit:
            break
        try:
            rows = _load(item["path"])
        except Exception:  # noqa: BLE001 - one corrupt session must not hide the rest
            continue
        meta = _meta(rows)
        meta["project"] = item["project"]
        meta["mtime"] = item["mtime"]
        if not _allowed(meta.get("cwd") or ""):
            continue
        text = messages(rows)
        meta["user_messages"] = sum(1 for m in text if m["role"] == "user")
        meta["first_user"] = next((m["text"][:120] for m in text if m["role"] == "user"), "")
        if needle and needle not in (meta["title"] + meta["first_user"]).lower():
            continue
        out.append(meta)
    return out


def _norm_id(session_id: str) -> str:
    return session_id[8:] if session_id.startswith("session-") else session_id


def _header_cwd(path: Path) -> str:
    """Read only the header frame; enough to know where the session belongs."""
    try:
        with path.open("rb") as f:
            head = zstandard.ZstdDecompressor().stream_reader(f).read(8192).decode(
                "utf-8", "replace"
            )
        return json.loads(head.splitlines()[0]).get("cwd") or ""
    except (OSError, json.JSONDecodeError, IndexError, zstandard.ZstdError):
        return ""


def _allowed(cwd: str) -> bool:
    """Optional whitelist: sessions must live under one of `session_roots`."""
    roots = config.session_roots()
    if not roots:
        return True
    try:
        target = Path(cwd).resolve()
    except (OSError, ValueError):
        return False
    return any(target == root or root in target.parents for root in roots)


def find(session_id: str) -> Path:
    """Accept a full id or a unique prefix, with or without the `session-` part."""
    items = session_files()
    needle = _norm_id(session_id)
    candidates = [item for item in items if _norm_id(item["id"]) == needle]
    if not candidates:
        candidates = [item for item in items if _norm_id(item["id"]).startswith(needle)]
    if not candidates:
        raise ValueError(f"session not found: {session_id} (run `nodus sessions`)")
    if len(candidates) > 1:
        raise ValueError(
            f"ambiguous session prefix: {session_id} matches "
            + ", ".join(c["id"] for c in candidates[:5])
        )
    return candidates[0]["path"]


def load(session_id: str) -> tuple[dict, list[dict]]:
    path = find(session_id)
    rows = _load(path)
    meta = _meta(rows)
    meta["project"] = path.parent.parent.name
    meta["path"] = str(path)
    if not _allowed(meta.get("cwd") or ""):
        raise ValueError(
            f"session outside configured session_roots: {meta.get('cwd')}"
        )
    return meta, rows


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"


def transcript(session_id: str, tail: int = 40, include_reasoning: bool = False) -> str:
    meta, rows = load(session_id)
    msgs = messages(rows, include_reasoning)
    if tail > 0:
        msgs = msgs[-tail:]
    lines = [
        f"# {meta['title'] or meta['id']}",
        "",
        f"- 会话：{meta['id']}",
        f"- 工作目录：{meta['cwd']}",
        f"- 时间：{_fmt_time(meta['created_at'])} → {_fmt_time(meta['updated_at'])}",
        f"- 消息：{sum(1 for m in msgs if m['role'] == 'user')} user / "
        f"{sum(1 for m in msgs if m['role'] == 'assistant')} assistant（本页）",
        "",
    ]
    for msg in msgs:
        who = {"user": "用户", "assistant": "助手", "reasoning": "思考"}[msg["role"]]
        lines.append(f"## {_fmt_time(msg['ts'])} {who}")
        lines.append("")
        lines.append(msg["text"].strip())
        lines.append("")
    return "\n".join(lines)


def extract_refs(text: str) -> dict:
    urls = sorted({u.rstrip(".,;") for u in URL_RE.findall(text)})
    paths = sorted({p.rstrip(".,;") for p in WIN_PATH_RE.findall(text)})
    return {"urls": urls, "paths": paths}


def brief(session_id: str, task: str, tail: int = 12, max_chars: int = 40_000) -> str:
    """A handoff document: the task, then the source conversation's own words."""
    meta, rows = load(session_id)
    msgs = [m for m in messages(rows) if m["role"] in ("user", "assistant")]
    if tail > 0:
        msgs = msgs[-tail:]
    body: list[str] = []
    used = 0
    for msg in msgs:
        text = msg["text"].strip()
        if used + len(text) > max_chars:
            body.append(f"（后略，超出 {max_chars} 字符上限）")
            break
        used += len(text)
        who = "用户" if msg["role"] == "user" else "助手"
        body.append(f"### {_fmt_time(msg['ts'])} {who}\n\n{text}\n")
    refs = extract_refs("\n".join(m["text"] for m in msgs))
    ref_lines = []
    if refs["urls"]:
        ref_lines.append("链接：")
        ref_lines.extend(f"- {u}" for u in refs["urls"])
    if refs["paths"]:
        ref_lines.append("本地路径：")
        ref_lines.extend(f"- `{p}`" for p in refs["paths"])

    return "\n".join(
        [
            f"# 跨会话交接",
            "",
            f"- 主题：{task.strip().splitlines()[0][:80]}",
            f"- 生成时间：{_fmt_time(time.time())}",
            f"- 来源会话：{meta['id']}（{meta['title'] or '无标题'}）",
            f"- 来源工作目录：{meta['cwd']}",
            f"- 会话时间：{_fmt_time(meta['created_at'])} → {_fmt_time(meta['updated_at'])}",
            f"- 取自最近 {len(msgs)} 条对话（共 {meta['rows']} 条事件）",
            "",
            "## 要接的事",
            "",
            task,
            "",
            "## 来源会话原话（未改写）",
            "",
            "\n".join(body).strip(),
            "",
            "## 会话里出现过的材料",
            "",
            "\n".join(ref_lines) if ref_lines else "（没有提取到链接或本地路径）",
            "",
            "## 交接约定",
            "",
            "- 上面的原话是证据，不是总结；有冲突时以来源会话为准。",
            "- 目标会话要做的事写在「要接的事」里，不要再问一遍背景。",
            "- 需要补充的事实，直接读来源会话的工作目录或上面列出的路径。",
            "",
        ]
    )


@contextlib.contextmanager
def session_lock(session_id: str, stale_seconds: int = 3600):
    """One writer per session. A lock older than `stale_seconds` is reclaimed."""
    lock_dir = config.HOME / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{session_id}.lock"
    acquired = False
    for attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = time.time() - path.stat().st_mtime
            if age > stale_seconds and attempt == 0:
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            raise RuntimeError(
                f"session busy: {path.name} held for {int(age)}s"
            ) from None
        os.write(fd, f"{os.getpid()} {time.time()}".encode())
        os.close(fd)
        acquired = True
        break
    if not acquired:
        raise RuntimeError(f"could not lock {path.name}")
    try:
        yield path
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def write_brief(session_id: str, task: str, out: str = "", tail: int = 12) -> Path:
    text = brief(session_id, task, tail=tail)
    path = Path(out) if out else (
        Path.home() / ".nodus" / "briefs" / f"brief-{session_id[:20]}-{int(time.time())}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
