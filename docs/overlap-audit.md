# 重叠审计：谁在把同一件事算第二遍

> 2026-11-03 · 基线 `041d0f9`（`nodus/lifecycle.py` 落地之前）· 只读代码，不改行为。
> 口径：全文的 `文件:行` 都是这个基线上的行号；结论来自逐处读实现，不是跑统计。
> 例外：第 4 节第 5 点引用了 `~/.nodus/nodus.sqlite3` 的只读取样（319 条 run，`ws-default`，2026-11-03 03:15 前后）。
>
> **2026-11-03 收口**：F1（提交 `8717a3b`）、C1（提交 `af659e7`）与 S3（提交
> `b4568cd`）已修，见 1.1。下面第 1–5 节保持 `041d0f9` 基线的原文与行号，不再
> 回改；新增的收口记录单独成节。
>
> **2026-11-03 第二批收口**：C6（`recover` 三处自己解析 `usage_json`）与 C7（`usage --days`
> 的费用依据缺省词）已修，见 1.2。C6 是这轮扫描新发现的条目，不在上面的 20 条里。
>
> **2026-11-03 第三批收口**：S1（`stalled` 没有任何 writer）已修，见 1.3。
>
> **2026-11-03 第四批收口**：`report` / `pet` / `bench` / `desk` / `lineage._route_summary` /
> `cli/commands/system.py cost` 这六处自己算钱的显示已改走 `ledger`，缺币种不再当作某币种、
> 也不跨币种求和。这是**行为改动**，见 1.4。

目的：过去几周新加的模块各自把"状态""成本""失败原因"算了一遍，`docs/architecture.md` 第 2 节
只列模块名，不解释谁和谁在抢同一件事。这份清单把重复算的地方钉到行号，给出"留谁 / 退谁 / 合并到哪"，
**一条都不在本提交里改**。`nodus/lifecycle.py` 只把状态和注意力判定写成一份定义，现有调用方一个没动。

## 1. 结论

- 共 **20 条**重叠（成本 5、状态 5、失败分类 4、观察 2、其它 4）。
- 其中 **1 条是今天的真实矛盾**（C1：缺货币时 `ledger` 记 `"-"`，`telemetry` 记 `"USD"`），
  **1 条是同一页面上三个不同的"失败数"**（S3），**1 条是同一段报错被四套词表各分一次类**（F1）。
  C1、F1 与 S3 已在 2026-11-03 收口，见 1.1。
- 状态机（S1）2026-11-03 已收口：`watchdog` 是第一个写 `stalled` 的模块，见 1.3。其余状态读取方仍在
  各自判断，迁移仍是单独的行为改动。

### 1.1 收口记录（2026-11-03）

- **F1 已收口**，提交 `8717a3b`。唯一分类在 `nodus/diagnosis.py`：**13 个 cause、4 个 class
  （transient / deterministic / human / none）、13 个 kind**（由 cause 映射，不另写规则）。
  `recover.classify/cause/problem_text/suggestion_text`、`server._failure_kind/_likely_cause`
  和 UI 的标签全部从这里派生；`tests/test_diagnosis.py` 用 13 条真实报错/事实逐条断言四个入口
  同答一题。旧 `_CAUSE_RULES` 的 14 条标签收敛成 13 条：依赖/PATH/路径四条旧标签并入
  `unknown`，上下文并入 `bad-request`，目标服务没起并入 `network`；四个 deterministic cause
  （auth / bad-request / model-missing / content-policy）和重试门保持原样。F3（同一批正则）
  也随之消失：现在只有 `diagnosis.py` 一张匹配表；F2（中文文案两套）仍在。
- **C1 已收口**，提交 `af659e7`。`telemetry.usage_summary` 不再自己解析 `usage_json`，改用
  `ledger.parse/currency_of`；缺币种的金额统一进 `ledger.UNKNOWN_CURRENCY`（`"-"`），
  不再写成 `USD`。`usage.py` 和 `server._today` 在 `439f40b` 时已经调用 `ledger`，本轮核对
  没有第二份解析。缺币种选择 `unknown` 而不是按 provider 推导：provider 不唯一、历史行没有
  provider，且 `ledger` 已经用 `"-"` 保留了 12 条真实记录；按 provider 猜会把 4.7 元说成
  4.7 美元。`tests/test_telemetry.py` 用同一批 run 对比 `ledger` / `usage.summary` /
  `server._today` 三个读法。
