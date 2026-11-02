"""The writer for `stalled`: a run that has not failed but is not moving.

`recover` answers one question: should this failure be retried? The watchdog
answers a different one: a run whose status is still `running`, but which has
recorded no substantive event for N minutes. Until this module, `lifecycle`'s
`running -> stalled` edge had no writer at all: the runner only turned silence
into `failed` after 900s of no output, and `needs_attention` could name a stall
from facts nobody supplied.

The judgement is lifecycle's, not a second copy: the status must be `running`
and `lifecycle.needs_attention` must answer `stalled` for the newest
substantive event. `NODUS_STALL_SECONDS` (default 600 = 10 minutes) is
deliberately earlier than the runner's 900s hard stop: at ten minutes the board
should say "not moving", at fifteen the runner kills a silent child.

What counts as progress is deliberately narrow -- a `tool_call`, an artifact,
or a status change. A `message` event is not enough: an agent can print for ten
minutes without moving, and this module is about progress, not output. A run
that is thinking through one long turn is the false-positive case; raise
`NODUS_STALL_SECONDS` for it.

A sweep writes, in this order:

1. release the write lock (before the status write, the order the runner uses:
   a run that is not moving must free its directory for the next one);
2. a conditional ``UPDATE ... WHERE status='running'`` after
   ``lifecycle.can_transition`` -- an illegal or late transition is logged and
   left alone;
3. one ``run.stalled`` event naming the last substantive event and the idle
   time;
4. stop a worker this daemon owns when it has already called a tool, so the
   stall does not keep spending.

The `tool_call` guard on step 4 is not cosmetic. A silent kill looks like an
upstream failure to `runner.should_fall_through` (no tool call, non-zero exit),
so killing a worker that never acted would start the route's next endpoint --
more spending, which is the opposite of what this module is for. A worker with
no tool call yet is left to the runner's own 900s idle timeout.

A killed worker's runner then records `failed` (a legal `stalled -> failed`
edge), so that stall survives in the event stream rather than in the final
status; a run the daemon does not own, or one with no tool call, keeps the
`stalled` status on the board. The event says which of the three happened.

It never retries and never touches the budget: `recover.retry_plan` refuses a
`stalled` row (`not-failed`), so the decision stays with a person or with the
run's eventual failure. Only a row that is still ``running`` is transitioned;
a row already ``stalled`` -- including one a previous daemon wrote -- is
skipped, which is what makes a restart idempotent.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback

from . import db, lifecycle, runner

#: Ten minutes. The runner's hard stop is fifteen (`runner.IDLE_TIMEOUT`); this
#: is the point where the board should say "not moving", not where the process
#: dies.
DEFAULT_STALL_SECONDS = 600.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


#: How often the daemon sweeps. One query for the live rows, one per candidate.
INTERVAL = max(1.0, _env_float("NODUS_WATCHDOG_INTERVAL", 60.0))

#: Event types that mean the worker did something. `run.stalled` is included so
#: a run whose status was flipped back to `running` by hand waits a full window
#: before being marked again instead of being re-marked on the next sweep.
SUBSTANTIVE_EVENTS: tuple[str, ...] = (
    "tool_call",
    "tool_result",
    "run.started",
    "run.attempt",
    "run.fallback",
    "run.completed",
    "run.failed",
    "run.stalled",
    "verify.started",
    "verify.green",
    "verify.red",
)

_started = False
_start_lock = threading.Lock()


def enabled() -> bool:
    """Is the watchdog on? `NODUS_WATCHDOG=0` turns the daemon loop off."""
    return os.environ.get("NODUS_WATCHDOG", "1") != "0"


def stall_seconds() -> float:
    """The idle window, in seconds. Read per sweep so tests can change it."""
    raw = os.environ.get("NODUS_STALL_SECONDS")
    if not raw:
        return DEFAULT_STALL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_STALL_SECONDS
    return value if value > 0 else DEFAULT_STALL_SECONDS


def _span(seconds: float) -> str:
    """A duration a person reads: seconds under two minutes, minutes above."""
    seconds = max(0.0, float(seconds))
    if seconds < 120:
        return f"{seconds:.0f} 秒"
    return f"{seconds / 60:.1f} 分钟"


def _note(log, message: str) -> None:
    if log:
        log(message)


def _event_label(event: dict) -> str:
    etype = str(event.get("type") or "?")
    if etype != "tool_call":
        return etype
    payload = event.get("payload") or {}
    name = str(payload.get("name") or "").strip()
    return f"tool_call（{name}）" if name else "tool_call"


def _snapshot(run_id: str) -> dict:
    """Newest substantive event, newest artifact and tool-call count, one query each."""
    marks = ",".join("?" * len(SUBSTANTIVE_EVENTS))
    with db.connect() as conn:
        event = conn.execute(
            f"SELECT type, payload_json, ts FROM event WHERE run_id=?"
            f" AND type IN ({marks}) ORDER BY ts DESC LIMIT 1",
            (run_id, *SUBSTANTIVE_EVENTS),
        ).fetchone()
        artifact = conn.execute(
            "SELECT MAX(created_at) AS ts FROM artifact WHERE run_id=?", (run_id,)
        ).fetchone()
        calls = conn.execute(
            "SELECT COUNT(*) AS n FROM event WHERE run_id=? AND type='tool_call'",
            (run_id,),
        ).fetchone()
    payload: dict = {}
    if event is not None:
        try:
            payload = json.loads(event["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
    return {
        "event": (
            {
                "type": str(event["type"]),
                "ts": float(event["ts"]),
                "payload": payload,
            }
            if event is not None
            else None
        ),
        "artifact_at": (
            float(artifact["ts"])
            if artifact is not None and artifact["ts"] is not None
            else None
        ),
        "tool_calls": int(calls["n"] or 0) if calls is not None else 0,
    }


def _last_progress(run: dict, snapshot: dict) -> tuple[float | None, str]:
    """The newest moment that counts as progress, and what it was.

    `started_at` is the floor: a running run with no events and no artifacts is
    measured from when it started, not from the epoch.
    """
    started = float(run.get("started_at") or 0)
    last_at: float | None = started if started else None
    label = "run.started" if started else ""
    event = snapshot.get("event")
    if event is not None and (last_at is None or float(event["ts"]) > last_at):
        last_at = float(event["ts"])
        label = _event_label(event)
    artifact_at = snapshot.get("artifact_at")
    if artifact_at is not None and (last_at is None or float(artifact_at) > last_at):
        last_at = float(artifact_at)
        label = "artifact"
    return last_at, label


def judge(run: dict, now: float | None = None) -> dict | None:
    """The stall decision for one run, or None when there is nothing to write.

    Reads the run's events and artifacts; writes nothing. The caller can use the
    returned facts for tests or for a dry run.
    """
    now = time.time() if now is None else now
    status = str(run.get("status") or "")
    if status != "running":
        return None
    run_id = str(run.get("id") or "")
    if not run_id:
        return None
    snapshot = _snapshot(run_id)
    last_at, label = _last_progress(run, snapshot)
    if last_at is None:
        return None
    window = stall_seconds()
    token = lifecycle.needs_attention(
        {"status": status},
        {"now": now, "last_progress_at": last_at, "stall_after": window},
    )
    if token != "stalled":
        return None
    return {
        "run": run,
        "run_id": run_id,
        "task_id": str(run.get("task_id") or ""),
        "now": now,
        "last_at": last_at,
        "last_event": label,
        "idle_seconds": max(0.0, now - last_at),
        "window_seconds": window,
        "tool_calls": snapshot["tool_calls"],
    }


def _worker_owned(run_id: str) -> bool:
    """Is the worker process the daemon's own child?"""
    try:
        with runner.RUNNER.lock:
            return run_id in runner.RUNNER.procs
    except AttributeError:  # a caller replaced RUNNER with a stub
        return False


