"""The first screen must separate "the machine is on it" from "you must decide".

`/board` keeps `decisions`/`decision_counts` for MCP and the pet; these tests
pin the newer contract: `needs_you` carries full context for a human, while
`auto_recovering` and `deterministic` keep the other two classes off that list.
"""

from __future__ import annotations

import time

import pytest

from nodus import config, db, server, ui

ORPHAN = "orphaned: the process that owned this run is gone"

PYTEST_RED = """..F
=================================== FAILURES ===================================
_______________________ test_token_refresh _______________________

    def test_token_refresh():
        token = login()
>       assert refresh(token) == 200
E       assert 401 == 200

tests/test_auth.py:14: AssertionError
=========================== short test summary info ===========================
FAILED tests/test_auth.py::test_token_refresh - assert 401 == 200
1 failed, 2 passed in 0.31s
"""


def add_event(run: dict, etype: str, payload: dict) -> None:
    db.append_event(run["id"], db.next_seq(run["id"]), etype, payload)


def add_tool_call(run: dict, name: str, **args) -> None:
    add_event(run, "tool_call", {"id": "call-1", "name": name, "input": args})


def add_artifact(run: dict, task: dict, rel: str) -> None:
    from pathlib import Path as _P

    db.add_artifact(
        run["id"], task["id"], "file", str(_P(run["cwd"]) / rel), "h", rel_path=rel
    )


def red_event(run: dict) -> None:
    add_event(
        run,
        "verify.red",
        {
            "phase": "red",
            "ok": False,
            "exit": 1,
            "duration_ms": 310,
            "cmd": ["python", "-m", "pytest", "-q"],
            "tail": PYTEST_RED,
        },
    )


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-board-recovery")
    (tmp_path / "home").mkdir()
    return tmp_path


def seed(
    home,
    *,
    error: str,
    status: str = "failed",
    title: str = "修复登录",
    goal: str = "修好登录页的报错，让 pytest 通过",
    folder: str = "shop",
    note: str = "",
    task: dict | None = None,
    parent: str | None = None,
    ended: float | None = None,
    **over,
) -> tuple[dict, dict]:
    work = home / "work"
    work.mkdir(parents=True, exist_ok=True)
    cwd = work / folder
    cwd.mkdir(parents=True, exist_ok=True)
    task = task or db.create_task(goal=goal, acceptance="pytest -q", title=title)
    run = db.create_run(
        task["id"], "claude", str(cwd), parent_run_id=parent, note=note,
        route="claude", provider="relay",
    )
    db.update_run(
        run["id"],
        status=status,
        error=error,
        ended_at=time.time() if ended is None else ended,
        **over,
    )
    return task, db.get_run(run["id"])


def find(board: dict, field: str, run_id: str) -> dict:
    return next(item for item in board[field] if item.get("run") == run_id)


# ---- needs_you ---------------------------------------------------------


def test_the_unknown_failure_arrives_with_project_task_problem_change(home):
    task, run = seed(
        home,
        error="exit code 1",
        title="修复登录",
        goal="x" * 200,
    )
    db.add_artifact(run["id"], task["id"], "file", str(home / "app.py"), "h", rel_path="app.py")

    board = server.board_state()
    entry = find(board, "needs_you", run["id"])

    assert entry["project"] == "shop"
    assert entry["cwd"].endswith("shop")
    assert entry["task_title"] == "修复登录"
    assert len(entry["goal"]) <= 120          # truncated, not the whole essay
    assert "看不出" in entry["problem"]        # recover.problem_text, not the page
    assert entry["change"] == "改了 1 个文件：app.py"
    assert entry["suggestion"]                  # one actionable sentence
    assert entry["kind"] == "failed" and entry["run"] == run["id"]


def test_a_deterministic_failure_is_not_a_decision(home):
    _, run = seed(home, error="401 Unauthorized")

    board = server.board_state()
    assert all(item.get("run") != run["id"] for item in board["needs_you"])
    entry = find(board, "deterministic", run["id"])
    assert "重试不会好" in entry["problem"]
    assert "密钥" in entry["suggestion"]
    assert entry["project"] == "shop"


