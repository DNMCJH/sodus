"""Recovery decides by value: the request, the progress, the signature, the money.

The rule under test is the one the operator asked for: a retry is a purchase,
not a reflex. Deterministic errors are never bought; a cheap first failure is;
a second identical death is not; a retry the remaining budget cannot cover is
a question for the operator, not a decision for the daemon.
"""

from __future__ import annotations

import json
import time

import pytest

from nodus import config, db, recover, runner

ORPHAN = "orphaned: the process that owned this run is gone"
TIMEOUT_CMD = "Command timed out after 60s"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-recover-test")
    monkeypatch.delenv("NODUS_DAILY_BUDGET", raising=False)
    monkeypatch.delenv("NODUS_AUTO_RECOVER", raising=False)
    monkeypatch.setattr(recover, "RETRY_LIMIT", 3)
    monkeypatch.setattr(recover, "COST_CAP", 0.5)
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
    started: float | None = None,
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
        started_at=started if started is not None else time.time() - 60,
        ended_at=time.time() if ended is None else ended,
        **over,
    )
    return db.get_run(run["id"])


def add_tool_call(run: dict, name: str = "edit", **args) -> None:
    db.append_event(
        run["id"],
        db.next_seq(run["id"]),
        "tool_call",
        {"id": "call-1", "name": name, "input": args or {"filePath": "app.py"}},
    )


def add_file(run: dict, rel: str = "app.py") -> None:
    db.add_artifact(run["id"], run["task_id"], "file", str(rel), "h", rel_path=rel)


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


def previous_failure(work, task: dict, error: str, **over) -> dict:
    """A failed attempt that happened before the one under test."""
    ended = over.pop("ended", time.time() - 600)
    return make_run(work, error, task=task, ended=ended, started=ended - 40, **over)


# ---- the five questions -------------------------------------------------


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
    ],
)
def test_a_wrong_request_is_never_retried(error):
    assert recover.classify({"error": error}) == "deterministic"
    assert recover.why_not_retried({"error": error})


@pytest.mark.parametrize(
    "error",
    [
        ORPHAN,
        "ORPHANED: THE PROCESS THAT OWNED THIS RUN IS GONE",
        "request timed out",
        "TIMEOUT（超时）",
        "ECONNRESET",
        "socket hang up",
        "502 Bad Gateway",
        "HTTP 503: 服务不可用",
        "429 too many requests",
        "exit code 1",
        "",
        "    ",
    ],
)
def test_everything_else_is_judged_by_the_other_four_questions(error):
    assert recover.classify({"error": error}) == "transient"


def test_problem_text_is_a_sentence_not_an_error_dump():
    assert "进程退出了" in recover.problem_text({"error": ORPHAN})
    assert "重试不会好" in recover.problem_text({"error": "401 Unauthorized"})
    assert "退出码" in recover.problem_text({"error": "exit code 1"})
    for error in (ORPHAN, "429", "content policy", "exit code 1"):
        text = recover.problem_text({"error": error})
        assert any("\u4e00" <= char <= "\u9fff" for char in text)
        assert "Error" not in text and "Traceback" not in text


# ---- how far it got -----------------------------------------------------


def test_an_attempt_that_never_started_is_cheap(home, work):
    run = make_run(work, ORPHAN, ended=time.time() - 10)
    progress = recover.progress(run)
    assert progress == {"tool_calls": 0, "files": 0, "barely": True, "moved": False}


def test_an_attempt_that_already_edited_is_not_cheap(home, work):
    run = make_run(work, ORPHAN, ended=time.time() - 600)
    add_tool_call(run, "write", filePath="app.py")
    add_tool_call(run, "bash", command="python -m pytest -q")
    add_file(run, "app.py")
    progress = recover.progress(run)
    assert progress["tool_calls"] == 2
    assert progress["files"] == 1
    assert progress["barely"] is False
    assert progress["moved"] is True


def test_the_failure_stage_comes_from_what_the_worker_last_did(home, work):
    assert recover.stage(make_run(work, ORPHAN)) == "start"

    edited = make_run(work, ORPHAN)
    add_tool_call(edited, "edit", file_path="app.py")
    assert recover.stage(edited) == "edit"

    shell = make_run(work, ORPHAN)
    add_tool_call(shell, "bash", command="python -m pytest -q tests/")
    assert recover.stage(shell) == "test"

    red = make_run(work, ORPHAN)
    db.append_event(
        red["id"],
        db.next_seq(red["id"]),
        "verify.red",
        {"ok": False, "exit": 1, "cmd": ["python", "-m", "pytest"], "tail": "1 failed"},
    )
    assert recover.stage(red) == "test"


