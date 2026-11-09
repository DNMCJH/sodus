"""`--route auto` and the policy event that explains it.

The worker is stubbed at `_attempt` / `_finalize`: these tests are about which
route gets chosen and what the run records, not about any CLI. No network —
`decision.available` is forced off so `assess()` never calls Jev.
"""

from __future__ import annotations

import sys
import time

import pytest

from nodus import agents, config, db, decision, providers, routes, runner
from nodus.cli import build_parser


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-auto")
    (tmp_path / "home").mkdir()
    return tmp_path


@pytest.fixture(autouse=True)
def rules_only(monkeypatch):
    monkeypatch.setattr(decision, "available", lambda: (False, "no jev in tests"))


@pytest.fixture()
def stub(monkeypatch):
    """A fake provider/agent pair; the route keeps whatever name was asked for.

    Patching `resolve_for_run` with a recording wrapper matters: the assertion
    is about the *name* the policy layer picked, and a stub that returns one
    fixed route would hide exactly that.
    """
    provider = providers.Provider(name="p0", protocol=providers.ANTHROPIC)
    spec = agents.AgentSpec(
        name="fake",
        vendor="claude",
        command=(sys.executable, "-c", "pass"),
        accepts=(providers.ANTHROPIC,),
    )
    seen: list[str] = []

    def fake_resolve(name: str):
        seen.append(name)
        return routes.Route(name=name, agent="fake", providers=("p0",))

    monkeypatch.setattr(routes, "resolve_for_run", fake_resolve)
    monkeypatch.setattr(routes, "problems", lambda value: [])
    monkeypatch.setattr(routes, "ready", lambda name: True)
    monkeypatch.setattr(providers, "load", lambda: {"p0": provider})
    monkeypatch.setattr(agents, "get", lambda name: spec)
    monkeypatch.setattr(agents, "missing", lambda spec, value, secrets: [])
    monkeypatch.setattr(runner.pet, "narrate", lambda *args, **kwargs: None)

    def fake_attempt(
        self, rid, index, spec, provider, workdir, secrets, profile_name,
        prompt, effort, model, session_id, session_mode, readonly=False,
    ):
        stamp = time.time()
        return {
            "index": index,
            "provider": provider.name,
            "model": model or "",
            "started_at": stamp,
            "ended_at": stamp,
            "exit_code": 0,
            "error": "",
            "errors": [],
            "ok": True,
            "tool_calls": 0,
            "output_lines": 1,
            "noise": [],
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_read": 0,
                "cache_write": 0,
            },
            "texts": ["ok"],
            "session_ref": {},
        }

    def fake_finalize(self, rid, *args, **kwargs):
        db.update_run(rid, status="completed", ended_at=time.time())

    monkeypatch.setattr(runner.Runner, "_attempt", fake_attempt)
    monkeypatch.setattr(runner.Runner, "_finalize", fake_finalize)
    return seen


def _run(tmp_path, *, goal: str, route: str, acceptance: str = "", note: str = "") -> dict:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    task = db.create_task(goal, acceptance=acceptance)
    started = runner.RUNNER.start(task["id"], route, cwd=str(work), note=note)
    finished = runner.RUNNER.wait(started["id"], timeout=10)
    assert finished["status"] == "completed", finished
    return finished


def _event(rid: str, etype: str) -> dict:
    return next(e for e in db.events_after(rid) if e["type"] == etype)


def test_auto_uses_the_policy_recommendation(home, stub, tmp_path):
    run = _run(tmp_path, goal="给 README 改个错别字", route="auto")

    assert run["route"] == "dsh"
    decision_event = _event(run["id"], "policy.decision")
    payload = decision_event["payload"]
    assert payload["chosen_by"] == "policy"
    assert payload["chosen_route"] == "dsh"
    assert payload["requested_route"] == "auto"
    assert payload["assess"]["difficulty"] == 1
    assert payload["assess"]["kind"] == "writing"
    assert payload["recommend"]["route"] == "dsh"


