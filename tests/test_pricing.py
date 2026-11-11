"""Pricing: published lists, measured calibrations, and an honest "unknown".

Every timestamp here is built as an explicit +08:00 datetime and converted to
epoch seconds. Nothing reads the machine clock or the machine timezone, so
this file passes in Berlin on a Sunday exactly as it does in Shanghai.
"""

from __future__ import annotations

import datetime

import pytest

from sodus import config, ledger, pricing, providers

BEIJING = datetime.timezone(datetime.timedelta(hours=8))


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    """Epoch seconds for a Beijing wall-clock time."""
    return datetime.datetime(year, month, day, hour, minute, tzinfo=BEIJING).timestamp()


# 2026-10-26 is a Monday; 2026-10-25 a Sunday; 2026-10-31 a Saturday.
MONDAY_PEAK = at(2026, 10, 26, 10)
MONDAY_LUNCH = at(2026, 10, 26, 13)
MONDAY_BEFORE = at(2026, 10, 26, 8, 59)
SUNDAY_NOON = at(2026, 10, 25, 12)
SATURDAY_PEAK_HOURS = at(2026, 10, 31, 10)


def _tokens(**over) -> dict:
    base = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0}
    base.update(over)
    return base


def test_monday_mid_morning_is_peak():
    assert pricing.is_peak(MONDAY_PEAK) is True


def test_the_lunch_dip_is_idle():
    assert pricing.is_peak(MONDAY_LUNCH) is False


def test_the_window_edges_are_half_open():
    assert pricing.is_peak(at(2026, 10, 26, 9, 0)) is True
    assert pricing.is_peak(at(2026, 10, 26, 11, 59)) is True
    assert pricing.is_peak(at(2026, 10, 26, 12, 0)) is False
    assert pricing.is_peak(at(2026, 10, 26, 14, 0)) is True
    assert pricing.is_peak(at(2026, 10, 26, 18, 0)) is False
    assert pricing.is_peak(MONDAY_BEFORE) is False


def test_the_weekend_is_idle_even_during_peak_hours():
    assert pricing.is_peak(SATURDAY_PEAK_HOURS) is False
    assert pricing.is_peak(SUNDAY_NOON) is False


def test_deepseek_flash_peak_prices_the_published_rates():
    # One million tokens of each kind, so the rate is the amount.
    tokens = _tokens(input_tokens=1_000_000, output_tokens=1_000_000, cache_read=1_000_000)
    amount, currency, basis, detail = pricing.price("deepseek-flash", "deepseek", tokens, MONDAY_PEAK)
    assert currency == "CNY"
    assert basis == "list_price"
    assert detail["tier"] == "peak"
    # 2 (miss) + 8 (output) + 0.04 (cache hit)
    assert amount == pytest.approx(10.04)


def test_deepseek_flash_idle_is_half_the_peak_price():
    tokens = _tokens(input_tokens=1_000_000, output_tokens=1_000_000, cache_read=1_000_000)
    peak = pricing.price("deepseek-flash", "deepseek", tokens, MONDAY_PEAK)[0]
    idle = pricing.price("deepseek-flash", "deepseek", tokens, MONDAY_LUNCH)[0]
    assert idle == pytest.approx(5.02)
    assert idle == pytest.approx(peak / 2)


def test_the_same_run_prices_differently_on_the_weekend():
    tokens = _tokens(output_tokens=2_000_000)
    saturday = pricing.price("deepseek-flash", "deepseek", tokens, SATURDAY_PEAK_HOURS)
    monday = pricing.price("deepseek-flash", "deepseek", tokens, MONDAY_PEAK)
    assert saturday[0] == pytest.approx(8.0)   # 2M x 4
    assert monday[0] == pytest.approx(16.0)    # 2M x 8


def test_cache_writes_are_billed_as_cache_misses():
    """DeepSeek has no separate write price; the recorded fields are disjoint."""
    tokens = _tokens(input_tokens=500_000, cache_write=500_000)
    amount = pricing.price("deepseek-flash", "deepseek", tokens, MONDAY_PEAK)[0]
    assert amount == pytest.approx(2.0)  # 1M x 2, peak miss rate


