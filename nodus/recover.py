"""Automatic recovery: retry only what a retry can actually fix.

A failure is one of three things, and only the first one is ours to handle:

    transient      the attempt never got a fair chance — the owner process
                   died, the network dropped, the endpoint was rate limiting
                   or returned 5xx. Running the same work again is the fix.
    deterministic  the request itself is wrong — bad key, bad model name, bad
                   parameters, content policy. Running it again produces the
                   same error and one more invoice line.
    human          nothing proves either way. Guessing here is how an
                   automatic retry corrupts a tree somebody half-edited.

The retry is a *child* run, not a resurrection of the failed row: the failed
attempt keeps its own record, and the child points back with `parent_run_id`.
A chain gets `RETRY_LIMIT` retries (3 by default, `NODUS_RETRY_LIMIT`), each
after a backoff long enough for a provider blip to pass. The old task,
directory, route, model and session are kept, so a resumed conversation
continues instead of starting over.

The board does not retell a failure as "执行失败": `cause()` names the matched
signal, `why_not_retried()` explains what has to be fixed first, `chain()` and
`retry_number()` let one decision row list every attempt that led to it.

The daemon calls `auto_retry()` once after reaping orphans (the case the
operator actually complained about: a restart should not require them to press
anything), then every `SWEEP_SECONDS`. `NODUS_AUTO_RECOVER=0` turns both the
startup pass and the sweep off; the board still classifies, it just stops
claiming work is being retried.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

from . import db, routes

#: How many retries a single run chain gets. The first retry is attempt 1 of 2.
RETRY_LIMIT = int(os.environ.get("NODUS_RETRY_LIMIT", "2"))

#: Seconds to wait before attempt 1, then attempt 2. Orphaned runs skip this:
#: the process that would have been racing us is already gone.
BACKOFF = (60.0, 300.0)

#: A failure older than this is history, not an interruption. Without a floor,
#: the first daemon start would resume every failure ever recorded.
MAX_AGE = float(os.environ.get("NODUS_AUTO_RECOVER_MAX_AGE", str(24 * 3600)))

#: How often the daemon looks for newly retryable failures.
SWEEP_SECONDS = float(os.environ.get("NODUS_AUTO_RECOVER_INTERVAL", "300"))

NOTE_PREFIX = "auto-retry"

#: A corrupt `parent_run_id` chain must terminate. Nobody retries 50 times.
MAX_CHAIN = 50

#: Failure signals, checked in this order. Deterministic reasons come before
#: network ones on purpose: a 401 wrapped in a connection reset is still a
#: rejected key, and retrying it burns a retry that a transient error needs.
_ORPHANED = re.compile(r"orphaned", re.I)
_AUTH = re.compile(
    r"\b(?:401|403)\b|unauthoriz|forbidden|invalid[\s_-]*api[\s_-]*key|"
    r"authenticat|authoriz|api[\s_-]*key|密钥|鉴权|未授权|认证失败|授权失败",
    re.I,
)
_PARAM = re.compile(
    r"\b400\b|bad request|invalid[\s_-]?(?:request|argument|parameter|param|input)|"
    r"unrecognized|unknown (?:argument|parameter)|validation error|missing required|"
    r"参数错误|参数不合法|参数无效|参数缺失",
    re.I,
)
_POLICY = re.compile(
    r"content[\s_-]?policy|safety|content[\s_-]?filter|blocked by|违规|"
    r"内容[^\n]{0,10}(?:安全|审核|被拦|拦截)",
    re.I,
)
_MODEL = re.compile(
    r"model[\s_-]?not[\s_-]?found|model_not_found|no such model|unknown model|"
    r"invalid model|model[^.\n]{0,40}(?:does not exist|not available|not found)|"
    r"模型[^\n。]{0,12}(?:不存在|下线|不可用|名不对|弃用)",
    re.I,
)
_RATE = re.compile(
    r"rate[\s_-]?limit|\b429\b|too many requests|overloaded|slow down|"
    r"限流|频率限制|请求过多|太频繁",
    re.I,
)
_TIMEOUT = re.compile(r"timed?[\s_-]?out|\btimeout\b|no output for|etimedout|超时", re.I)
_NETWORK = re.compile(
    r"econnreset|econnrefused|econnaborted|etimedout|enotfound|eai_again|epipe|"
    r"ehostunreach|enetunreach|connection (?:reset|refused|closed|aborted|error)|"
    r"reset by peer|broken pipe|fetch failed|network|socket|dns|tls|ssl|"
    r"网络|连接(?:断|被重置|失败|超时)|断线|掉线",
    re.I,
)
_UPSTREAM = re.compile(
    r"\b5\d\d\b|internal server error|bad gateway|service unavailable|gateway timeout|"
    r"upstream|上游|服务(?:器)?(?:错误|不可用|异常)",
    re.I,
)

_TRANSIENT = {"orphaned", "timeout", "network", "rate_limit", "upstream"}
_DETERMINISTIC = {"auth", "param", "policy", "model"}

_PROBLEM = {
    "orphaned": "执行到一半，启动它的进程退出了（不是模型出错）。",
    "timeout": "跑太久没有响应，被当成卡死中止了。",
    "rate_limit": "上游限流了，等一会儿可以再试。",
    "network": "网络断了（连接被重置或超时），这次执行没走完。",
    "upstream": "上游服务出错（5xx），不是任务本身的问题。",
    "auth": "密钥被上游拒绝（401/403），重试不会好，需要换密钥或补权限。",
    "param": "请求参数被拒绝（400），重试不会好，需要改配置或这次指令。",
    "policy": "内容被上游安全策略拦了，重试不会好。",
    "model": "模型名不存在或没有权限，重试不会好，需要换模型。",
    "unknown": "执行失败，从报错里看不出是环境问题还是任务本身的问题。",
}

_SUGGESTION = {
    "orphaned": "不用你管：系统会从断点重新派一轮，有会话就接着会话。",
    "timeout": "不用你管：退避后会自动再试，连续失败才会来找你。",
    "network": "不用你管：退避后会自动再试，连续失败才会来找你。",
    "rate_limit": "不用你管：退避后会自动再试，连续失败才会来找你。",
    "upstream": "不用你管：退避后会自动再试，连续失败才会来找你。",
    "auth": "换好密钥或补权限后点「重跑」，任务本身不用改。",
    "param": "检查这次指令/模型/参数，改完再重跑。",
    "policy": "换个说法或换个模型再派。",
    "model": "换一个可用模型再重跑。",
    "unknown": "点「看日志」看一眼，再决定重跑还是自己接手。",
}

#: Why a retry cannot help, in the operator's terms. Shown on the one section
#: that exists for deterministic failures, so "fix the cause first" is a reason
#: and not a rule the reader has to trust.
_WHY_NOT_RETRIED = {
    "auth": "上游判定这次请求无权（401/403）。密钥和权限不变，再发一次还是同一个结果。",
    "param": "请求本身不合法（400）。参数不改，重发只会得到同一个 400。",
    "policy": "输入被上游安全策略拦下。同样的内容再发一次还是被拦。",
    "model": "模型名不存在或这个密钥没有该模型权限。换模型之前，重试没有意义。",
}


def cause(run: dict) -> str:
    """The failure reason this module matched, for callers that render it."""
    return _cause(run)


def why_not_retried(run: dict) -> str:
    """Why this deterministic failure must be fixed before it can run again."""
    return _WHY_NOT_RETRIED.get(_cause(run), "")


def enabled() -> bool:
    """Is automatic recovery on? The board uses this to avoid lying."""
    return os.environ.get("NODUS_AUTO_RECOVER", "1") != "0"


def _cause(run: dict) -> str:
    """The first matching reason, from most specific to most guessy."""
    text = str(run.get("error") or "")
    if not text.strip():
        return "unknown"
    for name, pattern in (
        ("orphaned", _ORPHANED),
        ("auth", _AUTH),
        ("param", _PARAM),
        ("policy", _POLICY),
        ("model", _MODEL),
        ("timeout", _TIMEOUT),
        ("rate_limit", _RATE),
        ("network", _NETWORK),
        ("upstream", _UPSTREAM),
    ):
        if pattern.search(text):
            return name
    return "unknown"


def classify(run: dict) -> str:
    """Transient, deterministic, or human. Unknown is human on purpose."""
    reason = _cause(run)
    if reason in _TRANSIENT:
        return "transient"
    if reason in _DETERMINISTIC:
        return "deterministic"
    return "human"


def problem_text(run: dict) -> str:
    """The one sentence a person reads on the board instead of a stack trace."""
    return _PROBLEM[_cause(run)]


def suggestion_text(run: dict) -> str:
    """What to do about it, in the operator's terms."""
    return _SUGGESTION[_cause(run)]


