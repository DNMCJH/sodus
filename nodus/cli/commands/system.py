"""Machine and account commands: serve, agents, doctor, probe, status, stats."""

from __future__ import annotations

import json
import os
import shutil
import time
import unicodedata
from pathlib import Path

from ... import (
    agents,
    config,
    db,
    decision,
    daemon,
    desk as desk_view,
    ledger,
    policy,
    providers,
    routes,
    runner,
    server,
    telemetry,
    usage as usage_stats,
)
from ..client import call


def add(sub) -> None:
    p = sub.add_parser("serve", help="run the HTTP daemon in the foreground")
    p.add_argument("--port", type=int, default=4199)
    p.set_defaults(func=serve, needs_daemon=False)

    p = sub.add_parser("mcp", help="MCP server over stdio (for Claude Code / DSH)")
    p.set_defaults(func=mcp_cmd, needs_daemon=False)

    p = sub.add_parser("up", help="start the daemon detached; survives this shell")
    p.add_argument("--port", type=int, default=daemon.DEFAULT_PORT)
    p.add_argument("--wait", type=float, default=20.0, help="seconds to wait for /health")
    p.set_defaults(func=up_cmd, needs_daemon=False)

    p = sub.add_parser("down", help="stop the daemon this machine's state file points at")
    p.set_defaults(func=down_cmd, needs_daemon=False)

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

    p = sub.add_parser(
        "usage",
        help="token 用量与成本：按日/月/全部，按 route、model、provider、agent 汇总",
    )
    p.add_argument(
        "--period",
        choices=usage_stats.PERIODS,
        default=None,
        help="day|month|all；默认 month（本月）",
    )
    p.add_argument(
        "--by",
        choices=usage_stats.GROUP_BYS,
        default=None,
        help="route|model|provider|agent；默认 route",
    )
    p.add_argument("--json", action="store_true", dest="as_json", help="机器可读的完整数据")
    p.add_argument(
        "--days",
        type=float,
        default=None,
        help="旧模式：最近 N 天按 route 汇总（输出与旧版一致）",
    )
    p.set_defaults(func=usage, needs_daemon=False)

    p = sub.add_parser("decisions", help="recent Jev decisions")
    p.add_argument("--last", type=int, default=10)
    p.set_defaults(func=decisions_cmd)

    p = sub.add_parser("jev", help="is the Jev key configured")
    p.set_defaults(func=jev)

    p = sub.add_parser(
        "route-for",
        help="assess a task and recommend one of the six routes (does not dispatch)",
    )
    p.add_argument("task", help="the task text, in quotes")
    p.add_argument("--json", action="store_true", help="print the assessment and recommendation as JSON")
    p.add_argument(
        "--apply",
        action="store_true",
        help="print the dispatch command for the recommended route (no task is created)",
    )
    p.set_defaults(func=route_for, needs_daemon=False)

    p = sub.add_parser("reconcile", help="fail runs whose owner died mid-run")
    p.add_argument("--stale", type=int, default=900)
    p.set_defaults(func=reconcile)

    p = sub.add_parser("desk", help="render a markdown progress page (for a vault dashboard)")
    p.add_argument("--out", help="write to this path; default prints to stdout")
    p.add_argument("--limit", type=int, default=15, help="how many tasks (default 15)")
    p.set_defaults(func=desk, needs_daemon=False)

    p = sub.add_parser("locks", help="which working directories have a writer")
    p.set_defaults(func=locks, needs_daemon=False)

    p = sub.add_parser(
        "cost", help="measured per-token prices, and how they compare to the bill"
    )
    p.add_argument("--runs", type=int, default=10, help="how many recent runs to compare")
    p.set_defaults(func=cost, needs_daemon=False)

    p = sub.add_parser("unlock", help="force-release a directory a dead run left locked")
    p.add_argument("cwd")
    p.set_defaults(func=unlock, needs_daemon=False)


