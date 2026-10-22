"""The daily ceiling refuses new runs and never kills a running one."""

from __future__ import annotations

import json

from nodus import budget, config, db


def _run(cost: float, currency: str = "CNY") -> str:
    created = db.create_run(task_id="budget-test", agent="claude", cwd=".")
    db.update_run(
        created["id"],
        usage_json=json.dumps({"cost": cost, "currency": currency}),
    )
    return created["id"]


def _clean(ids: list[str]) -> None:
    conn = db.connect()
    for rid in ids:
        conn.execute("DELETE FROM run WHERE id=?", (rid,))
    conn.commit()
    conn.close()


def test_no_limit_means_no_refusal(monkeypatch):
    monkeypatch.setattr(budget, "limit", lambda: 0.0)
    info = budget.check()
    assert info["over"] is False and info["remaining"] is None


def test_limit_reads_the_environment_first(monkeypatch):
    monkeypatch.setenv("NODUS_DAILY_BUDGET", "12.5")
    assert budget.limit() == 12.5
    monkeypatch.setenv("NODUS_DAILY_BUDGET", "not-a-number")
    assert budget.limit() == 0.0


def test_spend_today_counts_only_cny(monkeypatch):
    monkeypatch.setattr(budget, "limit", lambda: 100.0)
    ids = [_run(1.5), _run(2.0, "USD")]
    try:
        # The USD run is excluded rather than converted at an invented rate.
        assert budget.spent() >= 1.5
        assert budget.spent() < 3.5
    finally:
        _clean(ids)


def test_over_the_limit_is_reported_with_numbers(monkeypatch):
    monkeypatch.setattr(budget, "limit", lambda: 1.0)
    monkeypatch.setattr(budget, "spent", lambda now=None: 2.5)
    info = budget.check()
    assert info["over"] is True and info["remaining"] == 0.0
    message = budget.refuse_message(info)
    assert "2.50" in message and "1.00" in message


def test_the_ceiling_refuses_new_runs(monkeypatch):
    monkeypatch.setattr(budget, "limit", lambda: 1.0)
    monkeypatch.setattr(budget, "spent", lambda now=None: 9.0)
    from nodus import runner

    try:
        runner.RUNNER.start(task_id="budget-refuse", route_name="claude", cwd=".")
    except RuntimeError as error:
        assert "daily ceiling" in str(error)
    else:  # pragma: no cover - the point of the test
        raise AssertionError("a run was allowed past the daily ceiling")
