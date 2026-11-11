"""Automatic recovery: retry by value, not by a counter.

A retry spends money, so "failed" is not by itself a reason to spend it. Each
failed run is judged on five questions, in this order; the first one that says
no ends the conversation:

    the request     a 401/403/400, a content policy block, a model that does
                    not exist. Running it again returns the same answer and
                    bills for it. These are `deterministic`, shown on their own
                    section with what has to change first.
    how far it got  a run that never got going (0-1 tool calls, no files) is
                    cheap to redo; one that already edited a tree is worth
                    continuing in its own session instead of starting over.
    the signature   the same cause, the same key error and the same failure
                    stage as the previous attempt means the same death. A
                    second identical attempt is money for the same wall.
    the headroom    the remaining daily/task budget versus what one retry of
                    this task has cost historically. Not enough left is a
                    question for the operator, not a decision for the daemon.
    the cap         `SODUS_RETRY_COST_CAP` (CNY, default 0.5) is the most one
                    automatic retry may be estimated to cost; above it the
                    retry waits for a human click.

`RETRY_LIMIT` (default 3) is a ceiling, not a schedule: with no evidence of
progress the chain stops earlier, and a chain of identical failures stops
after the second one.

The retry is a *child* run, not a resurrection of the failed row: the failed
attempt keeps its own record, and the child points back with `parent_run_id`.
The original task, directory, route and model are kept. A retry that can
resume continues the same vendor session; one that cannot records what has to
be redone so the cost of starting over is on the record, not hidden.

The daemon calls `auto_retry()` once after reaping orphans (a restart should
not require pressing anything), then every `SWEEP_SECONDS`.
`SODUS_AUTO_RECOVER=0` turns both passes off; the board still classifies, it
just stops claiming work is being retried.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

from . import budget, config, db, diagnosis, ledger, routes

#: The ceiling on automatic retries per chain. Three is the operator's line
#: ("rerun is normal, unless two or three reruns still produce nothing"), but
#: the gates below are expected to stop a chain long before the ceiling does.
RETRY_LIMIT = int(os.environ.get("SODUS_RETRY_LIMIT", "3"))

#: Seconds to wait before attempt 1, 2, 3. Orphaned runs skip this: the
#: process that would have been racing us is already gone.
BACKOFF = (60.0, 300.0, 900.0)

#: A failure older than this is history, not an interruption. Without a floor,
#: the first daemon start would resume every failure ever recorded.
MAX_AGE = float(os.environ.get("SODUS_AUTO_RECOVER_MAX_AGE", str(24 * 3600)))

#: How often the daemon looks for newly retryable failures.
SWEEP_SECONDS = float(os.environ.get("SODUS_AUTO_RECOVER_INTERVAL", "300"))

NOTE_PREFIX = "auto-retry"

#: A corrupt `parent_run_id` chain must terminate. Nobody retries 50 times.
MAX_CHAIN = 50

#: The most one automatic retry may be estimated to cost, in CNY. Above this
#: the daemon does not spend: the retry becomes a question on the board.
COST_CAP = float(os.environ.get("SODUS_RETRY_COST_CAP", "0.5"))

#: Only costs in this currency are compared with the caps above. Converting at
#: an invented rate is how a 7x error gets into a budget.
COST_CURRENCY = "CNY"

#: The stage a failure happened in, inferred from what the worker last did.
_STAGE_LABEL = {
    "start": "刚启动",
    "edit": "改文件",
    "test": "跑测试",
    "shell": "跑命令",
    "tool": "调工具",
}
_EDIT_TOOL = re.compile(r"write|edit|patch|create|apply", re.I)
_SHELL_TOOL = re.compile(r"bash|shell|exec|command|run", re.I)
_TEST_CMD = re.compile(
    r"pytest|unittest|npm (?:run )?test|pnpm test|yarn test|go test|cargo test|"
    r"jest|vitest|tox|nox|make test|ctest",
    re.I,
)


def _short(text: str, n: int = 90) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= n else value[: n - 1].rstrip() + "…"


def cause(run: dict, facts: dict | None = None) -> str:
    """The failure reason `diagnosis` matched, for callers that render it."""
    return diagnosis.cause(run, facts)


def why_not_retried(run: dict) -> str:
    """Why this deterministic failure must be fixed before it can run again."""
    return diagnosis.why_not_retried(diagnosis.cause(run))


def enabled() -> bool:
    """Is automatic recovery on? The board uses this to avoid lying."""
    override = os.environ.get("SODUS_AUTO_RECOVER")
    if override is not None:
        return override.lower() in ("1", "true", "yes", "on")
    return bool(config.load_config().get("daemon", {}).get("auto_recover", False))


def _cause(run: dict) -> str:
    """Backwards-compatible name for `cause`; same answer, same table."""
    return cause(run)


def classify(run: dict, facts: dict | None = None) -> str:
    """transient / deterministic / human / none, from `diagnosis`.

    `recover` needs exactly one bit -- "may the machine try again" -- and
    treats `human` as a no for the same reason `deterministic` is a no: a
    person has to change something first. `none` is a run that has not failed.
    """
    return diagnosis.classify(run, facts)


def problem_text(run: dict) -> str:
    """The one sentence a person reads on the board instead of a stack trace."""
    return diagnosis.problem(diagnosis.cause(run))


def suggestion_text(run: dict) -> str:
    """What to do about it, in the operator's terms."""
    return diagnosis.suggestion(diagnosis.cause(run))


