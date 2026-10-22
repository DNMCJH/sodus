"""The board: no build step, no placeholder left in the served page."""

from __future__ import annotations

from nodus import ui


def test_the_page_asks_for_the_token_exactly_once():
    assert ui.PAGE.count("__TOKEN__") == 1


def test_the_sections_come_in_the_agreed_order():
    # The first screen is the three questions that need no scrolling; the
    # working sections sit behind <details> in this order.
    order = ["正在跑", "需要你决定", "今天的钱", "最近执行", "提案", "派活"]
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
    # "opencode-glm · 目录 Temp/nodus-oc-smoke" said where, not what. The task
    # title wins, and a path that does appear keeps its full value in a tooltip.
    assert "task.title || task.goal" in ui.PAGE
    assert 'title="${esc(r.cwd)}"' in ui.PAGE
    assert "NOISE_DIRS" in ui.PAGE
    assert ".slice(-2).join" not in ui.PAGE


def test_remaining_failures_expand_where_they_are_counted():
    # No "look in the collapsed section at the bottom": the count sits at the
    # end of the failed rows and opens them in place.
    assert "在下面「更多」里" not in ui.PAGE
    assert "还有 ${n} ${failed ? \"个失败\" : \"个提案\"}" in ui.PAGE
    assert "toggleMoreFailed" in ui.PAGE
    assert "extraDecisions" in ui.PAGE
