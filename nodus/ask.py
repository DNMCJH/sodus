"""Asynchronous answer entry point; workers never write the caller's git tree."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from . import config, db, lifecycle, propose, runner


def view(row: dict) -> dict:
    result = dict(row)
    status = row['status']
    run = db.get_run(row['run_id']) if row.get('run_id') else {}
    if run and status in ('queued', 'running') and run['status'] in ('queued', 'running', 'stalled'):
        status = run['status']
    result['status'] = lifecycle.ask_state(
        {'status': 'completed' if status == 'answered' else status},
        row.get('promoted_task_id'), row['expires_at'], time.time())
    result['task_id'] = row.get('promoted_task_id')
    result['wrote_files'] = bool(row.get('wrote_files'))
    result['poll'] = '/ask/' + row['id']
    return result


def submit(body: dict, depth: str = '0') -> tuple[int, dict]:
    try:
        level = int(depth)
    except (ValueError, TypeError):
        return 400, {'error': 'invalid_depth'}
    if level < 0:
        return 400, {'error': 'invalid_depth'}
    if level >= 1:
        return 403, {'error': 'recursion_denied'}
    if not isinstance(body, dict):
        return 400, {'error': 'invalid_body'}
    for key in ('request_id', 'text'):
        if not isinstance(body.get(key), str) or not body[key].strip():
            return 400, {'error': key + ' required'}
    for key in ('cwd', 'route', 'follow_up_of', 'origin', 'client'):
        if body.get(key) is not None and not isinstance(body[key], str):
            return 400, {'error': 'invalid_' + key}
    if body.get('origin', 'http') not in ('http', 'cli', 'mcp'):
        return 400, {'error': 'invalid_origin'}
    payload = {key: body.get(key) for key in ('text', 'cwd', 'follow_up_of', 'route')}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    row, created = db.ask_create(body['request_id'], body['text'], digest,
        cwd=body.get('cwd'), route=body.get('route'), follow_up_of=body.get('follow_up_of'),
        origin=body.get('origin') or 'http', client=body.get('client'),
        session_ref=json.dumps(body.get('session_ref')) if body.get('session_ref') is not None else None, depth=level)
    if not created:
        if row['payload_hash'] != digest:
            return 409, {'error': 'request_id_conflict', 'id': row['id'], 'payload_hash': row['payload_hash']}
        return 200, view(row)
    threading.Thread(target=execute, args=(row['id'],), daemon=True).start()
    return 202, {'id': row['id'], 'request_id': row['request_id'], 'status': 'queued', 'poll': '/ask/' + row['id']}


def get(aid: str) -> tuple[int, dict]:
    row = db.ask_get(aid)
    if not row:
        return (410, {'error': 'expired'}) if db.ask_is_expired(aid) else (404, {'error': 'not_found'})
    if row['expires_at'] < time.time():
        return 410, {'error': 'expired'}
    return 200, view(row)


def promote(aid: str, body: dict) -> tuple[int, dict]:
    if not isinstance(body, dict) or not isinstance(body.get('acceptance'), str) or not body['acceptance'].strip():
        return 400, {'error': 'acceptance required'}
    if body.get('route') is not None and not isinstance(body['route'], str):
        return 400, {'error': 'invalid_route'}
    code, row = get(aid)
    if code != 200:
        return code, row
    try:
        tid = db.ask_promote(aid, body['acceptance'], body.get('route'))
    except TimeoutError:
        return 410, {'error': 'expired'}
    except KeyError:
        return 404, {'error': 'not_found'}
    return 201, {'task_id': tid}


def execute(aid: str) -> None:
    row = db.ask_get(aid)
    ctx = None
    rid = None
    try:
        target = Path(row.get('cwd') or Path.cwd()).resolve()
        root = propose.repo_root(target)
        if root:
            ctx = propose.prepare(root, aid)
            target = Path(ctx['worktree']) / target.relative_to(root.resolve())
        brief = 'Answer the question. Do not modify files; execution requires explicit promotion.\n\n' + row['text']
        if row.get('follow_up_of'):
            parent = db.ask_get(row['follow_up_of'])
            if parent:
                brief = 'Previous question: ' + parent['text'] + '\nPrevious answer: ' + (parent.get('answer') or '') + '\n\n' + brief
        task = db.create_task(brief)
        started = time.time()
        run = runner.RUNNER.start(task['id'], route_name=row.get('route') or config.default_route(),
                                  cwd=str(target), kind='ask', verify_run=False, include_handoff=False)
        rid = run['id']
        db.ask_update(aid, run_id=rid, status='running')
        run = runner.RUNNER.wait(rid)
        while run.get('status') not in ('completed', 'failed'):
            run = runner.RUNNER.wait(rid)
        wrote = bool(runner.touched_files(target, started)) if not ctx else False
        if ctx:
            # Unlike proposal collection this must never create a git commit.
            propose._git(Path(ctx['worktree']), 'add', '-A')
            wrote = bool(propose._git(Path(ctx['worktree']), 'diff', '--cached', ctx['base'], '--').stdout.strip())
        final = config.HOME / 'runs' / rid / 'final.md'
        answer = final.read_text(encoding='utf-8') if final.is_file() else ''
        usage = json.loads(run.get('usage_json') or '{}')
        db.ask_update(aid, status='answered' if run['status'] == 'completed' else 'failed',
                      answer=answer, cost=usage.get('cost'), error=run.get('error'), wrote_files=int(wrote))
    except Exception as exc:
        db.ask_update(aid, status='failed', error=f'{type(exc).__name__}: {exc}')
    finally:
        if ctx:
            try:
                propose.discard(ctx)
            except Exception as exc:
                db.ask_update(aid, status='failed', error=f'worktree cleanup: {exc}')
