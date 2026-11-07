import json

import pytest

from nodus import config, db, dream, memory


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'HOME', tmp_path/'state')
    monkeypatch.setenv('NODUS_WORKSPACE', 'w3c')
    monkeypatch.setattr(memory, 'saved_project', lambda: tmp_path)


def test_rollback_middle_and_candidate_state(tmp_path):
    target = tmp_path/'AGENTS.md'
    target.write_text('# Manual heading\n',encoding='utf-8')
    ids = [db.memory_add_candidate(t,workspace=str(tmp_path)) for t in ['alpha','beta','gamma']]
    snapshots = []
    for cid, text in zip(ids,['alpha','beta','gamma']):
        result = memory.apply_ops(target,[dict(op='add',text='- '+text,candidate_id=cid)])
        snapshots.append(target.read_text(encoding='utf-8'))
    assert result['memory_version'] == 3
    result = memory.rollback(target,2)
    assert result['memory_version'] == 4
    assert target.read_text(encoding='utf-8') == snapshots[1]
    assert [db.memory_get_candidate(cid)['status'] for cid in ids] == ['adopted','adopted','retired']
    memory.rollback(target,1)
    assert target.read_text(encoding='utf-8') == snapshots[0]
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM dream_batch WHERE memory_version IS NOT NULL').fetchone()[0] == 5


def test_rollback_drift_refused(tmp_path):
    target = tmp_path/'AGENTS.md'
    memory.apply_ops(target,[dict(op='add',text='- alpha')])
    target.write_text(target.read_text(encoding='utf-8').replace('- alpha','- human'),encoding='utf-8')
    before = target.read_bytes()
    with pytest.raises(RuntimeError,match='reindex'):
        memory.rollback(target,1)
    assert target.read_bytes() == before


def test_daily_order_dedupe_and_next_day(monkeypatch):
    calls = []
    for name in ['plan','extract','count']:
        monkeypatch.setattr(dream,name,lambda *args,n=name: calls.append(n) or {'id':'fake'})
    monkeypatch.setattr(dream,'apply_approved',lambda: calls.append('apply') or {})
    dream.daily_once('2026-11-08')
    assert calls == ['plan','extract','count','apply']
    assert dream.daily_once('2026-11-08')['status'] == 'already_run'
    dream.daily_once('2026-11-09')
    assert len(calls) == 8


def test_daily_cap_and_busy(monkeypatch):
    monkeypatch.setattr(config,'dream_daily_tokens',lambda: 0)
    with pytest.raises(RuntimeError,match='token limit'): dream.daily_once('2026-11-08')
    with db.connect() as c:
        assert not c.execute("SELECT 1 FROM meta WHERE key LIKE 'dream_last_run:%'").fetchone()
    task = db.create_task('busy')
    run = db.create_run(task['id'],'codex','.')
    db.update_run(run['id'],status='running')
    assert dream.daily_once('2026-11-08')['status'] == 'busy'


def test_daily_failure_claim_and_loop_survives(monkeypatch):
    monkeypatch.setattr(dream,'plan',lambda: (_ for _ in ()).throw(RuntimeError('broken')))
    with pytest.raises(RuntimeError): dream.daily_once('2026-11-08')
    assert dream.daily_once('2026-11-08')['status'] == 'already_run'
    monkeypatch.setattr(dream,'daily_once',lambda: (_ for _ in ()).throw(RuntimeError('broken')))
    import time
    monkeypatch.setattr(time,'sleep',lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
    logs = []
    with pytest.raises(KeyboardInterrupt): dream._daily_loop(logs.append)
    assert 'broken' in logs[0]


def test_approved_only(tmp_path):
    yes = db.memory_add_candidate('approved',workspace=str(tmp_path),status='adopted')
    no = db.memory_add_candidate('pending',workspace=str(tmp_path))
    assert dream.apply_approved()['status'] == 'applied'
    assert memory.marker_body((tmp_path/'AGENTS.md').read_text(encoding='utf-8')) == '- approved'
    assert db.memory_get_candidate(no)['status'] == 'proposed'
    assert dream.apply_approved()['status'] == 'idle'
    row = db.memory_get_candidate(yes)
    db.memory_update_candidate(yes,row['version'],status='retire_proposed')
    dream.apply_approved()
    assert db.memory_get_candidate(yes)['status'] == 'retired'


def test_rollback_cli(tmp_path,capsys):
    from nodus.cli import main
    target = tmp_path/'AGENTS.md'
    memory.apply_ops(target,[dict(op='add',text='- alpha')])
    assert main(['memory','rollback','--to','1','--project',str(tmp_path)],prog='sodus') == 0
    assert json.loads(capsys.readouterr().out)['memory_version'] == 2


def test_review_controls_and_board(tmp_path):
    from nodus import server, ui
    cid = db.memory_add_candidate('board fact',workspace=str(tmp_path))
    row = server._memory_board()[0]
    assert row['id'] == cid and row['version'] == 1
    assert row['evidence_count'] == 0 and row['status'] == 'proposed'
    for action in ['reject','edit','retire']:
        assert "','"+action+"')" in ui.PAGE
    assert 'encodeURIComponent(id)' in ui.PAGE
    assert '版本已变化，请刷新后重试' in ui.PAGE


def test_memory_board_merges_staged_and_harvest(monkeypatch, tmp_path):
    from nodus import memory, server
    staged = db.memory_add_candidate('same fact', workspace=str(tmp_path))
    harvest = [{'id':'h', 'text':'harvest fact', 'sources':[], 'conflict':False, 'adopted':False},
               {'id':'dup', 'text':'same fact', 'sources':[], 'conflict':False, 'adopted':False}]
    monkeypatch.setattr(memory, 'saved_project', lambda: tmp_path)
    monkeypatch.setattr(memory, 'harvest', lambda _: harvest)
    rows = server._memory_board()
    assert {r['text'] for r in rows} == {'same fact', 'harvest fact'}
    assert any(r['id'] == staged for r in rows)
