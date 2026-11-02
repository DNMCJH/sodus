"""The ledger: one way to read `run.usage_json`, one way to add it up.

Before this module, four places parsed the same JSON and each decided for
itself what a missing field meant: `usage.py` counted a run with no usage as
"unrecorded", `server._today()` quietly called a missing currency USD,
`policy._usage_cost()` called it "unknown", and `budget.spent()` accepted
anything that parsed as CNY. Four answers to the same question is four chances
to disagree about money, so the question is answered once, here.

Two rules, and the second is the important one:

- A missing field is ``None``, never a default. ``parse()`` says "this run did
  not record a currency", and the caller decides what to print. Inventing
  ``USD`` for a run that recorded ``¥`` is a silent 7x error.
- Amounts are never added across currencies. ``summarize()`` returns one
  bucket per currency plus ``unknown``; the only thing it does to two
  currencies is sort them.

The event shape is the one `runner.py` and `llm.py` already write, and this
module must keep reading it -- a ledger that needs a new format would need a
migration, and the history is the thing worth keeping::

    {"input_tokens": 10, "output_tokens": 20,
     "cache_read": 30, "cache_write": 40,
     "cost": 0.75, "currency": "CNY", "cost_basis": "calibrated",
     "attempts": [{"provider": ..., "cost": ..., "currency": ...}]}
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

#: Cost rows with no recorded currency are kept apart under this key. It is
#: the same sentinel `usage.py` prints, so a report does not need a mapping.
UNKNOWN_CURRENCY = "-"

#: What a human reads for the key above. The key stays a machine token; the
#: six reports that print a currency bucket ask `currency_label`, so the same
#: amount is not worded differently from one command to the next.
UNKNOWN_LABEL = "来源未记录"

#: Printed where the row did not record which provider it went through. Old
#: rows predate the column, and a report must not guess an endpoint.
PROVIDER_UNKNOWN = "来源未记录"

#: What ``summarize()`` accepts for ``group_by``.
GROUP_BYS = ("route", "model", "provider", "agent")

#: What ``bounds()`` / ``summarize()`` accept for ``period``.
PERIODS = ("day", "month", "all")

CACHE_HIT_RATE_FORMULA = "cache_read / (prompt + cache_read + cache_write)"

#: Recorded usage field -> the name the reports use for it. The left side is
#: the on-disk spelling (`input_tokens`), the right side the report's spelling
#: (`prompt`); both are part of the public JSON, so neither is free to change.
TOKEN_FIELDS = (
    ("input_tokens", "prompt"),
    ("output_tokens", "completion"),
    ("cache_read", "cache_read"),
    ("cache_write", "cache_write"),
)


# ---- the typed event ---------------------------------------------------


def number(value: Any) -> bool:
    """A real number, not a bool dressed as one.

    ``isinstance(True, int)`` is true, and a `cost: true` in hand-written JSON
    would otherwise add one yuan to a total nobody measured.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class Tokens:
    """Token counts as recorded. ``None`` means the field was absent.

    A run that recorded only output tokens is missing input, not recording
    zero input: a zero would look like a measurement and dilute every mean
    computed from it. Consumers that must print a number can use ``total()``,
    which treats absent as nothing to add.
    """

    input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None

    def total(self) -> int:
        return sum(
            int(v) for v in (self.input, self.output, self.cache_read, self.cache_write)
            if v is not None
        )

    def prompt_side(self) -> int:
        """The cache-hit-rate denominator: fresh input + reads + writes."""
        return sum(
            int(v) for v in (self.input, self.cache_read, self.cache_write)
            if v is not None
        )

    def as_report(self) -> dict[str, int]:
        """The counters a report adds, absent fields counting as nothing."""
        return {
            "prompt": int(self.input or 0),
            "completion": int(self.output or 0),
            "cache_read": int(self.cache_read or 0),
            "cache_write": int(self.cache_write or 0),
        }

    def recorded(self) -> bool:
        return any(v is not None for v in (self.input, self.output, self.cache_read, self.cache_write))


@dataclass(frozen=True)
class Cost:
    """A priced amount. ``amount`` is None when nothing was priced.

    `basis` names which of `runner._price`'s rungs answered — ``calibrated``,
    ``metered``, ``list_price``, ``reported`` — or "" when the run predates
    the field. `attempts` carries the per-attempt rows a fallback run writes.
    """

    amount: float | None = None
    currency: str | None = None
    basis: str | None = None
    attempts: tuple[dict, ...] = ()

    def priced(self) -> bool:
        return self.amount is not None