- **S3 已收口**，提交 `b4568cd`。唯一失败计数器是 `server.board_state()` 的
  `decision_counts`：`failed` = 最近 50 条**工作** run 里已经结束且不算成功的（进程失败，
  或跑完但测试红），`red` = 其中"跑完测试红"的子集，两个键故意重叠、不能相加；
  `needs_you` / `handling` / `stopped` / `unverifiable` 是同一份 board 的行数，不再各查
  一次。新增 `server.attention()` 只从 board 取 `needs_you`、`failed` 和 live 行，
  `statuslight.snapshot` 直接调它，不再自己 `db.list_runs(limit=50)`（旧实现数的是 50 条
  **所有 kind** 的 `status == "failed"`：chat 行的失败会说话，花钱项反而看不见）。`lineage.for_task` 保留"该任务全部 run"的口径，`nodus debug lineage` 的标题行改成
  本任务历史并写明分母，`failed` 桶含测试红或还在跑的重试时逐条标出。测试
  `tests/test_board_recovery.py::test_one_failure_number_feeds_board_light_and_task_history`
  用 2 失败（1 条在重试）+ 1 完成断言 board / statuslight / lineage 三个读法同数；
  `::test_a_red_test_is_one_failure_for_board_light_and_history` 钉住 completed+red 这个
  原始矛盾；数字没变时 `statuslight.line` 仍返回空串，不重复说话。

### 1.2 收口记录（2026-11-03，第二批）

- **C6 已收口**，提交 `a405f16`。`recover.estimate_cost` / `_task_remaining` /
  `_over_budget` 三处不再自己 `json.loads(usage_json)`，改调 `ledger.amount_in` /
  `ledger.spent`；`amount_in` 是新增的"一行版 `spent`"，解析仍只有 `ledger.parse` 一处。
  币种读法作为实参写在每个调用点，不在 ledger 里设默认：
  - `estimate_cost`：没记币种的金额算 CNY（旧行为；真实库 323 条 run 里 28 条有价无币种），
    显式写了别的币种的 run 不进均值；
  - `_task_remaining`：同样把没记币种算作预算币种（该函数只支持 CNY）；
  - `_over_budget`：预算写了币种时严格只算该币种；没写币种时按 CNY 读。这是本轮唯一的
    预算结论修正（另一处是 `cost: true` 不再当成 1 元，真实库 0 行）：旧实现在"没写币种"
    时把 CNY 和 USD 直接相加，和同一份 budget 的 `_task_remaining`（按 CNY 读）互相矛盾。
    真实库 0 个 task 有 budget（`task.budget_json` 非空 0 行），所以没有真实 run 的结论
    变化；4 组合成预算 × 全部真实 task/run 对比，`_over_budget` 0 差异。
  验收：323 条真实 run 逐条对比旧/新 `estimate_cost`，0 差异；`pytest` 全绿。
- **C7 已收口**，提交 `a405f16`。`usage --days` 的费用依据缺省值此前是两个词：
  `af659e7` 之前 `telemetry` 写 `reported`（含义是 CLI 自报，错的），之后写 `none`
  （CLI 原样打印英文 key）。现在统一显示 **未标注**：这条 run 没有记录 `cost_basis`
  （字段比最老的行新），既不是 CLI 自报，也不是实测；真正 `cost_basis=reported` 的
  run 仍显示"CLI 自报偏大"。

扫描（`git grep -n "usage_json" -- nodus`，按"读 token / 已是 ledger / 自己算钱"分类）：

