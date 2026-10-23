# MCP 工具（stdio）

```powershell
nodus mcp            # 装过
python -m nodus mcp  # 没装
```

协议 MCP **2024-11-05**，stdio transport：**一行一个 JSON-RPC 2.0 消息**，不用
`Content-Length` 分帧。进程只从 stdin 读、往 stdout 写协议消息，日志全走 stderr。
它自己开 SQLite、自己起 worker，**不需要 `nodus up`**，也不经过 HTTP。

## 七个工具

| 工具 | 参数 | 返回 |
|---|---|---|
| `nodus_task_create` | `title`*、`description`*、`acceptance` | task 记录（含 `id`） |
| `nodus_run` | `task_id`*、`route`（默认 claude）、`cwd`（默认进程 cwd）、`wait`（默认 false） | `run_id` + 当前状态 |
| `nodus_run_status` | `run_id`* | 状态、usage、`verify.state`、handoff 路径 |
| `nodus_proposals` | 无 | 未处理提案（id、分支、stat、文件） |
| `nodus_cost_today` | 无 | 今日按 agent 分组的成本，含 `budget` |
| `nodus_memory_remember` | `text`*、`scope`（private/project/public，默认 project）、`key`、`tags`、`agent`、`run_id` | 记忆记录 |
| `nodus_memory_recall` | `query`、`scope`、`agent`、`limit`（1–50，默认 5） | `{count, memories}` |

`*` = 必填。`nodus_run` 的 `cwd` 必须是已存在目录，不会替你建。

## 真实的 tools/list 摘要

```json
{"name":"nodus_task_create","required":["title","description"]}
{"name":"nodus_run","required":["task_id"],"defaults":{"route":"claude","wait":false}}
{"name":"nodus_run_status","required":["run_id"]}
{"name":"nodus_proposals","required":[]}
{"name":"nodus_cost_today","required":[]}
{"name":"nodus_memory_remember","required":["text"]}
{"name":"nodus_memory_recall","required":[]}
```

server `instructions` 原文：

> Nodus 把任务交给多个 agent，并用可执行证据验收。先 nodus_task_create，再
> nodus_run，用 nodus_run_status 看成本与 verify；nodus_proposals 列待人工处理的
> 提案，nodus_cost_today 看今日花费。

## `nodus_run_status` 实跑返回

```json
{
  "run_id": "run-138753b90fa6",
  "task_id": "task-7c7dca2a6b92",
  "status": "completed",
  "agent": "dsh",
  "route": "dsh",
  "provider": "deepseek",
  "duration_s": 44.09,
  "error": "",
  "usage": {"input_tokens": 20934, "output_tokens": 891, "cache_read": 132992,
            "cost": 0.0, "currency": "CNY", "cost_basis": "metered"},
  "verify": {"state": "ok", "meaning": "测试通过（verify.green）"},
  "handoff": "C:\\Users\\35336\\.nodus\\runs\\run-138753b90fa6\\handoff.md"
}
```

`verify.state` 只有三种：`ok`（测试绿）、`red`（测试红）、`none`（**没跑过测试，
completed 只代表进程退出码 0**）。判读见主文件。

## `nodus_proposals` 实跑返回

```json
{"count": 1, "proposals": [{"proposal_id": "prop-086b8942c205",
  "run_id": "run-d95b57c4252a", "branch": "nodus/run-d95b57c4252a",
  "stat": "1 file(s), +1 −0", "files": [{"path": "ui-demo.txt", "added": 1, "removed": 0}]}]}
```

## `nodus_cost_today` 实跑返回

```json
{"day": "2026-10-23", "runs": 16, "priced": 11, "unpriced": 5,
 "totals": {"CNY": 0.227974},
 "by_agent": [{"agent": "dsh", "costs": {"CNY": 0.18}, "runs": 3, "priced": 3}],
 "basis": {"metered": 11}}
```

窗口从本地 0 点起；不同币种分开汇总，**不折算汇率**。

## 手动验证

```powershell
@'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"nodus_cost_today","arguments":{}}}
'@ | python -m nodus mcp
```

stdout 出三行 JSON（通知没有回复），stderr 有一句 `nodus mcp: ready (0.4.0, protocol 2024-11-05)`。

## 边界，必须知道

- **MCP 进程和 daemon 二选一。** MCP 起的 run 归 MCP 进程所有；daemon 启动时的对账
  会把"不是自己起的"在线 run 当孤儿判失败。别在 MCP 有 run 在跑的时候 `nodus up`。
- **MCP 进程退出 = 它起的 run 失去所有者。** 长任务要么 `wait=true`，要么保持客户端
  会话开着、之后用 `nodus_run_status` 轮询。真断了用 `nodus reconcile` 收尾。
- **`wait=true` 等不到头不算跑完。** 最多等 `NODUS_MCP_WAIT` 秒（默认 600），超时返回
  当前状态并带 `wait_timed_out: true`。
- **提案的合入/丢弃没有 MCP 工具。** 只在 CLI（`nodus merge` / `nodus reject`），这是
  故意的：不让模型批准自己的改动。
- **stdio 没有鉴权。** 它是本地子进程协议，不要包成网络端口暴露。
- 工具失败返回 `isError: true` + 人话消息，进程不会退出。协议层错误（坏 JSON、未知
  method）才走 JSON-RPC error。
