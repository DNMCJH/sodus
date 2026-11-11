# 更新日志

## 0.9.0 — 全面更名为 sodus

- 包名、Python 模块、CLI 命令、MCP 工具名、状态目录、数据库文件、环境变量统一改为 sodus；nodus 命令和包移除。
- sodus migrate-home --apply 把 ~/.nodus 复制到 ~/.sodus，数据库改名为 sodus.sqlite3；源目录不动。
- MCP 工具名 nodus_* 改为 sodus_*，宿主需重跑 sodus mcp install。

## 0.8.1 — Plan execution

- Schema v8: node run evidence and persisted replan feedback, with v7 migration.
- Approved plans materialize idempotent tasks and execute a serial DAG with budget
  gates, failure stop, concurrent execution protection and daemon restart reconcile.
- Add daemon HTTP and CLI plan execute/replan; replanning creates a new parent-linked
  version through the opus planner and requires fresh review and human approval.

## 0.8.0（2026-11-09）

交接台与无人值守安全边界：

- **项目级 `sodus handoff`**：当前项目解析 git root，默认 `HANDOFF.md`（config `handoff.path` 可覆盖）；`init/show/append/sync/check/list`；`sync` 只改 `<!-- sodus:auto:start/end -->` 机器段，`check` 校验 snapshot、HEAD、关键路径、daemon 版本；HTTP `GET /handoff`、`GET /handoffs` 与 MCP `nodus_handoff_show/list/check` 只读；不做内容聚合、不做自动 hook。
- **B3 worker 环境隔离**：`build_env` 改白名单，不再继承 daemon 全部环境；provider secrets 仍按需注入；plan/plan_review 使用只读参数（codex `--sandbox read-only`、claude `--permission-mode plan`），并在临时 git worktree/过滤副本中运行，结束后清理；隔离失败回退原 cwd 并记录 warning。
- **B4 批准门禁**：HTTP `POST /plans/{id}/approve` 一律 403；`sodus plan approve` 只能在交互式 TTY 中确认（显示摘要并要求输入 plan id），拒绝 `NODUS_ROLE=worker`/`NODUS_RUN=1`；确认后本地调用 `plan.approve`，保留 plan hash/HEAD/AGENTS/validator 复核。
- **已知边界**：同机同用户仍可伪造 TTY、读 token 或直连 SQLite，这不是密码学身份隔离；plan/review 的 git 隔离是 HEAD 快照，不含未提交改动；无 OS 级沙箱。


## 0.7.2（2026-11-09）

- 预算改为按模型族软上限：Claude/opus 系 ¥100、GPT 系 ¥80、其它合并 ¥80（CNY，0/缺省=off）。只有软门禁，`--allow-expensive` 可豁免；没有日硬上限，relay 余额仍是真正硬约束。按已记账成本检查，不预留未来/在途成本。
- `sodus mcp install --host opencode`：写 `~/.config/opencode/opencode.json` 的 `mcp.sodus`（local stdio），保留 provider 等其它字段，原子写 + 备份，支持 `--check`/`--uninstall`；四宿主（Claude Code/Codex/opencode/DSH）入口齐。
- 四终端并发：文档说明多 MCP 客户端共用单 daemon、不同 cwd 并行、同一 cwd 写锁串行、SQLite 单写者；新增 in-process ThreadingHTTPServer 四线程并发测试。
- 实机启用：`~/.nodus/config.json` 写 `budget.soft_families`；opencode MCP 条目由 `sodus mcp install --host opencode` 写入。


正式启用前的稳定性/安全版，汇总 0.7.0 发布前复查的两份 review：

