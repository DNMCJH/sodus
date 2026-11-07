"""Dream planning, replayable extraction and transactional evidence staging."""
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
        cursor = max(cursor, _cursor(c, workspace))
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


def _cursor(c, workspace):
    row = c.execute('SELECT value FROM meta WHERE key=?', ('dream_cursor:' + workspace,)).fetchone()
    return int(row[0]) if row else 0


def _episodes(c, batch):
    return [dict(r) for r in c.execute('''SELECT e.*, e.rowid AS seq FROM memory_episode e
        LEFT JOIN task t ON t.id=e.task_id
        WHERE e.workspace=? AND e.rowid>? AND e.rowid<=?
        AND (e.verdict='pass' OR t.accepted_by='human') ORDER BY e.rowid''',
        (batch['workspace_id'], batch['cursor_from'], batch['cursor_to']))]


def _check_tokens(c):
    from . import ledger
    start, end, _ = ledger.bounds('day')
    used = 0
    for row in c.execute("SELECT usage_json FROM run WHERE kind='dream' AND started_at>=? AND started_at<?", (start, end)):
        usage = json.loads(row[0] or '{}')
        used += usage.get('total_tokens') or sum(usage.get(k) or 0 for k in ('input_tokens','output_tokens','cache_read','cache_write'))
    if used >= config.dream_daily_tokens():
        raise RuntimeError('dream daily token limit reached')


def plan(workspace=None, limit=50):
    if limit < 0:
        raise ValueError('limit must be nonnegative')
    workspace = workspace or config.workspace_id()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        cursor = _cursor(c, workspace)
        existing = c.execute("SELECT * FROM dream_batch WHERE workspace_id=? AND cursor_from=? AND status IN ('planned','extracted') ORDER BY cursor_to LIMIT 1", (workspace, cursor)).fetchone()
        if existing:
            return dict(existing)
        _check_tokens(c)
        rows = c.execute('SELECT rowid FROM memory_episode WHERE workspace=? AND rowid>? ORDER BY rowid LIMIT ?', (workspace, cursor, limit)).fetchall()
        if not rows:
            return None
        bid = db.new_id('dream')
        c.execute("INSERT OR IGNORE INTO dream_batch(id,workspace_id,status,cursor_from,cursor_to,started_at) VALUES(?,?,'planned',?,?,?)", (bid,workspace,cursor,rows[-1][0],db.now()))
        return dict(c.execute('SELECT * FROM dream_batch WHERE workspace_id=? AND cursor_from=? AND cursor_to=?', (workspace,cursor,rows[-1][0])).fetchone())


def extract_model(episodes, batch):
    """Replace this function in offline tests; production uses normal run accounting."""
    from tempfile import TemporaryDirectory
    from . import runner
    prompt = ('Extract reusable lessons only from these episodes. Return JSON only: '
              '{"candidates":[{"text":"lesson", "episode_ids":["episode id"]}]}. '
              'Do not modify files.\n' + json.dumps(episodes, ensure_ascii=False))
    task = db.create_task(prompt)
    with TemporaryDirectory(prefix='sodus-dream-') as cwd:
        run = runner.RUNNER.start(task['id'], route_name=config.dream_route(), cwd=cwd,
                                  kind='dream', verify_run=False, include_handoff=False, include_memory=False)
        with db.connect() as c:
            c.execute('UPDATE dream_batch SET run_id=? WHERE id=?', (run['id'], batch['id']))
        while run['status'] not in ('completed','failed'):
            run = runner.RUNNER.wait(run['id'])
        if run['status'] != 'completed':
            raise RuntimeError(run.get('error') or 'dream run failed')
        return (config.HOME / 'runs' / run['id'] / 'final.md').read_text(encoding='utf-8')


def _get(c, batch):
    bid = batch if isinstance(batch, str) else batch['id']
    row = c.execute('SELECT * FROM dream_batch WHERE id=?', (bid,)).fetchone()
    if row is None:
        raise ValueError('unknown dream batch')
    return dict(row)


def extract(batch):
    # Serialize extract calls in-process without holding a DB transaction over a run.
    with _extract_lock:
        with db.connect() as c:
            row = _get(c, batch)
            if row['output_json'] is not None:
                return row
            _check_tokens(c)
            episodes = _episodes(c, row)
        output = extract_model(episodes, row) if episodes else '{"candidates":[]}'
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False)
        with db.connect() as c:
            c.execute("UPDATE dream_batch SET output_json=?,status='extracted',error=NULL WHERE id=? AND output_json IS NULL", (output,row['id']))
            return _get(c, row)