@dataclass(frozen=True)
class Usage:
    """One run's recorded usage, typed.

    ``raw`` keeps the parsed object for callers that need a field this model
    does not name yet (`meter`, `meter_observed`, `calibration`). Reading it
    is fine; the named fields are the ones with a single definition.
    """

    tokens: Tokens = field(default_factory=Tokens)
    cost: Cost = field(default_factory=Cost)
    raw: Mapping[str, Any] = field(default_factory=dict)

    def recorded(self) -> bool:
        """Did this run record anything at all?

        An empty object and a missing one are both "unrecorded": a run that
        recorded nothing is counted as a run but contributes no tokens and no
        money, and is never treated as a free run.
        """
        return bool(self.raw)


def _int_or_none(value: Any) -> int | None:
    if not number(value):
        return None
    return int(value)


def parse(value: Mapping[str, Any] | str | None) -> Usage:
    """Parse a usage object, a `usage_json` string, or nothing.

    Never raises on bad input: a run whose JSON was truncated by a crash is
    still a run, and a stats page that dies on one bad row is worse than one
    that shows a gap. Unparseable input is ``Usage()`` — unrecorded.
    """
    if isinstance(value, str):
        if not value:
            return Usage()
        try:
            data = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return Usage()
    else:
        data = value
    if not isinstance(data, Mapping):
        return Usage()
    tokens = Tokens(
        input=_int_or_none(data.get("input_tokens")),
        output=_int_or_none(data.get("output_tokens")),
        cache_read=_int_or_none(data.get("cache_read")),
        cache_write=_int_or_none(data.get("cache_write")),
    )
    cost = Cost(
        amount=float(data["cost"]) if number(data.get("cost")) else None,
        currency=_currency(data.get("currency")),
        basis=str(data["cost_basis"]) if data.get("cost_basis") else None,
        attempts=tuple(a for a in (data.get("attempts") or ()) if isinstance(a, Mapping)),
    )
    return Usage(tokens=tokens, cost=cost, raw=dict(data))


def _currency(value: Any) -> str | None:
    """A currency code, or ``None`` when it was not recorded.

    Deliberately no default: `USD` is a real currency and guessing it for a
    run that recorded nothing turns a yuan total into a seven-times-larger
    dollar one.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text.upper() if text else None


def currency_of(usage: Usage | Mapping[str, Any] | str | None) -> str:
    """The bucket key a cost belongs in; ``UNKNOWN_CURRENCY`` when unnamed."""
    parsed = usage if isinstance(usage, Usage) else parse(usage)
    return parsed.cost.currency or UNKNOWN_CURRENCY


def currency_label(currency: str | None) -> str:
    """The word to print for a currency bucket: the code, or ``来源未记录``.

    Never ``"-"`` and never an empty string. Printing the sentinel as a
    currency is how an unnamed amount starts looking like a real one, and an
    empty one reads as "free".
    """
    text = str(currency or "").strip().upper()
    if not text or text == UNKNOWN_CURRENCY:
        return UNKNOWN_LABEL
    return text


def provider_label(provider: str | None) -> str:
    """``来源 <name>``, or ``来源未记录`` when the row named none.

    Both forms read inside the same parentheses in `unlabeled_line`, so the
    missing case is a phrase rather than "来源 -".
    """
    text = str(provider or "").strip()
    if not text or text == UNKNOWN_CURRENCY:
        return PROVIDER_UNKNOWN
    return f"来源 {text}"


def unlabeled_line(row: Mapping[str, Any], amount: str) -> str:
    """One source's unrecorded-currency money, in the sentence every report uses.

    The caller formats `amount` with its own precision; this function owns the
    words, so a task report, the pet and the cost command cannot call the same
    bucket three different things. No currency symbol is added: there is no
    unit, which is the whole reason the row is here instead of in a total.
    """
    return (
        f"{provider_label(row.get('provider'))}"
        f"  {row.get('runs')} 次  {amount}"
    )


def cost_of(value: Mapping[str, Any] | str | None) -> tuple[float, str, str] | None:
    """(amount, currency, basis) when the row recorded a numeric price.

    ``policy._usage_cost`` wants exactly this, including ``"unknown"`` as the
    currency word in its own output, so the caller passes that in.
    """
    parsed = parse(value)
    if parsed.cost.amount is None:
        return None
    return parsed.cost.amount, currency_of(parsed), parsed.cost.basis or ""


def amount_in(
    value: Mapping[str, Any] | str | None,
    currency: str,
    unrecorded_is: str | None = None,
) -> float | None:
    """This row's amount when it is priced in `currency`, else ``None``.

    One row of what ``spent()`` does for many, for callers that need each run
    separately (a mean, a per-run comparison). A missing currency is not a real
    currency, so it is skipped unless the caller says which bucket it counts
    in: `unrecorded_is` is an argument, never a default, because treating an
    unnamed amount as yuan is a budget decision, not a fact about the row.
    """
    parsed = parse(value)
    if parsed.cost.amount is None:
        return None
    want = currency.upper()
    fallback = unrecorded_is.upper() if unrecorded_is else None
    if (parsed.cost.currency or fallback) != want:
        return None
    return float(parsed.cost.amount)


# ---- windows ----------------------------------------------------------


def _local_midnight(year: int, month: int, day: int) -> float:
    """Midnight in the operator's timezone; ``mktime`` normalises overflows."""
    return time.mktime((year, month, day, 0, 0, 0, 0, 0, -1))


