"""The board: no build step, no placeholder left in the served page."""

from __future__ import annotations

from nodus import ui


def test_the_page_asks_for_the_token_exactly_once():
    assert ui.PAGE.count("__TOKEN__") == 1


def test_the_sections_come_in_the_agreed_order():
    # The first screen is the three questions that need no scrolling; the
    # working sections sit behind <details> in this order.
    order = ["正在跑", "等我决定", "今天的钱", "最近执行", "提案", "派活"]
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