def count(batch):
    from . import memory
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = _get(c, batch)
        if row['status'] == 'staged':
            return row
        if row['status'] != 'extracted':
            raise ValueError('batch must be extracted')
        payload = json.loads(row['output_json'])
        candidates = payload['candidates'] if isinstance(payload, dict) else payload
        eligible = {e['id']: e for e in _episodes(c, row)}
        for item in candidates:
            text = item['text'].strip()
            if not text:
                continue
            digest = memory._hash(memory._norm(text))
            cid = db.new_id('candidate')
            stamp = db.now()
            c.execute('''INSERT INTO memory_candidate(id,workspace,kind,text,hash,status,version,hits,sources_json,created_at,updated_at)
                VALUES(?,?,'lesson',?,?,'pending',1,0,?,?,?) ON CONFLICT(workspace,hash) DO NOTHING''',
                (cid,row['workspace_id'],text,digest,json.dumps({'batch_id':row['id'],'method':'llm'}),stamp,stamp))
            candidate = c.execute('SELECT * FROM memory_candidate WHERE workspace=? AND hash=?', (row['workspace_id'],digest)).fetchone()
            cid = candidate['id']
            for eid in set(item.get('episode_ids', [])):
                if eid not in eligible:
                    continue
                episode = eligible[eid]
                evidence = json.loads(episode.get('evidence_json') or '{}')
                root = evidence.get('lineage_root') or episode['task_id'] or episode['run_id'] or eid
                exposed = c.execute("SELECT 1 FROM memory_exposure WHERE candidate_id=? AND run_id=? AND channel IN ('injected','eligible') LIMIT 1", (cid,episode['run_id'])).fetchone()
                c.execute('INSERT OR IGNORE INTO candidate_evidence VALUES(?,?,?,?,?)', (cid,eid,root,int(bool(exposed)),stamp))
            n = c.execute('SELECT COUNT(DISTINCT lineage_root) FROM candidate_evidence WHERE candidate_id=? AND exposed=0', (cid,)).fetchone()[0]
            if n >= 3 and candidate['status'] == 'pending':
                c.execute("UPDATE memory_candidate SET status='proposed',updated_at=? WHERE id=?", (stamp,cid))
            db._memory_index(c, cid)
        c.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=CAST(MAX(CAST(value AS INTEGER),CAST(excluded.value AS INTEGER)) AS TEXT)', ('dream_cursor:'+row['workspace_id'],str(row['cursor_to'])))
        c.execute("UPDATE dream_batch SET status='staged',cursor=?,ended_at=? WHERE id=?", (json.dumps({'workspace':row['workspace_id'],'seq':row['cursor_to']}),db.now(),row['id']))
        return _get(c, row)


import threading
_extract_lock = threading.Lock()


def apply_approved():
    """Only explicit human approvals are eligible for L3 writes."""
    from . import memory
    root = memory.saved_project()
    with db.connect() as c:
        rows = c.execute("SELECT * FROM memory_candidate WHERE workspace IN (?,?) AND status IN ('adopted','retire_proposed')", (str(root),config.workspace_id())).fetchall()
    ops = [dict(op='add' if r['status']=='adopted' else 'retire',text='- '+r['text'],candidate_id=r['id']) for r in rows]
    if ops:
        target = root/'AGENTS.md'
        existing = memory.marker_body(target.read_text(encoding='utf-8')) if target.exists() else ''
        ops = [op for op in ops if op['op']=='retire' or op['text'] not in existing.splitlines()]
        if ops: return memory.apply_ops(target,ops)
    return {'status':'idle'}


def daily_once(today=None):
    """Claim one local calendar day before work; failures never hot-loop."""
    from datetime import date
    today = today or date.today().isoformat()
    key = 'dream_last_run:'+config.workspace_id()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM run WHERE status IN ('queued','running') LIMIT 1").fetchone():
            return {'status':'busy'}
        last = c.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        if last and last[0] == today: return {'status':'already_run'}
        _check_tokens(c)
        c.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,today))
    batch = plan()
    result = count(extract(batch)) if batch else {'status':'idle'}
    applied = apply_approved()
    return dict(batch=result,apply=applied)


def _daily_loop(log):
    import time
    while True:
        try: daily_once()
        except Exception as exc: log(f'dream daily failed: {type(exc).__name__}: {exc}')
        time.sleep(300)


def start_daily(log=lambda message: None):
    thread = threading.Thread(target=_daily_loop,args=(log,),name='dream-daily',daemon=True)
    thread.start()
    return thread
