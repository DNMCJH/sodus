"""Vendor parsers: one bad line must never kill a run, and cost must survive."""

from __future__ import annotations

import json

from nodus import adapters


def test_claude_result_carries_cost():
    line = json.dumps(
        {
            "type": "result",
            "result": "done",
            "total_cost_usd": 0.0123,
            "usage": {
                "input_tokens": 5,
                "output_tokens": 7,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 9,
            },
        }
    )
    events = adapters.parse("claude", line)
    usage = [e for e in events if e["type"] == "usage"][0]
    assert usage["cost"] == 0.0123
    assert usage["cache_read"] == 100
    assert usage["cache_write"] == 9
    message = [e for e in events if e["type"] == "message"][0]
    assert message["final"] is True


def test_claude_assistant_tool_use_and_thinking():
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "ok"},
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "a"}},
                ],
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        }
    )
    kinds = [e["type"] for e in adapters.parse("claude", line)]
    assert kinds == ["thinking", "message", "tool_call", "usage"]


def test_codex_command_execution_maps_tool_call_and_result():
    call = json.dumps(
        {
            "type": "item.started",
            "item": {"type": "command_execution", "id": "c1", "command": "pytest"},
        }
    )
    done = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "c1",
                "command": "pytest",
                "exit_code": 0,
                "aggregated_output": "1 passed",
            },
        }
    )
    assert adapters.parse("codex", call)[0]["type"] == "tool_call"
    result = adapters.parse("codex", done)[0]
    assert result["type"] == "tool_result"
    assert result["ok"] is True


def test_dsh_final_and_error():
    assert adapters.parse("dsh", json.dumps({"type": "final", "text": "hi"}))[0]["final"] is True
    assert adapters.parse("dsh", json.dumps({"type": "error", "message": "boom"}))[0]["type"] == "error"


def test_dsh_tool_call_and_result():
    call = json.dumps(
        {
            "type": "tool_call",
            "callId": "c1",
            "tool": "write",
            "input": {"file_path": "hello.txt", "content": "hi\n"},
        }
    )
    done = json.dumps(
        {
            "type": "tool_result",
            "callId": "c1",
            "status": "completed",
            "result": "<path>hello.txt</path>",
        }
    )
    tool_call = adapters.parse("dsh", call)[0]
    assert tool_call["type"] == "tool_call"
    assert tool_call["name"] == "write"
    result = adapters.parse("dsh", done)[0]
    assert result["type"] == "tool_result"
    assert result["ok"] is True
    assert "hello.txt" in result["text"]


def test_dsh_step_usage_is_normalized():
    line = json.dumps(
        {
            "type": "status",
            "phase": "step_end",
            "usage": {
                "inputTokens": 21085,
                "outputTokens": 367,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 12,
            },
        }
    )
    events = adapters.parse("dsh", line)
    usage = [e for e in events if e["type"] == "usage"][0]
    assert usage["input_tokens"] == 21085
    assert usage["output_tokens"] == 367
    assert usage["cache_write"] == 12


def test_dsh_text_and_thinking():
    assert adapters.parse("dsh", json.dumps({"type": "text", "text": "hi"}))[0]["type"] == "message"
    thinking = adapters.parse("dsh", json.dumps({"type": "thinking", "text": "hmm"}))[0]
    assert thinking["type"] == "thinking"


def test_dsh_log_line_is_not_json():
    assert adapters.parse("dsh", "[10/20/26 02:32:00] INFO Processing request") == []


def test_dsh_turn_error_becomes_an_error_event():
    """A DSH turn that dies on auth reports it only in `reason`.

    The trailing `dsh: AUTH: 401 ...` line is plain text, so without this the
    run records nothing but "exit code 1" and a fallback can never tell an
    endpoint failure from a task failure.
    """
    line = json.dumps(
        {
            "type": "status",
            "phase": "turn_end",
            "reason": {
                "kind": "error",
                "error": {"message": "401: WorkBuddy: 401 Unauthorized", "code": "AUTH"},
            },
        }
    )
    events = adapters.parse("dsh", line)
    assert [e["type"] for e in events] == ["status", "error"]
    assert "401" in events[1]["message"]


def test_dsh_a_clean_turn_end_is_not_an_error():
    line = json.dumps({"type": "status", "phase": "turn_end", "reason": {"kind": "done"}})
    assert [e["type"] for e in adapters.parse("dsh", line)] == ["status"]


def test_unknown_line_is_preserved_as_raw():
    events = adapters.parse("claude", json.dumps({"type": "something_new", "x": 1}))
    assert events[0]["type"] == "raw"


def test_garbage_does_not_raise():
    assert adapters.parse("claude", "not json") == []
    assert adapters.parse("nope", "{}")[0]["type"] == "raw"
