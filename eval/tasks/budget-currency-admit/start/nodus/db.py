"""Trimmed copy of the workspace database.

``budget.py`` reads exactly one table, so that is all this file creates. The
real ``db.py`` is much larger; the query shape and the ``usage_json`` column
are the parts that matter here.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    started_at REAL,
    usage_json TEXT
);
CREATE INDEX IF NOT EXISTS run_workspace_started
    ON run (workspace_id, started_at);
"""


def path() -> Path:
    """Where the database file lives. ``NODUS_DB`` keeps tests off the real one."""
    return Path(os.environ.get("NODUS_DB") or "nodus.db")


def connect() -> sqlite3.Connection:
    """Open the database, creating the schema on first use."""
    conn = sqlite3.connect(path())
    conn.executescript(SCHEMA)
    return conn
