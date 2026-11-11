"""The benchmark is a measuring instrument; its own arithmetic is tested offline.

No model is called here: `bench.execute` takes a client seam, and the hidden
tests it scores are generated inside `tmp_path`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sodus import bench

TASKS = Path(__file__).resolve().parent.parent / "eval" / "tasks"


# ---- the eval set itself ------------------------------------------------


def test_eval_set_loads_completely():
    tasks = bench.discover(TASKS)
    # 9 easy/medium tasks + 3 hard ones (added so the router has data above
    # difficulty 2, where every "hard task needs a stronger route" claim was
    # previously unsupported).
    assert len(tasks) == 12
    assert len({task.id for task in tasks}) == 12
    for task in tasks:
        assert task.title.strip()
        assert task.prompt.strip()
        assert task.timeout > 0
        assert task.start.is_dir()
        assert task.test_files, f"{task.id}: hidden_tests is empty"
        assert (task.root / "task.json").is_file()
        # 0 means "not declared"; anything else must be a real 1-5 rating.
        assert 0 <= task.difficulty <= 5, f"{task.id}: difficulty out of range"


def test_the_hard_tasks_declare_their_difficulty():
    """The router reads `difficulty`, so the hard set has to declare it.

    Without this the hardest task scored 2 (derived from its title) and the
    policy could not tell that any benchmark had ever tested a hard task.
    """
    tasks = {task.id: task for task in bench.discover(TASKS)}
    assert tasks["interface-first-rules"].difficulty == 5
    assert tasks["debug-day-boundary"].difficulty == 4
    assert tasks["budget-currency-admit"].difficulty == 4


def test_a_task_without_required_fields_is_rejected(tmp_path):
    root = tmp_path / "broken"
    (root / "start").mkdir(parents=True)
    (root / "hidden_tests").mkdir()
    (root / "hidden_tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    (root / "task.json").write_text(json.dumps({"id": "broken", "title": "t", "timeout": 30}))
    with pytest.raises(ValueError, match="prompt"):
        bench.load_task(root)


def test_a_task_without_hidden_tests_is_rejected(tmp_path):
    root = tmp_path / "broken"
    (root / "start").mkdir(parents=True)
    (root / "hidden_tests").mkdir()
    (root / "task.json").write_text(
        json.dumps({"id": "broken", "title": "t", "prompt": "p", "timeout": 30})
    )
    with pytest.raises(ValueError, match="hidden_tests"):
        bench.load_task(root)


# ---- scoring ------------------------------------------------------------


def test_all_hidden_tests_green_is_a_solve():
    verdict = bench.score(tests_ok=True, timed_out=False, status="completed", changed=True)
    assert verdict == "solve"


def test_red_tests_after_a_completed_run_are_not_a_solve():
    verdict = bench.score(tests_ok=False, timed_out=False, status="completed", changed=True)
    assert verdict == "tests_red"


def test_a_timeout_is_a_timeout():
    verdict = bench.score(tests_ok=False, timed_out=True, status="running", changed=False)
    assert verdict == "timeout"


def test_no_change_is_named_as_such():
    verdict = bench.score(tests_ok=False, timed_out=False, status="completed", changed=False)
    assert verdict == "no_change"


def test_a_failed_run_is_an_agent_error():
    verdict = bench.score(tests_ok=False, timed_out=False, status="failed", changed=True)
    assert verdict == "agent_error"


def test_the_hidden_tests_outrank_the_process_status():
    # A worker can exit non-zero after doing the work, or be killed one second
    # too early. If the tree passes, the job got done; that is the measure.
    assert bench.score(tests_ok=True, timed_out=True, status="running", changed=True) == "solve"


# ---- one attempt, without a model ---------------------------------------


def make_task(tmp_path: Path, hidden: str) -> bench.Task:
    root = tmp_path / "task"
    (root / "start").mkdir(parents=True)
    (root / "start" / "solution.py").write_text("def answer():\n    return 1\n")
    (root / "hidden_tests").mkdir()
    (root / "hidden_tests" / "test_hidden.py").write_text(hidden)
    (root / "task.json").write_text(
        json.dumps({"id": "fake-task", "title": "fake", "prompt": "do it", "timeout": 30})
    )
    return bench.load_task(root)


class FakeClient:
    """Same three calls the real runner client offers; writes a file or not."""

    def __init__(self, status: str, writes: bool, error: str = "") -> None:
        self.status = status
        self.writes = writes
        self.error = error
        self.stopped: list[str] = []

    def start(self, task, workdir):
        if self.writes:
            (Path(workdir) / "solution.py").write_text("def answer():\n    return 2\n")
        return {"id": "run-fake"}

    def wait(self, run_id, timeout):
        started = 1000.0
        return {
            "id": run_id,
            "status": self.status,
            "started_at": started,
            "ended_at": started + 12 if self.status in ("completed", "failed") else started,
            "usage_json": json.dumps({"cost": 0.01, "currency": "CNY"}),
            "error": self.error,
        }

    def stop(self, run_id):
        self.stopped.append(run_id)
        return True


def run_fake(tmp_path: Path, hidden: str, client: FakeClient) -> dict:
    task = make_task(tmp_path, hidden)
    return bench.execute(
        task,
        "fake-route",
        tmp_path / "work",
        client=client,
        test_runner=bench.run_pytest,
    )


def test_execute_solve_when_hidden_tests_pass(tmp_path):
    row = run_fake(tmp_path, "def test_ok():\n    assert True\n", FakeClient("completed", writes=True))
    assert row["verdict"] == "solve"
    assert row["tests_ok"] is True
    assert row["cost"] == 0.01
    assert row["duration_s"] == 12.0


def test_execute_tests_red_when_the_tree_is_wrong(tmp_path):
    hidden = "import solution\n\ndef test_ok():\n    assert solution.answer() == 7\n"
    row = run_fake(tmp_path, hidden, FakeClient("completed", writes=True))
    assert row["verdict"] == "tests_red"
    assert row["tests_ok"] is False
    assert "failed" in row["pytest_tail"]


def test_execute_no_change_when_the_agent_touched_nothing(tmp_path):
    hidden = "import solution\n\ndef test_ok():\n    assert solution.answer() == 7\n"
    row = run_fake(tmp_path, hidden, FakeClient("completed", writes=False))
    assert row["verdict"] == "no_change"
    assert row["changed"] is False


def test_execute_timeout_and_stops_the_run(tmp_path):
    hidden = "import solution\n\ndef test_ok():\n    assert solution.answer() == 7\n"
    client = FakeClient("running", writes=False)
    row = run_fake(tmp_path, hidden, client)
    assert row["verdict"] == "timeout"
    assert client.stopped == ["run-fake"]


def test_execute_agent_error_when_the_run_failed(tmp_path):
    hidden = "import solution\n\ndef test_ok():\n    assert solution.answer() == 7\n"
    row = run_fake(tmp_path, hidden, FakeClient("failed", writes=True, error="exit code 1"))
    assert row["verdict"] == "agent_error"
    assert row["error"] == "exit code 1"


# ---- the table ----------------------------------------------------------


def fake_rows() -> list[dict]:
    return [
        {
            "task": "alpha",
            "verdict": "solve",
            "duration_s": 38.0,
            "cost": 0.04,
            "currency": "CNY",
            "pytest_tail": "11 passed in 0.10s",
        },
        {
            "task": "beta",
            "verdict": "tests_red",
            "duration_s": 44.0,
            "cost": 0.05,
            "currency": "CNY",
            "pytest_tail": "1 failed, 2 passed in 0.10s",
        },
    ]


def test_report_shows_solve_count_percentage_and_cost():
    text = bench.report(fake_rows())
    assert "solve 1/2 (50%)" in text
    assert "¥0.09" in text
    assert "平均 41s" in text
    for fragment in ("alpha", "beta", "tests_red", "1 failed, 2 passed"):
        assert fragment in text


def test_summary_rounds_to_whole_percent():
    totals = {
        "attempts": 6,
        "solved": 4,
        "mean_s": 41.2,
        "cost_cny": 0.09,
    }
    assert bench.format_summary(totals) == "solve 4/6 (67%) · 平均 41s · ¥0.09"


def test_one_currency_keeps_the_old_compact_summary():
    totals = {
        "attempts": 2,
        "solved": 1,
        "mean_s": 41.0,
        "costs": {"CNY": 0.09},
        "unlabeled": [],
        "cost_cny": 0.09,
    }
    assert bench.format_summary(totals) == "solve 1/2 (50%) · 平均 41s · ¥0.09"


def test_an_unlabeled_attempt_is_not_counted_as_yuan():
    """The old default was CNY: a dollar or unnamed amount joined the yuan total."""
    rows = [
        {"task": "a", "verdict": "solve", "duration_s": 1.0, "cost": 1.0, "currency": "CNY"},
        {"task": "b", "verdict": "solve", "duration_s": 1.0, "cost": 0.5, "currency": "USD"},
        {"task": "c", "verdict": "solve", "duration_s": 1.0, "cost": 0.25,
         "currency": "-", "provider": "relay-old"},
        {"task": "d", "verdict": "solve", "duration_s": 1.0, "cost": 0.75,
         "currency": "-", "provider": "claude"},
    ]

    totals = bench.summarize(rows)

    assert totals["costs"] == {"CNY": 1.0, "USD": 0.5}
    assert totals["cost_cny"] == 1.0
    assert totals["unlabeled"] == [
        {"provider": "claude", "total": 0.75, "runs": 1},
        {"provider": "relay-old", "total": 0.25, "runs": 1},
    ]
    text = bench.format_summary(totals)
    assert "CNY  1.0000" in text
    assert "USD  0.5000" in text
    assert "来源 claude  1 次  0.7500" in text
    assert "来源 relay-old  1 次  0.2500" in text
    assert "2.5000" not in text  # no total across buckets


def test_money_names_the_missing_unit_instead_of_guessing_yuan():
    assert bench._money({"cost": 0.04, "currency": "-"}) == "来源未记录 0.040"
    assert bench._money({"cost": 0.04, "currency": "CNY"}) == "¥0.040"
    assert bench._money({"cost": 0.04, "currency": "USD"}) == "USD 0.040"


def test_an_empty_table_does_not_divide_by_zero():
    assert bench.format_summary(bench.summarize([])) == "solve 0/0 (0%) · 平均 0s · ¥0.00"
