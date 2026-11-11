"""The price list: one function that answers "what did this cost".

`runner._price` and `llm._price` each carried their own ladder of
price sources, and they already disagreed in the small print: the runner keeps
the account meter's reading as `meter_observed` and prefers a calibration,
the chat path has no meter fallback and no `meter_observed`. Neither could be
called from a report, because both need a live provider, a live meter, and a
network. So nothing could *recompute* a price — every report had to trust
whatever number was written into `usage_json` at run time.

This module is the recomputable half. It never touches the network and never
writes anything; it answers from published prices and measured calibrations::

    price(model, provider, tokens, at) -> (amount, currency, basis, detail)

Three sources, in the same order of trustworthiness the runner uses:

    calibrated   a measured per-token model for this endpoint's model
    list_price   a published price list (DeepSeek's own, or the provider's)
    None         it cannot be computed, and says so

`None` is the important return. A model whose price nobody knows is *unknown*,
not free: returning 0.0 would make an expensive run look like a rounding error,
and the caller can print "unpriced" honestly instead.

Peak and idle hours are read from the `at` argument (epoch seconds) and
converted to Beijing time with a fixed +08:00 offset — never from the machine's
local timezone. A test in Berlin and a daemon in Shanghai must price the same
run the same way, and a test that reads the wall clock is a test that fails on
a Saturday.
"""

from __future__ import annotations

import datetime
import time
from typing import Any, Mapping

from . import config, ledger, providers

#: Beijing has no daylight saving: the offset is a fact, not a calendar.
BEIJING = datetime.timezone(datetime.timedelta(hours=8))

#: Peak windows, Beijing time, as [start, end). 12:00-14:00 is the lunch dip
#: and is idle, like the evenings and the weekend.
PEAK_WINDOWS = ((9, 12), (14, 18))

#: CNY per million tokens, DeepSeek's published list. Peak is the published
#: number; idle is half of it, which is how DeepSeek bills off-peak.
DEEPSEEK_FLASH = {
    "cache_read_peak": 0.04,
    "cache_read_idle": 0.02,
    "cache_miss_peak": 2.0,
    "cache_miss_idle": 1.0,
    "output_peak": 8.0,
    "output_idle": 4.0,
}

#: Model slug -> the price list that applies. Data, not a chain of ifs: a new
#: vendor means one entry here, and the basename is what matches, so
#: `deepseek/deepseek-flash` and `deepseek-flash` are the same model.
MODELS = {"deepseek-flash": DEEPSEEK_FLASH}

#: Basis words, matching what `runner._price` writes into `cost_basis`.
CALIBRATED = "calibrated"
LIST_PRICE = "list_price"


def _basename(model: str) -> str:
    """The model name without a vendor namespace (`vendor/model`)."""
    return str(model or "").strip().split("/")[-1]


def is_peak(at: float | None = None) -> bool:
    """Is `at` inside DeepSeek's peak window, in Beijing time?

    Monday to Friday, 09:00-12:00 and 14:00-18:00. Everything else — early
    morning, lunch, evening, the whole weekend — is idle.
    """
    moment = time.time() if at is None else float(at)
    local = datetime.datetime.fromtimestamp(moment, tz=BEIJING)
    if local.weekday() >= 5:  # Saturday, Sunday
        return False
    for start, end in PEAK_WINDOWS:
        if start <= local.hour < end:
            return True
    return False


def _rates(table: Mapping[str, float], at: float | None) -> tuple[dict, str]:
    """The active per-million rates and which tier they are.

    Half of peak is how the idle tier is defined, so it is derived rather than
    typed twice: a table that lists only peaks cannot disagree with itself.
    """
    peak = is_peak(at)
    rates = {
        key: (value if peak else table[key.replace("_peak", "_idle")])
        for key, value in table.items()
        if key.endswith("_peak")
    }
    return rates, "peak" if peak else "idle"


