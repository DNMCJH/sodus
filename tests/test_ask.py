import json
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from sodus import ask, agents, config, db, propose, runner, server


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'HOME', tmp_path / 'state')
    monkeypatch.setenv('SODUS_WORKSPACE', 'ask-test')
    monkeypatch.setattr(ask, 'execute', lambda aid: None)


def test_idempotency_conflict_expiry_and_workspace(monkeypatch):
    code, row = ask.submit(dict(request_id='r', text='问题'))
    assert code == 202
    assert ask.submit(dict(request_id='r', text='问题'))[0] == 200
    assert ask.submit(dict(request_id='r', text='changed'))[0] == 409
    assert ask.get(row['id'])[0] == 200
    monkeypatch.setenv('SODUS_WORKSPACE', 'other')
    assert ask.get(row['id'])[0] == 404
    assert ask.submit(dict(request_id='r', text='问题'))[0] == 202
    monkeypatch.setenv('SODUS_WORKSPACE', 'ask-test')
    db.ask_update(row['id'], expires_at=1)
    assert ask.get(row['id'])[0] == 410
    assert db.ask_sweep() == 1
    assert ask.get(row['id'])[0] == 410
    assert ask.submit(dict(request_id='r', text='new'))[0] == 202
    assert ask.get('missing')[0] == 404


def test_view_maps_failed_run_to_failed_ask(monkeypatch):
    row = dict(id='a', status='running', run_id='r', expires_at=9999999999, promoted_task_id=None, wrote_files=0)
    monkeypatch.setattr(db, 'get_run', lambda _: {'status':'failed'})
    assert ask.view(row)['status'] == 'failed'


def test_memory_candidates_default_workspace_and_evidence(monkeypatch):
    cid = db.memory_add_candidate('dream candidate', workspace=config.workspace_id())
    with db.connect() as c:
        c.execute("INSERT INTO candidate_evidence(candidate_id,episode_id,lineage_root,exposed,created_at) VALUES(?,?,?,?,?)",
                  (cid, 'episode-1', 'lineage-1', 0, db.now()))
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
    try:
        req = urllib.request.Request(f'http://127.0.0.1:{httpd.server_port}/memory/candidates',
                                     headers={'Authorization':'Bearer '+config.token()})
        with urllib.request.urlopen(req) as response:
            rows = json.load(response)
        row = next(r for r in rows if r['id'] == cid)
        assert row['evidence_count'] == 1
    finally:
        httpd.shutdown(); httpd.server_close(); thread.join()


def test_concurrent_creation_and_promotion():
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda _: ask.submit(dict(request_id='race', text='why')), range(12)))
    assert [r[0] for r in rows].count(202) == 1
    aid = rows[0][1]['id']
    db.ask_update(aid, answer='because', status='answered')
    assert ask.promote(aid, {})[0] == 400
    with ThreadPoolExecutor(max_workers=6) as pool:
        promoted = list(pool.map(lambda _: ask.promote(aid, dict(acceptance='pytest')), range(12)))
    assert all(code == 201 for code, _ in promoted)
    tids = {row['task_id'] for _, row in promoted}
    assert len(tids) == 1
    task = db.get_task(tids.pop())
    assert 'why' in task['goal'] and 'because' in task['goal']
    assert ask.get(aid)[1]['status'] == 'promoted'


def test_recursion_validation():
    assert ask.submit({}, '1') == (403, {'error': 'recursion_denied'})
    assert ask.submit({}, '5')[0] == 403
    assert ask.submit({}, 'bad')[0] == 400
    for body in ({}, [], dict(request_id='r', text=' '), dict(request_id='r', text='ok', cwd=2)):
        assert ask.submit(body)[0] == 400
    # Relative cwd is rejected so asks never silently run inside daemon HOME.
    assert ask.submit(dict(request_id='rel', text='ok', cwd='.'))[0] == 400
    assert ask.submit(dict(request_id='rel2', text='ok', cwd='sub/dir'))[0] == 400


def test_http_auth_and_contract(monkeypatch):
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{httpd.server_port}'
    from sodus import mcp
    from sodus.cli import client
    monkeypatch.setattr(client, 'BASE', base)
    monkeypatch.delenv('SODUS_ROLE', raising=False)
    monkeypatch.setenv('SODUS_DEPTH', '0')
    def request(path, body=None, auth=True, depth='0'):
        headers = {'Content-Type': 'application/json', 'X-Sodus-Depth': depth}
        if auth:
            headers['Authorization'] = 'Bearer ' + config.token()
        req = urllib.request.Request(base+path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            response = urllib.request.urlopen(req)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)
    try:
        assert request('/asks', auth=False)[0] == 401
        assert request('/asks') == (200, [])
        assert request('/ask', {}, auth=False)[0] == 401
        assert request('/ask', {}, depth='1')[0] == 403
        code, row = request('/ask', dict(request_id='http', text='why'))
        assert code == 202
        listed = request('/asks')[1]
        assert len(listed) == 1
        assert listed[0]['id'] == row['id']
        assert set(listed[0]) == {'id', 'status', 'created_at', 'expires_at'}
        assert request('/asks?workspace=another')[1] == []
        assert request('/asks?workspace=ask-test')[1] == listed
        assert request('/ask', dict(request_id='http', text='why'))[0] == 200
        assert request('/ask', dict(request_id='http', text='other'))[0] == 409
        assert request(row['poll'])[0] == 200
        mcp_result = mcp.call_tool('sodus_ask', {'request_id': 'http', 'text': 'why'})
        assert not mcp_result['isError']
        assert json.loads(mcp_result['content'][0]['text'])['id'] == row['id']
        assert request(row['poll']+'/promote', {})[0] == 400
        first = request(row['poll']+'/promote', dict(acceptance='pytest'))
        assert first[0] == 201
        assert request(row['poll']+'/promote', dict(acceptance='pytest')) == first
        db.ask_update(row['id'], expires_at=1)
        assert request('/asks')[1] == []
        assert request(row['poll'])[0] == 410
        expired = mcp.call_tool('sodus_ask_status', {'id': row['id']})
        assert expired['isError']
        assert expired['content'][0]['text'] == 'HTTP 410: {"error": "expired"}'
        assert request(row['poll']+'/promote', dict(acceptance='pytest'))[0] == 410
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_schema_v3_upgrade():
    with db.connect() as c:
        c.execute('DROP TABLE ask')
        c.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    for _ in range(2):
        db._SCHEMA_READY.clear()
        with db.connect() as c:
            assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
            assert len(c.execute('PRAGMA table_info(ask)').fetchall()) == 22


