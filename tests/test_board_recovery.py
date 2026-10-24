"""The first screen: a decision must say what broke, why it stopped, and what next.

`/board` keeps `decisions`/`decision_counts` for MCP and the pet; these tests
pin the newer contract. A failure the daemon retries is *not* a decision; a
failure it refused is a decision with the three things the operator asked for:
where exactly it broke (last tool call, raw error tail, failed tests, diff
stat), why the machine stopped retrying (one sentence from the retry gate), and
a concrete next step.
"""

from __future__ import annotations

import json
import subprocess
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


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-board-recovery")
    monkeypatch.delenv("NODUS_DAILY_BUDGET", raising=False)
    monkeypatch.delenv("NODUS_AUTO_RECOVER", raising=False)
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


def test_the_same_error_twice_is_a_decision(home):
    _, first, second = same_signature_pair(home)

    board = server.board_state()
    assert all(item.get("run") != second["id"] for item in board["auto_recovering"])
    entry = find(board, "needs_you", second["id"])

    assert entry["project"] == "shop"
    assert entry["task_title"] == "修复 token 刷新"
    # Line 2 is the failure class, not the gate: the gate has its own line and
    # its own field, and repeating it as the headline hid what actually broke.
    assert entry["failure_kind"] == "证据缺失"
    assert entry["headline"].startswith("证据缺失：")
    assert entry["evidence_missing"].startswith("证据缺失：")
    assert "连续两次同样的错" in entry["why"]
    assert entry["gate"] == "same-signature"
    assert "只会一样" in entry["why"]
    assert entry["retry_line"].startswith("自动重试 1 次：")
    assert "第1次 40s orphaned" in entry["retry_line"]
    assert entry["likely_cause"]
    assert entry["suggestion"] and "由你决定" not in entry["suggestion"]


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

    assert entry["gate"] == "budget"
    assert "剩余额度不够" in entry["problem"]
    assert "¥0.10" in entry["why"]
    assert "NODUS_DAILY_BUDGET" in entry["suggestion"]
    # The money sentence explains why the machine stopped (why), not what broke.
    assert "剩余额度不够" in entry["why"]
    assert entry["failure_kind"] in server.FAILURE_KINDS
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

    assert entry["gate"] == "cost-cap"
    assert "¥1.20" in entry["why"] and "¥0.50" in entry["why"]
    assert "手动批准" in entry["suggestion"] or "值不值" in entry["suggestion"]


def test_a_spent_chain_arrives_with_the_four_pieces_of_evidence(home):
    task, _original, children = spent_chain(home)

    board = server.board_state()
    entry = find(board, "needs_you", children[2]["id"])

    # Line 1: project · the last two directories · task; line 2: the class.
    assert entry["project"] == "shop"
    assert entry["project_path"] == "work/shop"
    assert entry["task_title"] == "修复登录"
    assert entry["failure_kind"] == "进程退出"
    assert entry["headline"] == (
        "进程退出：ModuleNotFoundError: No module named 'jwt'"
    )
    assert entry["gate"] == "retry-limit"
    assert "上限" in entry["why"]
    # Line 3 and 4: how many tries, and what it probably was.
    assert entry["retry_line"].startswith("自动重试 3 次：")
    assert "第1次 42s tests_red" in entry["retry_line"]
    assert "第2次 38s timeout" in entry["retry_line"]
    assert entry["likely_cause"].startswith("大概率是依赖没装")

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
    assert entry["suggestion"]


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

    entry = find(server.board_state(), "needs_you", run["id"])

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

    entry = find(server.board_state(), "needs_you", run["id"])

    missing = "证据缺失：这次运行没有记录 tool_call / 改动 / 测试输出"
    assert entry["failure_kind"] == "证据缺失"
    assert entry["headline"].startswith(missing)
    assert entry["evidence_missing"] == missing
    assert entry["evidence"]["attempts"] == []
    assert entry["retry_line"].startswith("未重试（原因：")
    # The cause still comes from the one line that survived, not from a shrug.
    assert entry["likely_cause"].startswith("大概率是进程被外部杀掉")
    assert entry["suggestion"] and "由你决定" not in entry["suggestion"]
    # The one line that did survive is still shown, in brackets.
    assert "orphaned" in entry["headline"]


def test_every_cause_label_has_a_concrete_advice():
    # The labels are the lookup keys: renaming one without renaming its advice
    # silently downgrades 建议 to a generic sentence, which is the whole bug.
    labels = [label for _pattern, label in server._CAUSE_RULES]
    assert labels
    assert [label for label in labels if label not in server._CAUSE_ADVICE] == []
    for advice in server._CAUSE_ADVICE.values():
        assert "由你决定" not in advice
        assert "重跑" in advice or "接手" in advice or "重试" in advice


