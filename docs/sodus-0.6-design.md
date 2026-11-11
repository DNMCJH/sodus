> 产品名 Sodus。

# Sodus 0.6 设计定稿（v0.2）

> 2026-11-08 · 分支 `feat/sodus-0.6`（worktree `sodus-0.6`），**不合并 main**，等用户确认。
> 基线：main 的 0.5.1 tag。输入：`reports/brief-0.6-20261108.md`、`_reviews/opus-review.md`、
> `_reviews/sol-review.md`、`docs/architecture.md`（ADR-001–018）、`docs/routing-memory-lineage.md`、`docs/vision.md`。
> v0.1（spec-sodus-memory）经两份评审后修订为本版；本版只是设计，**W0 不改任何 .py**。
> 对应 ADR：ADR-019（事实源与暂存层分离）、ADR-020（任务状态与入口边界），原文在 `docs/architecture.md` 第 5 节。

## 0. 决策落点索引

brief 的 10 条已确认决策，每条在本文的落点：

| # | 决策 | 主落点 | 相关 |
|---|---|---|---|
| D1 | 状态机：不新增第五套，扩 `lifecycle.py`，task 由 run+review 派生 | §3 | ADR-020 |
| D2 | 事实源 = AGENTS.md；SQLite 只做可重建暂存；schema_version + 迁移测试；section hash 漂移挂起 dream | §4.1、§4.4、§4.5 | ADR-019 |
| D3 | 证据门槛：verify verdict + 文件 hash；lesson 只在 pass/人工确认后成候选；失败 episode 入库 | §4.2 | ADR-014 |
| D4 | 曝光与退役：记录 exposure；退役不只看 hits；未曝光只提案 | §6 | — |
| D5 | 防递归：worker 标记禁用 /ask，最多 1 层 | §2.5 | ADR-020 |
| D6 | 通知：无推送，轮询；SSE 只服务看板；不写活跃 DSH 会话 | §2.6 | ADR-005 |
| D7 | request_id：id + payload hash 幂等，409，TTL 24h，answer→task 升级 | §2.2–§2.4 | ADR-020 |
| D8 | 检索：FTS5 trigram + CJK 测试 | §4.6 | — |
| D9 | dream 协议：批次/游标/事务/staging/journal/幂等/write_lock/≥3 不同 lineage/不自证/marker/DB 版本回滚 | §5 | ADR-012、ADR-019 |
| D10 | 指标与发行门槛：rot + 效用指标；A/B 先算功效；迁移/升级/回滚测试；干净 venv e2e；dream 便宜路由 + 日上限 + ledger | §8、§9 | ADR-008、ADR-017 |

## 1. 目标与非目标

**目标**（一句话：DSH 里问一句，Sodus 答或派活；每次结束的 run 留下有证据的经验，人审核后变成下一次的事实）

1. 单入口：`POST /ask` 与 MCP `sodus_ask`，问答与执行分开，问答可升级为任务。
2. 有证据的记忆：episode → 候选 → 人采纳 → AGENTS.md，事实源唯一。
3. 离线巩固（dream）：可崩溃、可重放、可回滚，不与 run/看板采纳抢写。
4. 用 bench 衡量记忆是否真有用，并量化记忆腐烂。

**非目标**（brief 边界，原样保留）

- 单用户 Windows；daemon 端口 4199；SQLite；DSH 是唯一对话框。
- 不重写 harness；不上向量库、多租户、云 VM、eBPF、Lean。
- 不做推送通知（§2.6）；不自动采纳任何事实（采纳必须是人，见 `routing-memory-lineage.md` 第四节第 7 步）。
- 不恢复旧的三档作用域（private/project/public）和 `context_for` 注入（§4.1）。

## 2. 入口契约：`POST /ask` / MCP `sodus_ask`

### 2.1 请求判别：问答 vs 执行

`/ask` **只产生回答**，不改主树。执行必须显式升级（§2.4）或走已有的 `sodus_task_create` + `sodus_run`。
这样「追问」和「派活」不会被同一个入口模糊处理（sol-review 第 1 条）。

- 回答由一条 `kind='ask'` 的 run 产生，走正常的 routes / ledger / budget 闸（不用 `llm.py`：那是宠物聊天，
  400 token / 10 次每分钟的限制是故意的，不适合通用问答）。
- 工作区是 git 仓库时，ask run 在临时 worktree 里跑（复用 ADR-015 的机制），主树不被借锁；跑完若 diff 非空，
  回答带 `wrote_files: true`，补丁丢弃，提示用升级接口。非 git 工作区：ask run 按 ADR-012 正常拿写锁（串行）。
- 路由：默认 `config.routing.default`（0.5.1 已实现）；请求可带 `route` 覆盖。

### 2.2 请求与响应