# ---- what the attempt did before it died -------------------------------


def _events(run: dict, prefixes: tuple[str, ...]) -> list[dict]:
    rid = str(run.get("id") or "")
    if not rid:
        return []
    try:
        return db.events_filtered(rid, prefixes=prefixes)
    except (KeyError, TypeError, ValueError):
        return []


def _changed_files(run: dict) -> list[str]:
    rid = str(run.get("id") or "")
    if not rid:
        return []
    try:
        artifacts = db.artifacts_for(rid)
    except (KeyError, TypeError):
        return []
    files: list[str] = []
    for artifact in artifacts:
        if artifact.get("kind") != "file":
            continue
        path = str(artifact.get("rel_path") or artifact.get("path") or "")
        if path and path not in files:
            files.append(path)
    return files


def progress(run: dict) -> dict:
    """How far the attempt got: tool calls made, files already changed.

    This is the cheap-vs-expensive axis. Zero or one tool call with no files
    touched means almost nothing has to be redone; a tree that already changed
    is worth continuing rather than starting over.
    """
    calls = len(_events(run, ("tool_call",)))
    files = len(_changed_files(run))
    return {
        "tool_calls": calls,
        "files": files,
        "barely": calls <= 1 and files == 0,
        "moved": calls >= 2 or files >= 1,
    }


def stage(run: dict) -> str:
    """Where the attempt died: start, edit, test, shell or tool.

    Read from what actually happened, not from the task's acceptance text: a
    run that never called a tool did not fail "while running tests".
    """
    rid = str(run.get("id") or "")
    if rid:
        try:
            if db.verify_states([rid]).get(rid) == "red":
                return "test"
        except (KeyError, TypeError):
            pass
    calls = _events(run, ("tool_call",))
    if not calls:
        return "start"
    payload = calls[-1].get("payload") or {}
    name = str(payload.get("name") or "")
    args = payload.get("input")
    args = args if isinstance(args, dict) else {}
    if _EDIT_TOOL.search(name):
        return "edit"
    if _SHELL_TOOL.search(name):
        command = str(args.get("command") or "")
        return "test" if _TEST_CMD.search(command) else "shell"
    return "tool"


def _key_error(run: dict) -> str:
    """The line of the error that names the failure, normalized for comparison.

    Paths, line numbers and long hex ids vary between attempts of the same
    failure; the sentence does not. Numbers that are the failure itself (401,
    exit code 2) are kept, so a different assertion is a different signature.
    """
    text = str(run.get("error") or "").strip()
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    key = ""
    for line in reversed(lines):
        if re.search(
            r"(?:error|exception|failed|failure|denied|not found|timed out|"
            r"超时|失败|refused|forbidden|unauthorized|exit code)",
            line,
            re.I,
        ):
            key = line
            break
    if not key and lines:
        key = lines[-1]
    key = re.sub(r"\b[0-9a-f]{8,}\b", "#", key, flags=re.I)
    key = re.sub(r"(?i)\bline\s+\d+\b", "line #", key)
    key = re.sub(r":\d+:", ":#:", key)
    key = re.sub(r"\s+", " ", key).strip().lower()
    return key[:160]


