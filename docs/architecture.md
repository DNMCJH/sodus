# Nodus 架构

> 版本 0.4.0（主干，未发版）· 2026-11-03 · 这份文档解释「为什么代码长这样」，README 解释「怎么用」。
> 2026-11-03 校准：补齐 0.4.0 之后进主干的模块（observe / ledger / usage / telemetry / budget / policy /
> recover / memory / lineage / bench / llm / jobs / harness_status / lifecycle），把已实现的 ADR 标出来，
> 第 7 节改成真实现状。同一天新增 `nodus/lifecycle.py`（状态机，只定义不迁移调用方）和
> `docs/overlap-audit.md`（谁在重复算同一件事）。

## 1. 需求（先定边界）

Nodus 是**任务验收层**：它不训练模型、不代理请求、不写 agent runtime，只回答一个问题——这个任务算不算完成。

| 角色 | 动作 | 系统里的落点 |
|---|---|---|
| 派活的人 | 写目标 + 验收标准，指定或让 Jev 选 agent | `task` → `run` |
| 干活的 agent | 读任务、改代码、留下交接 | `runner` + `handoff.md` |
| 验收的人 | 看 exit code、看证据路径、看审查结论 | `verify` / `review` / `report` |
| 下一个会话 | 读 brief 或 resume，接着干 | `sessions/` + `brief` / `resume` |

四条硬规则，写在代码里而不是文档里：

1. **验收只认可执行证据**：测试 exit code + 测试文件 hash，agent 自述不算。
2. **审查只降级不升级**：review 可以把 `done` 变成 `needs_review`，不能把失败变成成功。
3. **跨会话只读 + 显式续写**：会话文件只读；续写要过锁、过 cwd 校验、过白名单。
4. **单机单用户优先**：SQLite、环回监听、一个 token；多租户只留 workspace_id 接缝。

## 2. 分层

```
nodus/cli/            argparse + 每个命令一个 handler（不碰业务）
  client.py        HTTP 客户端：唯一知道 daemon 地址的地方
nodus/server.py       HTTP + SSE，状态所有者；看板把行分成 needs_you / handling / stopped
                      （watchdog 标的 stalled 在 stopped）
nodus/daemon.py       daemon 自身：`nodus up/down`，分离进程的生命周期
nodus/jobs.py         daemon 作业：tdd/review/fix 的线程与持久化状态（pipeline 表）
nodus/tdd.py          流水线：红→绿→（可选）审查
nodus/review.py       跨 agent 审查：证据收集 + 结构化 findings
nodus/fix.py          修复回路：审查→修→验→复审
nodus/report.py       任务报告（只读聚合）
nodus/runner.py       进程生命周期：尝试循环、spawn、读流、落事件、交接单
nodus/propose.py      提案：worktree 分支 → patch → proposal 行；merge / reject
nodus/routes.py       路由组：一个 CLI + 有序 provider 链 + 校验
nodus/profiles.py     配置快照：按 profile × agent × provider 物化配置目录
nodus/agents.py       CLI 注册表：可执行、参数方言、流格式、能说哪种协议
nodus/providers.py    端点注册表：地址、凭据名、协议、单价、计量口径
nodus/adapters.py     各家 JSONL → 统一事件（纯函数）
nodus/verify.py       测试检测与执行（纯函数 + 落盘）
nodus/sessions/       会话层：三家存储统一读取 + 接续/分支（DSH/Claude/Codex 各一个适配器）
nodus/lineage.py      会话血缘：一次任务的所有 sitting / run / 成功与未验证（ok/unverified）
nodus/meter.py        计费表：run 前后读端点的账
nodus/pricing.py      标定单价：token → 钱（basis=list_price，峰值/空闲两档）
nodus/ledger.py       usage_json 的唯一读法：解析、求和（含跑着的行的事件流）、分币种、缓存命中率
nodus/usage.py        `nodus stats usage` 的薄壳：选行 + JSON 信封，算术在 ledger
nodus/diagnosis.py    失败分类的唯一词表：13 cause / 4 class / 13 kind，以及每个 cause 的
                      problem / suggestion / advice / fix（建议句和停下的行的名词短语都从这里取）
nodus/telemetry.py    端点探活 + 旧的 per-route 汇总（token/金额已交 ledger，形状未变）
nodus/budget.py       机器级日额度：CNY 上限、拒绝新 run、不杀在跑的
nodus/lifecycle.py    run/proposal 状态机 + needs_attention，纯函数，不碰 db/网络
nodus/watchdog.py     卡住检测：running 但 N 分钟没有实质事件 → stalled（这条边的第一个 writer）
nodus/recover.py      自动恢复：gate 决定重试/退避/停下，按价值而不是次数
nodus/policy.py       派活前判断：难度、类型、所需能力 → 推荐路由（规则优先，Jev 复核）
nodus/observe.py      DSH 会话观察：读 session 日志、定价、写 observation
nodus/observe_sync.py 观察的定时同步（daemon 里扫，不用人手动 --import）
nodus/observe_magpie.py / observe_ccswitch.py  第三方用量日志的只读覆盖探针（不写 observation）
nodus/harness_status.py 五个终端：装没装、在不在跑、今天多少（只读，不启动任何东西）
nodus/memory.py       记忆摘要：读各 harness 已有的 AGENTS/CLAUDE 文件，等人采纳
nodus/bench.py        任务级评测：hidden_tests 打分，不看 agent 自述
nodus/llm.py          宠物的一句话聊天：按 agent 走路由，限 400 token / 10 次每分钟
nodus/db.py           SQLite：所有读取都按 workspace 过滤；工作目录写锁；`runs_in_window`
                      是「今天取哪些行」的唯一 SQL（看板、stats、预算闸、终端面板共用）
nodus/config.py       路径、token、secrets、用户配置、当前 profile
```