def test_a_transient_failure_with_retries_left_is_recovering(home):
    _, run = seed(home, error="ECONNRESET")

    board = server.board_state()
    assert all(item.get("run") != run["id"] for item in board["needs_you"])
    entry = next(item for item in board["auto_recovering"] if item["run_id"] == run["id"])
    assert entry["task_title"] == "修复登录"
    assert entry["attempt"] == 1
    assert entry["next_retry_at"] > time.time()


def test_an_orphan_is_scheduled_immediately(home):
    _, run = seed(home, error=ORPHAN, ended=time.time() - 1)

    board = server.board_state()
    entry = next(item for item in board["auto_recovering"] if item["run_id"] == run["id"])
    assert entry["next_retry_at"] <= time.time()


def test_exhausted_retries_become_a_decision_again(home):
    task, original = seed(home, error=ORPHAN, ended=time.time() - 600)
    _, first = seed(
        home, error=ORPHAN, task=task, parent=original["id"],
        note="auto-retry 1/2\n继续", ended=time.time() - 500,
    )
    _, second = seed(
        home, error=ORPHAN, task=task, parent=first["id"],
        note="auto-retry 2/2\n继续", ended=time.time() - 400,
    )

    entry = find(server.board_state(), "needs_you", second["id"])
    assert "自动重试 2 次都没成功" in entry["problem"]
    assert "日志" in entry["suggestion"]


def test_over_budget_is_a_decision_not_a_retry(home):
    task = db.create_task(goal="g", acceptance="a", title="预算任务")
    db.update_task(task["id"], budget_json='{"max_runs": 1}')
    _, run = seed(home, error=ORPHAN, task=task)

    board = server.board_state()
    entry = find(board, "needs_you", run["id"])
    assert "预算上限" in entry["problem"]
    assert all(item["run_id"] != run["id"] for item in board["auto_recovering"])


def test_a_failure_the_chain_moved_past_is_kept_in_history(home):
    # The old failure is not a decision and not auto-recovering any more, but
    # dropping it would leave decision_counts.failed explaining a row nobody
    # can find.
    task, original = seed(home, error=ORPHAN, ended=time.time() - 600)
    seed(home, error="", status="completed", task=task, parent=original["id"])

    board = server.board_state()
    assert all(item.get("run") != original["id"] for item in board["needs_you"])
    entry = find(board, "history_failures", original["id"])
    assert "已经自动重试过" in entry["problem"]
    assert board["decision_counts"]["history_failed"] == 1


def test_switching_recovery_off_makes_transient_failures_a_decision(home, monkeypatch):
    monkeypatch.setenv("NODUS_AUTO_RECOVER", "0")
    _, run = seed(home, error=ORPHAN)

    board = server.board_state()
    entry = find(board, "needs_you", run["id"])
    assert "自动恢复已关闭" in entry["problem"]
    assert board["auto_recovering"] == []


# ---- proposals and unverified runs still land on the first screen ------


def test_a_proposal_carries_the_repo_and_the_stat(home):
    task, run = seed(home, error="", status="completed", folder="shop")
    proposal = db.create_proposal(
        run_id=run["id"],
        task_id=task["id"],
        repo=str(home / "work" / "shop"),
        branch="nodus/fix-login",
        worktree=str(home / "worktrees" / "fix-login"),
        base="abc123",
        patch_path=str(home / "change.patch"),
        files=[{"path": "app.py", "added": 3, "removed": 1}],
        stat="1 file changed, 3 insertions(+), 1 deletion(-)",
    )

    board = server.board_state()
    entry = next(item for item in board["needs_you"] if item["id"] == proposal["id"])
    assert entry["kind"] == "proposal"
    assert entry["project"] == "shop"
    assert entry["task_title"] == "修复登录"
    assert entry["change"].startswith("1 file changed")
    assert "合并" in entry["suggestion"]


def test_unverified_runs_stay_one_explained_row(home):
    _, run = seed(home, error="", status="completed")

    board = server.board_state()
    entry = next(item for item in board["needs_you"] if item["kind"] == "unverified")
    assert "没有测试证据" in entry["problem"]
    assert entry["project"] == "shop"
    assert board["unverified_runs"][0]["id"] == run["id"]


