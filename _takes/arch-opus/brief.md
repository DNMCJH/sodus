# sodus 架构评审 brief：多宿主入口 / 核心收束 / DSH 面板（2026-11-08）

## 已核对的事实（只读；以 0.6 分支为准）
- 代码：`A:\VScode\Code\Projects\nodus-0.6`（feat/sodus-0.6，0.6.0）；main 仍是 0.5.1，未合并。
- daemon：单进程 `nodusd` 监听 127.0.0.1:4199，Bearer token。路由含 `/ask` `/asks` `/board` `/runs` `/tasks` `/events`(SSE) `/pipelines` `/proposals` `/memory/candidates` `/llm/chat` `/pet` `/health` `/agents` `/providers` `/routes` `/profiles`。
- 后台循环：recover（失败重试）、watchdog（stalled）、observe_sync（扫 agent 会话计费）、`dream.start_daily`（每日巩固）、statuslight/pet。
- 执行面：runner 起 worker 子进程（claude / codex / opencode / dsh），profiles 给每个 vendor 隔离 config dir，propose 用 git worktree 做并行提案，verify 在 run 收尾跑，write_lock 一目录一写者。
- 入口现状：CLI 19 个顶层命令；`sodus mcp` stdio（7 个老工具 + sodus_ask/sodus_ask_status，nodus_* 别名）；`docs/mcp.md` 写了 Claude Code `.mcp.json` 与 DSH `cordis.patch.yml` 的接法。
- **但本机实际没接上**：`A:\dsh\home\cordis.patch.yml` 没有 nodus/sodus MCP；`~/.codex/config.toml` 的 mcp_servers 里没有 nodus；Claude 配置待核。也就是说「从 Codex / Claude Code 调 sodus」目前是代码和文档支持，没有配到你的 harness。
- DSH 插件体系：bundle patch + client module（`window.__ModuleLoader__.load`）+ slots/nav；本机已有 `nodus-pet`（fork dsh-pet，client.ts/nav-icon.ts/DOM patch）与 `plugins/dsh-adaptive-width` 两个例子；安装入口 `dsh plugin --profile web add`。
- 看板：`GET /board` 返回 JSON `board_state`，`/` 返回 board HTML；SSE `/events` 只服务看板。你桌面上有 `nodus-board-snapshot.yml`（Playwright 快照），说明现在确实要开浏览器看。
- 预算：ledger + 日 soft/hard 额度；daemon 单实例、SQLite 单写者。

## 要回答的问题
Q1 **多宿主调用**：Codex / Claude Code / DSH / 其他 harness 调 sodus，应该统一走哪条通道（stdio MCP / HTTP / CLI，还是分层）？怎么把 MCP 注册自动化（例如 `sodus mcp install --host codex|claude|dsh` 写配置）？身份、workspace、session 语义怎么定？现有递归防护（`NODUS_ROLE=worker` / `NODUS_DEPTH`）会不会误伤外部 agent？「单入口」到底该定义成单接口还是单宿主？
Q2 **核心收束**：现在的 daemon 是「核心」还是「功能堆」？给一个目标分层：core（状态机 / 路由 / 账本 / 记忆 / 策略 / 认证 / 事件）vs adapters（DSH、MCP、CLI、看板、pet、tdd/review/fix 流水线、宠物聊天）。哪些该进 core、哪些该拆出去？稳定契约应该是什么（HTTP/OpenAPI、事件流、DB schema 版本？），怎么版本化？要不要把执行面（runner + agents）独立出去，还是保持单进程？给迁移步骤与风险。
Q3 **DSH 的角色**：「DSH 只接收任务，真正执行交给 sodus 分配」这个要求是否值得强制？对比两种模式：A) 现状（DSH 自己也能干活，调 sodus 只是辅助）；B) 瘦客户端（DSH 只保留 sodus MCP + 展示，去掉 shell/写文件能力）。分别评估延迟、故障域、上下文、调试、落地成本。你的推荐是什么？
Q4 **DSH 面板**：基于现有 DSH client plugin API（slot/nav icon/client module）做一个 sodus 面板是否值得？应该展示什么（在跑的 run、needs-you 提案/失败、今日花费、ask 队列、记忆候选）？刷新用轮询 `/board` 还是 SSE `/events`？和现有 pet / 浏览器看板 / 桌面 tray 怎么分工？给最小 v1 的范围与工作量估计（人日），并说明 DSH 升级兼容风险（DOM hack vs 官方 slot）。做/不做/先做什么？
Q5 **0.7 路线**：按优先级给 5–8 项，每项带验收标准（可测），并明确本版不做什么。

## 输出要求
写到当前工作目录的 `take.md`，中文 ≤1200 字；结构：结论 → 目标分层（可用文字/mermaid）→ Q1–Q5 → 风险 → 路线；末尾 `SUMMARY`。只读仓库，不改任何代码。
如果你认为「收束成核心」这个前提本身不成立，直接说，并给证据。

