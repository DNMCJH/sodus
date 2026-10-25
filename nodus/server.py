"""HTTP API + SSE. One user, one machine: no auth beyond a loopback bind."""

from __future__ import annotations

import errno
import hmac
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import (
    budget as budget_mod,
    agents,
    config,
    db,
    jobs,
    ledger,
    llm,
    memory,
    observe,
    pet,
    profiles,
    propose,
    providers,
    recover,
    routes,
    runner,
    ui,
    verify,
)

MAX_BODY = 1_000_000
#: The HTTP contract lives in the repo root so editors and code generators can
#: find it without importing the package; the daemon also serves it verbatim.
SPEC_PATH = Path(__file__).resolve().parent.parent / "openapi.yaml"


def _log(message: str) -> None:
    """One line per event in `~/.nodus/daemon.log`.

    A detached daemon has nowhere to print, so an unexplained exit used to
    leave no evidence at all.
    """
    try:
        config.HOME.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with (config.HOME / "daemon.log").open("a", encoding="utf-8") as f:
            f.write(f"{stamp} {message}\n")
    except OSError:
        pass


def _is_disconnect(exc: BaseException | None) -> bool:
    """A client that hung up is not a server error.

    Browsers open the SSE stream and walk away; Windows reports the dead pipe as
    WinError 10053/10054, Python usually as one of the three ConnectionError
    subclasses. Logging a 20-line traceback for each one is what grew
    daemon.log past 6 MB. Anything that is not a disconnect keeps its traceback.
    """
    if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
        return True
    if getattr(exc, "winerror", None) in (10053, 10054):
        return True
    return getattr(exc, "errno", None) in (
        errno.ECONNABORTED,
        errno.ECONNRESET,
        errno.EPIPE,
    )


def authorized(header: str) -> bool:
    """Constant-time token check; a plain == leaks length/prefix timing."""
    return hmac.compare_digest(header or "", f"Bearer {config.token()}")


def body_allowed(length: int) -> bool:
    return 0 <= length <= MAX_BODY


def _usage(raw: str | None) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _day_start(now: float | None = None) -> tuple[float, str]:
    """Midnight in the operator's timezone.

    "Today" has to be their day: a UTC boundary would move the total under
    them at 08:00 local time.
    """
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return midnight, time.strftime("%Y-%m-%d", lt)


def _live_rows(conn, now: float) -> list[dict]:
    """Runs that are still going, with what has been recorded so far.

    `run.usage_json` is only written when a run finalises, so a live row has no
    price yet. What it does have is its `usage` events, which is a real record
    of what the CLI reported mid-flight — a lower bound, never presented as the
    final `_price()` number.
    """
    rows = conn.execute(
        "SELECT * FROM run WHERE workspace_id=? AND status IN ('queued','running')"
        " ORDER BY started_at",
        (config.workspace_id(),),
    ).fetchall()
    out = []
    for row in rows:
        run = dict(row)
        tokens = {"input": 0, "output": 0}
        reported: float | None = None
        for ev in conn.execute(
            "SELECT payload_json FROM event WHERE run_id=? AND type='usage'", (run["id"],)
        ):
            payload = _usage(ev["payload_json"])
            tokens["input"] += int(payload.get("input_tokens") or 0)
            tokens["output"] += int(payload.get("output_tokens") or 0)
            if isinstance(payload.get("cost"), (int, float)):
                reported = (reported or 0.0) + float(payload["cost"])
        out.append(
            {
                "id": run["id"],
                "agent": run.get("agent") or "",
                "route": run.get("route") or run.get("agent") or "",
                "provider": run.get("provider") or "",
                "cwd": run.get("cwd") or "",
                "note": run.get("note") or "",
                "status": run.get("status") or "",
                "started_at": run.get("started_at"),
                "tokens": tokens,
                "partial_cost": round(reported, 6) if reported is not None else None,
                # The CLI's own `total_cost_usd`; different basis from _price().
                "partial_currency": "USD" if reported is not None else "",
            }
        )
    return out


def _task_titles(task_ids: list[str]) -> dict[str, str]:
    """Task label per id, in one query.

    A run row records the instruction it was given, not what the task was
    called. The unverified list is scanned by eye and needs a label that tells
    43 rows apart; the goal is the fallback when the title is empty.
    """
    ids = [tid for tid in dict.fromkeys(task_ids) if tid]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    conn = db.connect()
    try:
        rows = conn.execute(
            f"SELECT id, title, goal FROM task WHERE id IN ({marks})", tuple(ids)
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, str] = {}
    for row in rows:
        title = str(row["title"] or "").strip()
        if not title:
            title = str(row["goal"] or "").strip().split("\n")[0].strip()
        out[row["id"]] = title
    return out


def _ended_at(run: dict) -> float:
    """When a run stopped; a run with no end time sorts by when it started."""
    ended = run.get("ended_at")
    return float(ended) if ended is not None else float(run.get("started_at") or 0)


def _unverified_rows(
    runs: list[dict], states: dict[str, str], titles: dict[str, str] | None = None
) -> list[dict]:
    """One row per completed run with no test evidence, newest first, capped.

    The page used to collapse these runs into a single number and offer one
    rerun button, which acted on whichever run happened to be newest. These
    rows are what lets the operator see the actual list and rerun exactly one.
    """
    titles = titles or {}
    rows = []
    for run in runs:
        if run.get("status") != "completed":
            continue
        if states.get(run["id"], "none") != "none":
            continue
        note = (run.get("note") or "").strip().split("\n")[0].strip()
        rows.append(
            {
                "id": run["id"],
                "task_id": run.get("task_id") or "",
                "title": (titles.get(run.get("task_id") or "") or note)[:120],
                "agent": run.get("agent") or "",
                "ended_at": _ended_at(run),
                "verify": "none",
            }
        )
    rows.sort(key=_ended_at, reverse=True)
    return rows[:50]


def _first_line(text) -> str:
    return str(text or "").strip().split("\n")[0].strip()


def _clip(text: str, n: int = 120) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= n else value[: n - 1].rstrip() + "…"


def _project(cwd) -> str:
    """Last path segment, whichever slash the platform wrote."""
    value = str(cwd or "").rstrip("/\\")
    return re.split(r"[\\/]", value)[-1] if value else ""


def _project_path(cwd, levels: int = 2) -> str:
    """The last `levels` directories, so two same-named checkouts stay apart.

    The project name alone is not a location: `research/2026-10-survey/
    nodus-removal` and `research/2026-09-survey/nodus-removal` read the same
    on a card and are different directories.
    """
    parts = [part for part in re.split(r"[\\/]+", str(cwd or "").strip()) if part]
    if len(parts) <= 1:
        return ""
    return "/".join(parts[-levels:])


def _task_facts(task_ids: list[str]) -> dict[str, dict]:
    """Title and goal per task, one query. The board names a task, not a run."""
    ids = [tid for tid in dict.fromkeys(task_ids) if tid]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    conn = db.connect()
    try:
        rows = conn.execute(
            f"SELECT id, title, goal FROM task WHERE id IN ({marks})", tuple(ids)
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, dict] = {}
    for row in rows:
        title = str(row["title"] or "").strip() or _first_line(row["goal"])
        out[row["id"]] = {"title": title, "goal": str(row["goal"] or "")}
    return out


def _handoff_change(run: dict) -> str | None:
    """The file list a failed run wrote into its handoff before giving up."""
    try:
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT path FROM handoff WHERE from_run_id=? ORDER BY created_at DESC LIMIT 1",
                (run["id"],),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        text = Path(row["path"]).read_text(encoding="utf-8")
    except (OSError, TypeError):
        return None
    if "## 改动文件" not in text:
        return None
    section = text.split("## 改动文件", 1)[1].split("\n## ", 1)[0]
    files = [
        line[2:].strip()
        for line in section.splitlines()
        if line.startswith("- ") and "（无改动）" not in line
    ]
    if not files:
        return None
    return f"改了 {len(files)} 个文件：{'、'.join(_project(p) for p in files[:3])}"


def _change_summary(run: dict) -> str | None:
    """What this run touched, in one phrase, or None when nothing is recorded.

    Proposal mode runs in a worktree, so the artifact paths are the only trace
    of the work once that branch is gone. A run that died before writing
    anything says so by having no change line, not by inventing one.
    """
    try:
        artifacts = db.artifacts_for(run["id"])
    except (KeyError, TypeError):
        return None
    files: list[str] = []
    for artifact in artifacts:
        if artifact.get("kind") != "file":
            continue
        path = str(artifact.get("rel_path") or artifact.get("path") or "")
        if path and path not in files:
            files.append(path)
    if not files:
        return _handoff_change(run)
    shown = "、".join(files[:3])
    return f"改了 {len(files)} 个文件：{shown}" + (" …" if len(files) > 3 else "")


# ---- evidence for a decision -------------------------------------------
#
# A decision row that says "执行失败" has not told the operator anything. Each
# of the four questions below is answered from a record that already exists;
# nothing here re-runs a worker, and a missing record is reported as missing
# instead of being guessed.

#: Original output a person can actually read. The full log stays one click
#: away; 200 characters is what fits on a card and what the requirement asks.
ERROR_TAIL_CHARS = 200

#: The five classes a failure can fall into, in the operator's words. Every
#: decision row names exactly one: "执行失败" is not a class, it is a shrug.
FAILURE_KINDS = ("进程退出", "超时", "测试红", "模型报错", "证据缺失", "无法归类")

#: The only things a person has to decide: merge a proposal (a value
#: judgement), pick a direction when the request is ambiguous, and spend money
#: past the budget. A failure is never one of them — retrying, verifying and
#: giving up are the machine's own work, and the board reports them as
#: statements ("我在处理", "已停止"), not as questions.
#:
#: `ambiguous` has no producer yet: tasks carry no question field, so nothing
#: writes it today. It stays in the tuple so that when one does, it lands here
#: as one more decision kind instead of being smuggled into the failure path.
DECISION_KINDS = ("proposal", "budget", "ambiguous")

#: Retry gates that are really the money question: "继续花，还是停？". These are
#: the only failure-shaped rows that still reach a person, and they are asked
#: as a spend decision, never as "执行失败，要不要重跑".
SPEND_GATES = ("budget", "cost-cap", "over-budget")

#: Same text, different question: "what kind of failure" is about the process,
#: "what probably caused it" is about the setup. Both read the same records.
_TIMEOUT_TEXT = re.compile(r"timed?[\s_-]?out|etimedout|\btimeout\b|no output for|超时", re.I)
_MODEL_TEXT = re.compile(
    r"model[\s_-]?not[\s_-]?found|unknown model|invalid model|no such model|"
    r"context[\s_-]?length|maximum context|too many tokens|"
    r"\b(?:401|403|429)\b|unauthoriz|forbidden|invalid[\s_-]*api[\s_-]*key|"
    r"rate[\s_-]?limit|quota|模型|密钥|鉴权",
    re.I,
)
_DEAD_TEXT = re.compile(
    r"orphaned|killed|sigterm|terminated|interrupted|exit code -?\d+|"
    r"traceback|error|exception|failed|denied|not found|no such file",
    re.I,
)