```
POST /ask
Authorization: Bearer <token>
X-Sodus-Depth: 0            # 可选；见 §2.5
{
  "request_id": "dsh-<uuid>",     # 必填，客户端生成
  "text": "为什么 watchdog 默认 600 秒？",
  "cwd": "A:\\...\\sodus",         # 工作区；决定 workspace_id 与 AGENTS.md
  "follow_up_of": "ask-xxxx",      # 可选：追问续接，带上一问的 id
  "route": "claude"                # 可选
}
→ 202 {"id": "ask-…", "request_id": "…", "status": "queued", "poll": "/ask/ask-…"}

GET /ask/{id}
→ 200 {"id", "status", "answer", "run_id", "task_id", "wrote_files", "cost", "expires_at"}
```

MCP `sodus_ask` 是同一契约的薄壳（参数同 body，返回 202 的 JSON）；配套 `sodus_ask_status(id)` 等价 `GET /ask/{id}`。
MCP 工具名：现有工具前缀是 `sodus_`，新工具按 sodus 命名过渡（`docs/sodus-migration.md`）用 `sodus_`，并给
`sodus_ask` 别名，避免一个服务器里两套前缀没人解释。

`follow_up_of`：追问把上一问的问答作为上下文前缀拼进 brief（只读 ask 表，不读 DSH 会话），新问题仍是新 id。

### 2.3 幂等：request_id + payload hash

- 键：`(workspace_id, request_id)` 唯一。`payload_hash = sha256(canonical_json({text, cwd, follow_up_of, route}))`。
- 同 id、同 hash：返回已有记录（200，不新建 run）。这覆盖客户端重试与断线重发。
- 同 id、异 hash：**409** `{"error": "request_id_conflict", "id": <已有 ask id>, "payload_hash": <已有 hash>}`。
- 并发去重：INSERT 在一个事务里依赖唯一索引；后到者捕获 `IntegrityError` 再按上两条处理，不靠先查后插。
- TTL 24 小时（`created_at + 86400`）。过期后 sweep 删行（answer 文本已在 run 的 artifact 里，不丢），id 可复用。
  `GET /ask/{id}` 对已过期的 id 返回 410。

### 2.4 升级：answer → task

```
POST /ask/{id}/promote   {"acceptance": "测试命令或验收标准", "route": "…"}
→ 201 {"task_id": "task-…"}
```

- 用 ask 的 text + answer 作为 task.goal 的上下文；`acceptance` 必填（没有验收标准的 task 不建）。
- ask 行写 `promoted_task_id`；重复 promote 返回同一个 task_id（幂等）。
- 升级后的 task 走 ADR-014/015 的正常流程；ask 本身的状态不变（§3.2 派生）。

### 2.5 防递归（D5）

DSH 既是入口又是 worker；worker 里的 DSH 也挂着 sodus MCP，所以 run 内可能再调 `sodus_ask`。

- runner 起 worker 时在环境里设 `SODUS_ROLE=worker` 和 `SODUS_DEPTH=<父 depth+1>`（经 `agents.build_env`，
  不进生成的配置文件，符合 ADR-007/011 "配置里不出现 env 块"）。
- MCP 服务进程继承这两个变量；`sodus_ask` 发现 `SODUS_ROLE=worker` 时直接返回错误，不发 HTTP。
- HTTP 侧兜底：`X-Sodus-Depth ≥ 1` → **403** `recursion_denied`。MCP 客户端转发时把 `SODUS_DEPTH` 写成该头。
- 最多 1 层：DSH 用户会话（depth 0）→ ask/task 的 worker（depth 1），worker 不能再发 ask。
  `sodus_run`/`sodus_task_create` 在 worker 内同样拒绝，理由相同。
- 已知缺口：绕过 MCP、直接带 token 打 HTTP 且不带头的 worker 拦不住；token 只在用户目录，这与 ADR-005 的
  "会话锁是建议性的"同一性质，写明不粉饰。

### 2.6 通知与会话归属（D6）

- MCP 没有服务端推送，DSH 没有会话间通道（ADR-005）。**不承诺推送**，v0.1 的"下一条消息附带结果"删除。
- 获取结果只有两条路：`GET /ask/{id}`（或 `sodus_ask_status`）与 `sodus status`（列出本 workspace 未读完的 ask）。
- SSE（`server._sse`）只服务看板，不作为 ask 的通知通道。
- 不写活跃 DSH 会话：Sodus 不往任何 DSH session 存储追加内容（ADR-005 原样成立）。
- 断线续传：ask id 持久，断线后用同一 id 轮询即可；DSH 侧重发同一 request_id 得到同一记录（§2.3）。
- 会话归属：ask 行记 `origin`（`mcp` / `http` / `cli`）与 `client`（MCP initialize 的 clientInfo.name）和可选的
  `session_ref`（只读引用，用于事后在 lineage 里关联），**不**据此写回该会话。

