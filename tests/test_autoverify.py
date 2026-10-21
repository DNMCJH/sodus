"""Auto-verify: a run that changed files runs the project's tests before it closes."""

from __future__ import annotations

import sys
import textwrap

from nodus import agents, config, db, providers, routes, runner, verify

WRITER = textwrap.dedent(
    """
    import json, pathlib, sys

    def emit(obj):
        print(json.dumps(obj), flush=True)

    emit({"type": "system", "subtype": "init", "session_id": "s-av", "model": "fake"})
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Write", "input": {}}
    ], "usage": {"input_tokens": 4, "output_tokens": 2}}})
    pathlib.Path("out.txt").write_text("done", encoding="utf-8")
    emit({"type": "result", "result": "done", "session_id": "s-av",
          "usage": {"input_tokens": 4, "output_tokens": 2}})
    sys.exit(0)
    """
)

IDLE = textwrap.dedent(
    """
    import json, sys

    print(json.dumps({"type": "system", "subtype": "init", "session_id": "s-idle",
                      "model": "fake"}), flush=True)
    print(json.dumps({"type": "result", "result": "nothing to do", "session_id": "s-idle",
                      "usage": {"input_tokens": 2, "output_tokens": 1}}), flush=True)
    sys.exit(0)
    """
)


def _rig(tmp_path, monkeypatch, script_body: str):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setattr(config, "auto_verify", lambda: True)
    script = tmp_path / "fake.py"
    script.write_text(script_body, encoding="utf-8")
    monkeypatch.setitem(
        agents.SPECS,
        "fake",
        agents.AgentSpec(
            name="fake", vendor="claude", command=(sys.executable, str(script)),
            accepts=(providers.ANTHROPIC,),
        ),
    )
    providers.upsert(
        providers.Provider(name="p", protocol=providers.ANTHROPIC,
                           base_url="https://p.test", key_secret="K")
    )
    routes.upsert(routes.Route(name="fake", agent="fake", providers=("p",)))
    monkeypatch.setattr(config, "load_secrets", lambda ws=None: {"K": "sk"})
    work = tmp_path / "proj"
    work.mkdir()
    (work / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return work


def _run(work, verify_run=None) -> dict:
    task = db.create_task("goal")
    run = runner.RUNNER.start(task["id"], "fake", cwd=str(work), verify_run=verify_run)
    return runner.RUNNER.wait(run["id"], timeout=120)


def test_a_run_that_wrote_files_gets_its_tests_run(tmp_path, monkeypatch):
    work = _rig(tmp_path, monkeypatch, WRITER)
    (work / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    done = _run(work)

    assert done["status"] == "completed"
    assert db.verify_states([done["id"]]).get(done["id"], "none") == "ok"
    types = [e["type"] for e in db.events_after(done["id"])]
    assert "verify.started" in types and "verify.green" in types
    handoff = (config.HOME / "runs" / done["id"] / "handoff.md").read_text(encoding="utf-8")
    assert "测试通过" in handoff


def test_a_red_suite_turns_the_run_into_red_evidence(tmp_path, monkeypatch):
    work = _rig(tmp_path, monkeypatch, WRITER)
    (work / "test_bad.py").write_text("def test_bad():\n    assert 0\n", encoding="utf-8")

    done = _run(work)

    # the process still exited 0 — that is the whole point of a separate mark
    assert done["status"] == "completed"
    assert db.verify_states([done["id"]]).get(done["id"], "none") == "red"
    handoff = (config.HOME / "runs" / done["id"] / "handoff.md").read_text(encoding="utf-8")
    assert "测试跑过但失败" in handoff


def test_no_verify_leaves_the_run_unchecked(tmp_path, monkeypatch):
    work = _rig(tmp_path, monkeypatch, WRITER)
    (work / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    done = _run(work, verify_run=False)

    assert db.verify_states([done["id"]]).get(done["id"], "none") == "none"
    assert "verify.green" not in [e["type"] for e in db.events_after(done["id"])]


def test_a_run_that_changed_nothing_is_not_tested(tmp_path, monkeypatch):
    work = _rig(tmp_path, monkeypatch, IDLE)
    (work / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    done = _run(work)

    assert db.verify_states([done["id"]]).get(done["id"], "none") == "none"


def test_an_unknown_project_is_left_alone(tmp_path, monkeypatch):
    """No markers means no safe command, and guessing is how you break a tree."""
    work = _rig(tmp_path, monkeypatch, WRITER)
    (work / "pyproject.toml").unlink()
    (work / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    done = _run(work)

    assert db.verify_states([done["id"]]).get(done["id"], "none") == "none"
    assert verify.detect(work) is None


def test_the_marker_detector_only_knows_standard_suites(tmp_path):
    assert verify.detect(tmp_path) is None
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    assert verify.detect(tmp_path)["kind"] == "go"
