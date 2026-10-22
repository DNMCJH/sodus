"""Recovery stage 1: classify failures, and retry only what a retry can fix.

The rule under test is narrow on purpose. A retry that might replay a
half-finished edit costs more than a failure a human looks at, so only the
provably-transient cases become child runs, and only while the chain has
retries left.
"""

from __future__ import annotations

import json
import time

import pytest

from nodus import config, db, recover

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
    error,
    *,
    task: dict | None = None,
    parent: str | None = None,
    note: str = "",
    status: str = "failed",
    ended: float | None = None,
    **over,
) -> dict:
    task = task or db.create_task(goal=f"goal: {str(error)[:24]}", acceptance="pytest -q")
    run = db.create_run(
        task["id"],
        "dsh",
        str(work),
        parent_run_id=parent,
        note=note,
        route="dsh",
    )
    db.update_run(
        run["id"],
        status=status,
        error=error,
        ended_at=time.time() if ended is None else ended,
        **over,
    )
    return db.get_run(run["id"])


def retry_child(work, parent, attempt, *, status="failed", error=ORPHAN, ended=None) -> dict:
    return make_run(
        work,
        error,
        task=db.get_task(parent["task_id"]),
        parent=parent["id"],
        note=f"auto-retry {attempt}/2",
        status=status,
        ended=ended,
    )


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
        "ECONNRESET while reading（连接被重置）",
        "fetch failed: ETIMEDOUT",
        "socket hang up",
        "network is unreachable",
        "ssl handshake failed",
        "HTTP 503 Service Unavailable",
        "502 Bad Gateway",
        "HTTP 503: 服务不可用",
        "429 Too Many Requests",
        "Rate limit exceeded（限流）",
        "too many requests, slow down",
    ],
)
def test_transient_errors(error):
    assert recover.classify({"error": error}) == "transient"


@pytest.mark.parametrize(
    "error",
    [
        "401 Unauthorized",
        "403 Forbidden",
        "HTTP 403: 密钥无效",
        "invalid api key",
        "missing API key for provider relay",
        "400 Bad Request",
        "404 page not found",
        "invalid request: messages must not be empty",
        "content policy violation",
        "内容被安全策略拦截",
        "model not found: gpt-x",
        "模型已下线",
        "参数错误：temperature 超出范围",
        "insufficient balance",
    ],
)
def test_deterministic_errors(error):
    assert recover.classify({"error": error}) == "deterministic"


@pytest.mark.parametrize(
    "error",
    [
        "exit code 1",
        "agent stopped after 3 tool calls",
        "something nobody classified",
        "python: can't open file 'x.py'",
        "",
        "   ",
        None,
    ],
)
def test_unclassified_is_human(error):
    # Guessing here is how an automatic retry applies the same edit twice.
    assert recover.classify({"error": error}) == "human"


def test_problem_text_is_a_sentence_not_an_error_dump():
    assert recover.problem_text({"error": ORPHAN}) == (
        "跑到一半，启动它的进程退出了（不是模型出错）"
    )
    assert recover.problem_text({"error": "request timed out"}) == "运行超时，没有收到结果"
    assert recover.problem_text({"error": "401 Unauthorized"}) == (
        "这家供应商拒绝了请求（密钥或权限问题），重试没有意义"
    )
    assert "需要人看一眼" in recover.problem_text({"error": "exit code 1"})

    network = recover.problem_text({"error": "ECONNRESET"})
    assert "网络" in network
    assert "ECONNRESET" not in network
    for run in (
        {"error": ORPHAN},
        {"error": "429"},
        {"error": "content policy"},
        {"error": "exit code 1"},
    ):
        text = recover.problem_text(run)
        assert any("\u4e00" <= char <= "\u9fff" for char in text)


# ---- attempts / next_retry_at / should_retry ---------------------------


def test_attempts_counts_the_chain(home, work):
    long_ago = time.time() - 10_000
    original = make_run(work, ORPHAN, ended=long_ago)
    assert recover.attempts(original) == 0

    first = retry_child(work, original, 1, ended=long_ago + 100)
    assert recover.attempts(original) == 1
    assert recover.attempts(first) == 1

    second = retry_child(work, first, 2, ended=long_ago + 200)
    assert recover.attempts(original) == 2
    assert recover.attempts(first) == 2
    assert recover.attempts(second) == 2