def signature(run: dict) -> dict:
    """The identity of this failure: cause + key error + stage."""
    return {"cause": cause(run), "error": _key_error(run), "stage": stage(run)}


def signature_text(sig: dict | None) -> str:
    """One readable phrase for a signature, used in the "why not retry" line."""
    sig = sig or {}
    where = _STAGE_LABEL.get(str(sig.get("stage") or ""), "未知阶段")
    what = str(sig.get("error") or "").strip() or diagnosis.problem(
        str(sig.get("cause") or "")
    )
    return f"都在{where}阶段、都是「{_short(what, 80)}」"


def previous_attempt(run: dict) -> dict:
    """The attempt this one should be compared with, or {} when it is the first.

    The parent is the previous attempt by definition. A manually dispatched
    rerun has no parent, so the newest earlier failure of the same task is
    used instead: the point is to compare against *the last attempt*, not to
    insist on the automatic chain.
    """
    rid = str(run.get("id") or "")
    parent_id = str(run.get("parent_run_id") or "")
    if parent_id:
        parent = db.get_run(parent_id)
        if parent and str(parent.get("status") or "") == "failed":
            return parent
    tid = str(run.get("task_id") or "")
    if not tid:
        return {}
    started = float(run.get("started_at") or 0)
    best: dict = {}
    for other in db.list_runs(tid, limit=200):
        oid = str(other.get("id") or "")
        if not oid or oid == rid or oid == parent_id:
            continue
        if str(other.get("status") or "") != "failed":
            continue
        ended = float(other.get("ended_at") or other.get("started_at") or 0)
        if started and ended and ended > started:
            continue
        if not best or ended > float(best.get("ended_at") or best.get("started_at") or 0):
            best = other
    return best


def repeated(run: dict) -> dict:
    """The previous attempt when this failure is the same death, else {}."""
    previous = previous_attempt(run)
    if not previous:
        return {}
    return previous if signature(previous) == signature(run) else {}


# ---- what one retry is likely to cost ----------------------------------


def estimate_cost(run: dict) -> float | None:
    """What one retry of this task has cost historically, in CNY.

    The task's own record is the only honest basis: a global average would let
    a cheap task inherit an expensive one's cost. The failed run is excluded
    from the mean (it stopped early); when it is the only priced attempt, its
    own bill is the best available estimate.

    Reading the bill is `ledger.amount_in`, not a local `json.loads`: the old
    copy treated an unrecorded currency as yuan and a USD run as absent, which
    is this call site's rule, so it is passed in explicitly rather than being
    a default in the ledger.
    """
    tid = str(run.get("task_id") or "")
    if not tid:
        return None
    costs: list[float] = []
    own: float | None = None
    for other in db.list_runs(tid, limit=200):
        cost = ledger.amount_in(
            other.get("usage_json"), COST_CURRENCY, unrecorded_is=COST_CURRENCY
        )
        if cost is None:
            continue
        if str(other.get("id") or "") == str(run.get("id") or ""):
            own = cost
        else:
            costs.append(cost)
    if costs:
        return sum(costs) / len(costs)
    return own


def _task_remaining(run: dict, task: dict | None) -> float | None:
    """The task's own budget left, in CNY, or None when it has no cost budget."""
    raw = task.get("budget_json") if task else None
    if not raw:
        return None
    try:
        shape = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(shape, dict):
        return None
    max_cost = shape.get("max_cost")
    if not isinstance(max_cost, (int, float)) or max_cost <= 0:
        return None
    currency = str(shape.get("currency") or COST_CURRENCY).upper()
    if currency != COST_CURRENCY:
        return None
    spent = ledger.spent(
        db.list_runs(str(run.get("task_id") or ""), limit=500),
        currency,
        unrecorded_is=currency,
    )
    return max(0.0, float(max_cost) - spent)


