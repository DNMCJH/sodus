"""Topology commands: providers, routes, profiles.

These are file operations, so they work with the daemon down — which is exactly
when you need them: a broken endpoint is why you are here.
"""

from __future__ import annotations

import json

from ... import agents, config, profiles, providers, routes


def add(sub) -> None:
    p = sub.add_parser("providers", help="endpoints: list, show, add, remove, probe")
    p.add_argument("action", choices=["list", "show", "add", "rm", "probe"])
    p.add_argument("name", nargs="?", default="")
    p.add_argument("--protocol", choices=[providers.ANTHROPIC, providers.OPENAI, providers.DEEPSEEK])
    p.add_argument("--base-url", default="")
    p.add_argument("--key-secret", default="", help="secret NAME in secrets.json")
    p.add_argument("--key", default="", help="store this value as --key-secret (writes secrets.json)")
    p.add_argument("--model", default="")
    p.add_argument("--tier", action="append", default=[], metavar="TAG=MODEL")
    p.add_argument("--meter", choices=["relay", "deepseek"], default="")
    p.add_argument("--price", default="", metavar="IN,OUT,CACHE_READ,CNY|USD")
    p.add_argument("--wire-api", default="", choices=["", "responses", "chat"])
    p.add_argument("--note", default="")
    p.set_defaults(func=provider_cmd, needs_daemon=False)

    p = sub.add_parser("routes", help="routing groups: list, show, set, remove")
    p.add_argument("action", choices=["list", "show", "set", "rm"])
    p.add_argument("name", nargs="?", default="")
    p.add_argument("--agent", default="")
    p.add_argument("--providers", default="", help="ordered fallback chain, e.g. relay,minimax")
    p.add_argument("--model", default="")
    p.add_argument("--effort", default="")
    p.set_defaults(func=route_cmd, needs_daemon=False)

    p = sub.add_parser("profiles", help="configuration snapshots: list, show, use, new, rm")
    p.add_argument("action", choices=["list", "show", "use", "new", "rm"])
    p.add_argument("name", nargs="?", default="")
    p.add_argument("--from", dest="source", default="")
    p.set_defaults(func=profile_cmd, needs_daemon=False)

    p = sub.add_parser("topology", help="agents × providers × routes in one view")
    p.set_defaults(func=topology, needs_daemon=False)


# ---- provider ----------------------------------------------------------


def provider_cmd(args) -> int:
    table = providers.load()
    if args.action == "list":
        secrets = config.load_secrets()
        for p in sorted(table.values(), key=lambda x: x.name):
            gap = providers.missing(p, secrets)
            mark = "ok" if not gap else "MISSING " + ",".join(gap)
            price = (
                f"{p.pricing.input}/{p.pricing.output} {p.pricing.currency}" if p.pricing else "-"
            )
            print(f"{p.name:<14} {p.protocol:<10} {mark:<22} {p.base_url or '-':<34} {price}")
        return 0

    if args.action == "show":
        p = table.get(args.name)
        if p is None:
            raise ValueError(f"unknown provider: {args.name} (known: {', '.join(sorted(table))})")
        print(json.dumps(p.to_json(), indent=2, ensure_ascii=False))
        return 0

    if args.action == "probe":
        targets = [table[args.name]] if args.name else sorted(table.values(), key=lambda x: x.name)
        secrets = config.load_secrets()
        proxy = secrets.get("TYPESAFE_PROXY") or None
        bad = 0
        for p in targets:
            r = providers.probe(p, secrets, proxy if p.protocol != providers.DEEPSEEK else None)
            bad += 0 if r.get("ok") else 1
            print(
                f"{p.name:<14} {'ok' if r.get('ok') else 'FAIL':<5} status={r.get('status')} "
                f"models={r.get('models', '-')} {r.get('latency_ms', '-')}ms {r.get('error', '')}"
            )
        return 0 if not bad else 1

    if args.action == "add":
        if not args.name:
            raise ValueError("provider add needs a name")
        if not args.protocol or not args.base_url:
            raise ValueError("provider add needs --protocol and --base-url")
        key_secret = args.key_secret
        if args.key:
            key_secret = key_secret or f"{args.name.upper().replace('-', '_')}_API_KEY"
            providers._add_secret(key_secret, args.key)
        tiers = {}
        for item in args.tier:
            if "=" not in item:
                raise ValueError(f"--tier wants TAG=MODEL, got {item!r}")
            tag, model = item.split("=", 1)
            tiers[tag.strip()] = model.strip()
        pricing = None
        if args.price:
            parts = [x.strip() for x in args.price.split(",")]
            if len(parts) < 2:
                raise ValueError("--price wants IN,OUT[,CACHE_READ][,CURRENCY]")
            pricing = providers.Pricing(
                input=float(parts[0]),
                output=float(parts[1]),
                cache_read=float(parts[2]) if len(parts) > 2 and parts[2] else 0.0,
                currency=(parts[3] if len(parts) > 3 and parts[3] else "USD").upper(),
            )
        providers.upsert(
            providers.Provider(
                name=args.name,
                protocol=args.protocol,
                base_url=args.base_url.rstrip("/"),
                key_secret=key_secret or None,
                model=args.model or None,
                tiers=tiers,
                pricing=pricing,
                meter=args.meter or None,
                wire_api=args.wire_api or "responses",
                note=args.note,
            )
        )
        print(f"{args.name}: saved to {providers.path()}")
        return 0

    if args.action == "rm":
        if not providers.remove(args.name):
            raise ValueError(f"unknown provider: {args.name}")
        print(f"{args.name}: removed (a route that still names it will report the gap)")
        return 0
    return 0