def test_attempts_ignore_a_different_chain_of_the_same_task(home, work):
    task = db.create_task(goal="two chains, one task", acceptance="pytest -q")
    long_ago = time.time() - 10_000
    left = make_run(work, ORPHAN, task=task, ended=long_ago)
    retry_child(work, left, 1, ended=long_ago + 100)

    right = make_run(work, ORPHAN, task=task, ended=long_ago)
    right1 = retry_child(work, right, 1, ended=long_ago + 100)
    retry_child(work, right1, 2, ended=long_ago + 200)

    assert recover.attempts(left) == 1
    assert recover.attempts(right) == 2
    assert recover.attempts(right1) == 2


def test_a_manual_child_is_not_an_automatic_retry(home, work):
    original = make_run(work, ORPHAN, ended=time.time() - 600)
    make_run(
        work,
        ORPHAN,
        task=db.get_task(original["task_id"]),
        parent=original["id"],
        note="manual rerun",
    )
    assert recover.attempts(original) == 0
    assert recover.attempts(db.get_run(original["id"])) == 0


def test_backoff_window_starts_at_one_minute_then_five(home, work):
    now = time.time()
    fresh = make_run(work, "ECONNRESET", ended=now)
    assert recover.should_retry(fresh) is False
    assert recover.next_retry_at(fresh) == pytest.approx(now + 60)

    old = make_run(work, "ECONNRESET", ended=now - 61)
    assert recover.should_retry(old) is True

    original = make_run(work, ORPHAN, ended=now - 10_000)
    first = retry_child(work, original, 1, ended=now - 10)
    assert recover.attempts(first) == 1
    assert recover.should_retry(first) is False
    assert recover.next_retry_at(first) == pytest.approx(now - 10 + 300)


def test_the_retry_limit_is_two(home, work):
    now = time.time()
    original = make_run(work, ORPHAN, ended=now - 10_000)
    first = retry_child(work, original, 1, ended=now - 9_000)
    second = retry_child(work, first, 2, ended=now - 8_000)

    assert recover.attempts(second) == 2
    assert recover.should_retry(second) is False
    assert recover.should_retry(first) is False  # it already has a child
    assert recover.should_retry(original) is False  # same chain
    assert recover.next_retry_at(second) is None


def test_deterministic_and_human_failures_are_not_retried(home, work):
    now = time.time()
    auth = make_run(work, "401 Unauthorized", ended=now - 3600)
    unknown = make_run(work, "exit code 1", ended=now - 3600)

    assert recover.should_retry(auth) is False
    assert recover.next_retry_at(auth) is None
    assert recover.should_retry(unknown) is False
    assert recover.next_retry_at(unknown) is None


def test_a_run_that_did_not_fail_is_not_retried(home, work):
    done = make_run(work, ORPHAN, status="completed", ended=time.time() - 3600)
    assert recover.should_retry(done) is False
    assert recover.next_retry_at(done) is None


def test_over_budget_is_not_retried(home, work):
    now = time.time()
    expensive = db.create_task(goal="costly", acceptance="pytest -q")
    db.update_task(expensive["id"], budget_json=json.dumps(
        {"max_cost": 1.0, "currency": "CNY"}
    ))
    spent = make_run(
        work,
        ORPHAN,
        task=expensive,
        ended=now - 3600,
        usage_json=json.dumps({"cost": 1.5, "currency": "CNY"}),
    )
    counted = db.create_task(goal="bounded", acceptance="pytest -q")
    db.update_task(counted["id"], budget_json=json.dumps({"max_runs": 1}))
    one_run = make_run(work, ORPHAN, task=counted, ended=now - 3600)

    assert recover.should_retry(spent) is False
    assert recover.should_retry(one_run) is False


# ---- auto_retry --------------------------------------------------------


def test_auto_retry_result_shape(home, work):
    make_run(work, ORPHAN, ended=time.time() - 3600)
    result = recover.auto_retry(dry_run=True)
    assert set(result) == {"retried", "skipped", "errors"}


def test_dry_run_on_a_real_database_writes_nothing(home, work):
    old = time.time() - 3600
    retryable = make_run(
        work,
        ORPHAN,
        ended=old,
        session_ref_json=json.dumps({"provider": "dsh", "session_id": "ses-1"}),
    )
    fixed = make_run(work, "401 Unauthorized", ended=old)
    done = make_run(work, ORPHAN, status="completed", ended=old)
    before = [run["id"] for run in db.list_runs(limit=100)]
    before_error = db.get_run(retryable["id"])["error"]

    result = recover.auto_retry(dry_run=True)

    assert set(result) == {"retried", "skipped", "errors"}
    assert [item["run_id"] for item in result["retried"]] == [retryable["id"]]
    assert result["retried"][0]["child_run_id"] == ""
    assert result["retried"][0]["attempt"] == 1
    assert result["retried"][0]["limit"] == 2
    reasons = {item["run_id"]: item["reason"] for item in result["skipped"]}
    assert reasons[fixed["id"]] == "deterministic"
    assert done["id"] not in reasons
    assert result["errors"] == []

    after = [run["id"] for run in db.list_runs(limit=100)]
    assert after == before
    stored = db.get_run(retryable["id"])
    assert stored["status"] == "failed"
    assert stored["error"] == before_error
    assert not any(
        str(run.get("note") or "").startswith("auto-retry")
        for run in db.list_runs(limit=100)
    )