| 位置 | 现在 |
|---|---|
| `nodus/ledger.py:1`、`:160`、`:332`、`:435`、`:470`、`:500` | 唯一解析入口（docstring 与 `parse`/`summarize`/`by_agent`/`spent`/`amount_in`） |
| `nodus/usage.py:55` | SQL 取 `usage_json`，交 `ledger.summarize`；本身不解析 |
| `nodus/telemetry.py:116` | `ledger.parse` / `currency_of` |
| `nodus/harness_status.py:233`、`:265` | `ledger.cost_of`（`:265` 只取列） |
| `nodus/mcp.py:289` | `ledger.parse` |
| `nodus/policy.py:805`、`:840`、`:871` | `ledger.cost_of`（`:840` 只取列） |
| `nodus/server.py:1844`、`:1858` | SQL 取列，交 `ledger.by_agent` / `ledger.summarize` |
| `nodus/budget.py:67` | SQL 取列，交 `ledger.spent` |
| `nodus/recover.py:326` | 本提交：`ledger.amount_in` / `ledger.spent` |
| `nodus/report.py:23`、`:114-118` | **自己算钱**：cost，缺币种记 `USD`，缺 basis 记 `reported` |
| `nodus/pet.py:65`、`:81-87` | **自己算钱**：cost 求和，缺币种记 `USD` |
| `nodus/bench.py:239-247` | **自己算钱**：cost，缺币种记 `CNY` |
| `nodus/desk.py:24-38` | **自己算钱**（单条展示）：缺币种当 `$` |
| `nodus/lineage.py:250-271`、`:296-298`、`:448` | **自己算钱**：按 route 求和，币种只取第一条 |
| `nodus/cli/commands/system.py:147-156` | **自己算钱**：`cost` 子命令的标定漂移合计 |
| `nodus/runner.py:1007`、`nodus/llm.py:482`、`nodus/observe.py:317` | 写 `usage_json`，不是读方 |
| `nodus/db.py:51`、`:243` | 建表 / 迁移 LIKE，不是金额 |
| `nodus/ui.py:1401` | 整行 JSON 交给前端，token 展示 |
| `nodus/pricing.py:9`、`nodus/server.py:114`、`nodus/telemetry.py:7`、`nodus/usage.py:3` | docstring / 注释，不是读方 |

**剩余**：`report.py`、`pet.py`、`bench.py`、`desk.py`、`lineage._route_summary`、
`cli/commands/system.py cost` 这六处仍是自己读 `usage_json` 算钱。每处改完都会动用户
可见数字（`report`/`pet` 的缺币种默认 `USD` 就是 C1 修掉的同一个 7 倍错误），留作单独的
行为改动，不混进这次"口径不变"的收口。

> 2026-11-03 更新：这六处已在 1.4 收口（提交 `fefe7cf`）。上面表格的行号保留 `041d0f9`
> 基线位置，不回改。

### 1.3 收口记录（2026-11-03，第三批）

- **S1 已收口**，提交 `b5ef809`。新模块 `nodus/watchdog.py` 是 `running -> stalled` 的
  第一个 writer：daemon 每 60 秒扫一次 `running` 行，把最后一条实质事件（tool_call / artifact /
  状态变化）的 ts 交给 `lifecycle.needs_attention`，只有它答 `stalled` 才动手。默认窗口 600 秒
  （`NODUS_STALL_SECONDS`），比 `runner.IDLE_TIMEOUT` 的 900 秒早：十分钟先让看板说"没在动"。
  写入顺序是 `db.release_write` → `UPDATE ... WHERE status='running'`（先过
  `lifecycle.can_transition`，晚到的完成/失败只记日志、不改）→ 一条 `run.stalled` 事件，写明最后
  事件和 idle 时长。已经是 `stalled` 的行不再处理，重启不重复；同一个 run 只标一次。
  看板把 `stalled` 放进"已停止"（`stop_reason="stalled"`），不新增计数：`stopped` 行数 +1，
  `failed` 不变，`attention()`/状态灯不变（宠物不会为卡住说话）。
  worker：本 daemon 拥有且已经有过 tool_call 的进程树会被 kill；没有 tool_call 的留给 runner 的
  900 秒 idle kill（提前杀会命中 fallback：无 tool_call + 非零退出 → 换 endpoint 再花钱）；
  别的进程拥有的不碰。后两种如实写进事件的 `worker` 字段。
  被 kill 的那一种会由 runner 的收尾写成 `failed`（合法边），`stalled` 只活到收尾为止；事件流里
  `run.stalled` 在 `run.failed` 之前，卡住原因不丢。另外两种才让 `stalled` 持续存在。
  验收：`tests/test_watchdog.py` 17 条全绿；另做了真实验——临时 `NODUS_HOME` 里起两个真 worker
  （一个只输出 init 后挂住，一个正常完成），另一个进程跑 watchdog，`NODUS_STALL_SECONDS=30`。
  第 0 秒 sweep 时挂住的 run idle 29 秒、不动；第 2 秒 idle 31 秒时标 `stalled`，写锁释放
  （`active_locks=[]`），事件是「无实质进展：最后事件是 run.attempt，距今 31 秒」
  （`idle_seconds=30.7`、`window_seconds=30`、`worker="not-owned"`）；正常 run 保持 `completed`，
  没有 `run.stalled` 事件。看板 `stopped` 出现该行，`decision_counts.stopped` 从 0 变 1，
  `failed` 仍为 0，`attention()` 为 `{waiting:0, running:0, failed:0}`。

