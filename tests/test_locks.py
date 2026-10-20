"""Write lock: one directory, one writer, enforced by the database."""

from __future__ import annotations

import sys
import textwrap
import time

import pytest

from nodus import agents, config, db, providers, routes, runner

SLOW = textwrap.dedent(
    """
    import json, sys, time

    def emit(obj):
        print(json.dumps(obj), flush=True)

    emit({"type": "system", "subtype": "init", "session_id": "s-lock", "model": "fake"})
    print("working...", flush=True)          # a non-JSON line: proves the pump runs
    time.sleep(1.5)
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}],
          "usage": {"input_tokens": 3, "output_tokens": 1}}})
    emit({"type": "result", "result": "done", "session_id": "s-lock",
          "usage": {"input_tokens": 3, "output_tokens": 1}})
    sys.exit(0)
    """
)


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    script = tmp_path / "slow.py"
    script.write_text(SLOW, encoding="utf-8")
    monkeypatch.setitem(
        agents.SPECS,
        "fake",
        agents.AgentSpec(
            name="fake", vendor="claude", command=(sys.executable, str(script)),
            accepts=(providers.ANTHROPIC,),
        ),
    )
    providers.upsert(
        providers.Provider(
            name="p", protocol=providers.ANTHROPIC,
            base_url="https://p.test", key_secret="K",
        )
    )
    routes.upsert(routes.Route(name="fake", agent="fake", providers=("p",)))
    monkeypatch.setattr(config, "load_secrets", lambda ws=None: {"K": "sk"})
    work = tmp_path / "work"
    work.mkdir()
    return type("Rig", (), {"work": work, "task": db.create_task("goal")})


def test_a_second_run_is_refused_while_the_first_owns_the_directory(rig):
    first = runner.RUNNER.start(rig.task["id"], "fake", cwd=str(rig.work))
    with pytest.raises(RuntimeError, match="already being written"):
        runner.RUNNER.start(rig.task["id"], "fake", cwd=str(rig.work))
    # the refusal names the holder, so you can go look at it
    with pytest.raises(RuntimeError, match=first["id"]):
        runner.RUNNER.start(rig.task["id"], "fake", cwd=str(rig.work))
    done = runner.RUNNER.wait(first["id"], timeout=60)
    assert done["status"] == "completed"


def test_the_lock_is_free_once_the_run_ends(rig):
    first = runner.RUNNER.start(rig.task["id"], "fake", cwd=str(rig.work))
    runner.RUNNER.wait(first["id"], timeout=60)
    second = runner.RUNNER.start(rig.task["id"], "fake", cwd=str(rig.work))
    assert second["status"] in ("queued", "running")
    runner.RUNNER.wait(second["id"], timeout=60)


def test_two_directories_can_be_written_at_once(rig, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    a = runner.RUNNER.start(rig.task["id"], "fake", cwd=str(rig.work))
    b = runner.RUNNER.start(rig.task["id"], "fake", cwd=str(other))
    assert a["id"] != b["id"]
    runner.RUNNER.wait(a["id"], timeout=60)
    runner.RUNNER.wait(b["id"], timeout=60)


def test_a_finished_holders_lock_is_debris_not_a_block(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    task = db.create_task("t")
    work = tmp_path / "w"
    work.mkdir()
    first = db.create_run(task["id"], "claude", str(work), lock=True)
    db.update_run(first["id"], status="completed", ended_at=time.time())

    second = db.create_run(task["id"], "claude", str(work), lock=True)

    assert second["id"] != first["id"]
    assert db.write_holder(work)["run_id"] == second["id"]


def test_reaping_an_orphan_frees_its_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    task = db.create_task("t")
    work = tmp_path / "w"
    work.mkdir()
    run = db.create_run(task["id"], "dsh", str(work), lock=True)
    db.update_run(run["id"], status="running", started_at=time.time() - 5)
    assert db.write_holder(work)["run_id"] == run["id"]

    runner.RUNNER.reap_orphans()

    assert db.write_holder(work) == {}


def test_the_holder_is_visible_from_the_outside(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    task = db.create_task("t")
    work = tmp_path / "w"
    work.mkdir()
    run = db.create_run(task["id"], "claude", str(work), route="claude", lock=True)

    locks = db.active_locks()
    assert [row["run_id"] for row in locks] == [run["id"]]
    assert locks[0]["cwd"] == str(work.resolve())

    assert db.release_lock_for_cwd(str(work)) is True
    assert db.active_locks() == []


def test_a_failed_run_still_hands_the_directory_back(tmp_path, monkeypatch):
    """The failure path is the one that leaks locks in practice."""
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    task = db.create_task("t")
    work = tmp_path / "w"
    work.mkdir()
    run = db.create_run(task["id"], "claude", str(work), lock=True)

    runner.RUNNER._fail(run["id"], "boom")

    assert db.get_run(run["id"])["status"] == "failed"
    assert db.write_holder(work) == {}