## 3. 任务状态映射（D1，ADR-020）

### 3.1 原则

不新增第五套状态（ADR-018）。`sodus/lifecycle.py` 增加两个**派生函数**，不增加新的可写状态表：

- `task_state(facts) -> str`：输入是该 task 的 run 状态、最后一次 verify verdict、review verdict（`review.py` 的 accept / revise / reject / unparsed / unknown）、人工确认；
- `ask_state(facts) -> str`：输入是 ask 的 run 状态、`promoted_task_id`、`expires_at`、`now`。

`task.status` 列保留，但从 0.6 起只由「调用 `task_state` 后写回」这一种方式写（缓存，不是第二份判断）；
`tdd.py`/`fix.py` 里现有的 `db.update_task(status=…)` 在 W1 迁到这个函数上，迁移前后由对照测试锁住行为。
run 表的 `queued → running → {completed, failed, stalled}` 不变：`completed` 仍只是"进程退出 0"，
"是否验证"仍不进 run 状态表（`lifecycle.py` 模块文档原意），而是作为 task 派生的输入。

### 3.2 映射表

verify verdict（与 §4.2 episode 共用一套词）：`pass`=`verify.green`；`red`=`verify.red`；
`unverified`=completed 但没跑 verify（没改文件 / 检测不到已知测试命令 / `--no-verify`）；`failed`=run 终态 failed。

**task_state**（取 task 最新一条 kind=work 的 run，按优先级自上而下匹配）：

| run 状态 | verify | review | 人工 | task 状态 | 说明 |
|---|---|---|---|---|---|
| 无 run | — | — | — | `open` | |
| queued / running | — | — | — | `running` | |
| stalled | — | — | — | `stalled` | `needs_attention` 已覆盖 |
| failed | failed | — | — | `failed` | |
| completed | red | — | — | `failed` | exit=0 ≠ 成功（ADR-014） |
| completed | pass | revise / reject / unparsed / unknown | — | `needs_review` | 审查只降级（ADR-003） |
| completed | pass | 无 / accept | — | `done` | 唯一的"验收通过" |
| completed | unverified | — | 无 | `unverified` | |
| completed | unverified | — | confirmed | `unverified` + `accepted_by=human` | 人工拍板 ≠ 验收通过 |

人工确认只写 `task.accepted_by='human'`（新列；已有的 `task.acceptance` 是验收标准文本，不复用），不把状态改成 `done`；它的作用是让该 task 的 episode 可以产出候选
（§4.2），而不是伪造验收（sol-review 第 1 条）。

**ask_state**：

| 条件 | ask 状态 |
|---|---|
| `now > expires_at` | `expired` |
| `promoted_task_id` 非空 | `promoted`（task 自己的状态看 task_state） |
| run queued / running / stalled | 同 run（`queued` / `running` / `stalled`） |
| run completed | `answered` |
| run failed | `failed` |

### 3.3 与现有读方的关系

- `lineage` 的 ok/unverified 与上表的 `done`/`unverified` 一致，`tests/test_lifecycle.py` 增加对照用例。
- 看板三组（needs_you / handling / stopped）不变；`unverified` 进 needs_you（需要人看），`stalled` 仍进 stopped。
- `recover` 的 gate 仍只作用于 run，不读 task_state。

## 4. 记忆四层与 DDL 草案（D2/D3/D8，ADR-019）

### 4.1 四层与唯一真源

| 层 | 内容 | 存放 | 真源？ | 可重建自 |
|---|---|---|---|---|
| L0 原料 | run 目录（handoff.md、verify-*.txt、events）、DSH/Claude/Codex 会话日志、observation | 文件系统 + 已有表 | 是（它们自己的事实） | — |
| L1 episode | 每个**结束**的 run 一条：verdict、改动文件 hash、摘要、L0 引用 | SQLite `episode` | 否 | L0 + run/event 表 |
| L2 候选 | 从 episode 抽出的 lesson / 从 harness 文件 harvest 的事实，带 provenance 与证据 | SQLite `candidate` + `candidate_evidence` | 否 | L1 + 已存的 dream 输出 |
| L3 采纳事实 | 项目 `AGENTS.md`（全局的进 `~/.sodus/AGENTS.md`）「已验证事实」marker 区块 | 文件 | **唯一真源** | — |

另有横切的 `memory_exposure`（§6）、`dream_batch`（§5）和 FTS 索引。

- SQLite 里**没有**任何"已采纳事实"的权威副本；candidate 的 `status='adopted'` 只是索引，删库重建后从 L3 反向
  导入即可恢复（`sodus memory reindex`）。`memory_exposure` 不可重建，但它只影响退役提案，丢失的后果是"更多条目
  只提案不退役"，方向是保守的。
