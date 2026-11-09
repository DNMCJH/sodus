"""Plan validation, generation, independent review and explicit approval."""

import json
import os
import sys
import time
from pathlib import Path
from ... import config
from ... import plan as service
from .. import client


def add(sub):
    p = sub.add_parser("plan", help="validate, import and inspect plans")
    p.set_defaults(func=run, needs_daemon=False)
    actions = p.add_subparsers(dest="plan_action", required=True)
    for action in ("validate", "import", "show", "list", "new", "review", "approve"):
        child = actions.add_parser(action)
        if action in ("validate", "import"):
            child.add_argument("file")
            child.add_argument("--cwd", default=".")
        if action in ("show", "review", "approve"):
            child.add_argument("id")
        if action == "approve":
            child.add_argument("--by", required=True, choices=["human"])
        if action == "new":
            child.add_argument("goal")
            child.add_argument("--cwd", default=".")
            child.add_argument("--accept", default="")
            child.add_argument("--route")
            group = child.add_mutually_exclusive_group()
            group.add_argument("--wait", dest="wait", action="store_true", default=True)
            group.add_argument("--no-wait", dest="wait", action="store_false")
        child.add_argument("--json", action="store_true")


def run(args):
    try:
        if args.plan_action == "approve":
            if os.environ.get("NODUS_ROLE") == "worker" or os.environ.get("NODUS_RUN") == "1":
                raise ValueError("approval is not allowed for worker/run processes")
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise ValueError("approval requires an interactive terminal")
            row = service.get_plan(args.id)
            if row is None:
                raise ValueError("plan not found: " + args.id)
            summary = {"id": row["id"], "status": row["status"],
                       "review_verdict": (row.get("review_json") or {}).get("verdict"),
                       "plan_hash": (row.get("hash") or "")[:12]}
            print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)
            print("Type the plan id to confirm approval: ", end="", flush=True, file=sys.stderr)
            try:
                confirmation = input()
            except EOFError:
                raise ValueError("approval cancelled: end of input") from None
            if confirmation != args.id:
                raise ValueError("approval cancelled: plan id did not match")
            value = service.approve(args.id, "human")
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        if args.plan_action in ("new", "review"):
            client.ensure_daemon()
            if args.plan_action == "new":
                deadline = time.monotonic() + config.plan_timeout() + 30
                value = client.call("POST", "/plan", {"goal": args.goal, "cwd": str(Path(args.cwd).resolve()),
                                    "acceptance": args.accept, "route": args.route})
                if args.wait:
                    while value.get("status") not in ("validated", "failed"):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            value = {**value, "status": "failed", "errors": [{"kind": "timeout", "message": "CLI planning deadline exceeded"}]}
                            break
                        time.sleep(min(0.5, remaining))
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            continue
                        try:
                            value = client.call("GET", "/plans/" + value["id"], retries=0, timeout=min(30, remaining))
                        except (TimeoutError, RuntimeError) as exc:
                            value = {**value, "status": "failed", "errors": [{"kind": "timeout", "message": str(exc)}]}
                            break
            elif args.plan_action == "review":
                deadline = time.monotonic() + config.review_timeout() + 30
                value = client.call("POST", "/plans/" + args.id + "/review", {})
                while value.get("status") == "reviewing":
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        value = {**value, "status": "review_failed", "errors_json": json.dumps([{"kind": "timeout", "message": "CLI review deadline exceeded"}])}
                        break
                    time.sleep(min(0.5, remaining))
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        continue
                    value = client.call("GET", "/plans/" + args.id, retries=0, timeout=min(30, remaining))
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return int(value.get("status") in ("failed", "review_failed"))
        if args.plan_action in ("validate", "import"):
            value = service.load(args.file)
            errors = service.validate_plan(value, args.cwd)
            if args.plan_action == "validate" or errors:
                print(
                    json.dumps(
                        {"valid": not errors, "errors": errors},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return int(bool(errors))
            print(service.import_plan(value, args.cwd))
        elif args.plan_action == "show":
            value = service.get_plan(args.id)
            if value is None:
                print("plan not found")
                return 1
            print(json.dumps(value, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(service.list_plans(), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(
            json.dumps(
                {"valid": False, "errors": [{"kind": "input", "message": str(exc)}]},
                ensure_ascii=False,
            )
        )
        return 1