def test_auto_judges_title_goal_and_acceptance(home, stub, tmp_path):
    run = _run(
        tmp_path,
        goal="把 nodus/db.py 里的连接管理重构成上下文管理器",
        acceptance="pytest 全绿",
        route="auto",
    )

    payload = _event(run["id"], "policy.decision")["payload"]
    assert payload["assess"]["difficulty"] >= 3
    assert run["route"] == payload["recommend"]["route"]


def test_a_broken_policy_still_dispatches_on_the_default_route(home, stub, tmp_path, monkeypatch):
    def explode(assessment):
        raise RuntimeError("recommend exploded")

    monkeypatch.setattr(runner.policy, "recommend", explode)

    run = _run(tmp_path, goal="给 README 改个错别字", route="auto")

    assert run["route"] == "claude"
    fallback = _event(run["id"], "policy.fallback")["payload"]
    assert fallback["reason"] == "policy_error"
    assert fallback["fallback_route"] == "claude"
    assert "RuntimeError" in fallback["error"]


def test_an_empty_recommendation_is_a_policy_error(home, stub, tmp_path, monkeypatch):
    monkeypatch.setattr(runner.policy, "recommend", lambda assessment: {})

    run = _run(tmp_path, goal="给 README 改个错别字", route="auto")

    assert run["route"] == "claude"
    fallback = _event(run["id"], "policy.fallback")["payload"]
    assert fallback["reason"] == "policy_error"
    assert "no route" in fallback["error"]


def test_auto_skips_a_recommended_route_the_machine_cannot_run(home, stub, tmp_path, monkeypatch):
    # The policy still recommends dsh for the typo; only claude works here.
    monkeypatch.setattr(routes, "ready", lambda name: name == "claude")

    run = _run(tmp_path, goal="给 README 改个错别字", route="auto")

    assert run["route"] == "claude"
    fallback = _event(run["id"], "policy.fallback")["payload"]
    assert fallback["reason"] == "recommended route is not ready"
    assert fallback["recommended"] == "dsh"
    assert fallback["fallback_route"] == "claude"


def test_an_explicit_route_is_recorded_as_the_operators_choice(home, stub, tmp_path):
    run = _run(tmp_path, goal="给 README 改个错别字", route="codex")

    assert run["route"] == "codex"
    payload = _event(run["id"], "policy.decision")["payload"]
    assert payload["chosen_by"] == "operator"
    assert payload["chosen_route"] == "codex"
    assert payload["requested_route"] == "codex"
    # The assessment is context, not authority: policy would have said dsh.
    assert payload["recommend"]["route"] == "dsh"
    assert not any(e["type"] == "policy.fallback" for e in db.events_after(run["id"]))


def test_a_policy_failure_on_an_explicit_route_does_not_block_the_run(
    home, stub, tmp_path, monkeypatch
):
    def explode(text, **kwargs):
        raise RuntimeError("assess exploded")

    monkeypatch.setattr(runner.policy, "assess", explode)

    run = _run(tmp_path, goal="给 README 改个错别字", route="codex")

    assert run["route"] == "codex"
    assert not any(e["type"].startswith("policy.") for e in db.events_after(run["id"]))


def test_cli_run_sends_auto_to_the_daemon(monkeypatch):
    from nodus.cli.commands import tasks as tasks_cmd

    sent: dict = {}

    def fake_call(method, path, body=None):
        sent.update(body or {})
        return {"id": "run-test", "route": "dsh"}

    monkeypatch.setattr(tasks_cmd, "call", fake_call)
    monkeypatch.setattr(tasks_cmd, "watch", lambda rid, after=0: 0)

    args = build_parser().parse_args(["run", "task-test", "--route", "auto"])
    assert tasks_cmd.run(args) == 0
    assert sent["route"] == "auto"


def test_runner_passes_readonly_to_attempt(home, stub, tmp_path, monkeypatch):
    original = runner.Runner._attempt
    seen = []
    def attempt(self, *args, **kwargs):
        seen.append(kwargs.get('readonly'))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(runner.Runner, '_attempt', attempt)
    task = db.create_task('readonly planning')
    run = runner.RUNNER.start(task['id'], 'codex', cwd=str(tmp_path), readonly=True, kind='plan')
    assert runner.RUNNER.wait(run['id'], timeout=10)['status'] == 'completed'
    assert seen == [True]