def test_auto_retry_creates_two_children_with_the_right_parent(home, work):
    old = time.time() - 3600
    one = make_run(work, ORPHAN, ended=old)
    two = make_run(work, "ECONNRESET", ended=old)

    result = recover.auto_retry()

    assert result["errors"] == []
    assert len(result["retried"]) == 2
    assert {item["run_id"] for item in result["retried"]} == {one["id"], two["id"]}
    for item in result["retried"]:
        child = db.get_run(item["child_run_id"])
        parent = db.get_run(item["run_id"])
        assert child["parent_run_id"] == item["run_id"]
        assert child["task_id"] == parent["task_id"]
        assert child["cwd"] == parent["cwd"]
        assert child["route"] == parent["route"]
        assert child["agent"] == parent["agent"]
        assert child["note"] == "auto-retry 1/2"
        assert child["status"] == "queued"
        assert item["attempt"] == 1
        assert item["limit"] == 2

    assert recover.attempts(one) == 1
    assert recover.attempts(two) == 1


def test_auto_retry_carries_the_vendor_session(home, work):
    run = make_run(
        work,
        ORPHAN,
        ended=time.time() - 3600,
        session_ref_json=json.dumps({"provider": "dsh", "session_id": "ses-42"}),
    )
    result = recover.auto_retry()
    child = db.get_run(result["retried"][0]["child_run_id"])
    assert json.loads(child["session_ref_json"])["session_id"] == "ses-42"


def test_auto_retry_waits_out_the_backoff(home, work):
    fresh = make_run(work, "ECONNRESET", ended=time.time())
    result = recover.auto_retry(dry_run=True)
    assert result["retried"] == []
    reasons = {item["run_id"]: item["reason"] for item in result["skipped"]}
    assert reasons[fresh["id"]] == "backoff"


def test_auto_retry_stops_at_the_limit(home, work):
    now = time.time()
    original = make_run(work, ORPHAN, ended=now - 10_000)
    first = retry_child(work, original, 1, ended=now - 9_000)
    second = retry_child(work, first, 2, ended=now - 8_000)

    result = recover.auto_retry(dry_run=True)

    assert result["retried"] == []
    reasons = {item["run_id"]: item["reason"] for item in result["skipped"]}
    assert reasons[second["id"]] == "retry-limit"
    assert recover.attempts(second) == 2


def test_a_live_retry_child_blocks_another(home, work):
    original = make_run(work, ORPHAN, ended=time.time() - 10_000)
    queued = retry_child(work, original, 1, status="queued")

    assert recover.should_retry(original) is False
    assert recover.should_retry(queued) is False

    result = recover.auto_retry(dry_run=True)
    assert result["retried"] == []
    reasons = {item["run_id"]: item["reason"] for item in result["skipped"]}
    assert reasons[original["id"]] == "already-retried"


def test_auto_retry_skips_deterministic_human_and_budget(home, work):
    now = time.time()
    auth = make_run(work, "403 Forbidden", ended=now - 3600)
    unknown = make_run(work, "exit code 9", ended=now - 3600)
    bounded = db.create_task(goal="bounded", acceptance="a")
    db.update_task(bounded["id"], budget_json=json.dumps({"max_cost": 0.5}))
    over = make_run(
        work,
        ORPHAN,
        task=bounded,
        ended=now - 3600,
        usage_json=json.dumps({"cost": 0.75, "currency": "CNY"}),
    )

    result = recover.auto_retry(dry_run=True)

    assert result["retried"] == []
    reasons = {item["run_id"]: item["reason"] for item in result["skipped"]}
    assert reasons[auth["id"]] == "deterministic"
    assert reasons[unknown["id"]] == "human"
    assert reasons[over["id"]] == "over-budget"


def test_auto_retry_skips_a_run_without_a_directory(home, work):
    run = make_run(work, ORPHAN, ended=time.time() - 3600)
    db.update_run(run["id"], cwd="")
    result = recover.auto_retry(dry_run=True)
    assert result["retried"] == []
    reasons = {item["run_id"]: item["reason"] for item in result["skipped"]}
    assert reasons[run["id"]] == "missing-context"
