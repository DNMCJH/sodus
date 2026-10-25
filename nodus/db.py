"""SQLite state. Every connection is short-lived; WAL keeps readers unblocked."""

from __future__ import annotations

import json
import os
import socket
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
  parent_session_id TEXT,
  origin_run_id TEXT,
  relation TEXT DEFAULT 'fresh',
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
  kind TEXT DEFAULT 'work',
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
-- One row per run that owns a working directory. A partial unique index makes
-- "two runs writing the same tree" impossible rather than unlikely: the second
-- INSERT fails instead of the two workers silently overwriting each other.
CREATE TABLE IF NOT EXISTS write_lock (
  id TEXT PRIMARY KEY,
  workspace_id TEXT,
  cwd TEXT NOT NULL,
  run_id TEXT,
  acquired_at REAL,
  released_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_write_lock_active
  ON write_lock(cwd) WHERE released_at IS NULL;
-- A proposal is work that exists on a branch, not in the main tree. The row
-- is what `nodus proposals` lists and what `nodus merge` consumes.
CREATE TABLE IF NOT EXISTS proposal (
  id TEXT PRIMARY KEY,
  workspace_id TEXT,
  run_id TEXT,
  task_id TEXT,
  repo TEXT,
  branch TEXT,
  worktree TEXT,
  base TEXT,
  patch_path TEXT,
  files_json TEXT,
  stat TEXT,
  status TEXT DEFAULT 'open',
  created_at REAL,
  closed_at REAL
);
-- A pipeline is a sequence of runs plus verification, owned by the daemon
-- rather than by whichever terminal happened to start it.
CREATE TABLE IF NOT EXISTS pipeline (
  id TEXT PRIMARY KEY,
  workspace_id TEXT,
  kind TEXT,
  label TEXT,
  status TEXT DEFAULT 'queued',
  params_json TEXT,
  result_json TEXT,
  error TEXT,
  created_at REAL,
  started_at REAL,
  ended_at REAL
);
-- Memory is not a table. Confirmed facts are appended to the project's
-- AGENTS.md, where a human can read them, diff them and revert them
-- (`memory.py`); `connect()` drops the old rows this table used to hold.
"""


class Busy(RuntimeError):
    """A working directory already has a writer."""

    def __init__(self, cwd: str, holder: dict) -> None:
        self.cwd = cwd
        self.holder = holder
        super().__init__(describe_busy(cwd, holder))


def describe_busy(cwd: str, holder: dict) -> str:
    if not holder:
        return f"{cwd} is already being written by another run"
    age = time.time() - float(holder.get("acquired_at") or 0)
    who = holder.get("run_id") or "?"
    return (
        f"{cwd} is already being written by {who}"
        f" (agent={holder.get('agent') or '?'}, {int(age)}s);"
        " wait for it, or use a different --cwd"
    )


#: Databases this process has already opened and migrated. Schema repair is
#: idempotent but expensive (executescript + a PRAGMA table_info per table);
#: a board render opens dozens of connections, so paying it each time was the
#: single biggest cost of the page (board ~10s -> measured 2026-10-25). One
#: entry per database path keeps tests that build a fresh database safe.
_SCHEMA_READY: set[str] = set()


def connect() -> sqlite3.Connection:
    path = config.HOME / "nodus.sqlite3"
    key = str(path)
    ready = key in _SCHEMA_READY
    if not ready:
        config.ensure_dirs()
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if ready:
        return conn
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
    # Who started this run. A daemon that starts later must not adopt (or kill)
    # a run a live MCP process owns — that is the whole point of recording it.
    if "owner_pid" not in cols:
        conn.execute("ALTER TABLE run ADD COLUMN owner_pid INTEGER")
    if "owner_host" not in cols:
        conn.execute("ALTER TABLE run ADD COLUMN owner_host TEXT")
    # A pet utterance is a token bill, not work. "kind" separates the two so the
    # board and the route history stop reading chat as attempts. Old rows carry
    # the note llm._record writes ("桌宠...") or the ledger source it stamps
    # ("llm/chat"); those are the only signals that already exist.
    if "kind" not in cols:
        conn.execute("ALTER TABLE run ADD COLUMN kind TEXT DEFAULT 'work'")
        # Two signals, both already in the old rows: the note the pet writes
        # ("桌宠" with or without a route name after it) and the ledger source
        # llm._record stamps on every call it makes ("source": "llm/chat").
        # Matching the note from its first character is deliberate: the pet's
        # rows in the live database say exactly "桌宠" (no route suffix), so
        # the narrower "桌宠 %" would have migrated almost none of them.
        conn.execute(
            "UPDATE run SET kind='chat' WHERE (kind IS NULL OR kind='work')"
            " AND (note LIKE '桌宠%' OR usage_json LIKE '%llm/chat%')"
        )
    conn.execute("UPDATE run SET kind='work' WHERE kind IS NULL OR kind=''")
    # Lineage: where this session was continued or forked from, and which run
    # brought it into existence. Old rows have no parent by definition, and
    # 'fresh' is what "no parent" is called, so the backfill states the truth
    # rather than inventing a chain.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(session)")}
    for column in ("parent_session_id", "origin_run_id"):
        if column not in cols:
            conn.execute(f"ALTER TABLE session ADD COLUMN {column} TEXT")
    if "relation" not in cols:
        conn.execute("ALTER TABLE session ADD COLUMN relation TEXT DEFAULT 'fresh'")
        conn.execute("UPDATE session SET relation='fresh' WHERE relation IS NULL OR relation=''")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(artifact)")}
    if "rel_path" not in cols:
        conn.execute("ALTER TABLE artifact ADD COLUMN rel_path TEXT")
    # The SQLite memory store is gone: a fact belongs in AGENTS.md now, where
    # `memory.adopt` can date it and a human can revert it. Rows an older build
    # wrote are copied out before the table is dropped, so an upgrade never
    # silently deletes somebody's notes.
    legacy = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory'"
    ).fetchone()
    if legacy:
        rows = [dict(row) for row in conn.execute("SELECT * FROM memory").fetchall()]
        if rows:
            (config.HOME / "memory-legacy.json").write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        conn.execute("DROP TABLE memory")
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
    _SCHEMA_READY.add(key)
    return conn


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


def create_session(
    workspace: str,
    title: str = "",
    parent_session_id: str = "",
    origin_run_id: str = "",
    relation: str = "fresh",
) -> dict:
    sid = new_id("ses")
    with connect() as c:
        c.execute(
            "INSERT INTO session(id, workspace_id, workspace, title, parent_session_id,"
            " origin_run_id, relation, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                sid,
                config.workspace_id(),
                workspace,
                title,
                parent_session_id or None,
                origin_run_id or None,
                relation or "fresh",
                now(),
            ),
        )
    return get_session(sid)


def get_session(sid: str) -> dict:
    with connect() as c:
        row = c.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()
    return dict(row) if row else {}


def set_session_lineage(sid: str, **fields: Any) -> None:
    """Write only the lineage fields, and only the ones the caller decided on.

    Kept narrow on purpose: the rest of the session row belongs to whoever
    created it, and a run finishing must never rewrite a title or a status.
    """
    allowed = {"parent_session_id", "origin_run_id", "relation"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE session SET {cols} WHERE id=?", (*fields.values(), sid))


def child_sessions(sid: str) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM session WHERE parent_session_id=? AND workspace_id=?"
            " ORDER BY created_at",
            (sid, config.workspace_id()),
        ).fetchall()
    return [dict(r) for r in rows]


def runs_for_session(sid: str) -> list[dict]:
    """Runs that executed inside one session row (newest first)."""
    if not sid:
        return []
    with connect() as c:
        rows = c.execute(
            "SELECT r.* FROM run r JOIN task t ON t.id = r.task_id"
            " WHERE t.session_id=? AND r.workspace_id=? ORDER BY r.started_at DESC",
            (sid, config.workspace_id()),
        ).fetchall()
    return [dict(r) for r in rows]


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
    lock: bool = False,
    rid: str = "",
    owner_pid: int | None = None,
    owner_host: str = "",
    kind: str = "work",
) -> dict:
    """Create a run, optionally taking the write lock on its directory.

    The row and the lock are inserted in one transaction. Doing them
    separately leaves a window where a lock exists with no run behind it, and
    a second process would reasonably conclude the holder was dead.

    A lock whose run has already finished is debris, and this retries once
    after clearing it: the alternative is every caller implementing the same
    two lines and one of them forgetting.
    """
    key = str(Path(cwd).resolve())
    for attempt in (0, 1):
        rid = rid or new_id("run")
        try:
            _insert_run(rid, task_id, agent, cwd, parent_run_id, note, effort, model,
                        route, provider, profile, key if lock else "",
                        owner_pid=owner_pid, owner_host=owner_host, kind=kind)
            return get_run(rid)
        except Busy as busy:
            if attempt == 0 and release_if_finished(busy.holder):
                continue
            raise
    raise Busy(key, {})  # pragma: no cover - the loop either returns or raises


def _insert_run(
    rid: str,
    task_id: str,
    agent: str,
    cwd: str,
    parent_run_id: str | None,
    note: str,
    effort: str,
    model: str,
    route: str,
    provider: str,
    profile: str,
    lock_key: str,
    owner_pid: int | None = None,
    owner_host: str = "",
    kind: str = "work",
) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO run(id, workspace_id, task_id, agent, cwd, parent_run_id, note,"
            " kind, effort, model, route, provider, profile, owner_pid, owner_host, started_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid,
                config.workspace_id(),
                task_id,
                agent,
                cwd,
                parent_run_id,
                note,
                kind or "work",
                effort,
                model,
                route,
                provider,
                profile,
                os.getpid() if owner_pid is None else owner_pid,
                owner_host or socket.gethostname(),
                now(),
            ),
        )
        if not lock_key:
            return
        try:
            c.execute(
                "INSERT INTO write_lock(id, workspace_id, cwd, run_id, acquired_at)"
                " VALUES(?,?,?,?,?)",
                (new_id("lock"), config.workspace_id(), lock_key, rid, now()),
            )
        except sqlite3.IntegrityError:
            c.rollback()
            raise Busy(lock_key, holder_of(c, lock_key)) from None


def holder_of(conn: sqlite3.Connection, key: str) -> dict:
    row = conn.execute(
        "SELECT l.*, r.agent, r.route, r.status AS run_status"
        " FROM write_lock l LEFT JOIN run r ON r.id = l.run_id"
        " WHERE l.cwd = ? AND l.released_at IS NULL",
        (key,),
    ).fetchone()
    return dict(row) if row else {}


def write_holder(cwd: str) -> dict:
    with connect() as c:
        return holder_of(c, str(Path(cwd).resolve()))


def release_write(run_id: str) -> None:
    if not run_id:
        return
    with connect() as c:
        c.execute(
            "UPDATE write_lock SET released_at=? WHERE run_id=? AND released_at IS NULL",
            (now(), run_id),
        )


def release_if_finished(holder: dict) -> bool:
    """Drop a lock whose run is over. Returns True when it was stale.

    A run that finished always released its own lock; this only catches the
    paths that skip it (an exception between the last event and the status
    write, or a hand edit).
    """
    run_id = holder.get("run_id") or ""
    if not run_id:
        return False
    with connect() as c:
        row = c.execute("SELECT status FROM run WHERE id=?", (run_id,)).fetchone()
    status = row["status"] if row else None
    if status in ("completed", "failed") or status is None:
        release_write(run_id)
        return True
    return False


def active_locks() -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT l.*, r.agent, r.route, r.status AS run_status FROM write_lock l"
            " LEFT JOIN run r ON r.id = l.run_id"
            " WHERE l.released_at IS NULL ORDER BY l.acquired_at"
        ).fetchall()
    return [dict(r) for r in rows]


def lock_path(path: str, owner: str) -> tuple[bool, dict]:
    """Take the write lock on a directory without creating a run.

    A merge is not a run: it happens in the main tree, it is short, and the only
    thing it needs from the run machinery is the same exclusion the runs have.
    """
    key = str(Path(path).resolve())
    with connect() as c:
        try:
            c.execute(
                "INSERT INTO write_lock(id, workspace_id, cwd, run_id, acquired_at)"
                " VALUES(?,?,?,?,?)",
                (new_id("lock"), config.workspace_id(), key, owner, now()),
            )
        except sqlite3.IntegrityError:
            return False, holder_of(c, key)
    return True, {}


def release_lock_for_cwd(cwd: str) -> bool:
    """Force-release whatever holds this directory. For `nodus unlock`."""
    with connect() as c:
        holder = holder_of(c, str(Path(cwd).resolve()))
        if not holder:
            return False
        c.execute(
            "UPDATE write_lock SET released_at=? WHERE cwd=? AND released_at IS NULL",
            (now(), str(Path(cwd).resolve())),
        )
    return True


def get_run(rid: str) -> dict:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM run WHERE id=? AND workspace_id=?",
            (rid, config.workspace_id()),
        ).fetchone()
    return dict(row) if row else {}


def list_runs(
    task_id: str | None = None, limit: int = 50, kind: str | None = None
) -> list[dict]:
    """Newest runs first, optionally of one kind.

    "kind" filters *before* the limit, which is the point: the pet's utterances
    are newer than every real run and must not push all the work out of a
    50-row window.
    Rows written before the column existed read as 'work' (see connect()).
    """
    where = ["workspace_id=?"]
    args: list[Any] = [config.workspace_id()]
    if task_id:
        where.append("task_id=?")
        args.append(task_id)
    if kind:
        where.append("COALESCE(NULLIF(kind, ''), 'work')=?")
        args.append(kind)
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM run WHERE " + " AND ".join(where) +
            " ORDER BY started_at DESC LIMIT ?",
            (*args, limit),
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


# ---- proposals ---------------------------------------------------------


def create_proposal(
    run_id: str,
    task_id: str,
    repo: str,
    branch: str,
    worktree: str,
    base: str,
    patch_path: str,
    files: list[dict],
    stat: str,
) -> dict:
    pid = new_id("prop")
    with connect() as c:
        c.execute(
            "INSERT INTO proposal(id, workspace_id, run_id, task_id, repo, branch, worktree,"
            " base, patch_path, files_json, stat, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pid,
                config.workspace_id(),
                run_id,
                task_id,
                repo,
                branch,
                worktree,
                base,
                patch_path,
                json.dumps(files, ensure_ascii=False),
                stat,
                now(),
            ),
        )
    return get_proposal(pid)


def get_proposal(pid: str) -> dict:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM proposal WHERE id=? AND workspace_id=?",
            (pid, config.workspace_id()),
        ).fetchone()
    if not row:
        return {}
    out = dict(row)
    try:
        out["files"] = json.loads(out.get("files_json") or "[]")
    except json.JSONDecodeError:
        out["files"] = []
    return out


def list_proposals(status: str = "open", limit: int = 50) -> list[dict]:
    with connect() as c:
        if status:
            rows = c.execute(
                "SELECT * FROM proposal WHERE status=? AND workspace_id=?"
                " ORDER BY created_at DESC LIMIT ?",
                (status, config.workspace_id(), limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM proposal WHERE workspace_id=? ORDER BY created_at DESC LIMIT ?",
                (config.workspace_id(), limit),
            ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["files"] = json.loads(item.get("files_json") or "[]")
        except json.JSONDecodeError:
            item["files"] = []
        out.append(item)
    return out


def proposals_for_run(run_id: str) -> list[dict]:
    """What a run put on a branch. Usually zero or one, but the query says what
    the data says instead of assuming."""
    if not run_id:
        return []
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM proposal WHERE run_id=? AND workspace_id=? ORDER BY created_at",
            (run_id, config.workspace_id()),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["files"] = json.loads(item.get("files_json") or "[]")
        except json.JSONDecodeError:
            item["files"] = []
        out.append(item)
    return out


def close_proposal(pid: str, status: str) -> None:
    with connect() as c:
        c.execute(
            "UPDATE proposal SET status=?, closed_at=? WHERE id=?",
            (status, now(), pid),
        )


# ---- pipelines ---------------------------------------------------------


def create_pipeline(kind: str, params: dict, label: str = "") -> dict:
    pid = new_id("pipe")
    with connect() as c:
        c.execute(
            "INSERT INTO pipeline(id, workspace_id, kind, label, params_json, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (pid, config.workspace_id(), kind, label, json.dumps(params, default=str), now()),
        )
    return get_pipeline(pid)


def get_pipeline(pid: str) -> dict:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM pipeline WHERE id=? AND workspace_id=?",
            (pid, config.workspace_id()),
        ).fetchone()
    return dict(row) if row else {}


def list_pipelines(status: str = "", limit: int = 20) -> list[dict]:
    with connect() as c:
        if status:
            rows = c.execute(
                "SELECT * FROM pipeline WHERE status=? AND workspace_id=?"
                " ORDER BY created_at DESC LIMIT ?",
                (status, config.workspace_id(), limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM pipeline WHERE workspace_id=? ORDER BY created_at DESC LIMIT ?",
                (config.workspace_id(), limit),
            ).fetchall()
    return [dict(r) for r in rows]


def update_pipeline(pid: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE pipeline SET {cols} WHERE id=?", (*fields.values(), pid))