### 1.4 收口记录（2026-11-03，第四批：六处金额展示）

- **六处已收口**，提交 `fefe7cf`。这是**行为改动**：缺币种的金额不再当成某个币种，
  也不再把不同币种加成一个数。`ledger` 新增 `Money` 收集器与三个展示词：
  `UNKNOWN_LABEL="未标注币种"`、`currency_label()`、`unlabeled_line()`。`Money`
  每个币种一个桶（形状同 `summarize()` 的 `totals["cost"]`），缺币种的按 provider 分桶，
  `unlabeled` 是**列表**而不是总数——两个来源的未标注金额没有共同单位可以相加，所以结构上
  就没有"合计"这个字段可打印。六处各自的变化：
  - `report.py`：`collect()` 不再自己 `json.loads`，token 与金额都读 `ledger.parse`；
    缺币种默认 `USD` 的旧逻辑删除。真实任务 `task-07b9e79c4e68` 的 6 条无币种 run 原来
    报 **`$3.0320`（CLI 自报）**，现在是「未标注币种（来源未记录） 6 次 3.0320」，且不进任何
    币种合计。多币种时每个币种一行（`- 成本：CNY ¥…` / `- 成本：USD $…`），单币种保持原
    一行。顺带把缺 `cost_basis` 的默认词从 `reported`（错：那表示 CLI 自报）改成 `none`
    →「未标注」，与 C7 一致。
  - `pet.py`：`state()["cost"]` 保持 `{币种: 金额}` 旧形状（widget 还在读），缺币种的金额
    改放新增的 `cost_unlabeled`（按 provider、带次数），不再并进 `USD`（同一批 demo 数据：
    原来 `{"CNY": 2.0, "USD": 1.5}`，现在 `{"CNY": 2.0, "USD": 0.5}` + 两条未标注）。
    `describe()` 缺币种时打「未标注币种 0.1234」，不再打出空符号加一个空格。
  - `bench.py`：`_usage`/`_money` 缺币种从默认 `CNY` 改为 `ledger.UNKNOWN_CURRENCY`；
    `summarize()` 增加 `costs`（分币种）与 `unlabeled`，`cost_cny` 保留给
    `policy._bench_history` 读 results.json，但只含 CNY 行。`format_summary()` 单币种仍是
    老的一行（`solve 1/2 (50%) · 平均 41s · ¥0.09`），多币种或含未标注才分行。
  - `desk.py`：表格「成本」列缺币种原来印 `$`（7 倍错误），现在印「未标注币种 0.03」。
  - `lineage._route_summary`：不再把一条 route 的所有金额加进一个 float、再用第一条币种给
    它命名（同一批 demo 数据原来打 `3.5 CNY`，那是 2.0 CNY + 0.5 USD + 0.8 + 0.2 的和）。
    现在输出 `costs`（`{币种: {total, runs}}`）与 `unlabeled`（按 provider）；渲染时单币种
    仍接在 route 行末尾，多币种或含未标注改成分行。
  - `cli/commands/system.py cost`：标定漂移按币种分桶。账单侧（`meter_observed`）按构造
    就是 CNY，所以只有 CNY 桶算 `（账单 − 标定）/ 标定`；USD 桶与未标注桶如实列出金额，写明
    「账单侧只有 CNY，不换算、不比较」（demo 数据原来报「3 个 run 合计 ¥3.3000」，那 3.3 里
    含 0.5 USD 与 0.8 无币种）。
- **没动的**：`nodus stats`（`system._money_text`）仍把未标注印成一个带标签的金额，用旧词
  「币种未记录」，也没有按 provider 拆——它不在这六处里，`ledger.summarize` 的桶也不带
  provider；要拆得先给桶加来源维度，算下一件单独的事。`ui.py:662` 的「币种未记录」同理。
  纯 CNY 数据下 `stats`/`cost`/`report`/`lineage`/`desk` 与 bench 的输出与改造前逐字相同
  （报告只差生成时间；`pet` 的 `cost` map 不变，只是多一个空的 `cost_unlabeled: []`）。
