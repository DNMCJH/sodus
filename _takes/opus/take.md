# take：单对话框入口 + 学习型记忆层（opus，2026-11-08）

**结论**：对话框继续用 DSH，sodus 只当 Router 后的任务账本和验收方，两者并存，不做替代。记忆层就是 `memory.py` 加一个异步的"做梦"步骤，产出提案，由人在看板上采纳。

## 1. 单对话框分层
- **对话壳**：DSH GUI（或看板上的一个输入框）。不存状态，符合"Sodus 不是聊天客户端"。
- **Router**：在 `server.py` 新增 `POST /ask`，同时作为 MCP 工具 `sodus_ask` 暴露。先用 `policy.assess` 判断：难度≤2 由 `llm.py` 直接回答；否则建 task 并返回 task id。
- **任务存储**：沿用现有 SQLite 的 task/run/event 表和 lineage，不新增表。
- **Worker**：沿用 routes + runner + verify/recover。

## 2. 记忆层
- **L0 原始**：transcript 和 run 事件。只引用路径，不复制。
- **L1 情节**：每个 task 结束时写一条 `memory/episodes.jsonl`：`{task,route,verdict,cause,cost,lesson}`。
- **L2 候选**：`memory/candidates.jsonl`：`{id,kind:decision|lesson|preference|command,text,sources[],verified,hits,last_seen,status}`。
- **L3 playbook**：项目 `AGENTS.md` 里的「已验证事实」，加上 `playbooks/<类型>.md`。
- **索引**：关键词或 SQLite FTS，不引入向量库。

**做梦**的触发条件：daemon 空闲，且新增 episode ≥5 条或到了夜间；预算闸放行后才跑，用 dsh 这类便宜路由。它负责去重、合并、标出矛盾、给长期未命中的条目标过期，把重复出现 ≥3 次的经验提升为候选，最后输出 AGENTS.md 的 diff，放进 Needs Review。

**防 memory rot**：只有 exit code 通过的 run 才能产出 lesson；每条都带 source 和 last_seen；每个文件有条数上限；遇到矛盾一律交给人裁决。

**指标**：用 bench 对比开/关记忆的 solve 率；同一 cause 的重复失败率；候选被采纳的比例；brief 的 token 数。

## 3. MVP（4 周）
- W1：`/ask` 和 MCP 工具（第一步做这个）。
- W2：run 结束时写 episode。
- W3：先做手动命令 `nodus dream`，跑通后再挂到 daemon 定时执行。
- W4：用 bench 做 A/B。

验证标准：DSH 里一句话能建出 task，完成后能查到 episode；dream 的 diff 被采纳后，bench 上同类任务成功率不下降。

## 4. 取舍
**采用**：sessionless 的 Task、小上下文 worker、Needs Review、"安静时不烧 token"（用 observe_sync 式轮询）、dreaming 的人工审核模式和纯文本输出。

**不采用**：多租户、云 VM、eBPF、Lean 形式化、自动应用模式、Elixir 重写。

## SUMMARY
对话框继续用 DSH，在 sodus 加一个 `/ask` Router，复用 task/run。记忆分 L0–L3 四层，"做梦"产出 AGENTS.md 提案，由人采纳，效果用 bench 的 A/B 验证。第一步：`/ask` 和 MCP 工具。