def bounds(period: str = "day", now: float | None = None) -> tuple[float | None, float | None, str]:
    """(start, end, label) for a period, in local time. ``end`` is exclusive.

    ``all`` has no bounds and includes runs that predate a usable timestamp.
    """
    if period not in PERIODS:
        raise ValueError(f"period must be one of {', '.join(PERIODS)}")
    moment = time.time() if now is None else float(now)
    lt = time.localtime(moment)
    if period == "all":
        return None, None, "全部历史"
    if period == "day":
        start = _local_midnight(lt.tm_year, lt.tm_mon, lt.tm_mday)
        end = _local_midnight(lt.tm_year, lt.tm_mon, lt.tm_mday + 1)
        return start, end, time.strftime("%Y-%m-%d", lt)
    start = _local_midnight(lt.tm_year, lt.tm_mon, 1)
    end = _local_midnight(lt.tm_year, lt.tm_mon + 1, 1)
    return start, end, time.strftime("%Y-%m", lt)


def in_window(run: Mapping[str, Any], start: float | None, end: float | None) -> bool:
    """Is this run inside [start, end)? Unbounded sides accept everything.

    A run with no `started_at` is excluded from a bounded window rather than
    placed at epoch zero, where it would show up in "all" and in nothing else
    while pretending to be the oldest run in the workspace.
    """
    if start is None and end is None:
        return True
    started = run.get("started_at")
    if not number(started):
        return False
    if start is not None and started < start:
        return False
    if end is not None and started >= end:
        return False
    return True


# ---- aggregation ------------------------------------------------------


def group_key(run: Mapping[str, Any], group_by: str) -> str:
    """Which bucket this run belongs to.

    Route falls back to the agent for rows written before the column existed;
    an unknown model/provider is named ``"-"`` instead of being folded into a
    real one.
    """
    if group_by == "route":
        return str(run.get("route") or run.get("agent") or "-")
    if group_by == "agent":
        return str(run.get("agent") or "-")
    return str(run.get(group_by) or "-")


def _blank() -> dict:
    return {
        "runs": 0,
        "completed": 0,
        "unrecorded": 0,
        "priced": 0,
        "tokens": {"prompt": 0, "completion": 0, "cache_read": 0, "cache_write": 0, "total": 0},
        "cost": {},
        "basis": {},
    }


def _add(bucket: dict, run: Mapping[str, Any]) -> None:
    """Fold one run into a bucket.

    A priced run with no recorded currency lands under ``UNKNOWN_CURRENCY``
    and never in a real currency's sum — the bucket exists so the money is
    not lost, and stays apart so it is not silently labelled.
    """
    bucket["runs"] += 1
    if run.get("status") == "completed":
        bucket["completed"] += 1
    usage = parse(run.get("usage_json"))
    if not usage.recorded():
        bucket["unrecorded"] += 1
        return
    tokens = bucket["tokens"]
    counted = usage.tokens.as_report()
    for _field, key in TOKEN_FIELDS:
        tokens[key] += counted[key]
    if usage.cost.amount is None:
        return
    currency = usage.cost.currency or UNKNOWN_CURRENCY
    money = bucket["cost"].setdefault(currency, {"total": 0.0, "runs": 0})
    money["total"] += float(usage.cost.amount)
    money["runs"] += 1
    bucket["priced"] += 1
    basis = usage.cost.basis or "none"
    bucket["basis"][basis] = bucket["basis"].get(basis, 0) + 1


