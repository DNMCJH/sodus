"""Delivery pipeline commands: verify, tdd, review, fix, report.

Only `verify` and `report` run in this process. `tdd` / `review` / `fix` are
posted to the daemon as jobs, because a pipeline is a sequence of runs and used
to die with the terminal that started it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ... import config, report, verify
from ..client import call, fmt_event


def add(sub) -> None:
    p = sub.add_parser("verify", help="run the detected test command")
    p.add_argument("--cwd", default=".")
    p.add_argument("--command", default="")
    p.set_defaults(func=verify_cmd)

    p = sub.add_parser("tdd", help="red -> green pipeline, optionally reviewed")
    p.add_argument("task")
    p.add_argument("--cwd", default=".")
    p.add_argument("--writer", default="claude")
    p.add_argument("--impl", default="codex")
    p.add_argument("--test", default="")
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--review", action="store_true", help="审查后再收尾")
    p.add_argument("--reviewer", default="", help="指定审查者，默认选另一家")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=tdd_cmd)

    p = sub.add_parser("review", help="cross-agent review of a run")
    p.add_argument("run")
    p.add_argument("--by", default="")
    p.add_argument("--focus", default="")
    p.add_argument("--scope", choices=["task", "run"], default="task")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=review_cmd)

    p = sub.add_parser("audit", help="batch review multiple runs")
    p.add_argument("runs", nargs="*", help="run IDs to review (or use --since)")
    p.add_argument("--by", default="", help="reviewer route")
    p.add_argument("--since", default="", help="review runs from last N days (e.g., 7d)")
    p.add_argument("--task", default="", help="only review runs for this task")
    p.add_argument("--failed-only", action="store_true", help="only review failed runs")
    p.set_defaults(func=audit_cmd)

    p = sub.add_parser("fix", help="review -> fix -> verify -> re-review")
    p.add_argument("run")
    p.add_argument("--by", default="")
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--review-first", action="store_true")
    p.set_defaults(func=fix_cmd)

    p = sub.add_parser("report", help="one-page task report")
    p.add_argument("target", help="task-id 或 run-id")
    p.add_argument("--out", default="")
    p.add_argument("--print", action="store_true", dest="show_md")
    p.set_defaults(func=report_cmd)

    p = sub.add_parser("pipelines", help="daemon-run pipelines (tdd/review/fix)")
    p.add_argument("--status", default="")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=pipelines)


def verify_cmd(args) -> int:
    cwd = Path(args.cwd).resolve()
    if args.command:
        command = args.command.split()
        reason = "explicit --command"
        kind = "custom"
    else:
        found = verify.detect(cwd)
        if not found:
            print(f"no test runner detected in {cwd}")
            return 2
        command, reason, kind = found["cmd"], found["reason"], found["kind"]
    result = verify.run(command, cwd)
    mark = "PASS" if result["ok"] else "FAIL"
    print(
        f"{mark}  exit={result['exit']}  {result['duration_ms']}ms  "
        f"[{kind}] {' '.join(command)}"
    )
    print(f"  detected by: {reason}")
    if not result["ok"]:
        print(result["tail"])
    return 0 if result["ok"] else 1


# ---- daemon-side pipelines --------------------------------------------

TERMINAL = ("completed", "failed", "stopped")


def _wait(pid: str) -> dict:
    """Follow a pipeline and every run it starts, until it closes."""
    cursor = 0
    children: dict[str, int] = {}
    while True:
        row = call("GET", f"/pipelines/{pid}")
        for ev in call("GET", f"/pipelines/{pid}/events?after={cursor}"):
            cursor = max(cursor, ev["seq"])
            if ev["type"] == "pipeline.child":
                rid = ev["payload"].get("run_id")
                if rid and rid not in children:
                    children[rid] = 0
                    print(f"  · run {rid} started", flush=True)
        for rid, after in list(children.items()):
            for ev in call("GET", f"/runs/{rid}/events?after={after}"):
                children[rid] = max(children[rid], ev["seq"])
                line = fmt_event(ev)
                if line is not None:
                    print(line, flush=True)
        if row.get("status") in TERMINAL:
            return row
        time.sleep(0.5)


def _submit(kind: str, params: dict, label: str = "") -> dict:
    row = call("POST", "/pipelines", {"kind": kind, "params": params, "label": label})
    print(f"pipeline {row['id']} ({kind}) — 跑在 daemon 里，关终端也不会断", flush=True)
    done = _wait(row["id"])
    if done.get("error"):
        print(f"  {done['error']}")
    try:
        return {"row": done, "result": json.loads(done.get("result_json") or "{}")}
    except json.JSONDecodeError:
        return {"row": done, "result": {}}


def tdd_cmd(args) -> int:
    res = _submit(
        "tdd",
        {
            "task_id": args.task,
            "cwd": str(Path(args.cwd).resolve()),
            "writer": args.writer,
            "impl": args.impl,
            "test_cmd": args.test,
            "retries": args.retries,
            "dry_run": args.dry_run,
            "review": args.review,
            "reviewer": args.reviewer,
        },
        label=args.task,
    )["result"]
    if not res:
        return 1
    if res.get("cmd"):
        print(f"test: {' '.join(res['cmd'])}  ({res.get('reason')})")
    if res.get("stage") == "dry_run":
        print("plan:")
        for step in res["planned"]:
            print(f"  {step}")
        return 0
    print(f"stage={res.get('stage')}  ok={res.get('ok')}")
    if res.get("red"):
        print(f"  red: exit={res['red']['exit']} (must be non-zero)")
        if res.get("red_artifact"):
            print(f"       {res['red_artifact']}")
    if res.get("green"):
        print(f"  green: exit={res['green']['exit']}")
        if res.get("green_artifact"):
            print(f"         {res['green_artifact']}")
    if res.get("tests_changed"):
        print(f"  WARNING: test files changed during implementation: {res['tests_changed']}")
    if res.get("error"):
        print(f"  {res['error']}")
    rev = res.get("review")
    if rev:
        if not rev.get("ok"):
            print(f"  review failed: {rev.get('error')}")
            print(f"    run={rev.get('review_run')}")
        else:
            counts = rev.get("counts") or {}
            print(
                f"  review: verdict={rev.get('verdict')} by={rev.get('reviewer')} "
                f"critical={counts.get('critical', 0)} high={counts.get('high', 0)} "
                f"medium={counts.get('medium', 0)} low={counts.get('low', 0)}"
            )
            for finding in rev.get("findings") or []:
                loc = finding["file"] or "?"
                if finding.get("line"):
                    loc += f":{finding['line']}"
                print(f"    [{finding['severity'].upper()}] {loc}  {finding['issue']}")
            if rev.get("review_run"):
                print(f"    detail: {config.HOME / 'runs' / rev['review_run'] / 'review.md'}")
    if not res.get("ok"):
        return 1
    if rev and (not rev.get("ok") or rev.get("verdict") != "accept"):
        return 2
    return 0


def review_cmd(args) -> int:
    res = _submit(
        "review",
        {
            "target_run_id": args.run,
            "by": args.by,
            "focus": args.focus,
            "scope": args.scope,
            "dry_run": args.dry_run,
        },
        label=args.run,
    )["result"]
    if not res:
        return 1
    if args.dry_run:
        print(
            f"dry-run: reviewer={res['reviewer']} target={res['target_run']} "
            f"mode={res['context_mode']} chars={res['context_chars']} "
            f"note={res['note_chars']} truncated={res['context_truncated']}"
        )
        print(f"  files: {', '.join(res['context_files']) or '(none)'}")
        return 0
    if not res.get("ok"):
        print(f"review failed: {res.get('error')}")
        print(f"  run={res.get('review_run')}")
        return 1
    counts = res.get("counts") or {}
    print(
        f"review {res['review_run']} by {res['reviewer']}  verdict={res['verdict']}  "
        f"critical={counts.get('critical', 0)} high={counts.get('high', 0)} "
        f"medium={counts.get('medium', 0)} low={counts.get('low', 0)}"
    )
    if res.get("summary"):
        print(f"  {res['summary']}")
    for finding in res.get("findings") or []:
        loc = finding["file"] or "?"
        if finding.get("line"):
            loc += f":{finding['line']}"
        print(f"  [{finding['severity'].upper():<8}] {loc}  {finding['issue']}")
    if res.get("test_gaps"):
        print("  test gaps:")
        for gap in res["test_gaps"]:
            print(f"    - {gap}")
    print(f"  detail: {config.HOME / 'runs' / res['review_run'] / 'review.md'}")
    clean = res["verdict"] == "accept" and counts.get("critical", 0) == 0
    return 0 if clean else 2


def fix_cmd(args) -> int:
    res = _submit(
        "fix",
        {
            "target_run_id": args.run,
            "by": args.by,
            "rounds": args.rounds,
            "review_first": args.review_first,
        },
        label=args.run,
    )["result"]
    if not res:
        return 2
    before = res.get("review_before") or {}
    print(
        f"fix target={res.get('target_run')} agent={res.get('agent')} "
        f"fixer={res.get('fixer')}"
    )
    print(
        f"  review before: {before.get('verdict')} by {before.get('reviewer')} "
        f"({before.get('review_run')})"
    )
    if res.get("error"):
        print(f"  {res['error']}")
    for r in res.get("rounds") or []:
        line = (
            f"  round {r['round']}: fix={r['fix_run']} "
            f"verify={'PASS' if r['verify']['ok'] else 'FAIL'}"
        )
        if r.get("review_verdict"):
            line += f" re-review={r['review_verdict']}"
        if r.get("tests_changed"):
            line += f" tests_changed={r['tests_changed']}"
        if r.get("error"):
            line += f" error={r['error']}"
        print(line)
    after = res.get("review_after") or {}
    print(
        f"  review after: {after.get('verdict')} by {after.get('reviewer')} "
        f"({after.get('review_run')})"
    )
    if after.get("review_run"):
        print(f"  detail: {config.HOME / 'runs' / after['review_run'] / 'review.md'}")
    return 0 if res.get("ok") else 2


def pipelines(args) -> int:
    rows = call("GET", f"/pipelines?status={args.status}")[: args.limit]
    if not rows:
        print("(no pipelines)")
        return 0
    for row in rows:
        stamp = time.strftime("%m-%d %H:%M", time.localtime(row.get("created_at") or 0))
        print(
            f"{row['id']:<22} {row['kind']:<7} {row['status']:<10} {stamp}  "
            f"{(row.get('label') or '')[:28]}"
        )
    return 0


def report_cmd(args) -> int:
    task_id = args.target
    if not task_id.startswith("task-"):
        task_id = call("GET", f"/runs/{args.target}").get("task_id", "")
        if not task_id:
            print(f"unknown run: {args.target}")
            return 1
    path, data = report.write_report(task_id, args.out)
    if args.show_md:
        print(path.read_text(encoding="utf-8"))
    print(path)
    print(
        f"runs={len(data['runs'])} verifies={len(data['verifies'])} "
        f"reviews={len(data['reviews'])} decisions={len(data['decisions'])}"
    )
    for flag in data["flags"]:
        print(f"  - {flag}")
    return 0


def audit_cmd(args) -> int:
    """Review multiple runs based on criteria."""
    runs_to_review = []

    # If explicit run IDs provided, use those
    if args.runs:
        runs_to_review = args.runs
    else:
        # Query for recent runs based on filters
        try:
            # Get recent runs from daemon
            all_runs = call("GET", "/runs")

            # Apply filters
            filtered = all_runs
            if args.since:
                # Parse "7d" into seconds
                if args.since.endswith("d"):
                    days = int(args.since[:-1])
                    cutoff = time.time() - (days * 86400)
                    filtered = [r for r in filtered if r.get("started_at", 0) > cutoff]

            if args.task:
                filtered = [r for r in filtered if r.get("task_id") == args.task]

            if args.failed_only:
                filtered = [r for r in filtered if r.get("status") == "failed"]
            else:
                # Only review completed runs by default
                filtered = [r for r in filtered if r.get("status") == "completed"]

            runs_to_review = [r["id"] for r in filtered]
        except Exception as e:
            print(f"Failed to query runs: {e}")
            return 1

    if not runs_to_review:
        print("No runs to audit")
        return 0

    print(f"Auditing {len(runs_to_review)} run(s)...")

    results = []
    for run_id in runs_to_review:
        print(f"\n→ Reviewing {run_id}...", flush=True)
        try:
            res = _submit("review", {
                "target_run_id": run_id,
                "by": args.by,
                "focus": "periodic audit",
                "scope": "task",
                "dry_run": False,
            }, label=run_id)["result"]
            results.append({"run_id": run_id, **res})
        except Exception as e:
            print(f"  Failed: {e}")
            results.append({"run_id": run_id, "ok": False, "error": str(e)})

    # Print summary
    print("\n" + "=" * 70)
    print("AUDIT SUMMARY")
    print("=" * 70)

    total = len(results)
    successful = sum(1 for r in results if r.get("ok"))
    accepted = sum(1 for r in results if r.get("verdict") == "accept")

    print(f"Total runs audited: {total}")
    print(f"Reviews completed: {successful} / {total}")
    print(f"Verdict 'accept': {accepted} / {successful}")

    # Count findings by severity
    all_findings = []
    for r in results:
        if r.get("findings"):
            all_findings.extend(r["findings"])

    if all_findings:
        critical = sum(1 for f in all_findings if f.get("severity") == "critical")
        high = sum(1 for f in all_findings if f.get("severity") == "high")
        medium = sum(1 for f in all_findings if f.get("severity") == "medium")
        low = sum(1 for f in all_findings if f.get("severity") == "low")

        print(f"\nFindings: critical={critical} high={high} medium={medium} low={low}")

        if critical > 0 or high > 0:
            print("\nCritical/High findings:")
            for r in results:
                for f in r.get("findings") or []:
                    if f.get("severity") in ("critical", "high"):
                        print(f"  [{f['severity'].upper()}] {r['run_id']} - {f.get('file', '?')}:{f.get('line', '?')} - {f['issue']}")

    return 0 if accepted == successful else 1
