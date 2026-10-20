"""Prompt assembly: rules are stable, the handoff is optional."""

from __future__ import annotations

from nodus.runner import build_prompt


TASK = {"goal": "implement add", "acceptance": "pytest green"}


def test_prompt_includes_handoff_by_default():
    prompt = build_prompt(TASK, "上一轮结论", "codex", "C:\\work")
    assert "## 上一位留下的交接" in prompt
    assert "上一轮结论" in prompt


def test_prompt_separates_note_from_task():
    prompt = build_prompt(TASK, "", "claude", "C:\\work", note="只写测试")
    assert "## 本次指令" in prompt
    assert "只写测试" in prompt
    assert "无，你是第一个执行者" in prompt


def test_prompt_can_drop_handoff_for_reviewers():
    prompt = build_prompt(TASK, "上一次审查说没问题", "claude", "C:\\work", include_handoff=False)
    assert "上一位留下的交接" not in prompt
    assert "上一次审查说没问题" not in prompt
    assert "implement add" in prompt
