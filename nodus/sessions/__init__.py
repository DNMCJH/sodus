"""Sessions across vendors: list, read, continue, branch.

Four operations, one name each, whatever wrote the conversation:

    list       `nodus sessions`      every session on this machine + every run
    read       `nodus session <id>`  one normalised transcript
    continue   `nodus resume <id>`   add a turn to the same session
    branch     `nodus branch <id>`   start a new session from this history

Continue and branch are vendor features, not Nodus inventions:

    claude   --resume <id>  /  --resume <id> --fork-session
    codex    exec resume <id>  /  exec fork <id>
    dsh      --session-id <id>  /  no native fork: seed a new session with a brief

The last row is the honest one. A DSH branch is a *seeded* session: it starts
from a written handoff, not from the original message history. That is a
different thing from a fork and the report says so rather than pretending.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path

from .. import config, db
from . import claude, codex, dsh, model

__all__ = [
    "model", "claude", "codex", "dsh",
    "scan", "list_all", "find", "load", "transcript",
    "brief", "write_brief", "session_lock", "extract_refs", "plan",
]

SOURCES = {"claude": claude, "codex": codex, "dsh": dsh}

URL_RE = re.compile(r"https?://[^\s)\]）>\"']+")
WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"'<>|]+")

#: Vendors whose history only resumes inside the exact config home that wrote
#: it. DSH injects its endpoint through the environment, so it needs none.
NEEDS_KNOWN_HOME = ("claude", "codex")


# ---- query -------------------------------------------------------------


def _allowed(cwd: str) -> bool:
    """Optional whitelist: sessions must live under one of `session_roots`.

    Read access is the one place where a conversation from anywhere on the disk
    can surface, so the boundary is configurable rather than assumed.
    """
    roots = config.session_roots()
    if not roots:
        return True
    try:
        target = Path(cwd).resolve()
    except (OSError, ValueError):
        return False
    return any(target == root or root in target.parents for root in roots)


def scan(vendor: str = "") -> list[model.Session]:
    out: list[model.Session] = []
    for name, module in SOURCES.items():
        if vendor and name != vendor:
            continue
        try:
            found = module.scan()
        except Exception:  # noqa: BLE001 - one broken store must not hide the rest
            continue
        out.extend(s for s in found if _allowed(s.cwd))
    # A resumed Codex thread writes a second rollout file under the same id, so
    # the same conversation can appear several times. Keep the newest copy: it
    # is the one that holds the whole history.
    newest: dict[tuple[str, str], model.Session] = {}
    for session in out:
        key = (session.vendor, session.id)
        current = newest.get(key)
        if current is None or session.updated_at > current.updated_at:
            newest[key] = session
    return list(newest.values())


def run_index() -> dict[tuple[str, str], tuple[str, str]]:
    """(vendor, session id) -> (run id, task title) for sessions a run produced.

    The task title is the one label that describes the work rather than the
    harness: a run's first user turn is a template, so the file's own first
    line is identical in every run.
    """
    titles = {
        task["id"]: (task.get("title") or task.get("goal") or "")
        for task in db.list_tasks(limit=1000)
    }
    index: dict[tuple[str, str], tuple[str, str]] = {}
    for run in db.list_runs(limit=10000):
        try:
            ref = json.loads(run.get("session_ref_json") or "{}")
        except json.JSONDecodeError:
            continue
        sid = ref.get("session_id")
        if not sid:
            continue
        vendor = ref.get("provider") or run.get("agent") or ""
        title = titles.get(run.get("task_id") or "", "") or run.get("note") or ""
        index[(vendor, sid)] = (run["id"], title)
    return index


def attach_runs(sessions: list[model.Session]) -> list[model.Session]:
    index = run_index()
    out = []
    for session in sessions:
        run_id, title = index.get((session.vendor, session.id), ("", ""))
        if not run_id or session.run_id:
            out.append(session)
            continue
        out.append(
            model.Session(
                **{
                    **session.__dict__,
                    "run_id": run_id,
                    "title": title or session.title,
                }
            )
        )
    return out


def orphans(sessions: list[model.Session]) -> list[model.Session]:
    """Runs whose session file is gone, kept visible from the run record.

    Deleting a profile should not silently erase the fact that a conversation
    happened; the run row still holds the id, the cwd and the verdict.
    """
    known = {(s.vendor, s.id) for s in sessions}
    out: list[model.Session] = []
    for run in db.list_runs(limit=10000):
        try:
            ref = json.loads(run.get("session_ref_json") or "{}")
        except json.JSONDecodeError:
            continue
        sid = ref.get("session_id")
        vendor = ref.get("provider") or run.get("agent") or ""
        if not sid or (vendor, sid) in known:
            continue
        out.append(
            model.Session(
                vendor=vendor,
                id=sid,
                cwd=ref.get("cwd") or run.get("cwd") or "",
                title=f"(会话文件已不在) {run.get('note') or run['id']}",
                created_at=run.get("started_at") or 0,
                updated_at=run.get("ended_at") or run.get("started_at") or 0,
                path=Path(""),
                run_id=run["id"],
            )
        )
    return out


def list_all(
    query: str = "",
    limit: int = 30,
    vendor: str = "",
    source: str = "",
    include_orphans: bool = True,
) -> list[model.Session]:
    sessions = attach_runs(scan(vendor))
    if include_orphans:
        # An orphan is reconstructed from a run row, so it never went through
        # scan()'s vendor filter: without this, `--vendor dsh` would list a
        # claude run whose file is gone next to the dsh sessions it asked for.
        sessions.extend(s for s in orphans(sessions) if not vendor or s.vendor == vendor)
    if source:
        sessions = [s for s in sessions if s.source == source]
    if query:
        needle = query.lower()
        hit = []
        for session in sessions:
            if needle in (session.title or "").lower() or needle in session.cwd.lower():
                hit.append(session)
                continue
            text = "\n".join(m["text"] for m in messages_of(session)[:40])
            if needle in text.lower():
                hit.append(session)
        sessions = hit
    sessions.sort(key=lambda s: s.updated_at, reverse=True)
    return sessions[:limit]


def _norm(session_id: str) -> str:
    value = session_id.strip()
    if ":" in value:
        value = value.split(":", 1)[1]
    return value[8:] if value.startswith("session-") else value


def find(ref: str, vendor: str = "") -> model.Session:
    """Look up a session by id, unique prefix, or `vendor:id`.

    Only a *known* vendor prefix counts as a qualifier: DSH ids start with
    `session-`, so a naive split on ":" would read `session:...` as a vendor.
    """
    wanted_vendor = vendor
    head, sep, tail = ref.partition(":")
    if sep and head in SOURCES:
        wanted_vendor, ref = head, tail
    needle = _norm(ref)
    known = attach_runs(scan(wanted_vendor))
    candidates = known + orphans(known)
    exact = [s for s in candidates if _norm(s.id) == needle]
    if not exact:
        exact = [s for s in candidates if _norm(s.id).startswith(needle)]
    if not exact:
        raise ValueError(f"session not found: {ref} (run `nodus sessions`)")
    if len(exact) > 1:
        raise ValueError(
            "ambiguous session prefix: "
            + ", ".join(f"{s.vendor}:{s.id}" for s in exact[:5])
            + " — qualify it as vendor:id"
        )
    return exact[0]


# ---- read --------------------------------------------------------------


def messages_of(session: model.Session, include_reasoning: bool = False) -> list[dict]:
    if not session.path or not Path(session.path).is_file():
        return []
    module = SOURCES.get(session.vendor)
    if module is None:
        return []
    return module.messages_of(session, include_reasoning)

def load(ref: str, vendor: str = "") -> tuple[model.Session, list[dict]]:
    session = find(ref, vendor)
    return session, messages_of(session)


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"


def transcript(ref: str, tail: int = 40, include_reasoning: bool = False) -> str:
    session = find(ref)
    msgs = messages_of(session, include_reasoning)
    if not msgs and not Path(session.path).is_file():
        return (
            f"# {session.title}\n\n会话文件已经不在了（run {session.run_id}）。\n"
            "用 `nodus show <run>` 看这次执行的记录。\n"
        )
    if tail > 0:
        msgs = msgs[-tail:]
    lines = [
        f"# {session.title or session.id}",
        "",
        f"- 会话：{session.vendor}:{session.id}",
        f"- 端点：{session.origin}",
        f"- 工作目录：{session.cwd}",
        f"- 时间：{_fmt_time(session.created_at)} → {_fmt_time(session.updated_at)}",
        f"- 来源：{'Sodus run ' + session.run_id if session.run_id else '你自己的会话'}",
        f"- 消息：{sum(1 for m in msgs if m['role'] == model.USER)} user / "
        f"{sum(1 for m in msgs if m['role'] == model.ASSISTANT)} assistant（本页）",
        "",
    ]
    labels = {model.USER: "用户", model.ASSISTANT: "助手", model.REASONING: "思考"}
    for msg in msgs:
        lines.append(f"## {_fmt_time(msg['ts'])} {labels.get(msg['role'], msg['role'])}")
        lines.append("")
        lines.append(msg["text"].strip())
        lines.append("")
    return "\n".join(lines)


def extract_refs(text: str) -> dict:
    urls = sorted({u.rstrip(".,;") for u in URL_RE.findall(text)})
    paths = sorted({p.rstrip(".,;") for p in WIN_PATH_RE.findall(text)})
    return {"urls": urls, "paths": paths}


def brief(ref: str, task: str, tail: int = 12, max_chars: int = 40_000) -> str:
    """A handoff document: the task, then the source conversation's own words."""
    session = find(ref)
    msgs = [m for m in messages_of(session) if m["role"] in (model.USER, model.ASSISTANT)]
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
        who = "用户" if msg["role"] == model.USER else "助手"
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
            "# 跨会话交接",
            "",
            f"- 主题：{task.strip().splitlines()[0][:80]}",
            f"- 生成时间：{_fmt_time(time.time())}",
            f"- 来源会话：{session.vendor}:{session.id}（{session.title or '无标题'}）",
            f"- 来源工作目录：{session.cwd}",
            f"- 会话时间：{_fmt_time(session.created_at)} → {_fmt_time(session.updated_at)}",
            f"- 取自最近 {len(msgs)} 条对话（共 {session.events} 条事件）",
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


