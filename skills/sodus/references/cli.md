# Sodus CLI 全量命令表

`sodus --version` → `0.4.0`。所有输出都来自本机实跑，不是从 `--help` 抄的推测。

## 全局

```text
sodus serve          前台跑 HTTP daemon
sodus mcp            stdio MCP server（不需要 daemon）
sodus up [--port N] [--wait S]     后台起 daemon，活着离开这个 shell
sodus down          停掉 state 文件指向的那个 daemon
```

`up` 会比对 state 目录指纹（`config.home_id()`）。同一个端口上如果有另一个 `SODUS_HOME` 的
daemon，它会拒绝说"已经是别人的"。

## 环境与诊断

| 命令 | 实测输出要点 |
|---|---|
| `sodus doctor` | 8 行 PASS：home / token(48 chars) / database(tasks=76 runs=117) / agents / sessions / secrets / write locks / disk |
| `sodus status` | provider 探测时间、`today runs=14 in=172502 out=2229`、jev 调用数、relay 余额 |
| `sodus agents` | 5 个 agent：claude codex dsh opencode-glm opencode-gemini |
| `sodus routes list` | 6 条路由：claude codex dsh minimax opencode-gemini opencode-glm |
| `sodus topology` | agents × providers × routes 一张表 |
| `sodus providers list\|show\|add\|remove\|probe` | 端点管理 |
| `sodus profiles list\|show\|use\|new\|rm` | 配置快照 |
| `sodus balance` | 只报中转站余额 |
| `sodus cost [--runs N]` | 实测单价 + 标定与账单漂移 |
| `sodus usage` | 每个 agent 的 in/out/cache/failed/成本 |
| `sodus locks` | 哪些目录有写入者 |
| `sodus unlock <cwd>` | 强解一个死 run 留下的锁 |

## 任务与执行

| 命令 | 说明 |
|---|---|
| `sodus task "<目标>" [--accept "<验收>"] [--title "<标题>"]` | 只打印 task id，例如 `task-5995110179e8` |
| `sodus tasks` | 任务列表 |
| `sodus run <task-id> [选项]` | 第一个参数是 **task id**，不是文本 |
| `sodus watch <run-id> [--after N]` | 跟随一个 run 的事件流 |
| `sodus stop <run-id>` | 杀这个 run 的进程树 |
| `sodus runs [--task <id>]` | 每行一个 run，含 `verify=ok\|red\|none` |
| `sodus show <run-id>` | 原始 run 记录 JSON |
| `sodus handoff <run-id>` | 该 run 写给下一位的交接 |
| `sodus report <task-id>` | 一页判决单，写到 `%USERPROFILE%\.sodus\tasks\<id>\report.md` |
| `sodus reconcile [--stale S]` | 把 owner 已死但还标着 running 的 run 判失败 |

`run` 的选项：

```text
--route ROUTE      auto | claude | codex | dsh | minimax | opencode-glm | opencode-gemini
--agent AGENT      老脚本用的同义参数（= route）
--cwd DIR          执行目录，写锁按它加
--wait SEC         等这个 run 结束
--propose          在独立 git worktree 里改，产出可合入的提案
--no-verify        worker 改了文件也不跑项目测试
--no-memory        不把记忆注入 brief
--model M          只这一次钉死模型
--effort E         推理档位
--profile P        用哪个配置快照
--parent RUN       挂到另一条 run 下面
--note TEXT        记一条备注
```

## 验收

| 命令 | 说明 |
|---|---|
| `sodus verify [--cwd D] [--command "C"]` | 检测测试命令并跑一次，打印 `FAIL exit=1 3060ms [pytest]` 这类结果 |
| `sodus tdd <task-id> [--writer A] [--impl B] [--review] [--dry-run]` | 红→绿流水线，可加交叉审查 |
| `sodus review <run-id> [--by A] [--focus F] [--scope task\|run] [--dry-run]` | 交叉审查一次执行 |
| `sodus fix <run-id> [--rounds N] [--review-first] [--by A]` | 审查→修→验→复审 |
| `sodus pipelines` | daemon 里的流水线 |
| `sodus bench` | 在 eval/tasks 上给路由打分 |

测试命令的检测顺序（`verify.detect`）：`pyproject.toml` / `pytest.ini` / `tox.ini` /
`setup.cfg` → `pytest -q`；`package.json` 有 `scripts.test` → `npm test --silent`
（有 `pnpm-lock.yaml` 就用 pnpm）；`Cargo.toml` → `cargo test -q`；`go.mod` →
`go test ./...`。一个都没有 → `verify=none`。

## 提案

