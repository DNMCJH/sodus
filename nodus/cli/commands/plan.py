"""Plan validation, generation, independent review and explicit approval."""

import json
from pathlib import Path
from ... import plan as service


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
        if args.plan_action in ("new", "review", "approve"):
            if args.plan_action == "new":
                # A local CLI process must remain alive to collect the result.
                # Async requests are owned by the daemon instead.
                if not args.wait:
                    from ..client import call, ensure_daemon
                    ensure_daemon()
                    value = call("POST", "/plan", {"goal": args.goal, "cwd": str(Path(args.cwd).resolve()),
                                    "acceptance": args.accept, "route": args.route})
                else:
                    value = service.create(args.goal, args.cwd, args.accept, args.route)
            elif args.plan_action == "review":
                value = service.review(args.id)
            else:
                value = service.approve(args.id, args.by)
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return int(value.get("status") == "failed")
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
    except (OSError, ValueError, KeyError) as exc:
        print(
            json.dumps(
                {"valid": False, "errors": [{"kind": "input", "message": str(exc)}]},
                ensure_ascii=False,
            )
        )
        return 1
