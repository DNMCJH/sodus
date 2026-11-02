"""Cost: a measured per-token model beats an account-wide balance delta."""

from __future__ import annotations

import argparse
import json
import time

import pytest

from nodus import agents, config, db, meter, providers, runner
from nodus.cli.commands import system


def test_the_measured_model_reproduces_a_real_run():
    """Coefficients come from six controlled runs; this is a seventh."""
    cal = providers.calibration_for("claude-opus-5-5[1M]")
    assert cal is not None
    usage = {"output_tokens": 486, "cache_read": 41103, "cache_write": 12447}
    # measured 0.059712 on the relay; the model must land inside 1%
    assert abs(cal.cost(usage) - 0.059712) / 0.059712 < 0.01


def test_a_model_without_a_calibration_stays_uncalibrated():
    assert providers.calibration_for("claude-sonnet-5-5[1M]") is None


def test_a_hand_written_calibration_wins_over_the_shipped_one(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "cost_models": {
                    "claude-opus-5-5[1M]": {
                        "per_output": 1e-05,
                        "per_cache_read": 0.0,
                        "per_cache_write": 0.0,
                        "per_call": 0.0,
                        "measured": "hand",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    cal = providers.calibration_for("claude-opus-5-5[1M]")
    assert cal.measured == "hand"
    assert cal.cost({"output_tokens": 1000, "cache_read": 999999, "cache_write": 0}) == pytest.approx(0.01)


def _attempt(model: str) -> dict:
    return {
        "model": model,
        "usage": {"input_tokens": 10, "output_tokens": 100, "cache_read": 1000, "cache_write": 2000},
        "session_ref": {},
        "started_at": 1.0,
        "ended_at": 2.0,
        "errors": [],
        "noise": [],
        "tool_calls": 1,
        "ok": True,
    }


def _relay() -> providers.Provider:
    return providers.Provider(name="relay", protocol=providers.ANTHROPIC, meter="relay")


def _claude() -> agents.AgentSpec:
    return agents.AgentSpec(name="claude", vendor="claude", command=("x",), accepts=(providers.ANTHROPIC,))


def test_a_calibrated_model_prices_the_run_instead_of_the_shared_meter(monkeypatch):
    before = meter.Reading(100.0, "relay_unit", "usage")
    monkeypatch.setattr(meter, "read", lambda *a, **k: meter.Reading(100.5, "relay_unit", "usage"))
    attempt = _attempt("claude-opus-5-5[1M]")

    runner.Runner()._price(attempt, _claude(), _relay(), {}, before)

    usage = attempt["usage"]
    assert usage["cost_basis"] == "calibrated"
    cal = providers.calibration_for("claude-opus-5-5[1M]")
    assert usage["cost"] == pytest.approx(cal.cost(usage))
    # the account number is kept, but only as a drift reference
    assert usage["meter_observed"] == pytest.approx(0.5)


def test_an_uncalibrated_model_falls_back_to_the_meter(monkeypatch):
    before = meter.Reading(100.0, "relay_unit", "usage")
    monkeypatch.setattr(meter, "read", lambda *a, **k: meter.Reading(100.25, "relay_unit", "usage"))
    attempt = _attempt("claude-sonnet-5-5[1M]")

    runner.Runner()._price(attempt, _claude(), _relay(), {}, before)

    usage = attempt["usage"]
    assert usage["cost_basis"] == "metered"
    assert usage["cost"] == pytest.approx(0.25)
    assert "meter_observed" not in usage


# ---- the drift block: money the bill side cannot vouch for --------------


def _drift_run(task_id: str, provider: str, usage: dict) -> None:
    run = db.create_run(task_id, "claude", "D:\\w", provider=provider)
    db.update_run(
        run["id"],
        status="completed",
        started_at=time.time() - 5,
        ended_at=time.time(),
        usage_json=json.dumps(usage),
    )


def test_the_drift_block_compares_only_yuan_and_names_the_rest(tmp_path, monkeypatch, capsys):
    """The bill side is CNY, so only a CNY calibration can be compared with it.

    The old block added every calibrated amount into one float and called the
    sum yuan; a dollar row and a row with no currency both landed in it.
    """
    monkeypatch.setattr(config, "HOME", tmp_path)
    task = db.create_task("drift")
    _drift_run(task["id"], "relay", {
        "cost": 0.5, "currency": "CNY", "cost_basis": "calibrated", "meter_observed": 0.6,
    })
    _drift_run(task["id"], "relay-usd", {
        "cost": 0.25, "currency": "USD", "cost_basis": "calibrated", "meter_observed": 0.3,
    })
    _drift_run(task["id"], "relay-old", {
        "cost": 0.75, "cost_basis": "calibrated", "meter_observed": 0.9,
    })

    assert system.cost(argparse.Namespace(runs=10)) == 0

    out = capsys.readouterr().out
    assert "最近 1 个已标定的 run（CNY）：标定合计 ¥0.5000，账单合计 ¥0.6000（+20.0%）" in out
    assert "最近 1 个已标定的 run（USD）：标定合计 0.2500" in out
    assert "未标注币种（来源 relay-old）  1 次  标定合计 0.7500" in out
    # No line claims the three are one sum: 1.5 / 1.8 would be that mistake.
    assert "1.5000" not in out and "1.8000" not in out


def test_the_drift_block_is_unchanged_when_every_row_is_yuan(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "HOME", tmp_path)
    task = db.create_task("drift")
    _drift_run(task["id"], "relay", {
        "cost": 0.5, "currency": "CNY", "cost_basis": "calibrated", "meter_observed": 0.6,
    })

    assert system.cost(argparse.Namespace(runs=10)) == 0

    out = capsys.readouterr().out
    assert "最近 1 个已标定的 run：标定合计 ¥0.5000，账单合计 ¥0.6000（+20.0%）" in out
    assert "未标注币种" not in out
