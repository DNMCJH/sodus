"""Explicit local project-console commands, independent of daemon startup."""
import json
from ... import handoff


def add(sub):
    parser = sub.add_parser('handoff', help='project handoff console')
    commands = parser.add_subparsers(dest='handoff_cmd', required=True)
    for name in ('init', 'show', 'append', 'sync', 'check', 'list'):
        p = commands.add_parser(name)
        p.set_defaults(func=execute, needs_daemon=False)
        if name == 'init':
            p.add_argument('--force', action='store_true')
        if name == 'append':
            p.add_argument('text')
            p.add_argument('--section', default='最近变更')
        if name in ('show', 'check', 'list'):
            p.add_argument('--json', action='store_true')


def execute(args):
    name = args.handoff_cmd
    try:
        if name == 'init':
            result = handoff.init(force=args.force)
        elif name == 'append':
            result = handoff.append(args.text, args.section)
        elif name == 'list':
            result = handoff.list_projects()
        else:
            result = getattr(handoff, name)()
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    if name == 'show' and not args.json:
        print(f"{result['path']}\nsnapshot_at: {result['snapshot_at']}\nHEAD: {result['head']}\n\n{result['content']}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if name == 'check' and not result['ok'] else 0
