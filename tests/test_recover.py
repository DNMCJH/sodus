"""Recovery: classify, retry within a budget, and never guess.

The rule under test is narrow on purpose. A retry that might replay a
half-finished edit costs more than a failure a human looks at, so only the
provably-transient cases may start another run.
"""

from __future__ import annotations

import json
import time

import pytest

from nodus import config, db, recover, runner

ORPHAN = "orphaned: the process that owned this run is gone"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-recover-test")
    (tmp_path / "home").mkdir()
    return tmp_path


@pytest.fixture()
def work(home):
    path = home / "work"
    path.mkdir()
    return path


def make_run(
    work,
    error: str,
    *,
    status: str = "failed",
    note: str = "",
    task: dict | None = None,
    parent: str | None = None,
    ended: float | None = None,
    **over,
) -> dict:
    task = task or db.create_task(
        goal=f"goal for {error[:24]}", acceptance="pytest -q", title="修复登录"
    )
    run = db.create_run(
        task["id"],
        "claude",
        str(work),
        parent_run_id=parent,
        note=note,
        route="claude",
        provider="relay",
    )
    db.update_run(
        run["id"],
        status=status,
        error=error,
        ended_at=time.time() if ended is None else ended,
        **over,
    )
    return db.get_run(run["id"])


def fake_start(monkeypatch) -> list[dict]:
    """Record the retry call and create the child row a real run would have."""
    calls: list[dict] = []

    def start(**kwargs):
        calls.append(kwargs)
        child = db.create_run(
            kwargs["task_id"],
            "claude",
            kwargs["cwd"],
            parent_run_id=kwargs["parent_run_id"],
            note=kwargs["note"],
            route=kwargs["route_name"],
            provider=kwargs.get("provider") or "",
        )
        db.update_run(child["id"], status="queued")
        return db.get_run(child["id"])

    monkeypatch.setattr(runner.RUNNER, "start", start)
    return calls


# ---- classification ----------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ORPHAN,
        ORPHAN + "; 3 file(s) written by the worker were kept",
        "no output for 900s, killed",
        "request timed out",
        "ECONNRESET",
        "fetch failed",
        "socket hang up",
        "502 Bad Gateway",
        "429 too many requests",
        "rate limit exceeded",
    ],
)
def test_transient_errors(error):
    assert recover.classify({"error": error}) == "transient"


@pytest.mark.parametrize(
    "error",
    [
        "401 Unauthorized",
        "403 Forbidden",
        "400 Bad Request: invalid model parameter",
        "content policy violation",
        "model not found: gpt-x",
        "invalid api key",
    ],
)
def test_deterministic_errors(error):
    assert recover.classify({"error": error}) == "deterministic"


@pytest.mark.parametrize(
    "error",
    [
        "exit code 1",
        "python: can't open file 'x.py'",
        "something nobody classified",
        "",
        "    ",
    ],
)
def test_unclassified_is_human(error):
    # Guessing here is how an automatic retry applies the same edit twice.
    assert recover.classify({"error": error}) == "human"


def test_problem_text_is_a_sentence_not_an_error_dump():
    assert "进程退出了" in recover.problem_text({"error": ORPHAN})
    assert "重试不会好" in recover.problem_text({"error": "401 Unauthorized"})
    assert "看不出" in recover.problem_text({"error": "exit code 1"})


# ---- should_retry ------------------------------------------------------


def test_an_orphan_is_retryable_immediately(home, work):
    run = make_run(work, ORPHAN)
    assert recover.should_retry(run) is True


def test_a_network_failure_waits_out_the_backoff(home, work):
    now = time.time()
    run = make_run(work, "ECONNRESET", ended=now)
    assert recover.should_retry(run, now=now + 10) is False
    assert recover.should_retry(run, now=now + 61) is True


def test_deterministic_failures_are_never_retried(home, work):
    run = make_run(work, "401 Unauthorized")
    assert recover.should_retry(run) is False


def test_a_run_that_did_not_fail_is_never_retried(home, work):
    done = make_run(work, ORPHAN, status="completed")
    assert recover.should_retry(done) is False


