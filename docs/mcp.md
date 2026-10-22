# MCP over stdio

Nodus 的第三个接口（另两个是 HTTP/OpenAPI 和 CLI）。实现的是 MCP **2024-11-05**
的 stdio transport：**一行一个 JSON-RPC 2.0 消息**，不用 LSP 的 `Content-Length`
分帧。规范原文：*"Messages are delimited by newlines, and MUST NOT contain
embedded newlines."*（[Transports](https://modelcontextprotocol.io/specification/2024-11-05/basic/transports)）

入口：

```powershell
python -m nodus mcp        # 或装好之后：nodus mcp
```

进程只从 stdin 读协议消息、往 stdout 写协议消息；日志一律走 stderr。它自己开
SQLite 连接、自己起 worker，**不需要 `nodus up`**，也不经过 HTTP。

## 工具

| 工具 | 参数 | 返回 |
|---|---|---|
| `nodus_task_create` | `title`、`description`、`acceptance`（可选） | `task_id` + 任务记录 |
| `nodus_run` | `task_id`、`route`（默认 claude）、`cwd`（默认进程 cwd）、`wait`（默认 false） | `run_id`、状态；`wait=true` 阻塞到结束 |
| `nodus_run_status` | `run_id` | 状态、成本/币种/计价依据、`verify`（ok/red/none） |
| `nodus_proposals` | 无 | 未处理的提案（分支、stat、文件） |
| `nodus_cost_today` | 无 | 今日（本地 0 点起）按 agent 分组的成本 |

所有工具失败都返回 `isError: true` + 人类可读消息，不会让进程退出。协议层错误
（坏 JSON、未知 method）仍然是 JSON-RPC error。

`wait=true` 最多等 `NODUS_MCP_WAIT` 秒（默认 600），超时返回当前状态并带
`wait_timed_out: true` —— 别把"等到了超时"读成"跑完了"。

## 接进 Claude Code

项目根目录的 `.mcp.json`：

```json
{
  "mcpServers": {
    "nodus": {
      "type": "stdio",
      "command": "A:\\Anaconda\\python.exe",
      "args": ["-m", "nodus", "mcp"],
      "env": {
        "PYTHONPATH": "A:\\VScode\\Code\\Projects\\nodus"
      }
    }
  }
}
```

用 `pip install -e .` 装过的话，`command` 可以直接写 `nodus`、`args` 写 `["mcp"]`，
`PYTHONPATH` 也可以省掉。GUI 客户端的 PATH 往往和终端不同，文档里用绝对路径更稳。

## 接进 DSH

在 `A:\dsh\home\cordis.patch.yml` 里追加（注意必须是 `insert:` 数组，否则会被当成
"修改一个不存在的条目"而静默跳过）：

```yaml
- insert:
    - name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: nodus
        transport: stdio
        command: 'A:\Anaconda\python.exe'
        args: ['-m', 'nodus', 'mcp']
        env:
          PYTHONPATH: 'A:\VScode\Code\Projects\nodus'
        toolCallTimeoutMs: 600000
```

重启 DSH 后工具会以 `mcp__nodus__nodus_task_create` 这样的名字出现。
`toolCallTimeoutMs` 要覆盖 `wait=true` 的等待时间；DSH 启动子进程时会清掉
`*_KEY` / `*_TOKEN` / `*_SECRET` 环境变量，但 Nodus 不需要任何密钥。

## 手动验证

```powershell
@'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"nodus_cost_today","arguments":{}}}
'@ | python -m nodus mcp
```

stdout 会是三行 JSON（通知没有回复）；stderr 会有 `nodus mcp: ready ...`。

## 边界

- **和 daemon 二选一。** MCP 进程自己起 run；daemon 启动时的对账会把"不是自己起的"
  在线 run 当孤儿标失败。别在 MCP 有 run 在跑时开 `nodus up`。
- **MCP 进程退出 = 它起的 run 失去所有者。** 长任务要么用 `wait=true`，要么保持
  客户端会话开着，之后用 `nodus_run_status` 轮询。真断了用 `nodus reconcile` 收尾。
- stdio 是本地子进程协议，没有鉴权；不要把它包成网络端口暴露出去。
- `cwd` 必须是已存在的目录，不会替你创建。
- 这 5 个工具是只读 + 建任务/起 run；**合入/丢弃提案仍然只在 CLI**（`nodus merge` /
  `nodus reject`），避免让模型自己批准自己的改动。
