"""Recovery: hand the failures a machine can fix back to the machine.

The board used to end every failure with the same question — "rerun it?" — and
answering it needed a human who knows whether a 429 means "wait a minute" or
"the key is dead". A remote operator who logs in twice a day cannot answer it
in time, and the cost is not the retry; it is the waiting.

So the line is drawn once, here:

    transient      the endpoint or the owner process failed. The task did
                   nothing wrong, and the same work is worth another attempt.
    deterministic  the request itself is wrong. Another attempt repeats it.
    human          nothing proves either way.

Only `transient` is retried: at most `RETRY_LIMIT` times per chain, never
inside the backoff window, never across the task's budget. Everything else is
reported, not acted on. Unknown text is `human` on purpose — a wrong retry
spends quota and can apply the same half-finished edit twice, so silence is
cheaper than a guess.

The retry is a child row, not a resurrection: the failed run keeps its record,
the child points back through `parent_run_id` and carries the original task,
directory, route, model and vendor session. This stage only writes that row;
starting it is the next stage's job, which is also why `dry_run` exists: the
decision can be inspected before anything is written.

`attempts` counts the automatic retries already recorded in this run's chain
(see its docstring for the exact convention), and `next_retry_at` is what the
board renders as "下次什么时候".
"""

from __future__ import annotations

import json
import re
import time

from . import db, routes

#: Automatic retries allowed per run chain. Two is enough for a provider blip
#: and small enough that a genuinely broken task comes back to a human.
RETRY_LIMIT = 2

#: First retry after a minute, the second after five. Exponential rather than
#: fixed: a 429 clears in seconds, an outage takes longer, and a fixed one
#: minute would spend the second attempt while the provider is still down.
BACKOFF_SECONDS = 60.0
BACKOFF_FACTOR = 5.0

#: How many recent runs `auto_retry` scans. Recency, not age, is the fence: the
#: operator asked for interruptions to be resumed, and a laptop that was off
#: for a week is still an interruption, not history.
SCAN_LIMIT = 500

#: A corrupt `parent_run_id` chain must terminate. Nobody retries 50 times.
MAX_CHAIN = 50

#: The note that marks an automatic retry, and the label `attempts` counts by.
NOTE_PREFIX = "auto-retry"
NOTE_FORMAT = "auto-retry {attempt}/{limit}"

# ---- what the error text says -------------------------------------------

#: Checked in this order. Deterministic reasons come before network ones on
#: purpose: a 401 wrapped in "connection reset" is still a rejected key, and
#: retrying it spends an attempt that a transient failure needs. A 5xx comes
#: last because `\b5\d\d\b` is the least specific signal in the list.
_CAUSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "orphaned",
        re.compile(
            r"orphaned|process that owned|owner (?:is )?gone|no activity for \d+\s*s|"
            r"进程(?:退出|没了)|启动它的进程",
            re.I,
        ),
    ),
    (
        "auth",
        re.compile(
            r"\b40[13]\b|unauthoriz|forbidden|invalid[\s_-]*api[\s_-]*key|"
            r"(?:missing|no) (?:api )?key|authenticat|authoriz|permission denied|"
            r"access denied|not authorized|密钥|鉴权|权限不足|没有权限|无权访问",
            re.I,
        ),
    ),
    (
        "model",
        re.compile(
            r"model[\s_-]*(?:not[\s_-]*(?:found|supported)|unavailable|deprecated|"
            r"does not exist)|no such model|unknown model|invalid model|"
            r"模型[^\n。]{0,12}(?:不存在|下线|不可用|名不对|弃用)",
            re.I,
        ),
    ),
    (
        "policy",
        re.compile(
            r"content[\s_-]*(?:policy|filter)|policy violation|safety|moderation|"
            r"blocked by (?:policy|safety)|违规|内容[^\n]{0,10}"
            r"(?:安全|审核|被拦|拦截|拒绝|不合规)",
            re.I,
        ),
    ),
    (
        "account",
        re.compile(
            r"insufficient[\s_-]*(?:quota|balance|credit)|余额不足|额度(?:用尽|不足)|"
            r"欠费|充值|billing|payment required|credit balance",
            re.I,
        ),
    ),
    (
        "param",
        re.compile(
            r"\b400\b|\b404\b|bad request|invalid[\s_-]?(?:request|argument|parameter|"
            r"param|input)|unrecognized|unknown (?:argument|parameter)|validation error|"
            r"missing required|参数(?:错误|不合法|无效|缺失)|不支持的参数|"
            r"context[\s_-]?length|maximum context|too many tokens|token limit|"
            r"context window|not found",
            re.I,
        ),
    ),
    (
        "timeout",
        re.compile(
            r"timed?[\s_-]?out|\btimeout\b|no output for \d+\s*s|超时|无响应|"
            r"没有响应|等了[^\n]{0,6}(?:没有|没)[^\n]{0,4}(?:结果|响应)",
            re.I,
        ),
    ),
    (
        "rate_limit",
        re.compile(
            r"rate[\s_-]?limit|\b429\b|too many requests|overloaded|slow down|"
            r"限流|频率限制|请求过多|太频繁",
            re.I,
        ),
    ),
    (
        "network",
        re.compile(
            r"econnreset|econnrefused|econnaborted|etimedout|enotfound|eai_again|"
            r"epipe|ehostunreach|enetunreach|connection (?:reset|refused|closed|"
            r"aborted|error|lost)|reset by peer|broken pipe|fetch failed|network|"
            r"socket|dns|tls|ssl|网络|连接(?:断|被重置|失败|超时)|断线|掉线",
            re.I,
        ),
    ),
    (
        "upstream",
        re.compile(
            r"\b5\d\d\b|internal server error|bad gateway|service unavailable|"
            r"gateway timeout|upstream|上游|服务(?:器)?(?:错误|不可用|异常)|服务端",
            re.I,
        ),
    ),
)

