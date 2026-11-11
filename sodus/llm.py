"""One chat completion on a route's own credential — the seam the pets use.

The four desktop pets used to reach a model through DSH's `ctx.llm`. The pet
host can now run standalone (`lib/standalone.js`), where there is no DSH and no
credential store — so the pet asks Sodus instead:

    宠物 ──► POST /llm/chat {agent} ──► route ──► provider

The caller names an **agent**, never an endpoint or a key. That is the whole
point: the account already has exactly one place that knows which endpoint it
reaches, what that endpoint charges, and how much has been spent today. Four
pets holding four copies of the same secret would be four places a spend can
happen unmetered.

Scope is deliberately small, and the limits are part of the contract:

    400 output tokens per call     a pet says one sentence, not an essay
    10 calls per agent per minute  a runaway click loop is not a workload

Cost is recorded the same way a run's cost is (runner.py `_price`), in the same
order of trustworthiness: a measured calibration, a published price list, or
the provider's own account meter read around the call. `cost_basis` says which
one answered, so nobody has to guess how much to believe the number.
"""

from __future__ import annotations

import collections
import json
import sys
import threading
import time
import urllib.error
import urllib.request

from . import config, db, meter, pricing, providers, routes

#: Hard ceiling on output tokens for one pet utterance.
MAX_OUTPUT_TOKENS = 400

#: Per agent, not per client: the pet host is one process with four pets, and
#: the thing worth stopping is a click loop, not four humans taking turns.
CALLS_PER_MINUTE = 10
WINDOW_SECONDS = 60.0

#: How long to wait for the model before calling it a timeout. A whisper is one
#: sentence; anything past this is a broken endpoint, not a slow answer.
TIMEOUT_SECONDS = 45.0

#: Models no provider pins. The DSH route's endpoint keeps its model inside the
#: CLI's own config, which is not what this module talks to — so the protocol
#: default names it, and `config.json` can override per route.
DEFAULT_MODEL = {providers.DEEPSEEK: "deepseek-chat"}

#: Per-agent call timestamps for the rate limit. In-memory on purpose: a
#: restarted daemon forgetting the last minute is harmless, a database write on
#: every pet utterance is not.
_CALLS: dict[str, collections.deque[float]] = {}
_CALLS_LOCK = threading.Lock()


def _log(message: str) -> None:
    print(f"[sodus llm] {message}", file=sys.stderr, flush=True)


# ---- limits -----------------------------------------------------------


def _slide(agent: str, now: float) -> None:
    """Drop timestamps that fell out of the window."""
    queue = _CALLS.setdefault(agent, collections.deque())
    while queue and queue[0] <= now - WINDOW_SECONDS:
        queue.popleft()


def allow(agent: str, now: float | None = None) -> bool:
    """Is this agent under its per-minute budget? Counts the call when it says yes."""
    moment = time.time() if now is None else now
    with _CALLS_LOCK:
        _slide(agent, moment)
        queue = _CALLS[agent]
        if len(queue) >= CALLS_PER_MINUTE:
            return False
        queue.append(moment)
        return True


# ---- route -> concrete request ----------------------------------------


def model_for(route: routes.Route, provider: providers.Provider) -> str:
    """The model string this endpoint expects.

    Four sources, in order: a per-route override in `config.json` (an escape
    hatch for a vendor that renames a model), the route's own `model` (opencode
    spells it `provider/model` in its namespace, so the namespace is dropped),
    the provider's pinned model, then the protocol default.
    """
    override = ((config.load_config().get("llm") or {}).get("models") or {}).get(route.name)
    if isinstance(override, str) and override.strip():
        return override.strip()
    if route.model:
        return route.model.split("/")[-1]
    if provider.model:
        return provider.model
    return DEFAULT_MODEL.get(provider.protocol, "")


