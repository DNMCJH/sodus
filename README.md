# Nodus

> 名字来自拉丁语 *nodus*（结、节点）。CLI、Python 包、仓库目录、状态目录都叫 `nodus`（`~/.nodus`）。
> 2026-10-21 之前它叫 `wb`／`workbench`，旧状态目录会在首次运行时自动迁移。

本地多 agent 任务编排器。不重写 agent loop，把 Claude Code / Codex / DSH / MiniMax 当 worker 驱动，
自己只管任务、会话、产物、交接和审计。产品原则见 [docs/vision.md](docs/vision.md)。

## 快速开始

```powershell
pip install -e .                 # 装一次，之后任何目录都能用 nodus
nodus topology                   # 一眼看全：agent × provider × route
nodus task "目标" --accept "验收标准"
nodus run <task-id> --route claude --cwd D:\work\demo
```

不装也行，等价写法是 `A:\Anaconda\python.exe nodus.py ...` 或 `python -m nodus ...`。

`nodus run` 会自动在 4199 端口拉起 daemon，**作为独立进程**，客户端退出它不退：
跑了三个并发 run、全部结束后 daemon PID 不变。日志在 `~/.nodus/daemon.log`。

要前台看输出就手动起：

```powershell
nodus serve
```

daemon 启动时会**重新对账**：库里任何还标着 `running` 的 run，其所有者进程必然已经死了，
一律标失败，并把 worker 已经写出的文件捞成 artifact（实测一次捞回 7 个产物文件）。
所以"exit 1 但东西其实做完了"这件事，现在库里看得见。

## 命令

| 命令 | 作用 |
|---|---|
| `nodus topology` | agents / providers / routes 一屏看完 |
| `nodus agents` | 各 CLI 是否装了 |
| `nodus providers list\|show\|probe\|add\|rm` | 端点：地址、凭据、单价、探活 |
| `nodus routes list\|show\|set\|rm` | 路由组：一个 CLI + 有序回退链 |
| `nodus profiles list\|show\|use\|new\|rm` | 配置快照：切一套环境 |
| `nodus doctor` | 自检：目录、token、数据库、路由、会话、密钥、磁盘 |
| `nodus --version` | 版本 |
| `nodus task "goal" --accept "..."` | 建任务，返回 task-id |
| `nodus sessions [--query TEXT]` | 列出 DSH 会话（读本地 zstd 存储） |
| `nodus session <id> [--tail N] [--reasoning]` | 打印另一个会话的对话 |
| `nodus brief <id> --task "..."` | 生成跨会话交接文件 |
| `nodus resume <session> --note "..." [--force]` | 在 DSH 会话上续一轮（必须同 cwd） |
| `nodus run <task> --route NAME [--profile NAME] [--model M] --cwd PATH [--note "..."]` | 起一次执行并跟踪 |
| `nodus watch <run>` | 跟踪已有 run |
| `nodus runs [--task ID]` | 列执行，带 route / provider / `verify=` 标记 |
| `nodus show <run>` | 看 run 的原始记录 |
| `nodus handoff <run>` | 看这次生成的交接单 |
| `nodus stop <run>` | 杀掉进程树 |
| `nodus reconcile [--stale 900]` | 把进程已死却还停在 running 的 run 标成 failed |
| `nodus route <task>` | Jev 选 agent + effort（和上面的 `routes` 是两件事） |
| `nodus triage <run>` | Jev 给执行打分 |
| `nodus decisions [--last N]` | 看历史决策 |
| `nodus usage [--days N]` | 按 route 汇总 token 与花费，标出走过回退的次数 |
| `nodus probe` | 探活各 provider（免费） |
| `nodus status` | 探活结果 + 今日用量 |
| `nodus verify --cwd DIR [--command "pytest -q"]` | 跑测试，返回 exit code（不花 token） |
| `nodus tdd <task> --cwd DIR [--review]` | 红→绿 TDD；`--review` 追加交叉审查 |
| `nodus review <run> [--by codex] [--scope task\|run] [--dry-run]` | 换一家审改动，落结构化 findings；有问题返回 exit 2 |
| `nodus fix <run> [--by dsh] [--rounds 2] [--review-first]` | 审查→修→验证→复审，收敛才 exit 0 |
| `nodus report <task-id\|run-id> [--print]` | 生成任务报告到 `tasks/<id>/report.md` |