- daemon 单实例：独占 bind（Windows `SO_EXCLUSIVEADDRUSE`）+ 先 bind 后 reconcile/recover + 启动锁；`auto-recover` 改为配置项、默认关闭。
- 安全：Host 头校验（拒绝非 loopback，防 DNS rebinding）；approve 前复核 HEAD/AGENTS 哈希与 plan hash；超时会终止 worker（杀进程树）；`mcp install` 合并用户 env、原子写；DSH 缺失记为 skipped。
- 质量信号：Windows 控制台终止码归为 infra 失败并自动重试 verify 一次，不再误判测试红；review 异步化 + 超时。
- planner 完善：validator 增加 base_sha 漂移 warning、AGENTS.md 哈希、acceptance 命令白名单；review 异步；plan_execution 走 schema v7；CLI reconcile 经 daemon。
- 已知阻塞（0.7.2）：worker env 白名单与 plan/review 只读沙箱、独立批准身份；plan 节点执行/物化/replan 未实现。因此 0.7.1 适合有人值守日常使用，不适合无人值守或自动批准执行。


多宿主入口、核心契约与 planner 的第一个版本：

- MCP 工具全部改由 daemon HTTP 执行（不再在 MCP 进程内起 run），daemon 成为唯一 run 所有者；`sodus mcp install --host codex|claude|dsh` 幂等写配置，带 `--check` / `--uninstall` / `--dry-run` 与备份。
- 新增只读 `GET /state`（running/counts/today/asks/memory，本地 p95 < 200ms）；`server.py` 的看板文案与渲染拆到 `nodus/board.py`，`server.py` 从 2767 行降到 725 行。
- planner：`plan.json` v1 契约 + schema v6 + 机械 validator（base_sha、路径存在、DAG 无环、acceptance、预算）；`sodus plan validate/import/new/review/approve` 与 `POST /plan`、`GET /plans/{id}`；planner 为 `kind='plan'` 只读 run（默认 opus 路由），sol 异源评审，批准需显式 `--by human`。
- 已知边界：plan 节点执行与预算派发留给后续版本；批准人身份靠显式声明，不做独立身份校验；plan 只读靠提示词，不提供文件系统沙箱。

围绕记忆闭环和轻量提问入口的一次功能版，汇总 W0–W3：

- `/ask` 提供 HTTP 异步提问、幂等请求、状态轮询和人工验收后 promote；MCP 同时提供 `sodus_ask` / `sodus_ask_status`。
- 记忆暂存层增加 episode、candidate、exposure 与 dream batch；可 dry-run/执行 dream plan、extract、count，支持历史 episode 回放。
- L3 事实通过 marker 区块写入 `AGENTS.md`，支持 reindex、apply/adopt 与版本 rollback；life-cycle 状态派生覆盖 stalled、unverified 等状态。
- daemon 增加每日 dream 触发；保留失败占用当日触发窗口的已知语义，详见 handoff。

版本统一为 0.6.0；新增 `sodus debug memory-stats` 只读记忆 rot 指标和发行 e2e 门禁。

## 0.5.1（2026-11-08）

0.5.0 之后的稳定化版本：不新增子系统，补默认路由、预算软闸门、opencode 权限与 daemon 状态一致性，让 sodus 可以交给其它会话稳定使用。

### 默认路由
- `config.routing.default` 决定不带 `--route` 时的 route（当前 `sol`），`sodus run` 不再硬编码 claude；`config.default_route()` 缺失时退回 claude，老配置行为不变。
- `server.py` 的默认 route 同样从配置读取；新增 `tests/test_default_route.py`。

### 预算软闸门
- `budget.soft_cny`：当日花费过软线后，`opus` / `claude` 两类 route 和 `claude-opus*` 模型需要显式 `--allow-expensive` 才会派发；硬上限 `daily_cny` 语义不变。
- `sodus run --allow-expensive`、`POST /runs` 的 `allow_expensive`、`sodus status` 的 `soft=` 行同步加上；新增 4 个预算测试。

### opencode
- `opencode run` 加 `--auto`：headless 下读取类工具不再被静默拒绝（此前表现为「无输出」而不是权限错误）。

### daemon 状态
- CLI 自动拉起 daemon 时把实际 pid 写回 `daemon.json`；此前 `ensure_daemon` 只启动不记账，`down`/`up` 会指向已死 pid。新增回归测试。