def cost(args) -> int:
    table = providers.all_calibrations()
    if not table:
        print("(no calibration; costs come from the account meter, which is shared)")
        return 0
    rate = config.cost_rate("relay_unit")
    for model, cal in sorted(table.items()):
        print(f"{model}  measured {cal.measured}")
        print(f"  per output token       {cal.per_output:.4e}")
        print(f"  per cache_read token   {cal.per_cache_read:.4e}")
        print(f"  per cache_write token  {cal.per_cache_write:.4e}")
        print(f"  per call               {cal.per_call:.4e}")
    print(f"单位：站内余额；× cost_rate(relay_unit)={rate} = CNY")

    # One bucket per currency; money that recorded no currency is grouped by
    # provider instead. The drift is (bill - calibration) / calibration, and
    # the bill side is CNY by construction (`_price` multiplies the meter's
    # relay units by `cost_rate`, which is a CNY rate). So only the CNY bucket
    # can be compared: converting a dollar amount to reach the bill would be
    # the invented exchange rate this command exists to avoid printing.
    buckets: dict[str, dict] = {}
    unlabeled: dict[str, dict] = {}
    counted = 0
    for run in db.list_runs(limit=200):
        parsed = ledger.parse(run.get("usage_json"))
        if parsed.cost.basis != "calibrated":
            continue
        observed = parsed.raw.get("meter_observed")
        if not ledger.number(observed):
            continue
        amount = parsed.cost.amount
        if amount is None:
            continue
        currency = ledger.currency_of(parsed)
        if currency == ledger.UNKNOWN_CURRENCY:
            bucket = unlabeled.setdefault(
                str(run.get("provider") or "").strip() or ledger.UNKNOWN_CURRENCY,
                {"calibrated": 0.0, "runs": 0},
            )
        else:
            bucket = buckets.setdefault(currency, {"calibrated": 0.0, "observed": 0.0, "runs": 0})
            bucket["observed"] += float(observed)
        bucket["calibrated"] += float(amount)
        bucket["runs"] += 1
        counted += 1
        if counted >= args.runs:
            break

    mixed = len(buckets) + len(unlabeled) > 1
    for currency, bucket in sorted(buckets.items()):
        scope = f"（{currency}）" if mixed else ""
        if currency != "CNY":
            print(
                f"\n最近 {bucket['runs']} 个已标定的 run{scope}："
                f"标定合计 {bucket['calibrated']:.4f}；账单侧只有 CNY，不换算、不比较"
            )
            continue
        calibrated, observed = bucket["calibrated"], bucket["observed"]
        drift = (observed - calibrated) / calibrated * 100 if calibrated else 0.0
        print(
            f"\n最近 {bucket['runs']} 个已标定的 run{scope}：标定合计 ¥{calibrated:.4f}，"
            f"账单合计 ¥{observed:.4f}（{drift:+.1f}%）"
        )
        print("账单那侧含并发重叠，只作漂移参考；持续偏离说明 relay 调价了，重跑标定。")
    for provider, bucket in sorted(unlabeled.items()):
        amount = f"标定合计 {bucket['calibrated']:.4f}；账单侧只有 CNY，不换算、不比较"
        print(
            "\n"
            + ledger.unlabeled_line(
                {"provider": provider, "runs": bucket["runs"]}, amount
            )
        )
    return 0


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


def mcp_cmd(args) -> int:
    # Imported here so `nodus --help` and every other command stay light; the
    # MCP server never needs the daemon, which is why it sets needs_daemon=False.
    from ... import mcp as mcp_server

    return mcp_server.serve()


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


def up_cmd(args) -> int:
    """Start the daemon detached and say where it is."""
    result = daemon.start(port=args.port, wait=args.wait)
    if not result.get("ok"):
        print(f"FAIL  {result.get('error', 'unknown error')}")
        if result.get("log"):
            print(f"      log: {result['log']}")
        return 1
    verb = "already up" if result.get("already") else "up"
    print(f"PASS  {verb}  pid={result.get('pid')}  {result.get('url')}")
    return 0


def down_cmd(args) -> int:
    result = daemon.stop()
    if result.get("ok"):
        print(f"PASS  stopped  pid={result.get('pid')}")
        return 0
    print(f"FAIL  {result.get('note', 'daemon still answering')}")
    return 1


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

    def sessions_check():
        from ... import sessions as sessions_mod

        found = sessions_mod.scan()
        by_vendor: dict[str, int] = {}
        for session in found:
            by_vendor[session.vendor] = by_vendor.get(session.vendor, 0) + 1
        detail = ", ".join(f"{k}={v}" for k, v in sorted(by_vendor.items())) or "none"
        return bool(found), detail

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
        ("sessions", sessions_check),
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
    """Two modes: the old --days window, or the new period/dimension report.

    `--days` is kept bit-for-bit so anything that already parses this output
    keeps working; everything else uses `usage.summary`.
    """
    if args.days is not None:
        return _legacy_usage(args)
    data = usage_stats.summary(args.period or "month", args.by or "route")
    if args.as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    _print_usage(data)
    return 0


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _human_tokens(count: int) -> str:
    count = int(count or 0)
    for unit, size in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if count >= size:
            return f"{count / size:.1f}{unit}"
    return str(count)