def write_brief(ref: str, task: str, out: str = "", tail: int = 12) -> Path:
    text = brief(ref, task, tail=tail)
    path = Path(out) if out else (
        config.HOME / "briefs" / f"brief-{_norm(ref)[:20]}-{int(time.time())}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---- continue / branch -------------------------------------------------


def _fork_plan(session: model.Session, profile: str, ref: str, note: str = "") -> dict:
    if session.vendor == "dsh":
        return {
            "session": session,
            "mode": "seed",
            "note": note or (
                "dsh 没有原生 fork，分支靠 brief 播种：新会话从这份交接开始，不是原历史的副本"
            ),
            "agent": session.vendor,
            "provider": "",
            "profile": profile or session.profile or config.active_profile(),
            "session_id": "",
            "source": ref,
        }
    return {
        "session": session,
        "mode": "fork",
        "note": note,
        "agent": session.vendor,
        "provider": session.provider,
        "profile": profile or session.profile or config.active_profile(),
        "session_id": session.id,
        "source": ref,
    }


def plan(
    ref: str,
    fork: bool = False,
    route: str = "",
    profile: str = "",
    in_place: bool = False,
    into: str = "",
) -> dict:
    """Everything a run needs to continue or branch this session.

    A session **you** wrote is never continued in place by default: it lives in
    the same store your own CLI reads, so appending a turn would show up in a
    conversation you are still having. Those fork (or, on DSH, get seeded),
    and `in_place=True` is the explicit override. A session a Nodus run wrote
    is our own record and is continued in place.

    `into` names a different vendor to hand the conversation to. That can only
    ever be a *seed*: a session id is resolved by the CLI that wrote it, out of
    its own store, so no vendor can resume another's conversation. What Nodus
    can do is read any of them and start a new one on the target vendor with a
    handoff as its first message.

    Raises with the reason when it cannot be honoured, instead of starting a run
    that would silently lose the history.
    """
    session = find(ref)
    target = into or session.vendor
    if target not in SOURCES:
        raise ValueError(f"unknown vendor: {into} (known: {', '.join(SOURCES)})")
    if target != session.vendor:
        return {
            "session": session,
            "mode": "seed",
            "cross": True,
            "note": (
                f"跨厂商交接：读 {session.vendor} 的对话，在 {target} 上开一个**新会话**。"
                "不是接续——会话 id 由写它的那个 CLI 解释，谁也没法续别人的"
            ),
            "agent": target,
            "provider": "",
            "profile": profile or config.active_profile(),
            "session_id": "",
            "source": ref,
        }

    if session.vendor in NEEDS_KNOWN_HOME and not session.provider:
        raise ValueError(
            f"{session.vendor}:{session.id} 在 {session.path.parent.parent} 下，"
            "不是 Sodus 建的（没有记录的端点）。"
            f"直接用它自己的 CLI 接：`{'claude --resume' if session.vendor == 'claude' else 'codex exec resume'} "
            f"{session.id}`"
        )

    if fork:
        return _fork_plan(session, profile, ref)
    if session.source == "user" and not in_place:
        return _fork_plan(
            session,
            profile,
            ref,
            note="这是你自己的会话：默认不往里写，改成从它分叉（要原地接续加 --in-place）",
        )
    return {
        "session": session,
        "mode": "resume",
        "note": "",
        "agent": session.vendor,
        "provider": session.provider,
        "profile": profile or session.profile or config.active_profile(),
        "session_id": session.id,
        "source": ref,
    }


# ---- one writer per session -------------------------------------------


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
            raise RuntimeError(f"session busy: {path.name} held for {int(age)}s") from None
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
