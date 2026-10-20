# Nodus 架构

> 版本 0.4.0 · 2026-10-21 · 这份文档解释「为什么代码长这样」，README 解释「怎么用」。

## 1. 需求（先定边界）

Nodus 是**任务验收层**：它不训练模型、不代理请求、不写 agent runtime，只回答一个问题——这个任务算不算完成。

| 角色 | 动作 | 系统里的落点 |
|---|---|---|
| 派活的人 | 写目标 + 验收标准，指定或让 Jev 选 agent | `task` → `run` |
| 干活的 agent | 读任务、改代码、留下交接 | `runner` + `handoff.md` |
| 验收的人 | 看 exit code、看证据路径、看审查结论 | `verify` / `review` / `report` |
| 下一个会话 | 读 brief 或 resume，接着干 | `dsh_sessions` + `brief` / `resume` |

四条硬规则，写在代码里而不是文档里：

1. **验收只认可执行证据**：测试 exit code + 测试文件 hash，agent 自述不算。
2. **审查只降级不升级**：review 可以把 `done` 变成 `needs_review`，不能把失败变成成功。
3. **跨会话只读 + 显式续写**：会话文件只读；续写要过锁、过 cwd 校验、过白名单。
4. **单机单用户优先**：SQLite、环回监听、一个 token；多租户只留 workspace_id 接缝。

## 2. 分层

```
nodus/cli/            argparse + 每个命令一个 handler（不碰业务）
  client.py        HTTP 客户端：唯一知道 daemon 地址的地方
nodus/server.py       HTTP + SSE，状态所有者
nodus/tdd.py          流水线：红→绿→（可选）审查
nodus/review.py       跨 agent 审查：证据收集 + 结构化 findings
nodus/fix.py          修复回路：审查→修→验→复审
nodus/report.py       任务报告（只读聚合）
nodus/runner.py       进程生命周期：尝试循环、spawn、读流、落事件、交接单
nodus/routes.py       路由组：一个 CLI + 有序 provider 链 + 校验
nodus/profiles.py     配置快照：按 profile × agent × provider 物化配置目录
nodus/agents.py       CLI 注册表：可执行、参数方言、流格式、能说哪种协议
nodus/providers.py    端点注册表：地址、凭据名、协议、单价、计量口径
nodus/adapters.py     各家 JSONL → 统一事件（纯函数）
nodus/verify.py       测试检测与执行（纯函数 + 落盘）
nodus/dsh_sessions.py DSH 会话读取 / brief / 锁（只读为主）
nodus/meter.py        计费表：run 前后读端点的账
nodus/db.py           SQLite：所有读取都按 workspace 过滤；工作目录写锁
nodus/config.py       路径、token、secrets、用户配置、当前 profile
```

依赖方向是单向的：

```
config → providers → agents → routes → profiles → runner → {tdd, review, fix, report} → {cli, server}
```

`adapters` 和 `verify` 不依赖上层；`db` 是基础设施，被所有层使用，但不反向 import 业务；
`meter` 只依赖 config + providers。

**这条依赖链是这次重构的判据。** 0.3.0 里 `meter._relay()` 要反读 `AgentSpec.config_home` 下的
`settings.json` 才能拿到 base URL 和 token——那说明"端点"这个概念不存在，事实寄居在别处。
现在 `providers` 在 `meter` 的上游，同样的事实只有一个家。

### 为什么 CLI 不直接 import 业务

`nodus run` 走 HTTP，`nodus tdd` 等流水线在 CLI 进程里执行（见 ADR-004）。两种路径都只通过
`db` 落状态，所以 `nodus watch` 从另一个进程也能看到。代价写在 ADR 里，不藏。

例外是 `nodus providers/routes/profiles`：它们是纯文件操作，故意不走 daemon——
端点坏掉正是你需要敲这些命令的时候。

## 3. 扩展点