- 测试：852 条全绿（基线 835，+17 条）。新增覆盖 `Money` 的三个语义（分币种、
  按 provider 分未标注、哨兵不算币种）、五处渲染的「单币种不变 / 多币种分行 / 未标注不并列」、
  以及漂移分桶；`test_lineage` 里原来断 route `cost` 的那条改成断 `costs`。

## 2. 最强的三条

### #1 C1 缺货币时两个模块给出两个币种（已收口）

`ledger` 的规矩写在模块 docstring 第一段：缺字段是 `None`，绝不补默认值；没记币种的金额进 `"-"` 桶
（`nodus/ledger.py:1-26`、`nodus/ledger.py:38`）。`telemetry.usage_summary` 自己解析 `usage_json`
（`nodus/telemetry.py:107`），并在缺币种时写 `currency = usage.get("currency") or "USD"`
（`nodus/telemetry.py:133`）。同一条 run，看板/CLI 记在 `"-"`，`telemetry` 记在 `USD`；
这不是展示差异，是两个总额。`ledger` 的 docstring 里点名过这个 7 倍误差的旧 bug，
`telemetry` 是它没覆盖到的第五处。

建议：留 `ledger.parse/cost_of/summarize`；`telemetry.usage_summary` 的统计部分退成
`ledger.summarize(db.list_runs(...))`，`telemetry` 只保留探活（`probe`/`last_probes`/`balance`）。

**已收口（提交 `af659e7`）**：`telemetry.usage_summary` 保留旧形状（CLI 还在读），但每个 run 的
token/金额/币种改由 `ledger.parse` 与 `ledger.currency_of` 给出；缺币种进 `"-"`。没有把统计
整体换成 `ledger.summarize`，因为那会改 `nodus usage --days` 的输出形状，超出这次"不改行为"的边界；
统计口径已同源，剩下的只是形状。

### #2 S3 同一个页面上三个"失败数"（已收口）

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

**已收口（提交 `b4568cd`）**：唯一计数是 `server.board_state()["decision_counts"]`：
`failed` 改成"50 条工作 run 里结束且不算成功"（进程失败，或跑完但测试红），`red` 是这个
数的子集而不是第二个失败数；`statuslight.snapshot` 改读新增的 `server.attention()`，只拿
board 的 `needs_you` / `failed` / live 行，不再自己查库；`lineage.for_task` 保留"本任务
历史"的口径（该任务全部 run），CLI 渲染里写明这个分母，`failed` 桶含测试红或还在跑的重试
时逐条标出。见 1.1。

### #3 F1 同一段 `run.error`，四套词表（已收口）

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

**已收口（`8717a3b`）**：分类搬到 `nodus/diagnosis.py`，`recover._cause` 只剩一个转发调用。
`cause`（13 个）、`class`（4 个）、`kind`（13 句）与展示标签是同一张表；`_CAUSE_RULES` 正则表删除，
`_outcome` 也读新 cause。测试 `tests/test_diagnosis.py::test_the_four_entry_points_name_one_cause`
对 13 个 cause 逐个断言四个入口。

## 3. 全部条目

### 3.1 成本（5 条）