def test_progress_decides_resume_or_restart(home, work):
    moved = make_run(work, ORPHAN, ended=time.time() - 600)
    add_tool_call(moved, "write", filePath="app.py")
    add_file(moved, "app.py")
    plan = recover.retry_plan(moved)
    assert plan["retry"] is True
    assert plan["mode"] == "fresh"
    assert plan["redo"]["tool_calls"] == 1
    assert plan["redo"]["files"] == 1
    assert "要重做" in plan["redo"]["note"]

    resumable = make_run(
        work,
        ORPHAN,
        ended=time.time() - 600,
        session_ref_json=json.dumps({"provider": "claude", "session_id": "ses-1"}),
    )
    add_tool_call(resumable, "write", filePath="app.py")
    plan = recover.retry_plan(resumable)
    assert plan["mode"] == "resume"
    assert plan["redo"] == {}


# ---- the same death twice ----------------------------------------------


def test_a_signature_is_cause_plus_key_error_plus_stage(home, work):
    first = make_run(work, ORPHAN + " while running pytest", ended=time.time() - 700)
    second = make_run(
        work,
        ORPHAN + " while running pytest",
        task=db.get_task(first["task_id"]),
        parent=first["id"],
        note="auto-retry 1/3\n继续",
        ended=time.time() - 300,
        started=time.time() - 340,
    )
    assert recover.signature(first) == recover.signature(second)
    assert recover.repeated(second)["id"] == first["id"]

    other = make_run(
        work,
        "ModuleNotFoundError: No module named 'jwt'",
        task=db.get_task(first["task_id"]),
        parent=second["id"],
        note="auto-retry 2/3\n继续",
        ended=time.time() - 100,
        started=time.time() - 140,
    )
    assert recover.signature(other)["error"] != recover.signature(first)["error"]


def test_a_manual_rerun_is_compared_with_the_previous_failure(home, work):
    task = db.create_task(goal="g", acceptance="a", title="t")
    first = previous_failure(work, task, ORPHAN)
    second = make_run(
        work,
        ORPHAN,
        task=task,
        ended=time.time() - 30,
        started=time.time() - 20,
    )
    assert recover.previous_attempt(second)["id"] == first["id"]
    assert recover.repeated(second)


def test_the_same_error_twice_is_not_retried_again(home, work):
    """Acceptance case 2: same signature, twice, must land on the board."""
    first = make_run(work, ORPHAN, ended=time.time() - 700, started=time.time() - 750)
    second = make_run(
        work,
        ORPHAN,
        task=db.get_task(first["task_id"]),
        parent=first["id"],
        note="auto-retry 1/3\n继续",
        ended=time.time() - 300,
        started=time.time() - 340,
    )

    plan = recover.retry_plan(second)
    assert plan["retry"] is False
    assert plan["gate"] == "same-signature"
    assert "连续两次同样的错" in plan["why"]
    assert recover.should_retry(second) is False
    assert recover.next_retry_at(second) is None


def test_a_different_error_is_progress_and_may_be_retried(home, work):
    first = make_run(work, ORPHAN, ended=time.time() - 700, started=time.time() - 750)
    second = make_run(
        work,
        "ModuleNotFoundError: No module named 'jwt'",
        task=db.get_task(first["task_id"]),
        parent=first["id"],
        note="auto-retry 1/3\n继续",
        ended=time.time() - 300,
        started=time.time() - 340,
    )
    assert recover.retry_plan(second)["retry"] is True


# ---- the money ----------------------------------------------------------


def test_barely_started_and_first_failure_is_retried(home, work):
    """Acceptance case 1: the first failure, almost untouched, is cheap."""
    run = make_run(work, ORPHAN, ended=time.time() - 1, started=time.time() - 5)
    plan = recover.retry_plan(run)
    assert plan["retry"] is True
    assert plan["gate"] == ""
    assert plan["progress"]["barely"] is True
    assert recover.should_retry(run) is True


def test_the_daily_budget_can_refuse_the_next_retry(home, work, monkeypatch):
    """Acceptance case 3: headroom below the historical cost is a question."""
    task = db.create_task(goal="g", acceptance="a", title="导出")
    previous_failure(
        work,
        task,
        ORPHAN,
        status="completed",
        usage_json=json.dumps({"cost": 0.5, "currency": "CNY"}),
    )
    run = make_run(work, ORPHAN, task=task, ended=time.time() - 1)
    db.update_run(run["id"], usage_json=json.dumps({"cost": 0.4, "currency": "CNY"}))
    run = db.get_run(run["id"])
    monkeypatch.setenv("NODUS_DAILY_BUDGET", "1.0")  # 0.9 spent -> 0.1 left

    plan = recover.retry_plan(run)
    assert plan["estimate"] == pytest.approx(0.5)
    assert plan["remaining"] == pytest.approx(0.1, abs=0.001)
    assert plan["retry"] is False
    assert plan["gate"] == "budget"
    assert "剩余额度不够" in plan["why"]
    assert recover.should_retry(run) is False


