# 重叠审计：谁在把同一件事算第二遍

> 2026-11-03 · 基线 `041d0f9`（`nodus/lifecycle.py` 落地之前）· 只读代码，不改行为。
> 口径：全文的 `文件:行` 都是这个基线上的行号；结论来自逐处读实现，不是跑统计。
> 例外：第 4 节第 5 点引用了 `~/.nodus/nodus.sqlite3` 的只读取样（319 条 run，`ws-default`，2026-11-03 03:15 前后）。

目的：过去几周新加的模块各自把"状态""成本""失败原因"算了一遍，`docs/architecture.md` 第 2 节
只列模块名，不解释谁和谁在抢同一件事。这份清单把重复算的地方钉到行号，给出"留谁 / 退谁 / 合并到哪"，
**一条都不在本提交里改**。`nodus/lifecycle.py` 只把状态和注意力判定写成一份定义，现有调用方一个没动。

## 1. 结论

- 共 **20 条**重叠（成本 5、状态 5、失败分类 4、观察 2、其它 4）。
- 其中 **1 条是今天的真实矛盾**（C1：缺货币时 `ledger` 记 `"-"`，`telemetry` 记 `"USD"`），
  **1 条是同一页面上三个不同的"失败数"**（S3），**1 条是同一段报错被四套词表各分一次类**（F1）。
- 状态机的结构性问题（S1：没有任何 writer 会写 `stalled`，但 900 秒无输出事实上就是 stalled）
  今天是"规则散落"而不是"答案冲突"；`lifecycle` 把目标状态写出来了，迁移仍是单独的行为改动。

## 2. 最强的三条

### #1 C1 缺货币时两个模块给出两个币种

`ledger` 的规矩写在模块 docstring 第一段：缺字段是 `None`，绝不补默认值；没记币种的金额进 `"-"` 桶
（`nodus/ledger.py:1-26`、`nodus/ledger.py:38`）。`telemetry.usage_summary` 自己解析 `usage_json`
（`nodus/telemetry.py:107`），并在缺币种时写 `currency = usage.get("currency") or "USD"`
（`nodus/telemetry.py:133`）。同一条 run，看板/CLI 记在 `"-"`，`telemetry` 记在 `USD`；
这不是展示差异，是两个总额。`ledger` 的 docstring 里点名过这个 7 倍误差的旧 bug，
`telemetry` 是它没覆盖到的第五处。

建议：留 `ledger.parse/cost_of/summarize`；`telemetry.usage_summary` 的统计部分退成
`ledger.summarize(db.list_runs(...))`，`telemetry` 只保留探活（`probe`/`last_probes`/`balance`）。

### #2 S3 同一个页面上三个"失败数"，外加第四个在任务灯里

- `lineage._route_summary` 的 `failed` 是"不是 ok 也不是 unverified"——**completed 但测试红也算 failed**
  （`nodus/lineage.py:284-293`，合计在 `nodus/lineage.py:338`）。
- `server._decisions` 的 `counts["failed"]` 只数 `status == "failed"`
  （`nodus/server.py:1813-1814`），测试红单独进 `counts["red"]`（`nodus/server.py:1798-1799`），
  同一批行在 `_stopped` 里又被分成 `deterministic` / `retry-limit` / `tests_red` 等
  （`nodus/server.py:1643-1741`）。
- `statuslight.snapshot` 又数一遍 `status == "failed"`，而且 `db.list_runs(limit=50)` **没有 `kind="work"`**
  （`nodus/statuslight.py:35-38`），宠物会把 chat 行的失败也算成"跑红了"；看板同一时刻用的是
  `kind="work"`（`nodus/server.py:1983`）。

同一条 completed+red 的 run：`lineage` 说 failed，`server.decision_counts` 说 red 不算 failed，
`statuslight` 完全看不见它。三个数字都能自圆其说，但一个人同时看到会以为系统坏了。

建议：留一个分类函数（`lineage._run_summary` 的三桶，或 `lifecycle` 的 terminal 事实）；
`decision_counts` 保持旧形状给 MCP/宠物读，但由同一个分类函数派生；`statuslight` 至少补 `kind="work"`。

### #3 F1 同一段 `run.error`，四套词表

| 出处 | 词表大小 | 例子（`no output for 900s, killed`） |
|---|---|---|
| `recover._cause`（`nodus/recover.py:207-225`） | 10 个 cause | `timeout` |
| `recover.classify`（`nodus/recover.py:228-232`） | 2 个（deterministic/retryable） | `retryable` |
| `server._failure_kind`（`nodus/server.py:895-919`） | 5 类 | `超时` |
| `server._CAUSE_RULES`（`nodus/server.py:376-426`） | 15 个标签 | `卡住超时` |
| `server._outcome`（`nodus/server.py:770-788`） | cause 或 `exit N` 或首行 | `timeout` |

