"""The vault page: plain markdown, and only claims the database can back."""

from __future__ import annotations

import json

from sodus import desk


def _row(**over) -> dict:
    base = {
        "id": "task-1",
        "title": "demo",
        "status": "open",
        "agent": "dsh",
        "run_status": "completed",
        "verify": "ok",
        "cost": "¥0.03",
        "updated": 1_700_000_000,
    }
    base.update(over)
    return base


def test_clean_tasks_produce_no_todos():
    assert desk.todos([_row(), _row(id="task-2")]) == []


def test_unverified_runs_collapse_into_one_line():
    rows = [_row(id=f"task-{i}", verify="none") for i in range(3)]
    lines = desk.todos(rows)
    assert len(lines) == 1
    assert "3 个任务跑完但没有验证记录" in lines[0]
    assert "task-0" in lines[0] and "task-2" in lines[0]


def test_hard_problems_stay_individual():
    rows = [
        _row(id="task-a", status="needs_review"),
        _row(id="task-b", run_status="failed"),
        _row(id="task-c", verify="red"),
    ]
    lines = desk.todos(rows)
    assert len(lines) == 3
    assert "审查有异议" in lines[0]
    assert "执行失败" in lines[1]
    assert "测试红着" in lines[2]


def test_render_is_checkbox_friendly():
    # Axel Desk turns open checkboxes into dashboard rows, so a pending item
    # must be a real `- [ ]` line, and a clean page must say so in words.
    md = desk.render([_row(verify="none", title="写测试")], generated_at=1_700_000_000)
    assert "- [ ] " in md
    assert "| 任务 |" in md
    assert "未验证" in md

    clean = desk.render([_row()], generated_at=1_700_000_000)
    assert "- [ ] " not in clean
    assert "没有需要人处理的项" in clean


def test_empty_workspace_still_renders():
    md = desk.render([], generated_at=1_700_000_000)
    assert "没有需要人处理的项" in md
    assert "（没有任务）" in md


def test_cost_cell_never_borrows_a_symbol_for_a_missing_currency():
    """The old `_money` printed a missing currency as "$" — a 7x overstatement."""
    assert desk._money({"usage_json": json.dumps({"cost": 0.03})}) == "来源未记录 0.03"
    assert desk._money({"usage_json": json.dumps({"cost": 0.03, "currency": "CNY"})}) == "¥0.03"
    assert desk._money({"usage_json": json.dumps({"cost": 0.03, "currency": "USD"})}) == "$0.03"
    assert desk._money({"usage_json": json.dumps({"cost": 0.03, "currency": "EUR"})}) == "EUR 0.03"
    assert desk._money({"usage_json": json.dumps({"input_tokens": 5})}) == "-"