| 想加什么 | 改哪里 | 不用改 |
|---|---|---|
| 新 CLI（Gemini CLI 等） | `agents.py` 加一个 `AgentSpec`；`adapters.py` 加一个 parser | runner、CLI、db |
| 新端点（同一 CLI 换 provider） | `providers.json` 加一条；或 `nodus providers add` | 任何 .py |
| 新回退链 | `routes.json` 加一条；或 `nodus routes set` | 任何 .py |
| 新配置快照 | `nodus profiles new` | 任何 .py |
| 新命令 | `nodus/cli/commands/<module>.py` 里 `add(sub)` + 一个 handler | 其他命令 |
| 新事件类型 | `adapters.py` 返回新的 `type`；消费方按前缀过滤 | schema（event 是 JSON） |
| 新存储后端 | 按 `db.py` 的函数面重写；调用方只认函数名 | 业务层 |
| 新通知渠道 | 目前没有；加的时候挂在 event 流的订阅侧 | 核心 |

## 4. 安全模型

| 面 | 措施 | 位置 |
|---|---|---|
| 网络 | 只绑 127.0.0.1；非环回要 `NODUS_ALLOW_REMOTE=1` | `server.serve` |
| 认证 | Bearer token，`hmac.compare_digest` 常量时间比较 | `server.authorized` |
| 请求体 | 上限 1 MB，超了 413 | `server.body_allowed` |
| 密钥 | `~/.nodus/secrets.json`，per-workspace 覆盖；ACL 去掉继承（仅当前用户 + SYSTEM）；只进入子进程环境，不打印 | `config.load_secrets` |
| 凭据注入 | `Provider.key_secret` 声明 secret 名，`providers.env()` 物化成该协议的环境变量；生成的 CLI 配置里**不允许出现 `env` 块** | `providers.env` / `profiles.materialize` |
| 最小权限 | 只注入端点自己的 key + agent 显式声明的额外 secret，不再把 secrets.json 全量灌进每个 worker | `agents.build_env` |
| 数据隔离 | 每张表带 `workspace_id`，所有读操作过滤 | `db.py` |
| 会话读取 | 可选 `session_roots` 白名单；默认不限制但每次显式指定会话 | `dsh_sessions._allowed` |
| 会话写入 | 单写者文件锁 + 会话 cwd 校验 + 120s 新鲜度守卫 | `dsh_sessions.session_lock` |
| 进程 | agent 以 `bypassPermissions` 跑，cwd 是唯一边界 | `agents.argv` |

已知缺口（不粉饰）：

- Windows 上 token 文件没有 ACL 加固，`chmod` 是 no-op。
- 没有文件读取审计；review 读哪些文件只记录在 `context.md`。
- 会话锁是建议性的：绕过 Nodus 直接跑 `dsh --session-id` 不会看这把锁。
- agent 能执行任意命令，这是产品定位决定的，不是遗漏。
- codex 的 `experimental_bearer_token` 写在它自己的 config.toml 里（codex 只能从配置读），
  那是用户私有目录，和 0.3.0 一样，没有变好也没有变坏。

## 5. ADR（架构决策记录）

**ADR-001 SQLite + WAL，单写者。** 单机单用户，状态量小，事务简单；WAL 让读不阻塞写。并发上限是真实的：10 个以上同时写没测过。

**ADR-002 文件交接，不共享对话。** 跨 agent 传的是 handoff.md（确定性模板）+ artifact，不传聊天记录。好处是上下文可预算、可审计；坏处是长任务需要人补「未决」。

**ADR-003 审查是建议，不是判决。** review 的 verdict 只影响任务状态降级和是否触发 fix；它不能替代测试。理由：LLM 审查会误报，也会漏报，门禁必须可执行。

**ADR-004 流水线在 CLI 进程执行。** `nodus tdd/review/fix` 直接在命令行进程里起 agent，不经过 daemon 队列。好处是终端能实时流、Ctrl-C 即停；坏处是关终端会中断流水线，且 `nodus stop` 管不到别的进程里的 run。迁移到 daemon 作业队列是后续工作，不是当前缺陷清单之外的隐藏项。