def _finish(bucket: dict) -> dict:
    tokens = bucket["tokens"]
    tokens["total"] = (
        tokens["prompt"] + tokens["completion"] + tokens["cache_read"] + tokens["cache_write"]
    )
    denominator = tokens["prompt"] + tokens["cache_read"] + tokens["cache_write"]
    cost = {}
    for currency, money in sorted(bucket["cost"].items()):
        runs = money["runs"]
        cost[currency] = {
            "total": round(money["total"], 6),
            # Mean over runs that recorded a price in this currency only: an
            # unpriced run must not drag the average toward zero.
            "mean": round(money["total"] / runs, 6) if runs else None,
            "runs": runs,
        }
    return {
        "runs": bucket["runs"],
        "completed": bucket["completed"],
        "unrecorded": bucket["unrecorded"],
        "priced": bucket["priced"],
        "tokens": tokens,
        "cost": cost,
        "basis": dict(sorted(bucket["basis"].items())),
        "cache_hit_rate": round(tokens["cache_read"] / denominator, 4) if denominator else None,
    }


def summarize(
    rows: Iterable[Mapping[str, Any]],
    group_by: str = "route",
    start: float | None = None,
    end: float | None = None,
    period: str | None = None,
    now: float | None = None,
) -> dict:
    """Aggregate run rows that the caller already selected.

    ``start``/``end`` filter explicit rows; ``period`` ("day"/"month"/"all")
    is the convenience form that derives the same window from ``now``, for
    callers that hold rows from several days and want one. Passing both is a
    mistake and raises — two window definitions in one call is exactly how a
    report starts disagreeing with itself.

    Groups are ordered by run count, then tokens; the order is part of the
    CLI's output, so it is decided here rather than by dict insertion order.
    """
    if group_by not in GROUP_BYS:
        raise ValueError(f"group_by must be one of {', '.join(GROUP_BYS)}")
    if period is not None:
        if start is not None or end is not None:
            raise ValueError("pass either an explicit window or a period, not both")
        start, end, _label = bounds(period, now)
    buckets: dict[str, dict] = {}
    total = _blank()
    for run in rows:
        if not in_window(run, start, end):
            continue
        _add(buckets.setdefault(group_key(run, group_by), _blank()), run)
        _add(total, run)
    groups = [{"key": key, **_finish(bucket)} for key, bucket in buckets.items()]
    groups.sort(key=lambda group: (-group["runs"], -group["tokens"]["total"], group["key"]))
    return {"groups": groups, "totals": _finish(total)}


# ---- shapes the board's money page needs ------------------------------


def by_agent(rows: Iterable[Mapping[str, Any]]) -> dict:
    """Today's spend per agent, in the shape `server._today` returns.

    That is a different question from ``summarize(group_by="agent")``: the
    board lists which routes an agent was reached through, prints money as a
    plain ``{currency: amount}`` map, and counts money rows separately from
    run rows. It lives here so the board and the CLI read the same ledger,
    and the shape stays exactly what the page and MCP already consume.
    """
    buckets: dict[str, dict] = {}
    totals: dict[str, float] = {}
    basis: dict[str, int] = {}
    runs = 0
    priced = 0
    for row in rows:
        runs += 1
        parsed = parse(row.get("usage_json"))
        agent = str(row.get("agent") or "-")
        bucket = buckets.setdefault(
            agent, {"agent": agent, "costs": {}, "runs": 0, "priced": 0, "routes": []}
        )
        bucket["runs"] += 1
        route = str(row.get("route") or "")
        if route and route != agent and route not in bucket["routes"]:
            bucket["routes"].append(route)
        if parsed.cost.amount is None:
            continue
        priced += 1
        bucket["priced"] += 1
        currency = currency_of(parsed)
        bucket["costs"][currency] = round(
            bucket["costs"].get(currency, 0.0) + float(parsed.cost.amount), 6
        )
        totals[currency] = round(totals.get(currency, 0.0) + float(parsed.cost.amount), 6)
        key = parsed.cost.basis or "none"
        basis[key] = basis.get(key, 0) + 1
    ordered = sorted(buckets.values(), key=lambda b: (-b["priced"], -b["runs"], b["agent"]))
    return {
        "runs": runs,
        "priced": priced,
        "unpriced": runs - priced,
        "totals": totals,
        "by_agent": ordered,
        "basis": basis,
    }