`exit code 1` 更典型：`recover` 归 `unknown`（仍算 retryable）、`_failure_kind` 归 `进程退出`、
`_CAUSE_RULES` 归 `进程自己退出`、`_outcome` 输出 `exit 1`。四套词表各自有测试，所以都不算错；
问题是加一个新错误模式要改四处，漏一处就出现"同一行在 A 区块叫 X、B 区块叫 Y"。

建议：留 `recover._cause` 作为机器的唯一分类（它就是重试决策的输入）；`server` 的三个展示词表
从一个 `{cause: (kind, label, advice)}` 表派生，且加一条测试断言"每个 cause 都有展示项"。

## 3. 全部条目

### 3.1 成本（5 条）

| 编号 | 谁在算同一件事 | 证据（文件:行） | 差异 / 风险 | 建议 |
|---|---|---|---|---|
| C1 ★ | `ledger` vs `telemetry.usage_summary` | `nodus/ledger.py:1-26`、`:38`、`:211`、`:356`；`nodus/telemetry.py:94`、`:107`、`:133` | 缺币种：`"-"` vs `"USD"`；`telemetry` 还自给 `cost_basis="reported"`（`:135`） | 留 `ledger`；`telemetry` 退成薄壳，只留探活 |
| C2 ★ | `server._today` / `usage._rows` / `budget.spent` / `harness_status.by_harness` 各自定义"今天" | `nodus/server.py:1853-1872`、`:1875-1882`；`nodus/usage.py:53-72`；`nodus/budget.py:53-73`；`nodus/harness_status.py:206-241` | 行集不同（`server._today` 只取 `agent/route/usage_json`，`usage._rows` 取 status/provider/model）；无币种的钱 `budget` 当 CNY、看板留 `"-"`；观察账进"今天的钱"（`server.py:1871`）但不进终端面板（`harness_status.py:11-25` 明说不折进去） | 算术留 `ledger`（已经是）；"今天取哪些行"合并成一个 helper；每个面板标自己的口径 |
| C3 | `server._live_rows` vs `ledger.parse` | `nodus/server.py:110-152`；`nodus/ledger.py:159` | 一个读 `usage` 事件（在跑，partial），一个读 `usage_json`（已结束）；两套 token 求和 | 事件求和移进 `ledger`（`Tokens` 已有），保留 "partial" 标记 |
| C4 | `policy._usage_cost` vs `ledger.UNKNOWN_CURRENCY` | `nodus/policy.py:797-811`；`nodus/ledger.py:38` | 已改为委托 `ledger.cost_of`，只把 `"-"` 改名 `"unknown"` 打印 | 保持（已收敛）；若 `usage.py` 的 JSON 消费者也改名就一起退 |
| C5 | `observe_ccswitch.estimated_usd` vs `ledger` 的分币种规则 | `nodus/observe_ccswitch.py:47`、`:61-65`、`:133` | CC Switch 的 rollup 是 USD，缺价模型记 0 并单独计 `zero_cost_requests`；与 `ledger` 的"不跨币种相加"一致但自成一套 | 保持只读探针；若并入看板，必须先有汇率口径，不能直接加总 |

### 3.2 状态（5 条）

| 编号 | 谁在算同一件事 | 证据（文件:行） | 差异 / 风险 | 建议 |
|---|---|---|---|---|
| S1 ★ | run 状态由谁定义 / 谁写 | `nodus/db.py:44`（默认 `queued`）、`:637`（终态判断）、`:757`（live）、`:771`（stale）；`nodus/runner.py:956`（唯一 writer）、`:796`（900s 无输出→failed）、`:1123`（wait 终态）、`:1184-1186`（reconcile 900s）；`nodus/server.py:119`；`nodus/recover.py:809`；`nodus/harness_status.py:49`；`nodus/pet.py:74` | `stalled` 没有任何 writer；900s 无输出被记成 `failed(timeout)`，语义上就是 stalled；7 处各自写 `IN ('queued','running')` / `("completed","failed")` | `lifecycle` 是状态表；读方逐批迁移；写 `stalled` 是单独的行为改动 |
| S2 | "这个任务还有没有在跑"两份 SQL | `nodus/recover.py:787-799`（`_has_retry_child`）、`:802-814`（`_task_has_live_run`） | 与 `db.live_runs` 同源不同写法，状态字面量再抄一遍 | 留 recover 的 gate；liveness 查询收敛到 db/一处 |
| S3 ★ | "失败"的计数口径 | `nodus/lineage.py:284-293`、`:338`；`nodus/server.py:1798-1799`、`:1813-1814`、`:1643-1741`；`nodus/statuslight.py:35-38` | 见第 2 节：completed+red 在 lineage 算 failed、在 server 算 red、在 statuslight 不可见；statuslight 还漏了 `kind="work"` | 一个分类函数；旧字段保留形状但派生；statuslight 补 kind |
| S4 | `ok` / `unverified` 的定义抄了四份 | `nodus/lineage.py:250-275`；`nodus/policy.py:855-860`、`:894`、`:914-915`；`nodus/desk.py:83`；`nodus/server.py:188-216`、`:1833-1837` | 今天四处一致（completed+ok=成功，completed+none=未验证）；但口径散着，改一处就漂 | 留 `lineage` 的三桶；policy/desk/server 调它或调 `lifecycle` 传出来的 facts |
| S5 | proposal 状态的合法值没有地方校验 | `nodus/db.py:1011-1015`（`close_proposal` 接受任意字符串）；`nodus/propose.py:162`、`:178`（只写 merged/rejected）；`nodus/db.py:966`、`nodus/server.py:1484`、`:1782`、`nodus/statuslight.py:28`（只读 open） | 今天唯一写者是 propose，但 db 层不设防，手滑写 `merge` 会让 proposal 从所有列表消失 | `lifecycle` 的 proposal 表先立着；校验放进 `close_proposal`（行为改动，单独做） |

