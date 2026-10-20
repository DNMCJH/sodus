"""Repair loop helpers: findings rendering and review bookkeeping."""

from __future__ import annotations

import time

from nodus import config, db, fix


FINDINGS = [
    {
        "severity": "high",
        "file": "slugify.py",
        "line": 9,
        "issue": "只处理半角空格",
        "evidence": "re.sub(r\" +\", \"-\", text)",
        "suggestion": "用 \\s+",
    },
    {
        "severity": "low",
        "file": "slugify.py",
        "line": 12,
        "issue": "连字符不折叠",
    },
]


def test_render_findings_includes_location_and_evidence():
    text = fix.render_findings(FINDINGS)
    assert "[HIGH] slugify.py:9" in text
    assert "只处理半角空格" in text
    assert "证据：" in text
    assert "[LOW]" in text


def test_render_findings_can_skip_low():
    text = fix.render_findings(FINDINGS, include_low=False)
    assert "[HIGH]" in text
    assert "[LOW]" not in text


def test_render_findings_empty():
    assert "没有给出具体问题" in fix.render_findings([])


def test_latest_review_picks_newest_for_target(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    task = db.create_task("demo", "green")
    target = db.create_run(task["id"], "codex", str(tmp_path))
    first = db.create_run(task["id"], "claude", str(tmp_path))
    second = db.create_run(task["id"], "claude", str(tmp_path))

    db.append_event(
        first["id"],
        1,
        "review.done",
        {"target_run": target["id"], "verdict": "revise", "counts": {}},
    )
    time.sleep(0.02)
    db.append_event(
        second["id"],
        1,
        "review.done",
        {"target_run": target["id"], "verdict": "accept", "counts": {}},
    )
    db.append_event(
        second["id"],
        2,
        "review.done",
        {"target_run": "run-other", "verdict": "reject", "counts": {}},
    )

    latest = fix.latest_review(task["id"], target["id"])
    assert latest["verdict"] == "accept"
    assert latest["review_run"] == second["id"]


def test_latest_review_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    task = db.create_task("demo", "green")
    assert fix.latest_review(task["id"], "run-missing") is None