**ADR-005 会话桥只读 + 显式续写。** DSH 没有会话间消息通道；往 append-only 存储里注入会破坏 GUI 的 live state。所以读走文件、写走 `--session-id` 新 turn，并要求同 cwd + 单写者。

**ADR-006 一个 CLI = 一个 spec + 一个 parser；一个端点 = 一条数据。** 除 `agents.py` 和 `adapters.py`
两处外，代码里不出现 CLI 名字；除 `providers.py` 的默认表外，代码里不出现厂商 URL。
0.3.0 把这两件事混在 `AgentSpec` 里，代价是加后端要写 Python，`minimax` 这个"agent"其实是
"claude CLI + minimax 端点"的粘合体。见 ADR-009。

**ADR-007 后端可换，壳不用换。** MiniMax 提供 Anthropic 兼容端点，Claude Code 的 stream-json 契约不变，
所以在 0.3.0 里它的 `vendor` 字段是 `claude`——同一个 CLI、同一个 parser、同一个 runner，差别只在
base URL、凭据和价目。0.4.0 把这个"差别"从 `AgentSpec` 的一个副本提升成 `Provider` 这个类型：
`claude` 只是 CLI，`relay` 和 `minimax` 是两个端点。
代价：`~/.claude/settings.json` 的 `env` 块优先级高于进程环境变量，换端点必须让配置文件里没有 `env`
（0.3.0 的做法是换 `CLAUDE_CONFIG_DIR`，实测报 401 并退避重试 10 次、烧掉 190 秒）。
0.4.0 改成"生成的 settings.json 永远不含 env，端点只从进程环境注入"，并留了一条测试盯着它。

**ADR-008 成本读表，不读自述。** 成本有四个来源，优先级 metered_request > metered > list_price > reported。metered_request 是本地网关 tail 各 agent 的会话文件、按会话 id 算出每次调用的成本（精度 1e-6，且不受并发污染）；metered 是在 run 前后读 provider 计费接口求差（中转站累计消耗、DeepSeek 余额）；list_price 是 token × 公开价目，只有确认对方按价目扣费时才等价于实付；reported 是 CLI 自己算的账，只做兜底。
理由：CLI 的价目表是别人的（Claude Code 用 Anthropic 价格给任意后端计价），中转站还会叠自己的分组倍率，两者都推不出来，只能读表。实测同一个 run：CLI 自报 0.2627，实付 0.3230（站内余额），比值 1.23；换一次模型组合变成 1.44。
理由二：余额差有精度下限（DeepSeek 只有 0.01 元）和并发污染，网关按请求读两个问题都没有。实测同一个 dsh 任务：网关 0.001386 美元（4 次调用），余额差是 0.09 元或 0——差两个数量级。
代价：每个 run 多一次前置 HTTP 和最多 4s 的落账等待（走网关再加最多 6s）；余额差的窗口会重叠，`meter.window_s` 记录窗口但不消除重叠。中转站"站内余额"到人民币的换算比例是账户事实，API 不暴露，放在 `config.json` 的 `cost_rates` 里；网关的成本是美元（它自己打印 `≈$`），靠 `currency` 字段区分。
0.4.0 的补充：计费挂在 `Provider` 上而不是 agent 上（同一个中转的两个协议入口共用一本账，
所以 `_relay()` 会把 base_url 末尾的 `/v1` 剥掉再请求账单），并且**按 attempt 各记一笔**，
`usage.attempts[]` 留全部，顶层只放产出结果的那一次。