def _money_text(cost: dict) -> str:
    parts = []
    for currency, bucket in sorted(cost.items()):
        total = bucket["total"]
        if currency == "CNY":
            # A fraction of a cent is not zero. One pet line costs a few tenths
            # of a fen, and `¥0.00` for a group that did spend money reads as
            # "free" — so below half a cent the fourth decimal is what's shown.
            shown = f"{total:.2f}"
            if total and shown == "0.00":
                shown = f"{total:.4f}"
            parts.append(f"¥{shown}")
        elif currency == usage_stats.UNKNOWN_CURRENCY:
            # Left as the old wording on purpose: `nodus stats` is not one of
            # the six displays this change reworks, and its unlabeled bucket is
            # still a single number (no provider split), which is the shape the
            # six no longer use. See docs/overlap-audit.md 1.4.
            parts.append(f"币种未记录 {total:.4f}")
        else:
            parts.append(f"{currency} {total:.4f}")
    return " + ".join(parts) or "还没计价"


def _mean_text(cost: dict) -> str:
    parts = []
    for currency, bucket in sorted(cost.items()):
        mean = bucket.get("mean")
        if mean is None:
            continue
        if currency == "CNY":
            parts.append(f"¥{mean:.4f}/次")
        elif currency == usage_stats.UNKNOWN_CURRENCY:
            parts.append(f"币种未记录 {mean:.4f}/次")
        else:
            parts.append(f"{currency} {mean:.4f}/次")
    return " · ".join(parts)


def _rate_text(rate: float | None) -> str:
    return "—" if rate is None else f"{rate * 100:.0f}%"


def _usage_row(name: str, bucket: dict) -> str:
    name = "（未记录）" if name == "-" else name
    return (
        f"{_pad(name, 26)}{bucket['runs']:>3} 次  "
        f"{_pad(_money_text(bucket['cost']), 20)} token "
        f"{_pad(_human_tokens(bucket['tokens']['total']), 8)}"
        f"（缓存命中 {_rate_text(bucket['cache_hit_rate'])}）"
    )


def _print_usage(data: dict) -> None:
    label = (data.get("window") or {}).get("label") or ""
    group_by = data.get("group_by") or "route"
    print(f"用量 · {label} · 按 {group_by}")
    for group in data["groups"]:
        print(_usage_row(group["key"], group))
    totals = data["totals"]
    print(_usage_row("合计", totals))
    if totals["unrecorded"]:
        print(
            f"{totals['unrecorded']} 个 run 没有 usage 记录：算次数，不算 0 元，也不进均值。"
        )
    unpriced = totals["runs"] - totals["unrecorded"] - totals["priced"]
    if unpriced:
        print(f"{unpriced} 个 run 有 usage 但没有金额记录。")
    means = _mean_text(totals["cost"])
    if means:
        print(f"均价（按已计价的 {totals['priced']} 次）：{means}")
    print("缓存命中 = cache_read / (prompt + cache_read + cache_write)，分母为 0 时显示 —。")


def _legacy_usage(args) -> int:
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
                "calibrated": "标定",
                "metered_request": "网关逐请求",
                "list_price": "价目表",
                "reported": "CLI 自报偏大",
                # `ledger`'s bucket for a priced run that did not record
                # `cost_basis` (the field postdates the oldest rows). It is not
                # "reported": that word means the CLI's own price book answered.
                "none": "未标注",
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