def test_the_retry_budget_is_per_chain(home, work):
    original = make_run(work, ORPHAN, ended=time.time() - 600)
    first = make_run(
        work,
        ORPHAN,
        task=db.get_task(original["task_id"]),
        parent=original["id"],
        note="auto-retry 1/2\n继续",
        ended=time.time() - 500,
    )
    second = make_run(
        work,
        ORPHAN,
        task=db.get_task(original["task_id"]),
        parent=first["id"],
        note="auto-retry 2/2\n继续",
        ended=time.time() - 400,
    )

    assert recover.retry_depth(original) == 0
    assert recover.retry_depth(first) == 1
    assert recover.retry_depth(second) == 2
    # The original already has a child, so it is not retried again; the child
    # is the live end of the chain until it too fails.
    assert recover.should_retry(original) is False
    assert recover.should_retry(first) is False  # it has a child too
    assert recover.should_retry(second) is False  # depth 2 -> attempt 3


def test_the_second_retry_is_still_allowed(home, work):
    original = make_run(work, ORPHAN, ended=time.time() - 600)
    first = make_run(
        work,
        ORPHAN,
        task=db.get_task(original["task_id"]),
        parent=original["id"],
        note="auto-retry 1/2\n继续",
        ended=time.time() - 500,
    )
    assert recover.should_retry(first) is True


def test_a_task_budget_stops_the_retry(home, work):
    task = db.create_task(goal="g", acceptance="a", title="t")
    db.update_task(task["id"], budget_json=json.dumps({"max_runs": 1}))
    run = make_run(work, ORPHAN, task=task)
    assert recover.should_retry(run) is False


def test_a_cost_budget_stops_the_retry(home, work):
    task = db.create_task(goal="g", acceptance="a", title="t")
    db.update_task(task["id"], budget_json=json.dumps({"max_cost": 1.0, "currency": "CNY"}))
    run = make_run(
        work,
        ORPHAN,
        task=task,
        usage_json=json.dumps({"cost": 1.5, "currency": "CNY"}),
    )
    assert recover.should_retry(run) is False


def test_an_unreadable_budget_does_not_block(home, work):
    task = db.create_task(goal="g", acceptance="a", title="t")
    db.update_task(task["id"], budget_json="{not json")
    run = make_run(work, ORPHAN, task=task)
    assert recover.should_retry(run) is True


# ---- auto_retry --------------------------------------------------------


def test_auto_retry_retries_one_and_names_the_other_two(home, work, monkeypatch):
    calls = fake_start(monkeypatch)
    orphan = make_run(work, ORPHAN)
    auth = make_run(work, "401 Unauthorized")
    unknown = make_run(work, "exit code 1")

    result = recover.auto_retry()

    assert result["considered"] == 3
    assert [item["run_id"] for item in result["retried"]] == [orphan["id"]]
    child = db.get_run(result["retried"][0]["child_run_id"])
    assert child["parent_run_id"] == orphan["id"]
    assert child["task_id"] == orphan["task_id"]
    assert child["cwd"] == orphan["cwd"]
    assert child["note"].splitlines()[0] == "auto-retry 1/2"

    skipped = {item["run_id"]: item for item in result["skipped"]}
    assert skipped[auth["id"]]["class"] == "deterministic"
    assert skipped[unknown["id"]]["class"] == "human"
    assert len(calls) == 1


def test_auto_retry_leaves_a_completed_run_alone(home, work, monkeypatch):
    calls = fake_start(monkeypatch)
    make_run(work, ORPHAN, status="completed")
    assert recover.auto_retry()["retried"] == []
    assert calls == []


def test_auto_retry_does_not_retry_twice_without_progress(home, work, monkeypatch):
    calls = fake_start(monkeypatch)
    orphan = make_run(work, ORPHAN)
    first = recover.auto_retry()
    again = recover.auto_retry()
    assert len(first["retried"]) == 1
    assert again["retried"] == []
    assert any(
        item["run_id"] == orphan["id"] and item["reason"] in ("already-retried", "task-running")
        for item in again["skipped"]
    )
    assert len(calls) == 1


def test_a_chain_stops_after_two_retries(home, work, monkeypatch):
    fake_start(monkeypatch)
    original = make_run(work, ORPHAN, ended=time.time() - 600)

    first = recover.auto_retry()
    child = db.get_run(first["retried"][0]["child_run_id"])
    db.update_run(child["id"], status="failed", error=ORPHAN, ended_at=time.time() - 500)

    second = recover.auto_retry()
    grandchild = db.get_run(second["retried"][0]["child_run_id"])
    db.update_run(grandchild["id"], status="failed", error=ORPHAN, ended_at=time.time() - 400)

    third = recover.auto_retry()
    assert third["retried"] == []
    assert any(
        item["run_id"] == grandchild["id"] and item["reason"] == "retry-limit"
        for item in third["skipped"]
    )
    # The original's two retries were spent by the chain, not by the task.
    assert db.get_run(original["id"])["status"] == "failed"