def _url(provider: providers.Provider) -> str:
    """Where this protocol's chat lives, from the provider's own base.

    Same rule `providers._probe_url` uses: a base that already ends in `/v1`
    must not get a second one.
    """
    base = (provider.base_url or "").rstrip("/")
    if provider.protocol == providers.ANTHROPIC:
        return f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
    if provider.protocol == providers.OPENAI:
        return (
            f"{base}/chat/completions"
            if base.endswith("/v1")
            else f"{base}/v1/chat/completions"
        )
    base = base or "https://api.deepseek.com"
    return f"{base}/chat/completions"


def _headers(provider: providers.Provider, secret: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "User-Agent": "sodus/0.4"}
    if provider.protocol == providers.ANTHROPIC:
        # The relay fronts Anthropic for Claude Code, which authenticates with
        # ANTHROPIC_AUTH_TOKEN — i.e. a bearer header, not x-api-key.
        headers["Authorization"] = f"Bearer {secret}"
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = f"Bearer {secret}"
    return headers


def _merge_roles(messages: list[dict]) -> list[dict]:
    """Collapse consecutive same-role turns.

    Anthropic rejects `user,user` outright; a chat memory restored from a file
    can contain it after an interrupted turn. Merging is what the model would
    have been shown anyway.
    """
    out: list[dict] = []
    for message in messages:
        if message["role"] == "system":
            continue
        if out and out[-1]["role"] == message["role"]:
            out[-1] = {
                "role": message["role"],
                "content": out[-1]["content"] + "\n" + message["content"],
            }
            continue
        out.append(dict(message))
    return out


def _payload(
    provider: providers.Provider,
    model: str,
    messages: list[dict],
    max_tokens: int,
) -> dict:
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = _merge_roles(messages)
    body: dict = {"model": model, "max_tokens": max_tokens, "messages": turns}
    if provider.protocol == providers.ANTHROPIC:
        # Anthropic has no `system` role; it is a top-level string.
        if system:
            body["system"] = system
    elif system:
        body["messages"] = [{"role": "system", "content": system}, *turns]
    return body