_TRANSIENT = frozenset({"orphaned", "timeout", "network", "rate_limit", "upstream"})
_DETERMINISTIC = frozenset({"auth", "param", "policy", "model", "account"})

#: One sentence per cause, for a reader who is not going to open the log.
_PROBLEM = {
    "orphaned": "跑到一半，启动它的进程退出了（不是模型出错）",
    "timeout": "运行超时，没有收到结果",
    "network": "网络断了，结果没传回来",
    "rate_limit": "上游限流了（429），等一会儿可以自动重试",
    "upstream": "上游服务出错（5xx），不是这次任务本身的问题",
    "auth": "这家供应商拒绝了请求（密钥或权限问题），重试没有意义",
    "param": "请求参数被拒绝了（400），重试没有意义",
    "policy": "内容被供应商的安全策略拦了，重试没有意义",
    "model": "模型名不对或这个模型已经下线，重试没有意义",
    "account": "账户余额或额度不足，充值之前重试没有意义",
    "unknown": "这次失败看不出原因（不是超时也不是网络问题），需要人看一眼",
}

_RETRY_NOTE = re.compile(
    r"^auto-retry(?:\s+(\d+)(?:\s*/\s*(\d+))?)?(?![a-z])", re.I
)


def _cause(run: dict) -> str:
    """The first matching reason, from most specific to most guessy."""
    text = str(run.get("error") or "")
    if not text.strip():
        return "unknown"
    for name, pattern in _CAUSES:
        if pattern.search(text):
            return name
    return "unknown"


def classify(run: dict) -> str:
    """`transient`, `deterministic`, or `human`. Unknown is human on purpose."""
    cause = _cause(run)
    if cause in _TRANSIENT:
        return "transient"
    if cause in _DETERMINISTIC:
        return "deterministic"
    return "human"


def problem_text(run: dict) -> str:
    """The one sentence a person reads instead of the raw error."""
    return _PROBLEM[_cause(run)]


# ---- retry bookkeeping ---------------------------------------------------


def _cached_run(rid: str, cache: dict) -> dict:
    key = ("run", rid)
    if key not in cache:
        cache[key] = db.get_run(rid) if rid else {}
    return cache[key]


def _cached_task(tid: str, cache: dict) -> dict:
    key = ("task", tid)
    if key not in cache:
        cache[key] = db.get_task(tid) if tid else {}
    return cache[key]


def _cached_runs(tid: str, cache: dict) -> list[dict]:
    key = ("runs", tid)
    if key not in cache:
        cache[key] = db.list_runs(tid, limit=SCAN_LIMIT) if tid else []
    return cache[key]


def _root_of(rid: str, by_id: dict[str, dict], cache: dict) -> str:
    """Topmost parent of a run, bounded against a broken chain."""
    current = rid
    seen: set[str] = set()
    while current and current not in seen and len(seen) < MAX_CHAIN:
        seen.add(current)
        row = by_id.get(current) or _cached_run(current, cache)
        parent = str((row or {}).get("parent_run_id") or "")
        if not parent:
            return current
        current = parent
    return current or rid


def _retry_number(note) -> int:
    """The attempt number in a note, or 0 when the note is not a retry."""
    match = _RETRY_NOTE.match(str(note or "").strip())
    if not match:
        return 0
    return int(match.group(1)) if match.group(1) else 1