# ---- route -------------------------------------------------------------


def route_cmd(args) -> int:
    table = routes.load()
    if args.action == "list":
        secrets = config.load_secrets()
        provs = providers.load()
        for r in sorted(table.values(), key=lambda x: x.name):
            gaps = list(routes.problems(r))
            spec = agents.get(r.agent)
            if spec is not None:
                for name in r.providers:
                    provider = provs.get(name)
                    if provider is not None:
                        gaps.extend(f"{name}:{g}" for g in agents.missing(spec, provider, secrets))
            mark = "ok" if not gaps else "GAP " + ";".join(gaps)
            print(f"{r.name:<12} {r.agent:<7} {' → '.join(r.providers):<28} {mark}")
        return 0

    if args.action == "show":
        r = table.get(args.name)
        if r is None:
            raise ValueError(f"unknown route: {args.name} (known: {', '.join(sorted(table))})")
        print(json.dumps(r.to_json(), indent=2, ensure_ascii=False))
        for gap in routes.problems(r):
            print(f"problem: {gap}")
        return 0

    if args.action == "set":
        if not args.name or not args.agent or not args.providers:
            raise ValueError("route set needs NAME --agent X --providers a,b")
        chain = tuple(x.strip() for x in args.providers.split(",") if x.strip())
        route = routes.Route(
            name=args.name,
            agent=args.agent,
            providers=chain,
            model=args.model or None,
            effort=args.effort or None,
        )
        gaps = routes.problems(route)
        if gaps:
            raise ValueError("; ".join(gaps))
        routes.upsert(route)
        print(f"{args.name}: {args.agent} → {' → '.join(chain)}")
        return 0

    if args.action == "rm":
        if not routes.remove(args.name):
            raise ValueError(f"unknown route: {args.name}")
        print(f"{args.name}: removed")
        return 0
    return 0


# ---- profile -----------------------------------------------------------


def profile_cmd(args) -> int:
    active = config.active_profile()
    if args.action == "list":
        for name in profiles.names() or ["default"]:
            p = profiles.read(name)
            mark = "*" if name == active else " "
            bits = [f"route={p.route or '-'}", f"model={p.model or '-'}"]
            print(f"{mark} {name:<12} {' '.join(bits)}")
        return 0

    if args.action == "show":
        p = profiles.read(args.name or active)
        print(json.dumps(vars(p), indent=2, ensure_ascii=False))
        print(f"dir: {profiles.dir_of(p.name)}")
        for agent in agents.registry():
            homes = sorted(x.name for x in (profiles.dir_of(p.name) / agent).glob("*")) if (
                profiles.dir_of(p.name) / agent
            ).is_dir() else []
            if homes:
                print(f"  {agent:<7} → {', '.join(homes)}")
        return 0

    if args.action == "use":
        if not args.name:
            raise ValueError("profile use needs a name")
        profiles.use(args.name)
        print(f"active profile: {args.name} (new runs only; running ones keep theirs)")
        return 0

    if args.action == "new":
        if not args.name:
            raise ValueError("profile new needs a name")
        p = profiles.create(args.name, args.source or None)
        print(f"{p.name}: created" + (f" from {args.source}" if args.source else ""))
        return 0

    if args.action == "rm":
        if not args.name:
            raise ValueError("profile rm needs a name")
        profiles.remove(args.name)
        print(f"{args.name}: removed")
        return 0
    return 0


# ---- one view ----------------------------------------------------------


def topology(args) -> int:
    secrets = config.load_secrets()
    prof = profiles.read()
    print(f"profile  {prof.name}   route={prof.route or '-'} model={prof.model or '-'}")
    print()
    print("agents")
    for spec in agents.registry().values():
        gap = agents.unavailable(spec)
        print(f"  {spec.name:<8} {spec.vendor:<7} {'ok' if not gap else ','.join(gap)}")
    print()
    print("providers")
    for p in sorted(providers.load().values(), key=lambda x: x.name):
        gap = providers.missing(p, secrets)
        print(f"  {p.name:<14} {p.protocol:<10} {'ok' if not gap else 'MISSING ' + ','.join(gap)}")
    print()
    print("routes")
    for r in sorted(routes.load().values(), key=lambda x: x.name):
        gap = routes.problems(r)
        mark = "ok" if not gap else "; ".join(gap)
        print(f"  {r.name:<12} {r.agent:<7} {' → '.join(r.providers):<28} {mark}")
    return 0