#: The run did not fail on its own: something outside killed it. The tool
#: output it left behind is the last thing it did, not the reason it stopped.
_PROCESS_DEATH = re.compile(r"orphaned|killed|sigterm|terminated|interrupted", re.I)

#: (pattern, the cause in three to eight characters). First match wins, so the
#: specific ones come before the generic network/exit ones. The label is a
#: *name*, not a sentence: it is the key `_CAUSE_ADVICE` looks the action up
#: by, so a label with a colon in it silently loses its advice. The sentence
#: is built around it, and the raw line it matched is quoted next to it.
_CAUSE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"No module named '?[\w.\-]+'?"), "依赖没装"),
    (re.compile(r"ModuleNotFoundError|ImportError|cannot import name"), "依赖或导入路径不对"),
    (
        re.compile(r"is not recognized as|command not found|不是内部或外部命令", re.I),
        "命令不在 PATH 里",
    ),
    (
        re.compile(r"no such file or directory|ENOENT|cannot find the path|"
                   r"系统找不到指定的(?:文件|路径)|路径不存在", re.I),
        "路径或环境不对",
    ),
    (
        re.compile(r"\b(?:401|403)\b|unauthoriz|forbidden|invalid[\s_-]*api[\s_-]*key|"
                   r"authentication|密钥|鉴权|未授权", re.I),
        "密钥无效或没有权限",
    ),
    (
        re.compile(r"rate[\s_-]?limit|\b429\b|too many requests|quota|限流", re.I),
        "被上游限流或额度用尽",
    ),
    (
        re.compile(r"context[\s_-]?length|maximum context|too many tokens", re.I),
        "上下文太长",
    ),
    (
        re.compile(r"model[\s_-]?not[\s_-]?found|unknown model|invalid model|"
                   r"no such model|模型[^\n。]{0,12}(?:不存在|不可用)", re.I),
        "模型名不对",
    ),
    (_TIMEOUT_TEXT, "卡住超时"),
    (
        re.compile(r"econnrefused|connection refused|could not connect", re.I),
        "目标服务没起",
    ),
    (
        re.compile(r"econnreset|connection reset|broken pipe|ssl handshake|dns|无法解析", re.I),
        "网络断了",
    ),
    (
        re.compile(r"\b5\d\d\b|internal server error|bad gateway|service unavailable", re.I),
        "上游服务出错",
    ),
    (
        re.compile(r"orphaned|killed|sigterm|terminated|interrupted", re.I),
        "进程被外部杀掉",
    ),
    # A process that exits non-zero and prints nothing is still a class of
    # failure; "看不出" is a worse answer than "它自己退了，没留话".
    (re.compile(r"exit code -?\d+", re.I), "进程自己退出"),
]

#: One board render asks the same question about the same run more than once
#: (a decision row, the history row, the log endpoint). A per-thread cache
#: cleared at the start of each request keeps that from being dozens of sqlite
#: connections and one `git diff` per card.
_REQUEST = threading.local()


def _fresh_cache() -> None:
    _REQUEST.data = {}


def _memo(key, build):
    data = getattr(_REQUEST, "data", None)
    if data is None:
        data = {}
        _REQUEST.data = data
    if key not in data:
        data[key] = build()
    return data[key]

_FAILED_LINE = re.compile(r"^FAILED\s+(\S+?)(?:\s+-\s+(.*))?$", re.M)
_TEST_HEAD = re.compile(r"^_{3,}\s+(.+?)\s+_{3,}$")


def _tail(text: str, n: int = ERROR_TAIL_CHARS) -> str:
    value = str(text or "").rstrip()
    return value if len(value) <= n else "…" + value[-n:]


def _repo_rel(path: str, cwd: str) -> str:
    """A file path relative to the run's directory, for a card, not a log."""
    value = str(path or "")
    if not value:
        return ""
    try:
        return str(Path(value).resolve().relative_to(Path(cwd).resolve())).replace("\\", "/")
    except (OSError, ValueError):
        return _project(value)


def _events(run_id: str, prefixes: tuple[str, ...]) -> list[dict]:
    def load() -> list[dict]:
        try:
            return db.events_filtered(run_id, prefixes=prefixes)
        except (KeyError, TypeError, ValueError):
            return []

    return _memo(("events", run_id, prefixes), load)


def _last_action(run: dict) -> dict:
    """The worker's last tool call: which file it edited, which command it ran."""
    rows = _events(str(run.get("id") or ""), ("tool_call",))
    if not rows:
        return {}
    event = rows[-1]
    payload = event.get("payload") or {}
    name = str(payload.get("name") or "tool")
    args = payload.get("input")
    args = args if isinstance(args, dict) else {}
    path = str(args.get("filePath") or args.get("file_path") or args.get("path") or "")
    command = str(args.get("command") or "")
    if path:
        line = f"{name} {_repo_rel(path, str(run.get('cwd') or ''))}"
    elif command:
        line = f"{name}：{_clip(command, 100)}"
    else:
        query = str(args.get("pattern") or args.get("query") or "")
        line = f"{name} {_clip(query, 70)}".strip()
    started = float(run.get("started_at") or 0)
    ts = float(event.get("ts") or 0)
    at = f"（第 {max(0, int(ts - started))} 秒）" if started and ts else ""
    return {"name": name, "line": line, "text": f"最后一次动作：{line}{at}"}


def _error_evidence(run: dict) -> dict:
    """The raw tail a person reads, newest and most specific record first.

    Two records can look like "the error": the last tool output that returned
    non-zero, and the reason the run was declared failed. When the process was
    killed, the tool output is what it was doing, not why it stopped — showing
    a paragraph of skill documentation as the error is worse than showing
    nothing, so the death reason wins there.
    """
    run_id = str(run.get("id") or "")
    events = _events(run_id, ("error", "tool_result", "run.failed"))
    death = _PROCESS_DEATH.search(str(run.get("error") or ""))
    if death:
        for event in reversed(events):
            if event.get("type") == "run.failed":
                text = str((event.get("payload") or {}).get("error") or "")
                if text.strip():
                    return {"tail": _tail(text), "source": "run.failed 事件（进程被杀）"}
        return {"tail": _tail(str(run.get("error") or "")), "source": "run.error（进程被杀）"}
    for event in reversed(events):
        payload = event.get("payload") or {}
        if event.get("type") == "tool_result" and payload.get("ok") is False:
            text = str(payload.get("text") or "")
            # Not every failed tool prints an error: some print usage or a
            # help page. Those are context, not the cause.
            if text.strip() and _DEAD_TEXT.search(text):
                return {"tail": _tail(text), "source": "最后一次失败的工具输出"}
    for event in reversed(events):
        if event.get("type") == "error":
            text = str((event.get("payload") or {}).get("message") or "")
            if text.strip():
                return {"tail": _tail(text), "source": "agent 报错事件"}
    for event in reversed(events):
        if event.get("type") == "run.failed":
            text = str((event.get("payload") or {}).get("error") or "")
            if text.strip():
                return {"tail": _tail(text), "source": "run.failed 事件"}
    text = str(run.get("error") or "")
    if text.strip():
        return {"tail": _tail(text), "source": "run.error"}
    return {}


def _failed_tests(text: str) -> list[dict]:
    """Names and assertion summaries out of a pytest-style summary.

    The short summary line is what pytest prints first and it can be truncated
    to the terminal width. The traceback block right above it carries the real
    assertion; this joins the two by test function name.
    """
    if not text:
        return []
    assertions: dict[str, str] = {}
    current = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        head = _TEST_HEAD.match(line.strip())
        if head:
            current = head.group(1).strip()
            continue
        if current and line.startswith("E ") and current not in assertions:
            assertions[current] = line[2:].strip()
    out: list[dict] = []
    for node, summary in _FAILED_LINE.findall(text)[:8]:
        func = node.split("::")[-1].split("[")[0]
        assertion = assertions.get(func) or (summary or "").strip()
        out.append({"name": node, "test": func, "assertion": _clip(assertion, 160)})
    return out


def _verify_red(run_id: str) -> dict:
    """The last red verification of this run, with its failed test names."""
    for event in reversed(_events(run_id, ("verify",))):
        if event.get("type") != "verify.red":
            continue
        payload = event.get("payload") or {}
        body = str(payload.get("tail") or "")
        path = str(payload.get("path") or "")
        if path:
            try:
                body = Path(path).read_text(encoding="utf-8", errors="replace")[-60_000:]
            except OSError:
                pass
        return {
            "cmd": payload.get("cmd") or [],
            "exit": payload.get("exit"),
            "tests": _failed_tests(body),
            "tail": _tail(body),
        }
    return {}


#: A git diff for a directory is stable enough to reuse across requests for a
#: few seconds: the card describes a run that already ended, and the
#: 5-repository worst case was ~1.6s in git subprocesses alone (board was 10s
#: before this, measured 2026-10-25). Keyed by cwd; a user editing files gets
#: at most this much stale line counts on old cards.
_NUMSTAT_TTL = 30.0
_NUMSTAT: dict[str, tuple[float, dict[str, tuple[int, int]]]] = {}


def _git_numstat_all(cwd: str) -> dict[str, tuple[int, int]]:
    """One `git diff --numstat` for the whole work tree, cached per request and
    briefly across requests.

    HEAD covers staged and unstaged changes; the fallback covers a repository
    with no commit yet. Running it once per directory and filtering in Python
    is the difference between one subprocess and one per decision row.
    """
    if not cwd:
        return {}
    now = time.time()
    hit = _NUMSTAT.get(cwd)
    if hit is not None and now - hit[0] < _NUMSTAT_TTL:
        return hit[1]
    stats = _read_git_numstat(cwd)
    _NUMSTAT[cwd] = (now, stats)
    return stats


def _read_git_numstat(cwd: str) -> dict[str, tuple[int, int]]:
    for base in (["HEAD"], []):
        args = ["git", "-C", cwd, "diff", "--numstat", *base]
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                creationflags=config.NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        if proc.returncode != 0:
            continue
        out: dict[str, tuple[int, int]] = {}
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            left, right, path = parts
            if left == "-" or right == "-":
                continue
            try:
                out[path.replace("\\", "/")] = (int(left), int(right))
            except ValueError:
                continue
        return out
    return {}


def _diff_stat(rows: list[dict], limit: int = 5) -> str:
    """A `git diff --stat` style line per file, for a card, not a terminal."""
    parts = []
    for row in rows[:limit]:
        added = int(row.get("added") or 0)
        removed = int(row.get("removed") or 0)
        parts.append(f"{_project(str(row.get('path') or ''))} | +{added} −{removed}")
    if len(rows) > limit:
        parts.append(f"… 另有 {len(rows) - limit} 个文件")
    return "；".join(parts)


