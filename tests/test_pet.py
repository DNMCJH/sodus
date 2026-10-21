"""The pet: an ambient light for "is anything waiting for me?"."""

from __future__ import annotations

import json
import time

import pytest

from nodus import config, db, pet


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(config, "load_config", lambda: {})
    return tmp_path / "home"


def test_nothing_happening_reads_as_idle(home):
    assert pet.state()["mood"] == "idle"


def test_a_running_run_reads_as_working(home):
    task = db.create_task("t")
    run = db.create_run(task["id"], "claude", "D:\\w", route="claude")
    db.update_run(run["id"], status="running", started_at=time.time())
    found = pet.state()
    assert found["mood"] == "working"
    assert found["running"] == 1


def test_an_open_proposal_asks_for_attention(home):
    db.create_proposal("run-1", "task-1", "D:\\r", "b", "w", "base", "p", [], "1 file")
    found = pet.state()
    assert found["mood"] == "attention"
    assert "提案" in found["bubble"]


def test_a_red_suite_outranks_everything(home):
    task = db.create_task("t")
    run = db.create_run(task["id"], "claude", "D:\\w", route="claude")
    db.update_run(run["id"], status="completed", ended_at=time.time())
    db.append_event(run["id"], 1, "verify.red", {"ok": False})
    db.create_proposal("run-2", "task-1", "D:\\r", "b", "w", "base", "p", [], "1 file")
    found = pet.state()
    assert found["mood"] == "error"
    assert found["red"] == 1


def test_an_old_failure_stops_being_an_emergency(home):
    task = db.create_task("t")
    run = db.create_run(task["id"], "claude", "D:\\w")
    db.update_run(run["id"], status="failed", ended_at=time.time() - pet.RECENT - 60)
    assert pet.state()["mood"] == "idle"


def test_costs_are_never_added_across_currencies(home):
    task = db.create_task("t")
    for currency, cost in (("CNY", 1.5), ("USD", 0.25), ("CNY", 0.5)):
        run = db.create_run(task["id"], "claude", "D:\\w")
        db.update_run(
            run["id"],
            status="completed",
            ended_at=time.time(),
            usage_json=json.dumps({"cost": cost, "currency": currency}),
        )
    assert pet.state()["cost"] == {"CNY": 2.0, "USD": 0.25}


def test_describe_says_what_happened_in_one_line(home):
    run = {
        "id": "run-abc",
        "agent": "claude",
        "route": "claude",
        "usage_json": json.dumps({"cost": 0.1234, "currency": "CNY"}),
    }
    assert "测试红了" in pet.describe(run, "red")
    assert "¥0.1234" in pet.describe(run, "ok")


def test_the_bridge_is_off_until_configured(home):
    assert pet.push("hello") is False
    assert pet.notify("hello") is None  # no thread, no error


def test_the_bridge_reports_failure_instead_of_raising(home, monkeypatch):
    monkeypatch.setattr(
        config, "load_config", lambda: {"pet_bridge": {"enabled": True, "base": "http://127.0.0.1:9"}}
    )
    assert pet.push("hello") is False


def test_the_bridge_uses_the_pet_id_from_config(home, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {"pet_bridge": {"enabled": True, "pet": "sidekick"}},
    )

    def fake_call(method, path, body=None, timeout=3.0):
        seen.append(path)
        if path.startswith("/lock?"):
            return {"lockId": "L1"}
        return {"ok": True}

    monkeypatch.setattr(pet, "_call", fake_call)
    assert pet.push("hi") is True
    assert seen[0] == "/lock?pet=sidekick"
    assert seen[1].startswith("/broadcast?pet=sidekick&lock=L1")
    assert seen[2] == "/lock/release?pet=sidekick"