`--agent` 仍然可用，是 `--route` 的旧名字。

## 结构

```
nodus.py                 入口，等价于 python -m nodus
nodus/cli/               argparse + 每个命令一个 handler
  client.py              HTTP 客户端（唯一知道 daemon 地址的地方）
nodus/config.py          路径、token、secrets、用户配置、旧目录迁移
nodus/providers.py       端点：地址、凭据、单价、计量口径（数据，可被 JSON 覆盖）
nodus/agents.py          CLI：可执行、参数方言、流格式、能说哪种协议
nodus/routes.py          路由组：一个 CLI + 有序 provider 回退链
nodus/profiles.py        配置快照：按 profile × agent × provider 物化配置目录
nodus/db.py              SQLite，所有读按 workspace 过滤
nodus/adapters.py        各家 JSONL → 统一事件
nodus/runner.py          起进程、读流、落事件、尝试循环与回退、生成交接单
nodus/verify.py          检测测试器、跑测试、把 red/green 落成证据
nodus/tdd.py             写测试 → 红 → 实现 → 绿 →（可选）审查
nodus/review.py          跨 agent 审查，结构化 findings
nodus/fix.py             审查 finding → 换一家修 → 验证 → 复审
nodus/report.py          任务报告
nodus/meter.py           计费表：run 前后读端点的账，求差
nodus/decision.py        Jev 路由与分诊
nodus/telemetry.py       探活、用量、余额
nodus/dsh_sessions.py    读 DSH 会话（zstd JSONL）、brief、会话锁
nodus/server.py          HTTP + SSE，默认 127.0.0.1:4199
tests/                   自己的一套 pytest（155 条）
```

架构决策（为什么代码这样分层、安全边界在哪）见 [docs/architecture.md](docs/architecture.md)。