def _git_numstat(cwd: str, files: list[str]) -> dict:
    """Line counts for the files this run touched, from the cached diff."""
    if not cwd or not files:
        return {}
    stats = _memo(("numstat", cwd), lambda: _git_numstat_all(cwd))
    if not stats:
        return {}
    added = removed = 0
    seen: list[str] = []
    rows: list[dict] = []
    for path in files:
        key = str(path or "").replace("\\", "/").lstrip("/")
        while key.startswith("./"):
            key = key[2:]
        match = stats.get(key)
        if match is None:
            for candidate, value in stats.items():
                if candidate.endswith("/" + key) or key.endswith("/" + candidate):
                    match = value
                    break
        if match is None:
            continue
        added += match[0]
        removed += match[1]
        seen.append(key)
        rows.append({"path": key, "added": match[0], "removed": match[1]})
    if not seen:
        return {}
    return {
        "files": seen,
        "added": added,
        "removed": removed,
        "rows": rows,
        "stat": _diff_stat(rows),
    }


def _change_evidence(run: dict, deep: bool = True) -> dict:
    """Which files this run changed, and — when git can still see them — lines."""
    run_id = str(run.get("id") or "")

    def load_artifacts() -> list[dict]:
        try:
            return db.artifacts_for(run_id)
        except (KeyError, TypeError):
            return []

    files: list[str] = []
    for artifact in _memo(("artifacts", run_id), load_artifacts):
        if artifact.get("kind") != "file":
            continue
        path = str(artifact.get("rel_path") or "") or _repo_rel(
            str(artifact.get("path") or ""), str(run.get("cwd") or "")
        )
        if path and path not in files:
            files.append(path)

    # A proposal is the one place the exact per-file line counts survive: the
    # branch and its numstat are recorded when the run ends.
    proposals = (
        _memo(("proposals", run_id), lambda: db.proposals_for_run(run_id))
        if run_id
        else []
    )
    if proposals:
        record = proposals[0]
        rows = [f for f in (record.get("files") or []) if isinstance(f, dict)]
        added = sum(int(f.get("added") or 0) for f in rows)
        removed = sum(int(f.get("removed") or 0) for f in rows)
        clean = [
            {
                "path": str(f.get("path") or ""),
                "added": int(f.get("added") or 0),
                "removed": int(f.get("removed") or 0),
            }
            for f in rows
            if f.get("path")
        ]
        return {
            "files": [row["path"] for row in clean],
            "added": added,
            "removed": removed,
            "rows": clean,
            "stat": str(record.get("stat") or _diff_stat(clean)),
            "basis": "git diff --numstat（提案分支）",
        }
    if not files:
        return {"files": [], "basis": "", "missing": "这次 run 没有记录改动文件（artifact 为空）"}
    if deep:
        counted = _git_numstat(str(run.get("cwd") or ""), files)
        if counted:
            return {
                **counted,
                "basis": "git diff --numstat（只统计这次 run 碰过的文件）",
            }
    return {
        "files": files,
        "basis": "",
        "missing": "行数没有记录（不是 git 仓库、改动已提交，或是历史条目）",
    }


def _outcome(run: dict, verify: str) -> str:
    """One token per attempt: the format the operator asked for."""
    if verify == "red":
        return "tests_red"
    if verify == "ok":
        return "ok"
    cause = recover.cause(run)
    if cause in {
        "timeout", "orphaned", "network", "rate_limit", "upstream",
        "auth", "param", "policy", "model",
    }:
        return cause
    error = str(run.get("error") or "")
    match = re.search(r"exit code (-?\d+)", error)
    if match:
        return f"exit {match.group(1)}"
    if error.strip():
        return _clip(_first_line(error), 28)
    return str(run.get("status") or "failed")


def _attempt_rows(run: dict) -> list[dict]:
    """The automatic retries already spent in this chain, one row each."""
    rows = recover.chain(run)
    if not rows:
        return []
    ids = tuple(row["id"] for row in rows)
    states = _memo(("verify", ids), lambda: db.verify_states(list(ids)))
    out: list[dict] = []
    for row in rows:
        if not recover.is_retry(row):
            continue
        started = float(row.get("started_at") or 0)
        ended = float(row.get("ended_at") or 0)
        seconds = int(round(ended - started)) if started and ended >= started else None
        out.append(
            {
                "n": recover.retry_number(row) or len(out) + 1,
                "seconds": seconds,
                "outcome": _outcome(row, states.get(row["id"], "none")),
                "run": str(row.get("id") or ""),
            }
        )
    out.sort(key=lambda item: item["n"])
    return out


def _evidence(run: dict, state: str = "none", deep: bool = True) -> dict:
    """Everything a reader needs to judge this failure without opening a log."""
    run_id = str(run.get("id") or "")
    action = _last_action(run)
    error = _error_evidence(run)
    red = _verify_red(run_id) if state == "red" else {}
    tests = red.get("tests") or []
    change = _change_evidence(run, deep=deep)
    attempts = _attempt_rows(run) if deep else []
    missing: list[str] = []
    if not action:
        missing.append("没有工具调用记录（event 表里没有这个 run 的 tool_call）")
    if not tests and not error.get("tail"):
        missing.append("没有原始报错（run.error 也是空的）")
    if change.get("missing"):
        missing.append(change["missing"])
    if state == "none" and str(run.get("status") or "") == "completed":
        missing.append("没有测试证据（verify 事件里既没有 green 也没有 red）")
    tail = error.get("tail") or ""
    source = error.get("source") or ""
    if not tail and red.get("tail"):
        tail, source = red["tail"], "测试输出末尾"
    return {
        "action": action.get("text") or "",
        "error_tail": tail,
        "error_source": source,
        "tests": tests,
        "test_cmd": " ".join(str(part) for part in (red.get("cmd") or [])),
        "test_exit": red.get("exit"),
        "change": change,
        "attempts": attempts,
        "missing": missing,
    }


def _assertion_phrase(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"^(?:AssertionError|assertionerror)\s*:\s*", "", value)
    value = re.sub(r"^assert\s+", "", value)
    return _clip(value, 90)


def _error_phrase(text: str) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    for line in reversed(lines):
        match = re.search(r"exit code (-?\d+)", line, re.I)
        if match:
            # An exit code is not a sentence; a card that says "exit code 1"
            # makes the reader translate it.
            return f"进程退出码 {match.group(1)}"
        if re.search(
            r"(?:Error|Exception|FAILED|failed|denied|not found|timed out|超时|失败)", line
        ):
            return _clip(line, 100)
    return _clip(lines[-1], 100) if lines else ""


def _evidence_gap(run: dict, evidence: dict) -> str:
    """The sentence for a run that predates the evidence records.

    A decision row built from an empty record must say the record is empty.
    Falling back to "执行失败" hides the fact that nobody can tell what
    happened; saying so is the honest version of the same sentence.

    What counts as evidence is an *operation*: a tool call, a test result, or a
    changed file. A `run.failed` lifecycle event is not — a legacy run carries
    one and still tells the reader nothing, which is exactly the case this
    sentence exists for.
    """
    run_id = str(run.get("id") or "")
    if _events(run_id, ("tool_call", "tool_result", "error")):
        return ""
    change = evidence.get("change") or {}
    if evidence.get("tests") or change.get("files"):
        return ""
    return "证据缺失：这次运行没有记录 tool_call / 改动 / 测试输出"


def _failure_kind(run: dict, evidence: dict, state: str, gap: str = "") -> str:
    """One of FAILURE_KINDS. This is the line the operator reads second.

    A run with no tool_call, no change and no test is classified as 证据缺失
    even when it left a one-line error: the error says how it stopped, not
    what it was doing. The recognisable part still shows up in the headline,
    the likely cause and the advice, so nothing is lost.
    """
    if state == "red" or (evidence.get("tests") or []):
        return "测试红"
    if gap:
        return "证据缺失"
    text = " ".join(
        str(part or "")
        for part in (evidence.get("error_tail"), run.get("error"), evidence.get("action"))
    )
    if not text.strip():
        return "证据缺失"
    if _TIMEOUT_TEXT.search(text):
        return "超时"
    if _MODEL_TEXT.search(text):
        return "模型报错"
    if _DEAD_TEXT.search(text):
        return "进程退出"
    return "无法归类"


def _cause_snippet(text: str) -> str:
    """The raw line the guess came from, short enough to sit in a sentence."""
    return _clip(" ".join(str(text or "").split()), 80)


def _likely_cause(run: dict, evidence: dict, state: str, gap: str = "") -> str:
    """What probably went wrong, from the error string alone.

    Rule-based on purpose: this runs on every board render, and a model call
    per row would make the board cost money and be slow. The wording says it
    is a guess ("大概率是") and shows the line it came from, so a wrong guess
    is visibly a guess. A run with no operations recorded falls back to saying
    so — but only after the error text has had its chance, because a legible
    `ModuleNotFoundError` is a real cause even in a run with no tool_call.
    """
    tests = evidence.get("tests") or []
    if state == "red" or tests:
        first = tests[0] if tests else {}
        name = str(first.get("test") or first.get("name") or "")
        assertion = _assertion_phrase(str(first.get("assertion") or ""))
        if name and assertion:
            return f"大概率是改动没做到位：{name} 的断言没过（{assertion}）"
        if name:
            return f"大概率是改动没做到位：{name} 没过"
        return "大概率是改动没做到位：测试跑完了但没通过"
    text = " ".join(
        str(part or "") for part in (evidence.get("error_tail"), run.get("error"))
    )
    for pattern, label in _CAUSE_RULES:
        match = pattern.search(text)
        if not match:
            continue
        return f"大概率是{label}（报错是 {_clip(match.group(0).strip(), 80)}）"
    if gap:
        return "推断不了：旧记录里没有 tool_call、也没有报错原文，只剩一个状态。"
    if text.strip():
        return f"看不出已知的错误模式，要读日志原文才知道：{_cause_snippet(text)}"
    return "没有报错原文，推断不了。"


def _attempt_phrase(attempts: list[dict]) -> str:
    """`第1次 42s tests_red / 第2次 38s timeout`, the format the operator asked
    for. Shared by the retry line and the stopped row so the two cannot drift.
    """
    parts = []
    for item in attempts:
        seconds = item.get("seconds")
        took = f"{seconds}s" if isinstance(seconds, (int, float)) else "时长没记"
        parts.append(f"第{item.get('n')}次 {took} {item.get('outcome') or '结果没记'}")
    return " / ".join(parts)


def _retry_line(evidence: dict, kind: str, why: str = "", plan: dict | None = None) -> str:
    """How many times it already tried, and what each try returned.

    "未重试" is a fact, not a blank: the reason it never retried is on the same
    line, because "no retries" and "retries were refused" are different
    situations for the person deciding what to do next.
    """
    attempts = evidence.get("attempts") or []
    if attempts:
        line = f"自动重试 {len(attempts)} 次：" + _attempt_phrase(attempts)
        limit = (plan or {}).get("limit")
        if isinstance(limit, int) and limit:
            line += f"（上限 {limit} 次）"
        return line
    reason = str(why or "").strip()
    if kind == "测试红" and not reason:
        reason = "它已经跑完了，问题出在验收结果，重试不会改代码"
    if not reason:
        reason = "没有重试记录：这条失败没有进入自动恢复，或者记录里没存重试历史"
    return f"未重试（原因：{reason}）"


