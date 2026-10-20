"""Machine and account commands: serve, agents, doctor, probe, status, usage."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from ... import (
    agents,
    config,
    db,
    decision,
    desk as desk_view,
    providers,
    routes,
    runner,
    server,
    telemetry,
)
from ..client import call


def add(sub) -> None:
    p = sub.add_parser("serve", help="run the HTTP daemon in the foreground")
    p.add_argument("--port", type=int, default=4199)
    p.set_defaults(func=serve, needs_daemon=False)

    p = sub.add_parser("agents", help="adapter readiness")
    p.set_defaults(func=agents_cmd)

    p = sub.add_parser("doctor", help="self-check: state dirs, token, db, agents, sessions")
    p.set_defaults(func=doctor, needs_daemon=False)

    p = sub.add_parser("probe", help="provider liveness (no generation tokens)")
    p.set_defaults(func=probe)

    p = sub.add_parser("status", help="last probes + today's usage + balance")
    p.set_defaults(func=status)

    p = sub.add_parser("balance", help="relay billing balance")
    p.set_defaults(func=balance)

    p = sub.add_parser("usage", help="token usage per agent")
    p.add_argument("--days", type=float, default=7)
    p.set_defaults(func=usage)

    p = sub.add_parser("decisions", help="recent Jev decisions")
    p.add_argument("--last", type=int, default=10)
    p.set_defaults(func=decisions_cmd)

    p = sub.add_parser("jev", help="is the Jev key configured")
    p.set_defaults(func=jev)

    p = sub.add_parser("reconcile", help="fail runs whose owner died mid-run")
    p.add_argument("--stale", type=int, default=900)
    p.set_defaults(func=reconcile)

    p = sub.add_parser("desk", help="render a markdown progress page (for a vault dashboard)")
    p.add_argument("--out", help="write to this path; default prints to stdout")
    p.add_argument("--limit", type=int, default=15, help="how many tasks (default 15)")
    p.set_defaults(func=desk, needs_daemon=False)

    p = sub.add_parser("locks", help="which working directories have a writer")
    p.set_defaults(func=locks, needs_daemon=False)

    p = sub.add_parser("unlock", help="force-release a directory a dead run left locked")
    p.add_argument("cwd")
    p.set_defaults(func=unlock, needs_daemon=False)


def locks(args) -> int:
    rows = db.active_locks()
    if not rows:
        print("(no directory is locked)")
        return 0
    for row in rows:
        age = int(time.time() - float(row.get("acquired_at") or 0))
        print(
            f"{row['cwd']}  {row.get('run_id') or '?'}  "
            f"{row.get('agent') or '?'}@{row.get('route') or '-'}  {age}s"
        )
    return 0


def unlock(args) -> int:
    if not db.release_lock_for_cwd(args.cwd):
        print(f"{args.cwd} was not locked")
        return 0
    print(f"{args.cwd}: unlocked")
    return 0


def serve(args) -> int:
    server.serve(args.port)
    return 0


def desk(args) -> int:
    text = desk_view.render(desk_view.collect(args.limit))
    if not args.out:
        print(text)
        return 0
    path = Path(args.out).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(path)
    return 0


def agents_cmd(args) -> int:
    for a in call("GET", "/agents"):
        mark = "ok" if a["ready"] else "MISSING " + ",".join(a["missing"])
        print(f"{a['name']:<8} {a['vendor']:<8} {','.join(a['protocols']):<12} {mark}")
    return 0


def _check(name: str, fn) -> tuple[bool, str]:
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001 - doctor must report, not crash
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    print(f"{'PASS' if ok else 'FAIL'}  {name:<14} {detail}")
    return ok, detail


def doctor(args) -> int:
    results: list[bool] = []

    def home():
        config.ensure_dirs()
        return os.access(config.HOME, os.W_OK), str(config.HOME)

    def token():
        path = config.HOME / "token"
        exists = path.exists()
        length = len(config.token()) if exists else 0
        mode = ""
        if os.name != "nt" and exists:
            mode = oct(path.stat().st_mode & 0o777)
        return exists and length == 48, f"{length} chars {mode}".strip()

    def database():
        conn = db.connect()
        try:
            status = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tasks = conn.execute("SELECT COUNT(*) FROM task").fetchone()[0]
            runs = conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]
        finally:
            conn.close()
        return status == "ok", f"{status}, tasks={tasks} runs={runs}"

    def agent_readiness():
        secrets = config.load_secrets()
        table = providers.load()
        ready, missing = [], []
        for route in routes.load().values():
            spec = agents.get(route.agent)
            gap = list(routes.problems(route))
            if spec is not None:
                for name in route.providers:
                    provider = table.get(name)
                    if provider is not None:
                        gap.extend(agents.missing(spec, provider, secrets))
            if gap:
                missing.append(f"{route.name}({','.join(gap)})")
            else:
                ready.append(route.name)
        detail = f"ready={','.join(ready) or '-'}"
        if missing:
            detail += f" gaps={','.join(missing)}"
        return bool(ready), detail

    def sessions():
        from ... import dsh_sessions

        found = dsh_sessions.session_files()
        return bool(found), f"{len(found)} files under {dsh_sessions.ROOT}"

    def secrets_check():
        keys = sorted(config.load_secrets())
        return bool(keys), ", ".join(keys) or "none"

    def disk():
        free = shutil.disk_usage(config.HOME).free / (1024**3)
        return free > 1.0, f"{free:.1f} GB free"

    def write_locks():
        rows = db.active_locks()
        if not rows:
            return True, "no directory is locked"
        detail = ", ".join(f"{r['cwd']}={r.get('run_id') or '?'}" for r in rows[:3])
        return True, f"{len(rows)} held: {detail}"

    for name, fn in (
        ("home", home),
        ("token", token),
        ("database", database),
        ("agents", agent_readiness),
        ("dsh sessions", sessions),
        ("secrets", secrets_check),
        ("write locks", write_locks),
        ("disk", disk),
    ):
        ok, _ = _check(name, fn)
        results.append(ok)
    print(f"\n{'ok' if all(results) else 'needs attention'}")
    return 0 if all(results) else 1


def probe(args) -> int:
    for r in telemetry.probe():
        mark = "ok" if r.get("ok") else "FAIL"
        print(
            f"{r['provider']:<10} {mark:<4} status={r.get('status')} "
            f"models={r.get('models', '-')} {r.get('latency_ms', '-')}ms "
            f"{r.get('error', '')}"
        )
    return 0


def status(args) -> int:
    latest = telemetry.last_probes()
    for name in sorted(providers.load()):
        r = latest.get(name)
        if not r:
            print(f"{name:<14} never probed")
            continue
        age = int((time.time() - r["ts"]) / 60)
        mark = "ok" if r.get("ok") else "FAIL"
        print(
            f"{name:<14} {mark:<4} {age}min ago  models={r.get('models', '-')} "
            f"{r.get('latency_ms', '-')}ms {r.get('error', '')}"
        )
    today = telemetry.usage_summary(1)
    total_in = sum(b["in"] for b in today.values())
    total_out = sum(b["out"] for b in today.values())
    print(
        f"today          runs={sum(b['runs'] for b in today.values())} "
        f"in={total_in} out={total_out}"
    )
    d = telemetry.decision_usage(1)
    print(f"jev today      calls={d['calls']} in={d['in']} out={d['out']}")
    b = telemetry.balance()
    if b:
        print(
            f"relay balance  站内余额 used={b['used_usd']:.2f} / limit={b['limit_usd']:.2f} "
            f"remaining={b['remaining_usd']:.2f}"
        )
    return 0


def balance(args) -> int:
    b = telemetry.balance()
    if not b:
        print("balance unavailable (relay does not expose billing endpoints)")
        return 1
    rate = config.cost_rate("relay_unit")
    print(
        f"{b['provider']}  站内余额 used={b['used_usd']:.2f} / limit={b['limit_usd']:.2f}  "
        f"remaining={b['remaining_usd']:.2f}  ≈ ¥{b['used_usd'] * rate:.2f} used"
    )
    print("单位说明：中转站的计价单位是“站内余额”，不是美元；换算比例来自 config.json 的 cost_rates")
    return 0


def usage(args) -> int:
    data = telemetry.usage_summary(args.days)
    if not data:
        print("(no runs in window)")
    metered = False
    for route, b in sorted(data.items()):
        money = " + ".join(
            f"{'¥' if c == 'CNY' else c + ' '}{v:.4f}" for c, v in sorted(b["costs"].items())
        )
        if money:
            labels = {
                "metered": "实测",
                "metered_request": "网关逐请求",
                "list_price": "价目表",
                "reported": "CLI 自报偏大",
            }
            money += "（" + "+".join(labels.get(x, x) for x in sorted(b["basis"])) + "）"
            metered = metered or "metered" in b["basis"]
        providers_used = ",".join(sorted(p for p in b["providers"] if p != "-")) or "-"
        fallback = f" fallback={b['fallbacks']}" if b["fallbacks"] else ""
        print(
            f"{route:<14} runs={b['runs']:<3} in={b['in']:<9} out={b['out']:<8} "
            f"cache_read={b['cache_read']:<9} failed={b['failed']} "
            f"via={providers_used}{fallback}  cost={money or 'n/a'}"
        )
    if metered:
        # A balance delta is account-wide: two runs in flight at once both see
        # the same spend, so summing over runs overstates. Say it where the
        # number is printed, not only in a doc nobody re-reads.
        print("- 标“实测”的成本来自中转站余额差：并发 run 的窗口重叠，合计是上界。")
    d = telemetry.decision_usage(args.days)
    print(
        f"{'jev':<14} calls={d['calls']:<2} in={d['in']:<9} out={d['out']:<8} "
        f"unavailable={d['unavailable']}"
    )
    return 0


def decisions_cmd(args) -> int:
    path = config.HOME / "decisions.jsonl"
    if not path.exists():
        print("(no decisions yet)")
        return 0
    lines = path.read_text(encoding="utf-8").strip().splitlines()[-args.last :]
    for line in lines:
        r = json.loads(line)
        parts = []
        for key, ans in (r.get("answers") or {}).items():
            value = ans.get("choice")
            if value is None:
                value = ans.get("score")
            if value is None:
                value = ans.get("noul")
            parts.append(f"{key}={value}")
        stamp = time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0)))
        print(f"{stamp}  {r.get('kind', '?'):<8} {' '.join(parts)}  ok={r.get('ok')}")
    return 0


def jev(args) -> int:
    ok, why = decision.available()
    print(("ready: " if ok else "not ready: ") + why)
    return 0 if ok else 1


def reconcile(args) -> int:
    rows = runner.RUNNER.reconcile(args.stale)
    for r in rows:
        print(f"{r['id']}  {r['agent']:<7} {r['status']} -> failed  ({r['error']})")
    print(f"{len(rows)} run(s) marked failed")
    return 0
