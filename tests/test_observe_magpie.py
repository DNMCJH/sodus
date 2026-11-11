"""The magpie probe reads its request log; a broken line must not sink it."""

from __future__ import annotations

from sodus import observe_magpie

ROWS = [
    {"t": "2026-10-26T11:17:31.2704483+08:00", "agent": "claude", "provider": "9527",
     "model": "claude-sonnet-5-5", "in": 100, "out": 20, "cache_read": 5, "cache_write": 0,
     "ms": 1000, "status": 200},
    {"t": "2026-10-26T11:18:31.0000000+08:00", "agent": "claude", "provider": "claude",
     "model": "claude-opus-5-5", "in": 200, "out": 40, "cache_read": 10, "cache_write": 2,
     "ms": 3000, "status": 401},
    {"t": "2026-10-26T11:19:31.0000000+08:00", "agent": "codex", "provider": "9527",
     "model": "gpt-6.1-sol", "in": 300, "out": 60, "cache_read": 0, "cache_write": 0,
     "ms": 2000, "status": 200},
]


def test_scan_aggregates_agents_providers_and_tokens(tmp_path):
    log = tmp_path / "usage.jsonl"
    lines = ["{bad json"] + [__import__("json").dumps(r) for r in ROWS]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = observe_magpie.scan(log)
    assert result["available"] is True
    assert result["requests"] == 3
    assert result["failed"] == 1
    assert result["unparsed"] == 1
    assert result["tokens"] == {"input": 600, "output": 120, "cache_read": 15, "cache_write": 2}
    assert result["start"] == "2026-10-26T11:17"
    assert result["end"] == "2026-10-26T11:19"
    assert result["ms"] == 6000
    assert set(result["by_agent"]) == {"claude", "codex"}
    assert result["by_agent"]["claude"]["requests"] == 2
    assert result["by_agent"]["claude"]["failed"] == 1
    assert result["by_provider"]["9527"]["requests"] == 2


def test_missing_log_is_an_answer_not_an_exception(tmp_path):
    result = observe_magpie.scan(tmp_path / "nope.jsonl")
    assert result["available"] is False
    assert "no magpie usage log" in result["error"]


def test_empty_log_says_so(tmp_path):
    log = tmp_path / "usage.jsonl"
    log.write_text("", encoding="utf-8")
    result = observe_magpie.scan(log)
    assert result["available"] is False
    assert result["error"] == "usage log is empty"
