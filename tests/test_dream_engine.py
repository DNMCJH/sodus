import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from nodus import config, db, dream


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'HOME', tmp_path / 'state')
    monkeypatch.setenv('NODUS_WORKSPACE', 'dream-tests')


def episode(root, verdict='pass', **kw):
    return db.memory_add_episode(verdict=verdict, evidence_json=json.dumps({'lineage_root': root}), **kw)


def test_plan_unique_and_workspace():
    episode('a')
    with ThreadPoolExecutor(4) as pool:
        batches = list(pool.map(lambda _: dream.plan(), range(4)))
    assert len({b['id'] for b in batches}) == 1
    assert dream.plan('other') is None
    assert dream.plan(limit=0)['id'] == batches[0]['id']
    with db.connect() as c:
        assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION) == '5'


def test_extract_replay_and_gate(monkeypatch):
    good = episode('a')
    episode('bad', 'failed')
    task = db.create_task('accepted')
    db.update_task(task['id'], accepted_by='human')
    human = episode('h', 'unverified', task_id=task['id'])
    calls = []
    def fake(rows, batch):
        calls.append(rows)
        return json.dumps({'candidates': [{'text': 'lesson', 'episode_ids': [r['id'] for r in rows]}]})
    monkeypatch.setattr(dream, 'extract_model', fake)
    b = dream.plan()
    assert dream.extract(b)['output_json'] == dream.extract(b)['output_json']
    assert len(calls) == 1
    assert {r['id'] for r in calls[0]} == {good, human}
    dream.count(b)
    assert dream.extract(b)['status'] == 'staged'
    assert dream.plan() is None


@pytest.mark.parametrize('raw,ok', [
    ('```json\n{"candidates":[{"text":"fenced"}]}\n```', True),
    ('garbage', False),
    ('{"candidates":[{}]}', False),
    ('[]', False),
])
def test_parse_output_schema_and_fenced_json(raw, ok):
    if ok:
        assert dream._parse_output(raw)['candidates'][0]['text'] == 'fenced'
    else:
        with pytest.raises((ValueError, json.JSONDecodeError, TypeError)):
            dream._parse_output(raw)


def test_extract_invalid_output_fails_and_allows_replan(monkeypatch):
    episode('bad-output')
    monkeypatch.setattr(dream, 'extract_model', lambda *_: 'not json')
    batch = dream.plan()
    failed = dream.extract(batch)
    assert failed['status'] == 'failed' and failed['error']
    monkeypatch.setattr(dream, 'extract_model', lambda *_: '```json\n{"candidates": []}\n```')
    retry = dream.plan()
    assert retry['id'] == batch['id'] and retry['status'] == 'planned'
    assert dream.extract(retry)['status'] == 'extracted'


def test_count_atomic_threshold_exposure_and_self_evidence(monkeypatch):
    from nodus import memory
    text = '数据库迁移需要事务'
    cid = db.memory_add_candidate(text, hash=memory._hash(memory._norm(text)), status='pending')
    ids = [episode(root, run_id='r'+str(i)) for i, root in enumerate(['a','a','b','c','d'])]
    db.memory_record_exposure(cid, None, 'r3', 'injected')
    b = dream.plan()
    monkeypatch.setattr(dream, 'extract_model', lambda *_: {'candidates': [{'text':text, 'episode_ids':ids + [cid]}]})
    dream.extract(b)
    original = db._memory_index
    monkeypatch.setattr(db, '_memory_index', lambda *_: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError):
        dream.count(b)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM candidate_evidence').fetchone()[0] == 0
        assert dream._cursor(c, config.workspace_id()) == 0
    monkeypatch.setattr(db, '_memory_index', original)
    assert dream.count(b)['status'] == 'staged'
    assert dream.count(b)['status'] == 'staged'
    assert db.memory_get_candidate(cid)['status'] == 'proposed'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM candidate_evidence').fetchone()[0] == 5
        assert c.execute('SELECT COUNT(DISTINCT lineage_root) FROM candidate_evidence WHERE exposed=0').fetchone()[0] == 3
        assert dream._cursor(c, config.workspace_id()) == b['cursor_to']