状态默认放 `%USERPROFILE%\.nodus\`：

```
providers.json                端点的覆盖项（只存和内置不同的部分）
routes.json                   路由组的覆盖项
profiles/<name>/profile.json  这个 profile 默认用哪条 route / 哪个 model / 哪个 effort
profiles/<name>/<agent>.json  手改的 agent 设置（modelSettings、sandbox、trust_level）
profiles/<name>/<agent>/<provider>/   实际给 CLI 的配置目录（由上面两者生成）
runs/<run-id>/raw-1.jsonl     第 1 次尝试的原始流（回退时会有 raw-2.jsonl）
runs/<run-id>/prompt.md       实际发给 agent 的上下文
runs/<run-id>/final.md        最终回复
runs/<run-id>/handoff.md      交接单
runs/<run-id>/verify-red.txt  red 阶段完整测试输出
runs/<run-id>/verify-green.txt
runs/<run-id>/review.json     结构化 findings
runs/<run-id>/review.md       人读版审查
runs/<run-id>/context.md      审查者看到的证据（可复查）
tasks/<task-id>/report.md     任务报告
backup/agents-<时间>/         升级前的 settings.json / config.toml 原件
nodus.sqlite3                 全部状态
```

## 三层：CLI / 端点 / 路由组

以前这三件事糊在一个对象里，叫 `AgentSpec`。结果 `minimax` 既是 agent 又是端点，
加一个后端要改 `argv()` 的 if/elif 链，`Pricing` 挂在 agent 上，failover 无处安放——因为"端点"根本不是个东西。

现在拆开：

```
provider  打给谁：地址、凭据、协议（anthropic/openai/deepseek）、单价、计量口径
agent     用什么跑：可执行文件、参数方言、流格式、能不能续会话
route     怎么配：一个 agent + 一串 provider，顺序就是回退顺序
profile   哪套环境：一个目录，装 agent 的本地配置，可切换、可复制、可回滚
```

同一个中转的两个协议入口是两个 provider（`relay` / `relay-openai`），共用一把 key。
这不是重复，是事实：它们确实共用凭据，而那值得被看得见。

```powershell
nodus topology                  # 一屏
nodus providers probe           # 四个端点各打一次模型列表，不花 token
nodus routes set cheap --agent claude --providers minimax
nodus run <task> --route cheap
nodus profiles new work --from default
nodus profiles use work
```

`providers.json` / `routes.json` 里**只存和内置不同的部分**。所以升级不会把你手上的默认值冻住。

### 回退只在"什么都没做"的时候发生

一条 route 上第一个端点失败，会换下一个——但**只有一个条件全部成立才换**：

1. 这次尝试**没有产生任何 tool_call**；
2. 失败信息像上游问题（401/403/429/5xx/连接/超时/限流），或者进程一个字都没输出就非零退出；
3. 后面还有 provider。

第一条压倒一切。跑到一半断线的 worker 不算上游故障——换个端点重跑会把同样的改动做第二遍，
那正是回退链最不该引入的故障模式。这条规则有独立测试（真起子进程，不是 mock）。

回退不是静默的：CLI 会打印 `!! relay-dead failed before doing anything (...) → retrying on minimax`，
交接单里写 `回退：relay-dead → minimax`，`nodus runs` 显示最终 provider，`nodus usage` 数回退次数。

实测一次（`run-90c6a47c9bae`）：第一段指向死端口，Claude Code 自己重试到放弃并打出
`Connection refused (ECONNREFUSED)`，Nodus 判定无副作用、换到 minimax，任务完成，花费 ¥0.013476。
代价是**慢**：CLI 自己的重试预算先耗掉了，整轮 187 秒。回退救的是结果，不是时间。


## 交接模型

跨 agent 传的是文件，不是对话。每次 run 的上下文 =
任务目标 + 验收标准 + 本次指令 + 上一次的 `handoff.md`。

交接单由 runner 确定性生成（不调模型）：目标、验收、本次结论、改动文件、错误、未决，外加一行 **验证**。

那一行的三种取值和 `nodus runs` 的 `verify=` 对齐：

| 标记 | 含义 |
|---|---|
| `ok` | 这个 run 上跑过测试并绿了 |
| `red` | 跑过，但红着 |
| `none` | 一次都没跑 —— **`completed` 只代表进程退出码 0** |

为什么要单独一列：2026-10-21 有一次真实事故，派给 dsh 的任务被 Windows 沙箱挡在启动前，
worker 什么都没干、把"被阻断"写进结论、进程照样退出 0，Nodus 记成 `completed`。
状态不会骗人，但它回答的是"进程活下来了吗"，不是"事情做成了吗"。
现在 `none` 会把这句话直接印在交接单和列表里，下一个 agent 和人都不会读错。

例外：审查 run 不注入交接单。审查者读上一轮的结论等于让它给自己找理由，实测会锚定（见下文 review 一节）。

## 各家的必需参数（实测）

| CLI | 端点 | 关键参数 | 说明 |
|---|---|---|---|
| claude | relay（默认）/ minimax | `--permission-mode bypassPermissions` | 否则 `-p` 模式下写文件会被拒 |
| minimax | minimax | 同 claude | 复用 Claude Code 这个壳，只换后端；差别只在 base URL、模型映射和价目 |
| codex | relay-openai | `--skip-git-repo-check` + `--dangerously-bypass-approvals-and-sandbox` | `-s workspace-write` 在 Windows 非交互下会挂起；`[windows] sandbox = elevated` 同样挂起 |
| dsh | deepseek | `DEEPSEEK_API_KEY` | DSH 启动子进程会清掉 `*_KEY`，必须在 `secrets.json` 里显式给 |

MiniMax 走的是 Anthropic 兼容端点（`https://api.minimax.cn/anthropic`），所以它和 claude 是同一个
agent（同一份 CLI、同一份 stream-json、同一个 parser），区别只有端点、模型映射和计费——
现在这件事在 `providers.json` 里是明写的，不再靠"另一个 AgentSpec"暗示。

那个 190 秒的坑还在，但结构变了：`~/.claude/settings.json` 的 `env` 块优先级高于进程环境变量。
Nodus 的做法是让生成的 settings.json **永远不含 `env`**，端点全从进程环境注入
（`profiles.materialize` 里有测试盯着这一条）。所以换端点不需要换 `CLAUDE_CONFIG_DIR`，
只需要换 provider——虽然配置目录仍然按 provider 分开，那是为了并发安全，不是为了隔离凭据。

## token 与耗时（2026-10-19 实测，单次 trivial 任务）

