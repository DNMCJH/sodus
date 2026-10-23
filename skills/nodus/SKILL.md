---
name: nodus
description: 用 Nodus 派活给不同 agent（Claude Code / Codex / DSH / opencode / minimax），并用可执行证据验收。当任务要跑几分钟以上、要并行多个活、要可验证的交付、要把工作分给不同模型、要控成本，或用户说"跑个长任务""派给别的模型""并行做""验收一下""提案合入""这活谁在跑"时使用。也用于查 Nodus 的运行记录、成本与待人工处理的提案。
---

# Nodus — 派活与验收

给 agent 的操作手册，不是给人看的介绍。Nodus 是任务验收层：把任务派到 6 条路由上的 agent，跑完用测试退出码和测试文件 hash 判定通过，而不是问执行者"你好了吗"。下面每条命令都在本机实跑过，输出照抄。

## 什么时候用

- **活要跑几分钟以上**：终端会断、会话会超时。交给 daemon 后进程死了还能 `nodus reconcile` 收尾。
- **并行多个活**：每个 run 对 cwd 拿写锁。同目录第二个 run 直接失败，不会两个 agent 对着同一个文件互踩。
- **交付必须可验证**：你说"加个功能"，它跑 pytest 给你 exit code 和 `verify=ok`。
- **想换模型或比价**：`--route auto` 按难度、类型和本地历史挑；同一任务可换路由重跑。
- **改动不能直接落地**：`--propose` 在 git worktree 里改，出 patch，由人决定合不合。
- **不适合**：一句话能答的问题、纯读写文件、需要你看屏幕才能判断的事。

## 怎么用

**建任务 → 派活 → 等结果**

```powershell
nodus task "把 version.txt 从 0.1.0 改成 0.2.0" --accept "pytest 全绿" --title "bump version"
# task-82aa91c1deb3

nodus run task-82aa91c1deb3 --route auto --cwd C:\work\demo --wait 240
# run run-1c769eb924a9 (opencode-glm)
#   · verify: A:\Anaconda\python.exe -m pytest -q (pyproject.toml present)
#   · verify green: PASS exit=0 (3557ms)
# [completed] provider=relay-openai exit=0 in=5848 out=229 cache_read=16320 cost=¥0.002742(metered)
```

`nodus run` 的第一个参数是 **task id**，不是任务文本。给文本会这样：

```
$ nodus run --route auto "写个 hello.txt"
error: HTTP 400: {"error": "unknown task: 写个 hello.txt"}
```

`--route` 取值：`auto` `claude` `codex` `dsh` `minimax` `opencode-glm` `opencode-gemini`。`auto` 是接受的值，由策略选路——上面这次选了 `opencode-glm`。

**看结果**

```powershell
nodus runs
# run-1c769eb924a9  completed opencode-glm relay-openai verify=ok   task=task-82aa91c1deb3
# run-5ba6617d5335  completed dsh      deepseek     verify=none task=task-5995110179e8

nodus show run-1c769eb924a9      # 原始 JSON：status / error / usage_json / attempts
nodus report task-82aa91c1deb3   # 判决单：runs=1 verifies=2 reviews=0 decisions=0
nodus handoff run-1c769eb924a9   # 给下一位的交接（含"验证：测试通过"或"没有验证记录"）
```

**提案：改动没进主工作区**

```powershell
nodus proposals                 # 没有 list 子命令；`nodus proposals list` 报 unrecognized arguments
# prop-8ebf100abd62      open      10-23 19:44  1 file(s), +1 −0       nodus/run-5013bdb067ed

nodus proposal prop-8ebf100abd62   # 看 patch 全文（主树没被改）
nodus merge prop-8ebf100abd62      # merged …；proposal closed; worktree and branch removed
nodus reject prop-57c3c3f51dd7     # 不要了：rejected …; worktree and branch removed
```

**成本 / 状态 / 诊断**

```powershell
nodus cost        # 实测单价 + 标定与账单的漂移
nodus status      # 今日 runs/in/out、provider 探测、relay 余额
nodus doctor      # 8 项自检：home token db agents sessions secrets locks disk
nodus route-for "把 300 个文件加类型注解" --apply   # 只评估不派活
```

**MCP（agent 的原生入口）**

