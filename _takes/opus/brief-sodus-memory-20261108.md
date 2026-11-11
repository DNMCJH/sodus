# 任务：sodus 单对话框主入口 + 学习型记忆层 设计评审

## 背景
- sodus 0.5.0 现状（可读仓库 README/docs/architecture.md）：task/run 是持久执行状态，daemon 常驻 127.0.0.1:4199，routes 把任务派给 claude/codex/dsh/opencode 等 CLI agent；有 sessions/briefs/resume，有 SQLite 账本、预算闸门、auto-recover/watchdog。当前入口是 CLI 命令，不是对话。
- 用户原本设想 sodus 做主入口，但最近看到一个更简洁的方向：**只有一个对话框**，不要把 sodus 做成通用 harness。参考产品 Comma（AFK AI，开源，Elixir + Lean）：
  - sessionless：对话只是界面，**Task 才是持久执行状态**；记忆/上下文/任务跨设备、跨对话延续。
  - 单个常驻 Router 接所有请求，小问题聊天里答，大工作变成 Task；Task 由独立 worker 循环 plan/execute/review/verify，直到完成。
  - worker 上下文隔离（小上下文减少压缩和漂移）；"你管目标，不管 swarm"。
  - Loop：agent 自己写的小程序盯着 inbox/repo/feed，有事才唤醒 agent，安静时不烧 token。
  - Routines / Needs Review / 任务看板；可把 Codex、Claude Code 等当外部 worker runtime 调用。
- 记忆层参考 Anthropic 的 "dreaming"（Claude Managed Agents，2026-05 research preview）：
  - 异步、定时地在会话之间做记忆巩固：回看 transcript，去重/合并/删除过期与矛盾记忆，重组存储，识别跨会话模式并提升为可读的 playbook/heuristic。
  - 可配置频率与更新模式（自动应用 vs 人工审核）；不改模型权重，不是 RAG；产出是纯文本、可审计。
  - 它针对的失败模式叫 "memory rot"：重复、矛盾、过期、杂物、模式盲区，agent 越跑越不可靠。

## 请你回答（≤800 字，结论先行）
1. **单对话框入口怎么分层**：对话壳 / router / 任务存储 / worker 各自边界；sodus 与 DSH（DeepSeek Harness，当前执行与记忆载体）的关系是替代还是并存？给出最小改动路径（基于现有代码，不要重写）。
2. **记忆层怎么设计**：存什么（任务、决策、教训、偏好、会话摘要？）、分几层（raw / 摘要 / playbook / 索引？）、巩固（"做梦"）何时触发、如何组织"已学到的材料"、怎样防止 memory rot、用什么指标评估它真的有效。
3. **2–4 周 MVP**：具体数据模型/接口/文件格式，第一步先做什么，怎么验证。
4. **取舍**：Comma 和 Anthropic dreaming 哪些做法适合单用户本地 Windows + sodus/DSH 现状，哪些不适合（例如多租户、云端 VM、eBPF、形式化验证没必要照抄）。

## 约束
- 单用户、本地 Windows、sodus + DSH + Claude Code/Codex/opencode；SQLite；预算有限；优先小改动。
- 输出写到 reports/take-<你的名字>-20261108.md，最后附 SUMMARY。