- 旧的 private/project/public 三档与"同 key 覆盖"规则（`routing-memory-lineage.md` 第二节）**由本层次替代**：
  作用域只剩"项目文件 vs 全局文件"（`memory.adopt` 已按此实现），覆盖改为"新事实采纳 + 旧事实退役提案"，
  不存在 key。
- `workspace_id` 映射：episode/candidate/exposure/batch 行都带 `workspace_id = config.workspace_id()`；
  workspace 到 L3 文件的映射用 `memory.project_root(cwd)`，结果记在 `candidate.target_path`。

### 4.2 证据门槛（D3）

- **每个结束的 run 都写 episode**（completed 与 failed 都写），verdict ∈ `pass | red | unverified | failed`（§3.2）。
- 记录改动文件的 `path + sha256`（取自 verify 时的工作树）与测试文件 hash（ADR-014 已有的证据）。
- lesson 只在 `verdict='pass'` 或所属 task `accepted_by='human'` 时才能成为候选；其余 episode 只参与统计
  （重复失败率、§8）。失败 episode 必须入库，否则重复失败率没有分母。
- 每个候选带 provenance：来源 episode / 文件、route、harness、batch_id、抽取方式（rule / llm）。

### 4.3 L0 的失效与校验

L0 路径会失效（run 目录被清理、会话文件被 DSH 轮转）。episode 里 `l0_refs` 存 `{path, sha256, size}`，
不复制原文；摘要（≤ 1 KB）存在 episode 本身。dream 读 L0 前校验 sha256，不一致或缺失按"只有摘要"处理，
不报错、不猜。保留策略：Sodus 不负责清理 L0；`sodus doctor` 报告失效引用比例（§8 过期占比的输入之一）。

### 4.4 DDL 草案（schema_version 1 → 2）

现状：`meta.schema_version='1'`，`connect()` 用 `CREATE TABLE IF NOT EXISTS` + 列补丁做迁移。
0.6 把迁移改为**按版本号顺序执行的函数列表**，每个版本一个函数、一个事务；v2 只**新增**表/列，0.5.1 打开 v2 库
时忽略未知表，所以降级是安全的（§9 有测试）。

```sql
-- v2: entry
CREATE TABLE IF NOT EXISTS ask (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  text TEXT NOT NULL,
  cwd TEXT,
  follow_up_of TEXT REFERENCES ask(id),
  route TEXT,
  run_id TEXT REFERENCES run(id),
  promoted_task_id TEXT REFERENCES task(id),
  origin TEXT CHECK (origin IN ('mcp','http','cli')),
  client TEXT,
  session_ref_json TEXT,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ask_request ON ask(workspace_id, request_id);

ALTER TABLE task ADD COLUMN accepted_by TEXT;         -- NULL | 'human'; task.acceptance is the criteria text

-- v2: memory staging (rebuildable; AGENTS.md is the source of truth)
CREATE TABLE IF NOT EXISTS episode (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  run_id TEXT NOT NULL UNIQUE REFERENCES run(id),
  task_id TEXT REFERENCES task(id),
  lineage_root TEXT NOT NULL,          -- root session id, else task id, else run id
  route TEXT,
  verdict TEXT NOT NULL CHECK (verdict IN ('pass','red','unverified','failed')),
  cause TEXT,                          -- diagnosis cause for failed/red
  files_json TEXT,                     -- [{path, sha256}]
  tests_hash TEXT,
  summary TEXT,
  l0_refs_json TEXT,                   -- [{path, sha256, size}]
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episode_lineage ON episode(workspace_id, lineage_root);

CREATE TABLE IF NOT EXISTS candidate (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  norm_hash TEXT NOT NULL,             -- memory._hash(memory._norm(text))
  text TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('lesson','harvested')),
  target_path TEXT NOT NULL,           -- the AGENTS.md this would land in
  status TEXT NOT NULL DEFAULT 'proposed'
    CHECK (status IN ('proposed','rejected','adopted','retire_proposed','retired')),
  version INTEGER NOT NULL DEFAULT 1,  -- CAS for board edits
  provenance_json TEXT NOT NULL,
  first_batch TEXT REFERENCES dream_batch(id),
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (workspace_id, target_path, norm_hash)
);

CREATE TABLE IF NOT EXISTS candidate_evidence (
  candidate_id TEXT NOT NULL REFERENCES candidate(id),
  episode_id TEXT NOT NULL REFERENCES episode(id),
  lineage_root TEXT NOT NULL,
  exposed INTEGER NOT NULL DEFAULT 0,  -- 1 if this candidate was exposed to that run
  PRIMARY KEY (candidate_id, episode_id)
);

CREATE TABLE IF NOT EXISTS memory_exposure (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL,
  norm_hash TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('retrieved','injected','displayed','eligible')),
  run_id TEXT, ask_id TEXT,
  harness TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exposure_hash ON memory_exposure(workspace_id, norm_hash, kind);

CREATE TABLE IF NOT EXISTS dream_batch (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN
    ('planned','extracted','staged','applied','failed','blocked_drift','rolled_back')),
  cursor_from INTEGER NOT NULL,        -- episode rowid watermark, exclusive
  cursor_to INTEGER NOT NULL,          -- inclusive
  output_json TEXT,                    -- stored LLM output; replay never re-calls the model
  ops_json TEXT,                       -- L3 ops applied: [{op:add|retire, norm_hash, text}]
  memory_version INTEGER,              -- meta.memory_version after apply
  run_id TEXT REFERENCES run(id),      -- the kind='dream' run; cost lives in ledger
  target_path TEXT,
  hash_before TEXT, hash_after TEXT,
  error TEXT,
  started_at REAL, ended_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dream_range ON dream_batch(workspace_id, cursor_from, cursor_to);

CREATE VIRTUAL TABLE IF NOT EXISTS candidate_fts USING fts5(
  text, content='candidate', content_rowid='rowid', tokenize='trigram'
);

-- meta keys: schema_version='2'; memory_version=<int>;
--            'l3_hash:<abs path>'=<sha256 of marker block body>; 'dream_cursor:<workspace_id>'=<rowid>
```