def test_a_task_budget_can_refuse_the_next_retry(home, work):
    task = db.create_task(goal="g", acceptance="a", title="导出")
    db.update_task(task["id"], budget_json=json.dumps({"max_cost": 0.6, "currency": "CNY"}))
    previous_failure(
        work,
        task,
        ORPHAN,
        status="completed",
        usage_json=json.dumps({"cost": 0.2, "currency": "CNY"}),
    )
    run = make_run(work, ORPHAN, task=task, ended=time.time() - 1)
    db.update_run(run["id"], usage_json=json.dumps({"cost": 0.3, "currency": "CNY"}))
    run = db.get_run(run["id"])

    plan = recover.retry_plan(run)
    assert plan["remaining"] == pytest.approx(0.1, abs=0.001)
    assert plan["gate"] == "budget"
    assert "¥0.10" in plan["why"]


def test_a_retry_above_the_cost_cap_waits_for_a_human(home, work):
    task = db.create_task(goal="g", acceptance="a", title="贵任务")
    previous_failure(
        work,
        task,
        ORPHAN,
        status="completed",
        usage_json=json.dumps({"cost": 1.2, "currency": "CNY"}),
    )
    run = make_run(work, ORPHAN, task=task, ended=time.time() - 1)

    plan = recover.retry_plan(run)
    assert plan["estimate"] == pytest.approx(1.2)
    assert plan["gate"] == "cost-cap"
    assert "¥0.50" in plan["why"]
    assert recover.should_retry(run) is False


def test_dollars_are_not_converted_into_a_yuan_estimate(home, work):
    task = db.create_task(goal="g", acceptance="a", title="美元任务")
    previous_failure(
        work,
        task,
        ORPHAN,
        status="completed",
        usage_json=json.dumps({"cost": 9.0, "currency": "USD"}),
    )
    run = make_run(work, ORPHAN, task=task, ended=time.time() - 1)
    assert recover.estimate_cost(run) is None
    assert recover.retry_plan(run)["gate"] == ""


# ---- the ceiling and the rest of the bookkeeping -----------------------


def test_the_ceiling_is_three_retries_per_chain(home, work):
    original = make_run(work, ORPHAN, ended=time.time() - 2000)
    parent = original
    rows = [original]
    for attempt in (1, 2, 3):
        parent = make_run(
            work,
            ORPHAN,
            task=db.get_task(original["task_id"]),
            parent=parent["id"],
            note=f"auto-retry {attempt}/3\n继续",
            ended=time.time() - 2000 + attempt * 100,
        )
        rows.append(parent)

    assert recover.retry_depth(rows[-1]) == 3
    plan = recover.retry_plan(rows[-1])
    assert plan["gate"] == "retry-limit"
    assert "上限（3 次）" in plan["why"]
    assert recover.should_retry(rows[-1]) is False


def test_a_first_retry_is_still_allowed_at_depth_two(home, work):
    original = make_run(work, ORPHAN, ended=time.time() - 2000)
    first = make_run(
        work, ORPHAN, task=db.get_task(original["task_id"]), parent=original["id"],
        note="auto-retry 1/3\n继续", ended=time.time() - 1900,
    )
    second = make_run(
        work, "timed out", task=db.get_task(original["task_id"]), parent=first["id"],
        note="auto-retry 2/3\n继续", ended=time.time() - 1800,
    )
    assert recover.retry_plan(second)["retry"] is True


def test_deterministic_failures_are_never_retried(home, work):
    run = make_run(work, "401 Unauthorized", ended=time.time() - 3600)
    plan = recover.retry_plan(run)
    assert plan["gate"] == "deterministic"
    assert "401" in plan["why"]
    assert recover.should_retry(run) is False


def test_a_run_that_did_not_fail_is_never_retried(home, work):
    done = make_run(work, ORPHAN, status="completed")
    assert recover.should_retry(done) is False


def test_a_network_failure_waits_out_the_backoff(home, work):
    now = time.time()
    run = make_run(work, "ECONNRESET", ended=now, started=now - 10)
    plan = recover.retry_plan(run, now=now + 10)
    assert plan["gate"] == "backoff"
    assert plan["due_at"] == pytest.approx(now + recover.BACKOFF[0])
    assert recover.should_retry(run, now=now + 61) is True


