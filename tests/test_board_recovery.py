"""The first screen: a failure is never a question.

`/board` keeps `decisions`/`decision_counts` for MCP and the pet; these tests
pin the contract that replaced the old decision blob:

- `needs_you` holds only the things a person owns: a proposal to merge and a
  spend decision past the budget (an ambiguous requirement would land here
  too). A failure in there is the bug this board exists to fix.
- `handling` is the machine at work: attempt number, history, likely cause and
  plan, with no button but 看日志.
- `stopped` is the report for a spent chain, a repeated error, a deterministic
  error or a red test nobody owns — what I tried, why I stopped, where the
  evidence is. A `重试` button may exist as convenience; the copy may not ask.
- `unverifiable` is information: a finished run with no test evidence says why
  (no test entry) and what would make the next one verifiable (an acceptance
  command).
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from nodus import config, db, diagnosis, lineage, server, statuslight, ui

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


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-board-recovery")
    monkeypatch.delenv("NODUS_DAILY_BUDGET", raising=False)
    monkeypatch.setenv("NODUS_AUTO_RECOVER", "1")
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
    started: float | None = None,
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
    end = time.time() if ended is None else ended
    db.update_run(
        run["id"],
        status=status,
        error=error,
        started_at=started if started is not None else end - 40,
        ended_at=end,
        **over,
    )
    return task, db.get_run(run["id"])


def add_event(run: dict, etype: str, payload: dict) -> None:
    db.append_event(run["id"], db.next_seq(run["id"]), etype, payload)


def add_tool_call(run: dict, name: str, **args) -> None:
    add_event(run, "tool_call", {"id": "call-1", "name": name, "input": args})


def add_artifact(run: dict, rel: str) -> None:
    from pathlib import Path as _P

    db.add_artifact(run["id"], run["task_id"], "file", str(_P(run["cwd"]) / rel), "h", rel_path=rel)


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


def find(board: dict, field: str, run_id: str) -> dict:
    return next(item for item in board[field] if item.get("run") == run_id)


def same_signature_pair(home) -> tuple[dict, dict, dict]:
    """Two failures of one task with the same cause, error and stage."""
    task, first = seed(
        home, error=ORPHAN, title="修复 token 刷新", folder="shop",
        ended=time.time() - 700, started=time.time() - 750,
    )
    _, second = seed(
        home, error=ORPHAN, task=task, parent=first["id"],
        note="auto-retry 1/3\n继续", title="修复 token 刷新",
        ended=time.time() - 300, started=time.time() - 340,
    )
    return task, first, second


def spent_chain(home) -> tuple[dict, dict, list[dict]]:
    """A task whose three retries were spent: red, timeout, then exit 1."""
    now = time.time()
    task, original = seed(home, error="exit code 1", ended=now - 900, started=now - 960)
    children = [
        seed(home, error="exit code 1", task=task, parent=original["id"],
             note="auto-retry 1/3\n继续", ended=now - 700, started=now - 742)[1],
        seed(home, error="Command timed out after 60s", task=task,
             note="auto-retry 2/3\n继续", ended=now - 500, started=now - 538)[1],
        seed(home, error="exit code 1", task=task,
             note="auto-retry 3/3\n继续", ended=now - 300, started=now - 351)[1],
    ]
    db.update_run(children[1]["id"], parent_run_id=children[0]["id"])
    db.update_run(children[2]["id"], parent_run_id=children[1]["id"])
    children = [db.get_run(row["id"]) for row in children]
    red_event(children[0])
    add_tool_call(children[2], "write", filePath=str(home / "work" / "shop" / "app.py"))
    add_event(
        children[2],
        "tool_result",
        {
            "id": "call-1",
            "ok": False,
            "text": "Traceback (most recent call last):\n"
            '  File "tests/test_auth.py", line 12, in test_token_refresh\n'
            "    import jwt\nModuleNotFoundError: No module named 'jwt'",
        },
    )
    add_artifact(children[2], "app.py")
    add_artifact(children[2], "tests/test_auth.py")
    return task, original, children


# ---- refusing to retry, with the reason -------------------------------


def test_the_same_error_twice_is_a_stopped_row(home):
    _, first, second = same_signature_pair(home)

    board = server.board_state()
    # Nothing about a failure reaches a person as a question anymore.
    assert board["needs_you"] == []
    assert all(item.get("run") != second["id"] for item in board["auto_recovering"])
    entry = find(board, "stopped", second["id"])

    assert entry["kind"] == "stopped"
    assert entry["stop_reason"] == "same-signature"
    assert entry["settled"] is False
    assert entry["project"] == "shop"
    assert entry["task_title"] == "修复 token 刷新"
    # The failure class is the line's tag, not the gate: the gate has its own
    # line, and repeating it as the headline hid what actually broke.
    assert entry["failure_kind"] == "证据缺失"
    assert entry["headline"].startswith("证据缺失：")
    assert entry["evidence_missing"].startswith("证据缺失：")
    assert "连续两次同样的错" in entry["why"]
    assert entry["gate"] == "same-signature"
    assert "只会一样" in entry["why"]
    assert entry["retry_line"].startswith("自动重试 1 次：")
    assert "第1次 40s orphaned" in entry["retry_line"]
    # The report: what I tried, why I stopped, where the evidence is.
    assert entry["statement"].startswith("1 次都试过了：")
    assert entry["likely_cause"]
    assert entry["cause_line"].startswith(entry["likely_cause"])
    assert entry["plan"] and "由你决定" not in entry["plan"]
    assert "要不要" not in entry["plan"]


def test_a_budget_it_cannot_afford_is_a_question(home, monkeypatch):
    task = db.create_task(goal="导出 CSV", acceptance="pytest -q", title="导出")
    _, previous = seed(
        home, error="", status="completed", task=task, title="导出",
        usage_json=json.dumps({"cost": 0.5, "currency": "CNY"}),
        ended=time.time() - 700, started=time.time() - 740,
    )
    _, run = seed(home, error=ORPHAN, task=task, title="导出", ended=time.time() - 1)
    db.update_run(run["id"], usage_json=json.dumps({"cost": 0.4, "currency": "CNY"}))
    monkeypatch.setenv("NODUS_DAILY_BUDGET", "1.0")

    board = server.board_state()
    entry = find(board, "needs_you", run["id"])

    # The question is money, not "is this failure yours to deal with".
    assert entry["kind"] == "budget"
    assert entry["decision"] == "spend"
    assert entry["gate"] == "budget"
    assert entry["question"].startswith("要不要再花")
    assert "¥" in entry["question"]
    assert "剩余额度不够" in entry["basis"]
    assert "¥0.10" in entry["basis"]
    assert "NODUS_DAILY_BUDGET" in entry["suggestion"]
    # A spend decision carries no failure framing: those fields stay empty so
    # no row on the first screen reads as "this failed, you deal with it".
    assert entry["failure_kind"] == ""
    assert entry["headline"] == ""
    assert entry["retry_line"] == ""
    assert all(item["run_id"] != run["id"] for item in board["auto_recovering"])


def test_a_retry_above_the_cost_cap_waits_for_a_click(home):
    task = db.create_task(goal="贵活", acceptance="pytest -q", title="贵任务")
    seed(
        home, error="", status="completed", task=task, title="贵任务",
        usage_json=json.dumps({"cost": 1.2, "currency": "CNY"}),
        ended=time.time() - 700, started=time.time() - 740,
    )
    _, run = seed(home, error=ORPHAN, task=task, title="贵任务", ended=time.time() - 1)

    entry = find(server.board_state(), "needs_you", run["id"])

    assert entry["kind"] == "budget"
    assert entry["gate"] == "cost-cap"
    assert "¥1.20" in entry["basis"] and "¥0.50" in entry["basis"]
    assert "要不要再花" in entry["question"]
    assert entry["failure_kind"] == ""


def test_a_spent_chain_arrives_with_the_four_pieces_of_evidence(home):
    task, _original, children = spent_chain(home)

    board = server.board_state()
    assert board["needs_you"] == []
    entry = find(board, "stopped", children[2]["id"])

    assert entry["kind"] == "stopped"
    assert entry["stop_reason"] == "retry-limit"
    # Line 1: project · the last two directories · task; line 2: the class.
    assert entry["project"] == "shop"
    assert entry["project_path"] == "work/shop"
    assert entry["task_title"] == "修复登录"
    assert entry["failure_kind"] == "进程自己退出"
    assert entry["headline"] == (
        "进程自己退出：ModuleNotFoundError: No module named 'jwt'"
    )
    assert entry["gate"] == "retry-limit"
    assert "上限" in entry["why"]
    # Line 3 and 4: what I tried, and what it probably was. The cause line
    # names what would have to change, not what the reader must go do.
    assert entry["retry_line"].startswith("自动重试 3 次：")
    assert "第1次 42s tests_red" in entry["retry_line"]
    assert "第2次 38s timeout" in entry["retry_line"]
    assert entry["statement"].startswith("3 次都试过了：")
    assert "第3次 51s exit 1" in entry["statement"]
    assert entry["likely_cause"].startswith("大概率是进程自己退出")
    assert "要解决得" in entry["cause_line"]
    assert "由你决定" not in entry["plan"]

    ev = entry["evidence"]
    # 哪一步
    assert "write" in ev["action"] and "app.py" in ev["action"]
    # 什么错: the raw tail, not a paraphrase, capped at 200 characters
    assert "ModuleNotFoundError" in ev["error_tail"]
    assert len(ev["error_tail"]) <= 201
    # 改了哪
    assert entry["change"] == "改了 2 个文件：app.py、test_auth.py"
    assert ev["change"]["files"] == ["app.py", "tests/test_auth.py"]
    # 试过什么: one entry per retry, with what it returned
    assert [item["outcome"] for item in ev["attempts"]] == [
        "tests_red", "timeout", "exit 1",
    ]
    assert [item["seconds"] for item in ev["attempts"]] == [42, 38, 51]
    assert entry["plan"]


def test_a_git_repo_gets_a_diff_stat(home):
    task = db.create_task(goal="改配置", acceptance="pytest -q", title="改配置")
    first = seed(
        home, error="exit code 1", task=task, title="改配置",
        ended=time.time() - 900, started=time.time() - 940,
    )[1]
    task, run = seed(
        home, error="exit code 1", task=task, title="改配置", parent=first["id"],
        note="auto-retry 1/3\n继续", ended=time.time() - 600, started=time.time() - 640,
    )
    cwd = run["cwd"]
    base = ["git", "-C", cwd]
    subprocess.run([*base, "init", "-q"], check=True)
    subprocess.run([*base, "config", "user.email", "t@example.com"], check=True)
    subprocess.run([*base, "config", "user.name", "t"], check=True)
    (home / "work" / "shop" / "app.py").write_text("one\n", encoding="utf-8")
    subprocess.run([*base, "add", "app.py"], check=True)
    subprocess.run([*base, "commit", "-qm", "base"], check=True)
    (home / "work" / "shop" / "app.py").write_text("one\ntwo\n", encoding="utf-8")
    add_artifact(run, "app.py")

    entry = find(server.board_state(), "stopped", run["id"])

    assert entry["evidence"]["change"]["added"] == 1
    assert "app.py | +1 −0" in entry["evidence"]["change"]["stat"]
    assert entry["change"] == "1 个文件 +1 −0：app.py"


def test_a_deterministic_failure_is_not_a_decision(home):
    _, run = seed(home, error="401 Unauthorized")

    board = server.board_state()
    assert all(item.get("run") != run["id"] for item in board["needs_you"])
    entry = find(board, "deterministic", run["id"])
    assert "重试不会好" in entry["problem"]
    assert "密钥" in entry["suggestion"]
    assert entry["project"] == "shop"
    assert "401" in entry["headline"]
    assert "密钥" in entry["why"]
    assert entry["evidence"]["attempts"] == []
    # The same failure is in 已停止, where it is a statement, not a question.
    stop = find(board, "stopped", run["id"])
    assert stop["stop_reason"] == "deterministic"
    assert stop["statement"] == "这个错重试不会变，所以我一次都没试"
    assert "重试还是同一个错" in stop["plan"]


def test_an_unclassified_failure_is_judged_not_hidden(home):
    _, run = seed(home, error="exit code 1")

    board = server.board_state()
    # A first failure with no evidence of progress and nothing spent is cheap:
    # the machine retries it instead of asking.
    entry = next(item for item in board["auto_recovering"] if item["run_id"] == run["id"])
    assert entry["attempt"] == 1
    assert entry["limit"] == 3
    assert "退出码" in entry["problem"]


def test_a_run_that_recorded_no_evidence_says_so(home, monkeypatch):
    """A legacy run: a status, a one-line error, and nothing else.

    The card must not degrade into "执行失败". "证据缺失" is the answer to
    "什么样的失败" when the record cannot answer it, and it has to be the
    sentence on the card, not a footnote.
    """
    monkeypatch.setenv("NODUS_AUTO_RECOVER", "0")
    task, run = seed(home, error=ORPHAN, title="opencode smoke", folder="nodus-oc-smoke")
    # A lifecycle event is not evidence: it says the run ended, not what it did.
    add_event(run, "run.failed", {"error": ORPHAN})

    board = server.board_state()
    assert board["needs_you"] == []
    entry = find(board, "stopped", run["id"])
    assert entry["stop_reason"] == "disabled"

    missing = "证据缺失：这次运行没有记录 tool_call / 改动 / 测试输出"
    assert entry["failure_kind"] == "证据缺失"
    assert entry["headline"].startswith(missing)
    assert entry["evidence_missing"] == missing
    assert entry["evidence"]["attempts"] == []
    assert entry["retry_line"].startswith("未重试（原因：")
    # The cause still comes from the one line that survived, not from a shrug.
    assert entry["likely_cause"].startswith("大概率是进程被外部杀掉")
    assert entry["statement"]
    assert "由你决定" not in entry["plan"]
    # The one line that did survive is still shown, in brackets.
    assert "orphaned" in entry["headline"]


def test_every_cause_has_a_concrete_advice():
    # The causes are the keys: renaming a display label without renaming its
    # advice silently downgrades 建议 to a generic sentence, which is the bug.
    assert diagnosis.CAUSES
    for cause in diagnosis.CAUSES:
        advice = diagnosis.advice_of(cause)
        assert advice, cause
        assert "由你决定" not in advice
        assert "重跑" in advice or "接手" in advice or "重试" in advice


def test_every_cause_has_a_noun_phrase_for_the_stopped_row():
    # 已停止 is a report, so its cause line says what would have to change
    # ("要解决得装上缺的依赖") instead of ordering the reader to go do it.
    for cause in diagnosis.CAUSES:
        fix = diagnosis.fix_of(cause)
        assert fix, cause
        assert "由你决定" not in fix
        assert "点" not in fix


@pytest.mark.parametrize(
    ("error", "label"),
    [
        # The 13 causes, one real-looking line each. The first three are
        # verbatim from `~/.nodus` history; the rest are the shapes the old
        # `recover`/`_CAUSE_RULES` tests used.
        ("orphaned: the process that owned this run is gone", "进程被外部杀掉"),
        ("exit code 1", "进程自己退出"),
        ("[WinError 2] 系统找不到指定的文件。", "无法归类"),
        ("stale run from hung test", "无法归类"),
        ("Command timed out after 60s", "卡住超时"),
        ("ECONNRESET", "网络断了"),
        ("502 Bad Gateway", "上游服务出错"),
        ("429 Too Many Requests", "被上游限流或额度用尽"),
        ("401 Unauthorized", "密钥无效或没有权限"),
        ("400 Bad Request: messages must not be empty", "请求被拒绝"),
        ("model_not_found: gpt-9", "模型名不对"),
        ("content policy violation", "内容被安全策略拦截"),
    ],
)
def test_the_likely_cause_and_the_advice_come_from_the_error_string(error, label):
    cause = server._likely_cause({"error": error}, {}, "none")
    assert cause.startswith(f"大概率是{label}")
    # The raw line it matched is quoted, so a wrong guess is visibly a guess.
    assert "报错是" in cause
    advice = server._advice_for("进程退出", cause, {"error_tail": error})
    assert advice and "由你决定" not in advice


def test_a_red_test_names_the_failed_test_and_its_assertion(home):
    task, run = seed(home, error="", status="completed", title="修复登录")
    red_event(run)
    add_tool_call(run, "edit", file_path=str(home / "work" / "shop" / "app.py"))
    add_artifact(run, "app.py")

    board = server.board_state()
    assert board["needs_you"] == []
    entry = find(board, "stopped", run["id"])

    assert entry["stop_reason"] == "tests_red"
    assert entry["statement"] == "跑完了一遍，测试没过，我没有再重试"
    assert entry["headline"] == "测试红：test_token_refresh 断言 401 == 200"
    assert entry["evidence"]["tests"] == [
        {
            "name": "tests/test_auth.py::test_token_refresh",
            "test": "test_token_refresh",
            "assertion": "assert 401 == 200",
        }
    ]
    assert entry["evidence"]["test_cmd"] == "python -m pytest -q"


def test_over_budget_is_a_spend_question_not_a_retry(home):
    task = db.create_task(goal="g", acceptance="a", title="预算任务")
    db.update_task(task["id"], budget_json='{"max_runs": 1}')
    _, run = seed(home, error=ORPHAN, task=task)

    board = server.board_state()
    entry = find(board, "needs_you", run["id"])
    assert entry["kind"] == "budget"
    assert entry["gate"] == "over-budget"
    assert "预算上限" in entry["question"]
    assert "预算上限" in entry["basis"]
    assert entry["failure_kind"] == ""
    assert all(item["run_id"] != run["id"] for item in board["auto_recovering"])


def test_switching_recovery_off_stops_instead_of_asking(home, monkeypatch):
    monkeypatch.setenv("NODUS_AUTO_RECOVER", "0")
    _, run = seed(home, error=ORPHAN)

    board = server.board_state()
    assert board["needs_you"] == []
    assert board["handling"] == []
    entry = find(board, "stopped", run["id"])
    assert entry["stop_reason"] == "disabled"
    assert "自动恢复已关闭" in entry["problem"]
    assert board["auto_recovering"] == []


# ---- history, proposals and unverified runs ----------------------------


def test_a_failure_the_chain_moved_past_is_kept_in_history(home):
    task, original = seed(home, error=ORPHAN, ended=time.time() - 600)
    seed(home, error="", status="completed", task=task, parent=original["id"])

    board = server.board_state()
    assert all(item.get("run") != original["id"] for item in board["needs_you"])
    entry = find(board, "history_failures", original["id"])
    assert "已经自动重试过" in entry["problem"]
    assert entry["settled"] is True
    # Being history does not excuse a row from the five answers.
    assert entry["failure_kind"] == "证据缺失"
    assert "证据缺失" in entry["headline"]
    assert "不会为同一次失败开两张单" in entry["why"]
    assert entry["retry_line"].startswith("未重试（原因：")
    assert entry["likely_cause"]
    assert entry["statement"] == "后面已经派过重试，这一条翻过去了"
    assert "由你决定" not in entry["plan"]
    assert board["decision_counts"]["history_failed"] == 1


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
    assert entry["decision"] == "merge"
    assert "要不要合进主工作区" in entry["question"]
    assert "nodus/fix-login" in entry["basis"]
    assert entry["project"] == "shop"
    assert entry["task_title"] == "修复登录"
    assert entry["change"].startswith("1 file changed")
    assert "合并" in entry["suggestion"]
    assert "nodus/fix-login" in entry["headline"]


def test_unverified_runs_are_information_not_a_question(home):
    _, run = seed(home, error="", status="completed")

    board = server.board_state()
    # Not a decision: the answer is "cannot be verified", and no person is
    # being asked to pick one.
    assert all(item["kind"] != "unverified" for item in board["needs_you"])
    entry = board["unverifiable"][0]
    assert entry["kind"] == "unverifiable"
    assert "没有测试证据" in entry["problem"]
    assert entry["project"] == "shop"
    assert entry["headline"].startswith("最近 1 个 run 跑完却没有测试证据")
    # The reason names the actual cause and the advice names the fix: a task
    # with an acceptance command would have run the test.
    assert "目标目录没有测试入口" in entry["reason"]
    assert "验收标准" in entry["suggestion"]
    assert board["unverified_runs"][0]["id"] == run["id"]


# ---- additive contract -------------------------------------------------


def test_board_keeps_the_old_keys_and_adds_the_new_ones(home):
    seed(home, error="exit code 1")
    board = server.board_state()
    assert {
        "now", "day", "profile", "running", "decisions", "decision_counts", "today",
        "unverified_runs", "needs_you", "auto_recovering", "deterministic",
        "history_failures", "handling", "stopped", "unverifiable",
    } <= set(board)
    assert board["decision_counts"]["needs_you"] == len(board["needs_you"])
    assert board["decision_counts"]["handling"] == len(board["handling"])
    assert board["decision_counts"]["stopped"] == len(board["stopped"])
    assert board["decision_counts"]["unverifiable"] == len(board["unverifiable"])
    assert board["decision_counts"]["deterministic"] == len(board["deterministic"])


def test_every_stopped_row_has_the_three_parts(home):
    _, _first, second = same_signature_pair(home)
    entry = find(server.board_state(), "stopped", second["id"])

    # 1. what broke, with the records behind it
    assert entry["evidence"]["error_tail"]
    assert entry["change"] is not None or entry["evidence"]["missing"]
    # 2. why the machine stopped
    assert entry["why"] and "由你决定" not in entry["why"]
    # 3. the report: what I tried and where the evidence is
    assert entry["statement"]
    assert entry["plan"] and "由你决定" not in entry["plan"]
    assert "要不要" not in entry["plan"]


def test_failures_recovery_gave_up_on_are_still_visible(home):
    """A count of five failures must not lead to one visible row."""
    task, original = seed(home, error=ORPHAN, ended=time.time() - 600)
    seed(home, error="", status="completed", task=task, parent=original["id"])

    board = server.board_state()
    assert "history_failures" in board
    assert board["decision_counts"]["history_failed"] >= 1
    ids = {item["run"] for item in board["history_failures"]}
    assert original["id"] in ids
    for item in board["history_failures"]:
        assert item["kind"] == "stopped"
        assert item["settled"] is True
        assert item.get("problem")


# ---- the hard acceptance: a failure never reaches a person as a question


def test_no_failure_lands_in_needs_you(home):
    """The whole point: 失败不进「需要你决定」，那是系统自己的活.

    One of every failure shape the board knows about, and not one of them may
    show up as a question. The only decisions left are the three in
    `server.DECISION_KINDS`.
    """
    same_signature_pair(home)          # repeated error
    spent_chain(home)                  # retry limit reached
    _task, red_run = seed(home, error="", status="completed", title="红测试")
    red_event(red_run)                 # acceptance failed
    seed(home, error="401 Unauthorized", title="密钥坏了")   # deterministic
    seed(home, error="", status="completed", title="没验证")  # verify=none
    seed(home, error=ORPHAN, title="正在被重试")               # handling

    board = server.board_state()
    assert board["needs_you"] == []
    assert board["handling"], "the retryable recent failure must be in handling"
    assert board["stopped"], "the spent chain must be in stopped"

    # Every failure is visible somewhere that says what the machine did.
    failed = {r["id"] for r in db.list_runs(limit=50) if r["status"] == "failed"}
    visible = {item["run"] for item in board["handling"]} | {
        item["run"] for item in board["stopped"]
    }
    assert failed <= visible

    # No row here is a failure card, and none of the three voices leaks.
    for item in board["needs_you"]:
        assert item["kind"] in server.DECISION_KINDS
        assert item["kind"] not in {"failed", "red", "unverified", "stopped", "handling"}
        assert item.get("failure_kind") not in server.FAILURE_KINDS


def test_a_retry_in_flight_says_what_i_am_doing(home):
    _, run = seed(home, error="exit code 1", folder="nodus-review", title="创新点对抗评审")

    board = server.board_state()
    entry = find(board, "handling", run["id"])

    assert entry["kind"] == "handling"
    assert entry["attempt"] == 1
    assert entry["limit"] == 3
    assert entry["project"] == "nodus-review"
    assert entry["statement"] == "第 1/3 次重试"
    assert entry["history"] == []
    assert entry["retry_line"] == "还没重试过：这是第 1 次"
    assert entry["likely_cause"]
    assert "3 次都不行我就停下" in entry["plan"]
    assert "「已停止」" in entry["plan"]
    # The evidence is behind 详情; the only button on the card is 看日志.
    assert entry["evidence"]["attempts"] == []


def test_a_second_attempt_reports_the_first_one(home):
    task, first = seed(
        home, error="exit code 1", title="修依赖", ended=time.time() - 700,
        started=time.time() - 740,
    )
    _, second = seed(
        home, error="Command timed out after 60s", task=task, parent=first["id"],
        note="auto-retry 1/3\n继续", title="修依赖",
        ended=time.time() - 300, started=time.time() - 340,
    )

    board = server.board_state()
    entry = find(board, "handling", second["id"])

    assert entry["attempt"] == 2
    assert "第 2/3 次重试" in entry["statement"]
    assert [item["n"] for item in entry["history"]] == [1]
    assert entry["history"][0]["outcome"] == "timeout"
    assert "第1次 40s timeout" in entry["retry_line"]
    assert "第 2/3 次" in entry["plan"]
    assert board["decision_counts"]["handling"] == 1


# ---- the page ----------------------------------------------------------


def test_every_block_has_its_own_title_and_voice():
    for title in ("需要你决定", "我在处理", "已停止", "不可验证"):
        assert f"<h2>{title}</h2>" in ui.PAGE
    assert "只有三件事要你定" in ui.PAGE
    assert "失败、重试、验收都是我这边的事" in ui.PAGE
    assert "试到上限我就停" in ui.PAGE
    assert "不算通过，也不算失败" in ui.PAGE


def test_no_failure_copy_asks_the_reader_to_decide():
    # "执行失败，要不要重跑由你定" was the sentence this change removes. The
    # only question on the page is the decision card's 要决定 line.
    for phrase in ("由你决定", "要不要重跑", "要不要你决定", "由你定"):
        assert phrase not in ui.PAGE
    assert 'q-k">要决定' in ui.PAGE
    assert "d.question" in ui.PAGE
    assert 'stopped: ["项目", "我试了", "原因", "我的判断", "收尾"]' in ui.PAGE
    assert 'handling: ["项目", "我在做", "已试过", "原因推测", "计划"]' in ui.PAGE


def test_the_handling_card_offers_only_the_log():
    start = ui.PAGE.index("function handlingCard")
    end = ui.PAGE.index("function stoppedCard")
    body = ui.PAGE[start:end]
    assert "看日志" in body
    assert "重试" not in body
    assert "不用你点" in body


def test_the_stopped_card_is_a_report_with_a_convenience_button():
    start = ui.PAGE.index("function stoppedCard")
    end = ui.PAGE.index("const unvList")
    body = ui.PAGE[start:end]
    assert "我停了，证据都在这" in body
    assert "看日志" in body
    # A 重试 button is allowed as a shortcut, but the copy is a statement.
    assert ">重试</button>" in body
    assert "要不要" not in body


def test_the_unverifiable_block_explains_itself():
    start = ui.PAGE.index("function unverifiableCard")
    end = ui.PAGE.index("function renderTodo")
    body = ui.PAGE[start:end]
    assert "d.reason" in body
    assert "不可验证不等于通过" in body
    assert "不算失败" in body


def test_the_page_renders_the_five_answers_in_the_order_they_are_asked():
    start = ui.PAGE.index("function answerLines")
    end = ui.PAGE.index("function retryChips")
    body = ui.PAGE[start:end]
    # 项目 · 路径 · 任务; what I am doing / tried; the attempts; the cause; the plan
    order = ["projectLine(d, when)", "doingText(d, voice)", "d.retry_line",
             "tagLine(d)", "d.why", "d.plan"]
    positions = [body.index(token) for token in order]
    assert positions == sorted(positions)
    project = ui.PAGE[ui.PAGE.index("function projectLine"):start]
    for token in ("d.project", "d.project_path", "d.task_title"):
        assert token in project
    tag = ui.PAGE[ui.PAGE.index("function tagLine"):ui.PAGE.index("function doingText")]
    assert "d.cause_line" in tag and "d.failure_kind" in tag
    doing = ui.PAGE[ui.PAGE.index("function doingText"):end]
    assert "d.statement" in doing
    assert 'handling: ["项目", "我在做", "已试过", "原因推测", "计划"]' in ui.PAGE
    assert 'stopped: ["项目", "我试了", "原因", "我的判断", "收尾"]' in ui.PAGE

    detail = ui.PAGE[ui.PAGE.index("function detailBlock"):ui.PAGE.index("function decisionCard")]
    for label in ("哪一步", "什么错", "改了哪", "试过什么", "为什么"):
        assert label in detail
    assert '<details class="more">' in detail
    assert "ev.error_tail" in detail


def test_the_empty_decision_state_points_at_the_two_working_blocks():
    assert "没有需要你决定的事" in ui.PAGE
    assert "现在没有在处理的失败" in ui.PAGE
    assert "把结论和证据写进「已停止」" in ui.PAGE


def test_the_page_has_no_cdn_and_no_framework():
    lowered = ui.PAGE.lower()
    assert "cdn." not in lowered and "https://" not in lowered
    assert "react" not in lowered and "vue" not in lowered


def test_the_page_renders_the_evidence_and_the_money_gates():
    for label in ("项目", "要决定", "依据", "我在做", "我试了", "看日志", "详情"):
        assert label in ui.PAGE
    assert "这类证据没有记录" in ui.PAGE
    assert "d.evidence_missing" in ui.PAGE
    assert "继续花" in ui.PAGE
    assert "不花，看日志" in ui.PAGE
    assert "加额度后重跑" not in ui.PAGE
    assert "花这笔钱重跑" not in ui.PAGE
    assert "continuous" not in ui.PAGE


# ---- chat is not work --------------------------------------------------


def _chat_run(home, *, status: str = "completed", moment: float | None = None) -> dict:
    """One pet utterance, shaped the way llm._record writes it."""
    run = db.create_run(
        "", "claude", str(home / "work"), note="桌宠", route="claude",
        provider="relay", kind="chat",
    )
    at = time.time() - 3 if moment is None else moment
    fields = {"status": status, "started_at": at, "ended_at": at}
    if status == "failed":
        fields["error"] = "exit code 1"
    db.update_run(run["id"], **fields)
    return db.get_run(run["id"])


def test_pet_chatter_does_not_fill_the_board_window(home):
    """119 newer chat rows must not hide the one run that is actually work.

    The bug this pins: db.list_runs(limit=50) returned 50 pet utterances newest
    first, so every work count on the board read zero and the page looked idle
    while real runs were sitting right there.
    """
    now = time.time()
    work = seed(
        home, error="", status="completed", title="修好登录",
        ended=now - 5, started=now - 45,
    )[1]
    chatter = [_chat_run(home, moment=now - 4 + index * 0.001) for index in range(60)]

    board = server.board_state()

    # The window now holds work: the completed run is the only unverified row.
    assert [row["id"] for row in board["unverified_runs"]] == [work["id"]]
    assert board["decision_counts"]["unverified"] == 1
    # No chat row leaked into any of the work lists.
    chat_ids = {run["id"] for run in chatter}
    for field in ("needs_you", "handling", "stopped", "history_failures"):
        assert not chat_ids & {item.get("run") for item in board[field]}, field
    assert not chat_ids & {item.get("run") for item in board["decisions"]}


def test_the_board_still_counts_the_money_a_pet_spends(home):
    """Chat is not work, but it is a token bill: today's total keeps it."""
    now = time.time()
    seed(home, error="", status="completed", title="修好登录", ended=now - 5, started=now - 45)
    for index in range(6):
        _chat_run(home, moment=now - 4 + index * 0.001)
    for run in [r for r in db.list_runs(limit=100) if r["kind"] == "chat"]:
        db.update_run(
            run["id"],
            usage_json=json.dumps({"cost": 0.02, "currency": "CNY"}),
        )

    today = server.board_state()["today"]

    # 1 work + 6 chat rows started today: the money page does not filter chat.
    assert today["runs"] == 7
    assert today["totals"]["CNY"] == pytest.approx(0.12)