def _is_retry(run: dict) -> bool:
    return str(run.get("note") or "").lstrip().startswith(NOTE_PREFIX)


_RETRY_NUMBER = re.compile(
    r"^\s*auto-retry(?:\s+(\d+)(?:\s*/\s*\d+)?)?(?![a-z])", re.I
)


def _retry_label(note) -> int:
    """The attempt number in an `auto-retry N/M` note, or 0 for anything else."""
    match = _RETRY_NUMBER.match(str(note or ""))
    if not match:
        return 0
    return int(match.group(1)) if match.group(1) else 1


def _chain_root(run: dict, rows: dict[str, dict]) -> str:
    """Walk `parent_run_id` to the top of the chain, bounded against a cycle."""
    current = str(run.get("id") or "")
    seen: set[str] = set()
    while current and current not in seen and len(seen) < MAX_CHAIN:
        seen.add(current)
        row = rows.get(current)
        if row is None:
            row = db.get_run(current)
            if not row:
                break
            rows[current] = row
        parent = str(row.get("parent_run_id") or "")
        if not parent:
            break
        current = parent
    return current


def retry_depth(run: dict) -> int:
    """How many automatic retries already sit above/including this run.

    The original failure has depth 0 (the next attempt would be 1); its retry
    child has depth 1, and so on. The walk is by `parent_run_id`, so two
    independent failures of the same task each get their own budget instead of
    sharing one counter.
    """
    depth = 0
    cur: dict | None = run
    seen: set[str] = set()
    while cur:
        rid = str(cur.get("id") or "")
        if not rid or rid in seen:
            break
        seen.add(rid)
        if _is_retry(cur):
            depth += 1
        parent = str(cur.get("parent_run_id") or "")
        cur = db.get_run(parent) if parent else None
    return depth


