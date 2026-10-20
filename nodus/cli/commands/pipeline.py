"""Delivery pipeline commands: verify, tdd, review, fix, report."""

from __future__ import annotations

from pathlib import Path

from ... import config, fix, report, review, tdd, verify
from ..client import call


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


def tdd_cmd(args) -> int:
    res = tdd.run_tdd(
        args.task,
        Path(args.cwd),
        writer=args.writer,
        impl=args.impl,
        test_cmd=args.test,
        retries=args.retries,
        dry_run=args.dry_run,
        review=args.review,
        reviewer=args.reviewer,
    )
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
    res = review.run_review(
        args.run,
        by=args.by,
        focus=args.focus,
        scope=args.scope,
        dry_run=args.dry_run,
    )
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
    res = fix.run_fix(
        args.run,
        by=args.by,
        rounds=args.rounds,
        review_first=args.review_first,
    )
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
