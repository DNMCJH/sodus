# Sodus 单对话框入口 + 学习型记忆层：设计规格 v0.1（待评审）

> 目的：确定 sodus 0.6 的设计，评审通过后开工。先决条件：0.5.x 必须是可发行、可交给其它会话稳定使用的版本。

## 1. 目标与非目标
目标：
- 用户只面对一个对话框（DSH），sodus 在背后做 Router、任务账本、验收。
- 记忆跨任务、跨会话积累；有异步巩固（"做梦"）机制，能组织已学材料并抑制 memory rot。
- 最小改动：复用现有 task/run/event/memory/sessions/MCP，不重写 harness。

非目标：多租户、云 VM、eBPF/Lean、向量库、自动应用默认开、自带聊天客户端。

## 2. 入口
### 2.1 POST /ask（MCP 工具 sodus_ask）
请求：{request_id, text, cwd?, acceptance?, budget_cny?, urgency?}
响应：{kind: "answer"|"task", answer?, task_id?, needs_input?: string, route?, reasoning?}
- request_id 幂等：重复请求返回同一结果，不重复建 task/run。
- 先 policy.assess(text, context) 判断难度/类型：低难度由 llm.py 直接回答（走便宜路由，预算计入）；否则建 task。
- acceptance 缺失时返回 needs_input 追问，不自动编验收标准。
- 决策（为什么答/为什么建 task/选了什么 route）写 event，可事后审计。

### 2.2 任务状态机
open -> running -> needs_review -> done | failed | cancelled
- needs_review：agent 认为完成、或需要人拍板；消息回到对话框。
- 复用 task/run/event；run 仍是 attempt；lineage 复用 parent_run_id + session 关联。
- 完成通知：SSE / MCP 轮询 / 下一条消息附带。

## 3. 记忆
### 3.1 分层
- L0 证据：session/run/event/handoff，只存引用（id/path），不复制正文。
- L1 episode：每个 task 结束写一条（结构见 3.2），只在高置信来源（exit=0 的 run、人工确认）写入。
- L2 candidate：巩固产出的候选记忆，SQLite 持久化，带版本与状态。
- L3 playbook：人工采纳后渲染成 markdown（项目 AGENTS.md「已验证事实」+ playbooks/<topic>.md），继续可读可 diff。

### 3.2 SQLite 表（建议）
memory_episode(id, task_id, run_id, workspace, route, verdict, cause, cost_cny, tokens, started_at, ended_at, summary, lessons_json, evidence_json, created_at)
memory_candidate(id, workspace, kind, key, text, sources_json, hash, status, version, supersedes, expires_at, hits, last_seen, created_at, updated_at)
  kind: decision|lesson|preference|command|material|playbook
  status: proposed|adopted|rejected|retired|superseded
memory_fts (FTS5: text, key, kind, workspace) —— 可重建索引
- 项目隔离：workspace 字段 + 查询层强制过滤。
- 上限：每 workspace 候选条数上限、文本长度上限；超限按 last_seen/hits 淘汰。

### 3.3 "做梦"（巩固）算法
触发：daemon 空闲 +（新 episode >= N 或 每晚一次 或 手动 sodus dream）；预算闸门 + 每日巩固预算上限；用便宜路由（dsh）。
步骤：
1) 取增量（未处理的 episode + 近期 candidate）；
2) 按 key/主题分组；
3) 近重复合并（hash + 相似度，沿用 memory.py 0.85）；
4) 矛盾检测：规则命中 + LLM 只做"挂起/裁决建议"，不自动覆盖；
5) 过期退役：last_seen 超期且 hits=0；
6) 跨会话模式提升：同 key/同类经验出现 >=3 次 -> playbook 候选；
7) 输出提案（add/update/retire 的 diff，含 sources）；
8) 默认进入 Needs Review（看板/对话），人工采纳才写 L3；
9) 采纳落盘：版本+1，旧条 superseded，写审计日志；可回滚到旧版本。
硬规则：只提案不自动应用（自动应用仅限低风险 kind，默认关）；单写者；幂等可重跑；失败不写半成品。

### 3.4 材料组织
kind=material：source、摘要、标签、引用、last_used。dream 生成主题索引页（knowledge/<topic>.md），合并重复、标记过期；第一阶段只在 sodus 内，不接 Obsidian。

## 4. 指标与验证
Memory rot（每周自动统计）：重复率、矛盾数、过期占比、条目增长率。
效用：检索命中率、提案采纳率、同 cause 重复失败率、任务通过率、每任务 token/成本、dream 自身成本。
A/B：bench 任务 + 历史 run 回放，随机开/关记忆，配对比较；预先定主指标、n 与停止规则；不显著写"不可区分"。

## 5. MVP 排期
W1：内存 schema + 历史 run 回放生成 episode + sodus dream --dry-run（只提案）。
W2：POST /ask + MCP sodus_ask（request_id 幂等）+ needs_review。
W3：dream 接 daemon 空闲/夜间触发 + 审核看板 + 采纳/回滚。
W4：A/B 与 rot 指标；根据结果决定是否默认开。

## 6. 可发行版门槛（动工前）
- 全量测试通过；wheel/sdist 可构建；干净 venv 安装后 sodus --version/doctor/mcp 可用。
- 版本语义、CHANGELOG、README/文档与实现一致；OpenAPI/MCP 契约版本更新。
- daemon 启停/状态文件一致（已修 ensure_daemon pid 写入）；不依赖开发机路径。
- 其它会话能只用 CLI/MCP 文档完成：建 task -> run -> 看状态 -> 结果。
