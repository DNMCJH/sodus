"""Billing meters: the provider's number, read as a delta around one run."""

from __future__ import annotations

import json

import pytest

from sodus import config, meter, providers


def _usage(value: float) -> meter.Reading:
    return meter.Reading(value=value, unit="relay_unit", kind="usage")


def _balance(value: float) -> meter.Reading:
    return meter.Reading(value=value, unit="cny", kind="balance")


def _relay(**over) -> providers.Provider:
    base = dict(
        name="relay",
        protocol=providers.ANTHROPIC,
        base_url="https://relay.test",
        key_secret="RELAY_API_KEY",
        meter="relay",
    )
    base.update(over)
    return providers.Provider(**base)


def test_usage_meter_delta_grows():
    assert meter.delta(_usage(100.0), _usage(100.25)) == pytest.approx(0.25)


def test_balance_meter_delta_shrinks():
    assert meter.delta(_balance(36.54), _balance(36.29)) == pytest.approx(0.25)


def test_negative_or_mixed_deltas_are_not_guessed():
    assert meter.delta(_balance(36.0), _balance(86.0)) is None  # topped up mid-run
    assert meter.delta(_usage(100.0), _balance(90.0)) is None  # different meters


def test_an_endpoint_without_a_meter_returns_none():
    assert meter.read(providers.Provider(name="x", protocol=providers.OPENAI), {}) is None


def test_relay_reading_is_cents_of_site_balance(monkeypatch):
    monkeypatch.setattr(meter, "_get_json", lambda url, token, timeout=15.0: {"total_usage": 249486.0})
    reading = meter.read(_relay(), {"RELAY_API_KEY": "sk"})
    assert reading == meter.Reading(value=2494.86, unit="relay_unit", kind="usage")


def test_unreachable_meter_fails_open(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("network down")

    monkeypatch.setattr(meter, "_get_json", boom)
    assert meter.read(_relay(), {"RELAY_API_KEY": "sk"}) is None


def test_a_meter_without_a_key_is_not_read(monkeypatch):
    called: list = []
    monkeypatch.setattr(meter, "_get_json", lambda *a, **k: called.append(1) or {})
    assert meter.read(_relay(), {}) is None
    assert called == []


def test_both_faces_of_one_relay_read_the_same_meter(monkeypatch):
    """Billing hangs off the account root, not the protocol root."""
    seen: list[str] = []

    def capture(url, token, timeout=15.0):
        seen.append(url)
        return {"total_usage": 100.0}

    monkeypatch.setattr(meter, "_get_json", capture)
    anthropic = _relay(base_url="https://relay.test")
    openai = _relay(base_url="https://relay.test/v1")
    meter.read(anthropic, {"RELAY_API_KEY": "sk"})
    meter.read(openai, {"RELAY_API_KEY": "sk"})
    assert seen[0] == seen[1] == "https://relay.test/v1/dashboard/billing/usage"


def test_cost_rate_is_configurable_because_the_api_never_states_it(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    assert config.cost_rate("relay_unit") == 1.0
    (tmp_path / "config.json").write_text(
        json.dumps({"cost_rates": {"relay_unit": 7.2}}), encoding="utf-8"
    )
    assert config.cost_rate("relay_unit") == pytest.approx(7.2)
    assert config.cost_rate("cny") == 1.0


def test_gateway_cost_sums_only_this_session(monkeypatch):
    body = {
        "rows": [
            {"session": "session-a", "cost": 0.01, "priced": True},
            {"session": "session-b", "cost": 0.99, "priced": True},
            {"session": "session-a", "cost": 0.02, "priced": True},
            {"session": "session-a", "cost": 0.50, "priced": False},  # no price table
        ]
    }
    monkeypatch.setattr(meter, "_get_json", lambda *a, **k: body)
    cost, count = meter.request_cost("session-a")
    assert cost == pytest.approx(0.03)
    assert count == 2


def test_gateway_without_matching_rows_is_none(monkeypatch):
    monkeypatch.setattr(meter, "_get_json", lambda *a, **k: {"rows": []})
    assert meter.request_cost("session-a") is None


def test_gateway_down_is_none_not_an_error(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("gateway down")

    monkeypatch.setattr(meter, "_get_json", boom)
    assert meter.request_cost("session-a") is None


def test_gateway_is_not_asked_without_a_session_id(monkeypatch):
    calls: list = []
    monkeypatch.setattr(meter, "_get_json", lambda *a, **k: calls.append(1) or {"rows": []})
    assert meter.request_cost("") is None
    assert calls == []


def test_gateway_currency_can_be_overridden(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.delenv("SODUS_MAGPIE_CURRENCY", raising=False)
    # Verified against `magpie usage`, which prints ≈$; a wrong unit is a 7x error.
    assert config.gateway_currency() == "USD"
    monkeypatch.setenv("SODUS_MAGPIE_CURRENCY", "cny")
    assert config.gateway_currency() == "CNY"