| vendor | 隔离配置后 input | 耗时 |
|---|---|---|
| claude | 34k–146k（随 cwd 文件量涨） | 7–30s |
| minimax | 18.5k（trivial 改词）/ 19.5k（修一行 bug，cache_read 97k） | 17s / 41s |
| codex | 20k–62k | 20–70s |
| dsh | 20k–24k（含 DSH 自身 system prompt + 全局 AGENTS.md，未隔离） | 1m18s |

MiniMax 的输入大头是 Claude Code 自己的 system prompt + 工具定义（约 18k），和模型无关。
这张表是 token 和耗时，不是钱——钱见下一节。

## 成本：读表，不读自述

**结论：成本从 provider 的计费接口读，不取 CLI 自报的数字。**

`nodus/meter.py` 有两条读法：**按请求读日志**（最准）和**按余额读差值**（兜底）。

| agent | 表 | 精度 |
|---|---|---|
| dsh | 本地网关的请求日志（按会话匹配）→ 没有就退回 DeepSeek 余额 | 1e-6 美元 / 0.01 元 |
| claude | 中转站 `/v1/dashboard/billing/usage`（累计消耗） | 1e-4 站内余额 |
| minimax | 没有余额接口（`/v1/account/balance` 要 admin key），用价目表 | 精确，官方就是按价目扣 |
| codex | 没有 | 记 n/a |

第一条只在装了本地网关（magpie）时生效：它 tail 各 agent 的会话文件，知道**每一次调用**的
token、模型和成本，并且带会话 id——正好和 run 记的 `session_ref.session_id` 对得上。
实测一次 dsh 改字任务：`cost=0.001386, currency=USD, cost_basis=metered_request, requests=4`，
折合约 ¥0.010；同一件事按余额差读是 ¥0.09 或者干脆 0，因为余额只有 0.01 元精度。

实测一次 claude 改字任务：`cost=0.322958, currency=CNY, cost_basis=metered, meter={unit:relay_unit, delta:0.322958, window_s:29.7}`。

四个容易搞错的地方：

1. **中转站的"$"不是美元。** 官方文档写得很清楚：模型广场的价格单位是"站内余额 / 1M Tokens"，不是美元；
   站内余额 = 官方美元价 × 分组倍率。这个账户 1 站内余额 = ¥1。
   所以 CLI 报的 `$0.2673` 既不是美元也不是实付，实付是 **¥0.3230**。
   按 7.2 的汇率把它当美元读，会高估约 5.8 倍。
2. **CLI 的价目表是别人的。** Claude Code 拿 Anthropic 的价格给任意后端计价，中转站的倍率它不知道。
   同一个 run：`total_cost_usd=0.2627`，实测 0.3230 站内余额，比值 1.23；另一次是 1.44。
   倍率随分组和模型浮动，**推不出来，只能读表**。
3. **换算比例是配置，不是常量。** `~/.nodus/config.json` 里 `{"cost_rates": {"relay_unit": 1.0}}`。
   拿不到 API 的地方就承认拿不到，别写死。
4. **网关的成本是美元。** `magpie usage` 自己打印的是 `≈$`，所以 `cost_basis=metered_request` 记 USD，
   不要和人民币的 `metered` 相加。币种分开统计这件事，报告里已经做了。

代价：每次 run 多 1 次前置 HTTP（约 0.2s），结束后最多等 4s 让计费落账；走网关按请求读时，
最多再等 6s 等它 tail 到会话文件。两个 run 并发时，余额差会同时覆盖两者——`meter.window_s` 记了窗口；
按请求读没有这个问题。只有 dsh 走网关这条路：Nodus 的 claude 跑在隔离配置目录里，网关扫不到。

## 为什么不要一家一个 CLI

一轮五家就是 15 万 token 起。讨论/评审用裸模型 API，CLI 只用于需要工具的活。

## Jev（可选决策层）

Jev 接在编排层，不接进 worker。原因：决策要可审计、不占 agent 上下文、失败要能降级。

