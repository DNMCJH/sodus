import json
from types import SimpleNamespace

from sodus import config, db
from sodus.cli.commands.memory import memory_stats


def test_memory_stats_rot_json_uses_temp_db(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    config.ensure_dirs()
    monkeypatch.setattr(db, "now", lambda: 2_000_000_000.0)
    a = db.memory_add_candidate("same", key="rule", status="active", last_seen=1)
    b = db.memory_add_candidate("different", key="rule", status="active", last_seen=1)
    with db.connect() as c:
        # Simulate a historically corrupted DB: the dedupe index is dropped,
        # duplicate hashes are written, and the index is NOT restored (a unique
        # index cannot coexist with the duplicates we are testing for). The
        # database is a temp home owned by this test.
        c.execute("DROP INDEX idx_memory_candidate_dedupe")
        c.execute("UPDATE memory_candidate SET hash='dup' WHERE id IN (?, ?)", (a, b))
        c.execute("UPDATE memory_candidate SET expires_at=? WHERE id=?", (1, a))
    db.memory_record_exposure(a, "task", "run", "retrieved")
    with db.connect() as c:
        c.execute("UPDATE memory_candidate SET hits=1 WHERE id=?", (a,))
    memory_stats(SimpleNamespace(json=True))
    result = json.loads(capsys.readouterr().out)
    assert result["entries"] == 2
    assert result["duplicate_hashes"] == 1
    assert result["contradictions"] == 1
    assert result["expired"] == 2
    assert result["exposures"] == 1
    assert result["hits"] == 1