依赖方向是单向的：

```
config → providers → agents → routes → profiles → runner
      → {tdd, review, fix, report, propose, jobs} → {cli, server}
```

`adapters` 和 `verify` 不依赖上层；`db` 是基础设施，被所有层使用，但不反向 import 业务；
`meter` 只依赖 config + providers。

0.4.0 之后新加的模块大多按"只读/纯函数"摆在链外，这是有意的：`ledger` 只读 `usage_json`，
`lifecycle` 不 import 任何内部模块（纯函数，测试可以只喂字典），`observe`/`harness_status` 只读别人的日志和
run 表，`recover`/`policy`/`budget` 只读 run/task/event。唯一的例外是 `watchdog`：它要写 `run.status`，
但只写 `running -> stalled` 这一条边，而且先过 `lifecycle.can_transition`，不是第二个状态判断。

**同一件事只剩一个定义**，这是 0.5.0 的主干判据（对应 `docs/overlap-audit.md` 的收口）：

- 金额：只经 `ledger.parse` / `cost_of` / `summarize` / `Money`；缺币种进 `"-"` 桶，按来源展示，不跨币种相加。
- 跑着的行的用量：`ledger.sum_events`；落盘的用量：`ledger.parse`。`server._live_rows` 不再自己求和。
- "今天取哪些行"：`db.runs_in_window`。各面板在它上面保留自己的口径（预算闸把无币种当 CNY、看板和 CLI
  叠加观察账、终端面板含跨午夜的 live 行），但不再各写一条 WHERE。
- 失败是什么：`diagnosis`。cause → kind / label / problem / suggestion / advice / fix 是同一行的投影，
  渲染句经 `CAUSE_BY_LABEL` 找回同一行，没有第二张文案表。
- 失败计数：`server.board_state()["decision_counts"]`；状态灯读 `server.attention()`，不自己查库。

### watchdog：没失败，但没在动

`recover` 回答"失败了要不要再试"；watchdog 回答另一件事：状态还是 `running`，但最近 N 分钟没有新的
实质事件（tool_call / artifact / 状态变化）。N 由 `NODUS_STALL_SECONDS` 给，默认 600 秒，比
`runner.IDLE_TIMEOUT` 的 900 秒早：十分钟先让看板说"没在动"，不是等它被记成一条超时失败。

判据不新写一遍：它把最后一条实质事件的 ts 交给 `lifecycle.needs_attention`，只有答案是 `stalled`
才动手。写入顺序是固定的：先 `db.release_write`（和 runner 一样，释放先于状态写入，目录先可用），
再 `UPDATE run SET status='stalled' WHERE status='running'`（先过 `lifecycle.can_transition`，晚到的
完成/失败只记日志、不改），最后追加一条 `run.stalled` 事件，写明最后事件和 idle 时长。

