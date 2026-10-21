"""Providers: the endpoint a request is actually sent to.

Three things used to live in one object: which CLI runs, which API it talks to,
and what that API charges. They change for different reasons — a CLI upgrade, a
provider outage, a price change — so they are three objects now:

    providers.py   the endpoint: URL, credential, protocol, price
    agents.py      the CLI: executable, argument dialect, stream format
    routes.py      the pairing: one agent, an ordered list of providers

A provider is data. The defaults below are just defaults: `providers.json` in
the Nodus home overrides a built-in by name, or adds a new one, without a code
change. Anything a user can only express by editing Python is a bug here.

The `protocol` field is not decoration: it decides which environment variables
materialise the endpoint, and a route is only valid if every provider in it
speaks the same protocol as the agent it drives.
"""

from __future__ import annotations

import json
import os
import tomllib
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config

#: Protocols an agent can speak. A provider declares one; an agent accepts some.
ANTHROPIC = "anthropic"
OPENAI = "openai"
DEEPSEEK = "deepseek"


@dataclass(frozen=True)
class Pricing:
    """List price per million tokens, in `currency`.

    A vendor that resells another vendor's CLI reports the *host* vendor's
    price. When we know the real list price, we compute instead of believe.
    """

    input: float
    output: float
    cache_read: float = 0.0
    currency: str = "USD"

    def cost(self, usage: dict) -> float:
        return round(
            (
                int(usage.get("input_tokens") or 0) * self.input
                + int(usage.get("output_tokens") or 0) * self.output
                + int(usage.get("cache_read") or 0) * self.cache_read
            )
            / 1_000_000,
            6,
        )


@dataclass(frozen=True)
class Calibration:
    """What one token costs on a specific relay, measured rather than published.

    The relay's own price table (`/api/pricing`) turned out to be shaped
    differently from the classic ratio formula: fitting a linear model to six
    controlled runs with that table produced a negative cache coefficient. So
    these coefficients come from an experiment — vary output, cache read and
    cache write separately, read the account meter around each run, solve.
    Residual on the fit was 0.1%, and a held-out run landed within 0.3%.

    Values are in the relay's balance unit; `config.cost_rate("relay_unit")`
    converts to CNY. A model without a calibration falls back to the meter
    delta, which is account-wide and therefore an upper bound under concurrency.
    """

    model: str
    per_output: float
    per_cache_read: float
    per_cache_write: float
    per_call: float
    measured: str  # date the experiment ran

    def cost(self, usage: dict) -> float:
        return round(
            self.per_call
            + int(usage.get("output_tokens") or 0) * self.per_output
            + int(usage.get("cache_read") or 0) * self.per_cache_read
            + int(usage.get("cache_write") or 0) * self.per_cache_write,
            6,
        )

    def as_json(self) -> dict:
        return {
            "per_output": self.per_output,
            "per_cache_read": self.per_cache_read,
            "per_cache_write": self.per_cache_write,
            "per_call": self.per_call,
            "measured": self.measured,
        }


#: Measured 2026-10-21 on the 9527 relay, claude-opus-5-5[1M].
#: `nodus cost` prints these; `scripts/calibrate_relay.py` re-measures them.
DEFAULT_CALIBRATIONS = {
    "claude-opus-5-5[1M]": Calibration(
        model="claude-opus-5-5[1M]",
        per_output=3.6728e-05,
        per_cache_read=9.6206e-08,
        per_cache_write=2.9769e-06,
        per_call=1.0350e-03,
        measured="2026-10-21",
    ),
}


def calibration_for(model: str) -> Calibration | None:
    """A measured cost model for this endpoint's model, if there is one.

    `config.json` wins: the relay can reprice at any time, and the answer to
    that is a fresh measurement, not a code change.
    """
    raw = (config.load_config().get("cost_models") or {}).get(model)
    if isinstance(raw, dict):
        try:
            return Calibration(
                model=model,
                per_output=float(raw["per_output"]),
                per_cache_read=float(raw.get("per_cache_read") or 0.0),
                per_cache_write=float(raw.get("per_cache_write") or 0.0),
                per_call=float(raw.get("per_call") or 0.0),
                measured=str(raw.get("measured") or "config"),
            )
        except (KeyError, TypeError, ValueError):
            pass
    return DEFAULT_CALIBRATIONS.get(model)