def test_a_retry_resumes_the_session_and_pins_the_endpoint(home, work, monkeypatch):
    calls = fake_start(monkeypatch)
    make_run(
        work,
        ORPHAN,
        session_ref_json=json.dumps({"provider": "claude", "session_id": "ses-1"}),
        model="claude-sonnet",
    )
    recover.auto_retry()
    assert len(calls) == 1
    call = calls[0]
    assert call["session_id"] == "ses-1"
    assert call["session_mode"] == "resume"
    assert call["provider"] == "relay"
    assert call["model"] == "claude-sonnet"


def test_a_network_failure_waits_for_the_next_sweep(home, work, monkeypatch):
    calls = fake_start(monkeypatch)
    failed = make_run(work, "ECONNRESET", ended=time.time())
    held = recover.auto_retry()
    assert held["retried"] == []
    assert any(
        item["run_id"] == failed["id"] and item["reason"] == "backoff"
        for item in held["skipped"]
    )
    later = recover.auto_retry(now=time.time() + 120)
    assert [item["run_id"] for item in later["retried"]] == [failed["id"]]
    assert len(calls) == 1


# ---- the board's view --------------------------------------------------


def test_recovering_shows_a_waiting_retry_and_then_the_live_child(home, work, monkeypatch):
    fake_start(monkeypatch)
    now = time.time()
    failed = make_run(work, "ECONNRESET", ended=now)

    waiting = recover.recovering(now=now)
    assert len(waiting) == 1
    assert waiting[0]["run_id"] == failed["id"]
    assert waiting[0]["attempt"] == 1
    assert waiting[0]["next_retry_at"] == pytest.approx(now + recover.BACKOFF[0])
    assert waiting[0]["task_title"] == "修复登录"

    recover.auto_retry(now=now + 120)
    live = recover.recovering(now=now + 121)
    assert len(live) == 1
    assert live[0]["run_id"] != failed["id"]
    assert live[0]["attempt"] == 1
    assert live[0]["next_retry_at"] is None


def test_recovering_disappears_when_switched_off(home, work, monkeypatch):
    make_run(work, "ECONNRESET", ended=time.time())
    monkeypatch.setenv("NODUS_AUTO_RECOVER", "0")
    assert recover.enabled() is False
    assert recover.recovering() == []