# ---- additive contract -------------------------------------------------


def test_board_keeps_the_old_keys_and_adds_the_new_ones(home):
    seed(home, error="exit code 1")
    board = server.board_state()
    assert {
        "now", "day", "profile", "running", "decisions", "decision_counts", "today",
        "unverified_runs", "needs_you", "auto_recovering", "deterministic",
        "history_failures",
    } <= set(board)
    assert board["decision_counts"]["needs_you"] == len(board["needs_you"])
    assert board["decision_counts"]["deterministic"] == len(board["deterministic"])


def test_failures_recovery_gave_up_on_are_still_visible(home):
    """A count of five failures must not lead to one visible row.

    recovery stops retrying for reasons of its own (already retried, too old,
    a newer attempt is live). Those runs still happened, so they are listed
    under their own group instead of being dropped from every list.
    """
    task, original = seed(
        home, error="orphaned: the process that owned this run is gone", ended=time.time() - 600
    )
    # The chain already moved past this failure: a child exists, so recovery
    # must not retry the original — but the original must still be findable.
    seed(home, error="", status="completed", task=task, parent=original["id"])

    board = server.board_state()
    assert "history_failures" in board
    assert board["decision_counts"]["history_failed"] >= 1
    ids = {item["run"] for item in board["history_failures"]}
    assert original["id"] in ids
    for item in board["history_failures"]:
        assert item["kind"] == "failed"
        assert item.get("problem")  # a reason the reader can act on


# ---- evidence on the row -----------------------------------------------


def test_a_human_failure_arrives_with_the_last_action_and_the_files(home):
    task, run = seed(home, error="exit code 1", title="修复登录")
    add_tool_call(run, "edit", file_path=str(home / "work" / "shop" / "app.py"))
    add_artifact(run, task, "app.py")

    entry = find(server.board_state(), "needs_you", run["id"])

    assert entry["headline"] == "进程退出码 1"
    assert "edit" in entry["evidence"]["action"] and "app.py" in entry["evidence"]["action"]
    assert entry["change"] == "改了 1 个文件：app.py"
    assert entry["evidence"]["change"]["files"] == ["app.py"]
    assert entry["suggestion"]


def test_a_spent_chain_lists_each_attempt_and_the_failed_test(home):
    now = time.time()
    task, original = seed(home, error=ORPHAN, ended=now - 900, started_at=now - 960)
    _, first = seed(
        home, error=ORPHAN, task=task, parent=original["id"],
        note="auto-retry 1/2\n继续", ended=now - 700, started_at=now - 742,
    )
    _, second = seed(
        home, error="orphaned: killed while running tests", task=task, parent=first["id"],
        note="auto-retry 2/2\n继续", ended=now - 500, started_at=now - 538,
    )
    red_event(first)
    add_tool_call(second, "write", filePath=str(home / "work" / "shop" / "app.py"))
    add_event(
        second,
        "tool_result",
        {"id": "call-1", "ok": False, "text": "ModuleNotFoundError: No module named 'jwt'"},
    )
    add_artifact(second, task, "app.py")

    entry = find(server.board_state(), "needs_you", second["id"])

    assert entry["headline"].startswith("第 2 次重试仍失败：")
    assert "ModuleNotFoundError" in entry["headline"]
    assert "write" in entry["evidence"]["action"]
    assert "ModuleNotFoundError" in entry["evidence"]["error_tail"]
    assert [item["outcome"] for item in entry["evidence"]["attempts"]] == [
        "tests_red", "orphaned",
    ]
    assert [item["seconds"] for item in entry["evidence"]["attempts"]] == [42, 38]


def test_the_page_renders_the_four_evidence_blocks():
    for label in ("哪一步", "什么错", "改了哪", "试过什么"):
        assert label in ui.PAGE
    assert "没有需要你决定的事" in ui.PAGE
    assert "这类证据没有记录" in ui.PAGE
    assert "headline" in ui.PAGE
