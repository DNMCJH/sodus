"""Read-only failure and verification evidence assembly for a single run.

Extracted from the retired board module so `GET /runs/{id}` and `sodus show`
keep full structured evidence without any HTML/board coupling.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path

from . import config, db, diagnosis, recover

ERROR_TAIL_CHARS = 200

_DEAD_TEXT = re.compile(
    r"orphaned|killed|sigterm|terminated|interrupted|exit code -?\d+|"
    r"traceback|error|exception|failed|denied|not found|no such file",
    re.I,
)
_PROCESS_DEATH = re.compile(r"orphaned|killed|sigterm|terminated|interrupted", re.I)
_FAILED_LINE = re.compile(r"^FAILED\s+(\S+?)(?:\s+-\s+(.*))?$", re.M)
_TEST_HEAD = re.compile(r"^_{3,}\s+(.+?)\s+_{3,}$")

_REQUEST = threading.local()
_NUMSTAT_TTL = 30.0
_NUMSTAT: dict[str, tuple[float, dict[str, tuple[int, int]]]] = {}


def fresh_cache() -> None:
    """Clear per-thread memoization at the start of each HTTP request."""
    _REQUEST.data = {}


def _memo(key, build):
    data = getattr(_REQUEST, "data", None)
    if data is None:
        data = {}
        _REQUEST.data = data
    if key not in data:
        data[key] = build()
    return data[key]


def _first_line(text) -> str:
    return str(text or "").strip().split("\n")[0].strip()


def _clip(text: str, n: int = 120) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= n else value[: n - 1].rstrip() + "…"


def _project(cwd) -> str:
    value = str(cwd or "").rstrip("/\\")
    return re.split(r"[\\/]", value)[-1] if value else ""


def _tail(text: str, n: int = ERROR_TAIL_CHARS) -> str:
    value = str(text or "").rstrip()
    return value if len(value) <= n else "…" + value[-n:]


def _repo_rel(path: str, cwd: str) -> str:
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


def _git_numstat_all(cwd: str) -> dict[str, tuple[int, int]]:
    if not cwd:
        return {}
    now = time.time()
    hit = _NUMSTAT.get(cwd)
    if hit is not None and now - hit[0] < _NUMSTAT_TTL:
        return hit[1]
    stats = _read_git_numstat(cwd)
    _NUMSTAT[cwd] = (now, stats)
    return stats


def _diff_stat(rows: list[dict], limit: int = 5) -> str:
    parts = []
    for row in rows[:limit]:
        added = int(row.get("added") or 0)
        removed = int(row.get("removed") or 0)
        parts.append(f"{_project(str(row.get('path') or ''))} | +{added} −{removed}")
    if len(rows) > limit:
        parts.append(f"… 另有 {len(rows) - limit} 个文件")
    return "；".join(parts)


def _git_numstat(cwd: str, files: list[str]) -> dict:
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
    if verify == "red":
        return "tests_red"
    if verify == "ok":
        return "ok"
    cause = recover.cause(run)
    if cause in diagnosis.DETERMINISTIC | {
        "timeout",
        "orphaned",
        "network",
        "5xx",
        "rate-limit",
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


def collect(run: dict, state: str = "none", deep: bool = True) -> dict:
    """Assemble structured evidence for a failed or red run without reading logs."""
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