def test_start_runs_one_pass_and_only_once(home, work, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(recover, "_started", False)
    monkeypatch.setattr(
        recover,
        "auto_retry",
        lambda **kwargs: {"retried": [], "skipped": [], "considered": 0},
    )
    monkeypatch.setattr(recover, "SWEEP_SECONDS", 3600)

    assert recover.start(log=seen.append) is True
    assert recover.start(log=seen.append) is False
    assert seen == []


# ---- the stage-1 contract: attempts, next_retry_at, dry_run -----------


def test_attempts_counts_the_whole_chain(home, work):
    original = make_run(work, ORPHAN)
    assert recover.attempts(original) == 0

    first = make_run(
        work,
        ORPHAN,
        task=db.get_task(original["task_id"]),
        parent=original["id"],
        note="auto-retry 1/2\n继续",
    )
    assert recover.attempts(original) == 1
    assert recover.attempts(first) == 1

    second = make_run(
        work,
        ORPHAN,
        task=db.get_task(original["task_id"]),
        parent=first["id"],
        note="auto-retry 2/2\n继续",
    )
    assert recover.attempts(original) == 2
    assert recover.attempts(first) == 2
    assert recover.attempts(second) == 2


def test_attempts_do_not_leak_between_chains(home, work):
    task = db.create_task(goal="two chains", acceptance="pytest -q")
    left = make_run(work, ORPHAN, task=task)
    make_run(
        work,
        ORPHAN,
        task=task,
        parent=left["id"],
        note="auto-retry 1/2\n继续",
    )

    right = make_run(work, ORPHAN, task=task)
    right_first = make_run(
        work,
        ORPHAN,
        task=task,
        parent=right["id"],
        note="auto-retry 1/2\n继续",
    )
    make_run(
        work,
        ORPHAN,
        task=task,
        parent=right_first["id"],
        note="auto-retry 2/2\n继续",
    )

    assert recover.attempts(left) == 1
    assert recover.attempts(right) == 2


def test_next_retry_at_names_the_backoff(home, work):
    now = time.time()
    network = make_run(work, "ECONNRESET", ended=now)
    assert recover.next_retry_at(network) == pytest.approx(now + recover.BACKOFF[0])
    assert recover.should_retry(network) is False

    # An orphan has no process left to race, so it is due immediately.
    orphan = make_run(work, ORPHAN, ended=now)
    assert recover.next_retry_at(orphan) == pytest.approx(now)
    assert recover.should_retry(orphan) is True

    auth = make_run(work, "401 Unauthorized", ended=now - 3600)
    assert recover.next_retry_at(auth) is None


def test_next_retry_at_is_none_when_the_chain_is_spent(home, work):
    now = time.time()
    original = make_run(work, ORPHAN, ended=now - 10_000)
    first = make_run(
        work,
        ORPHAN,
        task=db.get_task(original["task_id"]),
        parent=original["id"],
        note="auto-retry 1/2\n继续",
        ended=now - 9_000,
    )
    second = make_run(
        work,
        ORPHAN,
        task=db.get_task(original["task_id"]),
        parent=first["id"],
        note="auto-retry 2/2\n继续",
        ended=now - 8_000,
    )

    assert recover.attempts(second) == 2
    assert recover.should_retry(second) is False
    assert recover.next_retry_at(second) is None


def test_dry_run_on_the_real_database_writes_nothing(home, work):
    old = time.time() - 3600
    retryable = make_run(work, ORPHAN, ended=old)
    fixed = make_run(work, "401 Unauthorized", ended=old)
    done = make_run(work, ORPHAN, status="completed", ended=old)
    before = [run["id"] for run in db.list_runs(limit=100)]

    result = recover.auto_retry(dry_run=True)

    assert set(result) >= {"retried", "skipped", "errors"}
    assert [item["run_id"] for item in result["retried"]] == [retryable["id"]]
    assert result["retried"][0]["child_run_id"] == ""
    assert result["retried"][0]["attempt"] == 1
    assert result["retried"][0]["limit"] == 2
    reasons = {item["run_id"]: item["reason"] for item in result["skipped"]}
    assert reasons[fixed["id"]] == "deterministic"
    assert done["id"] not in reasons
    assert result["errors"] == []

    assert [run["id"] for run in db.list_runs(limit=100)] == before
    assert db.get_run(retryable["id"])["status"] == "failed"


def test_auto_retry_starts_two_children_for_two_failures(home, work, monkeypatch):
    calls = fake_start(monkeypatch)
    old = time.time() - 3600
    make_run(work, ORPHAN, ended=old)
    make_run(work, "ECONNRESET", ended=old)

    result = recover.auto_retry()

    assert result["errors"] == []
    assert len(result["retried"]) == 2
    for item in result["retried"]:
        child = db.get_run(item["child_run_id"])
        parent = db.get_run(item["run_id"])
        assert child["parent_run_id"] == item["run_id"]
        assert child["task_id"] == parent["task_id"]
        assert child["cwd"] == parent["cwd"]
        assert child["route"] == parent["route"]
        assert child["note"].splitlines()[0] == "auto-retry 1/2"
    assert len(calls) == 2


def test_a_retry_that_cannot_start_is_an_error(home, work, monkeypatch):
    run = make_run(work, ORPHAN, ended=time.time() - 3600)

    def broken(**kwargs):
        raise RuntimeError("unknown route: claude")

    monkeypatch.setattr(runner.RUNNER, "start", broken)
    result = recover.auto_retry()

    assert result["retried"] == []
    assert len(result["errors"]) == 1
    assert result["errors"][0]["run_id"] == run["id"]
    assert "unknown route" in result["errors"][0]["error"]
    assert not any(item["run_id"] == run["id"] for item in result["skipped"])


def test_summary_mentions_a_start_error():
    text = recover.summary(
        {"retried": [], "skipped": [], "errors": [{"run_id": "run-1"}]}
    )
    assert "could not start" in text