def attempts(run: dict) -> int:
    """How many automatic retries this run's chain has already used.

    The convention is the `parent_run_id` chain plus the note: every run in the
    same chain whose note starts with `auto-retry` counts. The original failure
    and all of its descendants therefore report the same number — 2 after two
    retries — which is the "this task has been retried N times" figure the
    board shows. Two independent failures of one task each count their own
    chain, so they do not share a budget.
    """
    tid = str(run.get("task_id") or "")
    if not tid:
        return retry_depth(run)
    rows = {str(row.get("id") or ""): row for row in db.list_runs(tid, limit=500)}
    rows.setdefault(str(run.get("id") or ""), run)
    root = _chain_root(run, rows)
    labels = [
        label
        for other in rows.values()
        if _chain_root(other, rows) == root
        for label in (_retry_label(other.get("note")),)
        if label
    ]
    return max(len(labels), max(labels, default=0))


def is_retry(run: dict) -> bool:
    """Did this run get started by recovery rather than by a person?"""
    return _is_retry(run)


def retry_number(run: dict) -> int:
    """The attempt number in this run's `auto-retry N/M` note (0 otherwise)."""
    return _retry_label(run.get("note"))


def chain(run: dict) -> list[dict]:
    """The whole retry chain this run belongs to, oldest first.

    Bounded by `MAX_CHAIN`: a corrupt `parent_run_id` must not turn one board
    request into an infinite walk. The list is what the board's "试过什么"
    line reads: the original failure plus each retry that followed it.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    current: dict | None = run
    while current:
        rid = str(current.get("id") or "")
        if not rid or rid in seen or len(rows) >= MAX_CHAIN:
            break
        seen.add(rid)
        rows.append(current)
        parent = str(current.get("parent_run_id") or "")
        current = db.get_run(parent) if parent else None
    rows.reverse()
    return rows


def _ended(run: dict) -> float:
    return float(run.get("ended_at") or run.get("started_at") or 0)


def _task(run: dict) -> dict:
    tid = str(run.get("task_id") or "")
    return db.get_task(tid) if tid else {}


def _has_retry_child(run: dict) -> bool:
    """One retry per failed run. A second child would be a second invoice."""
    rid = str(run.get("id") or "")
    if not rid:
        return False
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM run WHERE parent_run_id=? LIMIT 1", (rid,)
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _task_has_live_run(run: dict) -> bool:
    tid = str(run.get("task_id") or "")
    if not tid:
        return False
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM run WHERE task_id=? AND status IN ('queued','running') LIMIT 1",
            (tid,),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _over_budget(run: dict, task: dict | None = None) -> bool:
    """Does this task's own budget say stop?

    `task.budget_json` has been in the schema since the beginning and nothing
    enforced it. It is read here, narrowly: an absent or unreadable budget
    means "no limit", not "block everything". Recognised keys:

        {"max_runs": 20}                     total runs for this task
        {"max_cost": 5.0, "currency": "CNY"} priced spend for this task

    A budget is the operator's line, and an automatic retry must not cross it.
    """
    task = task if task is not None else _task(run)
    raw = task.get("budget_json") if task else None
    if not raw:
        return False
    try:
        budget = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(budget, dict):
        return False
    tid = str(run.get("task_id") or "")
    if not tid:
        return True
    max_runs = budget.get("max_runs")
    if isinstance(max_runs, (int, float)) and max_runs > 0:
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM run WHERE task_id=?", (tid,)
            ).fetchone()
        finally:
            conn.close()
        if row and int(row["n"]) >= int(max_runs):
            return True
    max_cost = budget.get("max_cost")
    if isinstance(max_cost, (int, float)) and max_cost > 0:
        currency = str(budget.get("currency") or "").upper()
        spent = 0.0
        for other in db.list_runs(tid, limit=500):
            try:
                usage = json.loads(other.get("usage_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if currency and str(usage.get("currency") or "").upper() != currency:
                continue
            cost = usage.get("cost")
            if isinstance(cost, (int, float)):
                spent += float(cost)
        if spent >= float(max_cost):
            return True
    return False


def _due_at(run: dict) -> float:
    """When the next attempt may start. An orphan has nothing to wait for."""
    ended = _ended(run)
    if _cause(run) == "orphaned":
        return ended
    depth = min(retry_depth(run), len(BACKOFF) - 1)
    return ended + BACKOFF[depth]


def block_reason(
    run: dict,
    limit: int | None = None,
    now: float | None = None,
    task: dict | None = None,
) -> str:
    """Why this run must not be retried right now; empty string means retry.

    Public because the board asks the same question: a transient failure with
    attempts left belongs to "auto recovering", the same failure without them
    is a human decision again. `limit=None` reads `RETRY_LIMIT` now instead of
    at import, so an env change or a test override is not silently ignored.
    """
    limit = RETRY_LIMIT if limit is None else int(limit)
    if str(run.get("status") or "") != "failed":
        return "not-failed"
    cls = classify(run)
    if cls != "transient":
        return cls
    if not run.get("task_id") or not run.get("cwd"):
        return "missing-context"
    task = task if task is not None else _task(run)
    if not task:
        return "task-gone"
    if retry_depth(run) + 1 > limit:
        return "retry-limit"
    if _has_retry_child(run):
        return "already-retried"
    if _task_has_live_run(run):
        return "task-running"
    if _over_budget(run, task):
        return "over-budget"
    ended = _ended(run)
    if not ended:
        return "no-end-time"
    now = time.time() if now is None else now
    if now - ended > MAX_AGE:
        return "too-old"
    if now < _due_at(run):
        return "backoff"
    return ""


def should_retry(
    run: dict, limit: int | None = None, now: float | None = None
) -> bool:
    """Will the next sweep retry this run? Kept pure enough to unit-test."""
    return block_reason(run, limit, now) == ""


def next_retry_at(
    run: dict, limit: int | None = None, now: float | None = None
) -> float | None:
    """When the next attempt may start, or None when no retry is coming.

    None is the honest answer for a deterministic failure, an exhausted chain,
    an over-budget task, or one whose retry is already in flight. During the
    backoff window this is the timestamp itself — what the board needs to say
    "下次什么时候". An orphan has nothing to wait for, so it is its own end.
    """
    reason = block_reason(run, limit, now)
    if reason not in ("", "backoff"):
        return None
    return _due_at(run)


def _session_id(run: dict) -> str:
    try:
        ref = json.loads(run.get("session_ref_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    return str((ref or {}).get("session_id") or "")


def _note(run: dict, attempt: int, limit: int) -> str:
    """First line is the label; the rest is the only instruction a resume gets.

    The previous failure's own last 300 characters go in with it: a retry that
    only says "it failed" asks the worker to rediscover the same wall. The
    label still comes first because `_retry_label` and the live row parse it.
    """
    previous = "原始执行" if attempt <= 1 else f"第 {attempt - 1} 次重试"
    tail = str(run.get("error") or "").strip()
    if len(tail) > 300:
        tail = "…" + tail[-300:]
    detail = f"上一次（{previous}）失败：{tail}\n" if tail else ""
    return (
        f"{NOTE_PREFIX} {attempt}/{limit}\n"
        f"{detail}"
        "请继续完成原任务：先看工作目录里已有的改动，不要重复已经做过的步骤。"
        "如果上次是测试红，先修失败的那条测试；看不出原因就先复现一次，再改。"
    )


def _start_retry(run: dict, attempt: int, limit: int) -> dict:
    """Create the child run and hand it to the normal runner.

    The endpoint that actually served the failed attempt is pinned when the
    route still contains it: a resumed session lives in that endpoint's config
    home, so falling back to the primary would look up a session that is not
    there. A changed route falls back to its own defaults rather than guessing.
    """
    from . import runner  # local import: runner imports this module's siblings

    route_name = str(run.get("route") or run.get("agent") or "")
    route = routes.resolve_for_run(route_name)
    provider = str(run.get("provider") or "")
    pin = provider if provider in route.providers else ""
    model = str(run.get("model") or "") if pin else ""
    return runner.RUNNER.start(
        task_id=str(run["task_id"]),
        route_name=route.name,
        cwd=str(run.get("cwd") or ""),
        parent_run_id=str(run["id"]),
        note=_note(run, attempt, limit),
        effort=str(run.get("effort") or ""),
        model=model,
        session_id=_session_id(run),
        session_mode="resume",
        profile=str(run.get("profile") or ""),
        provider=pin,
    )


def auto_retry(
    limit: int | None = None, dry_run: bool = False, now: float | None = None
) -> dict:
    """Scan failures and start a child run for each transient one.

    Returns what was done, not just a count: the caller (daemon log, a human
    checking the board, a test) needs to see which run was retried and which
    one was deliberately left alone, with the reason. A retry that could not be
    started is an `errors` entry rather than a silent success.

    `dry_run=True` makes the same decisions and writes nothing — no run row, no
    event, no session copy. Its `child_run_id` values are empty.
    """
    limit = RETRY_LIMIT if limit is None else int(limit)
    now = time.time() if now is None else now
    runs = db.list_runs(limit=500)
    failed = [r for r in runs if str(r.get("status") or "") == "failed"]
    facts = _facts([r.get("task_id") or "" for r in failed])
    out: dict = {"retried": [], "skipped": [], "errors": [], "considered": len(failed)}
    for run in failed:
        tid = str(run.get("task_id") or "")
        title = (facts.get(tid) or {}).get("title") or _first_line(run.get("note")) or ""
        reason = block_reason(run, limit, now)
        if reason:
            out["skipped"].append(
                {
                    "run_id": str(run.get("id") or ""),
                    "task_id": tid,
                    "task_title": title,
                    "class": classify(run),
                    "cause": cause(run),
                    "reason": reason,
                }
            )
            continue
        attempt = retry_depth(run) + 1
        if dry_run:
            out["retried"].append(
                {
                    "run_id": str(run.get("id") or ""),
                    "child_run_id": "",
                    "task_id": tid,
                    "task_title": title,
                    "attempt": attempt,
                    "limit": limit,
                    "cause": cause(run),
                }
            )
            continue
        try:
            child = _start_retry(run, attempt, limit)
        except Exception as exc:  # noqa: BLE001 - one bad run must not stop the sweep
            out["errors"].append(
                {
                    "run_id": str(run.get("id") or ""),
                    "task_id": tid,
                    "task_title": title,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        try:
            db.append_event(
                str(run["id"]),
                db.next_seq(str(run["id"])),
                "recover.retried",
                {
                    "child": child.get("id") or "",
                    "attempt": attempt,
                    "limit": limit,
                    "reason": problem_text(run),
                },
            )
        except Exception:  # noqa: BLE001 - the retry is already running
            pass
        out["retried"].append(
            {
                "run_id": str(run.get("id") or ""),
                "child_run_id": str(child.get("id") or ""),
                "task_id": tid,
                "task_title": title,
                "attempt": attempt,
                "limit": limit,
                "cause": cause(run),
            }
        )
    return out


def recovering(now: float | None = None) -> list[dict]:
    """Rows for the board's "auto retrying" block.

    Two shapes are the same fact: a failed run waiting out its backoff, and a
    live retry child. `run_id` names the run the row is about, so a click can
    open the right log; `attempt` is which retry that is (1 of `limit`); and
    `next_retry_at` is None while the retry is already running.
    """
    if not enabled():
        return []
    now = time.time() if now is None else now
    runs = db.list_runs(limit=200)
    facts = _facts([r.get("task_id") or "" for r in runs])
    out: list[dict] = []
    for run in runs:
        status = str(run.get("status") or "")
        tid = str(run.get("task_id") or "")
        title = (facts.get(tid) or {}).get("title") or _first_line(run.get("note")) or ""
        base = {
            "task_id": tid,
            "task_title": title,
            "project": _project(run.get("cwd")),
            "limit": RETRY_LIMIT,
            # What failed the last time: one sentence, so the row can say
            # "waiting to retry the timeout" instead of just an attempt count.
            "problem": problem_text(run),
        }
        if status in ("queued", "running") and _is_retry(run):
            out.append(
                {
                    **base,
                    "run_id": str(run.get("id") or ""),
                    "attempt": max(1, retry_depth(run)),
                    "next_retry_at": None,
                }
            )
            continue
        if status != "failed":
            continue
        reason = block_reason(run, RETRY_LIMIT, now)
        if reason not in ("", "backoff"):
            continue
        out.append(
            {
                **base,
                "run_id": str(run.get("id") or ""),
                "attempt": retry_depth(run) + 1,
                "next_retry_at": _due_at(run),
            }
        )
    return out


def summary(result: dict) -> str:
    """One line for `daemon.log`, no JSON."""
    retried = result.get("retried") or []
    skipped = result.get("skipped") or []
    errors = result.get("errors") or []
    if not retried and not skipped and not errors:
        return "nothing to recover"
    parts = [f"retried {len(retried)}"]
    if retried:
        parts.append(
            ", ".join(
                f"{item['run_id']}->{item['child_run_id']} ({item['attempt']}/{item['limit']})"
                for item in retried
            )
        )
    if skipped:
        by_reason: dict[str, int] = {}
        for item in skipped:
            by_reason[item["reason"]] = by_reason.get(item["reason"], 0) + 1
        parts.append(
            "left alone: "
            + ", ".join(f"{reason} {n}" for reason, n in sorted(by_reason.items()))
        )
    if errors:
        parts.append(f"{len(errors)} could not start")
    return "; ".join(parts)


# ---- daemon wiring ----------------------------------------------------

_started = False
_start_lock = threading.Lock()


def start(interval: float | None = None, log=None) -> bool:
    """Run one recovery pass now, then keep sweeping in the background.

    Called by `server.serve()` right after orphan reaping. The first pass is
    synchronous so the startup log explains what happened before the port
    answers; the loop is a daemon thread, so it never keeps the process alive.
    """
    global _started
    with _start_lock:
        if _started:
            return False
        _started = True
    interval = SWEEP_SECONDS if interval is None else interval
    try:
        first = auto_retry()
        if log and (first.get("retried") or first.get("skipped") or first.get("errors")):
            log(f"auto-recover: {summary(first)}")
    except Exception:  # noqa: BLE001 - a bad sweep must not stop the daemon
        if log:
            import traceback

            log("auto-recover failed:\n" + traceback.format_exc())

    def loop() -> None:
        while True:
            time.sleep(interval)
            try:
                result = auto_retry()
                if log and (result.get("retried") or result.get("errors")):
                    log(f"auto-recover: {summary(result)}")
            except Exception:  # noqa: BLE001
                if log:
                    import traceback

                    log("auto-recover sweep failed:\n" + traceback.format_exc())

    threading.Thread(target=loop, daemon=True, name="nodus-recover").start()
    return True


# ---- small shared helpers ---------------------------------------------


def _facts(task_ids: list[str]) -> dict[str, dict]:
    """Title + goal for many tasks in one query."""
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


def _first_line(text) -> str:
    return str(text or "").strip().split("\n")[0].strip()


def _project(cwd) -> str:
    """Last path segment, whichever slash the platform recorded."""
    value = str(cwd or "").rstrip("/\\")
    if not value:
        return ""
    return re.split(r"[\\/]", value)[-1]