| 编号 | 谁在算同一件事 | 证据（文件:行） | 差异 / 风险 | 建议 |
|---|---|---|---|---|
| C1 ★ **已收口** | `ledger` vs `telemetry.usage_summary` | `nodus/ledger.py:1-26`、`:38`、`:211`、`:356`；`nodus/telemetry.py:94`、`:107`、`:133` | 缺币种：`"-"` vs `"USD"`；`telemetry` 还自给 `cost_basis="reported"`（`:135`） | **已收口（提交 `af659e7`）**：`telemetry` 改读 `ledger.parse/currency_of`，缺币种进 `"-"`；`usage.py`/`server._today` 原本已走 `ledger` |
| C2 ★ | `server._today` / `usage._rows` / `budget.spent` / `harness_status.by_harness` 各自定义"今天" | `nodus/server.py:1853-1872`、`:1875-1882`；`nodus/usage.py:53-72`；`nodus/budget.py:53-73`；`nodus/harness_status.py:206-241` | 行集不同（`server._today` 只取 `agent/route/usage_json`，`usage._rows` 取 status/provider/model）；无币种的钱 `budget` 当 CNY、看板留 `"-"`；观察账进"今天的钱"（`server.py:1871`）但不进终端面板（`harness_status.py:11-25` 明说不折进去） | 算术留 `ledger`（已经是）；"今天取哪些行"合并成一个 helper；每个面板标自己的口径 |
| C3 | `server._live_rows` vs `ledger.parse` | `nodus/server.py:110-152`；`nodus/ledger.py:159` | 一个读 `usage` 事件（在跑，partial），一个读 `usage_json`（已结束）；两套 token 求和 | 事件求和移进 `ledger`（`Tokens` 已有），保留 "partial" 标记 |
| C4 | `policy._usage_cost` vs `ledger.UNKNOWN_CURRENCY` | `nodus/policy.py:797-811`；`nodus/ledger.py:38` | 已改为委托 `ledger.cost_of`，只把 `"-"` 改名 `"unknown"` 打印 | 保持（已收敛）；若 `usage.py` 的 JSON 消费者也改名就一起退 |
| C5 | `observe_ccswitch.estimated_usd` vs `ledger` 的分币种规则 | `nodus/observe_ccswitch.py:47`、`:61-65`、`:133` | CC Switch 的 rollup 是 USD，缺价模型记 0 并单独计 `zero_cost_requests`；与 `ledger` 的"不跨币种相加"一致但自成一套 | 保持只读探针；若并入看板，必须先有汇率口径，不能直接加总 |

### 3.2 状态（5 条）

| 编号 | 谁在算同一件事 | 证据（文件:行） | 差异 / 风险 | 建议 |
|---|---|---|---|---|
| S1 ★ **已收口** | run 状态由谁定义 / 谁写 | `nodus/db.py:44`（默认 `queued`）、`:637`（终态判断）、`:757`（live）、`:771`（stale）；`nodus/runner.py:956`（唯一 writer）、`:796`（900s 无输出→failed）、`:1123`（wait 终态）、`:1184-1186`（reconcile 900s）；`nodus/server.py:119`；`nodus/recover.py:809`；`nodus/harness_status.py:49`；`nodus/pet.py:74` | `stalled` 没有任何 writer；900s 无输出被记成 `failed(timeout)`，语义上就是 stalled；7 处各自写 `IN ('queued','running')` / `("completed","failed")` | **已收口（提交 `b5ef809`）**：`nodus/watchdog.py` 是 `running -> stalled` 的 writer，判据调 `lifecycle.needs_attention`，写前过 `can_transition`、先释放写锁；看板进"已停止"。其余读方的状态字面量仍未收敛 |
| S2 | "这个任务还有没有在跑"两份 SQL | `nodus/recover.py:787-799`（`_has_retry_child`）、`:802-814`（`_task_has_live_run`） | 与 `db.live_runs` 同源不同写法，状态字面量再抄一遍 | 留 recover 的 gate；liveness 查询收敛到 db/一处 |
| S3 ★ **已收口** | "失败"的计数口径 | `nodus/lineage.py:284-293`、`:338`；`nodus/server.py:1798-1799`、`:1813-1814`、`:1643-1741`；`nodus/statuslight.py:35-38` | 见第 2 节：completed+red 在 lineage 算 failed、在 server 算 red、在 statuslight 不可见；statuslight 还漏了 `kind="work"` | **已收口（提交 `b4568cd`）**：`decision_counts["failed"]` 含测试红（`red` 为子集、不相加）；`statuslight` 读 `server.attention()`；`lineage` 只改"本任务历史"标签与分母 |
| S4 | `ok` / `unverified` 的定义抄了四份 | `nodus/lineage.py:250-275`；`nodus/policy.py:855-860`、`:894`、`:914-915`；`nodus/desk.py:83`；`nodus/server.py:188-216`、`:1833-1837` | 今天四处一致（completed+ok=成功，completed+none=未验证）；但口径散着，改一处就漂 | 留 `lineage` 的三桶；policy/desk/server 调它或调 `lifecycle` 传出来的 facts |
| S5 | proposal 状态的合法值没有地方校验 | `nodus/db.py:1011-1015`（`close_proposal` 接受任意字符串）；`nodus/propose.py:162`、`:178`（只写 merged/rejected）；`nodus/db.py:966`、`nodus/server.py:1484`、`:1782`、`nodus/statuslight.py:28`（只读 open） | 今天唯一写者是 propose，但 db 层不设防，手滑写 `merge` 会让 proposal 从所有列表消失 | `lifecycle` 的 proposal 表先立着；校验放进 `close_proposal`（行为改动，单独做） |

