import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from sodus import config, db


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'HOME', tmp_path / 'state')
    monkeypatch.setenv('SODUS_WORKSPACE', 'staging-test')


def test_schema_and_reconnect():
    for _ in range(3):
        db._SCHEMA_READY.clear()  # exercise migration rather than the fast path
        with db.connect() as c:
            assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
            assert {'memory_episode', 'memory_candidate', 'memory_exposure', 'dream_batch', 'memory_fts', 'memory_fts_map'} <= names
            assert 'trigram' in c.execute("SELECT sql FROM sqlite_master WHERE name='memory_fts'").fetchone()[0]
    cid = db.memory_add_candidate('接口优先规则')
    db._SCHEMA_READY.clear()
    assert db.memory_get_candidate(cid)['text'] == '接口优先规则'


def test_v1_upgrade_preserves_rows():
    config.ensure_dirs()
    with sqlite3.connect(config.HOME / 'sodus.sqlite3') as c:
        c.executescript("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT); INSERT INTO meta VALUES('schema_version','1'); CREATE TABLE task(id TEXT PRIMARY KEY, title TEXT); INSERT INTO task VALUES('old','保留');")
    for _ in range(2):
        db._SCHEMA_READY.clear()
        with db.connect() as c:
            assert c.execute("SELECT title FROM task WHERE id='old'").fetchone()[0] == '保留'
            assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)


def test_episode_cursor():
    ids = [db.memory_add_episode(verdict=v, summary=v, created_at=1) for v in ('pass', 'failed', 'red')]
    rows = db.memory_episodes_after(0, 2)
    assert [r['id'] for r in rows] == ids[:2]
    assert [r['id'] for r in db.memory_episodes_after(rows[-1]['seq'], 2)] == ids[2:]
    assert db.memory_episodes_after(0, 0) == []


def test_dedupe_and_workspace():
    cid = db.memory_add_candidate('规则', workspace='a', hash='same')
    assert db.memory_add_candidate('另一个文本', workspace='a', hash='same') == cid
    assert db.memory_add_candidate('规则', workspace='b', hash='same') != cid
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert set(pool.map(lambda _: db.memory_add_candidate('规则', workspace='a', hash='same'), range(8))) == {cid}


def test_cas_and_index_update():
    cid = db.memory_add_candidate('数据库迁移')
    assert db.memory_update_candidate(cid, 1, text='接口优先规则')['version'] == 2
    with pytest.raises(ValueError, match='version conflict'):
        db.memory_update_candidate(cid, 1, text='覆盖')
    assert db.memory_get_candidate(cid)['text'] == '接口优先规则'
    assert db.memory_search_candidates('数据库迁移') == []
    assert db.memory_search_candidates('接口优先')[0]['id'] == cid
    assert db.memory_get_candidate('missing') is None
    with pytest.raises(ValueError):
        db.memory_update_candidate(cid, 2, version=99)


def test_exposure():
    cid = db.memory_add_candidate('接口优先规则')
    eid = db.memory_record_exposure(cid, 'task', 'run', 'retrieved')
    with db.connect() as c:
        row = dict(c.execute('SELECT * FROM memory_exposure WHERE id=?', (eid,)).fetchone())
        assert (row['candidate_id'], row['task_id'], row['run_id'], row['channel']) == (cid, 'task', 'run', 'retrieved')
        assert row['at'] > 0
    with pytest.raises(ValueError):
        db.memory_record_exposure('missing', None, None, 'displayed')


@pytest.mark.parametrize('query', ['接口', '接口优先', '数据库迁移', 'pytest 超时', 'watchdog'])
def test_search(query):
    cid = db.memory_add_candidate('接口优先规则，数据库迁移；pytest 超时，watchdog', workspace='a')
    assert db.memory_search_candidates(query, 'a')[0]['id'] == cid
    assert db.memory_search_candidates(query, 'b') == []
    assert db.memory_search_candidates(query, limit=0) == []


def test_no_fts_like_fallback():
    cid = db.memory_add_candidate('接口优先规则 100%')
    with db.connect() as c:
        c.execute('DROP TABLE memory_fts')
    assert db.memory_search_candidates('接口优先')[0]['id'] == cid
    assert db.memory_search_candidates('100%')[0]['id'] == cid
    assert db.memory_search_candidates('200%') == []
    db.memory_update_candidate(cid, 1, text='新的规则')
    assert db.memory_search_candidates('新的规则')[0]['id'] == cid


def test_tokenizer_fallback(caplog):
    class NoTrigram(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if "tokenize='trigram'" in sql:
                raise sqlite3.OperationalError('no such tokenizer: trigram')
            return super().execute(sql, parameters)
    with sqlite3.connect(':memory:', factory=NoTrigram) as c:
        db._migrate_v2(c)
        assert 'unicode61' in c.execute("SELECT sql FROM sqlite_master WHERE name='memory_fts'").fetchone()[0]
    assert 'trigram unavailable' in caplog.text