同一条 run 只标一次：sweep 只看 `running`，已经是 `stalled` 的行（包括上一个 daemon 留下的）不会再处理，
所以重启不重复。daemon 里隔 60 秒扫一次（`NODUS_WATCHDOG=0` 关掉），只查 live 行。

worker 分三种处理：本 daemon 拥有且已经有过 tool_call 的进程树会被 kill；没有 tool_call 的不杀，交给
runner 自己的 900 秒 idle kill；别的进程拥有的不碰。后两种如实写进事件的 `worker` 字段，不假装已经停掉。
没有 tool_call 不杀的原因很具体：runner 的 fallback 规则把"没 tool_call + 非零退出"当成上游故障，会换
下一个 endpoint 再花一笔钱——那正好是 watchdog 要避免的。

被 kill 的那一种有个代价，写在明处：runner 会按自己的收尾把 run 写成 `failed`（`stalled -> failed` 是
状态表里的合法边），所以这条 `stalled` 只活到 runner 收尾为止，最终态是 failed，后面的重试判断交给
`recover`。事件流里 `run.stalled` 在 `run.failed` 之前，卡住的原因不会丢。另外两种 worker 才会让
`stalled` 一直留到人或 runner 的 900 秒 kill。

看板上还在 `stalled` 的行进"已停止"：它不是失败（没有 error、exit code 或 failure class），也不该进
"需要你决定"（决策只有合并、花钱、方向三类）或"我在处理"（那是机器正在做的事，卡住是没人在做）。它只加
`stopped` 的行数，不新增计数，`failed` 仍是终态失败的数；`attention()` 与状态灯因此不变，宠物不会为
一次卡住说话。

**这条依赖链是这次重构的判据。** 0.3.0 里 `meter._relay()` 要反读 `AgentSpec.config_home` 下的
`settings.json` 才能拿到 base URL 和 token——那说明"端点"这个概念不存在，事实寄居在别处。
现在 `providers` 在 `meter` 的上游，同样的事实只有一个家。

### 为什么 CLI 不直接 import 业务

`nodus run` 走 HTTP；`nodus tdd/review/fix` 走 `POST /pipelines`，由 daemon 起线程跑（ADR-016，取代
ADR-004 的"流水线在 CLI 进程里跑"）。两种路径都只通过 `db` 落状态，所以 `nodus watch` 从另一个进程也能看到。
代价写在 ADR 里，不藏。

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
| 新状态 / 新"要不要人管"的判断 | `lifecycle.py` 加状态和边；调用方传 facts | 四个各自判状态的模块 |
| 新成本口径 | 只在 `ledger.py` 加；不要再解析 `usage_json` | CLI、看板、报表 |
| 新终端（harness） | `harness_status.py` 加一个 `Harness`；`agents.py` 里已有就能跑 | 看板 |
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
| 会话读取 | 可选 `session_roots` 白名单；默认不限制但每次显式指定会话 | `sessions._allowed` |
| 会话写入 | 单写者文件锁 + 会话 cwd 校验 + 120s 新鲜度守卫 | `sessions.session_lock` |
| 进程 | agent 以 `bypassPermissions` 跑，cwd 是唯一边界 | `agents.argv` |

已知缺口（不粉饰）：

- Windows 上 token 文件没有 ACL 加固，`chmod` 是 no-op。
- 没有文件读取审计；review 读哪些文件只记录在 `context.md`。
- 会话锁是建议性的：绕过 Nodus 直接跑 `dsh --session-id` 不会看这把锁。
- agent 能执行任意命令，这是产品定位决定的，不是遗漏。
- codex 的 `experimental_bearer_token` 写在它自己的 config.toml 里（codex 只能从配置读），
  那是用户私有目录，和 0.3.0 一样，没有变好也没有变坏。

## 5. ADR（架构决策记录）

状态：ADR-001 至 ADR-017 全部已实现；ADR-004 已被 ADR-016 取代，保留原文作为历史。
ADR-018 是本次新增（只建立定义，未迁移调用方）。

