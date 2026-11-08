> 产品名 Sodus，实现名 nodus。

# MCP over stdio

Sodus 的第三个接口（另两个是 HTTP/OpenAPI 和 CLI）。实现的是 MCP **2024-11-05**
的 stdio transport：**一行一个 JSON-RPC 2.0 消息**，不用 LSP 的 `Content-Length`
分帧。规范原文：*"Messages are delimited by newlines, and MUST NOT contain
embedded newlines."*（[Transports](https://modelcontextprotocol.io/specification/2024-11-05/basic/transports)）

入口：

```powershell
python -m nodus mcp        # 或装好之后：nodus mcp
```

进程只从 stdin 读协议消息、往 stdout 写协议消息；日志一律走 stderr。所有工具都通过 daemon HTTP 转发；请先运行 `sodus up`。daemon 是唯一的任务和存储所有者，MCP 进程退出后 run 仍可轮询。

## 工具

| 工具 | 参数 | 返回 |
|---|---|---|
| `sodus_ask`（别名 `nodus_ask`） | `request_id`、`text`（必填），`cwd`、`route`、`follow_up_of`（可选） | `POST /ask` 的 JSON（新建 202，幂等重试 200） |
| `sodus_ask_status`（别名 `nodus_ask_status`） | `id` | `GET /ask/{id}` 的 JSON；过期 410 错误原文 |
| `nodus_task_create` | `title`、`description`、`acceptance`（可选） | `task_id` + 任务记录 |
| `nodus_run` | `task_id`、`route`（默认 claude）、`cwd`（默认进程 cwd）、`wait`（默认 false） | `run_id`、状态；`wait=true` 阻塞到结束 |
| `nodus_run_status` | `run_id` | 状态、成本/币种/计价依据、`verify`（ok/red/none） |
| `nodus_proposals` | 无 | 未处理的提案（分支、stat、文件） |
| `nodus_cost_today` | 无 | 今日（本地 0 点起）按 agent 分组的成本 |
| `nodus_memory_remember` | `text`、`source`（可选） | daemon 写入项目 AGENTS.md 的事实，重复不追加 |
| `nodus_memory_candidates` | `project`、`limit`（1–500）、`conflicts_only`（均可选） | 只读候选事实、来源与冲突；采纳仍由人决定 |

所有工具失败都返回 `isError: true` + 人类可读消息，不会让进程退出。协议层错误
（坏 JSON、未知 method）仍然是 JSON-RPC error。

`wait=true` 最多等 `NODUS_MCP_WAIT` 秒（默认 600），超时返回当前状态并带
`wait_timed_out: true` —— 别把"等到了超时"读成"跑完了"。

## 一条命令安装宿主配置

```powershell
sodus mcp install --host codex,claude,dsh
sodus mcp install --check
sodus mcp install --uninstall --host claude
```

安装会备份配置为 `.bak-<timestamp>`，重复执行不会产生 diff；`--dry-run` 只预览。DSH 默认读取 `DSH_HOME`，也可用 `--dsh-home` 指定；未设置时提示“跳过 DSH”。DSH YAML 编辑需要 PyYAML（`python -m pip install PyYAML`），缺少时明确报错且不写配置。

默认检查全部宿主；`--check` 输出 `configured/missing/drift`，仅全齐时退出 0，否则退出 1。Codex 写 `~/.codex/config.toml` 的 `[mcp_servers.sodus]`，Claude 写 `~/.claude.json` 的 `mcpServers.sodus`，DSH 写 `$DSH_HOME/cordis.patch.yml` 的 insert 项；启动命令为绝对路径 `A:\Anaconda\python.exe`，参数为 `["-m", "sodus", "mcp"]`。请先安装本项目（`python -m pip install -e .`），再重启宿主。

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
`*_KEY` / `*_TOKEN` / `*_SECRET` 环境变量，但 Sodus 不需要任何密钥。

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

- 0.7 已修复旧版双所有者问题：所有工具依赖 daemon（`sodus up`），MCP 退出不会中断 daemon 拥有的 run。
- stdio 是本地子进程协议，没有鉴权；不要把它包成网络端口暴露出去。
- `cwd` 必须是已存在的目录，不会替你创建。
- 工具提供查询、问答和建任务/起 run；**合入/丢弃提案仍然只在 CLI**（`nodus merge` /
  `nodus reject`），避免让模型自己批准自己的改动。


## Ask 与递归保护

`sodus_ask` 设置 `origin=mcp`，将进程 `NODUS_DEPTH`（默认 0）作为
`X-Sodus-Depth` 转发；使用 CLI 同一 localhost 地址与 Bearer token。
`NODUS_ROLE=worker` 或 `NODUS_DEPTH>=1` 时，`sodus_ask`、`nodus_ask`、
`nodus_run`、`nodus_task_create` 在进程内返回 `isError=true`、`recursion_denied`，
不会创建任务或发送 HTTP。状态查询仍可使用；不提供会话推送或写回。
HTTP 仍兜底拒绝 depth>=1；直接 HTTP 故意省略深度头的 worker 是已知限制。

`sodus status` 的 `asks active` 行统计当前 workspace 未过期（24 小时 TTL）的 asks：
open=queued，running=running+stalled，done=answered+promoted，failed=failed。
它调用鉴权的 `GET /asks?workspace=<workspace_id>`；省略 workspace 则用 daemon 的 workspace，
响应只含 id/status/created_at/expires_at，按创建时间倒序，不含问题或答案。
ask 运行归 daemon 所有，MCP 退出后仍可按 id 轮询；所有 MCP run 工具也由同一 daemon 执行。


Memory review (0.6) uses the dashboard or HTTP `GET /memory/candidates` and
`POST /memory/candidates/{id}` with `{action, version, text?}`. Actions are
`adopt`, `reject`, `edit`, `retire`; stale versions return 409. There is no new MCP
review tool. Historical memory rollback is local CLI: `sodus memory rollback --to V`.
