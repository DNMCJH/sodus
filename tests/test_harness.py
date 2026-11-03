"""Harness fingerprints: read-only evidence, and a rate that never counts an
unverified run as a pass.

The two 2026 harness papers both need the same prerequisite before they can
optimize anything: a comparable record of what was running and how it ended.
This module is that record's read side — no scheduling, no mutation.
"""

from __future__ import annotations

from nodus import config, db, diagnosis, harness
from nodus.cli import build_parser, main


def _run(**over) -> dict:
    base = {
        "id": "run-1",
        "agent": "claude",
        "route": "claude",
        "model": "m1",
        "provider": "relay",
        "status": "completed",
        "error": "",
    }
    base.update(over)
    return base


def test_version_of_hashes_the_template_itself():
    assert harness.version_of("same") == harness.version_of("same")
    assert harness.version_of("same") != harness.version_of("same ")
    assert len(harness.version_of("")) == 8


def test_fingerprint_falls_back_to_the_agent_and_names_what_is_missing():
    fp = harness.fingerprint({"agent": "claude", "route": "", "model": "", "provider": ""})
    assert fp == {
        "agent": "claude",
        "route": "claude",
        "model": harness.UNRECORDED,
        "provider": harness.UNRECORDED,
        "brief_version": harness.UNRECORDED,
    }


def test_outcome_never_calls_a_completed_run_passed_without_verification():
    assert harness.outcome(_run(), "ok") == "passed"
    assert harness.outcome(_run(), "red") == "red"
    assert harness.outcome(_run(), "none") == "unverified"
    assert harness.outcome(_run(status="failed", error="429 Too Many Requests"), "none") == "failed"
    assert harness.outcome(_run(status="running"), "none") == "live"
    assert harness.outcome(_run(status="stalled"), "none") == "live"


def test_stats_buckets_by_component_and_cause_and_keeps_unverified_out_of_the_rate():
    rows = [
        _run(id="r1"),
        _run(id="r2", status="failed", error="Command timed out after 60s"),
        _run(id="r3", status="failed", error="Command timed out after 60s"),
        _run(id="r4"),
        _run(id="r5", status="running"),
    ]
    verify = {"r1": "ok", "r2": "none", "r3": "none", "r4": "none", "r5": "none"}
    data = harness.stats(rows, verify, {}, by="route")
    buckets = {(g["key"], g["cause"]): g for g in data["groups"]}
    clean = buckets[("claude", harness.NO_CAUSE)]
    assert (clean["passed"], clean["unverified"], clean["pass_rate"]) == (1, 1, 1.0)
    timeout = buckets[("claude", "timeout")]
    assert (timeout["failed"], timeout["passed"], timeout["pass_rate"]) == (2, 0, 0.0)
    assert data["totals"]["runs"] == 4
    assert data["live"] == 1


def test_the_brief_version_comes_from_the_recorded_event():
    rows = [_run(id="r1", route="a"), _run(id="r2", route="b")]
    data = harness.stats(rows, {}, {"r1": "abc12345"}, by="brief_version")
    keys = {g["key"] for g in data["groups"]}
    assert keys == {"abc12345", harness.UNRECORDED}


def test_the_brief_version_changes_when_the_prompt_changes():
    from nodus import runner

    assert len(runner.BRIEF_VERSION) == 8
    assert harness.version_of(runner.PROMPT_TEMPLATE) == runner.BRIEF_VERSION


def test_the_command_is_registered_under_debug_and_prints_evidence(tmp_path, monkeypatch, capsys):
    args = build_parser("nodus").parse_args(["debug", "harness-stats", "--by", "model"])
    assert args.func.__name__ == "harness_stats"
    assert args.by == "model"

    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-harness")
    task = db.create_task("demo", "pytest green")
    run = db.create_run(task["id"], "claude", str(tmp_path), route="claude")
    db.update_run(
        run["id"],
        status="failed",
        started_at=1.0,
        ended_at=2.0,
        error="Command timed out after 60s",
    )
    code = main(["debug", "harness-stats"])
    out = capsys.readouterr().out
    assert code == 0
    assert "harness-stats" in out
    assert "timeout" in out
    assert "unverified" in out
