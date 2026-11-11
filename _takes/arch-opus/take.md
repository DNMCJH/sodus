# sodus 架构评审 take（opus，2026-11-08，基于 nodus-0.6）

## 结论
「收束成核心」成立，但真正的问题不在「功能多」，在**两个状态所有者 + 展示逻辑长在 core 里**：
1. MCP 老工具（`nodus_run` 等）在 MCP 进程内 `runner.RUNNER.start` 自起 run、自开 SQLite；daemon 启动会把它当孤儿收掉（docs/mcp.md「二选一」）。
2. server.py 2692 行，约 2000 行是看板文案/证据拼装；`/board` 每次现算 git 证据，pet 实测 2~4s。
所以 0.7 先做「daemon 唯一所有者 + 契约化」，不拆进程。

## 目标分层
```
core(nodusd 进程): 状态机 lifecycle / db+schema / runner+profiles+propose+verify / ledger+budget / policy+routes / memory+dream / auth / 事件总线
contract: openapi.yaml(HTTP) + /events 事件类型表 + schema_version
adapters(只说 HTTP): mcp(stdio→HTTP) / cli / board(ui) / pet+statuslight / DSH 面板 / tdd·review·fix 流水线 / llm 宠物聊天
```
board_state 的文案层下沉到 adapters/board，core 只返回结构化事实。

## Q1 多宿主
- **分层单接口**：唯一契约是 HTTP；MCP 是给 agent 宿主的薄壳，CLI 给人。「单入口」=单接口（HTTP 契约），不是单宿主。
- MCP 所有工具改为转发 HTTP（删进程内 runner 路径）。
- `sodus mcp install --host claude|codex|dsh [--dry-run]`：分别写 `.mcp.json`/`~/.claude.json`、`~/.codex/config.toml [mcp_servers.sodus]`、`cordis.patch.yml insert:`；幂等、先备份、`--check` 只报差异。本机三处目前都没接。
- 身份：`origin`=宿主名（claude/codex/dsh），`client`=实例，`session_ref`=宿主会话 id；workspace 由 cwd→git root 推导，显式参数优先。
- 递归防护：`NODUS_ROLE/DEPTH` 只由 `agents.py` 注入 sodus 起的 worker，外部 agent 默认 depth=0，**不会误伤**；但 worker 直打 HTTP 可绕过——改为 runner 给每个 run 发一次性 run-token，HTTP 侧按 token 判深度。

## Q2 核心收束
- 进 core：上面 core 一行；拆出：看板文案、pet/statuslight、llm 聊天、observe_magpie/ccswitch 探针、tdd/review/fix（变成调用 `/runs` 的客户端）。
- 稳定契约：openapi.yaml（semver，`/v1` 前缀，破坏性改动加 `/v2`）+ 事件类型枚举（`event.v` 字段）+ DB schema_version 仅 core 内部，不对外承诺。
- 执行面**保持单进程**：SQLite 单写者、write_lock、reap 都依赖「一个所有者」；拆进程要换 IPC 和分布式锁，收益为零。
- 迁移：①MCP 转 HTTP ②`/board` 拆 `/state`（结构化、可缓存）+ 渲染层 ③流水线改走 HTTP ④openapi 契约测试。风险：老 MCP 用户需 daemon 常驻（`sodus mcp` 自动 `up`）。

## Q3 DSH 角色
| | A 现状 | B 瘦客户端 |
|---|---|---|
| 延迟 | 本地直接干，低 | 每步多一跳 + 排队 |
| 故障域 | 互相独立 | daemon 挂 = DSH 全瘫 |
| 上下文 | 对话内连续 | 被切到 worker，需回传 |
| 调试 | 一处 | 两处日志 |
| 成本 | 0 | 要改 DSH 能力配置 |

**推荐 A + 约定**：不强制。小改动 DSH 自己做；跨仓库/长任务/要验收/要并行提案时走 `sodus_ask`→promote。用 skill/提示词约定而非砍能力。

## Q4 DSH 面板
- 值得，但排在 MCP 收口之后。v1 只做：在跑 run、needs-you（提案/失败）、今日花费；ask 队列与记忆候选 v2。
- 刷新：SSE `/events` 触发 + 节流后拉一个**轻量** `/state`（不要轮询 2~4s 的 `/board`）。
- 分工：DSH 面板=工作中一眼看；浏览器看板=深度处理（merge/reject/候选审核）；pet=提醒；tray 不再加功能。
- 只用官方 `shell.overlay`/`settings.section` slot，不做 nav 图标 DOM hack（nodus-pet 已证明上游升级会断）。工作量：面板 1.5 人日 + `/state` 1 人日。

## 风险
daemon 变唯一依赖；`/v1` 化要兼容老路径一版；DSH slot 契约无版本承诺。

## 0.7 路线（按优先级）
1. MCP 全转 HTTP——验收：daemon 运行时 MCP `nodus_run` 不再产生孤儿；进程内无 `RUNNER.start` 调用。
2. `sodus mcp install`——验收：三宿主 `--check` 由缺→齐，重复执行零 diff。
3. run-token 深度——验收：worker 带 token 直打 `/ask` 返回 recursion_denied 测试。
4. `/state` 轻量端点——验收：p95 <200ms，字段进 openapi。
5. 文案层移出 server.py——验收：server.py <1000 行，看板快照测试不变。
6. openapi 契约测试 + 事件类型表——验收：CI 中路由与 openapi 不一致即失败。
7. DSH 面板 v1——验收：DSH 升级后只用官方 slot 仍加载；三块数据与 `/state` 一致。

**本版不做**：拆执行面进程、多用户/远程、DSH 瘦客户端改造、tray 新功能、nav 图标。

## SUMMARY
前提成立：问题是 MCP 与 daemon 双所有者、看板文案寄居 server.py。目标：单进程 core + HTTP 唯一契约，MCP/CLI/看板/pet/DSH 全是 HTTP adapter；单入口=单接口。DSH 保持能干活不强制瘦身；面板做但用官方 slot + SSE 触发轻量 `/state`。0.7 先收 MCP、自动注册、深度 token，再拆 `/state` 与文案层，最后面板。
