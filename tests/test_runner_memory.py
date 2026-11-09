"""Resume briefs carry the project's confirmed facts — when they have to.

`Runner.start(session_id=...)` used to send the operator's note verbatim: the
session had history, so the code assumed context was complete. Facts confirmed
after the last turn are not in that history. A harness that reads `AGENTS.md`
sees them anyway; one that does not would be the only worker blind to them, so
the runner injects `## 已验证事实` into the resumed brief for exactly those
routes and for nobody else.
"""

from __future__ import annotations

import sys
import time
from datetime import date

import pytest

from nodus import agents, config, db, memory, providers, routes, runner


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-resume")
    (tmp_path / "home").mkdir()
    return tmp_path


def _stub(monkeypatch, reads_memory_files: bool = False):
    """Route/agent plumbing plus the two calls that would spawn a CLI."""
    provider = providers.Provider(name="p0", protocol=providers.ANTHROPIC)
    spec = agents.AgentSpec(
        name="fake",
        vendor="claude",
        command=(sys.executable, "-c", "pass"),
        accepts=(providers.ANTHROPIC,),
        reads_memory_files=reads_memory_files,
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


def _resume_prompt(tmp_path, include_memory: bool = True) -> str:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "AGENTS.md").write_text(
        "# 项目\n\n## 已验证事实\n\n"
        f"- {date.today().isoformat()}（来源 CLAUDE.md）构建脚本统一用 pnpm\n",
        encoding="utf-8",
    )
    task = db.create_task("把构建脚本迁到 pnpm", acceptance="pytest green")
    run = runner.RUNNER.start(
        task["id"],
        "fake",
        cwd=str(work),
        note="继续上次的迁移",
        session_id="ses-existing-0001",
        include_memory=include_memory,
    )
    finished = runner.RUNNER.wait(run["id"], timeout=10)
    assert finished["status"] == "completed"
    return (config.HOME / "runs" / run["id"] / "prompt.md").read_text(encoding="utf-8")


def test_resume_brief_keeps_the_note_and_adds_the_facts(home, monkeypatch, tmp_path):
    _stub(monkeypatch, reads_memory_files=False)
    prompt = _resume_prompt(tmp_path)

    assert prompt.startswith("继续上次的迁移")
    assert "## 已验证事实" in prompt
    assert "构建脚本统一用 pnpm" in prompt


def test_resume_brief_can_switch_memory_off(home, monkeypatch, tmp_path):
    _stub(monkeypatch, reads_memory_files=False)
    prompt = _resume_prompt(tmp_path, include_memory=False)

    assert prompt == "继续上次的迁移"
    assert "已验证事实" not in prompt


def test_resume_brief_leaves_a_file_reading_harness_alone(home, monkeypatch, tmp_path):
    # Claude Code / Codex / DSH open AGENTS.md themselves: the note is the whole
    # brief, and the facts are not paid for a second time on every resume.
    _stub(monkeypatch, reads_memory_files=True)
    prompt = _resume_prompt(tmp_path)

    assert prompt == "继续上次的迁移"
    assert "已验证事实" not in prompt