def _headline(
    run: dict,
    evidence: dict,
    problem: str,
    state: str,
    plan: dict | None = None,
    gate: str = "",
) -> str:
    """The what-broke phrase, without the retry/gate framing.

    The framing (why the machine stopped, how many tries) has its own line
    now; duplicating it here is how the same card ends up saying "连续两次同样
    的错" on three different failures.
    """
    tests = evidence.get("tests") or []
    if tests:
        first = tests[0]
        assertion = _assertion_phrase(str(first.get("assertion") or ""))
        phrase = str(first.get("test") or first.get("name") or "测试")
        if assertion:
            phrase += f" 断言 {assertion}"
        if len(tests) > 1:
            phrase += f"，共 {len(tests)} 条失败"
        return phrase
    if evidence.get("error_tail"):
        return _error_phrase(evidence["error_tail"])
    match = re.search(r"exit code (-?\d+)", str(run.get("error") or ""))
    if match:
        return f"进程退出码 {match.group(1)}"
    return problem or recover.problem_text(run)


def _change_line(change: dict) -> str | None:
    files = [str(path) for path in (change.get("files") or []) if path]
    if not files:
        return None
    shown = "、".join(_project(path) for path in files[:3]) + (" …" if len(files) > 3 else "")
    if change.get("added") is not None and change.get("removed") is not None:
        return f"{len(files)} 个文件 +{change['added']} −{change['removed']}：{shown}"
    return f"改了 {len(files)} 个文件：{shown}"


#: What to actually do, per likely cause. Every one of these names an action;
#: none of them says "由你决定". The second half is the escape hatch for the
#: operator who would rather fix it than spend another run. The keys are the
#: labels in `_CAUSE_RULES`; a test pins the two tables together so a renamed
#: label cannot silently lose its advice.
_CAUSE_ADVICE: dict[str, str] = {
    "依赖没装": "先把依赖装上（{module}），再点「重跑」；不想装就自己改掉这个 import，再点「重跑」。",
    "依赖或导入路径不对": "先确认这个包装在跑 worker 的那个解释器里，再点「重跑」；路径的问题直接自己改更快。",
    "命令不在 PATH 里": "先把这条命令装进 PATH（或改成绝对路径），再点「重跑」；临时要做就自己接手跑。",
    "路径或环境不对": "先确认 cwd 和文件都在（{cwd}），再点「重跑」；路径本身错了就直接自己改。",
    "密钥无效或没有权限": "换上有效的密钥或补权限，再点「重跑」；也可以换 codex 路由重试，或者自己接手。",
    "被上游限流或额度用尽": "等几分钟再点「重跑」；一直 429 就换一条路由重试，或自己接手。",
    "上下文太长": "把这个任务拆小再派，或换一个能吃更长上下文的路由重试。",
    "模型名不对": "换一个可用模型（或换路由）再点「重跑」；模型名要在配置里改。",
    "卡住超时": "先看日志最后停在哪一步，再点「重跑」；每次都卡在同一个地方就换 codex 路由重试一次，或自己接手。",
    "目标服务没起": "先把要连的服务起起来，再点「重跑」；连不上就别重试，直接自己查。",
    "网络断了": "网络恢复后点「重跑」；反复断就换一条路由重试。",
    "上游服务出错": "等几分钟再点「重跑」；同一个 5xx 反复出现就自己接手。",
    "进程被外部杀掉": "先确认 daemon 没在重启、机器没休眠，再点「重跑」；总在同一个秒数附近死就换 codex 路由重试，或自己接手。",
    "进程自己退出": "它自己退了但没打印报错：先点「看日志」翻完整输出，再决定重跑；找不到原因就自己接手。",
}

_MODULE_NAME = re.compile(r"No module named '?([\w.\-]+)'?")

#: The same labels as `_CAUSE_ADVICE`, as a *noun phrase* instead of an order.
#: A stopped row is a report of what happened, so its cause line says what
#: would have to change ("要解决得装上缺的依赖"), not what the reader must do.
_CAUSE_FIX: dict[str, str] = {
    "依赖没装": "把缺的包装进跑 worker 的那个解释器",
    "依赖或导入路径不对": "确认那个包在正确的解释器里",
    "命令不在 PATH 里": "把命令装进 PATH，或写成绝对路径",
    "路径或环境不对": "先确认 cwd 和文件都在",
    "密钥无效或没有权限": "换上有效的密钥或补权限",
    "被上游限流或额度用尽": "等限流过去，或换一条路由",
    "上下文太长": "把任务拆小，或换一条长上下文路由",
    "模型名不对": "改配置里的模型名",
    "卡住超时": "换一条路由再试，或换个做法",
    "目标服务没起": "先把要连的服务起起来",
    "网络断了": "等网络恢复，或换一条路由",
    "上游服务出错": "等一会，或换一条路由",
    "进程被外部杀掉": "确认 daemon 没有在重启、机器没有休眠",
    "进程自己退出": "读完整日志定位它退在哪一步",
}


def _cause_label(cause: str) -> str:
    """The bare label (`依赖没装`) out of `大概率是依赖没装（报错是 …）`."""
    label = str(cause or "").split("（", 1)[0]
    return label.replace("大概率是", "", 1).strip()


def _cause_fix(cause: str) -> str:
    """`；要解决得…` for a stopped row, or "" when the cause is not a label."""
    fix = _CAUSE_FIX.get(_cause_label(cause))
    return f"；要解决得{fix}" if fix else ""


def _advice_for(kind: str, cause: str, evidence: dict, cwd: str = "") -> str:
    """The 建议 line: one concrete action, plus what to do instead.

    The cause wins over the missing-evidence case: a run with no tool_call but
    a legible `401` should be told to change the key, not to "rerun for
    evidence".
    """
    template = _CAUSE_ADVICE.get(_cause_label(cause))
    if template:
        module = ""
        match = _MODULE_NAME.search(str(evidence.get("error_tail") or ""))
        if match:
            module = f"pip install {match.group(1)} 之类"
        return template.format(module=module or "装好缺的那个包", cwd=cwd or "这个目录")
    if kind == "证据缺失":
        return (
            "这次运行什么都没记下来：直接点「重跑」重新跑一遍拿到新证据，"
            "或者自己翻日志（旧记录补不回来）。"
        )
    return ""


def _next_step(
    evidence: dict,
    fallback: str,
    plan: dict | None = None,
    gate: str = "",
    kind: str = "",
    cause: str = "",
    cwd: str = "",
) -> str:
    """The suggestion, built from what actually happened.

    "由你决定" is not a suggestion. A failed test names the test and the
    assertion; a failed tool call names the file or command; a money gate
    names the number to raise and the number it would cost. When the record
    has none of that, the honest next step is to read the first error line.
    """
    plan = plan or {}
    if gate == "budget":
        remaining = plan.get("remaining")
        estimate = plan.get("estimate")
        left = f"¥{remaining:.2f}" if isinstance(remaining, (int, float)) else "当前额度"
        cost = f"一次约 ¥{estimate:.2f}" if isinstance(estimate, (int, float)) else "一次重试"
        return (
            f"确认值得继续：把 NODUS_DAILY_BUDGET（或任务 budget_json.max_cost）"
            f"在 {left} 之上再加 {cost}，然后点「重跑」。不想花就自己接手改。"
        )
    if gate == "cost-cap":
        estimate = plan.get("estimate")
        cap = plan.get("cost_cap")
        return (
            f"这次重试预计 ¥{estimate:.2f}，超过自动上限 ¥{cap:.2f}。"
            "先看「试过什么」判断值不值：值得就点「重跑」手动批准，"
            "不值得就自己接手改。"
        )
    action_advice = _advice_for(kind, cause, evidence, cwd=cwd)
    if action_advice:
        return action_advice
    tests = evidence.get("tests") or []
    if tests:
        first = tests[0]
        assertion = _assertion_phrase(str(first.get("assertion") or ""))
        name = str(first.get("test") or first.get("name") or "失败的测试")
        suffix = f"（断言是 {assertion}）" if assertion else ""
        return f"先修 {name}{suffix}，改完点「重跑」让它只再跑这一条任务。"
    action = str(evidence.get("action") or "")
    if action:
        step = re.sub(r"^最后一次动作：", "", action)
        step = re.sub(r"（第 \d+ 秒）$", "", step).strip()
        return (
            f"先手工复现「{step}」这一步，确认它卡在哪；"
            "改好配置或代码后点「修好后重跑」。"
        )
    if evidence.get("error_tail"):
        return (
            "没有工具调用记录，先看日志里第一条报错确认失败发生在启动阶段，"
            "再决定换 route/model 还是自己接手。"
        )
    return fallback


def _task_label(task: dict, run: dict) -> str:
    return (
        str(task.get("title") or "").strip()
        or _first_line(task.get("goal"))
        or _first_line(run.get("note"))
        or "（任务没写标题）"
    )


def _run_entry(
    run: dict,
    facts: dict[str, dict],
    problem: str = "",
    suggestion: str = "",
    *,
    state: str = "none",
    headline: str = "",
    evidence: bool = True,
    deep: bool = True,
    kind: str = "failed",
    plan: dict | None = None,
    why: str = "",
    gate: str = "",
) -> dict:
    """One decision row: which project, which task, what broke, what it cost.

    `problem` stays a one-sentence judgement (used by the log view and the old
    API consumers); `headline` is what broke, `failure_kind` is which of the
    five classes it belongs to, `likely_cause` is the rule-based guess and
    `retry_line` is how many tries already happened. Those four plus
    `suggestion` are the five questions every decision row must answer; none
    of them is "由你决定", and an empty record says "证据缺失" instead of
    degrading into a generic sentence.
    """
    tid = str(run.get("task_id") or "")
    task = facts.get(tid) or {}
    cwd = str(run.get("cwd") or "")
    issue = problem or recover.problem_text(run)
    facts_ev = _evidence(run, state, deep=deep) if evidence else {}
    gap = _evidence_gap(run, facts_ev) if evidence else ""
    failure_kind = _failure_kind(run, facts_ev, state, gap=gap)
    likely_cause = _likely_cause(run, facts_ev, state, gap=gap)
    if plan:
        gate = gate or str(plan.get("gate") or "")
        why = why or str(plan.get("why") or "")
    phrase = headline or _headline(run, facts_ev, issue, state, plan=plan, gate=gate)
    if gap:
        # The gap sentence is the headline: it is the only thing we know for
        # sure. Whatever one-line error survived goes in brackets after it.
        phrase = gap + (f"（只留下：{phrase}）" if phrase else "")
    elif phrase and failure_kind not in phrase:
        phrase = f"{failure_kind}：{phrase}"
    advice = suggestion or recover.suggestion_text(run)
    if not suggestion and facts_ev and str(run.get("status") or "") == "failed":
        advice = _next_step(
            facts_ev,
            advice,
            plan=plan,
            gate=gate,
            kind=failure_kind,
            cause=likely_cause,
            cwd=cwd,
        )
    change = _change_line(facts_ev.get("change") or {}) if facts_ev else _change_summary(run)
    retry_view = None
    if plan:
        retry_view = {
            "attempt": plan.get("attempt"),
            "limit": plan.get("limit"),
            "mode": plan.get("mode") or "",
            "estimate": plan.get("estimate"),
            "remaining": plan.get("remaining"),
            "cost_cap": plan.get("cost_cap"),
            "redo": plan.get("redo") or {},
        }
    return {
        "kind": kind,
        "id": str(run.get("id") or ""),
        "run": str(run.get("id") or ""),
        "at": run.get("ended_at") or run.get("started_at"),
        "project": _project(cwd),
        "project_path": _project_path(cwd),
        "cwd": cwd,
        "task_id": tid,
        "task_title": _task_label(task, run),
        "goal": _clip(task.get("goal") or ""),
        "cause": recover.cause(run),
        "failure_kind": failure_kind,
        "problem": issue,
        "headline": phrase or issue,
        "likely_cause": likely_cause,
        "retry_line": _retry_line(facts_ev, failure_kind, why=why, plan=plan),
        "evidence_missing": gap,
        "change": change,
        "suggestion": advice,
        "why": why or (
            recover.why_not_retried(run)
            if recover.classify(run) == "deterministic"
            else ""
        ),
        "gate": gate,
        "retry": retry_view,
        "evidence": facts_ev,
        "title": issue,
        "detail": str(run.get("error") or "")[:160],
        "note": "",
    }