```powershell
# 1) 放 key 和代理
Copy-Item secrets.example.json $env:USERPROFILE\.nodus\secrets.json
# 编辑 secrets.json，填 TYPESAFE_API_KEY

# 2) 检查
A:\Anaconda\python.exe nodus.py jev

# 3) 路由：让 Jev 同时选 agent 和思考档位
A:\Anaconda\python.exe nodus.py route <task-id>
A:\Anaconda\python.exe nodus.py run <task-id> --agent auto
A:\Anaconda\python.exe nodus.py run <task-id> --agent codex --effort high

# 4) 分诊：给一次执行打分，决定你要不要看
A:\Anaconda\python.exe nodus.py triage <run-id>

# 5) 看历史决策，用来校准
A:\Anaconda\python.exe nodus.py decisions --last 10
```

路由的实现：一次请求里问两个 `choice`，`agent` 的 criteria 带上相对成本档和擅长项，
`effort` 的 criteria 是 low/medium/high 的语义。Jev 只输出标签，真正改参数的是 nodus：

| vendor | effort 落地方式 | 备注 |
|---|---|---|
| claude | `--effort <low\|medium\|high\|xhigh\|max>` | CLI 原生支持 |
| codex | `-c model_reasoning_effort="<level>"` | 走 config 覆盖 |
| dsh | 无 per-run 参数 | 档位在 profile 里，当前只记录不生效 |

行为约定：

- 任何失败（没 key、代理不通、451、超时、5xx）返回 `unavailable`，调用方按原逻辑继续。
- 决策只影响“先看哪个/用多深”，不认证完成、不授予权限。
- 每次决策追加到 `%USERPROFILE%\.nodus\decisions.jsonl`，包含 state hash、答案、token 用量和延迟，用来事后判断 Jev 是否真的省了事。
- 默认模型 `jev-latest`；官方 API 不接受 `jev-1.14` 这类版本号（会 400）。
- **成本档是占位值**（claude 3 / codex 2 / dsh 1），不是实测价格。要校准就改 `nodus/decision.py` 里的 `AGENT_PROFILE`，或者等 `decisions.jsonl` 攒够样本后按实际用量重排。
- Jev 自身只有 `jev-latest` 和 `jev-preview` 两个模型，没有思考档位；能调的是下游 agent 的 effort。

## 监控

探活走各家的 `/v1/models`，不花生成 token：
9527 一次约 1s 返回 43 个模型，TypeSafe 约 1s 返回 2 个。结果追加到
`%USERPROFILE%\.nodus\probes.jsonl`。

用量来自 run 表，按 agent 汇总。注意 `in` 是新鲜输入，`cache_read` 是缓存输入，
两者相加才是总的输入上下文；只把 `in` 当账单会严重低估。

Jev 没有余额接口，只能：
- 用 `/v1/models` 做连通性检查；
- 用 `decisions.jsonl` 里的 token 数估算消耗；
- 额度耗尽时接口会返回 401/402，探活会先发现。

坑：9527 会对默认的 Python UA 返回 403，必须显式带 `User-Agent`。

## 验收：verify

`nodus verify` 是唯一不依赖 agent 自述的验收信号。检测顺序：`pyproject.toml`/`pytest.ini`/`tox.ini`/`setup.cfg` → `package.json` 的 `scripts.test` → `Cargo.toml` → `go.mod`；都没有就返回 exit 2。

实测三种路径：

```
PASS  exit=0  3036ms  [pytest] A:\Anaconda\python.exe -m pytest -q
FAIL  exit=1  3384ms  [pytest] ...
no test runner detected in C:\Users\35336\AppData\Local\Temp
```

## TDD 闭环（已验证）

writer 写失败测试 → `verify` 必须红 → impl 实现（禁止改测试）→ `verify` 转绿；失败重试 ≤2 次，带失败输出。测试文件前后做 hash 比对，被改动就标记 suspicious。验收只看 exit code。

每次验证都落成证据：完整输出写 `runs/<id>/verify-<phase>.txt`，同时写一条 `verify.red|green` 事件，报告和审查都从这里读，不靠终端回滚。

2026-10-20 用 `%TEMP%\nodus-slug-demo` 实测（一条命令 `nodus tdd task-07b9e79c4e68 --cwd ... --review`）：

```
writer  claude  6 个测试，全部按预期失败
red     exit=1  3517ms
impl    codex   拿到失败输出，实现 slugify
green   exit=0  3201ms
tests_changed  []        测试文件未被修改
review  claude  verdict=accept  critical=0 high=0 low=1
```