### 3.3 失败分类（4 条）

| 编号 | 谁在算同一件事 | 证据（文件:行） | 差异 / 风险 | 建议 |
|---|---|---|---|---|
| F1 ★ | 同一段 error 的分类 | `nodus/recover.py:207-232`、`:125`；`nodus/server.py:376-426`、`:770-788`、`:895-919` | 10 cause / 2 class / 15 label / 5 kind / 1 outcome token，见第 2 节 | 留 `recover._cause`；展示层从一张 `{cause: …}` 表派生 |
| F2 | 同一个 cause 的中文文案 | `nodus/recover.py:127-151`（`_PROBLEM`/`_SUGGESTION`）；`nodus/server.py:1083-1174`（`_CAUSE_FIX`/`_CAUSE_ADVICE`/`_next_step`） | 两套中文句子按 cause 各写一遍，措辞已经不同 | 合并成一张 `{cause: {machine_sentence, action}}`，机器句归 recover，动作句归 server |
| F3 | 同一批错误正则 | `nodus/recover.py:82-122`；`nodus/server.py:353-369`、`:376-426` | timeout/model/auth/网络/进程退出各抄一遍（timeout 两边几乎同字） | 一张编译好的表，两个模块 import |
| F4 | "这次 run 到底做了没有" | `nodus/recover.py:276-290`（tool_call 数 + 改动文件）；`nodus/server.py:874-892`（tool_call/tool_result/error 事件 + change/tests） | 只有 `error` 事件的 run：`_evidence_gap` 认为有证据，`recover.progress` 认为 `barely` | 一个"已记录操作"读法（事件前缀 + artifact），两处共用 |

### 3.4 观察（2 条）

| 编号 | 谁在算同一件事 | 证据（文件:行） | 差异 / 风险 | 建议 |
|---|---|---|---|---|
| O1 ★ | 读 harness 会话日志、回答"在不在跑 / 今天多少" | `nodus/observe.py:36`、`:134`、`:257`、`:261`；`nodus/harness_status.py:93`、`:118`、`:206-241`、`:244` | `observe` 写 observation 并在"今天的钱"里计入（`server.py:1871`、`usage.py:71-72`）；`harness_status` 只读 run 表，docstring 明说不折入观察账，于是"五终端加起来"≠"今天的钱" | 保持两问，但在面板上写明口径；或把 observation 折进 per-harness 并单独标注 |
| O2 | 第三方用量日志的探针 | `nodus/observe_magpie.py:60`；`nodus/observe_ccswitch.py:61-65`、`:133-134` | 两个文件都只读、都不写 observation；token 字段映射与 `observe._observation` 类似，币种是 USD | 保持探针地位；若将来入账，复用 `observe` 的 token 归一化 |

### 3.5 其它（4 条）

| 编号 | 谁在算同一件事 | 证据（文件:行） | 差异 / 风险 | 建议 |
|---|---|---|---|---|
| X1 | `_first_line` / `_project` | `nodus/server.py:219-232`；`nodus/recover.py:1182-1190` | 逐字重复的小工具 | 收进一个 util（或 db 旁边的 text helper） |
| X2 | 看板要的 task facts | `nodus/server.py:247-266`（`_task_facts`）；`nodus/recover.py:1162-1180`（`_facts`） | 同一次刷新各查一遍 task，只为拿标题/项目 | 一个 `{task_id: facts}` 构建器，两边共用 |
| X3 | "为什么没有测试证据" | `nodus/server.py:1323-1350`（`_unverifiable_reason` 自己判断有没有测试入口）；`nodus/verify.py:28-55`（`detect`）；`nodus/config.py:157`（`auto_verify`） | server 在 `detect` 之上又写了一遍"有入口但没跑 / 没入口"的分支和 `NODUS_AUTO_VERIFY` 文案 | 让 `verify` 返回原因句，server 只排版 |
| X4 | 宠物灯自己数 | `nodus/statuslight.py:23-38` | 与看板 `_decisions` 并行维护两个计数；`db.list_runs(limit=50)` 未过滤 `kind`，chat 失败会触发"跑红了" | 直接读看板 `decision_counts`（或至少补 `kind="work"`） |