`ask.run_id` / `dream_batch.run_id` 指向 run 表：ask 与 dream 都是 run（`kind='ask'` / `kind='dream'`），所以成本、
预算闸、watchdog、看板全部复用，不另起账本。

### 4.5 L3 marker 区块与漂移检测

现状：`memory.adopt` 在 `## 已验证事实` 标题下追加，没有边界标记。0.6 改为：

```markdown
## 已验证事实
<!-- sodus:facts:begin -->
- 事实文本（2026-11-08，来源：…）
<!-- sodus:facts:end -->
```

- Sodus 只改 begin/end 之间的内容；标题外、区块外的手写内容永不触碰。首次写入时把现有小节内容包进 marker
  （迁移一次，记 journal）。
- 每次 Sodus 写完，`meta['l3_hash:<path>'] = sha256(区块内容)`。
- 任何写入（dream apply、看板采纳、退役、回滚）之前重算；不一致 = 漂移（人手改过）。漂移时：dream 批次置
  `blocked_drift` 并挂起该文件的所有 dream；看板显示"AGENTS.md 被手改，需要重新索引"；`sodus memory reindex`
  以文件为准重建 candidate 索引（手删的条目 → `retired`，手加的 → `adopted`，provenance=`manual`），更新 hash 后恢复。
- 漂移不是错误：AGENTS.md 是真源，手改永远合法，只是索引需要追上。

### 4.6 检索（D8）

- `candidate_fts` 用 FTS5 `trigram` tokenizer，中文无需分词即可子串匹配。本机 Python 的 SQLite 为 3.45.3
  （trigram 需 ≥ 3.34）；启动时探测，不支持则退化为 `LIKE` 并由 `sodus doctor` 警告。
- trigram 对少于 3 个字符的查询无结果（"写锁"只有两个字）：查询词 < 3 字符时走 `LIKE '%…%'`。
- CJK 检索测试（W3 必须有）：`数据库迁移`（纯中文）、`写锁`（2 字回退）、`pytest 超时`（中英混合）、
  `watchdog`（纯英文）、全角标点不影响命中。
- 检索命中写 `memory_exposure(kind='retrieved')`。

## 5. dream 协议（D9）

dream = 把新 episode 巩固成候选、把已批准的 L3 变更原子落盘的离线批处理。**dream 不采纳任何事实**：采纳与退役
永远由人在看板点，dream 只负责抽候选、计证据、提退役提案，以及执行人已批准的 L3 写入。

### 5.1 触发与成本

- daemon 定时（默认每日一次，空闲时）或 `sodus memory dream --now`。
- 路由：`config.routing.dream`（默认便宜路由，缺省回落 `routing.default`）。
- 每日 token 上限 `config.memory.dream_daily_tokens`；超出则当天不再起批，已在跑的不杀（同 `budget.py` 语义）。
- 抽取 LLM 调用是一条 `kind='dream'` 的 run，成本经 ledger 计量，日额度闸照常生效。

### 5.2 状态与步骤

```
planned ──抽取──> extracted ──计证据(DB 事务)──> staged ──L3 落盘──> applied
   │                 │                              │
   └────────────── failed / blocked_drift ──────────┘          applied ──回滚──> rolled_back
```

1. **plan**（事务）：`cursor_from = meta['dream_cursor:<ws>']`；取 `episode.rowid > cursor_from` 的前 N 条
   （默认 50），插入 `dream_batch(planned, cursor_from, cursor_to)`。唯一索引保证同一区间只有一个批次。
