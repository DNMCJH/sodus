"""Rebuildable, deterministic summaries of terminal runs."""
from __future__ import annotations

from datetime import datetime
import json
import sqlite3

from . import config, db


def episode_id(run_id: str) -> str:
    return 'ep-' + run_id


def verdict_for(run: dict, verify_state: str | None = None) -> str:
    if run.get('status') != 'completed':
        return 'failed'
    if verify_state is None:
        verify_state = db.verify_states([run['id']]).get(run['id'], 'none')
    if verify_state in ('ok', 'green', 'pass', 'verify.green'):
        return 'pass'
    if verify_state in ('red', 'verify.red'):
        return 'red'
    return 'unverified'


def record_episode(run: dict, verify_state: str | None = None) -> str | None:
    """Return the inserted id, or None for a duplicate/nonterminal run.

    The primary key arbitrates concurrent finish/replay calls. Historical
    evidence is copied from persisted events, never rehashed from today's tree.
    """
    if run.get('status') not in ('completed', 'failed', 'stalled'):
        return None
    rid = run['id']
    mark = verify_state if verify_state is not None else db.verify_states([rid]).get(rid, 'none')
    verdict = verdict_for(run, mark)
    usage = json.loads(run.get('usage_json') or '{}')
    events = db.events_filtered(rid, ('verify.', 'diagnosis.', 'run.'))
    artifacts = db.artifacts_for(rid)
    files = [{'path': a['path'], 'sha256': a.get('hash')}
             for a in artifacts if a.get('kind') == 'file']
    evidence = {
        'run_status': run['status'], 'verify': mark,
        'route': run.get('route'), 'provider': run.get('provider'),
        'cost': usage.get('cost'), 'currency': usage.get('currency'),
        'events': events, 'artifacts': artifacts, 'files': files,
        'tests_hash': None,  # No independent verify-time test snapshot in W1a.
        'lineage_root': run.get('task_id') or rid,
    }
    cause = run.get('error') if verdict in ('failed', 'red') else None
    summary = f"Run {rid}: status={run['status']}; verdict={verdict}; route={run.get('route') or '-'}"
    try:
        return db.memory_add_episode(
            id=episode_id(rid), run_id=rid, task_id=run.get('task_id'),
            workspace=run.get('workspace_id') or config.workspace_id(),
            route=run.get('route'), verdict=verdict, cause=cause,
            cost=usage.get('cost'), tokens=usage.get('total_tokens'),
            started_at=run.get('started_at'), ended_at=run.get('ended_at'),
            summary=summary[:256], lessons_json='[]',
            evidence_json=json.dumps(evidence, ensure_ascii=False),
            created_at=run.get('ended_at') or run.get('started_at') or 0,
        )
    except sqlite3.IntegrityError:
        with db.connect() as c:
            if c.execute('SELECT 1 FROM memory_episode WHERE id=?', (episode_id(rid),)).fetchone():
                return None
        raise


def replay_episodes(since: str | None = None, limit: int | None = None) -> dict:
    if limit is not None and limit < 0:
        raise ValueError('limit must be nonnegative')
    where = "workspace_id=? AND status IN ('completed','failed','stalled')"
    params = [config.workspace_id()]
    if since is not None:
        timestamp = datetime.fromisoformat(since.replace('Z', '+00:00')).timestamp()
        where += ' AND COALESCE(ended_at, started_at)>=?'
        params.append(timestamp)
    with db.connect() as c:
        runs = [dict(r) for r in c.execute(
            'SELECT * FROM run WHERE ' + where + ' ORDER BY COALESCE(ended_at, started_at), id LIMIT ?',
            (*params, limit if limit is not None else -1),
        )]
    result = dict(scanned=len(runs), created=0, skipped=0,
                  by_verdict={v: 0 for v in ('pass', 'red', 'unverified', 'failed')})
    for run in runs:
        created = record_episode(run)
        result['created' if created else 'skipped'] += 1
        result['by_verdict'][verdict_for(run)] += 1
    return result
