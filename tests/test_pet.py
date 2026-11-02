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
    assert pet.state()["cost_unlabeled"] == []


def test_a_missing_currency_is_not_filed_under_usd(home):
    task = db.create_task("t")
    for provider, usage in (
        ("relay-old", {"cost": 0.25}),
        ("relay-old", {"cost": 0.5}),
        ("relay-cny", {"cost": 1.5, "currency": "CNY"}),
    ):
        run = db.create_run(task["id"], "claude", "D:\\w", provider=provider)
        db.update_run(
            run["id"],
            status="completed",
            ended_at=time.time(),
            usage_json=json.dumps(usage),
        )
    found = pet.state()
    assert found["cost"] == {"CNY": 1.5}
    assert found["cost_unlabeled"] == [
        {"provider": "relay-old", "total": 0.75, "runs": 2}
    ]


def test_describe_does_not_lend_a_currency_symbol_to_an_unlabeled_run(home):
    run = {
        "id": "run-abc",
        "agent": "claude",
        "route": "claude",
        "usage_json": json.dumps({"cost": 0.1234}),
    }
    line = pet.describe(run, "ok")
    assert "未标注币种 0.1234" in line
    assert "$" not in line


def test_describe_says_what_happened_in_one_line(home):
    run = {
        "id": "run-abc",
        "agent": "claude",
        "route": "claude",
        "usage_json": json.dumps({"cost": 0.1234, "currency": "CNY"}),
    }
    assert "测试红了" in pet.describe(run, "red")
    assert "¥0.1234" in pet.describe(run, "ok")


def test_the_animation_pool_names_are_the_ones_the_pet_ships(home):
    """These are the labels its right-click menu shows; a rename breaks silently."""
    assert pet.ANIM["done"] == "工作状态-雀跃庆祝"
    assert pet.ANIM["error"] == "工作状态-垂头叹气冒汗"
    assert all(name.startswith("工作状态-") for name in pet.ANIM.values())


def test_narrate_is_off_until_configured(home):
    assert pet.narrate("hello", "done") is None  # no thread, no error


def test_animate_is_off_until_configured(home):
    assert pet.animate("工作状态-雀跃庆祝") is False


def test_a_broken_pet_never_breaks_the_caller(home, monkeypatch):
    monkeypatch.setattr(
        config, "load_config", lambda: {"pet_bridge": {"enabled": True, "base": "http://127.0.0.1:9"}}
    )
    assert pet.animate("工作状态-雀跃庆祝") is False
    assert pet.push("hi") is False


def test_auto_base_searches_instead_of_hardcoding_a_port(home, monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: {"pet_bridge": {"enabled": True, "base": "auto"}})
    monkeypatch.setattr(pet, "_alive", lambda base: base.startswith("http://127.0.0.1:3082"))
    monkeypatch.setattr(pet, "_DISCOVERED", "")
    assert pet.base_url().startswith("http://127.0.0.1:3082")


def test_the_bridge_is_off_until_configured(home):
    assert pet.push("hello") is False
    assert pet.notify("hello") is None  # no thread, no error


def test_the_bridge_reports_failure_instead_of_raising(home, monkeypatch):
    monkeypatch.setattr(
        config, "load_config", lambda: {"pet_bridge": {"enabled": True, "base": "http://127.0.0.1:9"}}
    )
    assert pet.push("hello") is False


def test_a_build_without_a_lock_still_gets_the_message(home, monkeypatch):
    """dsh-pet 0.3.7 has no /lock route; broadcast works on its own."""
    seen: list[str] = []
    monkeypatch.setattr(
        config, "load_config", lambda: {"pet_bridge": {"enabled": True, "pet": "main"}}
    )

    def fake_call(method, path, body=None, timeout=3.0):
        seen.append(path)
        if path.startswith("/lock?"):
            return None  # this build rejects it
        return {"ok": True}

    monkeypatch.setattr(pet, "_call", fake_call)
    assert pet.push("hi") is True
    assert seen == ["/lock?pet=main", "/broadcast?pet=main"]


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
    assert len(seen) == 3