@pytest.mark.parametrize(
    ("error", "label"),
    [
        ("ModuleNotFoundError: No module named 'jwt'", "依赖没装"),
        ("ImportError: cannot import name 'jwt' from 'jwt'", "依赖或导入路径不对"),
        ("bash: pytest: command not found", "命令不在 PATH 里"),
        ("FileNotFoundError: [Errno 2] No such file or directory: 'a.py'", "路径或环境不对"),
        ("401 Unauthorized", "密钥无效或没有权限"),
        ("429 Too Many Requests", "被上游限流或额度用尽"),
        ("This model's maximum context length is 8192 tokens", "上下文太长"),
        ("model_not_found: gpt-9", "模型名不对"),
        ("Command timed out after 60s", "卡住超时"),
        ("Connection refused", "目标服务没起"),
        ("502 Bad Gateway", "上游服务出错"),
        ("orphaned: the process that owned this run is gone", "进程被外部杀掉"),
        ("exit code 1", "进程自己退出"),
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

    entry = find(server.board_state(), "needs_you", run["id"])

    assert entry["headline"] == "测试红：test_token_refresh 断言 401 == 200"
    assert entry["evidence"]["tests"] == [
        {
            "name": "tests/test_auth.py::test_token_refresh",
            "test": "test_token_refresh",
            "assertion": "assert 401 == 200",
        }
    ]
    assert entry["evidence"]["test_cmd"] == "python -m pytest -q"


def test_over_budget_is_a_decision_not_a_retry(home):
    task = db.create_task(goal="g", acceptance="a", title="预算任务")
    db.update_task(task["id"], budget_json='{"max_runs": 1}')
    _, run = seed(home, error=ORPHAN, task=task)

    board = server.board_state()
    entry = find(board, "needs_you", run["id"])
    assert entry["gate"] == "over-budget"
    assert "预算上限" in entry["problem"]
    assert all(item["run_id"] != run["id"] for item in board["auto_recovering"])


def test_switching_recovery_off_makes_failures_a_decision(home, monkeypatch):
    monkeypatch.setenv("NODUS_AUTO_RECOVER", "0")
    _, run = seed(home, error=ORPHAN)

    board = server.board_state()
    entry = find(board, "needs_you", run["id"])
    assert entry["gate"] == "disabled"
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
    # Being history does not excuse a row from the five questions.
    assert entry["failure_kind"] == "证据缺失"
    assert "证据缺失" in entry["headline"]
    assert "不会为同一次失败开两张单" in entry["why"]
    assert entry["retry_line"].startswith("未重试（原因：")
    assert entry["likely_cause"]
    assert entry["suggestion"] and "由你决定" not in entry["suggestion"]
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
    assert entry["project"] == "shop"
    assert entry["task_title"] == "修复登录"
    assert entry["change"].startswith("1 file changed")
    assert "合并" in entry["suggestion"]
    assert "nodus/fix-login" in entry["headline"]


def test_unverified_runs_stay_one_explained_row(home):
    _, run = seed(home, error="", status="completed")

    board = server.board_state()
    entry = next(item for item in board["needs_you"] if item["kind"] == "unverified")
    assert "没有测试证据" in entry["problem"]
    assert entry["project"] == "shop"
    assert entry["headline"].startswith("最近 1 个 run 跑完却没有测试证据")
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


def test_every_decision_has_the_three_parts(home):
    _, _first, second = same_signature_pair(home)
    entry = find(server.board_state(), "needs_you", second["id"])

    # 1. what broke, with the records behind it
    assert entry["evidence"]["error_tail"]
    assert entry["change"] is not None or entry["evidence"]["missing"]
    # 2. why the machine stopped
    assert entry["why"] and "由你决定" not in entry["why"]
    # 3. a concrete next step
    assert entry["suggestion"] and "由你决定" not in entry["suggestion"]
    assert "重跑" in entry["suggestion"] or "接手" in entry["suggestion"]


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
        assert item["kind"] == "failed"
        assert item.get("problem")


# ---- the page ----------------------------------------------------------


def test_the_page_renders_the_five_answers():
    start = ui.PAGE.index("function questionLines")
    end = ui.PAGE.index("function detailBlock")
    body = ui.PAGE[start:end]
    # 项目 · 路径 · 任务; 失败类型; 重试历史; 可能原因; 建议
    order = ["d.project", "d.project_path", "d.headline", "d.retry_line",
             "d.likely_cause", "d.suggestion"]
    positions = [body.index(token) for token in order]
    assert positions == sorted(positions)
    detail = ui.PAGE[end:ui.PAGE.index("function decisionRow")]
    for label in ("哪一步", "什么错", "改了哪", "试过什么", "为什么"):
        assert label in detail
    assert "diff" not in detail.lower() or "change.stat" in detail


def test_the_empty_decision_state_names_what_is_running():
    assert "没有需要你决定的事" in ui.PAGE
    assert "个任务正在自动重试" in ui.PAGE
    assert "现在没有任务在自动重试" in ui.PAGE


def test_the_page_has_no_cdn_and_no_framework():
    lowered = ui.PAGE.lower()
    assert "cdn." not in lowered and "https://" not in lowered
    assert "react" not in lowered and "vue" not in lowered


def test_the_page_renders_the_evidence_and_the_money_gates():
    for label in ("项目", "失败", "重试", "原因", "建议", "哪一步", "什么错", "改了哪"):
        assert label in ui.PAGE
    assert "这类证据没有记录" in ui.PAGE
    assert "d.evidence_missing" in ui.PAGE
    assert "加额度后重跑" in ui.PAGE
    assert "花这笔钱重跑" in ui.PAGE
    assert "continuous" not in ui.PAGE