```powershell
python -m nodus mcp    # stdio，一行一个 JSON-RPC，不需要 daemon
```

七个工具：`nodus_task_create` `nodus_run` `nodus_run_status` `nodus_proposals` `nodus_cost_today` `nodus_memory_remember` `nodus_memory_recall`。参数见 [references/mcp.md](references/mcp.md)。

**合入/丢弃提案故意不提供 MCP 工具** —— 别指望模型批准自己的改动。

## 判决规则：结束 ≠ 通过

`status=completed` 只代表 worker 进程退出码 0。唯一的验收信号是测试。三态：

| 标记 | 含义 | 能当交付？ |
|---|---|---|
| `verify=ok` | 测试跑过且 exit 0 | 能，附 artifact 路径 |
| `verify=red` | 测试跑过且失败 | 不能 |
| `verify=none` | **没跑过测试** | 不能，只算"agent 说它做完了" |

实测的反例：

```
$ nodus report task-5995110179e8
runs=1 verifies=0 reviews=0 decisions=0
  - 没有任何验证记录：结果只靠 agent 自述
```

`verify=none` 三个常见原因：`--no-verify`、worker 没改文件、目录里检测不到测试命令（`pyproject.toml`/`pytest.ini`/`tox.ini`/`setup.cfg` → pytest；`package.json` 的 `scripts.test` → npm/pnpm；`Cargo.toml`；`go.mod`）。

更硬的证据在 `nodus tdd` / `nodus fix`：写测试前后对测试文件算 sha256，测完再算一次，变了就发 `verify.tests_changed` 事件——防的是 agent 改测试让自己通过。tdd 里 writer 失败且测试文件没变，直接终止，不重试。

所以：**别读 agent 的总结信它，读 `nodus runs` 的 verify 列和 `nodus report` 的缺口行。**

## 决策边界

`failed` 之后 Nodus 自己会重试（daemon 每 300s 扫一遍，`NODUS_AUTO_RECOVER=0` 关掉；上限 `NODUS_RETRY_LIMIT=3`，退避 60/300/900s；超过 24h 的失败算历史，不动）。它**不**重试这四种：

- **同一个错**：连续两次同 cause、同关键错误、同失败阶段的签名 → 停。"再跑一遍只会一样。"
- **预算不够**：剩余日预算或任务预算小于上次这类重试的实测成本 → 停，交给人。
- **单次太贵**：估算超过 `NODUS_RETRY_COST_CAP`（默认 ¥0.5）→ 停，等人点。
- **确定性失败**：401/403/400、内容审核、模型不存在。改请求本身才行，重发只会计费。

**必须找人的五类**：

1. 提案合入（`nodus merge` / `nodus reject` 只在 CLI）
2. 需求歧义——验收标准写不出来，就是任务没定义好
3. 超预算（日上限，或任务的 `budget_json`）
4. 重试耗尽（3 次还没过）
5. 密钥 / 权限（`nodus doctor` 的 secrets 行、401 类失败）

## 成本

```powershell
nodus cost      # claude-opus-5-5[1M] 实测单价；标定 ¥0.6817 vs 账单 ¥0.7607（+11.6%）
nodus status    # today runs=14 in=172502 out=2229；relay balance used=784.71 / limit=930.50
```

`nodus balance` 只报中转站余额。MCP 的 `nodus_cost_today` 返回今日按 agent 分组的 CNY，看板 <http://127.0.0.1:4199/board> 首屏就是今日花费。上限两处：

- **每日**：`config.json` 的 `budget.daily_cny`，或环境变量 `NODUS_DAILY_BUDGET`。只累计 CNY，到顶拒绝新 run，不杀正在跑的。
- **任务**：`budget_json.max_cost` / `max_runs`。

## 自检

```powershell
python skills\nodus\scripts\nodus-check.py         # 退出码 0 可用 / 1 不可用
python skills\nodus\scripts\nodus-check.py --json  # 给 agent 解析
```

## 参考

- [references/cli.md](references/cli.md) — 全量命令表、HTTP 接口、接进 Claude Code / DSH
- [references/mcp.md](references/mcp.md) — 七个 MCP 工具的参数与返回
- [references/troubleshooting.md](references/troubleshooting.md) — 跑不通时按症状查