def _attempts(run: dict, cache: dict) -> int:
    rid = str(run.get("id") or "")
    if not rid:
        return 0
    tid = str(run.get("task_id") or "")
    rows = _cached_runs(tid, cache) if tid else []
    by_id = {str(row.get("id") or ""): row for row in rows if row.get("id")}
    by_id.setdefault(rid, run)
    root = _root_of(rid, by_id, cache)
    marks: list[int] = []
    for other_id, other in by_id.items():
        if _root_of(other_id, by_id, cache) != root:
            continue
        number = _retry_number(other.get("note"))
        if number:
            marks.append(number)
    # Both the count and the highest label matter: a duplicated note must not
    # buy an extra retry, and a numeric label must not hide a missing sibling.
    return max([len(marks), *marks], default=0)


def attempts(run: dict) -> int:
    """How many automatic retries this run's chain already used.

    The convention is the `parent_run_id` chain plus the note: every run in the
    same chain whose note starts with `auto-retry` counts, and the highest
    `auto-retry N/M` label wins when the two disagree. A second failure chain
    of the same task has its own budget, so one task can hold two independent
    retry budgets without sharing a counter.

    The original failure returns 0; its first child returns 1.
    """
    return _attempts(run, {})


def _due_at(run: dict, done: int | None = None) -> float:
    """When the next attempt may start, in epoch seconds."""
    base = run.get("ended_at") or run.get("started_at") or db.now()
    count = _attempts(run, {}) if done is None else done
    return float(base) + BACKOFF_SECONDS * BACKOFF_FACTOR**count


def _has_retry_child(run: dict, cache: dict) -> bool:
    """Any automatic retry child, finished or live, means this run is not the tip."""
    rid = str(run.get("id") or "")
    if not rid:
        return False
    for other in _cached_runs(str(run.get("task_id") or ""), cache):
        if str(other.get("parent_run_id") or "") != rid:
            continue
        if _retry_number(other.get("note")):
            return True
    return False


def _task_has_live_run(tid: str, cache: dict) -> bool:
    for other in _cached_runs(tid, cache):
        if str(other.get("status") or "") in ("queued", "running"):
            return True
    return False


def _budget(task: dict) -> tuple[str, float, str] | None:
    """The task's own budget, if it has a readable one.

    `budget_json` has been in the schema since the beginning and nothing has
    enforced it. It is read narrowly here — an absent or unreadable value means
    "no limit", not "block everything". Recognised shapes:

        {"max_runs": 20}                      total runs for this task
        {"max_cost": 5.0, "currency": "CNY"}  priced spend for this task
        {"budget": 5}                         same, name the operator would use
        5                                     a bare number means max_cost
    """
    raw = task.get("budget_json") if task else None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        try:
            return ("cost", float(raw), "")
        except (TypeError, ValueError):
            return None
    if isinstance(data, bool):
        return None
    if isinstance(data, (int, float)):
        return ("cost", float(data), "")
    if not isinstance(data, dict):
        return None
    max_runs = data.get("max_runs")
    if isinstance(max_runs, (int, float)) and not isinstance(max_runs, bool):
        if max_runs > 0:
            return ("runs", float(max_runs), "")
    currency = str(data.get("currency") or data.get("unit") or "").upper()
    for key in ("max_cost", "budget", "max_spend", "limit"):
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value > 0:
            return ("cost", float(value), currency)
        if isinstance(value, dict):
            for unit, amount in value.items():
                if isinstance(amount, (int, float)) and amount > 0:
                    return ("cost", float(amount), str(unit).upper() or currency)
    return None