def _proposal_entry(proposal: dict, facts: dict[str, dict]) -> dict:
    run = db.get_run(proposal.get("run_id") or "") if proposal.get("run_id") else {}
    tid = str(proposal.get("task_id") or run.get("task_id") or "")
    task = facts.get(tid) or {}
    repo = str(proposal.get("repo") or run.get("cwd") or "")
    files = [str(item.get("path") or "") for item in (proposal.get("files") or [])]
    stat = str(proposal.get("stat") or "")
    return {
        "kind": "proposal",
        "decision": "merge",
        "question": "这个改动要不要合进主工作区？",
        "basis": f"改动在分支 {proposal.get('branch') or '?'} 上"
        + (f"：{stat}" if stat else ""),
        "id": str(proposal.get("id") or ""),
        "run": str(proposal.get("run_id") or ""),
        "at": proposal.get("created_at"),
        "project": _project(repo),
        "project_path": _project_path(repo),
        "cwd": repo,
        "task_id": tid,
        "task_title": _task_label(task, run),
        "goal": _clip(task.get("goal") or ""),
        "cause": "",
        "failure_kind": "提案",
        "problem": "改动做完了，但在独立分支上，主工作区没动。",
        "headline": f"改动在分支 {proposal.get('branch') or '?'} 上等你合入"
        + (f"：{stat}" if stat else ""),
        "likely_cause": "这个改动还没进主工作区：它只在分支上，合不合取决于测试过没过。",
        "retry_line": "未重试（原因：提案不是失败，重试不会让它更对）",
        "change": stat or (f"改了 {len(files)} 个文件" if files else None),
        "suggestion": "先看改动摘要；测试红着或没验证就先重跑，没问题再点「合并」。",
        "title": "提案在分支上，等你合入或丢弃",
        "detail": stat,
        "note": ", ".join(files)[:160],
    }


def _unverifiable_reason(cwd: str) -> tuple[str, str]:
    """Why a finished run has no test evidence, and what would change that.

    "unverified" is neither a failure nor a pass: the process exited 0 and
    nothing checked the result. The honest reason is either "this directory has
    no test entry" or "it has one and nobody ran it", and those need different
    advice — one is a task-definition problem, the other a setting.
    """
    found = None
    path = Path(cwd) if cwd else None
    try:
        if path is not None and path.is_dir():
            found = verify.detect(path)
    except OSError:  # a missing or locked directory must not take the board down
        found = None
    if found:
        return (
            f"目录里有测试入口（{found.get('reason') or '检测到测试命令'}），"
            "但这次 run 没跑：改完没跑测试，或自动验证被关掉了。",
            "确认 NODUS_AUTO_VERIFY 没被设成 0；下次改完文件就会自动跑测试，跑过才有绿灯。",
        )
    return (
        "目标目录没有测试入口（没有 pyproject.toml / package.json 的 scripts.test / "
        "Cargo.toml / go.mod），所以跑完也没有绿灯可看。",
        "派活时把验收标准写成能跑的命令（acceptance），并在有测试的项目目录里跑；"
        "这批旧 run 的证据补不回来。",
    )


def _unverified_entry(
    latest: dict, facts: dict[str, dict], count: int, run: dict | None = None
) -> dict:
    """The one informational row about runs nobody could verify.

    This used to sit in 需要你决定, which asked a person to triage something no
    person can decide: the record has no test evidence, so the answer is
    "cannot be verified", not "your call". It is information, and it says why.
    """
    tid = str(latest.get("task_id") or "")
    task = facts.get(tid) or {}
    full = run or {}
    cwd = str(full.get("cwd") or "")
    reason, advice = _unverifiable_reason(cwd)
    return {
        "kind": "unverifiable",
        "id": "",
        "run": "",
        "at": latest.get("ended_at"),
        "project": _project(cwd),
        "project_path": _project_path(cwd),
        "cwd": cwd,
        "task_id": tid,
        "task_title": _task_label(task, full or latest),
        "goal": _clip(task.get("goal") or ""),
        "cause": "",
        "failure_kind": "",
        "problem": (
            f"最近 {count} 个 run 跑完了但没有测试证据；"
            "进程退出码 0 只说明它退出了，不说明改对了。"
        ),
        "headline": f"最近 {count} 个 run 跑完却没有测试证据，不能算通过",
        "reason": reason,
        "likely_cause": "",
        "retry_line": "",
        "change": _change_summary(full) if full else None,
        "suggestion": advice,
        "title": f"{count} 个 run 跑完但没有测试证据（状态只代表进程退出码）",
        "detail": "",
        "note": "",
    }


def _unverifiable(
    unverified_runs: list[dict], runs: list[dict], facts: dict[str, dict]
) -> list[dict]:
    """The 不可验证 block: information, not a task and not a decision."""
    if not unverified_runs:
        return []
    latest = unverified_runs[0]
    latest_run = next((r for r in runs if r.get("id") == latest.get("id")), {})
    return [_unverified_entry(latest, facts, len(unverified_runs), latest_run)]


def _budget_decision(run: dict, facts: dict[str, dict], state: str, plan: dict) -> dict:
    """The one failure-shaped row that still reaches a person: the money.

    A retry the remaining budget (or the cost cap) cannot cover is a spend
    decision, so the board asks it as one — "要不要再花 ¥0.50" — and keeps the
    failure itself out of the question. The evidence stays behind 详情 because
    the answer usually depends on whether the last attempt got anywhere.
    """
    gate = str(plan.get("gate") or "")
    entry = _run_entry(run, facts, state=state, plan=plan, gate=gate)
    estimate = plan.get("estimate")
    if gate == "over-budget":
        question = "这个任务已经到预算上限：要不要再加预算让它继续？"
    else:
        money = f"¥{estimate:.2f}" if isinstance(estimate, (int, float)) else "一次重试的钱"
        question = f"要不要再花 {money} 让它再试一次？"
    entry.update(
        {
            "kind": "budget",
            "decision": "spend",
            "question": question,
            "basis": str(plan.get("why") or entry.get("problem") or ""),
            "money": {
                "remaining": plan.get("remaining"),
                "estimate": estimate,
                "cost_cap": plan.get("cost_cap"),
            },
            # A spend decision is not a failure card: these stay empty so that
            # no row on the first screen reads as "this failed, you deal with
            # it". The evidence is still one click down.
            "failure_kind": "",
            "headline": "",
            "likely_cause": "",
            "retry_line": "",
            # How the "yes" actually happens. The estimate gate is a soft stop
            # that a manual dispatch is allowed to pass; the daily ceiling is
            # not, and that one needs the number in the config raised.
            "suggestion": (
                "点「继续花」会按手动批准派一次；如果今天的额度已经用光"
                "（今天的钱见下面），要先把 budget.daily_cny 或 NODUS_DAILY_BUDGET 加够。"
            ),
        }
    )
    return entry


def _needs_you(
    runs: list[dict], states: dict[str, str], facts: dict[str, dict], now: float
) -> list[dict]:
    """The three decisions that are actually a person's call.

    Everything else about a failure — retrying it, verifying it, giving up on
    it — is the machine's job, and is reported by `_handling` and `_stopped`.
    What is left: a value judgement (merge this proposal), a spend decision
    (this retry is past the budget), and an ambiguous requirement, which no
    producer writes yet (`DECISION_KINDS`).

    Kept under the old name because every caller already knows it.
    """
    items: list[dict] = []
    for run in runs:
        if str(run.get("status") or "") != "failed":
            continue
        if recover.classify(run) == "deterministic":
            continue
        if not recover.enabled():
            # Recovery is off, so no retry will ever spend this money: the row
            # is a stopped chain, not a spend question.
            continue
        plan = recover.retry_plan(run, now=now)
        if plan["gate"] not in SPEND_GATES:
            continue
        items.append(
            _budget_decision(
                run, facts, states.get(str(run.get("id") or ""), "none"), plan
            )
        )
    items.sort(key=lambda item: item.get("at") or 0, reverse=True)
    for proposal in db.list_proposals("open"):
        items.append(_proposal_entry(proposal, facts))
    return items


def _handling_statement(entry: dict) -> str:
    """`第 2/3 次重试正在跑` — what I am doing, in the first person."""
    attempt = entry.get("attempt") or 1
    limit = entry.get("limit") or 3
    if entry.get("running"):
        return f"第 {attempt}/{limit} 次重试正在跑"
    return f"第 {attempt}/{limit} 次重试"


def _handling_plan(entry: dict) -> str:
    """What I will do next, and what happens if it also fails."""
    attempt = entry.get("attempt") or 1
    limit = entry.get("limit") or 3
    if entry.get("running"):
        return (
            "这一次跑完我再判断：绿了就算过，红了按同样的规则决定下一次；"
            f"{limit} 次用完就停下，把报错和证据写进「已停止」。"
        )
    how = "续上次的会话再跑" if str(entry.get("mode") or "") == "resume" else "从头再跑"
    redo = (entry.get("retry") or {}).get("redo") or {}
    extra = "（上次改过的文件要重做）" if redo else ""
    return (
        f"{how}（第 {attempt}/{limit} 次）{extra}；"
        f"{limit} 次都不行我就停下，把报错和证据写进「已停止」。"
    )


