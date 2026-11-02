"""The ledger: one reading of `usage_json`, one way to add it up.

The fixtures are synthetic on purpose. A test that reads the operator's real
history is a test that goes red because of the user's afternoon.
"""

from __future__ import annotations

import json

import pytest

from nodus import ledger


def _run(started_at: float = 1.0, usage=None, **fields) -> dict:
    row = {
        "agent": "claude",
        "route": "claude",
        "provider": "relay",
        "model": "claude-opus-5-5[1M]",
        "status": "completed",
        "started_at": started_at,
        "usage_json": json.dumps(usage) if isinstance(usage, dict) else usage,
    }
    row.update(fields)
    return row


# ---- parsing ----------------------------------------------------------


def test_missing_token_fields_are_none_not_zero():
    """A run that recorded only output did not record zero input."""
    usage = ledger.parse({"output_tokens": 12})
    assert usage.tokens.output == 12
    assert usage.tokens.input is None
    assert usage.tokens.cache_read is None
    assert usage.tokens.cache_write is None
    assert usage.tokens.total() == 12


def test_a_fully_recorded_run_round_trips():
    usage = ledger.parse(
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read": 300,
            "cache_write": 5,
            "cost": 0.012345,
            "currency": "CNY",
            "cost_basis": "calibrated",
        }
    )
    assert usage.tokens.as_report() == {
        "prompt": 100,
        "completion": 20,
        "cache_read": 300,
        "cache_write": 5,
    }
    assert usage.cost.amount == pytest.approx(0.012345)
    assert usage.cost.currency == "CNY"
    assert usage.cost.basis == "calibrated"


def test_an_unpriced_run_has_no_amount_and_no_currency():
    usage = ledger.parse({"input_tokens": 10})
    assert usage.cost.amount is None
    assert usage.cost.currency is None
    assert usage.cost.basis is None


def test_bad_input_is_unrecorded_instead_of_raising():
    """One truncated row must not take a stats page down."""
    for raw in (None, "", "{not json", "[]", "[1, 2]", 0):
        usage = ledger.parse(raw)
        assert not usage.recorded()
        assert usage.tokens.total() == 0


def test_a_bool_is_not_a_number():
    usage = ledger.parse({"cost": True, "input_tokens": False})
    assert usage.cost.amount is None
    assert usage.tokens.input is None


def test_currency_is_normalised_but_never_invented():
    assert ledger.parse({"cost": 1.0, "currency": "cny"}).cost.currency == "CNY"
    assert ledger.parse({"cost": 1.0, "currency": "  "}).cost.currency is None
    assert ledger.parse({"cost": 1.0}).cost.currency is None


def test_cost_of_reports_missing_currency_as_the_unknown_bucket():
    assert ledger.cost_of(json.dumps({"cost": 2.0, "currency": "CNY"})) == (2.0, "CNY", "")
    amount, currency, basis = ledger.cost_of(json.dumps({"cost": 2.0}))
    assert amount == 2.0
    assert currency == ledger.UNKNOWN_CURRENCY
    assert basis == ""
    assert ledger.cost_of(json.dumps({"currency": "CNY"})) is None


def test_attempts_are_kept_for_a_fallback_run():
    usage = ledger.parse(
        {"cost": 1.0, "currency": "CNY", "attempts": [{"provider": "relay", "cost": 1.0}]}
    )
    assert usage.cost.attempts[0]["provider"] == "relay"


# ---- grouping and windows --------------------------------------------


def test_groups_by_route_agent_provider_and_model():
    rows = [
        _run(1.0, {"input_tokens": 1}, route="claude", agent="claude"),
        _run(2.0, {"input_tokens": 1}, route="codex", agent="codex", provider="relay-openai"),
        _run(3.0, {"input_tokens": 1}, route="codex", agent="codex", model="gpt-6.1-sol"),
    ]
    assert {g["key"] for g in ledger.summarize(rows, "route")["groups"]} == {"claude", "codex"}
    agents = {g["key"]: g["runs"] for g in ledger.summarize(rows, "agent")["groups"]}
    assert agents == {"claude": 1, "codex": 2}
    assert {g["key"] for g in ledger.summarize(rows, "provider")["groups"]} == {
        "relay",
        "relay-openai",
    }
    assert {g["key"] for g in ledger.summarize(rows, "model")["groups"]} == {
        "claude-opus-5-5[1M]",
        "gpt-6.1-sol",
    }


def test_route_falls_back_to_the_agent_on_legacy_rows():
    rows = [_run(1.0, {"input_tokens": 1}, route=None, agent="codex")]
    assert ledger.summarize(rows, "route")["groups"][0]["key"] == "codex"


def test_an_invalid_dimension_is_a_named_error():
    with pytest.raises(ValueError):
        ledger.summarize([], group_by="harness")


def test_a_period_window_and_an_explicit_window_are_mutually_exclusive():
    with pytest.raises(ValueError):
        ledger.summarize([], period="day", start=0.0)