def test_an_orphan_is_due_immediately(home, work):
    now = time.time()
    run = make_run(work, ORPHAN, ended=now, started=now - 10)
    assert recover.next_retry_at(run, now=now) == pytest.approx(now)


def test_the_task_budget_max_runs_still_stops_the_retry(home, work):
    task = db.create_task(goal="g", acceptance="a", title="t")
    db.update_task(task["id"], budget_json=json.dumps({"max_runs": 1}))
    run = make_run(work, ORPHAN, task=task, ended=time.time() - 1)
    assert recover.retry_plan(run)["gate"] == "over-budget"


def test_an_unreadable_budget_does_not_block(home, work):
    task = db.create_task(goal="g", acceptance="a", title="t")
    db.update_task(task["id"], budget_json="{not json")
    run = make_run(work, ORPHAN, task=task, ended=time.time() - 1)
    assert recover.retry_plan(run)["gate"] == ""


def test_the_limit_is_read_at_call_time(home, work, monkeypatch):
    run = make_run(work, ORPHAN, ended=time.time() - 600)
    monkeypatch.setattr(recover, "RETRY_LIMIT", 0)
    assert recover.block_reason(run) == "retry-limit"
    monkeypatch.setattr(recover, "RETRY_LIMIT", 1)
    assert recover.block_reason(run) == ""


# ---- auto_retry --------------------------------------------------------


def test_auto_retry_starts_only_the_run_that_is_worth_it(home, work, monkeypatch):
    calls = fake_start(monkeypatch)
    cheap = make_run(work, ORPHAN, ended=time.time() - 1, started=time.time() - 5)
    first = make_run(work, ORPHAN, ended=time.time() - 700, started=time.time() - 750)
    repeated = make_run(
        work, ORPHAN, task=db.get_task(first["task_id"]), parent=first["id"],
        note="auto-retry 1/3\n继续", ended=time.time() - 300, started=time.time() - 340,
    )
    auth = make_run(work, "401 Unauthorized", ended=time.time() - 1)

    result = recover.auto_retry()

    assert result["errors"] == []
    assert [item["run_id"] for item in result["retried"]] == [cheap["id"]]
    assert result["retried"][0]["mode"] == "fresh"
    skipped = {item["run_id"]: item for item in result["skipped"]}
    assert skipped[repeated["id"]]["reason"] == "same-signature"
    assert "连续两次同样的错" in skipped[repeated["id"]]["why"]
    assert skipped[auth["id"]]["reason"] == "deterministic"
    assert [call["session_mode"] for call in calls] == ["fresh"]
    assert len(calls) == 1


def test_auto_retry_creates_a_child_that_points_back(home, work, monkeypatch):
    calls = fake_start(monkeypatch)
    parent = make_run(work, ORPHAN, ended=time.time() - 1, started=time.time() - 5)
    result = recover.auto_retry()

    child = db.get_run(result["retried"][0]["child_run_id"])
    assert child["parent_run_id"] == parent["id"]
    assert child["task_id"] == parent["task_id"]
    assert child["cwd"] == parent["cwd"]
    assert child["note"].splitlines()[0] == "auto-retry 1/3"
    assert len(calls) == 1


def test_a_resumed_retry_keeps_the_session_and_the_endpoint(home, work, monkeypatch):
    calls = fake_start(monkeypatch)
    make_run(
        work,
        ORPHAN,
        ended=time.time() - 1,
        started=time.time() - 5,
        session_ref_json=json.dumps({"provider": "claude", "session_id": "ses-1"}),
        model="claude-sonnet",
    )
    recover.auto_retry()
    call = calls[0]
    assert call["session_id"] == "ses-1"
    assert call["session_mode"] == "resume"
    assert call["provider"] == "relay"
    assert call["model"] == "claude-sonnet"


def test_a_from_scratch_retry_records_what_is_redone(home, work, monkeypatch):
    calls = fake_start(monkeypatch)
    run = make_run(work, ORPHAN, ended=time.time() - 1, started=time.time() - 50)
    add_tool_call(run, "write", filePath="app.py")
    add_file(run, "app.py")
    recover.auto_retry()
    note = calls[0]["note"]
    assert calls[0]["session_id"] == ""
    assert calls[0]["session_mode"] == "fresh"
    assert "没有可续的会话" in note
    assert "改了 1 个文件" in note
    assert "要重做" in note


