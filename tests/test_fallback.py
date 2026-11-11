"""Failover: a chain may only retry work that never started.

The fake CLI here is driven by its base URL: any endpoint whose host contains
`boom` answers 401. That keeps the test about the fallback rule instead of
about a mock library.
"""

from __future__ import annotations

import json
import sys
import textwrap

import pytest

from sodus import agents, config, db, providers, routes, runner

FAKE = textwrap.dedent(
    """
    import json, os, sys

    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    boom = "boom" in base
    touched = "touches" in base

    def emit(obj):
        print(json.dumps(obj), flush=True)

    if boom and touched:
        # it acted before the endpoint died: exactly the case that must not
        # be retried, because a second endpoint would replay the edit
        emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "x"}}
        ], "usage": {"input_tokens": 5, "output_tokens": 1}}})
        emit({"type": "result", "is_error": True, "result": "401 Unauthorized",
              "session_id": "s"})
        sys.exit(1)

    if boom:
        emit({"type": "result", "is_error": True, "result": "401 Unauthorized",
              "session_id": "s"})
        sys.exit(1)

    emit({"type": "system", "subtype": "init", "session_id": "s-ok", "model": "fake"})
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}],
          "usage": {"input_tokens": 10, "output_tokens": 5}}})
    emit({"type": "result", "result": "done", "session_id": "s-ok",
          "usage": {"input_tokens": 10, "output_tokens": 5}})
    sys.exit(0)
    """
)


@pytest.fixture()
def fake_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    scripts = tmp_path / "fake.py"
    scripts.write_text(FAKE, encoding="utf-8")
    monkeypatch.setitem(
        agents.SPECS,
        "fake",
        agents.AgentSpec(
            name="fake",
            vendor="claude",
            command=(sys.executable, str(scripts)),
            accepts=(providers.ANTHROPIC,),
        ),
    )
    work = tmp_path / "work"
    work.mkdir()

    def make(name: str, host: str) -> None:
        providers.upsert(
            providers.Provider(
                name=name,
                protocol=providers.ANTHROPIC,
                base_url=f"https://{host}.test",
                key_secret="FAKE_KEY",
            )
        )

    def chain(*names: str) -> None:
        routes.upsert(routes.Route(name="fake", agent="fake", providers=names))

    monkeypatch.setenv("FAKE_KEY", "sk")
    monkeypatch.setattr(config, "load_secrets", lambda ws=None: {"FAKE_KEY": "sk"})
    return type("Rig", (), {"make": staticmethod(make), "chain": staticmethod(chain), "work": work})


def _task() -> dict:
    return db.create_task("goal", acceptance="do it")


def test_an_endpoint_that_failed_before_acting_is_replaced(fake_chain):
    fake_chain.make("bad", "boom")
    fake_chain.make("good", "good")
    fake_chain.chain("bad", "good")
    task = _task()

    run = runner.RUNNER.start(task["id"], "fake", cwd=str(fake_chain.work))
    finished = runner.RUNNER.wait(run["id"], timeout=60)

    assert finished["status"] == "completed"
    assert finished["provider"] == "good"
    usage = json.loads(finished["usage_json"])
    assert [a["provider"] for a in usage["attempts"]] == ["bad", "good"]
    types = [e["type"] for e in db.events_after(run["id"])]
    assert "run.fallback" in types


def test_a_worker_that_already_acted_is_never_retried(fake_chain):
    """The whole reason the rule exists: no duplicated side effects."""
    fake_chain.make("bad", "boom-touches")
    fake_chain.make("good", "good")
    fake_chain.chain("bad", "good")
    task = _task()

    run = runner.RUNNER.start(task["id"], "fake", cwd=str(fake_chain.work))
    finished = runner.RUNNER.wait(run["id"], timeout=60)

    assert finished["status"] == "failed"
    assert finished["provider"] == "bad"
    usage = json.loads(finished["usage_json"])
    assert [a["provider"] for a in usage["attempts"]] == ["bad"]
    types = [e["type"] for e in db.events_after(run["id"])]
    assert "run.fallback" not in types


def test_the_chain_ends_when_the_last_endpoint_also_fails(fake_chain):
    fake_chain.make("bad", "boom")
    fake_chain.make("worse", "boom-two")
    fake_chain.chain("bad", "worse")
    task = _task()

    run = runner.RUNNER.start(task["id"], "fake", cwd=str(fake_chain.work))
    finished = runner.RUNNER.wait(run["id"], timeout=60)

    assert finished["status"] == "failed"
    usage = json.loads(finished["usage_json"])
    assert [a["provider"] for a in usage["attempts"]] == ["bad", "worse"]


def test_success_on_the_first_endpoint_records_one_attempt(fake_chain):
    fake_chain.make("good", "good")
    fake_chain.chain("good")
    task = _task()

    run = runner.RUNNER.start(task["id"], "fake", cwd=str(fake_chain.work))
    finished = runner.RUNNER.wait(run["id"], timeout=60)

    assert finished["status"] == "completed"
    usage = json.loads(finished["usage_json"])
    assert [a["provider"] for a in usage["attempts"]] == ["good"]


def test_the_handoff_names_the_endpoint_that_ran(fake_chain):
    fake_chain.make("bad", "boom")
    fake_chain.make("good", "good")
    fake_chain.chain("bad", "good")
    task = _task()

    run = runner.RUNNER.start(task["id"], "fake", cwd=str(fake_chain.work))
    runner.RUNNER.wait(run["id"], timeout=60)

    from pathlib import Path

    text = Path(config.HOME / "runs" / run["id"] / "handoff.md").read_text(encoding="utf-8")
    assert "端点：good" in text
    assert "回退：bad → good" in text