def test_pending_and_harvest_exemption(monkeypatch):
    eid = episode('a')
    harvested = db.memory_add_candidate('file fact', kind='harvested')
    monkeypatch.setattr(dream, 'extract_model', lambda *_: {'candidates':[{'text':'new lesson','episode_ids':[eid]}]})
    dream.count(dream.extract(dream.plan()))
    assert db.memory_get_candidate(harvested)['status'] == 'proposed'
    with db.connect() as c:
        assert c.execute("SELECT status FROM memory_candidate WHERE text='new lesson'").fetchone()[0] == 'pending'


def test_limit_and_cli(monkeypatch, capsys):
    from nodus.cli import main
    monkeypatch.setattr(config, 'dream_daily_tokens', lambda: 0)
    episode('a')
    with pytest.raises(RuntimeError, match='token limit'):
        dream.plan()
    assert main(['memory','dream','--dry-run','--json'], prog='sodus') == 0
    assert json.loads(capsys.readouterr().out)['dry_run']
    monkeypatch.setattr(config, 'dream_daily_tokens', lambda: 100)
    monkeypatch.setattr(dream, 'extract_model', lambda *_: {'candidates':[]})
    assert main(['memory','dream','--now','--json'], prog='sodus') == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'staged'


def test_model_adapter_route_kind_ledger_and_daily_cap(monkeypatch):
    from nodus import runner, ledger
    calls = []
    monkeypatch.setattr(config, 'dream_route', lambda: 'cheap')
    def start(tid, **kwargs):
        calls.append(kwargs)
        run = db.create_run(tid, 'codex', kwargs['cwd'])
        db.update_run(run['id'], kind=kwargs['kind'], status='completed', started_at=db.now(),
                      usage_json=json.dumps({'input_tokens':7,'output_tokens':3,'cost':0.01,'currency':'USD'}))
        path = config.HOME / 'runs' / run['id']
        path.mkdir(parents=True, exist_ok=True)
        (path / 'final.md').write_text('{"candidates":[]}', encoding='utf-8')
        return db.get_run(run['id'])
    monkeypatch.setattr(runner.RUNNER, 'start', start)
    episode('a')
    b = dream.extract(dream.plan())
    assert calls[0]['kind'] == 'dream' and calls[0]['route_name'] == 'cheap'
    assert db.get_run(b['run_id'])['usage_json']
    dream.count(b)
    episode('b')
    monkeypatch.setattr(config, 'dream_daily_tokens', lambda: 10)
    with pytest.raises(RuntimeError, match='token limit'):
        dream.plan()
    assert len(calls) == 1


def test_v5_upgrade_preserves_legacy_batch():
    with db.connect() as c:
        c.execute("INSERT INTO dream_batch(id,status,cursor) VALUES('legacy','staged','7')")
        c.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
    db._SCHEMA_READY.clear()
    for _ in range(2):
        with db.connect() as c:
            assert c.execute("SELECT cursor FROM dream_batch WHERE id='legacy'").fetchone()[0] == '7'
            assert c.execute("SELECT value FROM meta WHERE key='memory_version'").fetchone()[0] == '0'
            assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '5'
        db._SCHEMA_READY.clear()


@pytest.mark.parametrize('query', ['数据库迁移','写锁','pytest 超时','watchdog'])
def test_cjk_search(query):
    cid = db.memory_add_candidate('数据库迁移，写锁；pytest 超时，watchdog。')
    assert cid in {r['id'] for r in db.memory_search_candidates(query)}


def test_v5_migration_failure_rolls_back(monkeypatch):
    with db.connect() as c:
        c.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
        c.execute('DROP TABLE candidate_evidence')
    db._SCHEMA_READY.clear()
    original = db._migrate_v5
    def fail(c):
        original(c)
        raise RuntimeError('migration failure')
    monkeypatch.setattr(db, '_MIGRATIONS', tuple((v, fail if v == 5 else fn) for v,fn in db._MIGRATIONS))
    with pytest.raises(RuntimeError, match='migration failure'):
        db.connect()
    import sqlite3
    with sqlite3.connect(config.HOME / 'nodus.sqlite3') as c:
        assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '4'
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='candidate_evidence'").fetchone()