def test_the_window_excludes_rows_outside_it_and_keeps_unbounded_ones():
    rows = [_run(1.0), _run(100.0), _run(None)]
    assert ledger.summarize(rows, start=0.0, end=50.0)["totals"]["runs"] == 1
    assert ledger.summarize(rows)["totals"]["runs"] == 3


def test_unrecorded_runs_count_as_runs_and_nothing_else():
    rows = [_run(1.0, None), _run(2.0, {}), _run(3.0, {"cost": 1.0, "currency": "CNY"})]
    totals = ledger.summarize(rows)["totals"]
    assert totals["runs"] == 3
    assert totals["unrecorded"] == 2
    assert totals["priced"] == 1
    assert totals["cost"]["CNY"] == {"total": 1.0, "mean": 1.0, "runs": 1}


# ---- money ------------------------------------------------------------


def test_currencies_are_summed_apart_and_never_converted():
    rows = [
        _run(1.0, {"cost": 2.0, "currency": "CNY"}),
        _run(2.0, {"cost": 4.0, "currency": "CNY"}),
        _run(3.0, {"cost": 3.0, "currency": "USD"}),
    ]
    cost = ledger.summarize(rows)["totals"]["cost"]
    assert cost["CNY"]["total"] == pytest.approx(6.0)
    assert cost["USD"]["total"] == pytest.approx(3.0)


def test_a_cost_without_currency_lands_in_unknown_not_usd():
    cost = ledger.summarize([_run(1.0, {"cost": 1.5})])["totals"]["cost"]
    assert ledger.UNKNOWN_CURRENCY in cost
    assert "USD" not in cost


def test_unknown_money_stays_out_of_the_cny_total():
    """The whole point of the sentinel: an unnamed amount is not yuan."""
    rows = [
        _run(1.0, {"cost": 2.0, "currency": "CNY"}),
        _run(2.0, {"cost": 9.0}),
    ]
    totals = ledger.summarize(rows)["totals"]
    assert totals["cost"]["CNY"]["total"] == pytest.approx(2.0)
    assert totals["cost"][ledger.UNKNOWN_CURRENCY]["total"] == pytest.approx(9.0)
    assert ledger.spent(rows, "CNY") == pytest.approx(2.0)


def test_spent_ignores_other_currencies_and_unknown():
    rows = [
        _run(1.0, {"cost": 1.5, "currency": "CNY"}),
        _run(2.0, {"cost": 9.0, "currency": "USD"}),
        _run(3.0, {"cost": 7.0}),
        _run(4.0, {"input_tokens": 5}),
    ]
    assert ledger.spent(rows, "CNY") == pytest.approx(1.5)


def test_spent_can_be_told_how_to_read_an_unrecorded_currency():
    """The daily ceiling counts unlabelled spend as its own currency; the
    board does not. Both are explicit decisions, so both are testable."""
    rows = [_run(1.0, {"cost": 1.5, "currency": "CNY"}), _run(2.0, {"cost": 7.0})]
    assert ledger.spent(rows, "CNY") == pytest.approx(1.5)
    assert ledger.spent(rows, "CNY", unrecorded_is="CNY") == pytest.approx(8.5)
    # The override is a reading, not a rewrite: the report still files the
    # unnamed amount under the sentinel.
    assert ledger.currency_totals(rows) == {"CNY": 1.5, ledger.UNKNOWN_CURRENCY: 7.0}


def test_amount_in_is_spent_for_one_row():
    """The per-run form of `spent`, used by recovery's estimate and budgets."""
    assert ledger.amount_in({"cost": 1.5, "currency": "cny"}, "CNY") == pytest.approx(1.5)
    assert ledger.amount_in({"cost": 1.5, "currency": "USD"}, "CNY") is None
    assert ledger.amount_in({"cost": 1.5}, "CNY") is None
    assert ledger.amount_in({"cost": 1.5}, "CNY", unrecorded_is="CNY") == pytest.approx(1.5)
    assert ledger.amount_in({"input_tokens": 3}, "CNY") is None
    assert ledger.amount_in("not json", "CNY") is None
    # A bool is not a measurement (`isinstance(True, int)` is true).
    assert ledger.amount_in({"cost": True, "currency": "CNY"}, "CNY") is None


def test_currency_totals_keeps_each_currency_separate():
    rows = [
        _run(1.0, {"cost": 1.0, "currency": "CNY"}),
        _run(2.0, {"cost": 2.0, "currency": "usd"}),
        _run(3.0, {"cost": 3.0}),
    ]
    assert ledger.currency_totals(rows) == {"CNY": 1.0, "USD": 2.0, ledger.UNKNOWN_CURRENCY: 3.0}


# ---- the per-report accumulator --------------------------------------
#
# `Money` is what the six display sites add a run into. It is tested here once
# so that none of them has to re-test the arithmetic, only its own sentence.