def _spent(runs: list[dict], currency: str) -> float:
    total = 0.0
    for other in runs:
        try:
            usage = json.loads(other.get("usage_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(usage, dict):
            continue
        if currency and str(usage.get("currency") or "").upper() != currency:
            continue
        cost = usage.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            total += float(cost)
    return total


def _over_budget(run: dict, task: dict, cache: dict) -> bool:
    budget = _budget(task)
    if budget is None:
        return False
    kind, limit, currency = budget
    rows = _cached_runs(str(run.get("task_id") or ""), cache)
    if kind == "runs":
        return len(rows) >= limit
    return _spent(rows, currency) >= limit


def _block_reason(
    run: dict,
    limit: int = RETRY_LIMIT,
    now: float | None = None,
    planned: set[str] | None = None,
    cache: dict | None = None,
) -> str:
    """Why this run must not be retried now; empty string means it may be."""
    if str(run.get("status") or "") != "failed":
        return "not-failed"
    kind = classify(run)
    if kind != "transient":
        return kind
    tid = str(run.get("task_id") or "")
    if not tid or not str(run.get("cwd") or ""):
        return "missing-context"
    cache = {} if cache is None else cache
    task = _cached_task(tid, cache)
    if not task:
        return "task-gone"
    done = _attempts(run, cache)
    if done >= limit:
        return "retry-limit"
    if _over_budget(run, task, cache):
        return "over-budget"
    if _has_retry_child(run, cache):
        return "already-retried"
    if planned is not None and tid in planned:
        return "task-running"
    if _task_has_live_run(tid, cache):
        return "task-running"
    when = float(run.get("ended_at") or run.get("started_at") or db.now())
    when += BACKOFF_SECONDS * BACKOFF_FACTOR**done
    now = time.time() if now is None else now
    if now < when:
        return "backoff"
    return ""


def should_retry(run: dict, limit: int = RETRY_LIMIT) -> bool:
    """Would the next sweep start a retry for this run right now?"""
    return _block_reason(run, limit) == ""


def next_retry_at(
    run: dict, limit: int = RETRY_LIMIT, now: float | None = None
) -> float | None:
    """When the next automatic attempt may start, or None if none is coming.

    None is the honest answer for a deterministic failure, an exhausted chain,
    an over-budget task, or a run whose retry is already in flight. During the
    backoff window this is the timestamp itself — what the board needs to say
    "下次什么时候".
    """
    reason = _block_reason(run, limit, now=now)
    if reason in ("", "backoff"):
        return _due_at(run)
    return None


# ---- creating the retry --------------------------------------------------


def _agent_for(run: dict) -> str:
    """The agent that ran this, falling back to the route's own agent."""
    agent = str(run.get("agent") or "").strip()
    if agent:
        return agent
    name = str(run.get("route") or "").strip()
    if not name:
        return ""
    route = routes.resolve(name)
    return route.agent if route else ""


def _create_child(run: dict, attempt: int, limit: int) -> dict:
    """Write the child run. Starting it belongs to the next stage."""
    agent = _agent_for(run)
    if not agent:
        raise ValueError("run has no agent and no route to resolve one")
    child = db.create_run(
        task_id=str(run.get("task_id") or ""),
        agent=agent,
        cwd=str(run.get("cwd") or ""),
        parent_run_id=str(run.get("id") or ""),
        note=NOTE_FORMAT.format(attempt=attempt, limit=limit),
        effort=str(run.get("effort") or ""),
        model=str(run.get("model") or ""),
        route=str(run.get("route") or agent),
        provider=str(run.get("provider") or ""),
        profile=str(run.get("profile") or ""),
    )
    # The vendor session is part of "carry the work forward": the runner needs
    # its id to resume the conversation instead of starting over. The nodus
    # session itself needs no copy — it is inherited through `task_id`.
    session_ref = run.get("session_ref_json")
    if session_ref:
        db.update_run(child["id"], session_ref_json=session_ref)
        child = db.get_run(child["id"])
    return child


def auto_retry(limit: int = RETRY_LIMIT, dry_run: bool = False) -> dict:
    """Scan failures and create a child run for each one a retry can fix.

    Returns `{"retried": [...], "skipped": [...], "errors": [...]}` rather than
    a count: the caller (the daemon log, the board, a test) needs to see which
    run was retried and which was deliberately left alone, with the reason.

    `dry_run=True` computes the same answer and writes nothing — no run row, no
    event, no session copy. The `child_run_id` of a dry result is empty.
    """
    result: dict = {"retried": [], "skipped": [], "errors": []}
    now = time.time()
    planned: set[str] = set()
    cache: dict = {}
    for run in db.list_runs(limit=SCAN_LIMIT):
        if str(run.get("status") or "") != "failed":
            continue
        tid = str(run.get("task_id") or "")
        rid = str(run.get("id") or "")
        reason = _block_reason(run, limit, now=now, planned=planned, cache=cache)
        if reason:
            result["skipped"].append(
                {
                    "run_id": rid,
                    "task_id": tid,
                    "class": classify(run),
                    "reason": reason,
                    "problem": problem_text(run),
                }
            )
            continue
        attempt = _attempts(run, cache) + 1
        problem = problem_text(run)
        if dry_run:
            result["retried"].append(
                {
                    "run_id": rid,
                    "child_run_id": "",
                    "task_id": tid,
                    "attempt": attempt,
                    "limit": limit,
                    "problem": problem,
                }
            )
            if tid:
                planned.add(tid)
            continue
        try:
            child = _create_child(run, attempt, limit)
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the sweep
            result["errors"].append(
                {
                    "run_id": rid,
                    "task_id": tid,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if tid:
            planned.add(tid)
        result["retried"].append(
            {
                "run_id": rid,
                "child_run_id": str(child.get("id") or ""),
                "task_id": tid,
                "attempt": attempt,
                "limit": limit,
                "problem": problem,
            }
        )
    return result
