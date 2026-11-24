"""Explicit local project-console commands, independent of daemon startup."""
from __future__ import annotations

import json
from ... import handoff


def add(sub):
    parser = sub.add_parser('handoff', help='project handoff console & decision inbox')
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

    draft = commands.add_parser('decide-draft', help='draft a project-level decision (D-xxx)')
    draft.add_argument('title', help='one-sentence decision question')
    draft.add_argument('--why', default='', help='why this requires human judgment')
    draft.add_argument('--background', default='', help='brief context')
    draft.add_argument('--option', action='append', default=[], help='option with consequence/cost (repeatable)')
    draft.add_argument('--recommend', default='', help='recommended option and reason')
    draft.add_argument('--evidence', default='', help='changeset verify/review summary link')
    draft.add_argument('--wait-cost', default='', help='cost of delaying this decision')
    draft.add_argument('--cwd', default=None)
    draft.add_argument('--json', action='store_true')
    draft.set_defaults(func=execute, needs_daemon=False)

    close = commands.add_parser('decide-close', help='record a decision and archive D-xxx')
    close.add_argument('id', help='decision id, e.g. D-001')
    close.add_argument('--decision', required=True, help='chosen outcome')
    close.add_argument('--reason', default='', help='rationale or basis')
    close.add_argument('--status', choices=('decided', 'deferred'), default='decided')
    close.add_argument('--unnecessary', action='store_true', help='mark that this did not need to ask you')
    close.add_argument('--cwd', default=None)
    close.add_argument('--json', action='store_true')
    close.set_defaults(func=execute, needs_daemon=False)

    decisions = commands.add_parser('decisions', help='list pending decisions (current or --all projects)')
    decisions.add_argument('--all', dest='all_projects', action='store_true', help='aggregate across known projects')
    decisions.add_argument('--status', choices=('open', 'deferred', 'all'), default='open')
    decisions.add_argument('--cwd', default=None)
    decisions.add_argument('--json', action='store_true')
    decisions.set_defaults(func=execute, needs_daemon=False)

    reg = commands.add_parser('register', help='register a project in ~/.sodus/projects.json')
    reg.add_argument('path', nargs='?', default=None, help='project directory (default: cwd)')
    reg.add_argument('--name', default=None, help='optional display name')
    reg.add_argument('--json', action='store_true')
    reg.set_defaults(func=execute, needs_daemon=False)

    unreg = commands.add_parser('unregister', help='remove a project from ~/.sodus/projects.json')
    unreg.add_argument('path', nargs='?', default=None, help='project directory (default: cwd)')
    unreg.add_argument('--json', action='store_true')
    unreg.set_defaults(func=execute, needs_daemon=False)


def execute(args):
    name = args.handoff_cmd
    try:
        if name == 'init':
            result = handoff.init(force=args.force)
        elif name == 'append':
            result = handoff.append(args.text, args.section)
        elif name == 'list':
            result = handoff.list_projects()
        elif name == 'register':
            result = handoff.register_project(args.path, name=args.name)
        elif name == 'unregister':
            result = handoff.unregister_project(args.path)
        elif name == 'decide-draft':
            result = handoff.decide_draft(
                args.title,
                why=args.why,
                background=args.background,
                options=args.option,
                recommend=args.recommend,
                evidence=args.evidence,
                wait_cost=args.wait_cost,
                cwd=args.cwd,
            )
        elif name == 'decide-close':
            result = handoff.decide_close(
                args.id,
                decision=args.decision,
                reason=args.reason,
                status=args.status,
                unnecessary=args.unnecessary,
                cwd=args.cwd,
            )
        elif name == 'decisions':
            result = handoff.list_decisions(
                cwd=args.cwd,
                all_projects=args.all_projects,
                status=args.status,
            )
        else:
            result = getattr(handoff, name)()
    except OSError as exc:
        raise ValueError(str(exc)) from exc

    if name == 'show' and not args.json:
        print(f"{result['path']}\nsnapshot_at: {result['snapshot_at']}\nHEAD: {result['head']}\n\n{result['content']}")
    elif name == 'decisions' and not args.json:
        rows = result['decisions']
        if not rows:
            print(f"没有待拍板决策（扫描 {result['projects_scanned']} 个项目）。")
        else:
            for item in rows:
                rec = f" | 推荐：{item['recommend']}" if item.get('recommend') else ''
                print(f"[{item['project']}] {item['id']} ({item['status']}) {item['title']}{rec}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if name == 'check' and not result['ok'] else 0
