"""Lightweight read-only status; never assembles board evidence or probes git."""
from __future__ import annotations

import copy
import threading
import time
from . import board, config, db, ledger, memory, observe, recover

TTL = 1.5
_LOCK = threading.Lock()
_CACHE = None


def _slow(conn, workspace, start):
    rows = db.runs_in_window(start, conn=conn) + observe.observed_runs(start)
    cny = 0.0
    unpriced = 0
    for row in rows:
        parsed = ledger.parse(row.get("usage_json"))
        if parsed.cost.amount is None:
            unpriced += 1
        elif ledger.currency_of(parsed) == "CNY":
            cny += float(parsed.cost.amount)
    today = {"CNY": round(cny, 6), "runs": len(rows), "unpriced": unpriced}
    proposals = conn.execute("SELECT COUNT(*) FROM proposal WHERE workspace_id=? AND status='open'", (workspace,)).fetchone()[0]
    candidates = conn.execute("SELECT COUNT(*) FROM memory_candidate WHERE workspace IN (?,?) AND status IN ('proposed','retire_proposed')", (workspace, str(memory.saved_project()))).fetchone()[0]
    return today, proposals, candidates


def snapshot(now=None):
    global _CACHE
    now = time.time() if now is None else now
    workspace = config.workspace_id()
    start, _ = board._day_start(now)
    key = (str(config.HOME.resolve()), workspace, start, str(memory.saved_project()))
    conn = db.connect()
    try:
        with _LOCK:
            tick = time.monotonic()
            if _CACHE is None or _CACHE[0] != key or tick - _CACHE[1] >= TTL:
                _CACHE = (key, tick, _slow(conn, workspace, start))
            today, proposals, candidates = copy.deepcopy(_CACHE[2])
        live = [dict(r) for r in conn.execute(
            "SELECT id,task_id,agent,route,started_at,cwd FROM run WHERE workspace_id=? AND status IN ('queued','running','stalled') ORDER BY started_at DESC LIMIT 20", (workspace,))]
        statuses = dict(conn.execute("SELECT status,COUNT(*) FROM run WHERE workspace_id=? AND status IN ('queued','running','stalled') GROUP BY status", (workspace,)))
        ask_statuses = dict(conn.execute("SELECT status,COUNT(*) FROM ask WHERE workspace_id=? GROUP BY status", (workspace,)))
    finally:
        conn.close()
    asks = {"open": ask_statuses.get("queued", 0), "running": ask_statuses.get("running", 0),
            "done": ask_statuses.get("answered", 0), "failed": ask_statuses.get("failed", 0)}
    runs = db.list_runs(limit=50, kind="work")
    states = db.verify_states([r["id"] for r in runs])
    spend = sum(1 for r in runs if r["status"] == "failed" and recover.classify(r) != "deterministic"
                and recover.enabled() and recover.retry_plan(r, now=now)["gate"] in board.SPEND_GATES)
    unverifiable = int(any(r["status"] == "completed" and states.get(r["id"], "none") == "none" for r in runs))
    return {"version": "0.8.0", "workspace_id": workspace, "now": now,
            "running": live, "counts": {"running": statuses.get("running", 0), "queued": statuses.get("queued", 0),
            "needs_you": min(proposals, 50) + spend, "unverifiable": unverifiable, "proposals": proposals,
            "asks_open": asks["open"], "memory_candidates": candidates},
            "today": today, "asks": asks, "memory": {"candidates_pending": candidates}}
