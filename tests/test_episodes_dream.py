import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from nodus import config, db, dream, episodes
from nodus.cli import main, build_parser


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'HOME', tmp_path / 'state')
    monkeypatch.setenv('NODUS_WORKSPACE', 'episodes-test')


def run(status='completed', mark=None, **fields):
    task = db.create_task('episode task')
    row = db.create_run(task['id'], 'codex', '.', route='test')
    db.update_run(row['id'], status=status, ended_at=100, **fields)
    if mark:
        db.append_event(row['id'], db.next_seq(row['id']), 'verify.' + mark, {'ok': mark == 'green'})
    return db.get_run(row['id'])


@pytest.mark.parametrize('status,mark,expected', [
    ('completed', 'green', 'pass'), ('completed', None, 'unverified'),
    ('completed', 'red', 'red'), ('failed', 'green', 'failed'),
    ('stalled', None, 'failed'),
])
def test_verdict(status, mark, expected):
    assert episodes.verdict_for(run(status, mark)) == expected


def test_record_concurrent_and_evidence():
    row = run(mark='green', usage_json=json.dumps({'cost': 1.25, 'currency': 'CNY'}))
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: episodes.record_episode(row), range(8)))
    assert ids.count('ep-' + row['id']) == 1
    assert ids.count(None) == 7
    stored = db.memory_episodes_after(0)
    assert len(stored) == 1
    evidence = json.loads(stored[0]['evidence_json'])
    assert evidence['run_status'] == 'completed'
    assert evidence['verify'] == 'ok'
    assert evidence['cost'] == 1.25 and evidence['currency'] == 'CNY'
    assert evidence['route'] == 'test' and 'provider' in evidence
    assert episodes.record_episode(run('running')) is None


def test_replay_idempotent_filters_and_workspace(monkeypatch):
    run(mark='green')
    run('failed')
    run('running')
    monkeypatch.setenv('NODUS_WORKSPACE', 'other')
    run()
    monkeypatch.setenv('NODUS_WORKSPACE', 'episodes-test')
    assert episodes.replay_episodes(since='2020-01-01T00:00:00Z')['scanned'] == 0
    assert episodes.replay_episodes(limit=0)['scanned'] == 0
    first = episodes.replay_episodes()
    second = episodes.replay_episodes()
    assert first['created'] == 2 and first['skipped'] == 0
    assert second['created'] == 0 and second['skipped'] == 2
    assert first['by_verdict'] == second['by_verdict']
    assert len(db.memory_episodes_after(0)) == 2


def counts():
    with db.connect() as c:
        return {r['name']: c.execute('SELECT COUNT(*) FROM "' + r['name'] + '"').fetchone()[0]
                for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_dream_read_only_groups_and_cursor(monkeypatch):
    for mark in ('green', 'green', 'red', None):
        episodes.record_episode(run(mark=mark))
    monkeypatch.setenv('NODUS_WORKSPACE', 'other')
    episodes.record_episode(run(mark='green'))
    monkeypatch.setenv('NODUS_WORKSPACE', 'episodes-test')
    before = counts()
    preview = dream.dry_run()
    assert counts() == before
    assert preview['scanned'] == 4
    assert len(preview['groups']) == 3
    assert len(preview['proposals']) == 1
    assert preview['proposals'][0]['verdict'] == 'pass'
    assert dream.dry_run() == preview
    with db.connect() as c:
        c.execute('INSERT INTO dream_batch(id,status,cursor,started_at) VALUES(?,?,?,?)',
                  ('batch', 'staged', json.dumps({'workspace': 'episodes-test', 'seq': 2}), 1))
    assert dream.dry_run()['scanned'] == 2
    assert dream.dry_run(0)['scanned'] == 0


def test_cli_json_and_help(capsys):
    run(mark='green')
    assert main(['debug', 'replay-episodes', '--json'], prog='sodus') == 0
    assert json.loads(capsys.readouterr().out)['created'] == 1
    before = counts()
    assert main(['dream', '--dry-run', '--json'], prog='sodus') == 0
    assert json.loads(capsys.readouterr().out)['dry_run'] is True
    assert counts() == before
    assert main(['dream'], prog='sodus') == 1
    capsys.readouterr()
    assert 'dream' in build_parser('sodus').format_help()
    with pytest.raises(SystemExit) as exc:
        build_parser('sodus').parse_args(['dream', '--help'])
    assert exc.value.code == 0


def test_runner_episode_failure_is_isolated(monkeypatch, caplog):
    from nodus.runner import Runner
    row = run('running')
    def broken(*args, **kwargs):
        raise RuntimeError('episode unavailable')
    monkeypatch.setattr(episodes, 'record_episode', broken)
    Runner()._fail(row['id'], 'worker failed')
    assert db.get_run(row['id'])['status'] == 'failed'
    assert 'Could not record episode' in caplog.text


def test_runner_failure_records_episode():
    from nodus.runner import Runner
    row = run('running')
    Runner()._fail(row['id'], 'worker failed')
    stored = db.memory_episodes_after(0)
    assert len(stored) == 1
    assert stored[0]['verdict'] == 'failed'
    assert stored[0]['cause'] == 'worker failed'


@pytest.mark.parametrize('call', [lambda: dream.dry_run(-1),
                                 lambda: episodes.replay_episodes(limit=-1),
                                 lambda: episodes.replay_episodes(since='invalid')])
def test_invalid_arguments(call):
    with pytest.raises(ValueError):
        call()