def headroom(run: dict, task: dict | None = None, now: float | None = None) -> dict:
    """The tightest remaining budget that applies, plus the daily figure."""
    daily = budget.check(now)
    task_left = _task_remaining(run, task if task is not None else _task(run))
    left: list[float] = []
    if daily.get("limit"):
        left.append(float(daily["remaining"]))
    if task_left is not None:
        left.append(task_left)
    return {
        "daily": daily,
        "task": task_left,
        "remaining": min(left) if left else None,
    }


# ---- the decision ------------------------------------------------------


def retry_plan(
    run: dict,
    now: float | None = None,
    limit: int | None = None,
    task: dict | None = None,
) -> dict:
    """Everything the retry decision is based on, as data.

    The board and the daemon call the same function: a row that says "will
    retry" and a daemon that does not retry are the same bug seen twice. The
    returned `gate` is "" when the retry may start now, "backoff" while it is
    waiting for its slot, and a named reason otherwise.
    """
    now = time.time() if now is None else now
    limit = RETRY_LIMIT if limit is None else int(limit)
    plan: dict = {
        "run_id": str(run.get("id") or ""),
        "task_id": str(run.get("task_id") or ""),
        "retry": False,
        "gate": "",
        "why": "",
        "mode": "",
        "attempt": retry_depth(run) + 1,
        "limit": limit,
        "progress": {},
        "signature": {},
        "previous": {},
        "estimate": None,
        "remaining": None,
        "cost_cap": COST_CAP,
        "redo": {},
        "due_at": None,
    }

    def stop(gate: str, why: str = "") -> dict:
        plan["gate"] = gate
        plan["why"] = why
        return plan

    if str(run.get("status") or "") != "failed":
        return stop("not-failed")
    if classify(run) in ("deterministic", "human"):
        why = why_not_retried(run) or "请求本身要先改掉，重发只会得到同一个错误。"
        return stop("deterministic", why)
    if not run.get("task_id") or not run.get("cwd"):
        return stop(
            "missing-context",
            "这条 run 缺任务或目录信息，系统不知道该把重试派到哪里。",
        )
    task = task if task is not None else _task(run)
    if not task:
        return stop("task-gone", "任务记录已经不在库里，旧 run 只能当历史看。")

    # Cheap bookkeeping first: a failure the chain has moved past must not pay
    # for signature or budget queries.
    if retry_depth(run) + 1 > limit:
        return stop(
            "retry-limit",
            f"自动重试已经到上限（{limit} 次）还没成功。天花板就是 {limit} 次，"
            "再自动试下去只是换个时间死同一处。",
        )
    if _has_retry_child(run):
        return stop(
            "already-retried",
            "这条失败已经派出过一条重试，系统不会为同一次失败开两张单。",
        )
    if _task_has_live_run(run):
        return stop("task-running", "这个任务已经有新的一条在跑，等它结束再看。")
    ended = _ended(run)
    if not ended:
        return stop("no-end-time", "这条 run 没有结束时间，无法判断它是不是刚失败。")
    if now - ended > MAX_AGE:
        return stop(
            "too-old",
            f"失败已经超过 {MAX_AGE / 3600:.0f} 小时，算历史记录，不在自动恢复窗口内。",
        )

    plan["progress"] = progress(run)
    plan["signature"] = sig = signature(run)
    previous = previous_attempt(run)
    if previous:
        plan["previous"] = {
            "run_id": str(previous.get("id") or ""),
            "ended_at": previous.get("ended_at"),
            "signature": signature(previous),
        }
        if signature(previous) == sig:
            when = time.strftime(
                "%m-%d %H:%M", time.localtime(float(previous.get("ended_at") or 0))
            )
            return stop(
                "same-signature",
                f"连续两次同样的错：{signature_text(sig)}。"
                f"{when} 那次已经这样死过一次，再跑一遍只会一样。",
            )

    plan["estimate"] = estimate = estimate_cost(run)
    money = headroom(run, task, now)
    plan["remaining"] = money["remaining"]
    if _over_budget(run, task):
        return stop(
            "over-budget",
            "这个任务已经到自己的预算上限（budget_json 的 max_runs 或 max_cost）。",
        )
    remaining = money["remaining"]
    if remaining is not None and (
        remaining <= 0 or (estimate is not None and remaining < estimate)
    ):
        have = f"¥{remaining:.2f}"
        need = f"，上一次这类重试约 ¥{estimate:.2f}" if estimate is not None else ""
        return stop(
            "budget",
            f"剩余额度不够再自动试一次：只剩 {have}{need}。要不要继续花钱由你定。",
        )
    if estimate is not None and estimate > COST_CAP:
        return stop(
            "cost-cap",
            f"这次重试预计要 ¥{estimate:.2f}，超过自动重试的成本上限 ¥{COST_CAP:.2f}；"
            "系统没有先花这笔钱。",
        )

    # The decision is "retry"; what remains is when and how.
    mode = "resume" if _session_id(run) else "fresh"
    plan["mode"] = mode
    if mode == "fresh" and plan["progress"]["moved"]:
        # The work already in the tree is what makes a from-scratch retry
        # expensive. Put that cost on the record instead of hiding it.
        plan["redo"] = {
            "tool_calls": plan["progress"]["tool_calls"],
            "files": plan["progress"]["files"],
            "note": (
                "没有可续的会话，只能从头跑：上次已经调用 "
                f"{plan['progress']['tool_calls']} 次工具、改了 "
                f"{plan['progress']['files']} 个文件，这些工作要重做。"
            ),
        }
    plan["due_at"] = _due_at(run)
    if now < plan["due_at"]:
        return stop("backoff")
    plan["retry"] = True
    return plan