**ADR-001 SQLite + WAL，单写者。已实现。** 单机单用户，状态量小，事务简单；WAL 让读不阻塞写。并发上限是真实的：10 个以上同时写没测过。

**ADR-002 文件交接，不共享对话。已实现。** 跨 agent 传的是 handoff.md（确定性模板）+ artifact，不传聊天记录。好处是上下文可预算、可审计；坏处是长任务需要人补「未决」。

**ADR-003 审查是建议，不是判决。已实现。** review 的 verdict 只影响任务状态降级和是否触发 fix；它不能替代测试。理由：LLM 审查会误报，也会漏报，门禁必须可执行。

**ADR-004 流水线在 CLI 进程执行。已被 ADR-016 取代，不再成立。** `nodus tdd/review/fix` 直接在命令行进程里起 agent，不经过 daemon 队列。好处是终端能实时流、Ctrl-C 即停；坏处是关终端会中断流水线，且 `nodus stop` 管不到别的进程里的 run。这条代价就是 ADR-016 要修的问题，已经修完。

**ADR-005 会话桥只读 + 显式续写。已实现。** DSH 没有会话间消息通道；往 append-only 存储里注入会破坏 GUI 的 live state。所以读走文件、写走 `--session-id` 新 turn，并要求同 cwd + 单写者。

**ADR-006 一个 CLI = 一个 spec + 一个 parser；一个端点 = 一条数据。已实现。** 除 `agents.py` 和 `adapters.py`
两处外，代码里不出现 CLI 名字；除 `providers.py` 的默认表外，代码里不出现厂商 URL。
0.3.0 把这两件事混在 `AgentSpec` 里，代价是加后端要写 Python，`minimax` 这个"agent"其实是
"claude CLI + minimax 端点"的粘合体。见 ADR-009。

**ADR-007 后端可换，壳不用换。已实现。** MiniMax 提供 Anthropic 兼容端点，Claude Code 的 stream-json 契约不变，
所以在 0.3.0 里它的 `vendor` 字段是 `claude`——同一个 CLI、同一个 parser、同一个 runner，差别只在
base URL、凭据和价目。0.4.0 把这个"差别"从 `AgentSpec` 的一个副本提升成 `Provider` 这个类型：
`claude` 只是 CLI，`relay` 和 `minimax` 是两个端点。
代价：`~/.claude/settings.json` 的 `env` 块优先级高于进程环境变量，换端点必须让配置文件里没有 `env`
（0.3.0 的做法是换 `CLAUDE_CONFIG_DIR`，实测报 401 并退避重试 10 次、烧掉 190 秒）。
0.4.0 改成"生成的 settings.json 永远不含 env，端点只从进程环境注入"，并留了一条测试盯着它。

**ADR-008 成本读表，不读自述。已实现。** 成本有四个来源，优先级 metered_request > metered > list_price > reported。metered_request 是本地网关 tail 各 agent 的会话文件、按会话 id 算出每次调用的成本（精度 1e-6，且不受并发污染）；metered 是在 run 前后读 provider 计费接口求差（中转站累计消耗、DeepSeek 余额）；list_price 是 token × 公开价目，只有确认对方按价目扣费时才等价于实付；reported 是 CLI 自己算的账，只做兜底。
理由：CLI 的价目表是别人的（Claude Code 用 Anthropic 价格给任意后端计价），中转站还会叠自己的分组倍率，两者都推不出来，只能读表。实测同一个 run：CLI 自报 0.2627，实付 0.3230（站内余额），比值 1.23；换一次模型组合变成 1.44。
理由二：余额差有精度下限（DeepSeek 只有 0.01 元）和并发污染，网关按请求读两个问题都没有。实测同一个 dsh 任务：网关 0.001386 美元（4 次调用），余额差是 0.09 元或 0——差两个数量级。
代价：每个 run 多一次前置 HTTP 和最多 4s 的落账等待（走网关再加最多 6s）；余额差的窗口会重叠，`meter.window_s` 记录窗口但不消除重叠。中转站"站内余额"到人民币的换算比例是账户事实，API 不暴露，放在 `config.json` 的 `cost_rates` 里；网关的成本是美元（它自己打印 `≈$`），靠 `currency` 字段区分。
0.4.0 的补充：计费挂在 `Provider` 上而不是 agent 上（同一个中转的两个协议入口共用一本账，
所以 `_relay()` 会把 base_url 末尾的 `/v1` 剥掉再请求账单），并且**按 attempt 各记一笔**，
`usage.attempts[]` 留全部，顶层只放产出结果的那一次。