def stats_overview(args) -> int:
    """Today in five lines: runs, tokens, money, cache hit rate.

    Local database only. The relay balance is a network call that can take
    fifteen seconds to fail, so it stays behind `nodus stats balance`; an
    overview that hangs because a relay is down is not an overview.
    """
    data = usage_stats.summary("day", "route")
    totals = data["totals"]
    tokens = totals["tokens"]
    print(f"今日 {data['window']['label']}")
    print(
        f"  runs      {totals['runs']}（完成 {totals['completed']}，"
        f"没有 usage 记录 {totals['unrecorded']}）"
    )
    print(
        f"  token     {_human_tokens(tokens['total'])}  "
        f"输入 {_human_tokens(tokens['prompt'])} · 输出 {_human_tokens(tokens['completion'])} · "
        f"缓存读 {_human_tokens(tokens['cache_read'])} · 缓存写 {_human_tokens(tokens['cache_write'])}"
    )
    print(f"  花费      {_money_text(totals['cost'])}（已计价 {totals['priced']} 个 run）")
    print(f"  缓存命中  {_rate_text(totals['cache_hit_rate'])}")
    print()
    print(
        "明细  nodus stats usage --period day|month|all --by route|model|provider|agent · "
        "cost · bench · balance"
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


def route_for(args) -> int:
    """Judgement + suggestion, never a dispatch by itself.

    A route recommendation without a task id cannot be handed to `nodus run`,
    so `--apply` stops at printing the command a caller would use. Making the
    default mode read-only is more important than making the flag feel busy.
    """
    assessment = policy.assess(args.task)
    recommendation = policy.recommend(assessment)
    apply = {
        "dispatched": False,
        "route": recommendation["route"],
        "command": f"nodus run <task-id> --route {recommendation['route']}",
    }

    if args.json:
        payload = {"assess": assessment, "recommend": recommendation}
        if args.apply:
            payload["apply"] = apply
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    _print_assessment(assessment)
    _print_recommendation(recommendation)

    if args.apply:
        print()
        print(f"建议用 `--route {recommendation['route']}`：{apply['command']}")
        print("route-for 不创建任务、不派活；--apply 目前只输出这一步。")
    return 0


def _print_assessment(assessment: dict) -> None:
    needs = ", ".join(assessment.get("needs") or []) or "none"
    print("判断")
    print(
        f"  difficulty  {assessment['difficulty']}/5   "
        f"kind={assessment['kind']}   needs={needs}   "
        f"confidence={assessment['confidence']:.2f}"
    )
    if assessment.get("basis") == "rules+jev":
        jev = assessment.get("jev") or {}
        agreement = jev.get("agreement") or {}
        marks = " ".join(
            f"{name}={'agree' if agreement.get(name) else 'differ'}"
            for name in ("difficulty", "kind")
        )
        print(
            f"  basis       rules+jev   "
            f"({jev.get('model')}, {jev.get('latency_ms')}ms, {marks})"
        )
    else:
        jev = assessment.get("jev") or {}
        print(f"  basis       rules-only   ({jev.get('unavailable') or 'Jev unavailable'})")
    print(f"  signals     {', '.join(assessment.get('signals') or []) or '-'}")


def _print_recommendation(recommendation: dict) -> None:
    spec = recommendation.get("route_spec") or {}
    providers = " -> ".join(spec.get("providers") or []) or "-"
    evidence = recommendation.get("evidence") or {}
    history = evidence.get("history") or {}
    stats = history.get("stats") or {}
    scoring = recommendation.get("scoring") or {}
    print("\n建议")
    print(
        f"  route       {recommendation['route']}   "
        f"({spec.get('agent') or '?'} @ {providers})"
    )
    print(f"  为什么       {recommendation['rationale']}")
    if scoring.get("explain"):
        print(f"  打分        {scoring['explain']}")
    for rule in scoring.get("guardrails") or []:
        if rule.get("reason"):
            print(f"  护栏        {rule['reason']}")
    print(f"  证据        {evidence.get('status')} · {evidence.get('scope')}")
    if evidence.get("note"):
        print(f"              {evidence['note']}")
    if history.get("samples"):
        money = " ".join(
            f"{currency} {bucket['mean']:.6f}/run"
            for currency, bucket in sorted((stats.get("cost") or {}).items())
        )
        verified = stats.get("verified_samples") or 0
        proof = (
            f"有测试证据 {verified} 次（绿 {stats.get('verified_green')}）"
            if verified
            else "没有测试证据"
        )
        print(
            f"  本地历史     {stats.get('runs')} 次，完成 {stats.get('completed')}/"
            f"{stats.get('runs')}，{proof}，中位 {stats.get('median_s')}s"
            + (f"，成本 {money}" if money else "，没有成本记录")
        )
    print(f"  置信度       {recommendation['confidence']:.2f}")
    print(f"  无实测       {', '.join(recommendation.get('unmeasured') or []) or '-'}")
    scored = {row["route"]: row for row in scoring.get("candidates") or []}
    shown = 0
    print("  备选")
    for alt in recommendation.get("alternatives") or []:
        row = scored.get(alt["route"]) or {}
        if row.get("excluded"):
            # JSON keeps these (the evidence says why); text does not pretend
            # an unusable route is a real alternative.
            continue
        shown += 1
        value = f"{row['score']:>5.2f}" if row else "    -"
        print(f"    {alt['route']:<15} {value}  {alt['why']}")
    if not shown:
        print("    （没有可用备选：其余路由不满足这个任务的能力声明或不在本机）")


def reconcile(args) -> int:
    rows = runner.RUNNER.reconcile(args.stale)
    for r in rows:
        print(f"{r['id']}  {r['agent']:<7} {r['status']} -> failed  ({r['error']})")
    print(f"{len(rows)} run(s) marked failed")
    return 0