def all_calibrations() -> dict[str, Calibration]:
    out = dict(DEFAULT_CALIBRATIONS)
    for model in (config.load_config().get("cost_models") or {}):
        found = calibration_for(model)
        if found:
            out[model] = found
    return out


@dataclass(frozen=True)
class Provider:
    name: str
    protocol: str
    base_url: str | None = None
    #: name of the secret in secrets.json (never the value)
    key_secret: str | None = None
    #: model used when neither the run nor the route pins one
    model: str | None = None
    #: tier alias -> concrete model, materialised as ANTHROPIC_DEFAULT_*_MODEL
    tiers: dict[str, str] = field(default_factory=dict)
    pricing: Pricing | None = None
    #: which billing meter this endpoint exposes: "relay" / "deepseek" / None
    meter: str | None = None
    #: codex dialects: "responses" or "chat"
    wire_api: str = "responses"
    #: free-form note shown by `nodus provider list`
    note: str = ""

    # ---- serialisation -------------------------------------------------

    def to_json(self) -> dict:
        data = asdict(self)
        if self.pricing is not None:
            data["pricing"] = asdict(self.pricing)
        return {k: v for k, v in data.items() if v not in (None, "", {}, {})}

    @staticmethod
    def from_json(name: str, data: dict) -> "Provider":
        pricing = data.get("pricing")
        return Provider(
            name=name,
            protocol=str(data.get("protocol") or ANTHROPIC),
            base_url=(data.get("base_url") or None),
            key_secret=(data.get("key_secret") or None),
            model=(data.get("model") or None),
            tiers=dict(data.get("tiers") or {}),
            pricing=Pricing(**pricing) if isinstance(pricing, dict) else None,
            meter=(data.get("meter") or None),
            wire_api=str(data.get("wire_api") or "responses"),
            note=str(data.get("note") or ""),
        )


#: The relay this account uses. Kept as a default, not a hard-coded truth:
#: a different account overrides it in providers.json.
RELAY_BASE = os.environ.get("NODUS_RELAY_BASE", "https://9527.codes")
MINIMAX_BASE = os.environ.get("NODUS_MINIMAX_BASE", "https://api.minimax.cn/anthropic")
MINIMAX_MODEL = os.environ.get("NODUS_MINIMAX_MODEL", "MiniMax-M3.1-Flash")


def _builtin() -> dict[str, Provider]:
    return {
        # The relay speaks two protocols because it fronts several vendors.
        # Two names, one key: sharing a credential is the interesting fact and
        # it should be visible, not implied by a shared config file.
        "relay": Provider(
            name="relay",
            protocol=ANTHROPIC,
            base_url=RELAY_BASE,
            key_secret="RELAY_API_KEY",
            model="claude-opus-5-5[1M]",
            tiers={
                "opus": "claude-opus-5-5[1M]",
                "sonnet": "claude-sonnet-5-5[1M]",
                "haiku": "gemini-3.8-flash",
                "fable": "claude-opus-5-5[1M]",
            },
            meter="relay",
            note="9527 中转，站内余额计价",
        ),
        "relay-openai": Provider(
            name="relay-openai",
            protocol=OPENAI,
            base_url=f"{RELAY_BASE}/v1",
            key_secret="RELAY_API_KEY",
            model="gpt-6.1-sol",
            meter="relay",
            note="同一个中转的 OpenAI 协议入口",
        ),
        "minimax": Provider(
            name="minimax",
            protocol=ANTHROPIC,
            base_url=MINIMAX_BASE,
            key_secret="MINIMAX_API_KEY",
            model=MINIMAX_MODEL,
            tiers={
                "opus": MINIMAX_MODEL,
                "sonnet": MINIMAX_MODEL,
                "haiku": MINIMAX_MODEL,
            },
            pricing=Pricing(input=1.0, output=4.0, cache_read=0.1, currency="CNY"),
            note="官方 Anthropic 兼容端点，按价目表计费",
        ),
        "deepseek": Provider(
            name="deepseek",
            protocol=DEEPSEEK,
            base_url=None,  # the DSH CLI has its own default endpoint
            key_secret="DEEPSEEK_API_KEY",
            meter="deepseek",
            note="DSH 的官方端点",
        ),
    }


def path() -> Path:
    return config.HOME / "providers.json"