# Keep a reference before the autouse fixture replaces the background target.
execute = ask.execute

@pytest.mark.parametrize('git,fail', [(False,False), (True,False), (True,True)])
def test_background(tmp_path, monkeypatch, git, fail):
    target = tmp_path / 'project'
    target.mkdir()
    ctx = dict(worktree=str(tmp_path / 'worktree'), repo=str(target), branch='temporary', base='base')
    Path(ctx['worktree']).mkdir()
    monkeypatch.setattr(propose, 'repo_root', lambda path: target if git else None)
    monkeypatch.setattr(propose, 'prepare', lambda *a: ctx)
    discarded = []
    monkeypatch.setattr(propose, 'discard', lambda c: discarded.append(c))
    commands = []
    def git_command(path, *args):
        commands.append(args)
        return type('Result', (), {'stdout': 'diff' if args[0] == 'diff' else ''})()
    monkeypatch.setattr(propose, '_git', git_command)
    _, parent = ask.submit(dict(request_id='parent', text='previous'))
    db.ask_update(parent['id'], answer='context', status='answered')
    _, row = ask.submit(dict(request_id='child', text='question', cwd=str(target), follow_up_of=parent['id']))
    def start(tid, **kwargs):
        assert kwargs['kind'] == 'ask' and kwargs['verify_run'] is False
        assert kwargs['cwd'] == (ctx['worktree'] if git else str(target))
        assert 'previous' in db.get_task(tid)['goal'] and 'context' in db.get_task(tid)['goal']
        if fail:
            raise RuntimeError('blocked budget')
        run = db.create_run(tid, 'codex', kwargs['cwd'], kind='ask')
        path = config.HOME / 'runs' / run['id']
        path.mkdir(parents=True)
        (path / 'final.md').write_text('answer', encoding='utf-8')
        db.update_run(run['id'], status='completed', usage_json='{"cost": 0.25}')
        return db.get_run(run['id'])
    monkeypatch.setattr(runner.RUNNER, 'start', start)
    execute(row['id'])
    result = db.ask_get(row['id'])
    if fail:
        assert result['status'] == 'failed' and 'blocked budget' in result['error']
    else:
        assert result['answer'] == 'answer' and result['cost'] == .25
        assert bool(result['wrote_files']) == git
    assert bool(discarded) == git
    assert all(cmd[0] != 'commit' for cmd in commands)


def test_worker_environment(monkeypatch):
    from sodus import providers
    spec = next(iter(agents.registry().values()))
    provider = next(iter(providers.load().values()))
    monkeypatch.setenv('SODUS_DEPTH', '2')
    env = agents.build_env(spec, provider, {})
    assert env['SODUS_ROLE'] == 'worker' and env['SODUS_DEPTH'] == '3'


def test_status_asks_line(monkeypatch, capsys):
    from sodus.cli.commands import system
    from urllib.parse import parse_qs, urlparse
    def request(method, path):
        assert method == 'GET'
        assert urlparse(path).path == '/asks'
        assert parse_qs(urlparse(path).query)['workspace'] == [config.workspace_id()]
        return [{'status': s} for s in ['queued', 'running', 'stalled', 'answered', 'promoted', 'failed']]
    monkeypatch.setattr(system, 'call', request)
    monkeypatch.setattr(system.providers, 'load', lambda: {})
    monkeypatch.setattr(system.telemetry, 'last_probes', lambda: {})
    monkeypatch.setattr(system.telemetry, 'usage_summary', lambda _: {})
    monkeypatch.setattr(system.telemetry, 'decision_usage', lambda _: dict(calls=0, **{'in': 0, 'out': 0}))
    monkeypatch.setattr(system.telemetry, 'balance', lambda: None)
    monkeypatch.setattr(system.budget, 'check', lambda: {'limit': 0})
    assert system.status(None) == 0
    assert 'asks active    open=1 running=2 done=2 failed=1' in capsys.readouterr().out


def test_unauthorized_post_consumes_body_before_next_request(mcp_http):
    from http.client import HTTPConnection
    connection = HTTPConnection('127.0.0.1', mcp_http.server_port, timeout=5)
    try:
        connection.request('POST', '/ask', body=b'{"text":"unauthorized"}')
        response = connection.getresponse()
        assert response.status == 401
        assert json.loads(response.read()) == {'error': 'unauthorized'}
        connection.request('GET', '/asks', headers={'Authorization': 'Bearer ' + config.token()})
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == []
    finally:
        connection.close()
