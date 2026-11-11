"""The board: no build step, no placeholder left in the served page.

The first screen is a decision surface, not a log viewer: a failure the daemon
can retry must not look like a question, and a question must carry enough
context to answer without opening anything else.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from sodus import recover, ui


def test_the_page_asks_for_the_token_exactly_once():
    assert ui.PAGE.count("__TOKEN__") == 1


def test_the_sections_come_in_the_agreed_order():
    # The first screen is questions plus the two working blocks; the money and
    # the logs sit behind them.
    order = [
        "正在跑", "需要你决定", "我在处理", "已停止", "不可验证", "记忆待审",
        "今天的钱", "用量", "终端", "最近执行", "提案", "派活",
    ]
    positions = [ui.PAGE.index(name) for name in order]
    assert positions == sorted(positions)


def test_the_page_states_that_finished_is_not_passed():
    assert "结束 ≠ 通过" in ui.PAGE


def test_dispatch_defaults_to_the_safer_mode():
    # proposal mode and auto-verify are checked by default: the two settings
    # that keep an accidental click from rewriting a working tree
    assert 'id="f-propose" checked' in ui.PAGE
    assert 'id="f-verify" checked' in ui.PAGE


def test_nothing_is_loaded_from_a_cdn():
    # the daemon serves this offline; a CDN reference would make the board
    # depend on the network the tool is supposed to outlive
    lowered = ui.PAGE.lower()
    assert "cdn." not in lowered and "https://" not in lowered


def test_unverified_runs_are_listed_before_they_are_rerun():
    # One number must not stand in for a button that acts on one of 43 runs:
    # the page offers the list, and each row reruns exactly that run.
    assert "看看是哪" in ui.PAGE
    assert "重跑这一次" in ui.PAGE
    assert "rerunOne" in ui.PAGE
    assert "重跑并验收" not in ui.PAGE


def test_a_run_is_named_by_its_task_before_its_directory():
    # "Temp/sodus-oc-smoke" said where, not what. The server sends the task
    # title with every decision; paths that do appear keep a full-path tooltip.
    assert "d.task_title" in ui.PAGE
    assert 'title="${esc(r.cwd)}"' in ui.PAGE
    assert "NOISE_DIRS" in ui.PAGE
    assert ".slice(-2).join" not in ui.PAGE


def test_the_first_screen_renders_the_split_not_the_old_blob():
    # `decisions` stays in the API for MCP and the pet, but the page reads the
    # split by whose job it is: a failure is never a question.
    assert ".needs_you" in ui.PAGE
    assert ".handling" in ui.PAGE
    assert ".stopped" in ui.PAGE
    assert ".unverifiable" in ui.PAGE
    assert "b.decisions" not in ui.PAGE
    assert "b.auto_recovering" not in ui.PAGE


def test_a_failure_row_is_five_answers_in_the_order_they_are_asked():
    # 哪个项目 → 我在做/我试了 → 已经试过几次 → 可能原因 → 接下来怎么办.
    # The labels change with the voice; the order does not.
    start = ui.PAGE.index("function answerLines")
    end = ui.PAGE.index("function retryChips")
    body = ui.PAGE[start:end]
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
    assert "d.statement" in doing and "next_retry_at" in doing
    assert 'handling: ["项目", "我在做", "已试过", "原因推测", "计划"]' in ui.PAGE
    assert 'stopped: ["项目", "我试了", "原因", "我的判断", "收尾"]' in ui.PAGE
    assert "d.failure_kind" in body
    assert "d.evidence_missing" in body


def test_the_question_card_is_the_only_one_that_asks():
    # 需要你决定 is a value judgement: one question and the evidence behind it.
    start = ui.PAGE.index("function decisionCard")
    end = ui.PAGE.index("function handlingCard")
    body = ui.PAGE[start:end]
    assert 'q-k">要决定' in body
    assert "d.question" in body
    assert 'q-k">依据' in body
    assert "合并" in body and "继续花" in body


def test_evidence_is_folded_not_printed_under_the_answer():
    start = ui.PAGE.index("function detailBlock")
    end = ui.PAGE.index("function decisionCard")
    body = ui.PAGE[start:end]
    assert '<details class="more">' in body
    assert "ev.error_tail" in body


def test_the_handling_card_offers_only_the_log():
    # The operator has no decision here, so the block must not grow a rerun
    # button: 看日志 is information, not a prompt.
    start = ui.PAGE.index("function handlingCard")
    end = ui.PAGE.index("function stoppedCard")
    body = ui.PAGE[start:end]
    assert "看日志" in body
    assert "重试" not in body
    assert "不用你点" in body
    assert "next_retry_at" in ui.PAGE


def test_remaining_decisions_expand_where_they_are_counted():
    assert "在下面「更多」里" not in ui.PAGE
    assert "toggleMoreNeeds" in ui.PAGE
    assert "还有 ${rest.length} 条" in ui.PAGE
    # Eight is the fold, not three: the count and the rows must agree.
    assert "const LIMIT = 8" in ui.PAGE
    assert "needs.slice(0, 3)" not in ui.PAGE


def test_older_failures_stay_reachable_under_the_fresh_stops():
    # already-retried / task-running / too-old failures are not questions, but
    # they must not vanish while the failures are still counted.
    assert "settled" in ui.PAGE
    assert "toggleHistory" in ui.PAGE
    assert "更早的失败" in ui.PAGE
    assert "history_failures" not in ui.PAGE


def test_a_retry_in_flight_says_so_in_the_live_row():
    # The live row reads the note first; "auto-retry 1/2" must become a
    # sentence, not appear as the agent's task description.
    assert "正在自动重试（第" in ui.PAGE
    assert "auto-retry (\\d+)" in ui.PAGE


def test_failure_wording_lives_in_recover_not_in_the_page():
    # One classifier, one sentence per cause. Copying it into ui.py is how the
    # board and the log view end up disagreeing about the same failure.
    assert "启动它的进程退出了" not in ui.PAGE
    assert "启动它的进程退出了" in recover.problem_text(
        {"error": "orphaned: the process that owned this run is gone"}
    )


def test_the_inline_script_is_actually_javascript(tmp_path):
    # The page *is* the product: one stray escape in the Python template turns
    # the whole inline script into a syntax error and the board silently stops
    # rendering. Nothing else in the suite would catch that, so parse it with
    # node when node is present (there is no JS build step to do it first).
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the page script cannot be parsed")
    scripts = re.findall(r"<script>(.*?)</script>", ui.PAGE, re.S)
    assert scripts
    for index, script in enumerate(scripts):
        target = tmp_path / f"script{index}.js"
        target.write_text(script, encoding="utf-8")
        proc = subprocess.run([node, "--check", str(target)], capture_output=True)
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


def test_the_terminals_panel_treats_every_harness_the_same():
    # 一视同仁 is the requirement: one renderer, one set of fields, five rows.
    # The server decides 装没装/在不在跑; the page only words the three states,
    # so a sixth harness later needs no branch here.
    assert "<h2>终端</h2>" in ui.PAGE
    assert "function renderTerminals" in ui.PAGE
    assert "renderTerminals(board.terminals)" in ui.PAGE
    for state in ("运行中", "空闲", "未安装"):
        assert state in ui.PAGE
    assert "今日 <b>" in ui.PAGE and "会话" in ui.PAGE
    assert "last_active" in ui.PAGE
    start = ui.PAGE.index("function renderTerminals")
    end = ui.PAGE.index("function renderRuns")
    body = ui.PAGE[start:end]
    assert ".key ===" not in body


def test_the_board_refreshes_only_when_asked():
    # A 1s tick, a 15s /board poll, a 5s SSE fallback and the EventSource
    # itself all went away: an idle board must not cost a request. If any of
    # them comes back, this test is the place that says it was a decision.
    assert "setInterval" not in ui.PAGE
    assert "EventSource" not in ui.PAGE
    assert "data-start" not in ui.PAGE
    assert "function tick" not in ui.PAGE
    assert 'id="refresh"' in ui.PAGE
    # the button belongs to the header, not to a section further down
    assert ui.PAGE.index('id="refresh"') < ui.PAGE.index("<main>")
    assert "visibilitychange" in ui.PAGE
    assert "刷新中…" in ui.PAGE
    # one fetch on load, then only a click or a tab focus does another
    assert "loadRoutes(); refresh();" in ui.PAGE

