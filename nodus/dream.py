"""Read-only W1b dream preview; no extraction or adoption is performed."""
from __future__ import annotations

from collections import Counter
import json

from . import config, db


def dry_run(limit: int = 50) -> dict:
    if limit < 0:
        raise ValueError('limit must be nonnegative')
    workspace = config.workspace_id()
    with db.connect() as c:
        # W1a has no workspace column on batches; use a scoped cursor object.
        # Legacy numeric cursors belong to the current single-workspace DB.
        cursor = 0
        for batch in c.execute("SELECT cursor FROM dream_batch WHERE status IN ('staged','applied') ORDER BY started_at DESC, id DESC"):
            value = json.loads(batch['cursor'] or '0')
            if isinstance(value, dict):
                if value.get('workspace') != workspace:
                    continue
                value = value.get('seq', 0)
            cursor = int(value)
            break
        rows = [dict(r) for r in c.execute(
            'SELECT rowid AS seq, * FROM memory_episode WHERE workspace=? AND rowid>? ORDER BY rowid LIMIT ?',
            (workspace, cursor, limit),
        )]
    groups = Counter((r['route'] or '', r['verdict'], r['cause'] or '') for r in rows)
    stats = [dict(route=route, verdict=verdict, cause=cause, count=count)
             for (route, verdict, cause), count in sorted(groups.items())]
    proposals = [dict(**g, text=f"Review verified runs on route {g['route'] or '-'} for a reusable lesson.")
                 for g in stats if g['verdict'] == 'pass'][:5]
    return dict(dry_run=True, workspace=workspace, scanned=len(rows),
                cursor_from=cursor, cursor_to=rows[-1]['seq'] if rows else cursor,
                groups=stats, proposals=proposals)