2. **extract**：对 `verdict='pass'` 或 `accepted_by='human'` 的 episode 调便宜路由抽 lesson；输出原样存
   `output_json`，置 `extracted`。之后任何重放都读 `output_json`，**不再调模型**。
3. **count**（单个 DB 事务）：按 `norm_hash` upsert candidate（`INSERT … ON CONFLICT DO NOTHING`），写
   `candidate_evidence`，推进 `dream_cursor`，置 `staged`。事务失败则整体回滚，游标不动，可直接重放。
4. **apply**（仅当有人已批准的 L3 变更时）：见 §5.4。

### 5.3 证据计数规则

- 候选上板（`proposed` 进看板"待采纳"）需要 **≥ 3 个不同 `lineage_root`** 的证据：
  `COUNT(DISTINCT lineage_root) WHERE exposed=0`。同一 task / 同一会话链里的重试只算 1。
- 不能自证：证据只能是 episode，不能是另一个候选；一个候选曾被曝光（injected / eligible）给某 run，该 run 的
  episode 对它记 `exposed=1`，不计入独立证据。
- 规则 harvest 出来的候选（`kind='harvested'`，已有 `memory.harvest`）维持现状直接上板，不受 ≥3 限制——它们的
  证据是"某个 harness 文件里已经写着"，由人判断。

### 5.4 L3 落盘：staging + 原子替换 + recovery journal

L3 写入（dream apply、看板采纳、退役、回滚）**全部走这一条路径**，包括现在的 `memory.adopt`（W4 迁移；
它目前不拿写锁，是看板采纳与 dream 并发写的缺口）。

1. 拿写锁：`db.lock_path(<AGENTS.md 所在项目根>, owner=batch_id)`（ADR-012 的同一张表）。拿不到 = 有 run 或另一个
   写者在这棵树上，本批次保持 `staged` 下次再试，不等待。
2. 漂移检查（§4.5），不一致 → `blocked_drift`，释放锁，结束。
3. 写 journal `~/.sodus/memory/journal/<batch_id>.json`：`{batch_id, target, hash_before, hash_after, staging,
   ops, memory_version}`，fsync。
4. 写 staging 文件 `<target>.sodus-staging`（同目录，保证同卷），fsync；`os.replace(staging, target)`。
5. DB 事务：`meta.memory_version += 1`，写 `l3_hash`，candidate 状态，batch 置 `applied` 并记 `ops_json`、`memory_version`。
6. 删 journal，释放锁。

**崩溃恢复**（daemon 启动时，先于任何 dream）：对每个残留 journal 读目标文件区块 hash：
`== hash_after` → 补做第 5 步；`== hash_before` → 删 staging、batch 回到 `staged`（可重放）；其他 → 人改过，
batch 置 `failed`，保留 journal 与 staging 供人看。三种情况都幂等，可以反复执行。

### 5.5 回滚

- 不依赖 git（AGENTS.md 可能不在仓库里，也可能有无关的未提交改动）。
- `sodus memory rollback --to <memory_version>`：从 `dream_batch.ops_json` 按 `memory_version` 顺序重放 ≤ V 的
  ops，得到目标区块内容，再按 §5.4 写入（一次新的 apply，`memory_version` 继续递增，不改写历史）。
- 漂移时拒绝回滚，先 reindex——回滚不得覆盖人工编辑（sol-review 第 4 条）。

### 5.6 看板审核

- API：`GET /memory/candidates`、`POST /memory/candidates/{id}`，body `{action: adopt|reject|edit|retire,
  version, text?}`。`version` 做 CAS：不等于当前值返回 409（板子过期）。
- `edit` 改写文本 → 新 `norm_hash`，旧候选 `rejected`，新候选继承证据与 provenance（记 `edited_from`）。
- adopt / retire 被批准后排入下一次 apply（也可立即 apply，同一协议）。

## 6. 曝光与退役（D4）

### 6.1 曝光

| kind | 何时记 | 说明 |
|---|---|---|
| `retrieved` | FTS 检索返回该条 | `/ask` 的上下文检索、看板搜索 |
| `injected` | 写进 brief（`reads_memory_files=False` 的路由） | 唯一能确认"模型看到了"的 |
| `displayed` | ask 回答或看板展示了该条 | |
| `eligible` | run 的 cwd 对应的 AGENTS.md 区块里有该条，且 harness 会自己读文件 | 只能确认"有机会看到" |

opus-review 第 8 条的问题——会读文件的 harness 不走注入，hits 无从采集——由 `eligible` 回答：我们不声称知道
它读没读，只记它有机会读。

### 6.2 退役

退役不看单一的 hits。信号：

1. 与更新的已采纳事实矛盾（`memory.conflicts`）。
2. 事实引用的路径 / 命令已不存在（L0 校验、`Path.exists`）。
3. 在 `eligible/injected` 期间关联的 episode 重复失败（同 cause ≥ 2 个不同 lineage）。
4. 长期（默认 90 天）无 `retrieved/displayed`。

