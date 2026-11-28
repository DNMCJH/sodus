# sodus 近期设计决策复查请求

请对以下设计决策做批判性审查。你的角色是**局外评审者**，不是执行者。
找问题，不要验证我们已经认为对的东西。输出格式：每条发现用 [CRITICAL/HIGH/MEDIUM/LOW] 标注严重度，给出具体理由和建议。

---

## 决策 1：±1 合并策略放宽

**改动**：`policy.py` 里，当 `signals==[]` 且 `rules_confidence < 0.5` 时，允许 Jev 直接覆盖 `final_difficulty`，不再限制 ±1。

**理由**：解决短文本硬任务被低估的问题（infra 学习资料 21 字，rules=2，Jev=4，之前夹到 3，现在直接用 4）。

**潜在风险**：Jev 本身不可靠（TypeSafe System One，~1050ms p50，99.1% 成功率）。放宽之后如果 Jev 误判高，会导致任务被过度路由到昂贵路由。

---

## 决策 2：needs_clarification 触发条件

**改动**：`coordinator.py` 在以下两种情况设 `needs_clarification=True`：
1. `signals==[]` 且 `confidence < 0.55` 且 `difficulty >= 3`（规则盲区）
2. 包含"全套/所有/整套/全面/全量/每一/所有的/全站/全库"等词且 `confidence < 0.65`

**潜在风险**：scope 关键词匹配可能过于宽泛（"所有"在很多正常中文表达里出现）；阈值设定缺乏数据支撑。

---

## 决策 3：cortex.py 直连 Anthropic Messages API

**改动**：`cortex.py` 直接用 `urllib.request` 调 Anthropic API，而不是走 Claude Code subprocess 或 Jev 客户端。走 `routes.json + providers.json` 体系拿 key 和 base_url。

**潜在风险**：
- 绕过了 sodus 自身的 budget 检查（`budget.check()` 没被调用）
- 没有速率限制，没有并发控制
- cortex 调用产生的 token 消耗不会进 `meter.py` 记账

---

## 决策 4：clarify.py 提示词设计

**系统提示**中模型被定位为"需求接收层"，要求输出严格 JSON schema，包含 questions/draft_spec/acceptance_draft/scope_risk/estimated_effort。

**潜在风险**：
- Sonnet 5.5 是否能稳定输出纯 JSON（无 markdown fence，无前置 prose）？有多大比例会解析失败？
- draft_spec 中的 [ASSUMED] 标注靠模型自律，没有结构约束，之后 L2 规划者可能直接吃这个 spec 而忽略假设。

---

## 决策 5：L1 追问后不阻塞派活

**当前实现**：`coordinator.clarification_needed` 事件被发出，`refined_spec.md` 写入 run_dir，但任务**仍然继续派活执行**，不等用户回答问题。

**理由**：fail-open 原则，不因 L1 分析失败阻塞执行。

**潜在风险**：这意味着追问实际上是"事后通知"，不是"事前确认"。高 scope_risk 任务会在用户还没回答问题的时候就已经跑了几百 token。

---

## 决策 6：coordinator 路由 = Sonnet 5.5

**当前配置**：`coordinator` 路由 → `relay-supplement` → `claude-sonnet-5-5`，effort=medium。

**潜在风险**：
- relay-supplement 是补充分组，余额有限，coordinator 频繁调用会消耗这个 key
- 没有降级路由（如果 relay-supplement 不可用，cortex 直接返回 ok=False）

---

请逐条评审，重点关注实现漏洞、架构矛盾和用户体验缺陷。
