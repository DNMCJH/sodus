"""Resume briefs carry remembered facts too.

`Runner.start(session_id=...)` used to send the operator's note verbatim: the
session had history, so the code assumed context was complete. Project facts
confirmed after the last turn are not in that history, and the resumed worker
was the only one who could not see them.
"""

from __future__ import annotations

import sys
import time

import pytest

from nodus import agents, config, db, memory, providers, routes, runner


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-resume")
    (tmp_path / "home").mkdir()
    return tmp_path


@pytest.fixture()
def stub(monkeypatch):
    """Route/agent plumbing plus the two calls that would spawn a CLI."""
    provider = providers.Provider(name="p0", protocol=providers.ANTHROPIC)
    spec = agents.AgentSpec(
        name="fake",
        vendor="claude",
        command=(sys.executable, "-c", "pass"),
        accepts=(providers.ANTHROPIC,),
    )
    monkeypatch.setattr(
        routes,
        "resolve_for_run",
        lambda name: routes.Route(name=name, agent="fake", providers=("p0",)),
    )
    monkeypatch.setattr(routes, "problems", lambda value: [])
    monkeypatch.setattr(providers, "load", lambda: {"p0": provider})
    monkeypatch.setattr(agents, "get", lambda name: spec)
    monkeypatch.setattr(agents, "missing", lambda spec, value, secrets: [])
    monkeypatch.setattr(runner.pet, "narrate", lambda *args, **kwargs: None)

    def fake_attempt(
        self, rid, index, spec, provider, workdir, secrets, profile_name,
        prompt, effort, model, session_id, session_mode,
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


def _resume_prompt(tmp_path, include_memory: bool = True) -> str:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    task = db.create_task("把构建脚本迁到 pnpm", acceptance="pytest green")
    memory.remember("构建脚本统一用 pnpm", scope="project", key="build")
    run = runner.RUNNER.start(
        task["id"],
        "claude",
        cwd=str(work),
        note="继续上次的迁移",
        session_id="ses-existing-0001",
        include_memory=include_memory,
    )
    finished = runner.RUNNER.wait(run["id"], timeout=10)
    assert finished["status"] == "completed"
    return (config.HOME / "runs" / run["id"] / "prompt.md").read_text(encoding="utf-8")


def test_resume_brief_keeps_the_note_and_adds_memory(home, stub, tmp_path):
    prompt = _resume_prompt(tmp_path)

    assert prompt.startswith("继续上次的迁移")
    assert "以下是这个项目已确认的事实：" in prompt
    assert "- [project/build] 构建脚本统一用 pnpm" in prompt


def test_resume_brief_can_switch_memory_off(home, stub, tmp_path):
    prompt = _resume_prompt(tmp_path, include_memory=False)

    assert prompt == "继续上次的迁移"
    assert "以下是这个项目已确认的事实" not in prompt