def mark_stalled(finding: dict, log=None) -> dict | None:
    """Write the state, the lock release and the event for one judged run.

    The transition is checked against a fresh row, not the one the caller read:
    a run may have completed between the sweep and this write, and overwriting
    `completed` with `stalled` is exactly the bug `can_transition` is here to
    prevent.
    """
    run_id = str(finding.get("run_id") or "")
    fresh = db.get_run(run_id)
    if not fresh:
        return None
    status = str(fresh.get("status") or "")
    if not lifecycle.can_transition(lifecycle.RUN, status, "stalled"):
        _note(
            log,
            f"watchdog: run {run_id} is {status or 'unknown'!r}, not running; left alone",
        )
        return None
    started = float(fresh.get("started_at") or 0)
    if started > float(finding.get("last_at") or 0):
        # A resume between the sweep and the write moved the clock forward; the
        # finding is stale. Not a transition to refuse, just nothing to do yet.
        _note(log, f"watchdog: run {run_id} restarted; stale stall ignored")
        return None

    owned = _worker_owned(run_id)
    if owned and finding.get("tool_calls"):
        worker = "kill"
    elif owned:
        worker = "keep-no-tool-call"
    else:
        worker = "not-owned"

    # Free the directory before the status write, as the runner does. If the
    # conditional UPDATE then loses the race, the lock is still free for the
    # process that won it.
    db.release_write(run_id)
    with db.connect() as conn:
        changed = conn.execute(
            "UPDATE run SET status='stalled' WHERE id=? AND status='running'",
            (run_id,),
        ).rowcount
    if changed != 1:
        _note(log, f"watchdog: run {run_id} left running before the write; not marked")
        return None

    idle = float(finding.get("idle_seconds") or 0.0)
    message = (
        f"无实质进展：最后事件是 {finding.get('last_event') or '?'}，"
        f"距今 {_span(idle)}"
    )
    payload = {
        "reason": "no-substantive-progress",
        "message": message,
        "last_event": str(finding.get("last_event") or ""),
        "last_at": finding.get("last_at"),
        "idle_seconds": round(idle, 1),
        "window_seconds": finding.get("window_seconds"),
        "worker": worker,
    }
    seq = db.next_seq(run_id)
    db.append_event(run_id, seq, "run.stalled", payload)

    killed = False
    if worker == "kill":
        try:
            killed = runner.RUNNER.stop(run_id)
        except Exception:  # noqa: BLE001 - the stall is already recorded
            _note(log, f"watchdog: could not stop the worker for {run_id}:\n"
                       + traceback.format_exc())
    result = {
        "run_id": run_id,
        "task_id": str(fresh.get("task_id") or ""),
        "idle_seconds": round(idle, 1),
        "last_event": str(finding.get("last_event") or ""),
        "window_seconds": finding.get("window_seconds"),
        "worker": worker,
        "worker_stopped": killed,
        "event_seq": seq,
        "message": message,
    }
    _note(
        log,
        f"watchdog: {run_id} stalled ({message}); lock released; worker={worker}"
        + (" (killed)" if killed else ""),
    )
    return result