def _handling(recovering: list[dict], facts: dict[str, dict], now: float) -> list[dict]:
    """The failures the machine is on right now, said in the first person.

    One row per attempt in flight: what I am doing, what I already tried, the
    guess at the cause, and the plan. The only button is 看日志 — there is no
    "要不要重跑" here, because there is nothing for a person to press.

    The five answers come from the last failed run in the chain (a live retry
    child has no failure of its own yet); the progress comes from the child.
    """
    out: list[dict] = []
    for row in recovering:
        run = db.get_run(str(row.get("run_id") or "")) or {}
        if not run:
            continue
        failed = [r for r in recover.chain(run) if str(r.get("status") or "") == "failed"]
        anchor = failed[-1] if failed else run
        plan = (
            recover.retry_plan(run, now=now)
            if str(run.get("status") or "") == "failed"
            else None
        )
        anchor_id = str(anchor.get("id") or "")
        state = (
            db.verify_states([anchor_id]).get(anchor_id, "none") if anchor_id else "none"
        )
        entry = _run_entry(anchor, facts, str(row.get("problem") or ""), state=state, plan=plan)
        history = list((entry.get("evidence") or {}).get("attempts") or [])
        entry.update(
            {
                "kind": "handling",
                "decision": "",
                "id": str(run.get("id") or ""),
                "run": str(run.get("id") or ""),
                "anchor_run": anchor_id,
                "at": run.get("ended_at") or run.get("started_at") or entry.get("at"),
                "attempt": row.get("attempt"),
                "limit": row.get("limit"),
                "mode": row.get("mode") or "",
                "estimate": row.get("estimate"),
                "next_retry_at": row.get("next_retry_at"),
                "running": row.get("next_retry_at") is None,
                "history": history,
                "retry_line": (
                    entry.get("retry_line") if history else "还没重试过：这是第 1 次"
                ),
            }
        )
        cause = str(entry.get("likely_cause") or "")
        entry["cause_line"] = cause + _cause_fix(cause)
        entry["statement"] = _handling_statement(entry)
        entry["plan"] = _handling_plan(entry)
        entry["suggestion"] = entry["plan"]
        out.append(entry)
    out.sort(key=lambda item: (item.get("next_retry_at") or 0, item.get("attempt") or 0))
    return out


#: Gates that mean "this chain is over". They are the same three the old board
#: called history, plus the terminal ones handled below.
_STOP_HISTORY = {
    "already-retried": "已经自动重试过，不会为同一次失败再开一张单",
    "task-running": "这个任务已经有新的一条在跑，等它结束再看",
    "too-old": "太旧了，不在自动恢复窗口内",
}


#: Why a settled row is no longer moving. The statement on the card is about
#: the work, so these are phrased as "what happened next", not as a status.
_SETTLED_STATEMENT = {
    "already-retried": "后面已经派过重试，这一条翻过去了",
    "task-running": "这个任务已经有新的一条在跑，这一条翻过去了",
    "too-old": "太旧了，已经不在自动恢复窗口内",
}


def _stop_statement(entry: dict, attempts: list[dict]) -> str:
    """`3 次都试过了：第1次 42s tests_red / …` — the work report, not a question."""
    if attempts:
        return f"{len(attempts)} 次都试过了：{_attempt_phrase(attempts)}"
    reason = str(entry.get("stop_reason") or "")
    if entry.get("settled"):
        return _SETTLED_STATEMENT.get(reason, "这一条已经翻过去了")
    if reason == "tests_red":
        return "跑完了一遍，测试没过，我没有再重试"
    if reason == "deterministic":
        return "这个错重试不会变，所以我一次都没试"
    return "没有重试就停在这里"


def _stop_plan(entry: dict, settled: bool) -> str:
    """Where the evidence is, and the two things a person may still do."""
    reason = str(entry.get("stop_reason") or "")
    if settled:
        return "证据留在详情里，点「重试」可以再开一次。"
    if reason == "deterministic":
        return "重试还是同一个错，所以我停在这里；证据留在详情里，要换做法直接告诉我。"
    if reason == "tests_red":
        return "测试红着，我没有接着跑；失败的那条测试在详情里，点「重试」可以再试一次。"
    if reason == "disabled":
        return (
            "自动恢复现在是关着的，所以没人在重试；证据留在详情里，"
            "点「重试」也可以手动开一次。"
        )
    return (
        "已停止，不会再自动重试；报错原文、改动和每次尝试都留在详情里，"
        "点「重试」可以再试一次，或者告诉我换个思路。"
    )


def _stop_entry(entry: dict, reason: str, *, settled: bool) -> dict:
    """One 已停止 row: what I tried, why I stopped, where the evidence is."""
    attempts = list((entry.get("evidence") or {}).get("attempts") or [])
    entry["kind"] = "stopped"
    entry["stop_reason"] = reason
    entry["settled"] = settled
    entry["history"] = attempts
    cause = str(entry.get("likely_cause") or "")
    entry["cause_line"] = cause + _cause_fix(cause)
    entry["statement"] = _stop_statement(entry, attempts)
    entry["plan"] = _stop_plan(entry, settled)
    # The 建议 slot keeps the old shape but now carries the closing statement:
    # nothing here tells the person what they must go do.
    entry["suggestion"] = entry["plan"]
    return entry


def _stopped(
    runs: list[dict], states: dict[str, str], facts: dict[str, dict], now: float
) -> tuple[list[dict], list[dict]]:
    """Failures the machine is done with, told as a report, not a request.

    `stopped` is everything no longer moving: a spent retry chain, a repeated
    signature, a deterministic error, a red test nobody owns, and the older
    failures a later retry moved past (`settled`, also returned separately as
    `history_failures` for the old callers).

    Each row answers three things — what I tried, why I stopped, where the
    evidence is — and none of them asks the person whether the failure is
    theirs to handle.
    """
    stopped: list[dict] = []
    history: list[dict] = []
    for run in runs:
        if str(run.get("status") or "") != "failed":
            continue
        state = states.get(str(run.get("id") or ""), "none")
        plan = recover.retry_plan(run, now=now)
        gate = plan["gate"] if recover.enabled() else "disabled"
        if recover.classify(run) == "deterministic":
            entry = _run_entry(
                run,
                facts,
                recover.problem_text(run),
                state=state,
                why=recover.why_not_retried(run),
                gate="deterministic",
                plan=plan,
            )
            stopped.append(_stop_entry(entry, "deterministic", settled=False))
            continue
        if gate in _STOP_HISTORY:
            # Deep evidence on purpose: an old run that kept no tool_call must
            # say "证据缺失" rather than degrade into a generic sentence.
            entry = _run_entry(
                run,
                facts,
                _STOP_HISTORY[gate],
                state=state,
                deep=True,
                gate=gate,
                why=plan["why"],
                plan=plan,
            )
            row = _stop_entry(entry, gate, settled=True)
            history.append(row)
            stopped.append(row)
            continue
        if gate in ("", "backoff") or gate in SPEND_GATES:
            # In flight (「我在处理」) or waiting on a spend decision
            # (「需要你决定」); listing it twice would be noise.
            continue
        if gate == "disabled":
            problem = "自动恢复已关闭，这个失败不会有人重试。"
        elif gate == "retry-limit":
            depth = recover.retry_depth(run)
            problem = (
                f"自动重试 {depth} 次都没成功，最后一次是：{recover.problem_text(run)}"
                if depth
                else plan["why"]
            )
        else:
            problem = plan["why"] or recover.problem_text(run)
        entry = _run_entry(
            run,
            facts,
            problem,
            state=state,
            plan=plan,
            gate=gate,
            why=plan["why"] or problem,
        )
        stopped.append(_stop_entry(entry, gate, settled=False))
    # A red test on a completed run: the acceptance failed and recovery does
    # not own completed runs, so nobody is retrying it.
    red = [
        r
        for r in runs
        if states.get(r["id"]) == "red" and str(r.get("status") or "") != "failed"
    ]
    for run in red:
        entry = _run_entry(
            run,
            facts,
            "跑完了，但项目的测试没通过。",
            state="red",
            kind="red",
            # Every stopped row answers "why I stopped". For a red test the
            # answer is that recovery only owns failed runs: nobody is
            # retrying a completed one, so the report has to say so.
            why="自动重试只处理失败的 run；这条跑完了但验收没过，我没有接着跑。",
        )
        stopped.append(_stop_entry(entry, "tests_red", settled=False))
    # Fresh stops first, then the settled tail; newest first inside both.
    stopped.sort(key=lambda item: (bool(item.get("settled")), -(item.get("at") or 0)))
    return stopped, history


def _deterministic(
    runs: list[dict], facts: dict[str, dict], states: dict[str, str] | None = None
) -> list[dict]:
    """Failures retrying cannot fix, with the reason a retry would not help.

    Kept as its own list because MCP and the pet still read the key; the page
    folds these rows into 已停止, where the action ("change the key, then
    retry") is one line on a report instead of a section of its own.
    """
    states = states or {}
    rows: list[dict] = []
    for run in runs:
        if str(run.get("status") or "") != "failed":
            continue
        if recover.classify(run) != "deterministic":
            continue
        # The reason a retry cannot help belongs on the `why` line; the
        # headline says what broke, and the advice comes from the cause rules
        # (`401` gets "换密钥", not "改好配置或代码").
        rows.append(
            _run_entry(
                run,
                facts,
                recover.problem_text(run),
                "",
                state=states.get(str(run.get("id") or ""), "none"),
                why=recover.why_not_retried(run),
            )
        )
    rows.sort(key=lambda item: item.get("at") or 0, reverse=True)
    return rows


def _decisions(runs: list[dict], states: dict[str, str]) -> tuple[list[dict], dict]:
    """Everything on this board that cannot move without a human."""
    items: list[dict] = []
    counts = {"proposal": 0, "red": 0, "failed": 0, "unverified": 0}

    proposals = db.list_proposals("open")
    counts["proposal"] = len(proposals)
    for p in proposals[:5]:
        files = [str(f.get("path") or "") for f in (p.get("files") or [])]
        items.append(
            {
                "kind": "proposal",
                "id": p.get("id") or "",
                "run": p.get("run_id") or "",
                "title": "提案在分支上，等你合入或丢弃",
                "detail": p.get("stat") or "",
                "note": ", ".join(files)[:160],
                "at": p.get("created_at"),
            }
        )

    red = [r for r in runs if states.get(r["id"]) == "red"]
    counts["red"] = len(red)
    for r in red[:3]:
        items.append(
            {
                "kind": "red",
                "id": r["id"],
                "run": r["id"],
                "title": "跑完了，测试是红的",
                "detail": (r.get("note") or "")[:140],
                "note": "",
                "at": r.get("ended_at") or r.get("started_at"),
            }
        )

    failed = [r for r in runs if r.get("status") == "failed"]
    counts["failed"] = len(failed)
    for r in failed[:3]:
        # The old title here was "执行失败，要不要重跑由你定" — the exact
        # sentence this board exists to replace. MCP and the pet read this
        # list, so it gets the same class sentence as the page.
        evidence = _evidence(r, states.get(r["id"], "none"), deep=False)
        kind = _failure_kind(r, evidence, states.get(r["id"], "none"))
        items.append(
            {
                "kind": "failed",
                "id": r["id"],
                "run": r["id"],
                "title": f"{kind}：{_error_phrase(evidence.get('error_tail') or '') or recover.problem_text(r)}",
                "detail": (r.get("error") or "")[:160],
                "note": "",
                "at": r.get("ended_at") or r.get("started_at"),
            }
        )

    unverified = [
        r for r in runs if r.get("status") == "completed" and states.get(r["id"], "none") == "none"
    ]
    unverified.sort(key=_ended_at, reverse=True)
    counts["unverified"] = len(unverified)
    if unverified:
        items.append(
            {
                "kind": "unverified",
                "id": "",
                "run": "",
                "title": f"{len(unverified)} 个 run 跑完但没有测试证据（状态只代表进程退出码）",
                "detail": "",
                "note": "",
                "at": unverified[0].get("ended_at") or unverified[0].get("started_at"),
            }
        )
    return items, counts