规则：

- **从未曝光**（四种 kind 都为 0）的条目：只提 `retire_proposed`，永不自动退役。
- 自动退役只有一种情况：信号 1 成立且新事实是人采纳的；动作是移出区块并记 `ops_json`，可回滚。
- 其他一律是提案，进看板等人点。

## 7. 材料组织

```
~/.sodus/
  sodus.db                      # 暂存与索引（本文 §4.4），可删可重建（exposure 除外）
  memory/
    journal/<batch_id>.json     # §5.4 recovery journal，正常结束即删除
    batches/<batch_id>.json     # dream 输出备份（与 output_json 同内容，便于人读）
  runs/<run_id>/                # L0：handoff.md、verify-*.txt、context.md（已有）
<project>/AGENTS.md             # L3 真源（项目事实）
~/.sodus/AGENTS.md              # L3 真源（全局事实，memory.adopt 已如此）
```

- 设计文档：本文；ADR 在 `docs/architecture.md`；进度在 `docs/routing-memory-lineage.md` 第五节。
- 评审原文在 `_reviews/`，brief 在 `reports/`，均不进 main（worktree 内留存）。

## 8. 指标与 A/B（D10）

### 8.1 memory rot 指标（`sodus memory stats`，每次 dream 后算一次存入 batch）

| 指标 | 定义 |
|---|---|
| 重复率 | 区块内相似度 ≥ `memory.NEAR_DUPLICATE`(0.85) 的条目对数 / 条目数 |
| 矛盾数 | `memory.conflicts` 返回的组数 |
| 过期占比 | （引用路径失效 + 90 天无 retrieved/displayed）的条目 / 条目数 |
| 条目增长率 | 每周净增条目数（adopted − retired） |

### 8.2 效用指标

| 指标 | 定义 |
|---|---|
| 首次验收通过率 | 每个 task 第一条 run 的 verdict=`pass` 比例 |
| 重复失败率 | 失败 episode 中，同 workspace 内已有另一个 lineage 出现过同 cause 的比例 |
| 每个通过 task 的 token / 成本 | 经 ledger，按币种分桶，不跨币种相加 |
| 候选采纳率 | adopted / （adopted + rejected） |

### 8.3 A/B：先算功效再跑

- 材料：现有 `eval/tasks` **12 题**（opus-review 第 13 条写的是 6 题，当前仓库是 12 题，以仓库为准）。
- 设计：配对，同一题同一路由，A = 无 L3 区块，B = 有 L3 区块（由 dream 在 bench 的训练集生成，**不能**用同题的
  解答作为证据，否则是泄题）；结局 = hidden_tests 是否通过。
- 检验：McNemar。所需不一致对数 `n ≈ (z_{α/2}+z_β)² · (p10+p01) / (p10−p01)²`。示例：α=0.05、功效 0.8、
  p10=0.25、p01=0.05 → n ≈ 7.84 × 0.30 / 0.04 ≈ 59 对 → 12 题 × 5 次重复。
- W1 用现有 bench 报告（`reports/2026-10-2*_bench*.json`）估 p，算出 n 和成本；成本超出预算就只报告描述性结果，
  **不声称记忆有效**。

## 9. 发行门槛（0.6 合入 main 前全部满足，合入仍需用户确认）

1. `python -m pytest -q -o addopts=""` 全绿（W0 时 896 条已收集）。
2. schema 迁移测试：v1 库（含真实形状的 fixture）→ v2；重复升级幂等；0.5.1 代码打开 v2 库仍能读写（降级）；
   迁移中途失败事务回滚，`schema_version` 不变。
3. 记忆回滚测试：apply 3 批后 rollback 到 v1，区块内容逐字等于当时；漂移时回滚被拒。
4. 重启恢复：在 §5.4 第 3/4/5 步之间杀进程，重启后三种 journal 分支各有测试。
5. 幂等：`/ask` 同 id 同 payload / 异 payload(409) / 并发两发；dream 同一区间重放结果一致且不调模型。
6. 隔离：两个 workspace_id 的 ask/episode/candidate 互不可见。
7. 防递归：worker 环境下 `sodus_ask` 拒绝；`X-Sodus-Depth: 1` → 403。
8. CJK 检索测试（§4.6）。
9. 干净 venv 脚本化 e2e：`scripts/e2e_clean_venv.ps1` —— 新建 venv → `pip install -q .` → `sodus up` →
   `POST /ask` → 断开后用同一 id 轮询 → promote → `sodus status` → `sodus down`；退出码即结论。
10. dream 成本：一次 dream 后 ledger 里有 `kind='dream'` 的行，日额度到顶时不起新批。
11. 文档：README / architecture / routing-memory-lineage 与实现一致（本次 W0 已先校准 architecture 与 lineage 文档）。

