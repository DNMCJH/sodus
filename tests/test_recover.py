"""Recovery: retry is the default, deterministic errors are the exception.

The rule under test: every failed run gets up to three automatic retries,
including the ones nobody classified; only 401/403/400/model/content-policy
failures are withheld because a rerun would return the same answer and bill
for it. The board only asks a human after the chain is spent.
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
        db.update_run(child["id"], status="queued", started_at=time.time())
        return db.get_run(child["id"])

    monkeypatch.setattr(runner.RUNNER, "start", start)
    return calls


# ---- classification ----------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ORPHAN,
        ORPHAN + "; 3 file(s) written by the worker were kept",
        "ORPHANED: THE PROCESS THAT OWNED THIS RUN IS GONE",
        "orphaned: no activity for 900s",
        "no output for 900s, killed",
        "request timed out",
        "TIMEOUT（超时）",
        "等待超时：上游没有响应",
        "ECONNRESET",
        "ECONNRESET while reading（连接被重置）",
        "fetch failed: ETIMEDOUT",
        "socket hang up",
        "network is unreachable",
        "502 Bad Gateway",
        "HTTP 503: 服务不可用",
        "429 too many requests",
        "rate limit exceeded（限流）",
        # Nobody classified these. They are still retried: "exit code 1" is a
        # run to try again, not a question for a human.
        "exit code 1",
        "agent stopped after 3 tool calls",
        "python: can't open file 'x.py'",
        "PermissionError: [Errno 13] Permission denied: 'out.csv'",
        "写入失败：没有权限",
        "something nobody classified",
        "",
        "    ",
    ],
)
def test_everything_but_a_wrong_request_is_retryable(error):
    assert recover.classify({"error": error}) == "retryable"


@pytest.mark.parametrize(
    "error",
    [
        "401 Unauthorized",
        "403 Forbidden",
        "HTTP 403: 密钥无效",
        "鉴权失败：token 已过期",
        "400 Bad Request: invalid model parameter",
        "invalid request: messages must not be empty",
        "content policy violation",
        "内容被安全策略拦截",
        "model not found: gpt-x",
        "模型已下线",
        "invalid api key",
        "参数错误：temperature 超出范围",
    ],
)
def test_deterministic_errors(error):
    assert recover.classify({"error": error}) == "deterministic"


def test_problem_text_is_a_sentence_not_an_error_dump():
    assert "进程退出了" in recover.problem_text({"error": ORPHAN})
    assert "重试不会好" in recover.problem_text({"error": "401 Unauthorized"})
    # The old wording admitted the board could not tell what broke. It now
    # names what it has (an exit code) instead of claiming ignorance.
    assert "退出码" in recover.problem_text({"error": "exit code 1"})
    for error in (ORPHAN, "429", "content policy", "exit code 1"):
        text = recover.problem_text({"error": error})
        assert any("\u4e00" <= char <= "\u9fff" for char in text)
        assert "Error" not in text and "Traceback" not in text


def test_a_deterministic_failure_says_why_a_retry_would_not_help():
    text = recover.why_not_retried({"error": "401 Unauthorized"})
    assert "401" in text and "再发一次" in text
    assert recover.why_not_retried({"error": "exit code 1"}) == ""


# ---- should_retry ------------------------------------------------------


def test_an_orphan_is_retryable_immediately(home, work):
    run = make_run(work, ORPHAN)
    assert recover.should_retry(run) is True


def test_an_unclassified_failure_is_retried_not_escalated(home, work):
    run = make_run(work, "exit code 1")
    assert recover.classify(run) == "retryable"
    assert recover.should_retry(run) is False  # waiting out the first backoff
    assert recover.should_retry(run, now=time.time() + 61) is True


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


def test_the_retry_budget_is_three_and_per_chain(home, work):
    original = make_run(work, ORPHAN, ended=time.time() - 600)
    first = make_run(
        work,
        ORPHAN,
        task=db.get_task(original["task_id"]),
        parent=original["id"],
        note="auto-retry 1/3\n继续",
        ended=time.time() - 500,
    )
    second = make_run(
        work,
        ORPHAN,
        task=db.get_task(original["task_id"]),
        parent=first["id"],
        note="auto-retry 2/3\n继续",
        ended=time.time() - 400,
    )
    third = make_run(
        work,
        ORPHAN,
        task=db.get_task(original["task_id"]),
        parent=second["id"],
        note="auto-retry 3/3\n继续",
        ended=time.time() - 300,
    )

    assert recover.retry_depth(original) == 0
    assert recover.retry_depth(first) == 1
    assert recover.retry_depth(second) == 2
    assert recover.retry_depth(third) == 3
    # Every failed run in the chain already has a child except the tip; the
    # tip is the only one with attempts left (its depth is 2 before the 3rd).
    assert recover.should_retry(original) is False
    assert recover.should_retry(first) is False
    assert recover.should_retry(second) is False  # it has a child too
    assert recover.should_retry(third) is False  # depth 3 -> a 4th attempt
    assert recover.block_reason(third) == "retry-limit"


def test_the_third_retry_is_still_allowed(home, work):
    original = make_run(work, ORPHAN, ended=time.time() - 600)
    first = make_run(
        work,
        ORPHAN,
        task=db.get_task(original["task_id"]),
        parent=original["id"],
        note="auto-retry 1/3\n继续",
        ended=time.time() - 500,
    )
    second = make_run(
        work,
        ORPHAN,
        task=db.get_task(original["task_id"]),
        parent=first["id"],
        note="auto-retry 2/3\n继续",
        ended=time.time() - 400,
    )
    assert recover.should_retry(second) is True


def test_the_limit_is_read_at_call_time(home, work, monkeypatch):
    # NODUS_RETRY_LIMIT is the operator's dial; a test (or a long-lived
    # process) may change it after import, and the decision must follow.
    run = make_run(work, ORPHAN, ended=time.time() - 600)
    monkeypatch.setattr(recover, "RETRY_LIMIT", 0)
    assert recover.block_reason(run) == "retry-limit"
    monkeypatch.setattr(recover, "RETRY_LIMIT", 1)
    assert recover.block_reason(run) == ""
    assert recover.auto_retry(limit=0)["retried"] == []


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


def test_auto_retry_retries_everything_but_the_wrong_request(home, work, monkeypatch):
    calls = fake_start(monkeypatch)
    orphan = make_run(work, ORPHAN)
    auth = make_run(work, "401 Unauthorized")
    # Past the first backoff window: an unclassified failure is retried too.
    unknown = make_run(work, "exit code 1", ended=time.time() - 600)

    result = recover.auto_retry()

    assert result["considered"] == 3
    retried = {item["run_id"]: item for item in result["retried"]}
    assert set(retried) == {orphan["id"], unknown["id"]}
    assert retried[unknown["id"]]["cause"] == "unknown"
    child = db.get_run(retried[orphan["id"]]["child_run_id"])
    assert child["parent_run_id"] == orphan["id"]
    assert child["task_id"] == orphan["task_id"]
    assert child["cwd"] == orphan["cwd"]
    assert child["note"].splitlines()[0] == "auto-retry 1/3"

    skipped = {item["run_id"]: item for item in result["skipped"]}
    assert skipped[auth["id"]]["class"] == "deterministic"
    assert len(calls) == 2


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


def test_a_chain_stops_after_three_retries(home, work, monkeypatch):
    fake_start(monkeypatch)
    original = make_run(work, ORPHAN, ended=time.time() - 3600)
    ended = time.time() - 3000

    tip = original
    for attempt in range(1, 4):
        result = recover.auto_retry()
        assert len(result["retried"]) == 1, (attempt, result)
        tip = db.get_run(result["retried"][0]["child_run_id"])
        db.update_run(
            tip["id"],
            status="failed",
            error=ORPHAN,
            started_at=ended - 42,
            ended_at=ended,
        )
        ended += 100

    fourth = recover.auto_retry()
    assert fourth["retried"] == []
    assert any(
        item["run_id"] == tip["id"] and item["reason"] == "retry-limit"
        for item in fourth["skipped"]
    )
    assert recover.retry_depth(db.get_run(tip["id"])) == 3
    # The original's three retries were spent by the chain, not by the task.
    assert db.get_run(original["id"])["status"] == "failed"


def test_the_retry_note_carries_the_previous_error_tail(home, work, monkeypatch):
    calls = fake_start(monkeypatch)
    detail = "Traceback (most recent call last):\n" + ("x" * 400) + "\nModuleNotFoundError: jwt"
    make_run(work, detail, ended=time.time() - 600)
    recover.auto_retry()
    note = calls[0]["note"]
    assert note.splitlines()[0] == "auto-retry 1/3"
    assert "ModuleNotFoundError: jwt" in note
    assert "x" * 400 not in note          # 300-char tail, not the whole dump
    assert len(note.splitlines()[1]) <= 320


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
    assert waiting[0]["limit"] == 3
    assert waiting[0]["next_retry_at"] == pytest.approx(now + recover.BACKOFF[0])
    assert waiting[0]["task_title"] == "修复登录"
    assert "网络" in waiting[0]["problem"]

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


# ---- the stage-1 contract: attempts, chain, next_retry_at, dry_run ----


def test_attempts_counts_the_whole_chain(home, work):
    original = make_run(work, ORPHAN)
    assert recover.attempts(original) == 0

    first = make_run(
        work,
        ORPHAN,
        task=db.get_task(original["task_id"]),
        parent=original["id"],
        note="auto-retry 1/3\n继续",
    )
    assert recover.attempts(original) == 1
    assert recover.attempts(first) == 1

    second = make_run(
        work,
        ORPHAN,
        task=db.get_task(original["task_id"]),
        parent=first["id"],
        note="auto-retry 2/3\n继续",
    )
    assert recover.attempts(original) == 2
    assert recover.attempts(first) == 2
    assert recover.attempts(second) == 2


def test_chain_is_root_first_and_stops_at_a_missing_parent(home, work):
    original = make_run(work, ORPHAN)
    first = make_run(
        work,
        ORPHAN,
        task=db.get_task(original["task_id"]),
        parent=original["id"],
        note="auto-retry 1/3\n继续",
    )
    chain = recover.chain(first)
    assert [row["id"] for row in chain] == [original["id"], first["id"]]
    assert recover.is_retry(chain[0]) is False
    assert recover.retry_number(chain[1]) == 1


def test_attempts_do_not_leak_between_chains(home, work):
    task = db.create_task(goal="two chains", acceptance="pytest -q")
    left = make_run(work, ORPHAN, task=task)
    make_run(
        work,
        ORPHAN,
        task=task,
        parent=left["id"],
        note="auto-retry 1/3\n继续",
    )

    right = make_run(work, ORPHAN, task=task)
    right_first = make_run(
        work,
        ORPHAN,
        task=task,
        parent=right["id"],
        note="auto-retry 1/3\n继续",
    )
    make_run(
        work,
        ORPHAN,
        task=task,
        parent=right_first["id"],
        note="auto-retry 2/3\n继续",
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
    parent = original
    for attempt in (1, 2, 3):
        parent = make_run(
            work,
            ORPHAN,
            task=db.get_task(original["task_id"]),
            parent=parent["id"],
            note=f"auto-retry {attempt}/3\n继续",
            ended=now - 10_000 + attempt * 100,
        )

    assert recover.attempts(parent) == 3
    assert recover.should_retry(parent) is False
    assert recover.next_retry_at(parent) is None


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
    assert result["retried"][0]["limit"] == 3
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
        assert child["note"].splitlines()[0] == "auto-retry 1/3"
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