def test_money_keeps_currencies_apart_and_groups_unlabeled_by_provider():
    money = ledger.Money()
    money.add({"cost": 1.0, "currency": "CNY"}, "relay")
    money.add({"cost": 0.5, "currency": "usd"}, "relay")
    money.add({"cost": 0.25}, "relay-old")
    money.add({"cost": 0.75}, "claude")
    money.add({"input_tokens": 10}, "relay")  # unpriced: belongs in no bucket

    assert money.costs == {
        "CNY": {"total": 1.0, "runs": 1},
        "USD": {"total": 0.5, "runs": 1},
    }
    assert money.unlabeled == [
        {"provider": "claude", "total": 0.75, "runs": 1},
        {"provider": "relay-old", "total": 0.25, "runs": 1},
    ]
    assert money.amount("CNY") == 1.0
    assert money.amount("USD") == 0.5
    assert money.currencies() == ["CNY", "USD"]


def test_money_treats_the_sentinel_as_unlabeled_not_as_a_currency():
    """`bench`'s results.json writes "-"; it must not become a bucket called "-"."""
    money = ledger.Money()
    money.add({"cost": 3.0, "currency": ledger.UNKNOWN_CURRENCY}, "relay-old")
    assert money.costs == {}
    assert money.unlabeled == [{"provider": "relay-old", "total": 3.0, "runs": 1}]
    assert not money.empty()
    assert ledger.Money().empty()


def test_currency_label_is_never_the_sentinel_or_a_blank():
    assert ledger.currency_label("CNY") == "CNY"
    assert ledger.currency_label("cny") == "CNY"
    assert ledger.currency_label(ledger.UNKNOWN_CURRENCY) == ledger.UNKNOWN_LABEL
    assert ledger.currency_label("") == ledger.UNKNOWN_LABEL
    assert ledger.currency_label(None) == ledger.UNKNOWN_LABEL


def test_unlabeled_line_names_one_source_and_prints_no_symbol():
    line = ledger.unlabeled_line({"provider": "relay-old", "runs": 2}, "1.0000")
    assert line == "未标注币种（来源 relay-old）  2 次  1.0000"
    assert "¥" not in line and "$" not in line
    assert ledger.unlabeled_line({"runs": 1}, "0.5000") == (
        "未标注币种（来源未记录）  1 次  0.5000"
    )


# ---- the board's per-agent money page --------------------------------


def test_by_agent_counts_runs_and_money_separately():
    rows = [
        _run(1.0, {"cost": 0.5, "currency": "CNY", "cost_basis": "calibrated"}, agent="claude"),
        _run(2.0, {"cost": 0.25, "currency": "CNY", "cost_basis": "calibrated"}, agent="claude"),
        _run(3.0, None, agent="claude"),
        _run(4.0, {"cost": 0.1, "currency": "CNY", "cost_basis": "metered"}, agent="codex"),
    ]
    page = ledger.by_agent(rows)
    assert page["runs"] == 4
    assert page["priced"] == 3
    assert page["unpriced"] == 1
    assert page["totals"] == {"CNY": 0.85}
    assert page["basis"] == {"calibrated": 2, "metered": 1}
    by_name = {b["agent"]: b for b in page["by_agent"]}
    assert by_name["claude"]["runs"] == 3
    assert by_name["claude"]["priced"] == 2
    assert by_name["claude"]["costs"] == {"CNY": 0.75}
    # A priced agent sorts ahead of one that spent less.
    assert [b["agent"] for b in page["by_agent"]] == ["claude", "codex"]


def test_by_agent_lists_distinct_routes_and_keeps_currencies_apart():
    rows = [
        _run(1.0, {"cost": 1.0, "currency": "CNY"}, agent="claude", route="claude"),
        _run(2.0, {"cost": 1.0, "currency": "USD"}, agent="claude", route="minimax"),
        _run(3.0, {"cost": 1.0, "currency": "CNY"}, agent="claude", route="claude"),
    ]
    bucket = ledger.by_agent(rows)["by_agent"][0]
    assert bucket["routes"] == ["minimax"]
    assert bucket["costs"] == {"CNY": 2.0, "USD": 1.0}


def test_by_agent_does_not_default_a_missing_currency_to_usd():
    page = ledger.by_agent([_run(1.0, {"cost": 1.0})])
    assert page["totals"] == {ledger.UNKNOWN_CURRENCY: 1.0}


def test_cache_hit_rate_is_the_stated_formula_and_none_without_a_denominator():
    hit = _run(1.0, {"input_tokens": 100, "cache_read": 300, "cache_write": 100})
    assert ledger.summarize([hit])["totals"]["cache_hit_rate"] == pytest.approx(0.6)
    assert ledger.summarize([_run(1.0, {"output_tokens": 5})])["totals"]["cache_hit_rate"] is None


def test_groups_are_ordered_by_runs_then_tokens_then_key():
    rows = [
        _run(1.0, {"input_tokens": 10}, route="small"),
        _run(2.0, {"input_tokens": 10}, route="big"),
        _run(3.0, {"input_tokens": 5}, route="big"),
    ]
    assert [g["key"] for g in ledger.summarize(rows, "route")["groups"]] == ["big", "small"]