def _read(protocol: str, body: dict) -> tuple[str, dict, str]:
    """(text, usage, reported_model) from whichever shape answered."""
    reported = str(body.get("model") or "")
    if protocol == providers.ANTHROPIC:
        blocks = body.get("content") or []
        text = "\n".join(
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        raw = body.get("usage") or {}
        usage = {
            "input_tokens": int(raw.get("input_tokens") or 0),
            "output_tokens": int(raw.get("output_tokens") or 0),
            "cache_read": int(raw.get("cache_read_input_tokens") or 0),
            "cache_write": int(raw.get("cache_creation_input_tokens") or 0),
        }
        return text, usage, reported
    choices = body.get("choices") or []
    message = (choices[0] or {}).get("message") if choices else {}
    text = str((message or {}).get("content") or "").strip()
    raw = body.get("usage") or {}
    details = raw.get("prompt_tokens_details") or {}
    usage = {
        "input_tokens": int(raw.get("prompt_tokens") or 0),
        "output_tokens": int(raw.get("completion_tokens") or 0),
        "cache_read": int(
            details.get("cached_tokens") or raw.get("prompt_cache_hit_tokens") or 0
        ),
        "cache_write": 0,
    }
    return text, usage, reported


# ---- money ------------------------------------------------------------


def _needs_meter(provider: providers.Provider, model: str) -> bool:
    """Only ask the account meter when no cheaper answer exists.

    A meter read is two network calls and a balance is account-wide, so it is
    the last resort, not the default.
    """
    if provider.meter is None:
        return False
    if provider.meter == "relay" and providers.calibration_for(model) is not None:
        return False
    return provider.pricing is None


def _price(
    provider: providers.Provider,
    model: str,
    usage: dict,
    before: meter.Reading | None,
    secrets: dict[str, str],
) -> dict:
    """Cost in the same order of trustworthiness `runner._price` uses."""
    calibration = (
        providers.calibration_for(model) if provider.meter == "relay" else None
    )
    if calibration is not None or provider.pricing is not None:
        # pricing.py owns the arithmetic so a pet utterance and a task run on
        # the same endpoint cannot round differently.
        amount, currency, basis, _detail = pricing.price(model, provider, usage)
        if amount is not None:
            return {"cost": amount, "currency": currency, "cost_basis": basis}
    if before is not None:
        # Billing lands seconds after the call, so give it a moment: a delta of
        # zero recorded as "this was free" is worse than saying nothing.
        after = meter.read(provider, secrets, settle_from=before, settle=3.0)
        spent = meter.delta(before, after)
        if spent is not None:
            return {
                "cost": round(spent * config.cost_rate(before.unit), 6),
                "currency": "CNY",
                "cost_basis": "metered",
                "meter": {"unit": before.unit, "delta": round(spent, 6)},
            }
    return {}


# ---- the call ---------------------------------------------------------


def _fail(kind: str, message: str, status: int, **extra) -> tuple[int, dict]:
    return status, {"error": {"kind": kind, "message": message, **extra}}


def _validate(messages) -> str:
    """Empty string when the list is usable, else the reason."""
    if not isinstance(messages, list) or not messages:
        return "messages must be a non-empty list of {role, content}"
    for message in messages:
        if not isinstance(message, dict):
            return "each message must be an object with role and content"
        if message.get("role") not in ("system", "user", "assistant"):
            return "role must be system, user or assistant"
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            return "content must be a non-empty string"
    return ""


def _send(
    provider: providers.Provider,
    secret: str,
    body: dict,
) -> tuple[dict | None, tuple[int, dict] | None]:
    """One HTTP attempt. Returns (parsed body, None) or (None, error reply)."""
    url = _url(provider)
    if not url.startswith("http"):
        return None, _fail(
            "not-ready", f"provider {provider.name} has no base_url", 409
        )
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=_headers(provider, secret),
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        kind = {
            400: "bad-request",
            401: "unauthorized",
            403: "unauthorized",
            404: "bad-request",
            429: "rate-limited",
        }.get(exc.code, "provider-error")
        status = 400 if exc.code == 400 else (429 if exc.code == 429 else 502)
        return None, _fail(
            kind, f"{provider.name} HTTP {exc.code}: {detail}", status, provider=provider.name
        )
    except TimeoutError:
        return None, _fail(
            "timeout", f"{provider.name} did not answer in {int(TIMEOUT_SECONDS)}s", 504,
            provider=provider.name,
        )
    except (urllib.error.URLError, OSError) as exc:
        return None, _fail(
            "network", f"{provider.name} unreachable: {exc}", 502, provider=provider.name
        )
    try:
        return json.loads(raw), None
    except json.JSONDecodeError:
        return None, _fail(
            "bad-response",
            f"{provider.name} returned non-JSON: {raw[:200]}",
            502,
            provider=provider.name,
        )


def chat(
    agent: str,
    messages,
    max_tokens: int | None = None,
    note: str = "",
    cwd: str = "",
) -> tuple[int, dict]:
    """One completion through `agent`'s route. Returns (http status, payload).

    Never raises: every failure is a structured `{"error": {kind, message}}`
    with a status the caller can act on (429 means slow down, 502 means the
    endpoint is broken, 404 means the agent name is wrong).
    """
    name = str(agent or "").strip()
    if not name:
        return _fail("bad-request", "agent is required", 400)
    route = routes.resolve(name)
    if route is None:
        known = ", ".join(sorted(routes.load())) or "(none)"
        return _fail("unknown-agent", f"unknown agent: {name} (known: {known})", 404)

    problem = _validate(messages)
    if problem:
        return _fail("bad-request", problem, 400)

    limit = MAX_OUTPUT_TOKENS
    if isinstance(max_tokens, (int, float)) and not isinstance(max_tokens, bool):
        requested = int(max_tokens)
        if requested > 0:
            limit = min(requested, MAX_OUTPUT_TOKENS)

    if not allow(name):
        return _fail(
            "rate-limited",
            f"{name} is limited to {CALLS_PER_MINUTE} calls per {int(WINDOW_SECONDS)}s",
            429,
            retry_after=int(WINDOW_SECONDS),
        )

    table = providers.load()
    secrets = config.load_secrets()
    started = time.time()
    last_error: tuple[int, dict] | None = None
    for provider_name in route.providers:
        provider = table.get(provider_name)
        if provider is None:
            continue
        secret = providers.secret_value(provider, secrets)
        if provider.key_secret and not secret:
            continue  # a route's fallback is only real if it has a key
        model = model_for(route, provider)
        if not model:
            last_error = _fail(
                "not-ready", f"no model for route {route.name} on {provider.name}", 409
            )
            continue
        before = (
            meter.read(provider, secrets) if _needs_meter(provider, model) else None
        )
        body = _payload(provider, model, messages, limit)
        parsed, error = _send(provider, secret, body)
        if error is not None:
            # A fallback is announced, never silent: the caller may be talking to
            # a weaker model than the route's name suggests (today the relay has
            # no channel for claude-opus-5-5[1M] and `claude` serves MiniMax).
            _log(
                f"{route.name}: {provider.name} failed ({error[1]['error']['kind']}: "
                f"{error[1]['error']['message'][:160]})"
            )
            last_error = error
            continue
        text, usage, reported = _read(provider.protocol, parsed)
        if not text:
            stop = str(parsed.get("stop_reason") or "")
            kind = "max-tokens" if stop == "max_tokens" else "empty"
            last_error = _fail(kind, "model returned no text", 502)
            continue
        priced = _price(provider, model, usage, before, secrets)
        usage.update(priced)
        usage["source"] = "llm/chat"
        run_id = _record(route, provider, reported or model, usage, note, cwd, started)
        answer = {
            "text": text,
            "model": reported or model,
            "agent": route.agent,
            "provider": provider.name,
            "route": route.name,
            "cost": priced.get("cost"),
            "currency": priced.get("currency"),
            "cost_basis": priced.get("cost_basis"),
            "usage": {
                key: usage[key]
                for key in ("input_tokens", "output_tokens", "cache_read", "cache_write")
            },
            "run": run_id,
        }
        _log(
            f"{route.name} -> {provider.name}/{answer['model']}"
            f" out={usage['output_tokens']}"
            f" cost={answer['currency'] or '-'}{answer['cost'] if answer['cost'] is not None else '-'}"
            f" ({answer['cost_basis'] or 'unrecorded'})"
        )
        return 200, answer

    return last_error or _fail("not-ready", f"route {route.name} has no usable provider", 409)


def _record(
    route: routes.Route,
    provider: providers.Provider,
    model: str,
    usage: dict,
    note: str,
    cwd: str,
    started: float,
) -> str:
    """One row in `run`, so `sodus stats` counts this spend.

    A pet utterance is not a task, but it *is* a token bill, and the only place
    Sodus adds those up is the run table. `status` stays `completed` (the call
    did complete) and the note says where it came from, so the board's
    "no test evidence" list names it instead of hiding it.

    The row is written after the call, then backdated: inserting it up front
    would show every whisper as a queued run while the model is thinking.
    """
    rid = ""
    try:
        row = db.create_run(
            task_id="",
            agent=route.agent,
            cwd=cwd or str(config.HOME),
            note=note or f"桌宠 {route.name}",
            model=model,
            route=route.name,
            provider=provider.name,
            # A pet utterance is a token bill, not work: it must stay out of the
            # board's 50-row window and out of the route history, and the only
            # way to tell it apart afterwards is this column.
            kind="chat",
        )
        rid = row["id"]
        db.update_run(
            rid,
            status="completed",
            usage_json=json.dumps(usage, ensure_ascii=False),
            started_at=started,
            ended_at=db.now(),
        )
    except Exception as exc:  # noqa: BLE001 - a ledger write must not lose the answer
        _log(f"could not record this call: {type(exc).__name__}: {exc}")
    return rid
