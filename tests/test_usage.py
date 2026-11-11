"""Usage stats: one cache-hit definition, no invented zeros and no FX.

Every fixture row here is synthetic except the one integration test, which
writes into a throwaway SODUS_HOME so a test never touches the operator's real
history.
"""

from __future__ import annotations

import json
import time

import pytest

from sodus import config, db, usage


def _local(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> float:
    return time.mktime((year, month, day, hour, minute, second, 0, 0, -1))


def _run(started_at: float, usage_json=None, **fields) -> dict:
    row = {
        "agent": "claude",
        "route": "claude",
        "provider": "relay",
        "model": "claude-opus-5-5[1M]",
        "status": "completed",
        "started_at": started_at,
        "usage_json": json.dumps(usage_json) if isinstance(usage_json, dict) else usage_json,
    }
    row.update(fields)
    return row


def _bucket(**tokens) -> dict:
    base = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0}
    base.update(tokens)
    return base


def test_day_window_starts_at_local_midnight():
    now = _local(2026, 10, 24, 12)
    start, end, label = usage.bounds("day", now)
    assert start == _local(2026, 10, 24)
    assert end == _local(2026, 10, 25)
    assert label == "2026-10-24"
    rows = [
        _run(_local(2026, 10, 24, 0, 0, 0)),
        _run(_local(2026, 10, 23, 23, 59, 59)),
        _run(_local(2026, 10, 25, 0, 0, 0)),
    ]
    assert usage.summarize(rows, start=start, end=end)["totals"]["runs"] == 1


def test_month_window_is_a_calendar_month():
    now = _local(2026, 10, 24, 12)
    start, end, label = usage.bounds("month", now)
    assert start == _local(2026, 10, 1)
    assert end == _local(2026, 11, 1)
    assert label == "2026-10"
    rows = [
        _run(_local(2026, 10, 1, 0, 0, 0)),
        _run(_local(2026, 9, 30, 23, 59, 59)),
        _run(_local(2026, 11, 1, 0, 0, 0)),
    ]
    assert usage.summarize(rows, start=start, end=end)["totals"]["runs"] == 1


def test_all_period_has_no_bounds():
    start, end, label = usage.bounds("all")
    assert start is None and end is None and label == "全部历史"
    rows = [_run(1.0), _run(10_000_000.0)]
    assert usage.summarize(rows, start=start, end=end)["totals"]["runs"] == 2


def test_invalid_period_and_dimension_are_named_errors():
    with pytest.raises(ValueError):
        usage.bounds("week")
    with pytest.raises(ValueError):
        usage.summarize([], group_by="harness")


def test_route_falls_back_to_agent_for_legacy_rows():
    rows = [_run(1.0, route=None, agent="codex"), _run(2.0, route="claude")]
    groups = {group["key"]: group for group in usage.summarize(rows, "route")["groups"]}
    assert set(groups) == {"codex", "claude"}
    assert groups["codex"]["runs"] == 1


def test_each_dimension_buckets_by_its_own_column():
    rows = [
        _run(1.0, model="gpt-6.1-sol", provider="relay-openai", agent="codex", route="codex"),
        _run(2.0, model="MiniMax-M3", provider="minimax", agent="minimax", route="minimax"),
    ]
    by_model = {group["key"] for group in usage.summarize(rows, "model")["groups"]}
    by_provider = {group["key"] for group in usage.summarize(rows, "provider")["groups"]}
    by_agent = {group["key"] for group in usage.summarize(rows, "agent")["groups"]}
    assert by_model == {"gpt-6.1-sol", "MiniMax-M3"}
    assert by_provider == {"relay-openai", "minimax"}
    assert by_agent == {"codex", "minimax"}


def test_missing_model_stays_unknown_instead_of_joining_a_real_one():
    rows = [_run(1.0, model=""), _run(2.0, model="MiniMax-M3")]
    groups = {group["key"]: group for group in usage.summarize(rows, "model")["groups"]}
    assert "-" in groups and groups["-"]["runs"] == 1


def test_token_total_sums_all_four_fields():
    row = _run(1.0, _bucket(input_tokens=100, output_tokens=10, cache_read=300, cache_write=7))
    tokens = usage.summarize([row])["totals"]["tokens"]
    assert tokens == {
        "prompt": 100,
        "completion": 10,
        "cache_read": 300,
        "cache_write": 7,
        "total": 417,
    }


def test_cache_hit_rate_is_the_stated_formula():
    # 300 / (100 + 300 + 100) = 0.6, not 300/100 and not 300/500-with-completion.
    row = _run(1.0, _bucket(input_tokens=100, output_tokens=999, cache_read=300, cache_write=100))
    assert usage.summarize([row])["totals"]["cache_hit_rate"] == pytest.approx(0.6)


def test_cache_hit_rate_is_none_when_the_denominator_is_zero():
    row = _run(1.0, _bucket(output_tokens=50))
    assert usage.summarize([row])["totals"]["cache_hit_rate"] is None