### 3.3 失败分类（4 条）

| 编号 | 谁在算同一件事 | 证据（文件:行） | 差异 / 风险 | 建议 |
|---|---|---|---|---|
| F1 ★ **已收口** | 同一段 error 的分类 | `nodus/recover.py:207-232`、`:125`；`nodus/server.py:376-426`、`:770-788`、`:895-919` | 10 cause / 2 class / 14 label / 5 kind / 1 outcome token，见第 2 节 | **已收口（`8717a3b`）**：`nodus/diagnosis.py` 一张表，13 cause / 4 class / 13 kind；四个入口都调它 |
| F2 | 同一个 cause 的中文文案 | `nodus/recover.py:127-151`（`_PROBLEM`/`_SUGGESTION`）；`nodus/server.py:1083-1174`（`_CAUSE_FIX`/`_CAUSE_ADVICE`/`_next_step`） | 两套中文句子按 cause 各写一遍，措辞已经不同 | 合并成一张 `{cause: {machine_sentence, action}}`，机器句归 recover，动作句归 server |
| F3 ★ **已随 F1 收口** | 同一批错误正则 | `nodus/recover.py:82-122`；`nodus/server.py:353-369`、`:376-426` | timeout/model/auth/网络/进程退出各抄一遍（timeout 两边几乎同字） | **已收口（`8717a3b`）**：正则只在 `nodus/diagnosis.py`，`recover`/`server` 都不再自带匹配表 |
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
| X4 **已收口（随 S3）** | 宠物灯自己数 | `nodus/statuslight.py:23-38` | 与看板 `_decisions` 并行维护两个计数；`db.list_runs(limit=50)` 未过滤 `kind`，chat 失败会触发"跑红了" | **已收口**：`statuslight.snapshot` 改调 `server.attention()`，与看板同源；见 1.1 |

## 4. 写 lifecycle 时发现的"定义 vs 现状"边界

这些不是 bug，是必须知道才能读懂 `needs_attention` 的前提。都记在这里，不在本提交改：

1. **`stalled` 从 2026-11-03 起是现状，之前只是目标状态。** 900 秒无输出时 runner 直接 kill 并写
   `failed`，cause 是 `timeout`（`nodus/runner.py:795-800`）；`reconcile` 对死掉的 owner 也写 failed
   （`nodus/runner.py:1184-1186`）。`lifecycle` 早给出了 `running → stalled` 这条边，但直到 `watchdog`
   （见 1.3）才有人写：默认 600 秒无实质事件先标 `stalled`，runner 的 900 秒 idle kill 仍是后手。
   回归样本是收口前的快照，20 条真实 run 里没有 `status="stalled"`，所以旧测试只断言"认这个状态"，
   不断言"能观察到它"；`tests/test_watchdog.py` 补上了后者。
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
6. **唯一一条真矛盾是 C1（币种）**，其余是重复与口径差。C1 在当时没有修；
   2026-11-03 已收口，见 1.1。

## 5. 不在本提交做的事（写给下一个改的人）

- 不迁移 `recover` / `server` / `lineage` 到 `lifecycle`：迁移会改行为（至少是错误处理路径），
  而这次验收要求 724 条旧测试全绿、代码不变。
- 不改 `telemetry.usage_summary` 的 USD 默认值：当时会改 `nodus status`/报表的输出；
  2026-11-03 已按 `ledger` 收口，见 1.1。
- 不给 `close_proposal` 加校验：会让现有写者之外的手工 `UPDATE` 失败，需要单独评估。
- ~~不把 `stalled` 写进 `run.status`~~：2026-11-03 已由 `watchdog` 收口（见 1.3）；还剩 `recover` /
  `lineage` 等读方没有迁到 `lifecycle`，是下一批。
- 不给看板换新的计数口径：`decision_counts` 仍被 MCP 与桌面宠物读（`nodus/mcp.py`、`nodus/pet.py`），
  先保留形状；2026-11-03 S3 收口时只把 `failed` 的含义收紧成"结束且不算成功"（含测试红，
  `red` 为子集），键与形状没变，见 1.1。