def sweep(now: float | None = None, log=None) -> dict:
    """Judge every running run once and write the ones that stopped moving.

    One bad row is an `errors` entry, never a reason to stop the sweep.
    """
    now = time.time() if now is None else now
    running = [
        run for run in db.live_runs() if str(run.get("status") or "") == "running"
    ]
    result: dict = {"running": len(running), "stalled": [], "skipped": [], "errors": []}
    for run in running:
        run_id = str(run.get("id") or "")
        try:
            finding = judge(run, now)
            if finding is None:
                continue
            written = mark_stalled(finding, log=log)
            if written is None:
                result["skipped"].append(run_id)
            else:
                result["stalled"].append(written)
        except Exception as exc:  # noqa: BLE001 - one bad run must not stop the sweep
            message = f"{type(exc).__name__}: {exc}"
            result["errors"].append({"run_id": run_id, "error": message})
            _note(log, f"watchdog: sweep failed for {run_id}: {message}")
    return result


def summary(result: dict) -> str:
    """One line for `daemon.log`, no JSON."""
    stalled = result.get("stalled") or []
    errors = result.get("errors") or []
    parts = [f"{result.get('running', 0)} running"]
    if stalled:
        parts.append(
            "stalled: "
            + ", ".join(
                f"{item['run_id']} ({_span(item['idle_seconds'])})"
                for item in stalled
            )
        )
    else:
        parts.append("none stalled")
    if errors:
        parts.append(f"{len(errors)} error(s)")
    return "; ".join(parts)


def start(interval: float | None = None, log=None) -> bool:
    """Run one sweep now, then keep sweeping in the background."""
    global _started
    with _start_lock:
        if _started:
            return False
        _started = True
    interval = INTERVAL if interval is None else max(1.0, float(interval))
    try:
        first = sweep(log=log)
        if log and (first["stalled"] or first["errors"]):
            log("watchdog: " + summary(first))
    except Exception:  # noqa: BLE001 - a bad sweep must not stop the daemon
        if log:
            log("watchdog first sweep failed:\n" + traceback.format_exc())

    def loop() -> None:
        while True:
            time.sleep(interval)
            try:
                current = sweep(log=log)
                if log and (current["stalled"] or current["errors"]):
                    log("watchdog: " + summary(current))
            except Exception:  # noqa: BLE001
                if log:
                    log("watchdog sweep failed:\n" + traceback.format_exc())

    threading.Thread(target=loop, daemon=True, name="nodus-watchdog").start()
    return True