def load() -> dict[str, Provider]:
    """Built-ins merged with the user's file. Pure: never writes."""
    out = _builtin()
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(raw, dict):
        return out
    removed = set(raw.pop("_removed", []) or [])
    for name, data in raw.items():
        if not isinstance(data, dict):
            continue
        try:
            out[name] = Provider.from_json(name, data)
        except TypeError:
            continue
    for name in removed:
        out.pop(name, None)
    return out


def save(providers: dict[str, Provider]) -> None:
    """Persist only what differs from the built-ins.

    A full dump would freeze today's defaults into the user's file, and the
    next version could never improve them.
    """
    base = _builtin()
    data: dict = {}
    removed: list[str] = []
    for name, provider in providers.items():
        if name in base:
            if provider != base[name]:
                data[name] = provider.to_json()
        else:
            data[name] = provider.to_json()
    for name in base:
        if name not in providers:
            removed.append(name)
    if removed:
        data["_removed"] = removed
    config.ensure_dirs()
    path().write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def get(name: str) -> Provider | None:
    return load().get(name)


def upsert(provider: Provider) -> None:
    table = load()
    table[provider.name] = provider
    save(table)


def remove(name: str) -> bool:
    table = load()
    if name not in table:
        return False
    table.pop(name)
    save(table)
    return True


# ---- requirements ------------------------------------------------------


def missing(provider: Provider, secrets: dict[str, str]) -> list[str]:
    out: list[str] = []
    if provider.key_secret and not secrets.get(provider.key_secret):
        out.append(provider.key_secret)
    return out


def secret_value(provider: Provider, secrets: dict[str, str]) -> str:
    if not provider.key_secret:
        return ""
    return secrets.get(provider.key_secret) or ""


# ---- materialisation ---------------------------------------------------
#
# One place turns a provider into environment variables. Every CLI-specific
# quirk lives here or in agents.py, never in the runner.


def env(provider: Provider, secrets: dict[str, str]) -> dict[str, str]:
    value = secret_value(provider, secrets)
    out: dict[str, str] = {}
    if provider.protocol == ANTHROPIC:
        if provider.base_url:
            out["ANTHROPIC_BASE_URL"] = provider.base_url
        if value:
            out["ANTHROPIC_AUTH_TOKEN"] = value
        if provider.model:
            out["ANTHROPIC_MODEL"] = provider.model
        for tier, model in provider.tiers.items():
            out[f"ANTHROPIC_DEFAULT_{tier.upper()}_MODEL"] = model
    elif provider.protocol == OPENAI:
        if provider.base_url:
            out["OPENAI_BASE_URL"] = provider.base_url
        if value:
            out["OPENAI_API_KEY"] = value
    elif provider.protocol == DEEPSEEK:
        if provider.base_url:
            out["DEEPSEEK_BASE_URL"] = provider.base_url
        if value:
            out["DEEPSEEK_API_KEY"] = value
    return out


def cli_settings(provider: Provider) -> dict:
    """The non-env part of a Claude Code settings.json for this endpoint.

    `env` is deliberately absent. Claude Code's settings.json `env` block
    outranks the process environment, so a stale copy there would silently
    outvote whatever provider the run chose — the bug that cost 190s of retries
    the first time this was wired up.
    """
    settings: dict = {}
    if provider.model:
        settings["model"] = provider.model
    return settings


# ---- liveness ----------------------------------------------------------


#: An endpoint that keeps its URL in the CLI's own config still needs a probe.
DEFAULT_PROBE = {"deepseek": "https://api.deepseek.com/models"}


def _probe_url(provider: Provider) -> str:
    """Where the model list lives, for the three shapes this account has.

    All of them are OpenAI-compatible model listings; only the prefix differs.
    A base that already ends in /v1 must not get a second one.
    """
    base = (provider.base_url or "").rstrip("/")
    if not base:
        return DEFAULT_PROBE.get(provider.protocol, "")
    return f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"