墙钟 2m52s，in 89k / out 15k / cache_read 472k，claude 侧 $0.73。

## 审查：review

审查者永远是**另一家** agent（codex 的活给 claude 审，反之亦然），读的是落盘的证据：任务目标、验收标准、artifact 里当前的文件内容（git 仓库里走 `git diff HEAD -- <改动文件>`），不是实现者的自述。上下文有上限（每文件 16k 字符、总 80k），截断会标 `context_truncated`。

结论只有三种 `accept / revise / reject`，findings 必须带文件、行号、证据、建议。**审查不是判决**：它不能把任务从失败改成成功，只能把 `done` 降级为 `needs_review`。exit code 只表达"审查这件事"：0 通过，2 有 `critical` 或未通过，1 没跑成。

负向对照（证明它真能抓到东西）：把已绿的实现换成"只折叠半角空格"的版本，6 条测试仍然全绿，`nodus review run-350c09aeb28b` 直接给出

```
verdict=revise  critical=0 high=1
[HIGH] slugify.py:9  只有半角空格被替换成连字符…\t、\n、\xa0 会原样输出
```

同一轮还暴露了一个设计错误：审查者读到了交接单里上一轮的审查结论，指出"它描述的代码和当前文件不一致"。这说明审查会被上一轮结论锚定，已改成审查 run 不注入 handoff（`include_handoff=False`）。恢复实现后再审，verdict 回到 `accept`。

## 修复回路：fix

`nodus fix <run> --review-first` 把审查结论交回给另一个 agent，修完重新跑测试，再用同一个审查者复审。停止条件只有三个：复审通过、测试变红、轮数用完。fixer 不能自己宣布成功。

2026-10-20 实测（task-07b9e79c4e68 / run-350c09aeb28b，预置"测试 6 passed 但只折叠半角空格"）：

```
review before: revise by claude（HIGH slugify.py:9）
round 1: fix=dsh  verify=PASS  re-review=accept
exit 0
```

dsh 只改了一行：`re.sub(r" +", ...)` → `re.sub(r"\s+", ...)`，测试文件没动，复审 accept。

局限：fix 只认最新一次审查结论。如果审查误报，fixer 会被要求修一个不存在的问题——目前只能靠在 prompt 里要求它说明"不成立"，没有自动驳回。

## 跨会话：读、交接、续跑

DSH 把每个会话存成 `$DSH_HOME/sessions/<project>/<id>/session.v4.jsonl.zstd`，append-only、zstd 压缩。DSH 的 CLI 只负责启动 profile，没有跨会话 API，所以 Nodus 直接读这个存储（只读，不写、不重压、不注入）。

能力边界，全部实测过：

| 想做的事 | 能不能 | 怎么做 |
|---|---|---|
| 读另一个会话的内容 | 能 | `nodus session <id>` / `nodus brief <id> --task ...` |
| 让另一个会话接着干 | 能，有条件 | `nodus resume <id> --note "..."`：走 DSH `--session-id`，必须用该会话记录的工作目录，开的是新 turn |
| 两个会话实时互相问答 | 不能 | DSH web 没有会话间消息通道；只能文件接力，由人或调度器触发 |
| 往正在 GUI 打开的会话里插话 | 不安全 | 同一会话双写会打架；`nodus resume` 默认拒绝 120s 内写过的会话，要 `--force` |

实测：

```
nodus sessions --limit 3
→ session-5711924e… 沐曦训练营课程截止时间查询  user=4

nodus resume 8bb738b6 --force --note "最后确认一次：我让你记的数字是多少？"
→ 7391   （会话是另一个进程建的，这次是 turn 3；Nodus 只负责续一轮）
```

交接文件是机械抽取，不是总结：任务 + 来源会话原话 + 提取到的链接/本地路径，标注「未改写」。目标会话拿到的是证据，不是转述。

三条护栏：读会话只读不写；`nodus resume` 用 `~/.nodus/locks/<session>.lock` 保证单写者，并拒绝 120 秒内被写过的会话；`~/.nodus/config.json` 里配 `session_roots` 可以限制只读哪些工作区的会话（不配则不过滤）。

## 多 agent 协作实测（2026-10-20）

三个场景，都是真实 agent 调用，没有 mock：