## 10. 分周 MVP

| 周 | 内容 | 退出条件 |
|---|---|---|
| W0 | 本设计 + ADR-019/020 + 文档校准（只写文档） | 本文件 SUMMARY；无 .py 改动 |
| W1 | 版本化迁移框架、schema v2、`lifecycle.task_state/ask_state` + 对照测试、tdd/fix 迁到派生函数；A/B 功效计算 | 门槛 1、2、功效报告 |
| W2 | `POST/GET /ask`、promote、MCP `sodus_ask`(+`sodus_ask` 别名)、幂等/409/TTL、防递归、`sodus status` 列 ask | 门槛 5（ask 部分）、6、7 |
| W3 | episode 写入（runner 收尾）、exposure 记录、FTS trigram 检索 | 门槛 8 |
| W4 | dream 批次/游标/抽取/计数、marker 区块、§5.4 apply 协议（含 `memory.adopt` 迁移、写锁）、恢复、漂移、回滚、看板 CAS | 门槛 3、4、5（dream 部分）、10 |
| W5 | rot/效用指标、A/B 实跑、干净 venv e2e、README 校准 | 门槛 9、11，全部门槛复核 |

## 11. 与 ADR-001–018 的一致性

| ADR | 关系 | 说明 |
|---|---|---|
| 001 SQLite 单写者 | 一致 | 新表同库；dream 写 DB 在短事务里 |
| 002 文件交接 | 一致 | episode 引用 handoff，不传对话 |
| 003 审查只降级 | 一致 | §3.2 `needs_review` 行 |
| 004 | 已被 016 取代 | 不涉及 |
| 005 会话桥只读 | 一致，并加强 | §2.6 不写活跃会话、不推送 |
| 006/009 一个 CLI 一个 spec | 一致 | ask/dream 只选 route，不出现 CLI 名 |
| 007/011 配置不含 env | 一致 | `SODUS_ROLE/SODUS_DEPTH` 只进进程环境 |
| 008/017 成本读表 | 一致 | ask/dream 是 run，成本经 ledger |
| 010 回退 | 一致 | ask/dream run 走同一回退规则 |
| 012 写锁 | **扩展** | L3 写入经 `db.lock_path`；`memory.adopt` 迁入（ADR-019 记录） |
| 013 会话能力 | 一致 | `session_ref` 只读 |
| 014 自动验收 | 一致，作为门槛 | §4.2 verdict 直接取自 ADR-014 的事件 |
| 015 提案/worktree | 复用 | ask run 在临时 worktree 跑 |
| 016 流水线归 daemon | 一致 | dream 是 daemon 作业，重启不复活线程，靠 journal + 批次状态重放 |
| 018 单一状态定义 | **扩展** | ADR-020：lifecycle 加派生函数，不加第五份状态 |
| lineage 文档"记忆不再有 SQLite 表" | **修订** | ADR-019：事实仍不进 SQLite；新表只是可重建的暂存/索引 |

## 12. 未决问题（不阻塞 W1）

1. ask run 在 worktree 里跑会多一次 `git worktree add`（秒级）；若实测过慢，退回"非 git 同样拿写锁"的串行方案。
2. `eligible` 曝光量会很大（每个 run × 区块条目数）；W3 实测行数，必要时按 (run, 区块 hash) 记一行而不是逐条。
3. dream 默认便宜路由选哪条，依赖 W1 的 bench 成本数据，不在此拍板。
4. `lifecycle.py` 模块文档仍写"stalled 没有 writer"，与 watchdog 现状不符；属代码注释，W1 改 lifecycle 时一并校准。

## SUMMARY

- 产出：本文件（v0.2）覆盖 brief 的 10 条决策，落点见 §0：入口契约（§2）、状态映射（§3）、记忆四层与 DDL（§4）、
  dream 协议（§5）、曝光/退役（§6）、材料组织（§7）、指标与 A/B（§8）、发行门槛（§9）、分周 MVP（§10）。
- 配套：`docs/architecture.md` 追加 ADR-019（事实源与暂存层分离）、ADR-020（任务状态与入口边界）并更新 ADR 状态行；
  校准了 architecture 与 routing-memory-lineage 中的过时陈述（原文保留，标注校准日期）。
- ADR 冲突：两处扩展（012 写锁覆盖 L3、018 状态派生）与一处修订（lineage 文档"去 SQLite"）均已在 §11 与 ADR-019/020
  中说明，无未说明冲突。
- 实现中发现的缺口（W0 不修，已排进周计划）：`memory.adopt` 不拿写锁（W4）；`lifecycle.py` 模块文档过时（W1）；
  `task.status` 由 tdd/fix 直接写（W1）。
- W0 未修改任何 .py 文件。下一步：W1（迁移框架 + schema v2 + lifecycle 派生 + A/B 功效计算）。
