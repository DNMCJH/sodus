"""opencode as a worker: the prompt travels as an argument, the stream is its own.

The fake CLI below speaks opencode's `--format json` dialect and refuses to run
if anything arrives on stdin — which is exactly the regression this guards:
the runner hands every other agent its brief on stdin, and doing that here
would either be ignored or fill a pipe nobody drains.
"""

from __future__ import annotations

import json
import sys
import textwrap

import pytest

from sodus import agents, config, db, providers, routes, runner

FAKE = textwrap.dedent(
    """
    import json, sys

    if sys.stdin is not None and not sys.stdin.isatty():
        if sys.stdin.read().strip():
            print("stdin was written to", file=sys.stderr, flush=True)
            sys.exit(3)

    prompt = sys.argv[-1]
    session = "ses_fake0001"

    def emit(obj):
        print(json.dumps(obj), flush=True)

    emit({"type": "step_start", "sessionID": session, "part": {"type": "step-start"}})
    emit({"type": "text", "sessionID": session, "part": {"type": "text", "text": prompt}})
    emit({"type": "step_finish", "sessionID": session, "part": {
        "type": "step-finish",
        "tokens": {"input": 11, "output": 3, "reasoning": 2, "cache": {"read": 0, "write": 0}},
        "cost": 0,
    }})
    sys.exit(0)
    """
)


@pytest.fixture()
def fake_opencode(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    script = tmp_path / "fake_opencode.py"
    script.write_text(FAKE, encoding="utf-8")
    monkeypatch.setitem(
        agents.SPECS,
        "fake-opencode",
        agents.AgentSpec(
            name="fake-opencode",
            vendor="opencode",
            command=(sys.executable, str(script)),
            accepts=(providers.OPENAI,),
            resume_mode="flag",
            fork_mode="flag",
        ),
    )
    providers.upsert(
        providers.Provider(
            name="fake-openai",
            protocol=providers.OPENAI,
            base_url="https://billing-only.test/v1",
            key_secret="FAKE_KEY",
            # no meter and no price list: this run must not invent a cost
        )
    )
    routes.upsert(
        routes.Route(
            name="fake-opencode",
            agent="fake-opencode",
            providers=("fake-openai",),
            model="fake/model-1",
        )
    )
    monkeypatch.setattr(config, "load_secrets", lambda ws=None: {"FAKE_KEY": "sk"})
    work = tmp_path / "work"
    work.mkdir()
    return work


def test_the_prompt_reaches_opencode_as_an_argument(fake_opencode):
    task = db.create_task("写一个 hello", acceptance="文件存在")
    run = runner.RUNNER.start(task["id"], "fake-opencode", cwd=str(fake_opencode))
    finished = runner.RUNNER.wait(run["id"], timeout=60)

    assert finished["status"] == "completed", finished.get("error")
    # the fake echoes the argument it received; stdin was refused (exit 3)
    final = (config.HOME / "runs" / run["id"] / "final.md").read_text(encoding="utf-8")
    assert "你是 Sodus 里负责执行任务的 agent（fake-opencode）" in final
    assert "写一个 hello" in final


def test_a_run_records_its_own_brief_version(fake_opencode):
    """`run.harness` is what makes two prompt versions comparable later."""
    task = db.create_task("记录 harness 指纹", acceptance="文件存在")
    run = runner.RUNNER.start(task["id"], "fake-opencode", cwd=str(fake_opencode))
    runner.RUNNER.wait(run["id"], timeout=60)

    payload = db.event_payloads("run.harness", [run["id"]])[run["id"]]
    assert payload["brief_version"] == runner.BRIEF_VERSION
    assert payload["route"] == "fake-opencode"
    assert payload["agent"] == "fake-opencode"


def test_opencode_usage_is_summed_without_a_fake_cost(fake_opencode):
    task = db.create_task("do it")
    run = runner.RUNNER.start(task["id"], "fake-opencode", cwd=str(fake_opencode))
    finished = runner.RUNNER.wait(run["id"], timeout=60)

    usage = json.loads(finished["usage_json"])
    assert usage["input_tokens"] == 11
    assert usage["output_tokens"] == 5  # output + reasoning
    # No meter and no price list means no number is better than a zero.
    assert "cost" not in usage
    assert usage["attempts"][0]["cost"] is None


def test_a_run_records_the_session_the_stream_opened(fake_opencode):
    task = db.create_task("do it")
    run = runner.RUNNER.start(task["id"], "fake-opencode", cwd=str(fake_opencode))
    finished = runner.RUNNER.wait(run["id"], timeout=60)

    assert json.loads(finished["session_ref_json"])["session_id"] == "ses_fake0001"