### harness 统计
- 新增只读的 harness 指纹与统计：`nodus debug harness-stats`。
  - `nodus/harness.py`：brief 模板哈希（`version_of`）、run 的 harness 指纹（agent/route/model/provider/brief_version）、按 (harness 组件, cause) 聚合的统计。纯函数，不写库、不调度。
  - `nodus/runner.py`：worker prompt 抽成 `PROMPT_TEMPLATE` 常量，版本是模板的哈希（改文案就换版本）；每个新 run 追加一条 `run.harness` 事件记录版本，历史 run 没这条事件时退回「未记录」，不猜。
  - `nodus debug harness-stats [--by route|agent|model|provider|brief_version] [--kind work|chat|all] [--limit N]`：看「某条路线遇到某类失败时，跑完并且验证通过的比例」。**unverified 不进分母**（退出码不是验收证据），在跑的 run 不计。
  - `nodus/db.py`：`event_payloads`（一次批量取某类事件）与 `count_runs`。
  - 这条命令只提供计数，不做推荐、不改行为；候选 harness 的自动搜索没有做。

## 0.5.0（2026-11-03）

产品名 Sodus，实现名 nodus。这一版只改**显示名和入口面**，不动模块位置、仓库目录和默认状态目录。

### 阶段 B：显示名与对外接口名

- docs（`cli.md`、`mcp.md`、`vision.md`、`routing-memory-lineage.md`、`branding/README.md`、`branding/sodus/README.md` 及历史报告）：正文称呼统一为 Sodus，路径和命令仍写 nodus；每份首行注明「产品名 Sodus，实现名 nodus」。
- `docs/branding/README.md` 的改名阶段按 A/B/C/D 重写：A 已完成，B 与 C1 本版完成，C 的模块搬迁和 D 的仓库改名还没做。
- `openapi.yaml`：`info.title` 改 `Sodus HTTP API`，`summary`/描述改称 Sodus；路径、字段、`info.version` 不动。
- `nodus/mcp.py`：`SERVER_NAME` 改 `sodus`（`serverInfo.name`）；**工具名仍全部是 `nodus_*`**，客户端配置不用改。
- `nodus/cli/__init__.py`：描述行改 Sodus 的产品描述；`--version` 打印实际入口名（`nodus 0.5.0` / `sodus 0.5.0`），usage 行跟随入口名。
- `nodus/pet.py`、`nodus/ui.py`：检查后没有残留的用户可见 "Nodus"（阶段 A 已改完看板），只剩注释；`pet_bridge` 配置键和 `nodus-theme` localStorage 键不动。

### 阶段 C1：入口面与状态目录

- 新增 `sodus/` 包：`sodus/__init__.py`（版本 re-export）、`sodus/cli.py`（转发 `nodus.cli:main`，只改 usage 里的名字）、`sodus/__main__.py`。
- `pyproject.toml`：`nodus = "nodus.cli:main"` 保留，`sodus = "sodus.cli:main"`；打包 `include = ["nodus*", "sodus*"]`；版本 0.5.0。
- 版本号统一：`nodus/__init__.py` 的 `__version__`、`config.VERSION`、`pyproject.toml` 都是 0.5.0（此前包内是 0.2.0，发行版是 0.4.0）。
- 状态目录解析（`nodus/config.py`）：`SODUS_HOME` > `NODUS_HOME` > 已存在的 `~/.sodus` > `~/.nodus`。不自动建 `~/.sodus`，不自动搬数据。
- 新增 `nodus migrate-home`：把 `~/.nodus` 复制到 `~/.sodus`，校验 sqlite `integrity_check` 与 task/run/artifact 行数；默认 dry-run，`--apply` 才落地，失败删除半成品，绝不移动或删除原目录。
- 新增 `docs/sodus-migration.md`（切换与回滚说明）和本文件。

### 没改的东西

- 模块仍全在 `nodus/` 包里，没有搬迁（C2/C3）。
- 仓库目录仍叫 `nodus`，远程和 CI 不动（阶段 D）。
- MCP 工具名仍是 `nodus_*`，`serverInfo.name` 之外没有任何协议变化。
- 默认状态目录仍是 `~/.nodus`；只有手动跑 `migrate-home --apply` 之后 `sodus` 才会自动读 `~/.sodus`。
- 托盘 / 桌宠 / MCP 客户端配置不需要改。