def block_reason(
    run: dict,
    limit: int | None = None,
    now: float | None = None,
    task: dict | None = None,
) -> str:
    """Why this run must not be retried right now; empty string means retry.

    Kept as the short form of `retry_plan`: the board asks the same question,
    and a failure in backoff reports "backoff" so a caller can tell "waiting"
    from "refused".
    """
    return retry_plan(run, now=now, limit=limit, task=task)["gate"]


def should_retry(
    run: dict, limit: int | None = None, now: float | None = None
) -> bool:
    """Will the next sweep retry this run? Kept pure enough to unit-test."""
    return retry_plan(run, now=now, limit=limit)["retry"]


def next_retry_at(
    run: dict, limit: int | None = None, now: float | None = None
) -> float | None:
    """When the next attempt may start, or None when no retry is coming.

    None is the honest answer for a deterministic failure, a repeated
    signature, an exhausted chain, or a budget that cannot cover it. During
    the backoff window this is the timestamp itself. An orphan has nothing to
    wait for, so it is its own end.
    """
    plan = retry_plan(run, now=now, limit=limit)
    if plan["gate"] not in ("", "backoff"):
        return None
    return _due_at(run)


# ---- chain bookkeeping -------------------------------------------------


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
    """How many automatic retries this run's chain has already used."""
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
    """The whole retry chain this run belongs to, oldest first."""
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


