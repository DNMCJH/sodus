"""MCP over stdio: JSON-RPC framing, the five tools, and fail-soft errors."""

from __future__ import annotations

import io
import json
import time

import pytest

from nodus import config, db, mcp

EXPECTED_TOOLS = {
    "nodus_task_create",
    "nodus_run",
    "nodus_run_status",
    "nodus_proposals",
    "nodus_cost_today",
    "nodus_memory_remember",
    "nodus_memory_recall",
}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A state directory per test: db.connect() creates it on demand."""
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-mcp-test")
    (tmp_path / "home").mkdir()
    return tmp_path


def call(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    reply = mcp.handle_message(message)
    assert reply is not None, f"{method} produced no response"
    assert reply["jsonrpc"] == "2.0"
    assert reply["id"] == request_id
    return reply


def tool(name: str, arguments: dict | None = None) -> dict:
    reply = call("tools/call", {"name": name, "arguments": arguments or {}})
    result = reply["result"]
    payload = json.loads(result["content"][0]["text"]) if not result["isError"] else None
    return {"result": result, "payload": payload, "text": result["content"][0]["text"]}


# ---- lifecycle ---------------------------------------------------------


def test_initialize_handshake(home):
    reply = call(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        },
    )
    result = reply["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "nodus"
    assert result["serverInfo"]["version"] == config.VERSION


def test_initialize_falls_back_to_a_supported_version(home):
    reply = call("initialize", {"protocolVersion": "2099-01-01"})
    assert reply["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION


def test_notifications_get_no_reply(home):
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert mcp.handle_message(initialized) is None
    assert mcp.handle_message({"jsonrpc": "2.0", "method": "unknown/notification"}) is None


def test_ping(home):
    assert call("ping")["result"] == {}


def test_unknown_method_is_a_jsonrpc_error(home):
    reply = call("resources/list")
    assert reply["error"]["code"] == -32601
    assert "resources/list" in reply["error"]["message"]


# ---- tools/list --------------------------------------------------------


def test_tools_list_returns_every_tool(home):
    reply = call("tools/list")
    tools = reply["result"]["tools"]
    assert {item["name"] for item in tools} == EXPECTED_TOOLS
    assert len(tools) == 7


def test_every_tool_has_an_object_input_schema(home):
    tools = call("tools/list")["result"]["tools"]
    for item in tools:
        schema = item["inputSchema"]
        assert item["description"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert set(schema["required"]) <= set(schema["properties"])
    required = {item["name"]: set(item["inputSchema"]["required"]) for item in tools}
    assert required["nodus_task_create"] == {"title", "description"}
    assert required["nodus_run"] == {"task_id"}
    assert required["nodus_run_status"] == {"run_id"}


# ---- tools/call --------------------------------------------------------


def test_task_create_writes_a_real_task(home):
    got = tool(
        "nodus_task_create",
        {
            "title": "Add MCP",
            "description": "Expose Nodus over stdio",
            "acceptance": "pytest -q is green",
        },
    )
    assert got["result"]["isError"] is False
    payload = got["payload"]
    task = db.get_task(payload["task_id"])
    assert task["title"] == "Add MCP"
    assert task["goal"] == "Expose Nodus over stdio"
    assert task["acceptance"] == "pytest -q is green"
    assert payload["status"] == "open"


def test_task_create_acceptance_is_optional(home):
    got = tool("nodus_task_create", {"title": "t", "description": "d"})
    assert got["result"]["isError"] is False
    assert db.get_task(got["payload"]["task_id"])["acceptance"] == ""


def test_missing_required_argument_is_a_tool_error(home):
    got = tool("nodus_task_create", {"description": "no title"})
    assert got["result"]["isError"] is True
    assert "title" in got["text"]


def test_wrong_argument_type_is_a_tool_error(home):
    got = tool("nodus_task_create", {"title": 7, "description": "d"})
    assert got["result"]["isError"] is True
    assert "字符串" in got["text"]


def test_unknown_argument_is_rejected(home):
    got = tool("nodus_task_create", {"title": "t", "description": "d", "goal": "typo"})
    assert got["result"]["isError"] is True
    assert "goal" in got["text"]


def test_unknown_tool_is_a_tool_error(home):
    got = tool("nodus_does_not_exist")
    assert got["result"]["isError"] is True
    assert "未知工具" in got["text"]
    assert "nodus_cost_today" in got["text"]


def test_exception_inside_a_tool_becomes_is_error(home, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("database exploded")

    monkeypatch.setattr(mcp.db, "create_task", boom)
    got = tool("nodus_task_create", {"title": "t", "description": "d"})
    assert got["result"]["isError"] is True
    assert "database exploded" in got["text"]
    # The session survives: the next request is still answered.
    assert call("ping")["result"] == {}


def test_run_refuses_a_missing_cwd(home, tmp_path):
    got = tool("nodus_run", {"task_id": "task-x", "cwd": str(tmp_path / "nope")})
    assert got["result"]["isError"] is True
    assert "cwd" in got["text"]


# ---- read tools --------------------------------------------------------


def _seed_run(agent: str, cost: float, verify: str = "none") -> dict:
    task = db.create_task(goal=f"task for {agent}", title=agent, acceptance="pytest -q")
    run = db.create_run(task["id"], agent, str(config.HOME), route=agent, provider="relay")
    db.update_run(
        run["id"],
        status="completed",
        ended_at=time.time(),
        usage_json=json.dumps(
            {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read": 30,
                "cache_write": 40,
                "cost": cost,
                "currency": "CNY",
                "cost_basis": "calibrated",
            }
        ),
    )
    if verify != "none":
        db.append_event(run["id"], 1, f"verify.{verify}", {})
    return run


def test_run_status_reports_cost_and_acceptance(home):
    run = _seed_run("claude", 0.012345, verify="green")
    got = tool("nodus_run_status", {"run_id": run["id"]})
    assert got["result"]["isError"] is False
    payload = got["payload"]
    assert payload["status"] == "completed"
    assert payload["verify"]["state"] == "ok"
    assert "测试通过" in payload["verify"]["meaning"]
    assert payload["usage"]["cost"] == pytest.approx(0.012345)
    assert payload["usage"]["currency"] == "CNY"
    assert payload["usage"]["cost_basis"] == "calibrated"
    assert payload["usage"]["output_tokens"] == 20


def test_run_status_unknown_id_is_a_tool_error(home):
    got = tool("nodus_run_status", {"run_id": "run-nope"})
    assert got["result"]["isError"] is True
    assert "run-nope" in got["text"]


def test_proposals_lists_only_open_ones(home):
    open_one = db.create_proposal(
        run_id="run-a",
        task_id="task-a",
        repo="C:/repo",
        branch="nodus/run-a",
        worktree="C:/repo/.nodus-worktrees/run-a",
        base="abc123",
        patch_path="C:/home/runs/run-a/proposal.patch",
        files=[{"path": "a.py", "added": 2, "removed": 1}],
        stat="1 file changed",
    )
    closed = db.create_proposal(
        run_id="run-b",
        task_id="task-b",
        repo="C:/repo",
        branch="nodus/run-b",
        worktree="C:/repo/.nodus-worktrees/run-b",
        base="abc123",
        patch_path="C:/home/runs/run-b/proposal.patch",
        files=[],
        stat="0 files changed",
    )
    db.close_proposal(closed["id"], "merged")

    got = tool("nodus_proposals")
    assert got["result"]["isError"] is False
    assert got["payload"]["count"] == 1
    item = got["payload"]["proposals"][0]
    assert item["proposal_id"] == open_one["id"]
    assert item["branch"] == "nodus/run-a"
    assert item["files"] == [{"path": "a.py", "added": 2, "removed": 1}]


def test_cost_today_groups_by_agent(home):
    _seed_run("claude", 0.5)
    _seed_run("claude", 0.25)
    _seed_run("codex", 0.1)

    got = tool("nodus_cost_today")
    assert got["result"]["isError"] is False
    payload = got["payload"]
    assert payload["runs"] == 3
    assert payload["priced"] == 3
    assert payload["totals"] == {"CNY": 0.85}
    by_agent = {row["agent"]: row for row in payload["by_agent"]}
    assert by_agent["claude"]["runs"] == 2
    assert by_agent["claude"]["costs"] == {"CNY": 0.75}
    assert by_agent["codex"]["costs"] == {"CNY": 0.1}


# ---- stdio framing -----------------------------------------------------


def test_stdio_session_is_newline_delimited(home):
    # MCP's stdio transport is one JSON object per line; Content-Length framing
    # (LSP) is wrong here. This drives the real loop with a real pipe shape.
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "nodus_cost_today", "arguments": {}}},
    ]
    raw = b"".join(json.dumps(item).encode("utf-8") + b"\n" for item in requests)
    out = io.BytesIO()
    assert mcp.serve(stdin=io.BytesIO(raw), stdout=out) == 0

    lines = out.getvalue().splitlines()
    assert len(lines) == 3, "the notification must not produce a response line"
    assert not any(b"Content-Length" in line for line in lines)
    replies = [json.loads(line) for line in lines]
    assert [reply["id"] for reply in replies] == [1, 2, 3]
    assert {item["name"] for item in replies[1]["result"]["tools"]} == EXPECTED_TOOLS
    assert replies[2]["result"]["isError"] is False


def test_parse_error_does_not_end_the_session(home):
    raw = b"not json\n" + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}).encode() + b"\n"
    out = io.BytesIO()
    assert mcp.serve(stdin=io.BytesIO(raw), stdout=out) == 0
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(replies) == 2
    assert replies[0]["error"]["code"] == -32700
    assert replies[1]["result"] == {}
