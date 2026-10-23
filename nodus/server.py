"""HTTP API + SSE. One user, one machine: no auth beyond a loopback bind."""

from __future__ import annotations

import hmac
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import (
    budget as budget_mod,
    agents,
    config,
    db,
    jobs,
    pet,
    profiles,
    propose,
    providers,
    recover,
    routes,
    runner,
    ui,
    usage as usage_stats,
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
#: away; 300 characters is what fits on a card and what the requirement asks.
ERROR_TAIL_CHARS = 300

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
    """The raw tail a person reads, newest and most specific record first."""
    run_id = str(run.get("id") or "")
    events = _events(run_id, ("error", "tool_result", "run.failed"))
    for event in reversed(events):
        payload = event.get("payload") or {}
        if event.get("type") == "tool_result" and payload.get("ok") is False:
            text = str(payload.get("text") or "")
            if text.strip():
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


def _git_numstat_all(cwd: str) -> dict[str, tuple[int, int]]:
    """One `git diff --numstat` for the whole work tree, cached per request.

    HEAD covers staged and unstaged changes; the fallback covers a repository
    with no commit yet. Running it once per directory and filtering in Python
    is the difference between one subprocess and one per decision row.
    """
    if not cwd:
        return {}
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


def _headline(
    run: dict,
    evidence: dict,
    problem: str,
    state: str,
    plan: dict | None = None,
    gate: str = "",
) -> str:
    """The one-sentence conclusion on the second line of a decision row."""
    attempts = len(evidence.get("attempts") or [])
    tests = evidence.get("tests") or []
    phrase = ""
    if tests:
        first = tests[0]
        assertion = _assertion_phrase(str(first.get("assertion") or ""))
        phrase = str(first.get("test") or first.get("name") or "测试")
        if assertion:
            phrase += f" 断言 {assertion}"
        if len(tests) > 1:
            phrase += f"，共 {len(tests)} 条失败"
    elif evidence.get("error_tail"):
        phrase = _error_phrase(evidence["error_tail"])
    if not phrase:
        match = re.search(r"exit code (-?\d+)", str(run.get("error") or ""))
        if match:
            phrase = f"进程退出码 {match.group(1)}"
        else:
            phrase = problem or recover.problem_text(run)
    if state == "red":
        return f"第 {attempts} 次重试后测试仍红：{phrase}" if attempts else f"测试红：{phrase}"
    if recover.classify(run) == "deterministic":
        if "重试不会好" in problem:
            return problem
        return f"重试不会好：{problem}"
    if gate == "same-signature":
        return f"连续两次同样的错：{phrase}"
    if gate in ("budget", "cost-cap", "over-budget"):
        # The money sentence is already the conclusion; repeating the error
        # after it would bury the number the operator has to decide about.
        return problem
    if gate == "disabled":
        return problem
    if attempts:
        return f"第 {attempts} 次重试仍失败：{phrase}"
    return phrase


def _change_line(change: dict) -> str | None:
    files = [str(path) for path in (change.get("files") or []) if path]
    if not files:
        return None
    shown = "、".join(_project(path) for path in files[:3]) + (" …" if len(files) > 3 else "")
    if change.get("added") is not None and change.get("removed") is not None:
        return f"{len(files)} 个文件 +{change['added']} −{change['removed']}：{shown}"
    return f"改了 {len(files)} 个文件：{shown}"


def _next_step(
    evidence: dict, fallback: str, plan: dict | None = None, gate: str = ""
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
    API consumers); `headline` is the conclusion the card leads with, built
    from evidence. `why` is the one sentence that explains why the machine
    stopped retrying — every decision row has one, and none of them is
    "由你决定". Both are always present, even when the evidence is missing —
    "这类证据没有记录" is a fact, "执行失败" is not.
    """
    tid = str(run.get("task_id") or "")
    task = facts.get(tid) or {}
    cwd = str(run.get("cwd") or "")
    issue = problem or recover.problem_text(run)
    facts_ev = _evidence(run, state, deep=deep) if evidence else {}
    if plan:
        gate = gate or str(plan.get("gate") or "")
        why = why or str(plan.get("why") or "")
    advice = suggestion or recover.suggestion_text(run)
    if not suggestion and facts_ev and str(run.get("status") or "") == "failed":
        advice = _next_step(facts_ev, advice, plan=plan, gate=gate)
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
        "cwd": cwd,
        "task_id": tid,
        "task_title": _task_label(task, run),
        "goal": _clip(task.get("goal") or ""),
        "cause": recover.cause(run),
        "problem": issue,
        "headline": headline or _headline(run, facts_ev, issue, state, plan=plan, gate=gate),
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
        "id": str(proposal.get("id") or ""),
        "run": str(proposal.get("run_id") or ""),
        "at": proposal.get("created_at"),
        "project": _project(repo),
        "cwd": repo,
        "task_id": tid,
        "task_title": _task_label(task, run),
        "goal": _clip(task.get("goal") or ""),
        "problem": "改动做完了，但在独立分支上，主工作区没动，等你决定合不合。",
        "headline": f"改动在分支 {proposal.get('branch') or '?'} 上等你合入"
        + (f"：{stat}" if stat else ""),
        "change": stat or (f"改了 {len(files)} 个文件" if files else None),
        "suggestion": "先看改动摘要；测试红着或没验证就先重跑，没问题再点「合并」。",
        "title": "提案在分支上，等你合入或丢弃",
        "detail": stat,
        "note": ", ".join(files)[:160],
    }


def _unverified_entry(
    latest: dict, facts: dict[str, dict], count: int, run: dict | None = None
) -> dict:
    tid = str(latest.get("task_id") or "")
    task = facts.get(tid) or {}
    full = run or {}
    cwd = str(full.get("cwd") or "")
    return {
        "kind": "unverified",
        "id": "",
        "run": "",
        "at": latest.get("ended_at"),
        "project": _project(cwd),
        "cwd": cwd,
        "task_id": tid,
        "task_title": _task_label(task, full or latest),
        "goal": _clip(task.get("goal") or ""),
        "problem": (
            f"最近 {count} 个 run 跑完了但没有测试证据；"
            "进程退出码 0 只说明它退出了，不说明改对了。"
        ),
        "headline": f"最近 {count} 个 run 跑完却没有测试证据，不能算通过",
        "change": _change_summary(full) if full else None,
        "suggestion": "展开看是哪几次，对最近一次点「重跑」让它跑测试。",
        "title": f"{count} 个 run 跑完但没有测试证据（状态只代表进程退出码）",
        "detail": "",
        "note": "",
    }


def _needs_you(
    runs: list[dict],
    states: dict[str, str],
    facts: dict[str, dict],
    unverified_runs: list[dict],
    now: float,
) -> tuple[list[dict], list[dict]]:
    """Only what a person must decide. Recovery decides the rest by itself.

    A retryable failure that still has attempts left is *not* here: the board
    shows it under "auto recovering" instead. The moment the judgement says
    stop, the same failure moves here — with the reason it stopped, because a
    decision without a reason is not a decision.

    A deterministic failure is never here: it has its own section, because the
    decision is "change the key/parameters", not "run it again".
    """
    items: list[dict] = []
    failed: list[dict] = []
    # Failures that recovery has already given up on (or never owned) still
    # happened. Dropping them from every list is how a board starts lying: the
    # count says five failures and a human can only find one of them.
    history: list[dict] = []
    history_reasons = {
        "already-retried": "已经自动重试过，不会再试了",
        "task-running": "这个任务已经有新的一条在跑",
        "too-old": "太旧了，不在自动恢复窗口内",
    }
    for run in runs:
        if str(run.get("status") or "") != "failed":
            continue
        if recover.classify(run) == "deterministic":
            continue
        state = states.get(str(run.get("id") or ""), "none")
        plan = recover.retry_plan(run, now=now)
        gate = plan["gate"] if recover.enabled() else "disabled"
        if gate in history_reasons:
            history.append(
                _run_entry(
                    run,
                    facts,
                    history_reasons[gate],
                    "看日志确认它到底改到哪一步。",
                    state=state,
                    headline=history_reasons[gate],
                    deep=False,
                    gate=gate,
                    why=plan["why"],
                )
            )
            continue
        if gate in ("", "backoff"):
            # Being recovered right now: visible under "auto recovering", so
            # listing it twice would be noise.
            continue
        depth = recover.retry_depth(run)
        if gate == "disabled":
            problem = "自动恢复已关闭，这个失败不会有人重试。"
            suggestion = "手动点「重跑」，或去掉 NODUS_AUTO_RECOVER=0 再启动 daemon。"
        elif gate == "retry-limit":
            problem = (
                f"自动重试 {depth} 次都没成功，最后一次是：{recover.problem_text(run)}"
                if depth
                else plan["why"]
            )
            suggestion = ""
        elif gate in ("same-signature", "budget", "cost-cap", "over-budget"):
            problem = plan["why"]
            suggestion = ""
        elif gate in ("missing-context", "task-gone"):
            problem = plan["why"]
            suggestion = "重新派一次这个任务；旧记录只作为历史。"
        else:
            continue
        failed.append(
            _run_entry(
                run,
                facts,
                problem,
                suggestion,
                state=state,
                plan=plan,
                gate=gate,
                why=plan["why"] or problem,
            )
        )
    # A failed run with a red test is already in `failed`, with the test
    # evidence attached to it; listing it a second time here would
    # double-count one run.
    red = [
        r
        for r in runs
        if states.get(r["id"]) == "red" and str(r.get("status") or "") != "failed"
    ]
    for run in red:
        failed.append(
            _run_entry(
                run,
                facts,
                problem="跑完了，但项目的测试没通过。",
                suggestion="看失败的那条测试和断言；点「重跑」让它再改一次，或自己接手。",
                state="red",
                kind="red",
            )
        )
    # Interruptions first, newest first within them: a failed run or a red
    # test is the thing the operator did not ask for, a proposal is the
    # expected output of work they did ask for.
    failed.sort(key=lambda item: item.get("at") or 0, reverse=True)
    items.extend(failed)

    for proposal in db.list_proposals("open"):
        items.append(_proposal_entry(proposal, facts))

    if unverified_runs:
        # The aggregate row is built from the same run snapshot, not a second
        # lookup: the page must not print a project for a run it cannot see.
        latest_run = next(
            (r for r in runs if r.get("id") == unverified_runs[0].get("id")), {}
        )
        items.append(
            _unverified_entry(unverified_runs[0], facts, len(unverified_runs), latest_run)
        )
    return items, history


def _deterministic(
    runs: list[dict], facts: dict[str, dict], states: dict[str, str] | None = None
) -> list[dict]:
    """Failures retrying cannot fix, with the reason a retry would not help.

    This is a separate section on purpose: the action here is "fix the key or
    the parameters, then rerun", not "rerun". Calling both of them a failure
    would send the operator to the wrong button.
    """
    states = states or {}
    rows: list[dict] = []
    for run in runs:
        if str(run.get("status") or "") != "failed":
            continue
        if recover.classify(run) != "deterministic":
            continue
        problem = recover.problem_text(run)
        rows.append(
            _run_entry(
                run,
                facts,
                problem,
                recover.suggestion_text(run),
                state=states.get(str(run.get("id") or ""), "none"),
                headline=problem if "重试不会好" in problem else f"重试不会好：{problem}",
            )
        )
    rows.sort(key=lambda item: item.get("at") or 0, reverse=True)
    return rows[:5]


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
        items.append(
            {
                "kind": "failed",
                "id": r["id"],
                "run": r["id"],
                "title": "执行失败，要不要重跑由你定",
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
    currency, and adding two currencies would invent an exchange rate.
    """
    rows = conn.execute(
        "SELECT agent, route, usage_json FROM run"
        " WHERE workspace_id=? AND started_at IS NOT NULL AND started_at>=?",
        (config.workspace_id(), start),
    ).fetchall()
    by_agent: dict[str, dict] = {}
    totals: dict[str, float] = {}
    basis: dict[str, int] = {}
    priced = 0
    for row in rows:
        usage = _usage(row["usage_json"])
        agent = row["agent"] or "-"
        bucket = by_agent.setdefault(
            agent, {"agent": agent, "costs": {}, "runs": 0, "priced": 0, "routes": []}
        )
        bucket["runs"] += 1
        route = row["route"] or ""
        if route and route != agent and route not in bucket["routes"]:
            bucket["routes"].append(route)
        cost = usage.get("cost")
        if not isinstance(cost, (int, float)):
            continue
        priced += 1
        bucket["priced"] += 1
        currency = str(usage.get("currency") or "USD")
        bucket["costs"][currency] = round(bucket["costs"].get(currency, 0.0) + float(cost), 6)
        totals[currency] = round(totals.get(currency, 0.0) + float(cost), 6)
        key = str(usage.get("cost_basis") or "none")
        basis[key] = basis.get(key, 0) + 1
    ordered = sorted(by_agent.values(), key=lambda b: (-b["priced"], -b["runs"], b["agent"]))
    return {
        "runs": len(rows),
        "priced": priced,
        "unpriced": len(rows) - priced,
        "totals": totals,
        "by_agent": ordered,
        "basis": basis,
    }


def _usage_rows(conn, start: float) -> list[dict]:
    """Runs that started on/after `start`, in the shape `usage.summarize` wants."""
    rows = conn.execute(
        "SELECT agent, route, provider, model, status, usage_json, started_at FROM run"
        " WHERE workspace_id=? AND started_at IS NOT NULL AND started_at>=?",
        (config.workspace_id(), start),
    ).fetchall()
    return [dict(row) for row in rows]


def _usage_snapshot(conn, day_start: float, month_start: float) -> dict:
    """Today's tokens and the calendar month's total, on the same definitions
    the CLI uses. Cache hit rate is `cache_read / (prompt + cache_read +
    cache_write)` and is null when nothing was recorded -- the page says
    "no usage yet" rather than printing a 0% that would look measured.
    """
    day = usage_stats.summarize(_usage_rows(conn, day_start), group_by="route")
    month = usage_stats.summarize(_usage_rows(conn, month_start), group_by="route")
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


def board_state(now: float | None = None) -> dict:
    """The first screen: what is running, what waits on a human, today's money.

    `decisions` and `decision_counts` keep their old shape because MCP and the
    desktop pet read them. The recovery-era screen reads three newer fields:
    `needs_you` (a human decision), `auto_recovering` (the machine is already
    on it) and `deterministic` (retrying cannot help; fix the cause).
    """
    _fresh_cache()
    now = time.time() if now is None else now
    start, day = _day_start(now)
    month_start, _, _ = usage_stats.bounds("month", now)
    runs = db.list_runs(limit=50)
    states = db.verify_states([r["id"] for r in runs])
    decisions, counts = _decisions(runs, states)
    titles = _task_titles([r.get("task_id") or "" for r in runs])
    unverified_runs = _unverified_rows(runs, states, titles)
    facts = _task_facts([r.get("task_id") or "" for r in runs])
    needs_you, history_failures = _needs_you(runs, states, facts, unverified_runs, now)
    deterministic = _deterministic(runs, facts, states)
    auto_recovering = recover.recovering(now)
    counts["needs_you"] = len(needs_you)
    counts["auto_recovering"] = len(auto_recovering)
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
        "auto_recovering": auto_recovering,
        "deterministic": deterministic,
        "history_failures": history_failures,
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
    except (BrokenPipeError, ConnectionResetError):
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
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:  # noqa: BLE001 - last line of defence
            import traceback

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
    server = ThreadingHTTPServer((host, port), Handler)
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