def _today(conn, start: float) -> dict:
    """Cost recorded today, straight out of `run.usage_json`.

    No re-derivation and no FX: `_price()` already chose the number and its
    currency, and adding two currencies would invent an exchange rate. The
    reading and the bucketing are `ledger.by_agent`, so this page, the CLI and
    MCP cannot drift apart on what "today, per agent" means.
    """
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT agent, route, usage_json FROM run"
            " WHERE workspace_id=? AND started_at IS NOT NULL AND started_at>=?",
            (config.workspace_id(), start),
        ).fetchall()
    ]
    # A harness session the user drove is not a run, but its tokens were paid
    # for today; the board's first question ("what did today cost") includes it.
    rows.extend(observe.observed_runs(start))
    return ledger.by_agent(rows)


def _usage_rows(conn, start: float) -> list[dict]:
    """Runs that started on/after `start`, in the shape `ledger.summarize` wants."""
    rows = conn.execute(
        "SELECT agent, route, provider, model, status, usage_json, started_at FROM run"
        " WHERE workspace_id=? AND started_at IS NOT NULL AND started_at>=?",
        (config.workspace_id(), start),
    ).fetchall()
    return [dict(row) for row in rows] + observe.observed_runs(start)


def _usage_snapshot(conn, day_start: float, month_start: float) -> dict:
    """Today's tokens and the calendar month's total, on the same definitions
    the CLI uses. Cache hit rate is `cache_read / (prompt + cache_read +
    cache_write)` and is null when nothing was recorded -- the page says
    "no usage yet" rather than printing a 0% that would look measured.
    """
    day = ledger.summarize(_usage_rows(conn, day_start), group_by="route")
    month = ledger.summarize(_usage_rows(conn, month_start), group_by="route")
    return {
        "tokens": day["totals"]["tokens"],
        "cache_hit_rate": day["totals"]["cache_hit_rate"],
        "by_route": [
            {
                "route": group["key"],
                "runs": group["runs"],
                "tokens": group["tokens"]["total"],
                "cache_hit_rate": group["cache_hit_rate"],
                "cost": {
                    currency: bucket["total"] for currency, bucket in group["cost"].items()
                },
            }
            for group in day["groups"][:3]
        ],
        "month": {
            "runs": month["totals"]["runs"],
            "tokens": month["totals"]["tokens"],
            "cache_hit_rate": month["totals"]["cache_hit_rate"],
            "cost": {
                currency: bucket["total"]
                for currency, bucket in month["totals"]["cost"].items()
            },
        },
    }


def _memory_board(limit: int = 300) -> list[dict]:
    """Facts the harnesses wrote down, waiting for a human.

    Only pending rows: an adopted fact is not a decision waiting on anyone, and
    offering it again is what made a second click look like a failure.

    Never fatal: a project with no AGENTS.md, a locked file or a file that is
    not UTF-8 must not take the whole board down for a review widget. An empty
    list is the honest answer there.
    """
    try:
        rows = memory.harvest(memory.saved_project())
    except Exception:  # noqa: BLE001 - the board outranks this section
        return []
    return [
        {
            "id": row["id"],
            "text": row["text"],
            "sources": row["sources"],
            "conflict": row["conflict"],
            "adopted": row["adopted"],
        }
        for row in rows
        if not row["adopted"]
    ][:limit]


def board_state(now: float | None = None) -> dict:
    """The first screen: what is running, what waits on a human, today's money.

    The screen splits by *whose job it is*, not by how bad it looks:

    - `needs_you`: only the three decisions a person owns (a proposal to merge,
      a spend decision past the budget, an ambiguous requirement). A failure is
      never in here — that was the bug this split exists to fix.
    - `handling`: the failures the machine is on right now, with the attempt
      number, the history, the likely cause and the plan.
    - `stopped`: the ones it has finished with (spent chain, repeated error,
      deterministic error, red test nobody owns) plus the older failures a
      retry moved past. A report, not a question.
    - `unverifiable`: runs that completed with no test evidence. Information;
      it says why, and what would make the next run verifiable.

    `decisions`/`decision_counts`, `auto_recovering`, `deterministic` and
    `history_failures` keep their old shape because MCP and the desktop pet
    read them; the new fields are additive.

    `memory_candidates` is the other kind of human decision: facts harvested
    from the harnesses' own memory files, which only a person may adopt.
    """
    _fresh_cache()
    now = time.time() if now is None else now
    start, day = _day_start(now)
    month_start, _, _ = ledger.bounds("month", now)
    # Only work. The pet's small talk is hundreds of rows of chatter through
    # the same table; letting it into a 50-row window would push every real run
    # out of the board's counts (and call each utterance "unverified work"). Money is
    # the exception: _today and _usage_snapshot read the run table directly, so
    # chat spend still lands in today's total.
    runs = db.list_runs(limit=50, kind="work")
    states = db.verify_states([r["id"] for r in runs])
    decisions, counts = _decisions(runs, states)
    titles = _task_titles([r.get("task_id") or "" for r in runs])
    unverified_runs = _unverified_rows(runs, states, titles)
    facts = _task_facts([r.get("task_id") or "" for r in runs])
    needs_you = _needs_you(runs, states, facts, now)
    auto_recovering = recover.recovering(now)
    handling = _handling(auto_recovering, facts, now)
    stopped, history_failures = _stopped(runs, states, facts, now)
    unverifiable = _unverifiable(unverified_runs, runs, facts)
    deterministic = _deterministic(runs, facts, states)
    counts["needs_you"] = len(needs_you)
    counts["auto_recovering"] = len(auto_recovering)
    counts["handling"] = len(handling)
    counts["stopped"] = len(stopped)
    counts["unverifiable"] = len(unverifiable)
    counts["deterministic"] = len(deterministic)
    counts["history_failed"] = len(history_failures)
    conn = db.connect()
    try:
        live = _live_rows(conn, now)
        today = _today(conn, start)
        today.update(_usage_snapshot(conn, start, month_start))
    finally:
        conn.close()
    return {
        "now": now,
        "day": day,
        "profile": config.active_profile(),
        "running": live,
        "decisions": decisions,
        "decision_counts": counts,
        "unverified_runs": unverified_runs,
        "needs_you": needs_you,
        "handling": handling,
        "stopped": stopped,
        "unverifiable": unverifiable,
        "auto_recovering": auto_recovering,
        "deterministic": deterministic,
        "history_failures": history_failures,
        "memory_candidates": _memory_board(),
        "today": {"window_start": start, "budget": budget_mod.check(now), **today},
    }


