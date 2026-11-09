import copy
import json
import sqlite3
import subprocess
from pathlib import Path
import pytest
from nodus import config, db, plan
from sodus.cli import main


@pytest.fixture
def valid(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "state")
    monkeypatch.setenv("NODUS_WORKSPACE", "plan-test")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    value = dict(
        schema_version=1,
        goal="test",
        non_goals=[],
        base_sha=sha,
        recon=dict(files=["."], tests=["python -m pytest"], agents_md=None),
        milestones=[dict(id="m1", title="first", exit_criteria=["tests pass"])],
        nodes=[
            dict(
                id="n1",
                milestone="m1",
                title="test",
                description="test plan",
                deps=[],
                file_scope=["."],
                acceptance=["python -m pytest"],
                difficulty=1,
                route_hint="sol",
                budget_cny=0,
            )
        ],
        risks=["risk"],
        rollback="restore",
        review_points=["review"],
    )
    return value, repo


@pytest.mark.parametrize(
    "kind", ["base_sha", "path", "cycle", "acceptance", "duplicate_id", "milestone"]
)
def test_six_errors(valid, kind):
    value, repo = valid
    if kind == "base_sha":
        value["base_sha"] = "0" * 40
    if kind == "path":
        value["nodes"][0]["file_scope"] = ["missing.py"]
    if kind == "cycle":
        value["nodes"][0]["deps"] = ["n1"]
    if kind == "acceptance":
        value["nodes"][0]["acceptance"] = []
    if kind == "duplicate_id":
        value["nodes"].append(copy.deepcopy(value["nodes"][0]))
    if kind == "milestone":
        value["milestones"].append(
            dict(id="empty", title="empty", exit_criteria=["pass"])
        )
    assert kind in {e["kind"] for e in plan.validate_plan(value, repo)}
    with pytest.raises(ValueError):
        plan.import_plan(value, repo)
    assert plan.list_plans() == []


