import copy
import json
import sqlite3
import subprocess
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
                == "6"
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
    monkeypatch.setattr(plan, '_wait_run', lambda rid: None)
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
    reviewed = plan.review(row['id'])
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
    outputs[0] = '{"verdict":"wrong","findings":[]}'
    with pytest.raises(ValueError):
        plan.review(row['id'])
    with pytest.raises(ValueError):
        plan.approve(row['id'], 'human')


def test_http_plans(fake_planner):
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
        assert code == 202 and row['run_id'] and row['plan_id'] == row['id']
        for _ in range(100):
            code, row = request('/plans/' + row['id'])
            if row['status'] != 'draft':
                break
            time.sleep(.01)
        assert row['status'] == 'validated'
        path = '/plans/' + row['id']
        assert request(path + '/approve', {'by': 'human'})[0] == 400
        outputs[0] = '{"verdict":"approve","findings":[]}'
        assert request(path + '/review', {})[1]['status'] == 'reviewed'
        assert request(path + '/approve', {'by': 'robot'})[0] == 400
        assert request(path + '/approve', {'by': 'human'})[1]['status'] == 'approved'
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_cli_execution(fake_planner, capsys, monkeypatch):
    _, repo, _, outputs = fake_planner
    assert main(['plan', 'new', 'goal', '--cwd', str(repo), '--wait']) == 0
    row = json.loads(capsys.readouterr().out)
    outputs[0] = '{"verdict":"approve","findings":[]}'
    assert main(['plan', 'review', row['id']]) == 0
    capsys.readouterr()
    assert main(['plan', 'approve', row['id'], '--by', 'human']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'approved'
    from nodus.cli import client
    monkeypatch.setattr(client, 'ensure_daemon', lambda: None)
    monkeypatch.setattr(client, 'call', lambda *a: {'id':'async', 'status':'draft'})
    assert main(['plan', 'new', 'goal', '--no-wait']) == 0
    assert json.loads(capsys.readouterr().out)['id'] == 'async'


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
    monkeypatch.setattr(runner.RUNNER, 'wait', lambda rid: {'status': 'completed'})
    row = plan.create('test', repo)
    assert row['status'] == 'validated'
    assert calls[0]['kind'] == 'plan' and calls[0]['route_name'] == 'opus'
    assert calls[0]['verify_run'] is False and calls[0]['include_memory'] is False
    assert calls[0]['note'] == 'plan-only, do not write files'
    monkeypatch.setattr(runner.RUNNER, 'start', lambda *a, **kw: {'id': 'failed-fake'})
    monkeypatch.setattr(runner.RUNNER, 'wait', lambda rid: {'status': 'failed', 'error': 'fake failure'})
    row = plan.create('test', repo)
    assert row['status'] == 'failed' and row['run_id'] == 'failed-fake'
    assert 'fake failure' in row['errors_json']