def test_cache_hit_rate_sums_before_dividing_not_average_of_rates():
    # A: 90/100 = 0.9. B: 10/1000 = 0.01. Average would be 0.455; sums give 0.0909.
    runs = [
        _run(1.0, _bucket(input_tokens=10, cache_read=90)),
        _run(2.0, _bucket(input_tokens=990, cache_read=10)),
    ]
    rate = usage.summarize(runs)["totals"]["cache_hit_rate"]
    assert rate == pytest.approx(100 / 1100, abs=1e-4)
    assert rate != pytest.approx(0.455, abs=1e-2)


def test_runs_without_usage_are_unrecorded_and_stay_out_of_the_mean():
    runs = [
        _run(1.0, {"cost": 1.0, "currency": "CNY"}),
        _run(2.0, {"cost": 3.0, "currency": "CNY"}),
        _run(3.0, None),
        _run(4.0, ""),
    ]
    totals = usage.summarize(runs)["totals"]
    assert totals["runs"] == 4
    assert totals["unrecorded"] == 2
    assert totals["priced"] == 2
    # 4.0 / 2 priced runs, not 4.0 / 4 runs and not 1.0 for the missing rows.
    assert totals["cost"]["CNY"]["total"] == pytest.approx(4.0)
    assert totals["cost"]["CNY"]["mean"] == pytest.approx(2.0)
    assert totals["cost"]["CNY"]["runs"] == 2


def test_an_empty_usage_object_is_unrecorded_too():
    totals = usage.summarize([_run(1.0, {})])["totals"]
    assert totals["runs"] == 1 and totals["unrecorded"] == 1 and totals["priced"] == 0
    assert totals["cost"] == {}


def test_currencies_are_listed_separately_and_never_converted():
    runs = [
        _run(1.0, {"cost": 2.0, "currency": "CNY"}),
        _run(2.0, {"cost": 4.0, "currency": "CNY"}),
        _run(3.0, {"cost": 3.0, "currency": "USD"}),
    ]
    cost = usage.summarize(runs)["totals"]["cost"]
    assert set(cost) == {"CNY", "USD"}
    assert cost["CNY"]["total"] == pytest.approx(6.0)
    assert cost["CNY"]["mean"] == pytest.approx(3.0)
    assert cost["USD"]["total"] == pytest.approx(3.0)


def test_a_cost_without_a_currency_is_not_labelled_usd():
    cost = usage.summarize([_run(1.0, {"cost": 1.5})])["totals"]["cost"]
    assert usage.UNKNOWN_CURRENCY in cost
    assert "USD" not in cost


def test_both_cost_bases_contribute_and_are_counted():
    runs = [
        _run(1.0, {"cost": 1.0, "currency": "CNY", "cost_basis": "calibrated"}),
        _run(2.0, {"cost": 2.0, "currency": "CNY", "cost_basis": "metered"}),
    ]
    totals = usage.summarize(runs)["totals"]
    assert totals["cost"]["CNY"]["total"] == pytest.approx(3.0)
    assert totals["basis"] == {"calibrated": 1, "metered": 1}


def test_failed_runs_still_spend_money_and_tokens():
    runs = [
        _run(1.0, {"cost": 1.0, "currency": "CNY"}, status="failed"),
        _run(2.0, {"cost": 1.0, "currency": "CNY"}, status="completed"),
    ]
    totals = usage.summarize(runs)["totals"]
    assert totals["runs"] == 2 and totals["completed"] == 1
    assert totals["cost"]["CNY"]["total"] == pytest.approx(2.0)


def test_groups_are_ordered_by_runs_then_tokens():
    runs = [
        _run(1.0, _bucket(input_tokens=10), route="small"),
        _run(2.0, _bucket(input_tokens=10), route="big", model="m1"),
        _run(3.0, _bucket(input_tokens=5), route="big", model="m1"),
    ]
    keys = [group["key"] for group in usage.summarize(runs, "route")["groups"]]
    assert keys[0] == "big"


def test_summary_reads_the_run_table(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setenv("SODUS_WORKSPACE", "ws-usage-test")
    now = time.time()
    first = db.create_run(task_id="task-usage", agent="claude", cwd=".")
    db.update_run(
        first["id"],
        status="completed",
        started_at=now,
        usage_json=json.dumps(_bucket(input_tokens=100, cache_read=300, cost=2.0, currency="CNY")),
    )
    second = db.create_run(task_id="task-usage", agent="dsh", cwd=".")
    db.update_run(second["id"], status="failed", started_at=now, model="m")

    data = usage.summary(period="day", group_by="agent", now=now)
    assert data["period"] == "day" and data["group_by"] == "agent"
    assert data["window"]["label"] == time.strftime("%Y-%m-%d", time.localtime(now))
    assert data["cache_hit_rate_formula"] == usage.CACHE_HIT_RATE_FORMULA
    assert data["totals"]["runs"] == 2
    assert data["totals"]["unrecorded"] == 1
    assert data["totals"]["cost"]["CNY"]["total"] == pytest.approx(2.0)
    # json.dumps is what `sodus usage --json` does; the payload must survive it.
    assert json.loads(json.dumps(data))["totals"]["tokens"]["total"] == 400
