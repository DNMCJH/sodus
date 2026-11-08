"""Local plan commands: no daemon or model required."""

import json
from ... import plan as service


def add(sub):
    p = sub.add_parser("plan", help="validate, import and inspect plans")
    p.set_defaults(func=run, needs_daemon=False)
    actions = p.add_subparsers(dest="plan_action", required=True)
    for action in ("validate", "import", "show", "list"):
        child = actions.add_parser(action)
        if action in ("validate", "import"):
            child.add_argument("file")
            child.add_argument("--cwd", default=".")
        if action == "show":
            child.add_argument("id")
        child.add_argument("--json", action="store_true")


def run(args):
    try:
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
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {"valid": False, "errors": [{"kind": "input", "message": str(exc)}]},
                ensure_ascii=False,
            )
        )
        return 1