def test_a_vendor_namespaced_slug_names_the_same_model():
    tokens = _tokens(output_tokens=1_000_000)
    bare = pricing.price("deepseek-flash", "deepseek", tokens, MONDAY_PEAK)
    namespaced = pricing.price("deepseek/deepseek-flash", "deepseek", tokens, MONDAY_PEAK)
    assert bare[:3] == namespaced[:3]


def test_an_unknown_model_returns_no_number_at_all():
    """Not 0.0, not USD: a price nobody knows is unknown, and priced() says so."""
    amount, currency, basis, detail = pricing.price(
        "gpt-6.1-sol", None, _tokens(input_tokens=1_000_000), MONDAY_PEAK
    )
    assert amount is None
    assert currency is None
    assert basis is None
    assert detail["reason"] == "unknown-model"


def test_a_known_provider_without_a_price_for_the_model_is_also_unknown():
    endpoint = providers.Provider(name="relay-openai", protocol=providers.OPENAI, meter="relay")
    amount, currency, basis, detail = pricing.price("gpt-6.1-sol", endpoint, _tokens(output_tokens=5))
    assert (amount, currency, basis) == (None, None, None)
    assert detail["reason"] == "no-price-for-model"


def test_a_relay_calibration_prices_in_cny():
    endpoint = providers.Provider(name="relay", protocol=providers.ANTHROPIC, meter="relay")
    tokens = _tokens(input_tokens=10, output_tokens=100, cache_read=1000, cache_write=2000)
    amount, currency, basis, detail = pricing.price(
        "claude-opus-5-5[1M]", endpoint, tokens, MONDAY_PEAK
    )
    assert currency == "CNY"
    assert basis == "calibrated"
    assert detail["source"] == "calibration"
    calibration = providers.calibration_for("claude-opus-5-5[1M]")
    assert amount == pytest.approx(
        round(calibration.cost(tokens) * config.cost_rate("relay_unit"), 6)
    )


def test_the_calibration_path_comes_from_config_when_one_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    (tmp_path / "config.json").write_text(
        '{"cost_models": {"claude-opus-5-5[1M]": {"per_output": 1e-05, "per_call": 0.0}},'
        ' "cost_rates": {"relay_unit": 2.0}}',
        encoding="utf-8",
    )
    endpoint = providers.Provider(name="relay", protocol=providers.ANTHROPIC, meter="relay")
    amount, currency, basis, _detail = pricing.price(
        "claude-opus-5-5[1M]", endpoint, _tokens(output_tokens=1000), MONDAY_PEAK
    )
    # 1000 x 1e-05 = 0.01 relay units, x 2 CNY per unit
    assert amount == pytest.approx(0.02)
    assert currency == "CNY"
    assert basis == "calibrated"


def test_a_relay_model_without_a_calibration_is_not_priced_from_thin_air():
    endpoint = providers.Provider(name="relay", protocol=providers.ANTHROPIC, meter="relay")
    amount, currency, basis, _detail = pricing.price(
        "claude-sonnet-5-5[1M]", endpoint, _tokens(output_tokens=100), MONDAY_PEAK
    )
    assert (amount, currency, basis) == (None, None, None)


def test_minimax_prices_from_the_provider_list():
    endpoint = providers.load()["minimax"]
    # 1M input + 1M output + 1M cache read at 1 / 4 / 0.1 CNY
    tokens = _tokens(input_tokens=1_000_000, output_tokens=1_000_000, cache_read=1_000_000)
    amount, currency, basis, detail = pricing.price("MiniMax-M3.1-Flash", endpoint, tokens, MONDAY_PEAK)
    assert currency == "CNY"
    assert basis == "list_price"
    assert detail["source"] == "provider_pricing"
    assert amount == pytest.approx(5.1)


def test_tokens_may_be_a_parsed_usage_object():
    usage = ledger.parse({"input_tokens": 1_000_000, "output_tokens": 0})
    amount = pricing.price("deepseek-flash", "deepseek", usage, MONDAY_PEAK)[0]
    assert amount == pytest.approx(2.0)