**ADR-009 三层：CLI、端点、路由组。已实现。** 三者变化的原因不同（CLI 升级 / 端点故障或换价 / 任务该走哪条路），
所以是三个类型：`AgentSpec`（可执行 + 方言 + 流格式）、`Provider`（协议 + 地址 + 凭据名 + 单价 + 计量口径）、
`Route`（一个 CLI + 有序 provider 链）。`Route` 还解决了一个命名骗局：`--agent minimax` 里的
`minimax` 从来不是 agent，现在它是 route 名，指向 `claude` CLI 的 `minimax` 端点。
理由：只要"端点"不是第一类对象，failover、价目、探活、计费口径就都只能寄生在别处，改一处漏一处。
代价：`run` 表多了 `route` / `provider` / `profile` 三列；`usage` 的分组从 agent 改成 route，
历史行没有 route 就回落到 agent 名。

**ADR-010 回退只在"什么都没做"时发生。已实现。** 换端点重跑的唯一正当理由是上一次尝试没有产生任何副作用。
判据是**事件流里有几个 `tool_call`**，不是花了多少时间、也不是退出码：
有 tool_call 就算上游 401 也不换（重跑会把同样的改动做第二遍），没 tool_call 且错误像上游问题
（401/403/429/5xx/连接/超时/限流）才换。CLI 打在 stderr 上的非 JSON 噪声也进判定，
因为连接失败常常只出现在那里——0.4.0 修 DSH 的 `status.turn_end.reason` 时顺手补的。
代价：回退不是静默的（CLI 打 `!!`、交接单写 `回退：A → B`、run 记录只留最终 provider），
而且慢——CLI 自己的重试预算先耗掉，实测死端点那次整轮 187 秒。回退救结果，不救时间。

**ADR-011 profile 是"生成"的，不是"手改"的。已实现。** 每个 (profile, agent, provider) 一个配置目录，
内容由两层生成：手改的 `profiles/<p>/<agent>.json` 覆盖层 + provider 的地址/凭据/模型映射。
理由：Claude Code 从 JSON 的 `env` 读端点，Codex 从 TOML 的 `experimental_bearer_token` 读，
两条路径各写一遍必然漂移；生成让"provider 是唯一真相"在两家都成立。
目录按 provider 分开，是为了两个 run 用不同端点并发时不互相覆盖配置（同输入产出同字节，不同输入不同目录）。
代价：切换 profile 只影响新起的 run；配置目录里 CLI 自己写的东西（会话、onboarding）
跟着 (agent, provider) 走而不是跟着人走。

**ADR-012 一个目录一个写者，锁跟着 run 走。已实现。** 两个 run 同时写一棵树，后一个的编辑会盖在前一个的半成品上，
两份交接单描述的状态都不存在。约束落在数据库里：`write_lock` 表 + `WHERE released_at IS NULL` 的部分唯一索引，
第二个 INSERT 失败而不是两个 worker 悄悄覆盖。行和锁同一事务插入，避免"有锁没有 run"的窗口被误判成持有者已死。
释放先于状态写入，所以 `wait()` 返回"完成"就保证目录可再用——`nodus tdd` 紧接着就起下一个 run。
代价：`review`/`fix` 会借走目标目录，目标 run 还在跑时会被拒；这是对的（审查一份正在变的树本来就没有意义），
但报错信息必须点名持有者，否则用户会以为是 Nodus 坏了。