| 命令 | 说明 |
|---|---|
| `sodus proposals [--all] [--limit N]` | 待处理提案。**没有 `list` 子命令** |
| `sodus proposal <id>` | 一个提案 + patch 全文 + 下一句该跑什么 |
| `sodus merge <id>` | 合入主树，关提案，删 worktree 和分支 |
| `sodus reject <id>` | 丢弃，删 worktree 和分支 |

`--propose` 产出的提案，主工作区**没有被改动**。合入是人的动作，也是唯一会改主树的路径。

## 会话与血缘

```text
sodus sessions [--vendor claude|codex|dsh] [--source run|user] [--query Q] [--no-orphans]
sodus session <id>        打印一份会话记录
sodus brief               写跨会话交接
sodus lineage <id>        这个会话从哪来、产出了什么
sodus resume <id>         继续一个会话（任何 vendor）
sodus branch <id>         从它的历史开新会话
```

## 路由与决策

```text
sodus route-for "<任务文本>" [--json] [--apply]   评估但不派活
sodus route                                       问 Jev 该用谁、什么档位
sodus triage <run-id>                             问 Jev 这条 run 要不要人看
sodus decisions [--limit N]                       最近的 Jev 判决
sodus jev                                         Jev key 配没配
```

`route-for` 实测输出含难度、类型、`rules+jev` 依据、打分排序、本地历史、置信度。
`--apply` 目前只输出下一步该跑的那条命令，不建任务。

## 记忆

```text
sodus memory add|recall|list|forget
```

scope：`private`（只属一个 agent）、`project`（当前 workspace）、`public`。同 key 覆盖。

## HTTP 接口

地址 `http://127.0.0.1:4199`，绑 loopback；要对外得设 `SODUS_ALLOW_REMOTE=1`。
除 `/`、`/health`、`/favicon.ico`、`/openapi.yaml` 外都需要
`Authorization: Bearer <token>`，token 在 `%USERPROFILE%\.sodus\token`。

```text
GET  /health               {"ok": true, "version": "0.9.0", "profile": "default", "home": "…"}
GET  /openapi.yaml         OpenAPI 3.1 契约
GET  /state                轻量只读状态快照（在跑的、等人的、今日花费、ask 与记忆候选计数）
GET  /agents               每个 agent 的 ready + missing
GET  /routes               每条路由的 ready + problems
GET  /providers  /profiles
GET  /tasks  /runs  /proposals  /pipelines  /events
POST /tasks                {goal, acceptance, title, session_id} → 201
POST /runs                 {task_id, route, cwd, wait, propose, model, effort, profile, note} → 201
POST /runs/<id>/stop
POST /pipelines            {kind: tdd|review|fix, params, label}
POST /proposals/<id>/merge    成功 200，冲突 409
POST /proposals/<id>/reject
```

没带 token 或 token 错：`401`。

## 配置

状态目录 `%USERPROFILE%\.sodus`（`SODUS_HOME` 可改）。里面：

```text
config.json      budget.daily_cny、cost_rates 等
token            48 字符本地 token
secrets.json     各 provider 的密钥
sodus.sqlite3    任务/run/事件/提案
daemon.json      {"pid": …, "port": 4199, "started": …}
runs/<run-id>/   handoff.md、verify-*.txt
tasks/<task-id>/ report.md
proposals/       *.patch
worktrees/       提案的 git worktree
locks/           写入锁
```

## 接进 Claude Code

项目根目录 `.mcp.json`：

```json
{
  "mcpServers": {
    "sodus": {
      "type": "stdio",
      "command": "A:\\Anaconda\\python.exe",
      "args": ["-m", "sodus", "mcp"],
      "env": { "PYTHONPATH": "A:\\VScode\\Code\\Projects\\sodus" }
    }
  }
}
```

`pip install -e .` 装过的话，`command` 写 `sodus`、`args` 写 `["mcp"]` 就行。

## 接进 DSH

`A:\dsh\home\cordis.patch.yml` 里追加（必须是 `insert:` 数组，否则被静默跳过）：

```yaml
- insert:
    - name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: sodus
        transport: stdio
        command: 'A:\Anaconda\python.exe'
        args: ['-m', 'sodus', 'mcp']
        env:
          PYTHONPATH: 'A:\VScode\Code\Projects\sodus'
        toolCallTimeoutMs: 600000
```

重启后工具名是 `mcp__sodus__sodus_task_create` 这种。`toolCallTimeoutMs` 要盖住
`wait=true` 的等待。DSH 起子进程会清掉 `*_KEY`/`*_TOKEN`/`*_SECRET`，Sodus 不需要这些。
