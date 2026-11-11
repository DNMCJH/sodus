"""Local episode replay and read-only dream preview."""
import json

from ... import dream, episodes


def add(sub):
    p = sub.add_parser('dream', help='preview episode consolidation without writing')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--limit', type=int, default=50)
    p.add_argument('--json', action='store_true', dest='as_json')
    p.set_defaults(func=dream_cmd, needs_daemon=False)
    p = sub.add_parser('replay-episodes', help='rebuild missing run episodes')
    p.add_argument('--since')
    p.add_argument('--limit', type=int)
    p.add_argument('--json', action='store_true', dest='as_json')
    p.set_defaults(func=replay_cmd, needs_daemon=False)


def dream_cmd(args):
    if not args.dry_run:
        raise ValueError('dream execution is not implemented; use --dry-run')
    result = dream.dry_run(args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=None if args.as_json else 2))


def replay_cmd(args):
    result = episodes.replay_episodes(args.since, args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=None if args.as_json else 2))