# ---- one failure counter, three readers --------------------------------


def test_one_failure_number_feeds_board_light_and_task_history(home):
    """2 failed (one retrying) + 1 completed: every reader quotes the same 2.

    - board: `decision_counts["failed"]` counts terminal failures in the
      50-work-run window. The deterministic 401 is in `stopped`, the timeout is
      in `handling` because it is waiting out its backoff; both are counted
      once.
    - statuslight: reads that number off the board, not from its own query.
    - lineage: same task, but its own history — the render says 本任务历史 and
      the denominator, so a 2 here is not offered as the board's total.
    """
    now = time.time()
    task, broken = seed(
        home, error="401 Unauthorized", title="密钥坏了",
        ended=now - 600, started=now - 640,
    )
    seed(
        home, error="Command timed out after 60s", task=task, title="密钥坏了",
        ended=now - 30, started=now - 70,
    )
    done = seed(home, error="", status="completed", task=task, title="密钥坏了")[1]
    add_event(
        done,
        "verify.green",
        {"phase": "green", "ok": True, "exit": 0, "duration_ms": 40,
         "cmd": ["python", "-m", "pytest", "-q"], "tail": "1 passed"},
    )

    board = server.board_state()
    counts = board["decision_counts"]
    assert counts["failed"] == 2
    assert counts["red"] == 0
    assert counts["handling"] == 1
    assert counts["stopped"] == 1
    assert counts["unverified"] == 0
    assert broken["id"] in {item["run"] for item in board["stopped"]}

    snap = statuslight.snapshot()
    assert snap == {
        "waiting": counts["needs_you"],
        "running": len(board["running"]),
        "failed": counts["failed"],
    }
    assert snap["failed"] == 2
    # The same board says the same thing twice: no second bubble.
    assert statuslight.line(snap, statuslight.snapshot()) == ("", "")

    hist = lineage.for_task(task["id"])
    assert hist["attempts"] == 3
    assert hist["ok"] == 1
    assert hist["failed"] == counts["failed"]
    text = lineage.render({"kind": "task", **hist})
    assert "本任务历史" in text
    assert "不是看板的失败总数" in text
    assert "attempts 3  ok 1  failed 2" in text


def test_a_red_test_is_one_failure_for_board_light_and_history(home):
    """The audit's exact case: completed + red used to be failed/red/invisible.

    Now the board's `failed` includes it (and `red` is that subset), the light
    quotes the same number, and lineage's task history counts it once with the
    scope label next to it.
    """
    task, run = seed(home, error="", status="completed", title="红测试")
    red_event(run)

    board = server.board_state()
    counts = board["decision_counts"]
    assert counts["failed"] == 1
    assert counts["red"] == 1  # subset, not a second failure
    assert statuslight.snapshot()["failed"] == 1

    hist = lineage.for_task(task["id"])
    assert hist["failed"] == 1
    text = lineage.render({"kind": "task", **hist})
    assert "本任务历史" in text
    assert "failed 含测试红 1 条" in text