def _token_dict(tokens: Any) -> dict[str, int]:
    """The `usage` dict shape `Pricing.cost` / `Calibration.cost` expect.

    Accepts a `ledger.Usage`, a `ledger.Tokens`, or a plain mapping, so the
    caller can pass whatever it already has. Absent fields become 0 here and
    only here: these are per-token multiplications on a price list, where the
    alternative to zero is "do not bill this term at all", and the price list
    has no term for a token that was not used.
    """
    if isinstance(tokens, ledger.Usage):
        counted = tokens.tokens.as_report()
    elif isinstance(tokens, ledger.Tokens):
        counted = tokens.as_report()
    elif isinstance(tokens, Mapping):
        usage = ledger.parse({"input_tokens": tokens.get("input_tokens"),
                              "output_tokens": tokens.get("output_tokens"),
                              "cache_read": tokens.get("cache_read"),
                              "cache_write": tokens.get("cache_write")})
        counted = usage.tokens.as_report()
    else:
        raise TypeError("tokens must be a Usage, Tokens or mapping")
    return {
        "input_tokens": counted["prompt"],
        "output_tokens": counted["completion"],
        "cache_read": counted["cache_read"],
        "cache_write": counted["cache_write"],
    }


def _as_provider(provider: Any) -> providers.Provider | None:
    """A `Provider` from an object, a name, or nothing."""
    if provider is None:
        return None
    if isinstance(provider, providers.Provider):
        return provider
    if isinstance(provider, str):
        return providers.get(provider)
    return None


def price(
    model: str,
    provider: Any = None,
    tokens: Any = None,
    at: float | None = None,
) -> tuple[float | None, str | None, str | None, dict]:
    """What `tokens` on `model` cost, as (amount, currency, basis, detail).

    `amount`/`currency`/`basis` are all None when the price is not known —
    there is no currency to name a number that does not exist. `detail` always
    says why, and carries the rates used, so a surprising total can be checked
    without a debugger.
    """
    usage = _token_dict(tokens)
    endpoint = _as_provider(provider)
    detail: dict[str, Any] = {
        "model": str(model or ""),
        "provider": endpoint.name if endpoint else (provider if isinstance(provider, str) else ""),
    }

    # 1. A measured calibration. Only the relay this account uses has one, and
    #    the relay's unit is converted to CNY by `config.cost_rate`, which is
    #    an account fact the API never states. `provider=None` trusts the
    #    model's own table: that is what an offline report or a test holds.
    if endpoint is None or endpoint.meter == "relay":
        calibration = providers.calibration_for(str(model or ""))
        if calibration is not None:
            rate = config.cost_rate("relay_unit")
            amount = round(calibration.cost(usage) * rate, 6)
            detail.update(
                {
                    "source": "calibration",
                    "measured": calibration.measured,
                    "relay_unit_cny": rate,
                    "coefficients": calibration.as_json(),
                }
            )
            return amount, "CNY", CALIBRATED, detail

    # 2. A published price list for this model. DeepSeek's own list is peak
    #    and idle; cache writes are billed as cache misses because DeepSeek
    #    has no separate write price and the recorded fields are disjoint.
    rates = MODELS.get(_basename(model))
    if rates is not None:
        active, tier = _rates(rates, at)
        amount = round(
            (
                usage["cache_read"] * active["cache_read_peak"]
                + (usage["input_tokens"] + usage["cache_write"]) * active["cache_miss_peak"]
                + usage["output_tokens"] * active["output_peak"]
            )
            / 1_000_000,
            6,
        )
        detail.update(
            {
                "source": "deepseek_official",
                "tier": tier,
                "rates_per_mtok": active,
                "beijing_time": datetime.datetime.fromtimestamp(
                    time.time() if at is None else float(at), tz=BEIJING
                ).isoformat(),
            }
        )
        return amount, "CNY", LIST_PRICE, detail

    # 3. The provider's own price list (minimax ships one as data).
    if endpoint is not None and endpoint.pricing is not None:
        pricing = endpoint.pricing
        detail.update(
            {
                "source": "provider_pricing",
                "list": {
                    "input": pricing.input,
                    "output": pricing.output,
                    "cache_read": pricing.cache_read,
                    "currency": pricing.currency,
                },
            }
        )
        return round(pricing.cost(usage), 6), pricing.currency, LIST_PRICE, detail

    # 4. Nothing known. Not zero, not USD: unknown, with the reason stated.
    detail["reason"] = "unknown-model" if endpoint is None else "no-price-for-model"
    return None, None, None, detail
