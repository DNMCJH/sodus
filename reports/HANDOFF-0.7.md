# HANDOFF 0.7.0（2026-11-09）

## SUMMARY

sodus 0.7.0 已合并进 main（merge commit `94555b8`，tag `v0.7.0`），daemon 已重启为 0.7.0（pid 150512，单实例，auto-recover 关闭）。release_check PASS；MCP 入口、/state、planner 契约与执行链都已落地。

## 这版交付

- **MCP 收口**：`nodus/mcp.py` 所有工具改走 daemon HTTP，不再在 MCP 进程里起 run；daemon 是唯一 run 所有者。新增 `sodus mcp install --host codex|claude|dsh`（幂等/备份/`--check`/`--uninstall`/`--dry-run`），本机三处均已 `configured`。
- **/state**：只读 `GET /state`（running/counts/today/asks/memory），本机冷请求 162ms；测试内 p95 ~33ms。看板展示层从 `server.py` 拆到 `nodus/board.py`（server.py 2767→725 行），`GET /board` 行为不变。
- **planner 0.7.0**：`plan.json` v1 + schema v6 + 机械 validator（base_sha、路径存在、DAG 无环、acceptance、预算等）；`sodus plan validate/import/new/review/approve` 与 `POST /plan`、`GET /plans/{id}`、review/approve 端点；planner 为 `kind='plan'` 只读 run（默认 opus 路由），CLI 全部经 daemon HTTP，planning 有总超时、daemon 启动 reconcile、递归防护。
- **无窗口修复**：git/taskkill/zstd/verify 等 console 子进程统一 `creationflags=config.NO_WINDOW`（用户要求跑 sodus 不弹终端）；新增 `tests/test_no_window.py` 防回归。

## 验证证据

- release_check：`scripts/release_check.ps1` PASS，退出码 0（构建 0.7.0 wheel+sdist、隔离 venv、doctor、MCP tools、DB 升级冒烟、全量 pytest）。
- 全量 pytest：0.7 分支定向/全量多次退出码 0（c3 1091 项通过）；合并后的 main 与 release_check 覆盖同一棵树。
- 实机：`sodus --version` 0.7.0；`/health` version 0.7.0；`GET /state` 返回 version 0.7.0、running/counts/today/asks/memory 字段；`sodus mcp install --check` 三宿主 configured（codex/claude/dsh）。
- 真实 planner（M4–M6）：`plan-70cceecf7ca5`，status `validated → reviewed`，base_sha `94555b8…` 与目标 worktree HEAD 一致，validator `errors=[]`；5 个节点（n1 drift 可恢复、n2 漂移展示、n3 approved 中间态、n4 episode 刷新、n5 发布收尾），file_scope 均指向真实文件，acceptance 均为可执行 pytest 命令。证据：`reports/plan-M4-M6-20261109.json`。
- sol 异源评审：verdict **revise**，3 条 findings（P1 episode rowid 复用导致 dream 游标漏取；P1 无新 rowid 时补偿队列不会触发批次；P2 approved 候选在 busy/drift 时会从看板消失）。说明评审门禁真的拦住了问题，M4–M6 计划需按这些 findings 修订后再批准。

## 使用

- 多宿主：重启 Codex/Claude/DSH 后，MCP 工具由 daemon 执行；`sodus mcp install --check` 可查，`--uninstall` 可撤。
- 面板/脚本数据：`GET /state`（token 鉴权，见 `~/.nodus/token`）；浏览器看板仍在 `http://127.0.0.1:4199/`。
- planner：`sodus plan new "<目标>" --cwd <仓库> --route opus --wait`；`sodus plan review <id>`；`sodus plan approve <id> --by human`（当前 plan 评审为 revise，未批准）。
- 发行门禁：`powershell -ExecutionPolicy Bypass -File scripts/release_check.ps1`。

## 已知缺口（0.7.1/0.7.2）

- plan 节点执行/预算派发/物化成 task 未实现；replan 未实现（本次 review 的 revise 需要一次新版本计划）。
- review 仍同步占 handler 线程、固定 sol、无超时（medium 5）。
- validator 漏检：base_sha 漂移 warning、AGENTS.md 哈希核对、acceptance 语义白名单（medium 6）。
- mcp install 在 drift 时会整条覆盖（丢用户 env）、DSH 缺省 skip 逻辑、原子写入（medium 7）。
- 批准人身份只是 `--by human` 声明，无独立校验；plan 只读靠提示词，无文件系统沙箱；daemon 重启对 planning 只做 failed 归零，不恢复。
- DSH 的 MCP 条目已写入 `cordis.patch.yml`，但当前 DSH GUI 未重启，需重启后生效；本会话用的是旧 MCP 进程。

## 回滚/预算

- 回滚：`git reset --hard v0.6.0`（或 revert merge `94555b8`），重启 daemon；回退前备份 `~/.nodus/nodus.sqlite3`（schema 已到 v6，0.6 代码可读，只增表）。
- 预算：为跑 opus planner，`~/.nodus/config.json` 的 `budget.daily_cny` 50→200、`soft_cny` 20→200（备份 `.bak-20261109` / `.bak-20261109-soft`）；relay 剩 123.68。
