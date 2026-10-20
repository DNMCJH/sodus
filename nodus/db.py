"""SQLite state. Every connection is short-lived; WAL keeps readers unblocked."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
  id TEXT PRIMARY KEY,
  workspace_id TEXT,
  workspace TEXT,
  title TEXT,
  status TEXT DEFAULT 'open',
  created_at REAL
);
CREATE TABLE IF NOT EXISTS task (
  id TEXT PRIMARY KEY,
  workspace_id TEXT,
  session_id TEXT,
  title TEXT,
  goal TEXT,
  acceptance TEXT,
  status TEXT DEFAULT 'open',
  budget_json TEXT,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS run (
  id TEXT PRIMARY KEY,
  workspace_id TEXT,
  task_id TEXT,
  agent TEXT,
  status TEXT DEFAULT 'queued',
  parent_run_id TEXT,
  cwd TEXT,
  note TEXT,
  effort TEXT,
  session_ref_json TEXT,
  usage_json TEXT,
  started_at REAL,
  ended_at REAL,
  error TEXT
);
CREATE TABLE IF NOT EXISTS event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version INTEGER DEFAULT 1,
  run_id TEXT,
  seq INTEGER,
  type TEXT,
  payload_json TEXT,
  ts REAL
);
CREATE INDEX IF NOT EXISTS idx_event_run ON event(run_id, seq);
CREATE TABLE IF NOT EXISTS artifact (
  id TEXT PRIMARY KEY,
  workspace_id TEXT,
  run_id TEXT,
  task_id TEXT,
  kind TEXT,
  path TEXT,
  rel_path TEXT,
  hash TEXT,
  summary TEXT,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS handoff (
  id TEXT PRIMARY KEY,
  workspace_id TEXT,
  task_id TEXT,
  from_run_id TEXT,
  path TEXT,
  hash TEXT,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.HOME / "nodus.sqlite3", timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    for table in ("session", "task", "run", "artifact", "handoff"):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "workspace_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN workspace_id TEXT")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(event)")}
    if "version" not in cols:
        conn.execute("ALTER TABLE event ADD COLUMN version INTEGER DEFAULT 1")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(run)")}
    if "note" not in cols:
        conn.execute("ALTER TABLE run ADD COLUMN note TEXT")
    if "effort" not in cols:
        conn.execute("ALTER TABLE run ADD COLUMN effort TEXT")
    if "model" not in cols:
        conn.execute("ALTER TABLE run ADD COLUMN model TEXT")
    for column in ("route", "provider", "profile"):
        if column not in cols:
            conn.execute(f"ALTER TABLE run ADD COLUMN {column} TEXT")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(artifact)")}
    if "rel_path" not in cols:
        conn.execute("ALTER TABLE artifact ADD COLUMN rel_path TEXT")
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')"
    )
    # Legacy rows predate workspace scoping; adopt them into the current one.
    for table in ("session", "task", "run", "artifact", "handoff"):
        conn.execute(
            f"UPDATE {table} SET workspace_id=? WHERE workspace_id IS NULL OR workspace_id=''",
            (config.workspace_id(),),
        )
    conn.commit()
    return conn


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


def create_session(workspace: str, title: str = "") -> dict:
    sid = new_id("ses")
    with connect() as c:
        c.execute(
            "INSERT INTO session(id, workspace_id, workspace, title, created_at) VALUES(?,?,?,?,?)",
            (sid, config.workspace_id(), workspace, title, now()),
        )
    return get_session(sid)


def get_session(sid: str) -> dict:
    with connect() as c:
        row = c.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()
    return dict(row) if row else {}


def create_task(
    goal: str, acceptance: str = "", session_id: str | None = None, title: str = ""
) -> dict:
    tid = new_id("task")
    if not session_id:
        session_id = create_session(str(Path.cwd()))["id"]
    with connect() as c:
        c.execute(
            "INSERT INTO task(id, workspace_id, session_id, title, goal, acceptance, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (tid, config.workspace_id(), session_id, title or goal[:60], goal, acceptance, now()),
        )
    return get_task(tid)


def get_task(tid: str) -> dict:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM task WHERE id=? AND workspace_id=?",
            (tid, config.workspace_id()),
        ).fetchone()
    return dict(row) if row else {}


def update_task(tid: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE task SET {cols} WHERE id=?", (*fields.values(), tid))


def list_tasks(limit: int = 50) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM task WHERE workspace_id=? ORDER BY created_at DESC LIMIT ?",
            (config.workspace_id(), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def create_run(
    task_id: str,
    agent: str,
    cwd: str,
    parent_run_id: str | None = None,
    note: str = "",
    effort: str = "",
    model: str = "",
    route: str = "",
    provider: str = "",
    profile: str = "",
) -> dict:
    rid = new_id("run")
    with connect() as c:
        c.execute(
            "INSERT INTO run(id, workspace_id, task_id, agent, cwd, parent_run_id, note,"
            " effort, model, route, provider, profile, started_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid,
                config.workspace_id(),
                task_id,
                agent,
                cwd,
                parent_run_id,
                note,
                effort,
                model,
                route,
                provider,
                profile,
                now(),
            ),
        )
    return get_run(rid)


def get_run(rid: str) -> dict:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM run WHERE id=? AND workspace_id=?",
            (rid, config.workspace_id()),
        ).fetchone()
    return dict(row) if row else {}


def list_runs(task_id: str | None = None, limit: int = 50) -> list[dict]:
    with connect() as c:
        if task_id:
            rows = c.execute(
                "SELECT * FROM run WHERE task_id=? AND workspace_id=?"
                " ORDER BY started_at DESC LIMIT ?",
                (task_id, config.workspace_id(), limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM run WHERE workspace_id=? ORDER BY started_at DESC LIMIT ?",
                (config.workspace_id(), limit),
            ).fetchall()
    return [dict(r) for r in rows]


def verify_states(run_ids: list[str]) -> dict[str, str]:
    """Per-run verification mark.

    'ok'   a test run passed for this run
    'red'  tests ran and failed
    'none' nothing was ever executed — the status is only an exit code

    Kept out of the run row on purpose: an exit code is a fact, and whether the
    work was checked is a different fact that must not be flattened into it.
    """
    if not run_ids:
        return {}
    marks = ",".join("?" * len(run_ids))
    with connect() as c:
        rows = c.execute(
            f"SELECT run_id, type FROM event WHERE run_id IN ({marks})"
            " AND type IN ('verify.green', 'verify.red')",
            tuple(run_ids),
        ).fetchall()
    out: dict[str, str] = {}
    for row in rows:
        if row["type"] == "verify.green":
            out[row["run_id"]] = "ok"
        elif out.get(row["run_id"]) != "ok":
            out[row["run_id"]] = "red"
    return out


def update_run(rid: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE run SET {cols} WHERE id=?", (*fields.values(), rid))


def live_runs() -> list[dict]:
    """Every run still marked queued/running in this workspace.

    A fresh daemon owns none of them, so they are all orphans by definition.
    """
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM run WHERE status IN ('queued','running') AND workspace_id=?"
            " ORDER BY started_at",
            (config.workspace_id(),),
        ).fetchall()
    return [dict(r) for r in rows]


def stale_runs(seconds: int) -> list[dict]:
    """Runs still marked running/queued with no activity for `seconds`.

    Used when the process that owned the run died (crash, kill, job runner),
    leaving a row that will otherwise say "running" forever.
    """
    cutoff = now() - seconds
    with connect() as c:
        rows = c.execute(
            "SELECT r.*, (SELECT MAX(ts) FROM event e WHERE e.run_id = r.id) AS last_event"
            " FROM run r WHERE r.status IN ('queued','running') AND r.workspace_id=?",
            (config.workspace_id(),),
        ).fetchall()
    out = []
    for row in rows:
        last = row["last_event"] or row["started_at"] or 0
        if last >= cutoff:
            continue
        item = dict(row)
        item.pop("last_event", None)
        item["last_ts"] = last
        out.append(item)
    return out


def next_seq(rid: str) -> int:
    with connect() as c:
        row = c.execute("SELECT MAX(seq) AS m FROM event WHERE run_id=?", (rid,)).fetchone()
    return (row["m"] or 0) + 1


def append_event(rid: str, seq: int, etype: str, payload: dict, version: int = 1) -> dict:
    ev = {"run_id": rid, "seq": seq, "type": etype, "payload": payload, "ts": now()}
    with connect() as c:
        c.execute(
            "INSERT INTO event(version, run_id, seq, type, payload_json, ts) VALUES(?,?,?,?,?,?)",
            (version, rid, seq, etype, json.dumps(payload, ensure_ascii=False), ev["ts"]),
        )
    return ev


def events_after(rid: str, after: int = 0, limit: int = 500) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM event WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
            (rid, after, limit),
        ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "run_id": r["run_id"],
                "seq": r["seq"],
                "type": r["type"],
                "payload": json.loads(r["payload_json"]),
                "ts": r["ts"],
            }
        )
    return out


def events_filtered(run_id: str, prefixes: tuple[str, ...] = ()) -> list[dict]:
    """All matching events for a run, regardless of how noisy the stream was.

    Report-style consumers care about a handful of event types, and those are
    appended last; paging with a limit would silently drop them on long runs.
    """
    sql = "SELECT * FROM event WHERE run_id=?"
    params: list[Any] = [run_id]
    if prefixes:
        sql += " AND (" + " OR ".join("type LIKE ?" for _ in prefixes) + ")"
        params.extend(f"{p}%" for p in prefixes)
    sql += " ORDER BY seq"
    with connect() as c:
        rows = c.execute(sql, params).fetchall()
    return [
        {
            "run_id": r["run_id"],
            "seq": r["seq"],
            "type": r["type"],
            "payload": json.loads(r["payload_json"]),
            "ts": r["ts"],
        }
        for r in rows
    ]


def add_artifact(
    run_id: str,
    task_id: str,
    kind: str,
    path: str,
    digest: str,
    summary: str = "",
    rel_path: str = "",
) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO artifact(id, workspace_id, run_id, task_id, kind, path, rel_path, hash, summary, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (new_id("art"), config.workspace_id(), run_id, task_id, kind, path, rel_path, digest, summary, now()),
        )


def artifacts_for(run_id: str) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM artifact WHERE run_id=? AND workspace_id=?",
            (run_id, config.workspace_id()),
        ).fetchall()
    return [dict(r) for r in rows]


def artifacts_for_task(task_id: str) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM artifact WHERE task_id=? AND workspace_id=? ORDER BY created_at",
            (task_id, config.workspace_id()),
        ).fetchall()
    return [dict(r) for r in rows]


def add_handoff(task_id: str, from_run_id: str, path: str, digest: str) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO handoff(id, workspace_id, task_id, from_run_id, path, hash, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (new_id("hof"), config.workspace_id(), task_id, from_run_id, path, digest, now()),
        )


def latest_handoff(task_id: str) -> dict:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM handoff WHERE task_id=? AND workspace_id=?"
            " ORDER BY created_at DESC LIMIT 1",
            (task_id, config.workspace_id()),
        ).fetchone()
    return dict(row) if row else {}