**A. 顺序交接**（task-242f58a8b3f8，demo `%TEMP%\nodus-handoff-demo`）

```
claude  只做第一步（明确要求不写第四行）  → data.txt 三行
dsh     无额外指令，读 handoff 续做        → alpha/beta/gamma/delta
检查：handoff 点名 data.txt；run2 的 prompt 含 run1 结论原文
```

**B. 审查 → 修复 → 复审**（task-07b9e79c4e68）

```
预置：6 passed，但实现只折叠半角空格
claude 审查  → revise（HIGH slugify.py:9）
dsh    修复  → 一行改动 r" +" → r"\s+"
verify      → PASS（fix1，2995ms），测试文件未改
claude 复审  → accept
```

**C. 并发**（task-9c6598b809ac / task-54378b63663f）

```
两个 dsh 任务同时跑，52s 全部完成，产物各就各位
PRAGMA integrity_check = ok；每个 run 的 seq 无重复
```

并发这一轮撞到一次真挂起：dsh 子进程 13 分钟没有输出，进程还活着。因此补了两层：

- runner 空闲看门狗：`NODUS_IDLE_TIMEOUT`（默认 900s）内没有任何输出就杀掉并标 failed，不再无限等；
- `nodus reconcile`：把"启动它的进程已经死了、状态还停在 running"的 run 标成 failed（这次就是用它清掉的）。
- 报告也改了口径：完全没有验证记录时写"结果只靠 agent 自述"，不再硬说"缺少 red"。

## 报告：report

`nodus report <task>` 把 run、事件、决策汇成一页：墙钟与 agent 累计时间、token、成本、每次验证的 exit 和证据路径、审查结论、Jev 决策、以及**标记**（失败 run、测试被改、没有 red、red 意外通过、最新审查未通过、`needs_review`）。

`usage_json` 里记四个字段：`cost`、`currency`、`cost_basis`、`meter`。
`cost_basis` 只有四种取值，从可信到不可信：

| basis | 来源 | 什么时候用 |
|---|---|---|
| `metered_request` | 本地网关按会话读到的每次调用成本 | 装了网关且是 dsh 时 |
| `metered` | provider 计费接口的前后差值 | 其他所有能读表的 |
| `list_price` | token × 公开价目 | 按价目扣费的（minimax） |
| `reported` | CLI 自报 `total_cost_usd` | 前三者都拿不到时的兜底，报告里标"CLI 自报" |

币种仍然分开加总，不跨币种相加——2026-10-20 之前的历史 run 是 `reported`（USD），
读起来会偏大，`nodus usage` 会在后面标"（含 CLI 自报，偏大）"。
那批数据没有逐 run 账单可回溯，所以不猜、也不改，只标来源。

2026-10-20 修复：`total_cost_usd` 在 Claude 的 result 对象上，不在 `usage` 里，适配器之前一直漏读；已修，并回填了 7 个历史 run。

## 已知限制