def _json(handler: BaseHTTPRequestHandler, code: int, payload) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _sse(handler: BaseHTTPRequestHandler, run_id: str | None, after: int) -> None:
    q = runner.BUS.subscribe()
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.end_headers()
    try:
        backlog = db.events_after(run_id, after) if run_id else []
        for ev in backlog:
            handler.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            handler.wfile.flush()
        last_ping = time.time()
        while True:
            try:
                ev = q.get(timeout=15)
            except queue.Empty:
                if time.time() - last_ping > 15:
                    handler.wfile.write(b": ping\n\n")
                    handler.wfile.flush()
                    last_ping = time.time()
                continue
            if run_id and ev.get("run_id") != run_id:
                continue
            handler.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            handler.wfile.flush()
            last_ping = time.time()
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass
    finally:
        runner.BUS.unsubscribe(q)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # quiet by default
        return

    def _guard(self, handler) -> None:
        """Never let an exception take the connection (and the client) down."""
        try:
            handler()
        except Exception as exc:  # noqa: BLE001 - last line of defence
            # A client that hung up is not a server fault: no traceback for it.
            # Everything else still gets the full stack.
            if _is_disconnect(exc):
                return
            _log("handler crashed:\n" + traceback.format_exc())
            try:
                _json(self, 500, {"error": "internal error"})
            except OSError:
                pass

    def do_GET(self) -> None:
        self._guard(self._get)

    def do_POST(self) -> None:
        self._guard(self._post)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _authorized(self) -> bool:
        # `/` is the board itself: it has to load before anything else can be
        # requested, and it carries the token for the requests it makes.
        #
        # Compare the *path* only. Comparing `self.path` directly fails for the
        # natural way to open the board, `/?token=...` — the query string makes
        # it unequal to "/", so a correct token still got 401.
        path = urlparse(self.path).path or "/"
        if (path == "/" or path.startswith("/health")
                or path == "/favicon.ico" or path.startswith("/pet")
                or path.rstrip("/") == "/openapi.yaml"):
            return True
        if authorized(self.headers.get("Authorization", "")):
            return True
        # EventSource cannot set an Authorization header. The stream is
        # read-only, loopback-only and already public to anything on this
        # machine (which can read ~/.nodus/token anyway), but the token still
        # has to match — an unauthenticated stream would leak run payloads.
        u = urlparse(self.path)
        if u.path.rstrip("/") == "/events":
            token = (parse_qs(u.query).get("token") or [""])[0]
            return hmac.compare_digest(token, config.token())
        return False

    def _get(self) -> None:
        if not self._authorized():
            return _json(self, 401, {"error": "unauthorized"})
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"

        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/openapi.yaml":
            # Public on purpose: a client needs the contract before it can be
            # trusted with a token, and schema disclosure on loopback is cheap.
            try:
                body = SPEC_PATH.read_bytes()
            except OSError:
                return _json(self, 404, {"error": "openapi.yaml not found"})
            self.send_response(200)
            self.send_header("Content-Type", "application/yaml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/":
            body = ui.PAGE.replace("__TOKEN__", config.token()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/pet":
            # Public on purpose: a desktop widget has no way to hold the token,
            # and this is the same loopback-only machine.
            return _json(self, 200, pet.state())

        if path == "/health":
            return _json(
                self,
                200,
                {
                    "ok": True,
                    "version": config.VERSION,
                    "profile": config.active_profile(),
                    "home": config.home_id(),
                },
            )

        if path == "/agents":
            secrets = config.load_secrets()
            return _json(
                self,
                200,
                [
                    {
                        "name": s.name,
                        "vendor": s.vendor,
                        "protocols": list(s.accepts),
                        "ready": not agents.unavailable(s),
                        "missing": agents.unavailable(s),
                    }
                    for s in agents.registry().values()
                ],
            )

        if path == "/providers":
            secrets = config.load_secrets()
            return _json(
                self,
                200,
                [
                    {
                        "name": p.name,
                        "protocol": p.protocol,
                        "base_url": p.base_url,
                        "model": p.model,
                        "tiers": p.tiers,
                        "pricing": vars(p.pricing) if p.pricing else None,
                        "meter": p.meter,
                        "note": p.note,
                        "ready": not providers.missing(p, secrets),
                        "missing": providers.missing(p, secrets),
                    }
                    for p in providers.load().values()
                ],
            )

        if path == "/routes":
            secrets = config.load_secrets()
            table = providers.load()
            out = []
            for route in routes.load().values():
                spec = agents.get(route.agent)
                gaps = routes.problems(route)
                if spec is not None:
                    for name in route.providers:
                        provider = table.get(name)
                        if provider is not None:
                            gaps.extend(
                                f"{name}: {gap}" for gap in agents.missing(spec, provider, secrets)
                            )
                out.append(
                    {
                        "name": route.name,
                        "agent": route.agent,
                        "providers": list(route.providers),
                        "model": route.model,
                        "effort": route.effort,
                        "ready": not gaps,
                        "problems": gaps,
                    }
                )
            return _json(self, 200, out)

        if path == "/profiles":
            active = config.active_profile()
            return _json(
                self,
                200,
                [
                    {**vars(profiles.read(name)), "active": name == active}
                    for name in (profiles.names() or ["default"])
                ],
            )

        if path == "/board":
            return _json(self, 200, board_state())

        if path == "/tasks":
            return _json(self, 200, db.list_tasks())

        if path.startswith("/tasks/"):
            return _json(self, 200, db.get_task(path.split("/")[2]))

        if path == "/runs":
            task_id = (qs.get("task_id") or [None])[0]
            runs = db.list_runs(task_id)
            states = db.verify_states([r["id"] for r in runs])
            for run in runs:
                run["verify"] = states.get(run["id"], "none")
            return _json(self, 200, runs)

        if path.startswith("/runs/"):
            parts = path.split("/")
            rid = parts[2]
            if len(parts) == 3:
                _fresh_cache()
                run = db.get_run(rid)
                if run.get("status") == "failed":
                    # The log view and the board must agree on why something
                    # failed; the wording lives in recover.py, not in the page.
                    run["problem"] = recover.problem_text(run)
                    run["evidence"] = _evidence(run, db.verify_states([rid]).get(rid, "none"))
                elif db.verify_states([rid]).get(rid) == "red":
                    run["evidence"] = _evidence(run, "red")
                return _json(self, 200, run)
            if parts[3] == "events":
                after = int((qs.get("after") or [0])[0])
                if (qs.get("stream") or ["0"])[0] in ("1", "true"):
                    return _sse(self, rid, after)
                return _json(self, 200, db.events_after(rid, after))
            if parts[3] == "artifacts":
                return _json(self, 200, db.artifacts_for(rid))
            if parts[3] == "handoff":
                h = db.latest_handoff(db.get_run(rid).get("task_id", ""))
                text = ""
                if h:
                    try:
                        text = Path(h["path"]).read_text(encoding="utf-8")
                    except OSError:
                        text = ""
                return _json(self, 200, {"path": h.get("path"), "text": text})

        if path == "/events":
            return _sse(self, None, 0)

        if path == "/pipelines":
            return _json(self, 200, db.list_pipelines((qs.get("status") or [""])[0]))

        if path == "/proposals":
            return _json(self, 200, db.list_proposals("" if (qs.get("all") or [""])[0] else "open"))

        if path.startswith("/pipelines/"):
            parts = path.split("/")
            pid = parts[2]
            row = db.get_pipeline(pid)
            if not row:
                return _json(self, 404, {"error": "not found"})
            if len(parts) == 3:
                return _json(self, 200, row)
            if parts[3] == "events":
                after = int((qs.get("after") or [0])[0])
                return _json(self, 200, db.events_after(pid, after))

        return _json(self, 404, {"error": "not found"})

    def _post(self) -> None:
        if not self._authorized():
            return _json(self, 401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return _json(self, 400, {"error": "invalid content-length"})
        if not body_allowed(length):
            return _json(self, 413, {"error": "body too large"})
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        try:
            body = self._body()
        except json.JSONDecodeError:
            return _json(self, 400, {"error": "invalid json"})

        if path == "/tasks":
            task = db.create_task(
                goal=body.get("goal", ""),
                acceptance=body.get("acceptance", ""),
                session_id=body.get("session_id"),
                title=body.get("title", ""),
            )
            return _json(self, 201, task)

        if path == "/runs":
            try:
                run = runner.RUNNER.start(
                    task_id=body.get("task_id", ""),
                    route_name=body.get("route") or body.get("agent") or "claude",
                    cwd=body.get("cwd"),
                    parent_run_id=body.get("parent_run_id"),
                    note=body.get("note", ""),
                    effort=body.get("effort", ""),
                    model=body.get("model", ""),
                    profile=body.get("profile", ""),
                    wait=float(body.get("wait") or 0.0),
                    session_id=body.get("session_id", ""),
                    session_mode=body.get("session_mode", "resume"),
                    provider=body.get("provider", ""),
                    verify_run=body.get("verify"),
                    propose_run=bool(body.get("propose", False)),
                    include_memory=not bool(body.get("no_memory", False)),
                )
            except (ValueError, RuntimeError) as exc:
                return _json(self, 400, {"error": str(exc)})
            return _json(self, 201, run)

        if path == "/llm/chat":
            # The desktop pets' only way to a model. One route, one provider,
            # one credential — the caller names an agent and never an endpoint,
            # so a pet host needs no key of its own. Failures carry a `kind`
            # because the caller has to tell "Nodus is down" from "the agent
            # name is wrong" without parsing prose.
            status, payload = llm.chat(
                agent=body.get("agent") or "",
                messages=body.get("messages"),
                max_tokens=body.get("max_tokens"),
                note=str(body.get("note") or ""),
                cwd=str(body.get("cwd") or ""),
            )
            return _json(self, status, payload)

        if path == "/memory/adopt":
            ids = body.get("ids")
            if isinstance(ids, str):
                ids = [ids]
            if not isinstance(ids, list) or not ids:
                return _json(self, 400, {"error": "ids must be a non-empty list"})
            ids = [str(item).strip() for item in ids if str(item).strip()]
            if not all(re.fullmatch(r"mem-[0-9a-f]{6,40}", item) for item in ids):
                return _json(self, 400, {"error": "invalid candidate id"})
            try:
                result = memory.adopt(ids, memory.saved_project())
            except ValueError as exc:
                return _json(self, 400, {"error": str(exc)})
            except OSError as exc:
                _log(f"memory: write failed: {exc}")
                return _json(self, 409, {"error": f"could not write AGENTS.md: {exc}"})
            except Exception as exc:  # noqa: BLE001 - the reply carries the cause
                # "采纳失败：internal error" is unanswerable. Whatever broke, the
                # person clicking the button gets the actual message.
                _log(f"memory: adopt crashed: {type(exc).__name__}: {exc}")
                return _json(self, 500, {"error": f"{type(exc).__name__}: {exc}"})
            if result["added"]:
                _log(f"memory: adopted {result['added']} fact(s) into {result['path']}")
            elif result["already"]:
                _log(
                    f"memory: {result['already']} candidate(s) already adopted in "
                    f"{result['path']}"
                )
            else:
                _log("memory: nothing to adopt")
            return _json(self, 200, result)

        if path.startswith("/runs/") and path.endswith("/stop"):
            rid = path.split("/")[2]
            return _json(self, 200, {"stopped": runner.RUNNER.stop(rid)})

        if path == "/pipelines":
            kind = body.get("kind", "")
            params = body.get("params") or {}
            if kind not in ("tdd", "review", "fix"):
                return _json(self, 400, {"error": f"unknown pipeline: {kind}"})
            try:
                row = jobs.JOBS.submit(kind, params, body.get("label", ""))
            except (ValueError, TypeError) as exc:
                return _json(self, 400, {"error": str(exc)})
            return _json(self, 201, row)

        if path.startswith("/proposals/"):
            parts = path.split("/")
            pid, action = parts[2], (parts[3] if len(parts) > 3 else "")
            row = db.get_proposal(pid)
            if not row:
                return _json(self, 404, {"error": "not found"})
            if action == "reject":
                propose.reject(row)
                _log(f"proposal {pid} rejected from the board")
                return _json(self, 200, {"ok": True, "proposal": pid, "status": "rejected"})
            if action == "merge":
                result = propose.merge(row)
                _log(f"proposal {pid} merge -> {result}")
                return _json(self, 200 if result["ok"] else 409, result)

        if path.startswith("/pipelines/") and path.endswith("/stop"):
            pid = path.split("/")[2]
            return _json(self, 200, {"stopped": jobs.JOBS.stop(pid)})

        return _json(self, 404, {"error": "not found"})


class _QuietServer(ThreadingHTTPServer):
    """The HTTP server, minus the traceback for a client that hung up.

    socketserver writes a full traceback for every exception that escapes a
    request thread, including the ConnectionAbortedError(10053) a browser
    produces by closing an SSE stream. daemon.log had ~5,800 of those and they
    drowned the real failures. A disconnect is dropped silently; anything else
    still gets its full stack in the log.
    """

    def handle_error(self, request, client_address) -> None:
        if _is_disconnect(sys.exc_info()[1]):
            return
        _log("request crashed:\n" + traceback.format_exc())


def serve(port: int = 4199, host: str = "127.0.0.1") -> None:
    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get(
        "NODUS_ALLOW_REMOTE"
    ):
        raise SystemExit("refusing non-loopback bind without NODUS_ALLOW_REMOTE=1")
    config.ensure_dirs()
    profiles.ensure_ready()
    db.connect().close()
    # A daemon that just started owns no runs: anything still marked running
    # belongs to a process that is gone. Reap before accepting work, so a
    # restart never leaves clients waiting on a corpse.
    reaped = runner.RUNNER.reap_orphans()
    stalled = jobs.reap(db.list_pipelines("running"))
    server = _QuietServer((host, port), Handler)
    server.daemon_threads = True
    # The pet doubles as a status light: when something new starts waiting on a
    # human, the desktop pet says so. Off with NODUS_STATUS_LIGHT=0 for anyone
    # who does not run a pet — it costs one sqlite read per interval and never
    # speaks unless a count went up.
    if os.environ.get("NODUS_STATUS_LIGHT", "1") != "0":
        try:
            from . import statuslight

            statuslight.start()
        except Exception:  # a broken pet must never keep the board from serving
            pass
    _log(f"nodusd {config.VERSION} listening on {host}:{port} (pid {os.getpid()})")
    if reaped:
        _log(f"reaped {len(reaped)} orphaned run(s): " + ", ".join(r["id"] for r in reaped))
    if stalled:
        _log(f"closed {len(stalled)} pipeline(s) the previous daemon left running")
    # Reaping has just turned the previous daemon's live runs into failures.
    # Those are precisely the interruptions the operator should not have to
    # decide on, so recovery gets one pass before the port starts answering,
    # then keeps sweeping for failures that happen while the daemon is up.
    if recover.enabled():
        recover.start(log=_log)
    else:
        _log("auto-recover disabled (NODUS_AUTO_RECOVER=0)")
    if sys.stderr.isatty():  # a detached daemon writes to the log, not the console
        print(f"nodusd listening on http://{host}:{port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("nodusd stopped (interrupt)")
        raise
    except Exception:
        import traceback

        _log("nodusd crashed:\n" + traceback.format_exc())
        raise


def serve_async(port: int = 4199) -> threading.Thread:
    t = threading.Thread(target=serve, args=(port,), daemon=True)
    t.start()
    return t
