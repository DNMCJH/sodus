"""The CC Switch probe reads two tables and reports; it must never raise."""

from __future__ import annotations

import sqlite3
import time

from sodus import observe_ccswitch

ROLLUPS = """
create table usage_daily_rollups (
  date text, app_type text, provider_id text, model text,
  request_count integer, input_tokens integer, output_tokens integer,
  cache_read_tokens integer, cache_creation_tokens integer, total_cost_usd real
)
"""
PROXY = """
create table proxy_request_logs (
  app_type text, model text, input_tokens integer, output_tokens integer,
  cache_read_tokens integer, cache_creation_tokens integer,
  total_cost_usd real, created_at real
)
"""


def write_db(path, rollups=(), proxy=()):
    con = sqlite3.connect(path)
    con.execute(ROLLUPS)
    con.execute(PROXY)
    for row in rollups:
        con.execute(
            "insert into usage_daily_rollups values (?,?,?,?,?,?,?,?,?,?)", row
        )
    for row in proxy:
        con.execute("insert into proxy_request_logs values (?,?,?,?,?,?,?,?)", row)
    con.commit()
    con.close()


def test_reads_rollups_and_counts_zero_cost_rows(tmp_path):
    db = tmp_path / "cc-switch.db"
    write_db(
        db,
        rollups=[
            ("2026-09-26", "claude", "_session", "claude-opus-5", 3, 10, 2, 0, 0, 0.0),
            ("2026-09-25", "claude", "_session", "claude-opus-5", 7, 100, 20, 50, 0, 1.5),
            ("2026-09-26", "codex", "_codex_session", "gpt-6-sol", 75, 1000, 30, 4000, 0, 0.0),
        ],
    )
    result = observe_ccswitch.scan(db)
    assert result["available"] is True
    rollups = result["rollups"]
    assert rollups["rows"] == 3
    assert rollups["requests"] == 85
    assert rollups["start"] == "2026-09-25" and rollups["end"] == "2026-09-26"
    assert rollups["tokens"]["input"] == 1110
    assert rollups["tokens"]["cache_read"] == 4050
    assert rollups["estimated_usd"] == 1.5
    # three zero-cost rows, each with tokens; requests are counted in full
    assert rollups["zero_cost_requests"] == 3 + 75
    assert set(rollups["apps"]) == {"claude", "codex"}
    assert rollups["apps"]["claude"]["requests"] == 10


def test_reads_proxy_window_and_apps(tmp_path):
    db = tmp_path / "cc-switch.db"
    first = time.mktime((2026, 9, 28, 4, 0, 0, 0, 0, -1))
    last = time.mktime((2026, 10, 23, 23, 0, 0, 0, 0, -1))
    write_db(
        db,
        proxy=[
            ("codex", "gpt-6.1-sol", 1000000, 1000, 500000, 0, 2.0, first),
            ("claude", "claude-opus-5-5", 100, 10, 20000, 0, 3.5, last),
        ],
    )
    result = observe_ccswitch.scan(db)
    proxy = result["proxy"]
    assert proxy["rows"] == 2
    assert proxy["requests"] == 2
    assert proxy["start"] == "2026-09-28"
    assert proxy["end"] == "2026-10-23"
    assert proxy["estimated_usd"] == 5.5
    assert proxy["tokens"]["cache_read"] == 520000


def test_missing_database_is_an_answer_not_an_exception(tmp_path):
    result = observe_ccswitch.scan(tmp_path / "nope.db")
    assert result["available"] is False
    assert "no cc-switch database" in result["error"]


def test_a_table_that_is_gone_is_reported_and_the_other_still_reads(tmp_path):
    db = tmp_path / "cc-switch.db"
    con = sqlite3.connect(db)
    con.execute(ROLLUPS)
    con.execute(
        "insert into usage_daily_rollups values (?,?,?,?,?,?,?,?,?,?)",
        ("2026-09-26", "claude", "_session", "m", 1, 1, 1, 0, 0, 0.1),
    )
    con.commit()
    con.close()
    result = observe_ccswitch.scan(db)
    assert result["available"] is True
    assert result["rollups"]["rows"] == 1
    assert result["proxy"] is None
    assert any("proxy" in note for note in result["notes"])
