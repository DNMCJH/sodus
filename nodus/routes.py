"""Routes: one agent, an ordered list of providers.

A route is the unit a caller names. It exists because "claude" was doing three
jobs: the CLI, the endpoint behind it, and the name a task asked for. Splitting
them leaves a gap — something has to say *this CLI, through these endpoints, in
this order* — and that is a route.

The order is a fallback chain, not a preference list. A failed attempt falls
through only when it provably did nothing (see runner.py); a route is not a way
to race two providers and keep the cheaper answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import agents, config, providers


@dataclass(frozen=True)
class Route:
    name: str
    agent: str
    #: ordered; the first is the primary and the rest are fallbacks
    providers: tuple[str, ...]
    model: str | None = None
    effort: str | None = None

    @property
    def primary(self) -> str:
        return self.providers[0] if self.providers else ""

    def to_json(self) -> dict:
        return {
            "agent": self.agent,
            "providers": list(self.providers),
            **({"model": self.model} if self.model else {}),
            **({"effort": self.effort} if self.effort else {}),
        }


def _builtin() -> dict[str, Route]:
    return {
        # Relay is the account's normal path; MiniMax is the same protocol and
        # a different vendor, so it can absorb a relay outage. It is a strictly
        # weaker model, which is why a fallback is announced and recorded
        # rather than quietly succeeding.
        "claude": Route("claude", "claude", ("relay", "minimax")),
        "minimax": Route("minimax", "claude", ("minimax",)),
        "codex": Route("codex", "codex", ("relay-openai",)),
        "dsh": Route("dsh", "dsh", ("deepseek",)),
    }


def path():
    return config.HOME / "routes.json"


def load() -> dict[str, Route]:
    """Built-ins merged with `routes.json`. Pure: never writes."""
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
        chain = tuple(data.get("providers") or ())
        if not chain or not data.get("agent"):
            continue
        out[name] = Route(
            name=name,
            agent=str(data["agent"]),
            providers=chain,
            model=data.get("model") or None,
            effort=data.get("effort") or None,
        )
    for name in removed:
        out.pop(name, None)
    return out


def save(routes: dict[str, Route]) -> None:
    base = _builtin()
    data: dict = {}
    removed: list[str] = []
    for name, route in routes.items():
        if name in base:
            if route != base[name]:
                data[name] = route.to_json()
        else:
            data[name] = route.to_json()
    for name in base:
        if name not in routes:
            removed.append(name)
    if removed:
        data["_removed"] = removed
    config.ensure_dirs()
    path().write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def upsert(route: Route) -> None:
    table = load()
    table[route.name] = route
    save(table)


def remove(name: str) -> bool:
    table = load()
    if name not in table:
        return False
    table.pop(name)
    save(table)
    return True


def resolve(name: str) -> Route | None:
    """A route by name, falling back to a bare agent name.

    `--agent claude` predates routes and must keep working; every agent has a
    same-named built-in route, so the fallback only matters for a new agent
    whose route has not been written yet.
    """
    table = load()
    if name in table:
        return table[name]
    spec = agents.get(name)
    if spec is None:
        return None
    return Route(name=name, agent=name, providers=("",))


def problems(route: Route) -> list[str]:
    """Why this route cannot run, in the caller's language."""
    out: list[str] = []
    spec = agents.get(route.agent)
    table = providers.load()
    if spec is None:
        out.append(f"unknown agent: {route.agent}")
    if not route.providers or not route.primary:
        out.append("no providers")
        return out
    primary = table.get(route.primary)
    for name in route.providers:
        provider = table.get(name)
        if provider is None:
            out.append(f"unknown provider: {name}")
            continue
        if spec is not None and not spec.accepts_protocol(provider.protocol):
            out.append(f"{spec.name} cannot speak {provider.protocol} ({name})")
        if primary is not None and provider.protocol != primary.protocol:
            out.append(
                f"{name} speaks {provider.protocol}, but the chain starts with"
                f" {primary.protocol}"
            )
    return out


def resolve_for_run(name: str) -> Route:
    route = resolve(name)
    if route is None:
        known = ", ".join(sorted(load()))
        raise ValueError(f"unknown route: {name} (known: {known})")
    return route


def ready(name: str, secrets: dict[str, str] | None = None) -> bool:
    """Can this route run right now, with the secrets on this machine?

    The one question the pipeline code asks ("is there another agent I can hand
    this to"). It used to be `agents.ready`, which stopped meaning anything once
    an agent could be reached through more than one endpoint.
    """
    route = resolve(name)
    if route is None:
        return False
    if problems(route):
        return False
    spec = agents.get(route.agent)
    if spec is None:
        return False
    table = providers.load()
    secrets = secrets if secrets is not None else config.load_secrets()
    return any(
        not agents.missing(spec, table[provider], secrets)
        for provider in route.providers
        if provider in table
    )