- Windows 上子进程可能继承 stdout 管道不释放，runner 用 2s 退出宽限兜底，不是完美方案。
- codex 的 bypass 参数等于关掉沙箱，只在受控 cwd 下用。
- 还没有 SSE 客户端和 UI，`nodus watch` 是轮询（0.5s）。
- handoff 是确定性的模板，不是模型总结；接 `--parent` 做分叉还没验证。
- dsh 已跑通（2026-10-20）：headless + `--json` 的 `tool_call` / `tool_result` / `usage` / `text` 都能解析。但它没有隔离配置目录，跑的时候带着你的全局 `AGENTS.md`、skills 和 MCP，小任务 fresh input 约 2 万；也没有 per-run effort 入口。
- `--model` 已接：`nodus run <task> --route claude --model claude-opus-5-5[1M]`。claude 走 `--model`，codex 走 `-c model="..."`，dsh 没有 per-run 入口（只记进 run 记录）。
- 回退链救结果不救时间：CLI 自己的重试预算会先耗掉（实测死端点那次整轮 187 秒）。要更快失败得给"首字节前"设单独的短超时，那是猜，没做。
- 回退链里第 2 个端点用的是**它自己的模型**，不是本次 pin 的模型。pin 只对第一个端点生效，否则会把一个那端点不认识的模型名发过去。
- 路由准确性没测过；一次 choice 在 criteria 写得含糊时会给高置信度的错答案（实测把文档任务判给 codex，0.90）。
- usage 是逐条 assistant 消息累加，`input_tokens` 经常只有个位数、`cache_read` 偏高，只能看趋势不能当账单。
- 审查读的是**当前工作区**，不是 run 时刻的快照。文件被改动时，`context.md` 里对应文件会标"当前内容与 run 记录不一致"，但审查仍按当前内容做——负向对照就是靠这一点。
- 审查上下文到 80k 字符会截断，`review.json` 里有 `context_truncated` 字段，`review.md` 里没有显式警告，大 diff 时容易误判"看全了"。
- 成本现在靠读表，但还有四个洞：minimax 要 admin key 才能读余额（普通 key 返回 `token_type_mismatch`），所以它用价目表；dsh 的 DeepSeek 余额只有 0.01 元精度，低于这个数的 run 记 0；**dsh 和本机 DSH 会话共用同一个 key**，所以它的余额 delta 会混进别的用量，那个数是上界不是精确值；中转站的余额差是账户级的，两个 claude/codex run 并发时窗口重叠，那个数会偏大。
- 一次 run 有多次尝试时，顶层 `cost` 记的是**最后那次**（真正产出结果的那次）。前面失败尝试的开销留在 `usage.attempts[]` 里，`nodus usage` 的合计只加顶层那个——失败尝试通常没花到钱，但不是永远为 0。
- 并发只测了 2 个 run 同时跑；SQLite 单写者，10 个以上并发和跨小时长任务没测。
- fix 回路只认最新一次审查；审查误报时没有自动驳回，只能靠 fixer 在回复里说明"不成立"。
- 跨会话续跑只支持 dsh；claude/codex 的会话 ID 体系不同，没接。
- DSH 会话文件没有"正在被 GUI 使用"的标记，只能按最近写入时间猜；`--force` 有双写风险。
- 读会话依赖 `zstandard`（本机已装）；会话是 append-only，Nodus 只读不写。
- 流水线命令（tdd/review/fix）在 CLI 进程里执行（ADR-004）：关终端会中断，`nodus stop` 也管不到别的进程里的 run。daemon 作业队列还没做。daemon 本身现在独立于客户端，这个问题只影响流水线，不影响 `nodus run`。
- `nodus run` 不会自动跑验收：`completed` 只等于 exit 0，要不要验证得显式调 `nodus tdd` 或 `nodus verify`。`verify=none` 就是这个意思，别把它读成通过。
- 安全边界是 cwd：agent 以 bypass 权限跑，能执行任意命令。`~/.nodus/secrets.json` 已收紧 ACL（去掉继承，只留当前用户 + SYSTEM），但 `token` 文件没有做同样处理。
- `nodus route` 的画像表里还没有 minimax，`--agent auto` 只会在 claude / codex / dsh 里选。
- minimax 复用的是 Claude Code，所以 Claude Code 的限制它全都有：本地按 200k 上下文档位算（MiniMax 实际是 1M）、`--effort` 档位走 Claude 的枚举（MiniMax 支持 low/medium/high/xhigh/max，`xhigh` 没验证过）、不能 `nodus resume`。
- profiles 是快照，不是运行时隔离：切换只影响新起的 run，正在跑的那一个还用着旧目录。两个 run 用不同 provider 并发是安全的（目录按 provider 分开），用同一个 provider 但不同 profile 也安全（按 profile 分开）。

## 下一步

1. 给 dsh 建隔离 profile（`DSH_HOME` 或独立 headless 配置），别让它带着全局指令和 MCP 跑；profile 层已经在了，缺的是 dsh 认哪个环境变量。
2. `nodus run` 跑完后，如果能检测到测试器就自动验一次（可 `--no-verify` 关掉）。这是 `verify=none` 的正解，但要先想清楚副作用：未知仓库的测试可能有破坏性。
3. 打包：PyInstaller 单文件先试，失败就换 Go 重写这几百行。
4. `nodus discuss`：同一问题发给多家裸模型，产出对照表（不要一家一个 CLI，成本会炸）。
5. VS Code 扩展只做三件事：列表、派活、看事件流和 diff。
6. 多写者锁 + propose/merge：现在一个 cwd 只能有一个 run 在写，"协作"还只是串行交接。