def currency_totals(rows: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Money per currency over a set of runs. Never converts, never mixes."""
    totals: dict[str, float] = {}
    for row in rows:
        parsed = parse(row.get("usage_json"))
        if parsed.cost.amount is None:
            continue
        currency = currency_of(parsed)
        totals[currency] = round(
            totals.get(currency, 0.0) + float(parsed.cost.amount), 6
        )
    return totals


class Money:
    """Money from a set of runs: one bucket per currency, unlabeled kept apart.

    ``costs`` has the same ``{currency: {total, runs}}`` shape `summarize()`
    puts under ``totals["cost"]``. ``unlabeled`` is a list of ``{provider,
    total, runs}`` and deliberately carries no grand total: two providers'
    unnamed amounts are not known to share a unit, so adding them would invent
    the exchange rate this module refuses to guess.

    A caller adds one run at a time and reads the two properties at the end.
    The class is here so that no print site can hold a single "cost" number and
    then have to remember, on its own, whether it was allowed to add it up.
    """

    def __init__(self) -> None:
        self._costs: dict[str, dict] = {}
        self._unlabeled: dict[str, dict] = {}

    def add(
        self,
        usage: Usage | Mapping[str, Any] | str | None,
        provider: str = "",
    ) -> str | None:
        """File one row's price; returns its bucket key, or ``None`` if unpriced.

        `provider` is read only for a row that recorded no currency, because
        that is the one case where the report needs a source to group by. A
        row that named its currency never inherits a provider's unit.
        """
        parsed = usage if isinstance(usage, Usage) else parse(usage)
        amount = parsed.cost.amount
        if amount is None:
            return None
        currency = parsed.cost.currency
        if currency and currency != UNKNOWN_CURRENCY:
            bucket = self._costs.setdefault(currency, {"total": 0.0, "runs": 0})
            bucket["total"] += float(amount)
            bucket["runs"] += 1
            return currency
        key = str(provider or "").strip() or UNKNOWN_CURRENCY
        bucket = self._unlabeled.setdefault(key, {"total": 0.0, "runs": 0})
        bucket["total"] += float(amount)
        bucket["runs"] += 1
        return UNKNOWN_CURRENCY

    @property
    def costs(self) -> dict[str, dict]:
        return {
            currency: {"total": round(money["total"], 6), "runs": money["runs"]}
            for currency, money in sorted(self._costs.items())
        }

    @property
    def unlabeled(self) -> list[dict]:
        return [
            {
                "provider": provider,
                "total": round(money["total"], 6),
                "runs": money["runs"],
            }
            for provider, money in sorted(self._unlabeled.items())
        ]

    def currencies(self) -> list[str]:
        return sorted(self._costs)

    def amount(self, currency: str) -> float:
        return round(self._costs.get(currency.upper(), {}).get("total", 0.0), 6)

    def empty(self) -> bool:
        return not self._costs and not self._unlabeled


def spent(
    rows: Iterable[Mapping[str, Any]],
    currency: str = "CNY",
    unrecorded_is: str | None = None,
) -> float:
    """Sum only rows priced in `currency`.

    A USD run is excluded rather than converted at a rate nobody measured,
    and an unnamed amount is not guessed into a real currency.

    `unrecorded_is` is for one caller: the daily ceiling. It has always
    treated a row with no currency as its own currency, because every row this
    workspace writes is CNY and the alternative is a ceiling that under-reports
    spend and lets a run through the gate. That reading is passed in
    explicitly so it stays a decision at the call site, not a default in the
    ledger -- the board must not inherit it and label an unnamed amount USD.
    """
    want = currency.upper()
    total = 0.0
    for row in rows:
        amount = amount_in(row.get("usage_json"), want, unrecorded_is)
        if amount is not None:
            total += amount
    return total