**ADR-009 三层：CLI、端点、路由组。** 三者变化的原因不同（CLI 升级 / 端点故障或换价 / 任务该走哪条路），
所以是三个类型：`AgentSpec`（可执行 + 方言 + 流格式）、`Provider`（协议 + 地址 + 凭据名 + 单价 + 计量口径）、
`Route`（一个 CLI + 有序 provider 链）。`Route` 还解决了一个命名骗局：`--agent minimax` 里的
`minimax` 从来不是 agent，现在它是 route 名，指向 `claude` CLI 的 `minimax` 端点。
理由：只要"端点"不是第一类对象，failover、价目、探活、计费口径就都只能寄生在别处，改一处漏一处。
代价：`run` 表多了 `route` / `provider` / `profile` 三列；`usage` 的分组从 agent 改成 route，
历史行没有 route 就回落到 agent 名。

**ADR-010 回退只在"什么都没做"时发生。** 换端点重跑的唯一正当理由是上一次尝试没有产生任何副作用。
判据是**事件流里有几个 `tool_call`**，不是花了多少时间、也不是退出码：
有 tool_call 就算上游 401 也不换（重跑会把同样的改动做第二遍），没 tool_call 且错误像上游问题
（401/403/429/5xx/连接/超时/限流）才换。CLI 打在 stderr 上的非 JSON 噪声也进判定，
因为连接失败常常只出现在那里——0.4.0 修 DSH 的 `status.turn_end.reason` 时顺手补的。
代价：回退不是静默的（CLI 打 `!!`、交接单写 `回退：A → B`、run 记录只留最终 provider），
而且慢——CLI 自己的重试预算先耗掉，实测死端点那次整轮 187 秒。回退救结果，不救时间。

**ADR-011 profile 是"生成"的，不是"手改"的。** 每个 (profile, agent, provider) 一个配置目录，
内容由两层生成：手改的 `profiles/<p>/<agent>.json` 覆盖层 + provider 的地址/凭据/模型映射。
理由：Claude Code 从 JSON 的 `env` 读端点，Codex 从 TOML 的 `experimental_bearer_token` 读，
两条路径各写一遍必然漂移；生成让"provider 是唯一真相"在两家都成立。
目录按 provider 分开，是为了两个 run 用不同端点并发时不互相覆盖配置（同输入产出同字节，不同输入不同目录）。
代价：切换 profile 只影响新起的 run；配置目录里 CLI 自己写的东西（会话、onboarding）
跟着 (agent, provider) 走而不是跟着人走。

**ADR-012 一个目录一个写者，锁跟着 run 走。** 两个 run 同时写一棵树，后一个的编辑会盖在前一个的半成品上，
两份交接单描述的状态都不存在。约束落在数据库里：`write_lock` 表 + `WHERE released_at IS NULL` 的部分唯一索引，
第二个 INSERT 失败而不是两个 worker 悄悄覆盖。行和锁同一事务插入，避免"有锁没有 run"的窗口被误判成持有者已死。
释放先于状态写入，所以 `wait()` 返回"完成"就保证目录可再用——`nodus tdd` 紧接着就起下一个 run。
代价：`review`/`fix` 会借走目标目录，目标 run 还在跑时会被拒；这是对的（审查一份正在变的树本来就没有意义），
但报错信息必须点名持有者，否则用户会以为是 Nodus 坏了。

## 6. 质量门槛

- `python -m pytest -q`：166 条，覆盖解析、策略、隔离、写锁、报告、计费表、端点/路由/配置物化、
  以及回退规则（真起子进程，不是 mock）。
- `scripts/smoke_cli.ps1`：命令逐个 `--help` + 关键命令真实执行。
- 每个新能力都要有一次真实调用记录：0.4.0 的四条 route（relay / minimax / relay-openai / deepseek）
  各跑过一次真任务，failover 用一条指向死端口的 route 实测过（`run-90c6a47c9bae`）。

## 7. 还没做的

- daemon 作业队列（ADR-004 的迁移）。
- 打包（PyInstaller / Go）。
- UI / SSE 客户端。
- 多租户鉴权（现在只有 workspace 字段）。
- 多写者锁 + propose/merge：一个 cwd 现在只能有一个 run 在写。
- dsh 的 `DSH_HOME` 隔离：profile 层已经在了，缺的是 dsh 认哪个环境变量。
