"""Observed usage: a harness session is read, priced, and imported idempotently."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sodus import config, db, observe, usage

BJ = timezone(timedelta(hours=8))


def _ts() -> int:
    """Monday 2026-10-26 10:00 Beijing: DeepSeek peak, so the price is fixed."""
    return int(datetime(2026, 10, 26, 10, tzinfo=BJ).timestamp() * 1000)


def _write_session(root: Path, *, session: str = "session-abc", project: str = "--proj--") -> Path:
    zstandard = pytest.importorskip("zstandard")
    stamp = _ts()
    lines = [
        {
            "type": "request/header",
            "seq": 1,
            "time": stamp,
            "data": {"header": {"config": {"provider": "deepseek-official", "model": "deepseek-flash"}}},
        },
        {
            "type": "assistant/message",
            "seq": 2,
            "time": stamp + 1000,
            "data": {
                "usage": {
                    "inputTokens": 1_000_000,
                    "outputTokens": 500_000,
                    "cacheReadTokens": 20_000_000,
                    "cacheWriteTokens": 0,
                }
            },
        },
        {"type": "tool/result", "seq": 3, "time": stamp + 2000, "data": {"content": []}},
    ]
    text = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in lines)
    path = root / "sessions" / project / session / "session.v4.jsonl.zstd"
    path.parent.mkdir(parents=True, exist_ok=True)
    half = len(text) // 2
    # Two frames, because a real session file is many frames appended.
    frames = [
        zstandard.ZstdCompressor().compress(part.encode())
        for part in (text[:half], text[half:])
    ]
    path.write_bytes(b"".join(frames))
    return path


@pytest.fixture()
def dsh_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("SODUS_DSH_HOME", str(tmp_path / "dsh"))
    return tmp_path / "dsh"


def test_dry_run_prices_without_writing(dsh_root):
    _write_session(dsh_root)

    result = observe.scan(dry_run=True)

    assert result["messages"] == 1
    assert result["cost"] == {"CNY": 6.8}
    assert result["tokens"]["cache_read"] == 20_000_000
    assert result["unpriced"] == 0
    assert db.observations() == []


def test_import_is_idempotent(dsh_root):
    _write_session(dsh_root)

    first = observe.scan(dry_run=False)
    assert first["inserted"] == 1

    second = observe.scan(dry_run=False)
    assert second["inserted"] == 0
    assert second["skipped"] == 1

    forced = observe.scan(dry_run=False, force=True)
    assert forced["inserted"] == 0  # unique key, not the cursor, is the guard


def test_observed_runs_reach_the_usage_report(dsh_root):
    _write_session(dsh_root)
    observe.scan(dry_run=False)

    rows = observe.observed_runs()
    assert len(rows) == 1
    assert rows[0]["agent"] == "dsh"
    assert rows[0]["route"] == "dsh"
    usage_json = json.loads(rows[0]["usage_json"])
    assert usage_json["cache_read"] == 20_000_000
    assert usage_json["cost"] == 6.8
    assert usage_json["cost_basis"] == "list_price"

    report = usage.summary(period="all", group_by="route")
    dsh = [group for group in report["groups"] if group["key"] == "dsh"]
    assert dsh, report["groups"]
    assert dsh[0]["tokens"]["cache_read"] == 20_000_000
    assert dsh[0]["cost"]["CNY"]["total"] == 6.8
