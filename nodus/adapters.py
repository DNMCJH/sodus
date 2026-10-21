"""Per-vendor stream parsers.

Every parser turns one vendor line into a list of normalized events:

    session      {session_id}
    thinking     {text}
    message      {text}
    tool_call    {id, name, input}
    tool_result  {id, ok, text}
    usage        {input_tokens, output_tokens, cache_read, cache_write, cost}
    error        {message}
    raw          {data}

Unknown lines are preserved as `raw` instead of being dropped, so a vendor
upgrade degrades to noise rather than silence.
"""

from __future__ import annotations

import json


def _load(line: str) -> dict | None:
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _usage(u: dict | None) -> dict | None:
    if not isinstance(u, dict):
        return None
    out = {
        "input_tokens": u.get("input_tokens", 0) or 0,
        "output_tokens": u.get("output_tokens", 0) or 0,
        "cache_read": u.get("cache_read_input_tokens", 0) or 0,
        "cache_write": u.get("cache_creation_input_tokens", 0) or 0,
    }
    if "total_cost_usd" in u:
        out["cost"] = u["total_cost_usd"]
    return out


def parse_claude(line: str) -> list[dict]:
    obj = _load(line)
    if obj is None:
        return []
    t = obj.get("type")
    if t == "system" and obj.get("subtype") == "init":
        return [
            {
                "type": "session",
                "session_id": obj.get("session_id"),
                "model": obj.get("model"),
                "tools": obj.get("tools", []),
            }
        ]
    if t == "assistant":
        msg = obj.get("message") or {}
        out: list[dict] = []
        for block in msg.get("content") or []:
            kind = block.get("type")
            if kind == "text" and block.get("text"):
                out.append({"type": "message", "text": block["text"]})
            elif kind == "thinking" and block.get("thinking"):
                out.append({"type": "thinking", "text": block["thinking"]})
            elif kind == "tool_use":
                out.append(
                    {
                        "type": "tool_call",
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": block.get("input"),
                    }
                )
        usage = _usage(msg.get("usage"))
        if usage:
            out.append({"type": "usage", **usage})
        return out
    if t == "user":
        msg = obj.get("message") or {}
        out = []
        for block in msg.get("content") or []:
            if block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    text = "\n".join(
                        b.get("text", "") for b in content if b.get("type") == "text"
                    )
                else:
                    text = str(content or "")
                out.append(
                    {
                        "type": "tool_result",
                        "id": block.get("tool_use_id"),
                        "ok": not block.get("is_error", False),
                        "text": text[:4000],
                    }
                )
        return out
    if t == "result":
        out = []
        text = obj.get("result")
        if isinstance(text, str) and text:
            out.append({"type": "message", "text": text, "final": True})
        usage = _usage(obj.get("usage"))
        if usage:
            # total_cost_usd sits on the result object, not inside usage
            if isinstance(obj.get("total_cost_usd"), (int, float)):
                usage["cost"] = float(obj["total_cost_usd"])
            out.append({"type": "usage", **usage})
        if obj.get("is_error"):
            # `result` is empty when the failure happened before any turn; the
            # real reason sits in `errors` and is the difference between
            # "claude error" and "no conversation found with that session id".
            detail = (
                obj.get("result")
                or (obj.get("errors") or [None])[0]
                or obj.get("subtype")
                or "claude error"
            )
            out.append({"type": "error", "message": str(detail)})
        return out
    return [{"type": "raw", "data": obj}]


def parse_codex(line: str) -> list[dict]:
    obj = _load(line)
    if obj is None:
        return []
    t = obj.get("type")
    if t == "thread.started":
        return [{"type": "session", "session_id": obj.get("thread_id")}]
    if t == "turn.completed":
        u = _usage(obj.get("usage"))
        return [{"type": "usage", **u}] if u else []
    if t in ("item.started", "item.completed"):
        item = obj.get("item") or {}
        it = item.get("type")
        done = t == "item.completed"
        if it == "agent_message" and done:
            return [{"type": "message", "text": item.get("text", "")}]
        if it == "reasoning" and done:
            return [{"type": "thinking", "text": item.get("text", "")}]
        if it == "error":
            return [{"type": "error", "message": item.get("message", "codex error")}]
        if it == "command_execution":
            if not done:
                return [
                    {
                        "type": "tool_call",
                        "id": item.get("id"),
                        "name": "shell",
                        "input": {"command": item.get("command")},
                    }
                ]
            return [
                {
                    "type": "tool_result",
                    "id": item.get("id"),
                    "ok": item.get("exit_code") in (0, None),
                    "text": (item.get("aggregated_output") or "")[:4000],
                }
            ]
        if done:
            return [{"type": "raw", "data": item}]
        return []
    return [{"type": "raw", "data": obj}]


def parse_dsh(line: str) -> list[dict]:
    obj = _load(line)
    if obj is None:
        return []
    t = obj.get("type")
    if t == "session":
        return [
            {
                "type": "session",
                "session_id": obj.get("sessionId"),
                "cwd": obj.get("cwd"),
            }
        ]
    if t == "final":
        return [{"type": "message", "text": obj.get("text", ""), "final": True}]
    if t == "text":
        return [{"type": "message", "text": obj.get("text", "")}]
    if t == "thinking":
        return [{"type": "thinking", "text": obj.get("text", "")}]
    if t == "tool_call":
        return [
            {
                "type": "tool_call",
                "id": obj.get("callId"),
                "name": obj.get("tool"),
                "input": obj.get("input"),
            }
        ]
    if t == "tool_result":
        return [
            {
                "type": "tool_result",
                "id": obj.get("callId"),
                "ok": obj.get("status") == "completed",
                "text": str(obj.get("result") or "")[:4000],
            }
        ]
    if t == "status":
        out: list[dict] = [{"type": "status", "phase": obj.get("phase")}]
        usage = obj.get("usage")
        if isinstance(usage, dict):
            out.append(
                {
                    "type": "usage",
                    "input_tokens": usage.get("inputTokens", 0) or 0,
                    "output_tokens": usage.get("outputTokens", 0) or 0,
                    "cache_read": usage.get("cacheReadTokens", 0) or 0,
                    "cache_write": usage.get("cacheWriteTokens", 0) or 0,
                }
            )
        # A turn that dies on an auth or transport error reports it here and
        # nowhere else: the trailing stderr line is not JSON, so dropping this
        # leaves the run with nothing but "exit code 1" and no way to tell an
        # endpoint failure from a task failure.
        reason = obj.get("reason")
        if isinstance(reason, dict) and reason.get("kind") == "error":
            error = reason.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else None
            out.append({"type": "error", "message": str(message or reason)})
        return out
    if t == "error":
        return [{"type": "error", "message": str(obj)}]
    return [{"type": "raw", "data": obj}]


PARSERS = {"claude": parse_claude, "codex": parse_codex, "dsh": parse_dsh}


def parse(vendor: str, line: str) -> list[dict]:
    parser = PARSERS.get(vendor)
    if parser is None:
        return [{"type": "raw", "data": line}]
    try:
        return parser(line)
    except Exception as exc:  # a parser bug must not kill the run
        return [{"type": "error", "message": f"parser error: {exc}"}]
