"""MCP over stdio: the third interface next to HTTP/OpenAPI and the CLI.

Transport is the 2024-11-05 stdio one: **one JSON-RPC message per line**, no
`Content-Length` headers. MCP borrowed JSON-RPC from LSP but not its framing;
the spec says messages "are delimited by newlines, and MUST NOT contain
embedded newlines". Anything written to stdout that is not a protocol message
breaks the client, so stray prints are redirected to stderr while serving.

All tools forward to the daemon HTTP API; the daemon owns runs and storage.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

from . import config
from .cli import client
import time
from urllib.parse import quote, urlencode

PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_VERSIONS = (PROTOCOL_VERSION,)
#: What clients see as serverInfo.name. Every tool name shares this prefix.
SERVER_NAME = "sodus"

#: `wait=true` blocks until the run leaves queued/running. A client-side tool
#: timeout is usually shorter, so this is configurable and bounded.
WAIT_TIMEOUT = float(os.environ.get("SODUS_MCP_WAIT", "600"))


class ToolError(ValueError):
    """A caller mistake or a tool failure: reported as `isError`, never raised
    out of the server loop."""


def _schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


TOOLS: list[dict] = [
    {
        "name": "sodus_task_create",
        "description": (
            "在 Sodus 建一个任务：目标（description）+ 可选验收标准（acceptance）。"
            "返回 task_id，随后用 sodus_run 派给某个 agent。"
        ),
        "inputSchema": _schema(
            {
                "title": {"type": "string", "description": "短标题，列表里显示这一行"},
                "description": {"type": "string", "description": "任务目标，交给 agent 的原文"},
                "acceptance": {
                    "type": "string",
                    "description": "验收标准；不传则由执行者自行判断",
                },
            },
            ("title", "description"),
        ),
    },
    {
        "name": "sodus_run",
        "description": (
            "为一个任务起一次执行，返回 run_id 与当前状态。默认立即返回；"
            "wait=true 会阻塞到这次执行结束（默认最多 600 秒，可由进程环境变量 "
            "SODUS_MCP_WAIT 调整）。cwd 必须是已存在的目录，默认当前进程的工作目录。"
        ),
        "inputSchema": _schema(
            {
                "task_id": {"type": "string", "description": "sodus_task_create 返回的 id"},
                "route": {
                    "type": "string",
                    "description": "路由名（claude / codex / dsh / minimax ...），默认 claude",
                },
                "cwd": {
                    "type": "string",
                    "description": "执行目录，必须是已存在目录；默认当前工作目录",
                },
                "wait": {
                    "type": "boolean",
                    "description": "true = 等执行结束再返回（默认 false）",
                    "default": False,
                },
            },
            ("task_id",),
        ),
    },
    {
        "name": "sodus_run_status",
        "description": (
            "查一次执行的状态、成本与验收结果。verify 只有三种：ok（测试绿）、"
            "red（测试红）、none（没跑过测试，completed 只代表进程退出码 0）。"
        ),
        "inputSchema": _schema(
            {"run_id": {"type": "string", "description": "sodus_run 返回的 run id"}},
            ("run_id",),
        ),
    },
    {
        "name": "sodus_proposals",
        "description": (
            "列出待人工处理的提案：改动在独立 git 分支/worktree 上，主工作区没有被改。"
            "合入或丢弃由人在 CLI 里做（sodus merge / sodus reject）。"
        ),
        "inputSchema": _schema({}),
    },
    {
        "name": "sodus_cost_today",
        "description": (
            "今天的成本，按 agent 分组。窗口从本地时区 0 点开始；"
            "金额按记账时的币种分开汇总（不跨币种折算）。"
        ),
        "inputSchema": _schema({}),
    },
    {
        "name": "sodus_memory_remember",
        "description": (
            "把一条确认过的事实写进当前项目 AGENTS.md 的「已验证事实」小节，"
            "带日期和来源。不是另开一个存储：写的就是人和其它 harness 都读的那个文件，"
            "重复写同一条不会追加第二次。项目目录按 SODUS_PROJECT / config 的 "
            "project_dir / 当前工作目录的顺序确定。"
        ),
        "inputSchema": _schema(
            {
                "text": {
                    "type": "string",
                    "description": "一条自包含的事实，例如「构建脚本统一用 pnpm」",
                },
                "source": {
                    "type": "string",
                    "description": "来源标记，默认 agent；会写进括号里",
                },
            },
            ("text",),
        ),
    },
    {
        "name": "sodus_memory_candidates",
        "description": (
            "只读：列出各 harness 记忆文件（CLAUDE.md / AGENTS.md）里抽出来的候选事实，"
            "带来源路径、是否与另一条冲突、是否已采纳。"
            "采纳是人做的（sodus debug memory adopt），agent 不能替人决定。"
        ),
        "inputSchema": _schema(
            {
                "project": {
                    "type": "string",
                    "description": "项目目录；不传则用 SODUS_PROJECT / project_dir / cwd",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 50,
                },
                "conflicts_only": {
                    "type": "boolean",
                    "description": "只看互相冲突的候选",
                    "default": False,
                },
            }
        ),
    },
]


for _name, _description, _properties, _required in (
    ("sodus_ask", "异步提问；返回 ask id，用 sodus_ask_status 轮询。需要 HTTP daemon。",
     {key: {"type": "string"} for key in ("request_id", "text", "cwd", "route", "follow_up_of")},
     ("request_id", "text")),
    ("sodus_ask_status", "查询 ask 结果；过期返回 410 错误。", {"id": {"type": "string"}}, ("id",)),
):
    TOOLS.append({"name": _name, "description": _description,
                  "inputSchema": _schema(_properties, _required)})


# ---- argument parsing --------------------------------------------------

for _name in ('show', 'list', 'check'):
    TOOLS.append({'name': 'sodus_handoff_' + _name,
                  'description': '只读项目接力控制台：' + _name + '；不修改文件。',
                  'annotations': {'readOnlyHint': True},
                  'inputSchema': _schema({} if _name == 'list' else {'cwd': {'type': 'string'}})})

TOOLS.append({
    'name': 'sodus_handoff_decisions',
    'description': (
        '只读项目级决策收件箱：列出当前项目（默认调用方 cwd）或全部已知项目（all=true）'
        '在 HANDOFF.md 中等待拍板的 D-xxx 决策；写入仍由本地 CLI sodus handoff decide-* 完成。'
    ),
    'annotations': {'readOnlyHint': True},
    'inputSchema': _schema({
        'cwd': {'type': 'string', 'description': '项目目录；all=false 且不传时默认调用方当前目录'},
        'all': {'type': 'boolean', 'description': 'true = 聚合全部已知项目的待拍板决策', 'default': False},
        'status': {'type': 'string', 'enum': ['open', 'deferred', 'all'], 'default': 'open'},
    }),
})


def _arguments(value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ToolError("arguments 必须是一个对象")
    return dict(value)


def _string(args: dict, name: str, *, required: bool = True, default: str = "") -> str:
    value = args.pop(name, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ToolError(f"参数 `{name}` 必须是字符串")
    value = value.strip()
    if required and not value:
        raise ToolError(f"缺少必填参数 `{name}`")
    return value


def _boolean(args: dict, name: str, default: bool = False) -> bool:
    value = args.pop(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    if value in (0, 1):
        return bool(value)
    raise ToolError(f"参数 `{name}` 必须是布尔值")


def _integer(args: dict, name: str, default: int = 0) -> int:
    value = args.pop(name, default)
    if value is None:
        return default
    if isinstance(value, bool):
        raise ToolError(f"参数 `{name}` 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ToolError(f"参数 `{name}` 必须是整数") from None


def _no_extra(args: dict) -> None:
    if args:
        raise ToolError("未知参数：" + ", ".join(sorted(args)))


# ---- tools -------------------------------------------------------------


def _task_create(args: dict) -> dict:
    title = _string(args, "title")
    description = _string(args, "description")
    acceptance = _string(args, "acceptance", required=False, default="")
    _no_extra(args)
    task = _http("POST", "/tasks", {"goal": description, "acceptance": acceptance, "title": title})
    return {"task_id": task["id"], "description": task.get("goal", description), **task}


def _run(args: dict) -> dict:
    task_id = _string(args, "task_id")
    route = _string(args, "route", required=False, default="claude") or "claude"
    raw_cwd = _string(args, "cwd", required=False, default="")
    wait = _boolean(args, "wait")
    _no_extra(args)

    cwd = Path(raw_cwd).expanduser() if raw_cwd else Path.cwd()
    if not cwd.is_dir():
        raise ToolError(f"cwd 不是已存在的目录：{cwd}")

    run = _http("POST", "/runs", {"task_id": task_id, "route": route, "cwd": str(cwd.resolve())})
    deadline = time.monotonic() + max(0, WAIT_TIMEOUT)
    payload = _status_payload(run["id"])
    while wait and payload["status"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        payload = _status_payload(run["id"])
    payload["waited"] = wait
    if wait:
        payload["wait_timed_out"] = payload["status"] in ("queued", "running")
    return payload


def _run_status(args: dict) -> dict:
    run_id = _string(args, "run_id")
    _no_extra(args)
    return _status_payload(run_id)


def _http(method, path, body=None, **kwargs):
    try:
        if body is None:
            return client.call(method, path, **kwargs)
        return client.call(method, path, body, **kwargs)
    except (RuntimeError, OSError) as exc:
        if "unreachable" in str(exc) or isinstance(exc, OSError):
            raise ToolError(f"daemon 不可用，请先运行 sodus up：{exc}") from None
        raise


def _status_payload(run_id: str) -> dict:
    run = _http("GET", "/runs/" + quote(run_id, safe=""))
    if not run:
        raise ToolError(f"没有这个 run：{run_id}")
    return run.get("mcp", run)


def _proposals(args: dict) -> dict:
    _no_extra(args)
    rows = _http("GET", "/proposals")
    return {"count": len(rows), "proposals": [dict(row, proposal_id=row["id"], patch=row.get("patch_path", "")) for row in rows]}


def _cost_today(args: dict) -> dict:
    _no_extra(args)
    return _http("GET", "/cost/today")


def _memory_remember(args: dict) -> dict:
    text = _string(args, "text")
    source = _string(args, "source", required=False, default="agent") or "agent"
    _no_extra(args)
    return _http("POST", "/memory/remember", {"text": text, "source": source})


def _memory_candidates(args: dict) -> dict:
    project = _string(args, "project", required=False)
    limit = _integer(args, "limit", default=50)
    conflicts_only = _boolean(args, "conflicts_only")
    _no_extra(args)
    if not 1 <= limit <= 500:
        raise ToolError("limit 必须在 1 到 500 之间")
    return _http("GET", "/memory/harvest?" + urlencode(dict(project=project, limit=limit, conflicts_only=str(conflicts_only).lower())))


def _ask(args: dict) -> dict:
    body = {key: _string(args, key) for key in ("request_id", "text")}
    for key in ("cwd", "route", "follow_up_of"):
        if key in args:
            body[key] = _string(args, key, required=False)
    _no_extra(args)
    if "cwd" in body and body["cwd"]:
        raw = Path(body["cwd"]).expanduser()
        if not raw.is_absolute():
            body["cwd"] = str(raw.resolve())
    elif "cwd" in body:
        body.pop("cwd", None)
    body["origin"] = "mcp"
    return _http("POST", "/ask", body,
                       headers={"X-Sodus-Depth": os.environ.get("SODUS_DEPTH", "0")})


def _ask_status(args: dict) -> dict:
    from urllib.parse import quote
    from .cli import client
    aid = _string(args, "id")
    _no_extra(args)
    return _http("GET", "/ask/" + quote(aid, safe=""))


def _deny_worker() -> None:
    if os.environ.get("SODUS_ROLE") == "worker":
        raise ToolError("recursion_denied")
    try:
        depth = int(os.environ.get("SODUS_DEPTH", "0"))
    except ValueError:
        raise ToolError("invalid_depth") from None
    if depth >= 1:
        raise ToolError("recursion_denied")


_HANDLERS: dict[str, Callable[[dict], dict]] = {
    "sodus_ask": _ask,
    "sodus_ask_status": _ask_status,
    "sodus_task_create": _task_create,
    "sodus_run": _run,
    "sodus_run_status": _run_status,
    "sodus_proposals": _proposals,
    "sodus_cost_today": _cost_today,
    "sodus_memory_remember": _memory_remember,
    "sodus_memory_candidates": _memory_candidates,
}


def _handoff_read(args, action):
    cwd = _string(args, 'cwd', required=False) if action != 'list' else ''
    _no_extra(args)
    path = '/handoffs' if action == 'list' else '/handoff' + ('/check' if action == 'check' else '')
    return _http('GET', path + ('?' + urlencode({'cwd': cwd}) if cwd else ''))


def _handoff_decisions(args: dict) -> dict:
    all_projects = _boolean(args, 'all', default=False)
    status = _string(args, 'status', required=False, default='open') or 'open'
    cwd = _string(args, 'cwd', required=False)
    _no_extra(args)
    if status not in ('open', 'deferred', 'all'):
        raise ToolError('status 必须是 open、deferred 或 all')
    params: dict[str, str] = {'status': status}
    if all_projects:
        params['all'] = '1'
    else:
        resolved = Path(cwd or Path.cwd()).expanduser().resolve()
        if not resolved.is_dir():
            raise ToolError(f'cwd 不是已存在的目录：{cwd}')
        params['cwd'] = str(resolved)
    return _http('GET', '/handoff/decisions?' + urlencode(params))


for _action in ('show', 'list', 'check'):
    _HANDLERS['sodus_handoff_' + _action] = lambda args, action=_action: _handoff_read(args, action)
_HANDLERS['sodus_handoff_decisions'] = _handoff_decisions


# ---- MCP result helpers ------------------------------------------------


def _text_result(payload: Any) -> dict:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _error_result(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def call_tool(name: Any, arguments: Any = None) -> dict:
    """Run one tool and always return a tool result, never raise.

    MCP distinguishes protocol errors from tool failures; per Sodus' contract
    every failure here is a tool result with `isError: true` plus a readable
    message, so a bad argument cannot take the stdio session down.
    """
    if not isinstance(name, str) or not name:
        return _error_result("tools/call 缺少工具名")
    handler = _HANDLERS.get(name)
    if handler is None:
        known = ", ".join(sorted(_HANDLERS))
        return _error_result(f"未知工具：{name}。可用：{known}")
    try:
        if name in {"sodus_ask", "sodus_run", "sodus_task_create"}:
            _deny_worker()
        return _text_result(handler(_arguments(arguments)))
    except (ToolError, RuntimeError) as exc:
        return _error_result(str(exc))
    except Exception as exc:  # noqa: BLE001 - one bad tool must not end the server
        _log(f"{name} 执行异常：{type(exc).__name__}: {exc}")
        traceback.print_exc(file=sys.stderr)
        return _error_result(f"{name} 执行失败：{type(exc).__name__}: {exc}")


# ---- JSON-RPC ----------------------------------------------------------


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _initialize(params: dict) -> dict:
    requested = params.get("protocolVersion")
    # A server answers with a version it supports. Echoing an unknown one would
    # claim a capability we never tested against this implementation.
    version = requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION
    client = params.get("clientInfo")
    if isinstance(client, dict) and client.get("name"):
        _log(f"client {client.get('name')} {client.get('version') or ''}".rstrip())
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": config.VERSION},
        "instructions": (
            "Sodus 把任务交给多个 agent，并用可执行证据验收。"
            "用 sodus_ask 异步提问、sodus_ask_status 轮询；"
            "先 sodus_task_create，再 sodus_run，用 sodus_run_status 看成本与 verify；"
            "sodus_proposals 列待人工处理的提案，sodus_cost_today 看今日花费。"
        ),
    }


def _tools_call(params: dict) -> dict:
    name = params.get("name")
    arguments = params.get("arguments")
    return call_tool(name, {} if arguments is None else arguments)


def handle_message(message: Any) -> dict | None:
    """One decoded JSON-RPC message in, one response out (None for notifications)."""
    if not isinstance(message, dict):
        return _error(None, -32600, "invalid request: 必须是 JSON 对象")
    if message.get("jsonrpc") != "2.0":
        return _error(message.get("id"), -32600, "invalid request: 缺少 jsonrpc=2.0")
    method = message.get("method")
    if not isinstance(method, str) or not method:
        # A response from the client is not something this server initiates,
        # so there is nothing to answer. Notifications are the same: no reply.
        return None
    request_id = message.get("id")
    is_notification = "id" not in message
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        if is_notification:
            return None
        return _error(request_id, -32602, "invalid params: params 必须是对象")

    if method == "initialize":
        result = _initialize(params)
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = _tools_call(params)
    else:
        if is_notification:
            return None
        return _error(request_id, -32601, f"method not found: {method}")

    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


# ---- stdio transport ---------------------------------------------------


def _log(message: str) -> None:
    try:
        print(f"sodus mcp: {message}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _send(writer, message: Any) -> bool:
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        writer.write(data + b"\n")
        writer.flush()
    except (BrokenPipeError, OSError, TypeError, ValueError):
        return False
    return True


def serve(stdin=None, stdout=None) -> int:
    """Read newline-delimited JSON-RPC on stdin, write replies on stdout.

    `stdin` / `stdout` are injectable for tests; production uses the binary
    buffers so the framing is bytes, not the locale's text encoding.
    """
    reader = stdin if stdin is not None else getattr(sys.stdin, "buffer", sys.stdin)
    writer = stdout if stdout is not None else getattr(sys.stdout, "buffer", sys.stdout)
    if stdout is None:
        # Nothing except protocol messages may reach the real stdout. A print
        # from a library would otherwise be parsed as a JSON-RPC message.
        sys.stdout = sys.stderr
    _log(f"ready ({config.VERSION}, protocol {PROTOCOL_VERSION})")

    for raw in reader:
        line = raw.strip()
        if not line:
            continue
        if isinstance(line, str):
            line = line.encode("utf-8")
        if line.startswith(b"\xef\xbb\xbf"):
            line = line[3:]
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if not _send(writer, _error(None, -32700, f"parse error: {exc}")):
                break
            continue

        try:
            if isinstance(message, list):
                # JSON-RPC batching is not part of MCP 2024-11-05, but a list
                # is cheap to answer correctly instead of failing outright.
                if not message:
                    reply = _error(None, -32600, "invalid request: 空 batch")
                else:
                    replies = [r for r in (handle_message(m) for m in message) if r is not None]
                    reply = replies or None
            else:
                reply = handle_message(message)
        except Exception as exc:  # noqa: BLE001 - the loop must survive a bug
            _log(f"handler crashed: {type(exc).__name__}: {exc}")
            traceback.print_exc(file=sys.stderr)
            reply = _error(None, -32603, "internal error")

        if reply is not None and not _send(writer, reply):
            break
    return 0