def test_roundtrip_cli(valid, tmp_path, capsys):
    value, repo = valid
    assert plan.validate_plan(value, repo) == []
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    assert main(["plan", "validate", str(path), "--cwd", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"]
    assert main(["plan", "import", str(path), "--cwd", str(repo)]) == 0
    pid = capsys.readouterr().out.strip()
    stored = plan.get_plan(pid)
    assert stored["plan_json"] == value
    assert stored["status"] == "validated" and stored["version"] == 1
    assert stored["nodes"][0]["node_id"] == "n1"
    assert stored["approved_at"] is None
    assert main(["plan", "show", pid, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == pid
    assert main(["plan", "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == pid
    assert main(["plan", "show", "missing"]) == 1
    assert main(["plan", "validate", str(path) + "missing", "--json"]) == 1
    path.write_text("{", encoding="utf-8")
    assert main(["plan", "validate", str(path), "--json"]) == 1


def test_v5_migration(valid):
    config.ensure_dirs()
    with sqlite3.connect(config.HOME / "nodus.sqlite3") as c:
        c.executescript(db.SCHEMA)
        c.execute("INSERT INTO meta VALUES('schema_version','5')")
        c.execute("INSERT INTO task(id,title) VALUES('old','preserved')")
    for _ in range(3):
        db._SCHEMA_READY.clear()
        with db.connect() as c:
            assert (
                c.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]
                == "7"
            )
            assert (
                c.execute("SELECT title FROM task WHERE id='old'").fetchone()[0]
                == "preserved"
            )
            assert c.execute("SELECT count(*) FROM plan").fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM plan_node").fetchone()[0] == 0


@pytest.mark.parametrize(
    "value", [None, [], {"nodes": [None]}, {"nodes": [{"id": []}]}]
)
def test_malformed(value):
    assert plan.validate_plan(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("budget_cny", -1),
        ("budget_cny", float("nan")),
        ("difficulty", True),
        ("difficulty", 6),
        ("acceptance", ["完成即可"]),
        ("acceptance", "pytest"),
        ("file_scope", ["../"]),
        ("deps", ["missing"]),
    ],
)
def test_node_constraints(valid, field, value):
    document, repo = valid
    document["nodes"][0][field] = value
    assert plan.validate_plan(document, repo)


def test_workspace_isolation(valid, monkeypatch):
    value, repo = valid
    pid = plan.import_plan(value, repo)
    monkeypatch.setenv("NODUS_WORKSPACE", "other-plan-workspace")
    assert plan.get_plan(pid) is None
    assert plan.list_plans() == []


def test_cli_invalid_plan(valid, tmp_path, capsys):
    value, repo = valid
    value["nodes"][0]["acceptance"] = ["完成即可"]
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    for action in ("validate", "import"):
        assert main(["plan", action, str(path), "--cwd", str(repo), "--json"]) == 1
        assert json.loads(capsys.readouterr().out)["errors"]
    assert plan.list_plans() == []


def test_planner_route(monkeypatch):
    monkeypatch.setattr(config, 'load_config', lambda: {})
    assert config.planner_route() == 'opus'
    monkeypatch.setattr(config, 'load_config', lambda: {'routing': {'planner': ' sol '}})
    assert config.planner_route() == 'sol'


@pytest.fixture
def fake_planner(valid, monkeypatch):
    value, repo = valid
    calls = []
    outputs = [json.dumps(value)]
    def start(prompt, cwd, route, kind='plan'):
        rid = 'fake-' + str(len(calls))
        calls.append(dict(prompt=prompt, cwd=cwd, route=route, kind=kind))
        db.append_event(rid, 1, 'message', {'text': 'earlier explanation'})
        db.append_event(rid, 2, 'message', {'text': outputs[0]})
        return {'id': rid}
    monkeypatch.setattr(plan, '_start_planner_run', start)
    monkeypatch.setattr(plan, '_wait_run', lambda rid, timeout=None: None)
    return value, repo, calls, outputs


def test_create_retry_and_extract(fake_planner):
    value, repo, calls, outputs = fake_planner
    outputs[0] = 'garbage'
    failed = plan.create('goal', repo)
    assert failed['status'] == 'failed' and json.loads(failed['errors_json'])
    assert failed['run_id'] == 'fake-0'
    outputs[0] = '{}'
    assert plan.create('goal', repo)['status'] == 'failed'
    outputs[0] = '```json\n' + json.dumps(value) + '\n```'
    row = plan.create('goal', repo, 'pytest', route='custom')
    assert row['status'] == 'validated' and row['plan_json'] == value
    assert row['hash'] and row['base_sha'] == value['base_sha'] and row['nodes']
    assert calls[-1]['route'] == 'custom' and calls[-1]['kind'] == 'plan'
    assert 'git ls-files' in calls[-1]['prompt'] and 'Do not write files' in calls[-1]['prompt']
    assert plan.get_plan(failed['id'])['status'] == 'failed'


@pytest.mark.parametrize('verdict', ['approve', 'revise', 'reject'])
def test_review_and_human_gate(fake_planner, verdict):
    _, repo, calls, outputs = fake_planner
    row = plan.create('goal', repo)
    with pytest.raises(ValueError):
        plan.approve(row['id'], 'human')
    outputs[0] = json.dumps(dict(verdict=verdict, findings=['checked']))
    reviewing = plan.review(row['id'])
    assert reviewing['status'] in ('reviewing', 'reviewed')
    reviewed = _await_plan_status(row['id'], 'reviewed')
    assert reviewed['status'] == 'reviewed'
    assert reviewed['review_json']['verdict'] == verdict
    assert calls[-1]['route'] == 'sol' and calls[-1]['kind'] == 'plan_review'
    with pytest.raises(ValueError):
        plan.approve(row['id'], 'agent')
    if verdict == 'approve':
        approved = plan.approve(row['id'], 'human')
        assert approved['status'] == 'approved' and approved['approved_at']
        assert approved['approved_by'] == 'human'
    else:
        with pytest.raises(ValueError, match='verdict=approve'):
            plan.approve(row['id'], 'human')


def test_invalid_review_clears_old_verdict(fake_planner):
    _, repo, _, outputs = fake_planner
    row = plan.create('goal', repo)
    outputs[0] = '{"verdict":"approve","findings":[]}'
    plan.review(row['id'])
    _await_plan_status(row['id'], 'reviewed')
    outputs[0] = '{"verdict":"wrong","findings":[]}'
    plan.review(row['id'])
    failed = _await_plan_status(row['id'], 'review_failed')
    assert json.loads(failed['errors_json'])
    with pytest.raises(ValueError):
        plan.approve(row['id'], 'human')


def _await_plan_status(pid, status):
    import time
    for _ in range(200):
        row = plan.get_plan(pid)
        if row['status'] == status:
            return row
        time.sleep(.005)
    raise AssertionError(f'plan did not reach {status}: {row["status"]}')


def test_review_async_transition_and_timeout(fake_planner, monkeypatch):
    _, repo, _, outputs = fake_planner
    row = plan.create('goal', repo)
    outputs[0] = json.dumps(dict(verdict='approve', findings=[]))
    gate = __import__('threading').Event()
    original_wait = plan._wait_run
    def blocked_wait(rid, timeout=None):
        gate.wait(.05)
        if timeout == .01:
            raise TimeoutError('review deadline exceeded')
        return original_wait(rid, timeout)
    monkeypatch.setattr(plan, '_wait_run', blocked_wait)
    monkeypatch.setattr(config, 'review_timeout', lambda: .01)
    reviewing = plan.review(row['id'])
    assert reviewing['status'] == 'reviewing'
    gate.set()
    failed = _await_plan_status(row['id'], 'review_failed')
    assert 'deadline exceeded' in failed['errors_json']


def test_validator_drift_warnings_and_acceptance_semantics(valid):
    value, repo = valid
    (repo / 'drift.txt').write_text('head moved', encoding='utf-8')
    subprocess.run(['git', 'add', 'drift.txt'], cwd=repo, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.org', 'commit', '-m', 'drift'], cwd=repo, check=True, capture_output=True)
    value['recon']['agents_md'] = 'a' * 64
    (repo / 'AGENTS.md').write_text('actual rules', encoding='utf-8')
    warnings = []
    errors = plan.validate_plan(value, repo, warnings)
    assert not errors
    assert {w['kind'] for w in warnings} == {'base_sha', 'agents_md'}
    value['nodes'][0]['acceptance'] = ['tests pass']
    assert any(e['kind'] == 'acceptance' for e in plan.validate_plan(value, repo))


def test_validator_agents_missing_warning(valid):
    value, repo = valid
    value['recon']['agents_md'] = 'a' * 64
    warnings = []
    assert not plan.validate_plan(value, repo, warnings)
    assert any(w['kind'] == 'agents_md' and 'missing' in w['message'] for w in warnings)


def test_http_plans(fake_planner, monkeypatch):
    monkeypatch.delenv("NODUS_ROLE", raising=False)
    import threading
    import time
    import urllib.request
    import urllib.error
    from http.server import ThreadingHTTPServer
    from nodus import server
    _, repo, _, outputs = fake_planner
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    def request(path, body=None, auth=True):
        req = urllib.request.Request(f'http://127.0.0.1:{httpd.server_port}' + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + config.token()} if auth else {})})
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)
    try:
        assert request('/plan', {'goal': 'test'}, auth=False)[0] == 401
        assert request('/plan', {})[0] == 400
        assert request('/plans/missing')[0] == 404
        assert request('/plans/missing/review', {})[0] == 404
        code, row = request('/plan', {'goal': 'test', 'cwd': str(repo)})
        assert code == 202 and row['run_id'] and row['plan_id'] == row['id'] and row['status'] == 'planning'
        for _ in range(100):
            code, row = request('/plans/' + row['id'])
            if row['status'] != 'planning':
                break
            time.sleep(.01)
        assert row['status'] == 'validated'
        outputs[0] = json.dumps(dict(verdict='approve', findings=[]))
        code, reviewing = request('/plans/' + row['id'] + '/review', {})
        assert code == 202 and reviewing['status'] == 'reviewing'
        for _ in range(100):
            code, reviewing = request('/plans/' + row['id'])
            if reviewing['status'] != 'reviewing':
                break
            time.sleep(.01)
        assert reviewing['status'] == 'reviewed'
        path = '/plans/' + row['id']
        assert request(path + '/approve', {'by': 'human'})[1]['status'] == 'approved'
        row = plan.get_plan(row['id'])
        plan._update(row['id'], status='validated')
        outputs[0] = '{"verdict":"approve","findings":[]}'
        assert request(path + '/review', {})[1]['status'] in ('reviewing', 'reviewed')
        for _ in range(100):
            _, reviewed = request(path)
            if reviewed['status'] != 'reviewing': break
            time.sleep(.01)
        assert reviewed['status'] == 'reviewed'
        assert request(path + '/approve', {'by': 'robot'})[0] == 400
        assert request(path + '/approve', {'by': 'human'})[1]['status'] == 'approved'
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_cli_execution(valid, capsys, monkeypatch):
    _, repo = valid
    from nodus.cli import client
    from nodus.cli.commands import plan as cli_plan
    calls = []
    def call(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        if path == '/plan':
            return {'id': 'fake', 'status': 'planning'}
        if method == 'GET':
            return {'id': 'fake', 'status': 'validated'}
        return {'id': 'fake', 'status': 'reviewed' if path.endswith('/review') else 'approved'}
    monkeypatch.setattr(client, 'ensure_daemon', lambda: None)
    monkeypatch.setattr(client, 'call', call)
    monkeypatch.setattr(cli_plan.time, 'sleep', lambda _: None)
    assert main(['plan', 'new', 'goal', '--cwd', str(repo), '--wait']) == 0
    row = json.loads(capsys.readouterr().out)
    assert row['status'] == 'validated'
    assert [(c[0], c[1]) for c in calls] == [('POST', '/plan'), ('GET', '/plans/fake')]
    assert main(['plan', 'review', row['id']]) == 0
    capsys.readouterr()
    assert main(['plan', 'approve', row['id'], '--by', 'human']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'approved'
    assert calls[-2][1] == '/plans/fake/review' and calls[-2][0] == 'POST'
    assert calls[-1][1:3] == ('/plans/fake/approve', {'by': 'human'})
    calls.clear()
    assert main(['plan', 'new', 'goal', '--no-wait']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'planning'
    assert len(calls) == 1 and calls[0][1] == '/plan'
    assert 'runner' not in Path(cli_plan.__file__).read_text(encoding='utf-8')


def test_runner_contract_and_failure(valid, monkeypatch):
    from nodus import runner
    value, repo = valid
    calls = []
    def start(task_id, **kwargs):
        calls.append(kwargs)
        assert 'last assistant message' in db.get_task(task_id)['goal']
        db.append_event('integration-fake', 1, 'message', {'text': json.dumps(value)})
        return {'id': 'integration-fake'}
    monkeypatch.setattr(runner.RUNNER, 'start', start)
    monkeypatch.setattr(runner.RUNNER, 'wait', lambda rid, **kwargs: {'status': 'completed'})
    row = plan.create('test', repo)
    assert row['status'] == 'validated'
    assert calls[0]['kind'] == 'plan' and calls[0]['route_name'] == 'opus'
    assert calls[0]['verify_run'] is False and calls[0]['include_memory'] is False
    assert calls[0]['note'] == 'plan-only, do not write files'
    monkeypatch.setattr(runner.RUNNER, 'start', lambda *a, **kw: {'id': 'failed-fake'})
    monkeypatch.setattr(runner.RUNNER, 'wait', lambda rid, **kwargs: {'status': 'failed', 'error': 'fake failure'})
    row = plan.create('test', repo)
    assert row['status'] == 'failed' and row['run_id'] == 'failed-fake'
    assert 'fake failure' in row['errors_json']


def test_contract_packaged_and_read_failure(fake_planner, monkeypatch):
    _, repo, calls, _ = fake_planner
    monkeypatch.setattr(Path, 'read_text', lambda *a, **kw: (_ for _ in ()).throw(OSError('no docs')))
    assert plan.create('goal', repo)['status'] == 'validated'
    assert 'schema_version' in calls[-1]['prompt']
    monkeypatch.setattr(plan, '_plan_contract', lambda: (_ for _ in ()).throw(OSError('contract unavailable')))
    row = plan.create('goal', repo)
    assert row['status'] == 'failed' and 'contract unavailable' in row['errors_json']


def test_planning_deadline(valid, monkeypatch):
    from nodus import runner
    _, repo = valid
    monkeypatch.setenv('NODUS_PLAN_TIMEOUT', '0.01')
    monkeypatch.setattr(plan, '_start_planner_run', lambda *a, **kw: {'id': 'forever'})
    monkeypatch.setattr(runner.RUNNER, 'wait', lambda *a, **kw: {'status': 'running'})
    row = plan.create('goal', repo)
    assert row['status'] == 'failed' and 'deadline' in row['errors_json']


@pytest.mark.parametrize('run_status', ['failed', 'completed', 'running', 'missing'])
def test_startup_reconcile(valid, monkeypatch, run_status):
    value, repo = valid
    pid = plan.import_plan(value, repo)
    plan._update(pid, status='planning')
    if run_status != 'missing':
        task = db.create_task('restart test')
        db.create_run(task['id'], 'sol', str(repo), rid='old', kind='plan')
        with db.connect() as c:
            c.execute('UPDATE run SET status=? WHERE id=?', (run_status, 'old'))
    with plan._connect_meta() as c:
        c.execute('UPDATE plan_execution SET run_id=? WHERE plan_id=?', ('old', pid))
    assert plan.reconcile() == [pid]
    assert plan.reconcile() == []
    row = plan.get_plan(pid)
    assert row['status'] == 'failed' and json.loads(row['errors_json'])


def test_cli_deadline(valid, monkeypatch, capsys):
    from nodus.cli import client
    from nodus.cli.commands import plan as cli_plan
    monkeypatch.setattr(client, 'ensure_daemon', lambda: None)
    monkeypatch.setattr(client, 'call', lambda *a, **kw: {'id': 'forever', 'status': 'planning'})
    ticks = iter([0, 0, 1900, 1900])
    monkeypatch.setattr(cli_plan.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(cli_plan.time, 'sleep', lambda _: None)
    assert main(['plan', 'new', 'goal', '--wait']) == 1
    row = json.loads(capsys.readouterr().out)
    assert row['status'] == 'failed' and row['errors'][0]['kind'] == 'timeout'


@pytest.mark.parametrize('path', ['/plan', '/plans/unused/review', '/plans/unused/approve'])
@pytest.mark.parametrize('mode', ['depth', 'worker'])
def test_plan_recursion_denied(valid, monkeypatch, path, mode):
    from nodus import server
    from email.message import Message
    import io
    handler = object.__new__(server.Handler)
    handler.path = path
    handler.headers = Message()
    handler.headers['Authorization'] = 'Bearer ' + config.token()
    handler.headers['Content-Length'] = '2'
    if mode == 'depth':
        handler.headers['X-Sodus-Depth'] = '1'
    else:
        monkeypatch.setenv('NODUS_ROLE', 'worker')
    handler.rfile = io.BytesIO(b'{}')
    replies = []
    monkeypatch.setattr(server, '_json', lambda h, code, body: replies.append((code, body)))
    for name in ('create', 'review', 'approve'):
        monkeypatch.setattr(plan, name, lambda *a, **kw: pytest.fail('must not dispatch'))
    handler._post()
    assert replies == [(403, {'error': 'recursion_denied'})]


def test_plan_timeout_config(monkeypatch):
    monkeypatch.setattr(config, 'load_config', lambda: {})
    monkeypatch.delenv('NODUS_PLAN_TIMEOUT', raising=False)
    assert config.plan_timeout() == 1800
    monkeypatch.setenv('NODUS_PLAN_TIMEOUT', '12.5')
    assert config.plan_timeout() == 12.5
    for invalid in ('nan', 'inf', '-1', 'bad'):
        monkeypatch.setenv('NODUS_PLAN_TIMEOUT', invalid)
        assert config.plan_timeout() == 1800


def test_cli_poll_network_timeout(valid, monkeypatch, capsys):
    from nodus.cli import client
    monkeypatch.setattr(client, 'ensure_daemon', lambda: None)
    def call(method, *a, **kwargs):
        if method == 'POST':
            return {'id': 'fake', 'status': 'planning'}
        assert kwargs['retries'] == 0
        raise TimeoutError('socket deadline')
    monkeypatch.setattr(client, 'call', call)
    assert main(['plan', 'new', 'goal']) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'failed'


@pytest.mark.parametrize('change', ['hash', 'head', 'agents', 'warning'])
def test_approval_revalidates(valid, change, monkeypatch):
    import hashlib
    value, repo = valid
    if change == 'agents':
        (repo / 'AGENTS.md').write_text('before')
        value['recon']['agents_md'] = hashlib.sha256(b'before').hexdigest()
    pid = plan.import_plan(value, repo)
    plan._update(pid, status='reviewed')
    with db.connect() as c:
        c.execute('UPDATE plan_execution SET review_json=? WHERE plan_id=?', ('{"verdict":"approve"}', pid))
        if change == 'hash':
            c.execute("UPDATE plan SET hash='tampered' WHERE id=?", (pid,))
    if change == 'head':
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=t@t', 'commit', '--allow-empty', '-m', 'drift'], cwd=repo, check=True, capture_output=True)
    if change == 'agents':
        (repo / 'AGENTS.md').write_text('after')
    if change == 'warning':
        monkeypatch.setattr(plan, 'validate_plan', lambda value, cwd, warnings: warnings.append({'kind':'warning'}) or [])
    with pytest.raises(ValueError):
        plan.approve(pid, 'human')


def test_timeout_stops_worker(valid, monkeypatch):
    from nodus import runner
    task = db.create_task('timeout')
    run = db.create_run(task['id'], 'sol', cwd=str(valid[1]))
    stopped = []
    monkeypatch.setattr(runner.RUNNER, 'stop', lambda rid: stopped.append(rid))
    with pytest.raises(TimeoutError):
        plan._wait_run(run['id'], timeout=0)
    assert stopped == [run['id']]
    assert db.get_run(run['id'])['status'] == 'failed'


def test_review_timeout_stops_and_finishes(valid, monkeypatch):
    from nodus import runner
    value, repo = valid
    pid = plan.import_plan(value, repo)
    task = db.create_task('review timeout')
    run = db.create_run(task['id'], 'sol', cwd=str(repo), kind='plan_review')
    stopped = []
    monkeypatch.setattr(runner.RUNNER, 'stop', lambda rid: stopped.append(rid))
    monkeypatch.setattr(config, 'review_timeout', lambda: 0)
    plan._finish_review(pid, run['id'])
    assert stopped == [run['id']]
    assert plan.get_plan(pid)['status'] == 'review_failed'
    assert db.get_run(run['id'])['status'] == 'failed'


@pytest.mark.parametrize("failure", [False, True])
def test_planner_isolation_cleanup(valid, monkeypatch, failure):
    from nodus import runner
    value, repo = valid
    before = subprocess.run(['git', 'status', '--porcelain'], cwd=repo, capture_output=True).stdout
    seen = []
    def start(task_id, **kwargs):
        isolated = Path(kwargs['cwd'])
        seen.append(isolated)
        assert isolated.resolve() != repo.resolve()
        assert kwargs['readonly'] is True
        (isolated / 'worker-write.txt').write_text('isolated')
        if failure:
            raise RuntimeError('start failed')
        db.append_event('isolated-test', 1, 'message', {'text': json.dumps(value)})
        return {'id': 'isolated-test'}
    monkeypatch.setattr(runner.RUNNER, 'start', start)
    monkeypatch.setattr(runner.RUNNER, 'wait', lambda *a, **kw: {'status': 'completed'})
    row = plan.create('isolation', repo)
    assert row['status'] == ('failed' if failure else 'validated')
    assert seen and not seen[0].exists()
    assert not (repo / 'worker-write.txt').exists()
    assert subprocess.run(['git', 'status', '--porcelain'], cwd=repo, capture_output=True).stdout == before
    assert str(seen[0]) not in subprocess.run(['git', 'worktree', 'list'], cwd=repo, capture_output=True, text=True).stdout


def test_planner_copy_filter(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'keep.txt').write_text('before')
    for name in ('.venv', 'node_modules', 'cache', 'checkpoints'):
        (source / name).mkdir()
        (source / name / 'secret').write_text('excluded')
    cwd, cleanup = plan._isolated_cwd(str(source))
    try:
        copied = Path(cwd)
        assert (copied / 'keep.txt').read_text() == 'before'
        assert not any((copied / n).exists() for n in ('.venv', 'node_modules', 'cache', 'checkpoints'))
        (copied / 'keep.txt').write_text('changed')
        assert (source / 'keep.txt').read_text() == 'before'
    finally:
        cleanup()
    assert not Path(cwd).exists()


def test_isolation_fallback_warning(valid, monkeypatch, caplog):
    from nodus import runner
    _, repo = valid
    def fail(*a):
        raise OSError('isolation unavailable')
    monkeypatch.setattr(plan, '_isolated_cwd', fail)
    seen = []
    monkeypatch.setattr(runner.RUNNER, 'start', lambda *a, **kw: seen.append(kw) or {'id': 'fallback'})
    plan._start_planner_run('test', str(repo), 'sol')
    assert seen[0]['cwd'] == str(repo) and seen[0]['readonly']
    assert 'falling back to original cwd' in caplog.text


@pytest.mark.parametrize('kind', ['plan', 'plan_review'])
def test_failed_collector_cleans_workspace(valid, monkeypatch, kind):
    value, repo = valid
    pid = plan.import_plan(value, repo)
    isolated, cleanup = plan._isolated_cwd(str(repo))
    plan._planner_workspaces['collector-failed'] = cleanup
    with db.connect() as c:
        c.execute('UPDATE plan_execution SET run_id=? WHERE plan_id=?', ('collector-failed', pid))
    def fail(*a, **kw):
        raise TimeoutError('fake deadline')
    monkeypatch.setattr(plan, '_wait_run', fail)
    if kind == 'plan':
        plan._finish_create(pid)
    else:
        plan._finish_review(pid, 'collector-failed')
    assert not Path(isolated).exists()
    assert 'collector-failed' not in plan._planner_workspaces
