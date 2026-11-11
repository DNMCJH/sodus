"""Vendor parsers: one bad line must never kill a run, and cost must survive."""

from __future__ import annotations

import json

from sodus import adapters


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


# ---- opencode ----------------------------------------------------------
#
# Shapes below are copied from `opencode run --format json` on 1.19.3, one line
# per finished part, with the session id on every line.


def test_opencode_text_event_is_a_message():
    line = json.dumps(
        {"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "收到"}}
    )
    assert adapters.parse("opencode", line) == [{"type": "message", "text": "收到"}]


def test_opencode_step_start_carries_the_session_id():
    line = json.dumps(
        {"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}}
    )
    assert adapters.parse("opencode", line) == [{"type": "session", "session_id": "ses_1"}]


def test_opencode_step_finish_counts_reasoning_as_output():
    """`reasoning` sits outside `output`, and the other vendors bill both as
    output tokens. Ignoring it would undercount every thinking step."""
    line = json.dumps(
        {
            "type": "step_finish",
            "sessionID": "ses_1",
            "part": {
                "type": "step-finish",
                "tokens": {
                    "total": 14660,
                    "input": 14628,
                    "output": 11,
                    "reasoning": 21,
                    "cache": {"write": 2, "read": 3},
                },
                "cost": 0,
            },
        }
    )
    usage = adapters.parse("opencode", line)[0]
    assert usage["type"] == "usage"
    assert usage["input_tokens"] == 14628
    assert usage["output_tokens"] == 32
    assert usage["cache_read"] == 3
    assert usage["cache_write"] == 2


def test_opencode_reported_zero_is_not_a_cost():
    """The CLI reports `cost: 0` for a provider it has no price table for.

    Taking that at face value would record a run that looks priced and cost
    nothing; leaving it out lets the runner price it from the relay's meter.
    """
    line = json.dumps(
        {
            "type": "step_finish",
            "part": {
                "type": "step-finish",
                "tokens": {"input": 10, "output": 1, "reasoning": 0, "cache": {}},
                "cost": 0,
            },
        }
    )
    assert "cost" not in adapters.parse("opencode", line)[0]


def test_opencode_tool_use_is_both_call_and_result():
    line = json.dumps(
        {
            "type": "tool_use",
            "sessionID": "ses_1",
            "part": {
                "type": "tool",
                "tool": "bash",
                "callID": "call-1",
                "state": {
                    "status": "completed",
                    "input": {"command": "echo hello"},
                    "output": "hello\r\n",
                    "metadata": {"exit": 0},
                },
            },
        }
    )
    events = adapters.parse("opencode", line)
    assert [e["type"] for e in events] == ["tool_call", "tool_result"]
    assert events[0]["name"] == "bash"
    assert events[0]["input"] == {"command": "echo hello"}
    assert events[1]["ok"] is True


def test_opencode_a_failing_shell_tool_is_not_ok():
    line = json.dumps(
        {
            "type": "tool_use",
            "part": {
                "tool": "bash",
                "callID": "call-2",
                "state": {"status": "completed", "output": "boom", "metadata": {"exit": 1}},
            },
        }
    )
    assert adapters.parse("opencode", line)[1]["ok"] is False


def test_opencode_error_event_keeps_the_upstream_message():
    line = json.dumps(
        {
            "type": "error",
            "sessionID": "ses_1",
            "error": {
                "name": "UnknownError",
                "data": {"message": "Unexpected server error.", "ref": "err_1"},
            },
        }
    )
    event = adapters.parse("opencode", line)[0]
    assert event["type"] == "error"
    assert "Unexpected server error." in event["message"]


def test_opencode_stream_yields_final_text_and_usage():
    """The exact three-line stream of a real `回答两个字：收到` run."""
    lines = [
        '{"type":"step_start","timestamp":1,"sessionID":"ses_1","part":{"id":"prt_1","type":"step-start"}}',
        '{"type":"text","timestamp":2,"sessionID":"ses_1","part":{"id":"prt_2","type":"text","text":"收到"}}',
        '{"type":"step_finish","timestamp":3,"sessionID":"ses_1","part":{"id":"prt_3","reason":"stop","type":"step-finish","tokens":{"total":14666,"input":14618,"output":0,"reasoning":48,"cache":{"write":0,"read":0}},"cost":0}}',
    ]
    events = [event for line in lines for event in adapters.parse("opencode", line)]
    assert events[0] == {"type": "session", "session_id": "ses_1"}
    assert events[1] == {"type": "message", "text": "收到"}
    assert events[2]["output_tokens"] == 48
    assert "cost" not in events[2]


def test_garbage_does_not_raise():
    assert adapters.parse("claude", "not json") == []
    assert adapters.parse("nope", "{}")[0]["type"] == "raw"
    assert adapters.parse("opencode", "") == []
