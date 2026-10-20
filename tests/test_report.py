"""Report assembly: numbers come from the DB, gaps are stated instead of hidden."""

from __future__ import annotations

import json
import time

from nodus import config, db, report


def _data(**over):
    base = {
        "task": {
            "id": "task-1",
            "title": "demo",
            "goal": "make add work",
            "acceptance": "pytest green",
            "status": "done",
        },
        "runs": [
            {
                "id": "run-1",
                "agent": "claude",
                "status": "completed",
                "started_at": 100.0,
                "ended_at": 130.0,
                "usage_json": json.dumps(
                    {"input_tokens": 10, "output_tokens": 5, "cache_read": 100, "cost": 0.25}
                ),
            },
            {
                "id": "run-2",
                "agent": "minimax",
                "status": "completed",
                "started_at": 130.0,
                "ended_at": 140.0,
                "usage_json": json.dumps(
                    {
                        "input_tokens": 1000,
                        "output_tokens": 200,
                        "cache_read": 10000,
                        "cost": 0.0034,
                        "currency": "CNY",
                        "cost_basis": "list_price",
                    }
                ),
            }
        ],
        "verifies": [
            {
                "run_id": "run-1",
                "phase": "red",
                "ok": False,
                "exit": 1,
                "duration_ms": 120,
                "path": "/tmp/verify-red.txt",
            }
        ],
        "reviews": [],
        "decisions": [],
        "totals": {
            "in": 10,
            "out": 5,
            "cache_read": 100,
            "cache_write": 0,
            "costs": {"USD": 0.25, "CNY": 0.0034},
            "cost_agents": ["claude", "minimax"],
            "seconds": 30.0,
            "wall": 30.0,
        },
        "flags": ["未做交叉审查"],
        "generated_at": time.time(),
    }
    base.update(over)
    return base


def test_render_includes_totals_and_verification():
    md = report.render_md(_data())
    assert "# 任务报告 — demo" in md
    assert "$0.2500" in md
    assert "| red | FAIL（预期） | 1 | 120ms |" in md
    assert "未做交叉审查" in md


def test_render_states_missing_cost_instead_of_guessing():
    data = _data()
    data["totals"] = {**data["totals"], "costs": {}, "cost_agents": []}
    md = report.render_md(data)
    assert "成本：未上报" in md


def test_render_lists_review_findings_source():
    data = _data(
        reviews=[
            {
                "run_id": "run-9",
                "reviewer": "codex",
                "verdict": "revise",
                "summary": "空输入没处理",
                "counts": {"critical": 0, "high": 1, "medium": 0, "low": 0},
                "path": "/tmp/runs/run-9/review.json",
            }
        ]
    )
    md = report.render_md(data)
    assert "### revise — codex（run-9）" in md
    assert "high 1" in md
    assert "review.md" in md


def test_collect_reports_missing_red_for_task_without_events(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    task = db.create_task("demo", "pytest green")
    data = report.collect(task["id"])
    assert data["runs"] == []
    assert any("没有任何验证记录" in flag for flag in data["flags"])
    assert any("未做交叉审查" in flag for flag in data["flags"])


def test_collect_sums_usage_and_flags_changed_tests(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    task = db.create_task("demo", "pytest green")
    run = db.create_run(task["id"], "claude", str(tmp_path))
    db.update_run(
        run["id"],
        status="completed",
        started_at=100.0,
        ended_at=110.0,
        usage_json=json.dumps({"input_tokens": 7, "output_tokens": 3}),
    )
    db.append_event(
        run["id"], 1, "verify.tests_changed", {"files": ["tests/test_x.py"]}
    )
    data = report.collect(task["id"])
    assert data["totals"]["in"] == 7
    assert data["totals"]["out"] == 3
    assert any("测试文件在实现阶段被改动" in flag for flag in data["flags"])


def test_collect_finds_verify_event_after_many_events(tmp_path, monkeypatch):
    """Regression: verify/review events are appended last, after a long stream."""
    monkeypatch.setattr(config, "HOME", tmp_path)
    task = db.create_task("demo", "pytest green")
    run = db.create_run(task["id"], "claude", str(tmp_path))
    for i in range(60):
        db.append_event(run["id"], i + 1, "message", {"text": "noise"})
    db.append_event(run["id"], 61, "verify.red", {"phase": "red", "ok": False, "exit": 1})
    data = report.collect(task["id"])
    assert [v["phase"] for v in data["verifies"]] == ["red"]


def test_write_report_creates_file_and_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    task = db.create_task("demo", "pytest green")
    run = db.create_run(task["id"], "claude", str(tmp_path))
    db.update_run(run["id"], status="completed", started_at=1.0, ended_at=2.0)
    path, data = report.write_report(task["id"])
    assert path.exists()
    assert path.read_text(encoding="utf-8").startswith("# 任务报告")
    assert any(a["kind"] == "report" for a in db.artifacts_for(run["id"]))