def probe(provider: Provider, secrets: dict[str, str], proxy: str | None = None) -> dict:
    """Ask the endpoint for its model list. Free: no generation tokens."""
    import time

    result = {"provider": provider.name, "protocol": provider.protocol, "url": ""}
    url = _probe_url(provider)
    if not url:
        result.update({"ok": False, "status": 0, "error": "no endpoint to probe"})
        return result
    result["url"] = url
    headers = {"User-Agent": "nodus/0.4"}
    value = secret_value(provider, secrets)
    if value:
        headers["Authorization"] = f"Bearer {value}"
    opener = (
        urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
        if proxy
        else urllib.request.build_opener(urllib.request.ProxyHandler({}))
    )
    started = time.time()
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        models = body.get("data") or body.get("models") or []
        result.update(
            {
                "ok": True,
                "status": resp.status,
                "models": len(models),
                "latency_ms": int((time.time() - started) * 1000),
            }
        )
    except urllib.error.HTTPError as exc:
        result.update({"ok": False, "status": exc.code, "error": str(exc.reason)})
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        result.update({"ok": False, "status": 0, "error": str(exc)})
    return result


# ---- one-time import of pre-provider config ----------------------------


def _read_json(path_: Path) -> dict:
    try:
        return json.loads(path_.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_toml(path_: Path) -> dict:
    try:
        return tomllib.loads(path_.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _legacy_claude_env() -> dict:
    """The endpoint that used to be implied by a settings.json `env` block."""
    for candidate in (
        config.LEGACY_AGENTS_DIR / "claude" / "settings.json",
        Path.home() / ".claude" / "settings.json",
    ):
        env = _read_json(candidate).get("env") or {}
        if env.get("ANTHROPIC_BASE_URL"):
            return env
    return {}


def _legacy_codex_provider() -> dict:
    for candidate in (
        config.LEGACY_AGENTS_DIR / "codex" / "config.toml",
        Path.home() / ".codex" / "config.toml",
    ):
        data = _read_toml(candidate)
        name = data.get("model_provider")
        block = (data.get("model_providers") or {}).get(name) or {}
        if block.get("base_url"):
            return {**block, "model": data.get("model") or ""}
    return {}


def bootstrap() -> None:
    """Seed providers.json from whatever config the old layout implied.

    Idempotent: once the file exists this does nothing, so a later hand edit is
    never overwritten. Without it, upgrading would leave every agent without an
    endpoint — the tokens only existed inside the per-agent config directories
    that this refactor removes.
    """
    if path().exists():
        return
    secrets = config.load_secrets()
    table = _builtin()

    claude_env = _legacy_claude_env()
    if claude_env.get("ANTHROPIC_BASE_URL"):
        base = claude_env["ANTHROPIC_BASE_URL"].rstrip("/")
        token = claude_env.get("ANTHROPIC_AUTH_TOKEN") or ""
        tiers = {}
        for tier in ("opus", "sonnet", "haiku", "fable"):
            value = claude_env.get(f"ANTHROPIC_DEFAULT_{tier.upper()}_MODEL")
            if value:
                tiers[tier] = value
        table["relay"] = Provider(
            name="relay",
            protocol=ANTHROPIC,
            base_url=base,
            key_secret="RELAY_API_KEY",
            model=claude_env.get("ANTHROPIC_MODEL") or "claude-opus-5-5[1M]",
            tiers=tiers or table["relay"].tiers,
            meter="relay",
            note="9527 中转，站内余额计价",
        )
        if token and not secrets.get("RELAY_API_KEY"):
            _add_secret("RELAY_API_KEY", token)
            secrets["RELAY_API_KEY"] = token

    codex = _legacy_codex_provider()
    if codex.get("base_url"):
        token = codex.get("experimental_bearer_token") or codex.get("api_key") or ""
        if token and not secrets.get("RELAY_API_KEY"):
            _add_secret("RELAY_API_KEY", token)
            secrets["RELAY_API_KEY"] = token
        if codex["base_url"].rstrip("/") != table["relay-openai"].base_url:
            table["relay-openai"] = Provider(
                name="relay-openai",
                protocol=OPENAI,
                base_url=codex["base_url"].rstrip("/"),
                key_secret="RELAY_API_KEY",
                model=codex.get("model") or None,
                wire_api=str(codex.get("wire_api") or "responses"),
                meter="relay",
                note="同一个中转的 OpenAI 协议入口",
            )

    save(table)


def _add_secret(name: str, value: str) -> None:
    path_ = config.HOME / "secrets.json"
    data = _read_json(path_)
    data[name] = value
    config.ensure_dirs()
    path_.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        path_.chmod(0o600)
    except OSError:
        pass