**ADR-013 会话是能力，不是 DSH 的附属。已实现。** 查询、读取、接续、分支四个动作对三家都成立，
差别只在 CLI 的语法和存储格式，所以有 `nodus/sessions/` 一层，每个厂商一个适配器，上层只认 `Session`。
接续必须回到**写这个会话的那个端点**：`--resume` 在 endpoint 自己的配置目录里查 id，换 home 就变成
"No conversation found"（0.4.0 实测踩到过一次，报错原文就是这句）。所以会话对象带 `profile` + `provider`，
由路径反推；不在我们 profiles 里的会话（用户自己的 `~/.claude`）只能读，接续会明确拒绝。
分支有两种质地，不假装一样：claude/codex 是真 fork（新 id，历史原样），dsh 没有原生 fork，用 brief 播种。
代价：三家的 id 空间没有统一（dsh 是 `session-<uuid>`），所以 `find` 支持 `vendor:id`，并且只把**已知厂商名**当限定符——
否则 `session:` 会被误读成厂商。codex 一个 thread 可能写多个 rollout 文件，扫描时按 id 取最新的一份。

**ADR-014 改过文件的 run，关之前自己验一次。已实现。** `completed` 一直只等于"进程退出 0"，而这个产品的全部主张是
验收只认可执行证据。检测器已经知道四种标准测试命令，那就在 run 关闭前跑一次，落 `verify.green|red`，
和 tdd 共用一套证据。三条边界：没改文件不跑、检测不到已知命令不跑（**不猜**，在没人看过的仓库里跑未知测试
是少数能真正破坏工作的行为）、`--no-verify` 单次关掉。
代价：run 多花一次测试的时间（实测 3.8 秒），这段时间它仍算 `running`、写锁仍握着；测试挂住由
`NODUS_VERIFY_TIMEOUT`（默认 600 秒）兜底。验收从"事后可选"变成"默认发生"，这是这次改动唯一不可逆的地方。

**ADR-015 提案把"不能并行"这半个问题补上。已实现。** 写锁只解决互相覆盖，代价是同一棵树只能有一个写者；
而"协作"的另一半是同时干。解法是复用 git 已有的机制，而不是发明一套：每个 run 在 `git worktree` 里跑，
分支 `nodus/<run-id>`，结束时提交并落一份 patch + `proposal` 行，合并时在主树的写锁下 `git merge`。
这样做能成立的原因很具体：**worktree 路径就是另一个 cwd**，现有写锁立刻从"保护主树"变成"保护这条分支"，
并行不需要放宽任何规则，而"拿主树的锁"本身就成了 merge 的语义。这就是 ADR-012 里说的"写锁是地基不是终点"。
代价：提案必须有 git（没有 git 就明确拒绝，而不是偷偷退回串行）；冲突不自动解决，merge 失败就 abort、
提案留 open、主树不动；空提案不进列表，worktree 直接删——否则列表会被噪音训练成没人看。

**ADR-016 流水线归 daemon，不归终端。已实现。** ADR-004 承认的代价是"关终端会中断流水线、`nodus stop` 管不到"。
daemon 既然已经拥有 run，就该拥有 run 的序列：`nodus tdd/review/fix` 改成 `POST /pipelines`，
daemon 起线程跑同一批函数，CLI 只跟随（`pipeline.child` 事件告诉它该去看哪个 run 的流）。
行是持久的、线程不是——所以 daemon 重启会把 `running` 的流水线标失败，和孤儿 run 一个道理，不复活。
`nodus stop` 现在同时认 run id 和 pipe id。代价：多一张 `pipeline` 表和一跳 HTTP；
CLI 的进度靠轮询（0.5s），不是 SSE。

**ADR-017 成本用标定的 token 单价，不用账户余额差。已实现。** ADR-008 的 `metered` 档有个默认的洞：
余额差是**账户级**的，两个 run 并发时看到的是同一笔钱，合计必然偏高。修法不是排队（那会毁掉并行），
是给每个模型测出单价：固定模型、串行跑、每次只改一种 token 形状，读账户表前后差，最小二乘解系数。
opus 拟合残差 0.1%，留出验证 0.3%，多调用大 cache 的干净核对 0.8%，所以在成本阶梯里排在 `metered` 之上。
依据是**实验**而不是 relay 的价目表：同一张表套进经典比例公式联立，缓存写系数解出负数——
表是真的，公式不是。sonnet 用同样方法得到 13.7% 残差和负截距，因此**不发布**，继续走余额差。
代价：系数来自本账户本时刻，relay 调价就漂；所以每个 run 同时存 `meter_observed`，`nodus cost` 能看出漂移。
重新标定是 `scripts/calibrate_relay.py`，系数不合格时脚本拒绝输出。