# ---- reachability ------------------------------------------------------


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

    Recognised keys: `{"max_runs": 20}`, `{"max_cost": 5.0, "currency": "CNY"}`.
    An absent or unreadable budget means "no limit", not "block everything".

    A budget with a currency counts only that currency's rows. A budget with no
    currency means yuan, the same reading `_task_remaining` uses; the old local
    parser summed every currency together in that case, which is the one thing
    the ledger exists to prevent (and why this now asks it).
    """
    task = task if task is not None else _task(run)
    raw = task.get("budget_json") if task else None
    if not raw:
        return False
    try:
        shape = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(shape, dict):
        return False
    tid = str(run.get("task_id") or "")
    if not tid:
        return True
    max_runs = shape.get("max_runs")
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
    max_cost = shape.get("max_cost")
    if isinstance(max_cost, (int, float)) and max_cost > 0:
        currency = str(shape.get("currency") or "").upper()
        rows = db.list_runs(tid, limit=500)
        if currency:
            spent = ledger.spent(rows, currency)
        else:
            spent = ledger.spent(rows, COST_CURRENCY, unrecorded_is=COST_CURRENCY)
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


# ---- starting one retry ------------------------------------------------


def _session_id(run: dict) -> str:
    try:
        ref = json.loads(run.get("session_ref_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    return str((ref or {}).get("session_id") or "")


def _note(run: dict, plan: dict, attempt: int, limit: int) -> str:
    """First line is the label; the rest is the only instruction a retry gets.

    The previous failure's last 300 characters go in with it: a retry that
    only says "it failed" asks the worker to rediscover the same wall. When
    starting over costs work, that cost is stated instead of being hidden.
    """
    previous = "原始执行" if attempt <= 1 else f"第 {attempt - 1} 次重试"
    tail = str(run.get("error") or "").strip()
    if len(tail) > 300:
        tail = "…" + tail[-300:]
    detail = f"上一次（{previous}）失败：{tail}\n" if tail else ""
    redo = f"{plan['redo']['note']}\n" if plan.get("redo") else ""
    resume = "接着上次的会话继续，不要从零开始。\n" if plan.get("mode") == "resume" else ""
    return (
        f"{NOTE_PREFIX} {attempt}/{limit}\n"
        f"{detail}{redo}{resume}"
        "请继续完成原任务：先看工作目录里已有的改动，不要重复已经做过的步骤。"
        "如果上次是测试红，先修失败的那条测试；看不出原因就先复现一次，再改。"
    )


def _start_retry(run: dict, plan: dict, attempt: int, limit: int) -> dict:
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
    session = _session_id(run) if plan.get("mode") == "resume" else ""
    return runner.RUNNER.start(
        task_id=str(run["task_id"]),
        route_name=route.name,
        cwd=str(run.get("cwd") or ""),
        parent_run_id=str(run["id"]),
        note=_note(run, plan, attempt, limit),
        effort=str(run.get("effort") or ""),
        model=model,
        session_id=session,
        session_mode="resume" if session else "fresh",
        profile=str(run.get("profile") or ""),
        provider=pin,
    )


def auto_retry(
    limit: int | None = None, dry_run: bool = False, now: float | None = None
) -> dict:
    """Scan failures and start a child run for each one that is worth it.

    Returns what was done, not just a count: which run was retried, how
    (resume or from scratch, and what that redoes), and which one was left
    alone with the gate that stopped it. A retry that could not be started is
    an `errors` entry rather than a silent success.

    `dry_run=True` makes the same decisions and writes nothing.
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
        plan = retry_plan(run, now=now, limit=limit)
        if not plan["retry"]:
            out["skipped"].append(
                {
                    "run_id": str(run.get("id") or ""),
                    "task_id": tid,
                    "task_title": title,
                    "class": classify(run),
                    "cause": cause(run),
                    "reason": plan["gate"],
                    "why": plan["why"],
                    "estimate": plan["estimate"],
                }
            )
            continue
        attempt = plan["attempt"]
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
                    "mode": plan["mode"],
                    "estimate": plan["estimate"],
                    "redo": plan["redo"],
                }
            )
            continue
        try:
            child = _start_retry(run, plan, attempt, limit)
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
                    "mode": plan["mode"],
                    "estimate": plan["estimate"],
                    "redo": plan["redo"],
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
                "mode": plan["mode"],
                "estimate": plan["estimate"],
                "redo": plan["redo"],
            }
        )
    return out


def recovering(now: float | None = None) -> list[dict]:
    """Rows for the board's "auto retrying" block.

    Two shapes are the same fact: a failed run waiting out its backoff, and a
    live retry child. `run_id` names the run the row is about; `attempt` is
    which retry that is (1 of `limit`); `next_retry_at` is None while the
    retry is already running.
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
            "problem": problem_text(run),
        }
        if status in ("queued", "running") and _is_retry(run):
            out.append(
                {
                    **base,
                    "run_id": str(run.get("id") or ""),
                    "attempt": max(1, retry_depth(run)),
                    "next_retry_at": None,
                    "mode": "resume" if _session_id(run) else "fresh",
                    "estimate": None,
                }
            )
            continue
        if status != "failed":
            continue
        plan = retry_plan(run, now=now, limit=RETRY_LIMIT)
        if plan["gate"] not in ("", "backoff"):
            continue
        out.append(
            {
                **base,
                "run_id": str(run.get("id") or ""),
                "attempt": plan["attempt"],
                "next_retry_at": _due_at(run),
                "mode": plan["mode"],
                "estimate": plan["estimate"],
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
    """Run one recovery pass now, then keep sweeping in the background."""
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

    threading.Thread(target=loop, daemon=True, name="sodus-recover").start()
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