## 4. 写 lifecycle 时发现的"定义 vs 现状"边界

这些不是 bug，是必须知道才能读懂 `needs_attention` 的前提。都记在这里，不在本提交改：

1. **`stalled` 是目标状态，不是现状。** 900 秒无输出时 runner 直接 kill 并写 `failed`，cause 是 `timeout`
   （`nodus/runner.py:795-800`）；`reconcile` 对死掉的 owner 也是写 failed（`nodus/runner.py:1184-1186`）。
   `lifecycle` 给出 `running → stalled` 这条边，但没有任何 writer。回归里 20 条真实 run 没有一条
   `status="stalled"`，所以测试只断言"认这个状态"，不断言"能观察到它"。
2. **确定性失败在 lifecycle 里是 `done`，不是 `awaiting-human`。** 看板的 `_needs_you` 明确跳过
   deterministic（`nodus/server.py:1469-1470`），把它放进 `stopped`；`lifecycle` 跟随这个规则。
   "换密钥"是人的动作，但不属于"这条 run 还需要谁"的分类——如果把 deterministic 算成 awaiting-human，
   看板和 lifecycle 会立刻给出两个答案。
3. **proposal 不是 run。** `needs_attention(run, …)` 只回答 run。一个 run 后面挂着 open proposal 时，
   看板把它算进 `needs_you`（`nodus/server.py:1484-1485`），而 run 本身可能已经 `completed → done`。
   两者不矛盾（不同实体），但任何"照 lifecycle 重画看板"的迁移都必须把 proposal 那一行列出来；
   当前真实库里 open proposal 是 0（4 merged / 1 rejected）。
4. **`done` 不看 verify。** `completed` 是退出码；`ok`/`unverified` 是 `db.verify_states` 与 `lineage` 的读法。
   lineage 把 completed+red 归到自己的 `failed` 桶（`nodus/lineage.py:284-293`），那是血缘表的语义，
   不是 run 的状态。两边都说"结束了"，只是 lineage 的 `failed` 是"这次不算成功"。
5. **真实历史回归没有发现矛盾。** 20 条只读快照（`~/.nodus/nodus.sqlite3` → 临时副本）上，
   `lifecycle.needs_attention` 与 `recover.retry_plan` 的 gate、`lineage.for_task` 的 ok/unverified、
   `server.board_state` 的 needs_you/handling/stopped 三组归属全部一致（0 mismatch）。
   样本是 6 条 completed（verify 均为 none，lineage 都判 unverified）+ 14 条 failed：当前判定 17 `done` +
   2 `awaiting-human`（`cost-cap`）+ 1 条在失败当时 `retry-pending`（`backoff`，现在已 too-old → done）；
   看板那一侧命中 2 条 `needs_you` + 4 条 `stopped`，其余 14 条不在看板的 50 行窗口内。
   待重试那一档必须用"该 run 失败当时"的 gate（`now=ended_at+1`），因为 `too-old` 会随时间改变答案。
   verify=ok 的真实行不在这次样本窗口里，它由 `tests/test_lifecycle.py` 的 lineage 单测覆盖（ok 与
   unverified 都映射到 `done`）。
6. **唯一一条真矛盾是 C1（币种）**，其余是重复与口径差。C1 没有在本次修：`telemetry` 还有探活职责，
   改它要动 CLI 输出，属于下一批。

## 5. 不在本提交做的事（写给下一个改的人）

- 不迁移 `recover` / `server` / `lineage` 到 `lifecycle`：迁移会改行为（至少是错误处理路径），
  而这次验收要求 724 条旧测试全绿、代码不变。
- 不改 `telemetry.usage_summary` 的 USD 默认值：它是 C1 的修法，但会改 `nodus status`/报表的输出。
- 不给 `close_proposal` 加校验：会让现有写者之外的手工 `UPDATE` 失败，需要单独评估。
- 不把 `stalled` 写进 `run.status`：需要 runner、`recover`、看板三处同时理解它，是一次迁移。
- 不给看板换新的计数口径：`decision_counts` 仍被 MCP 与桌面宠物读（`nodus/mcp.py`、`nodus/pet.py`），
  先保留形状。
