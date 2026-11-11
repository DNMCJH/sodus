"""Recovery: a daemon restart must not leave corpses or lose written files."""

from __future__ import annotations

import time
import json
from pathlib import Path

from sodus import config, db, runner, memory


def _journal(tmp_path, batch_id, target, current, before, after):
    state = config.HOME / 'memory'
    (state / 'journal').mkdir(parents=True, exist_ok=True)
    staging = str(target) + '.sodus-staging'
    payload = dict(batch_id=batch_id, target=str(target), staging=staging,
                   hash_before=before, hash_after=after, ops=[])
    (state / 'journal' / (batch_id + '.json')).write_text(json.dumps(payload), encoding='utf-8')
    Path(staging).write_text(current, encoding='utf-8')


def test_recover_journals_three_crash_branches_are_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'HOME', tmp_path / 'home')
    target = tmp_path / 'AGENTS.md'
    base = '<!-- sodus:facts:begin -->\n- before\n<!-- sodus:facts:end -->\n'
    changed = '<!-- sodus:facts:begin -->\n- after\n<!-- sodus:facts:end -->\n'
    before, after = memory.marker_hash(base), memory.marker_hash(changed)
    cases = [('before', base), ('after', changed), ('third-party', '<!-- sodus:facts:begin -->\n- other\n<!-- sodus:facts:end -->\n')]
    for name, content in cases:
        bid = 'batch-' + name
        target.write_text(content, encoding='utf-8')
        _journal(tmp_path, bid, target, changed, before, after)
        first = memory.recover_journals()
        second = memory.recover_journals()
        assert first == [(bid, {'before':'staged','after':'applied','third-party':'failed'}[name])]
        if name == 'third-party':
            assert second == [(bid, 'failed')]
        else:
            assert second == []
        journal = config.HOME / 'memory' / 'journal' / (bid + '.json')
        if name == 'third-party':
            assert journal.exists()
        else:
            assert not journal.exists()


def test_reap_orphans_fails_live_runs_and_keeps_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    work = tmp_path / "work"
    work.mkdir()
    task = db.create_task("t")
    run = db.create_run(task["id"], "dsh", str(work), None, "", "", "")
    # an orphan is a run whose recorded owner is gone, not merely "not ours"
    db.update_run(run["id"], status="running", started_at=time.time() - 5, owner_pid=999999)
    (work / "out.md").write_text("the worker got this far", encoding="utf-8")

    reaped = runner.RUNNER.reap_orphans()

    assert [r["id"] for r in reaped] == [run["id"]]
    stored = db.get_run(run["id"])
    assert stored["status"] == "failed"
    assert "orphaned" in stored["error"]
    paths = [a["path"] for a in db.artifacts_for(run["id"])]
    assert str(work / "out.md") in paths


def test_reap_orphans_leaves_finished_runs_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    task = db.create_task("t")
    run = db.create_run(task["id"], "claude", str(tmp_path))
    db.update_run(run["id"], status="completed", ended_at=time.time())

    assert runner.RUNNER.reap_orphans() == []
    assert db.get_run(run["id"])["status"] == "completed"


def test_the_second_start_has_nothing_to_reap(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    task = db.create_task("t")
    run = db.create_run(task["id"], "dsh", str(tmp_path))
    # the first start reaps runs whose owner died before it came up
    db.update_run(run["id"], owner_pid=999999)

    assert len(runner.RUNNER.reap_orphans()) == 1
    assert runner.RUNNER.reap_orphans() == []
