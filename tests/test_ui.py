"""The board: no build step, no placeholder left in the served page.

The first screen is a decision surface, not a log viewer: a failure the daemon
can retry must not look like a question, and a question must carry enough
context to answer without opening anything else.
"""

from __future__ import annotations

from nodus import recover, ui


def test_the_page_asks_for_the_token_exactly_once():
    assert ui.PAGE.count("__TOKEN__") == 1


def test_the_sections_come_in_the_agreed_order():
    # The first screen is the questions that need no scrolling; the working
    # sections sit behind <details> in this order.
    order = [
        "正在跑", "需要你决定", "自动重试中", "不会自动重试", "今天的钱",
        "最近执行", "提案", "派活",
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
    assert "重跑最近一次" in ui.PAGE
    assert "重跑并验收" not in ui.PAGE
    assert "rerunOne" in ui.PAGE
    assert "重跑这一次" in ui.PAGE


def test_a_run_is_named_by_its_task_before_its_directory():
    # "Temp/nodus-oc-smoke" said where, not what. The server sends the task
    # title with every decision; paths that do appear keep a full-path tooltip.
    assert "d.task_title" in ui.PAGE
    assert 'title="${esc(r.cwd)}"' in ui.PAGE
    assert "NOISE_DIRS" in ui.PAGE
    assert ".slice(-2).join" not in ui.PAGE


def test_the_first_screen_renders_needs_you_not_the_old_blob():
    # `decisions` stays in the API for MCP and the pet, but the page reads the
    # recovered split so a retry in flight never looks like a question.
    assert "b.needs_you" in ui.PAGE
    assert "b.auto_recovering" in ui.PAGE
    assert "b.deterministic" in ui.PAGE
    assert "b.decisions" not in ui.PAGE


def test_a_decision_row_is_ordered_project_task_problem_change_suggestion():
    start = ui.PAGE.index("function decisionRow")
    end = ui.PAGE.index("function retryWait")
    body = ui.PAGE[start:end]
    order = ["d.project", "d.task_title", "d.problem", "d.change", "d.suggestion"]
    positions = [body.index(token) for token in order]
    assert positions == sorted(positions)


def test_the_auto_retry_block_shows_progress_and_offers_nothing_to_click():
    # The operator has no decision here, so the block must not grow a button.
    start = ui.PAGE.index("function renderRecover")
    end = ui.PAGE.index("function renderBlocked")
    body = ui.PAGE[start:end]
    assert "attempt" in body and "next_retry_at" in body
    assert "<button" not in body


def test_remaining_decisions_expand_where_they_are_counted():
    assert "在下面「更多」里" not in ui.PAGE
    assert "toggleMoreNeeds" in ui.PAGE
    assert "还有 ${rest.length} 件" in ui.PAGE


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