def test_the_retry_note_carries_the_previous_error_tail(home, work, monkeypatch):
    calls = fake_start(monkeypatch)
    detail = "Traceback (most recent call last):\n" + ("x" * 400) + "\nModuleNotFoundError: jwt"
    make_run(work, detail, ended=time.time() - 600, started=time.time() - 640)
    recover.auto_retry()
    note = calls[0]["note"]
    assert note.splitlines()[0] == "auto-retry 1/3"
    assert "ModuleNotFoundError: jwt" in note
    assert "x" * 400 not in note
    assert len(note.splitlines()[1]) <= 320


def test_auto_retry_does_not_retry_twice_without_progress(home, work, monkeypatch):
    calls = fake_start(monkeypatch)
    parent = make_run(work, ORPHAN, ended=time.time() - 1, started=time.time() - 5)
    first = recover.auto_retry()
    again = recover.auto_retry()
    assert len(first["retried"]) == 1
    assert again["retried"] == []
    assert any(
        item["run_id"] == parent["id"] and item["reason"] in ("already-retried", "task-running")
        for item in again["skipped"]
    )
    assert len(calls) == 1


def test_dry_run_on_the_real_database_writes_nothing(home, work):
    old = time.time() - 3600
    retryable = make_run(work, ORPHAN, ended=old, started=old - 40)
    fixed = make_run(work, "401 Unauthorized", ended=old, started=old - 40)
    done = make_run(work, ORPHAN, status="completed", ended=old)
    before = [run["id"] for run in db.list_runs(limit=100)]

    result = recover.auto_retry(dry_run=True)

    assert [item["run_id"] for item in result["retried"]] == [retryable["id"]]
    assert result["retried"][0]["child_run_id"] == ""
    assert result["retried"][0]["attempt"] == 1
    assert result["retried"][0]["limit"] == 3
    reasons = {item["run_id"]: item["reason"] for item in result["skipped"]}
    assert reasons[fixed["id"]] == "deterministic"
    assert done["id"] not in reasons
    assert result["errors"] == []
    assert [run["id"] for run in db.list_runs(limit=100)] == before


def test_a_retry_that_cannot_start_is_an_error(home, work, monkeypatch):
    run = make_run(work, ORPHAN, ended=time.time() - 1, started=time.time() - 5)

    def broken(**kwargs):
        raise RuntimeError("unknown route: claude")

    monkeypatch.setattr(runner.RUNNER, "start", broken)
    result = recover.auto_retry()

    assert result["retried"] == []
    assert len(result["errors"]) == 1
    assert result["errors"][0]["run_id"] == run["id"]
    assert "unknown route" in result["errors"][0]["error"]


# ---- the board's view --------------------------------------------------


def test_recovering_shows_a_waiting_retry_and_then_the_live_child(home, work, monkeypatch):
    fake_start(monkeypatch)
    now = time.time()
    failed = make_run(work, "ECONNRESET", ended=now, started=now - 10)

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


def test_recovering_does_not_list_a_refused_failure(home, work):
    first = make_run(work, ORPHAN, ended=time.time() - 700, started=time.time() - 750)
    second = make_run(
        work, ORPHAN, task=db.get_task(first["task_id"]), parent=first["id"],
        note="auto-retry 1/3\n继续", ended=time.time() - 300, started=time.time() - 340,
    )
    assert all(row["run_id"] != second["id"] for row in recover.recovering())


def test_recovering_disappears_when_switched_off(home, work, monkeypatch):
    make_run(work, "ECONNRESET", ended=time.time())
    monkeypatch.setenv("NODUS_AUTO_RECOVER", "0")
    assert recover.enabled() is False
    assert recover.recovering() == []


def test_attempts_counts_the_whole_chain(home, work):
    original = make_run(work, ORPHAN)
    assert recover.attempts(original) == 0
    first = make_run(
        work, ORPHAN, task=db.get_task(original["task_id"]), parent=original["id"],
        note="auto-retry 1/3\n继续",
    )
    assert recover.attempts(first) == 1


def test_chain_is_root_first_and_stops_at_a_missing_parent(home, work):
    original = make_run(work, ORPHAN)
    first = make_run(
        work, ORPHAN, task=db.get_task(original["task_id"]), parent=original["id"],
        note="auto-retry 1/3\n继续",
    )
    chain = recover.chain(first)
    assert [row["id"] for row in chain] == [original["id"], first["id"]]
    assert recover.is_retry(chain[0]) is False
    assert recover.retry_number(chain[1]) == 1


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


def test_summary_mentions_a_start_error():
    text = recover.summary(
        {"retried": [], "skipped": [], "errors": [{"run_id": "run-1"}]}
    )
    assert "could not start" in text