**ADR-018 状态和"要不要人管"只有一个定义。已实现（仅定义，未迁移调用方）。**
状态这件事在 0.4.0 之后分成了四份：`runner` 只写 `completed`/`failed`，`recover` 自己从 error 文本判
可重试性并给出 gate，`server` 把同一批失败分成 needs_you / handling / stopped，`lineage` 又按
"completed + verify" 判 ok/unverified。四份都能跑，但"这条 run 到底算不算结束""要不要叫人"没有单一答案。
解法不是再写一个第五份，而是把合法状态、合法转移和四种注意力答案写死在一个纯函数模块里
（`nodus/lifecycle.py`）：run 的 `queued → running → {completed, failed, stalled}`（`stalled → running | failed`），
proposal 的 `open → {merged, rejected}`；`needs_attention` 只读调用方传进来的 facts，不查库。
理由：纯函数可以在测试里把 recover 的 gate、lineage 的 ok/unverified、看板的三组行都抄出来对照，
四个模块互相矛盾时测试会红；而如果把判断做成服务或在各模块里继续加 if，矛盾只会更深。
代价：本提交只建立定义，**没有迁移任何调用方**，现有行为一字未改；`docs/overlap-audit.md` 记录了
谁还在自己算、以及建议留谁退谁。当时 `stalled` 没有任何 writer——900 秒无输出仍被 runner 记为
`failed`（cause=timeout）。2026-11-03 的 `watchdog` 补上了这条边（见第 2 节），但 recover/server/lineage
仍各自判断状态，迁移没有发生。

## 6. 质量门槛

- `python -m pytest -q -o addopts=""`：880 条（2026-11-03，0.5.0；仓库 addopts 已含 `-q`，必须覆盖 addopts 才看得到摘要），
  覆盖解析、策略、隔离、写锁、提案/合并（真 git 仓库）、会话、自动验收、daemon 作业、成本标定、报告、计费表、
  端点/路由/配置物化、自动恢复、状态机与真实历史回归、卡住检测，以及回退规则（真起子进程，不是 mock）。
- `scripts/smoke_cli.ps1`：命令逐个 `--help` + 关键命令真实执行。
- 每个新能力都要有一次真实调用记录：0.4.0 的四条 route（relay / minimax / relay-openai / deepseek）
  各跑过一次真任务，failover 用一条指向死端口的 route 实测过（`run-90c6a47c9bae`）。
- `docs/overlap-audit.md`：同一件事被多个模块各算一遍的清单，带文件:行证据；改之前先看它。

## 7. 还没做的

已经做完、从待办里移出的（2026-11-03 校准）：

- daemon 作业队列 —— ADR-016 已实现：`jobs.py` + `pipeline` 表，`nodus tdd/review/fix` 走 `POST /pipelines`。
- UI / SSE —— 已实现：`server._sse`（`text/event-stream`）+ `ui.py` 的看板；CLI 侧 `pipeline.py` 仍轮询（0.5s），
  这是客户端选择，不是缺 SSE。
- 多写者 propose/merge —— ADR-015 已实现：`propose.py` + `proposal` 表 + `git worktree` 分支，merge 在主树写锁下做。
- 卡住检测 —— `watchdog.py` 已实现：`running` 且 N 分钟没有实质事件 → `stalled`，看板进"已停止"；
  默认 600 秒，`NODUS_WATCHDOG=0` 关闭。

还没做：

- 打包（PyInstaller / Go）。
- 多租户鉴权（现在只有 workspace 字段）。
- dsh 的 `DSH_HOME` 隔离：profile 层已经在了，缺的是 dsh 认哪个环境变量。
- 把现有调用方迁到 `lifecycle`：`recover` / `server` / `lineage` 仍在各自判状态，清单见 `docs/overlap-audit.md`。
  已收口的是「谁写状态」（watchdog）和「失败是多少」（board 唯一计数），读方的迁移还没做。
- `sodus` 包接管各模块（现在只是入口面，模块仍在 `nodus/`）；仓库目录改名是阶段 D，单独立项。
