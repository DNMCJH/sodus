# sodus L1 设计决策复查报告

- **评审对象**：`scripts/_review_brief.md` 中的 6 条决策
- **评审角色**：局外评审者（只找问题，不验证既有结论）
- **评审方式**：静态代码审查，覆盖 `sodus/policy.py`、`coordinator.py`、`cortex.py`、`clarify.py`、`runner.py`、`budget.py`、`meter.py`、`decision.py`、`providers.py`、`routes.py`
- **测试状态**：**未执行测试**。本机 `pwsh` 启动时因 workspace ACL 错误失败，未能运行 pytest；下文所有“会/不会发生”的判断均来自代码路径静态推导，已尽量给出精确行号。
- **总评**：当前 L1 是一个“会产生费用、会阻塞派发、但不会影响执行”的旁路。它既没有预算/计量闭环（决策 3），也没有答案回灌闭环（决策 5）；同时“规则盲区”在决策 1 和决策 2 里有两个不同定义，导致两条决策互相拆台。

## 评级汇总

| # | 决策 | 评级 | 一句话理由 |
|---|------|------|------------|
| 1 | ±1 合并策略放宽 | **HIGH** | 实际触发条件比 brief 宽（`tool_use` 也算），Jev 可一步把极短任务从 2 拉到 5，并连带触发 blocking review 的整轮额外执行。 |
| 2 | needs_clarification 触发条件 | **HIGH** | 用的是被 Jev 抬高后的 composite confidence，结果是“Jev 正常时几乎不追问，Jev 故障时才追问”的逆向选择；关键词表也与 brief 不一致。 |
| 3 | cortex.py 直连 Anthropic | **CRITICAL** | 预算、计量、并发、速率、降级全部缺失；更糟的是“fail-open”在 JSON 解析层不成立，异常会击穿整个 `_decide_route` 并静默改回默认路由。 |
| 4 | clarify.py 提示词设计 | **HIGH** | 提示词里的 JSON 样例本身带 `//` 注释，是非法 JSON；输出又只给 1024 token，解析失败后没有重试，且类型未校验会触发异常。 |
| 5 | L1 追问后不阻塞派活 | **CRITICAL** | 没有答案回灌路径，`refined_spec.md` 也不会被 executor 读取；追问既不影响执行，也不进入预算/审查门控，属于“只写不读”的装饰性功能。 |
| 6 | coordinator 路由 = Sonnet 5.5 | **HIGH** | `coordinator.plan()` 根本不调用 cortex，Sonnet 只被 clarify 使用；`effort=medium` 在直连 API 里被完全忽略，且无 provider/route 级降级。 |

---

## 决策 1 [HIGH]：±1 合并策略放宽

### 事实纠偏：实现条件不是 `signals==[]`

brief 写的是“`signals==[]` 且 `rules_confidence < 0.5`”，但 `policy.py:556-568` 的实际条件是：

```python
strong = bool(set(rules["signals"]) & {"code", "code_path", "analysis", "ops", "trivial", "long_context"})
if jev_difficulty > rules["difficulty"]:
    if not strong and rules["confidence"] < 0.5:
        final_difficulty = jev_difficulty
    else:
        final_difficulty = min(5, rules["difficulty"] + 1)
```

`strong` 不包含 `tool_use`、`writing`、`retrieval`、`vision`、`code_weak`。而 `_rules` 的 confidence 在 `signal_count == 0` 时正好是 0.4（`policy.py:349-357`），`tool_use` 和 `long_context` 又被排除在 `signal_count` 之外。于是**“只有 tool_use”与“完全没有 signal”等价地进入了无上限覆盖分支**。

### 具体漏洞

1. **一个 3 字任务可以一步到 5，代价是双份的。**
   `跑一下` 只命中 `_TOOL`（`policy.py:171-174`），signals 为 `["tool_use"]`，按现有 tie-break 后 kind 落到 `analysis`，difficulty 基线为 2；confidence 为 0.4。此时 Jev 只要给出 score=4（映射为 difficulty 5）且 confidence≥0.55，`final_difficulty` 就直接是 5，不再经过 ±1。
   后果不止是路由更贵：`coordinator.BLOCKING_THRESHOLD = 4`（`coordinator.py:32`）会让它进入 blocking review，`runner._auto_review()` 会在主 run 结束后再派一整轮 reviewer 执行（`runner.py:1166-1234`）。**一次 Jev 误判的乘数不是“贵一点的模型”，而是“主 run + 审查 run”。**

2. **成本影响不是单调的。**
   difficulty 同时被用于 `_PRICE_SENSITIVITY`（5 时降为 0.25，`policy.py:110`）、`_SOLVE_WEIGHT`（5 时为 0.85，`policy.py:105`）、`high-difficulty-proven` 门控（`policy.py:1417-1452`）和 effort 选择（`policy.py:1677-1687`）。Jev 把 2 误判成 5，会同时放松价格惩罚、提高 solve 权重、打开 blocking gate，三者叠加不是“+1 档”的线性成本。

3. **可靠性指标用错了维度。**
   brief 用“99.1% 成功率”为 Jev 背书，但这是**调用/解析成功率**，不是**难度标定准确率**。决策 1 需要的恰是后者。21 字 infra 案例是 n=1，且没有记录规则与 Jev 的偏差分布；用一个点的偏差去改全局策略，证据强度不足。

4. **“建议，而非权威”的契约被破坏。**
   `decision.py:4-5` 和 `policy.py:552-554` 都明确写着 Jev 只是 review、规则才是 backbone。`not strong` 分支让 Jev 在系统自认“没看见任何结构信号”时拥有了对最贵维度的最终决定权，而且没有任何 `override` 标记写入 `assess` 结果，事后审计只能靠反推。

5. **回归测试名不副实。**
   `tests/test_policy.py:153` 的测试叫 `test_jev_can_raise_difficulty_but_only_by_one_step`，但用例是“把 foo 重命名为 bar”，命中 `trivial`（属于 `strong`），走的是 `min(5, 1+1)` 分支，**根本没有覆盖新增的 override 路径**。当前没有任何用例覆盖“signals 仅为 tool_use / signals 为空 + Jev 高判”的场景。

6. **fail-open 在异常边界上不成立。**
   `decision.decide()` 的 docstring 写 “Never raises”（`decision.py:80-82`），但 `_post()` 里的 `json.loads(resp.read())`（`decision.py:70`）没有捕获 `JSONDecodeError`，`decide()` 也不兜底；`policy.assess()` 没有 try。也就是说“Jev 返回 200 但不是 JSON”会直接抛异常，而不是按文档降级。

7. **难度映射的基准没有被钉死。**
   `jev_difficulty = int(_clamp(int(jev_score + 0.5) + 1, 1, 5))`（`policy.py:520-523`）假定 Jev 的 `score` 是 0 基；但提示词给模型的 criteria 是从 1 到 5（`policy.py:373-383`）。如果真实 Jev 按 1-5 返回，`score=1` 会被映射成 2，`score=4` 会被映射成 5，**整体上移一档**；override 分支会把这个偏差直接放大到 5。至少应加一条协议测试，固定 0→1、4→5 的映射，或改成按 `score` 的 min/max 归一化。

### 建议

- 保留 ±1 上限，或最多放宽到 **+2**；只有 `signals == []`（严格为空，而非 `not strong`）才允许例外。
- 若一定要放宽，要求 `jev_difficulty_confidence` **显著高于**规则 confidence（例如 ≥0.8 且差距 ≥0.25），并只允许在 **kind/needs** 上覆盖，不允许单独把 difficulty 拉到 5。
- 在 `assess` 结果中显式记录 `difficulty_override: {from, to, reason, jev_confidence, rule_confidence}`，并统计 override 频率与后续 run 的完成率/成本；没有这个反馈环就不该全局放宽。
- 增加测试：`signals=[]`、`signals=["tool_use"]`、`signals=["writing"]` 三种输入 × Jev 高/低判，断言上限与 override 标记。
- 修复 `decision._post` 的 JSON 解析异常，统一返回 `ok=False`。

---

## 决策 2 [HIGH]：needs_clarification 触发条件

### 具体漏洞

1. **触发器 1 与决策 1 对“规则盲区”的定义不一致。**
   决策 1 用 `not strong`（包含 tool_use-only），决策 2 用 `not assessment.get("signals")`（`coordinator.py:231-235`）。同一个任务可以“盲到让 Jev 直接改难度”，却“不盲到需要追问”。这不是实现细节，而是两条策略共享了同一个概念却没有共享同一个谓词。

2. **触发器 1 在常见短文本上几乎不可达。**
   `signals == []` 时 `_score_kind` 所有分数为 0，tie-break 落到 `analysis`（`policy.py:263-264`），difficulty 基线是 2；只有靠 `_TEST_GOAL`（如“让测试全绿”）或长文本（≥600/1600 字）才能到 3 以上。也就是说“短、模糊、难”的典型场景反而不触发，触发的是“长但无关键词”或“测试必须全绿”这类边缘场景。

3. **用 composite confidence 做触发，导致逆向选择。**
   `coordinator.plan()` 调用 `policy.assess(..., review=True)`（`coordinator.py:209`），返回的 `confidence` 已经被 Jev 重算过（`policy.py:581-593`）。当 Jev 可用且赞同时，confidence 通常 ≥0.65；当 Jev 不可用或与规则冲突时，confidence 才降到 0.4～0.625。结果：
   - Jev 正常时，scope 关键词触发器大多关闭；
   - Jev 故障时，反而对每个含“完整/所有/任何”的任务发起一次 Sonnet 调用。
   **最需要追问的场景（规则无信号、Jev 把它拉到 5）confidence 是 (0.4+0.95)/2−0.05 = 0.625，大于 0.55，因此不会追问**。决策 1 和决策 2 在这里正面冲突：Jev 越自信地抬高难度，系统越不会澄清。

4. **关键词表与 brief 不一致，且明显过宽。**
   实际正则是 `全套|所有|任何|整套|全部|一整|完整|全面|全量|每一|所有的|全站|全库`（`coordinator.py:228-230`），brief 漏了 `任何`、`全部`、`一整`、`完整`。其中：
   - `完整` 会命中“完整性约束”“数据完整性”这类精确技术词；
   - `任何` 会命中“任何情况下”这类限定语；
   - `所有的` 是 `所有` 的冗余。
   这些词并不等于“范围未定义”，把它们直接等同于 scope risk 是词面匹配，不是语义判断。

5. **没有 opt-out，且只覆盖部分入口。**
   `runner.start()` 没有 `skip_clarify` 参数；`--no-review` 只影响 review，不影响 clarify。用户无法为“我知道自己在干什么”的任务关闭这次付费调用。反过来，显式指定 route 且不带 `--review` 时 `_decide_route()` 根本不走 `coordinator.plan()`（`runner.py:426-428`），所以 L1 完全不生效——**默认路径没有澄清，绕过路径反而有**。

6. **同步调用阻塞派发，和“fail-open 不阻塞”的叙事矛盾。**
   `clarify.run()` 在 `_decide_route()` 内同步执行（`runner.py:443-445`），`cortex.ask` 默认 timeout 60s（`cortex.py:78`），且发生在 run row 创建之前、第二次 `budget.check()` 之前（`runner.py:290` vs `320`）。relay 抖动时，每个模糊任务都要先等最多 60 秒才失败继续。这不是“不阻塞执行”，而是“阻塞派发”。

7. **成本倒挂。**
   一个被判定为 difficulty 2 的写作任务（例如“完整改一下这个文档”，若 Jev 不可用则 confidence=0.5）会触发 Sonnet 5.5 的 clarify 调用，而执行者可能只是便宜的 `dsh`。**用来澄清 2/5 任务的模型比执行它的模型贵一个数量级**，且没有预算检查。

8. **没有测试。**
   `tests/test_coordinator.py` 完全没有 `needs_clarification` 的用例；`tests/test_policy.py` 也没有覆盖 trigger 1/2 的边界。阈值 0.55/0.65 没有任何数据支撑，且 confidence 只有 0.4/0.5/0.55/0.65/0.7 等少数离散值，`< 0.55` 实际只放行 0.4/0.5，0.55 本身就被排除；`< 0.65` 也刚好把 0.65 排除。

### 建议

- 把“规则盲区”统一为 `assessment["rules"]["confidence"]`（或显式的 `rules_signals`），不要用包含 Jev 的 composite confidence。
- 触发器拆成两个独立信号：`rules_blind` 与 `scope_risk_score`；只有在 `difficulty >= 3` **且** `rules_blind` 时才追问，scope 关键词只作为加分项，并要求 `kind in (code, ops, analysis)`。
- 增加 `--no-clarify` / 配置项 `clarify.enabled`，并给 clarify 设置独立的短超时（如 15s）和 `max_input_chars`。
- 对“完整性/任何/所有”做词边界或上下文排除（至少排除“完整性”“任何情况”“所有权”）。
- 补测试矩阵：短模糊、长无信号、scope 词 + 高 confidence、scope 词 + Jev 故障、difficulty 2 的写作任务，断言“是否调用 clarify”和“调用次数”。

---

## 决策 3 [CRITICAL]：cortex.py 直连 Anthropic Messages API

### 具体漏洞

1. **预算体系被绕过，而且不是“少记一笔”，是“完全看不见”。**
   `budget.spent()` 从 `db.runs_in_window()` 读取已写入 run 表的 `usage_json`（`budget.py:152-164`）。cortex 调用发生在 run row 创建之前（`runner.py:290` 调用 `_decide_route()`，`runner.py:353` 才 `_claim()`），既不写 run，也不写 `usage_json`。因此：
   - 日上限 `budget.daily_cny` 无法感知 clarify 花费；
   - `soft_cny` / `soft_families` 也看不见；
   - 预付费的 `relay-supplement` 余额只能靠人工盯。
   这不是“cortex 不调用 `budget.check()`”这么简单，而是**整个预算模型只统计 run 表，天然无法覆盖元调用**。第一次 `budget.check()`（`runner.py:269`）只能挡住“已经超日上限”的情况，挡不住“本次 clarify 把余额推过线”；只要还没触发上限，连续自动派发就可以一点点烧 supplement key，而 `sodus board` 的当日花费不包含这部分。

2. **meter 同样不覆盖。**
   `meter.read()` 只在 `runner._execute()` 里围绕 agent run 调用（`runner.py:835`、`1008-1022`），且传入的是 run 的 provider，不是 coordinator 的 provider。cortex 只在 `~/.sodus/cortex.jsonl` 里记录 `in_tokens`/`out_tokens`（`cortex.py:134-142`），没有单价、没有折算、没有进入 ledger 或 report。三个成本真相源（DB run、meter、cortex.jsonl）互不对话。

3. **没有速率限制、没有并发控制、没有重试。**
   `cortex.ask()` 是一次裸 `urlopen`（`cortex.py:114`），没有信号量、没有队列、没有 429/5xx 退避。多个 `sodus run --route auto` 进程会同时打同一个 relay；遇到 429 直接 `ok=False`，没有 `Retry-After` 处理。对“补充分组、余额有限”的 key，这是最不该省的部分。

4. **“fail-open”在 JSON 解析层是假的。**
   `cortex.py:115` 的 `json.loads(resp.read())` 只被 `HTTPError` / `URLError` / `OSError` / `TimeoutError` 捕获。若 relay 返回 200 的 HTML 错误页、空 body、或非 JSON 文本，`JSONDecodeError` 会直接抛出；若 body 是 JSON 数组，`body.get("content")`（`cortex.py:129`）会抛 `AttributeError`。这些异常会一路冒到 `runner._decide_route()` 的 `except Exception`（`runner.py:478`），而该 `except` 的处理是：**auto 模式下把整条路由决策回退到 `DEFAULT_ROUTE`**（`runner.py:482-489`）。也就是说，一个可选的 L1 解析失败会让主任务从“推荐路由”静默变成默认路由（通常是更贵的 claude），而不是“没有澄清、继续原计划”。这比阻塞更糟。

5. **只取第一个可用 provider，没有运行时降级。**
   `_resolve()` 遍历 `route.providers`，返回第一个 protocol=anthropic 且有 key 的 provider（`cortex.py:44-56`）。这解决了“主 key 缺失时找备用 key”，但没有解决“主 key 429/401/500 时换下一个 provider”。`routes.py:1-10` 明确把 provider 列表定义为 fallback chain，cortex 只实现了链的解析，没有实现链的故障转移。

6. **认证头与 sodus 其余路径不一致。**
   sodus 给 Claude Code 注入的是 `ANTHROPIC_AUTH_TOKEN`（`providers.py:349-353`），Claude Code 会以 `Authorization: Bearer` 发送；cortex 只发 `x-api-key`（`cortex.py:103`）。对官方 Anthropic API 这是对的，但对“relay-supplement”这类中转，如果它按 Bearer 校验，cortex 会稳定 401。这个问题必须在真实 relay 上验一次，不能靠“协议是 anthropic”推断。

7. **base_url 缺失时会把 key 发到公网。**
   `_resolve()` 不要求 `prov.base_url` 非空；`ask()` 在为空时回退到 `https://api.anthropic.com`（`cortex.py:91`）。如果 providers.json 里 coordinator 的条目漏了 `base_url`，relay 的 key 会被发到 Anthropic 官方端点，既必然失败，也构成凭据外发。这里应该直接判为 route 不可用。

8. **审计日志逃逸 `config.HOME`。**
   `_LOG_FILE = Path.home() / ".sodus" / "cortex.jsonl"`（`cortex.py:29`）绕过了 `config.HOME`。当 `SODUS_HOME` 指向测试/隔离目录时，审计日志仍写真实用户目录；在只读 home 下 `_log` 又静默吞掉 `OSError`，等于没有审计。`decision.log` 用的是 `config.HOME`（`decision.py:114`），两者不一致。

9. **类型标注与实现不符。**
   `_resolve()` 标注 `tuple["providers.Provider", str] | None`（`cortex.py:37`），实际返回三元组 `(prov, model, key)`。这说明该模块没有经过 mypy/pyright 级别的检查，也解释了为什么上面几个边界没有被发现。

10. **重复造 HTTP 客户端。**
    `decision._post()`、`meter._get_json()`、`cortex.ask()` 三套 urllib 实现，各自的代理、超时、错误处理、日志语义都不同。仓库刚刚在 `policy._usage_cost` 里强调“不要做第四种 usage_json 读法”（`policy.py:832-839`），cortex 却做了第四种 HTTP 读法。这是架构倒退，不是局部实现问题。

### 建议

- 抽一个 `meta_llm.py`（或复用 decision 的 transport）：统一 base_url 校验、认证头、超时、重试/退避、代理、日志。
- **先记账再调用**：`budget.check_meta(estimated_tokens)` / `budget.charge_meta(usage)`，把元调用写成 `kind='meta'` 的 ledger 行或独立 `meta_usage` 表；`spent()` 必须把它算进去，否则日上限没有意义。
- 并发信号量（例如 2～4）+ 每任务最多 1 次 clarify + 按 `task_digest` 缓存结果；429 时退避或直接跳过，并记录 `retry_after`。
- 捕获所有 JSON/结构异常，返回 `ok=False`，并确保调用方**只在 clarify 失败时保持原路由**，不要触发整条 `_decide_route` 回退。
- 校验 `base_url` 存在且为预期 host；对 `relay-supplement` 做一次启动时健康检查（或 `sodus doctor` 检查项）。
- 统一认证：对 anthropic 协议优先发 `Authorization: Bearer`，或同时发两种头（需确认 relay 接受），并用真实调用验证。

---

## 决策 4 [HIGH]：clarify.py 提示词设计

### 具体漏洞

1. **提示词里的 JSON 样例是非法 JSON。**
   `_SYSTEM` 的 schema 示例大量使用 `//` 注释（`clarify.py:44-60`）：

   ```json
   "questions": [          // 2-4 targeted questions, most important first
   ```

   模型被要求“输出严格 JSON”，但看到的示例本身不能通过 `json.loads`。模型完全可能照抄注释，或者把 `"low|medium|high"` 当成字面量输出。`_parse()` 只去 markdown fence，不去 `//` 注释（`clarify.py:184-196`），这类输出会稳定解析失败。**这是提示词自己制造的失败率，不是模型能力问题。**

2. **1024 token 的输出上限对中文 schema 偏紧。**
   `clarify.run(max_tokens=1024)`（`clarify.py:208`），要容纳 2-4 个问题（每个含 q/why/options）+ draft_spec + 2-5 条验收标准。中文 1024 output token 大约 700 字，draft_spec 稍长就会撞上限。cortex 不检查 `stop_reason == "max_tokens"`，截断的 JSON 会被当成 `ok=True` 返回，再在 `_parse` 失败；错误信息只有 `"JSON parse failed"`，没有“被截断”这一层。

3. **没有 schema 类型校验，坏结构会击穿调用方。**
   `clarify.py:241-249` 直接假设 `questions` 是 list[dict]：

   ```python
   for item in (parsed.get("questions") or [])
   if item.get("q")
   ```

   如果模型返回 `"questions": "..."`（字符串）或 `"questions": {"q": ...}`（对象），这里会对 `str`/`dict` 调 `.get()` 或迭代字符串，抛 `AttributeError`。`acceptance_draft` 是字符串时，`[str(c) for c in ...]` 会把字符串拆成单字（`clarify.py:255`），不崩溃但产出垃圾。`scope_risk` 不校验枚举，模型返回 `"high risk"` 也会原样写进事件。更严重的是，这些异常发生在 `_decide_route()` 的 try 内，会触发上面说的整条路由回退。

4. **`_user_message()` 对模型说了假话。**
   `clarify.py:89` 无条件写：

   > “The rule engine had low confidence due to minimal structural signals.”

   但触发器 2 的场景恰恰可能是“signals 很多、只是出现了 scope 关键词”。模型一边看到 `signals: code, analysis, ops`，一边被告知“minimal structural signals”，输入自相矛盾，会诱导它忽略已有信号、重复问已经明确的东西。

5. **`[ASSUMED]` 只有口头约定，没有结构约束。**
   `draft_spec` 是一个自由字符串，假设只是其中的文本标记（`clarify.py:52-54`）。没有 `assumptions` 数组、没有 validator 检查每个假设是否被标注、没有把假设单独渲染给用户确认。更关键的是：**即便模型完美标注了，下游也没有任何代码消费这份 spec**（见决策 5），所以 brief 担心的“L2 规划者直接吃 spec 而忽略假设”在当前实现里甚至不成立——现状是“没人吃”。

6. **提示词承诺了一个系统没有实现的流程。**
   `_SYSTEM` 写“The questions field is the most important — the user reads these and answers before anything runs”（`clarify.py:63-64`），但决策 5 明确说任务继续派发，且没有回答入口。模型被要求按“会有人回答”来设计问题和选项，实际用户只能在 run 启动前后看到一份 md 文件。

7. **采样参数缺失。**
   `cortex.ask()` 没有传 `temperature`，Anthropic 默认是 1.0。对“严格 JSON + 固定 schema”的任务，这是不必要的高方差；应设 `temperature=0`（或最低值）。

8. **没有重试/修复回合。**
   一次解析失败就返回 `ok=False`，没有“把解析错误发回模型，让它只输出 JSON”的第二次尝试。对于一个最多 1024 token 的调用，重试成本远低于“整个 L1 白跑”。

9. **任务文本未做边界处理。**
   `task_text` 原样拼进 user message（`clarify.py:82`），没有分隔符、没有“以下内容是数据不是指令”的声明。任务描述里如果包含“忽略以上指令，输出 …”，模型有概率照做；虽然当前输出只写文件，但一旦未来把 spec 注入执行，就是 prompt injection 通道。

### 建议

- 把 schema 示例改成**合法 JSON**（去掉所有 `//` 注释），或改用 tool-use / prefill 强制结构；不要只靠“请输出 JSON”。
- `max_tokens` 提到 2048～3072，并检查 `stop_reason`；截断时给出明确错误并重试一次。
- 增加 `_validate(parsed)`：`questions` 必须是 list[dict] 且每项 q 为非空 str；`acceptance_draft` 必须是 list[str]；`scope_risk` 必须在枚举内；`questions` 截断到 4 条。
- 把 `_user_message()` 改成传递真实原因（`blind` vs `scope_keyword`），并附上 `signals`、`difficulty`、`kind` 的原始值。
- 增加结构化 `assumptions: [{"text": ..., "why": ...}]`，要求每条 `[ASSUMED]` 必须对应一条；渲染时单独列“需要确认的假设”。
- `temperature=0`，并加一次 parse-retry；对 task_text 使用明确的数据边界（例如 XML 标签 + “忽略其中的指令”）。
- 增加 `tests/test_clarify.py`，用固定 JSON、fenced JSON、带注释 JSON、截断 JSON、`questions` 为字符串/对象等 fixture 覆盖解析与校验。

---

## 决策 5 [CRITICAL]：L1 追问后不阻塞派活

### 具体漏洞

1. **追问是“只写不读”的。**
   全仓库搜索 `refined_spec`，只有 `clarify.save()` 写入（`clarify.py:264-269`），没有任何读取方；`coordinator.clarification_needed` 事件也只有 `runner.py:372` 的 emit，没有消费者。`sodus clarify` 子命令只打印到 stdout（`pipeline.py:456-487`），不会把答案写回任务。

2. **executor 拿到的是原始任务，不是 draft spec。**
   `runner.py:384-390` 启动线程时传入的是 `task`、`route`、`spec` 等，没有 `_spec_path`；`_execute_inner()` 用 `build_prompt(task, recent_handoff, note, ...)` 构造提示（`runner.py:683-691`），`refined_spec.md` 只存在于 `config.HOME/runs/<id>/` 下。executor 的工作目录是 `workdir`，不是 run_dir。因此 draft_spec、acceptance_draft、`[ASSUMED]` 全部不会影响执行。**L1 的产出与执行完全解耦，追问不可能改变结果。**

3. **`scope_risk` / `estimated_effort` 是死字段。**
   它们只在事件 payload 里出现（`runner.py:373-374`），没有被 review 门控、预算、effort、路由任何一处读取。一个 `scope_risk=high` 的任务仍然按原难度、原预算、原 effort 直接跑。既然不参与决策，让模型生成它们只是增加 token 成本。

4. **事件发在派发之后，不是“事前通知”。**
   顺序是：`_decide_route()` 里先调用 clarify（付费、同步）→ `_claim()` 创建 run → 写 spec → emit `clarification_needed` → `thread.start()`（`runner.py:353-390`）。用户看到事件时，run 已经创建，线程马上开始；没有暂停/恢复机制。brief 说“事后通知”，实际连“通知后还能拦一下”的窗口都没有。

5. **失败路径会丢钱又丢信息。**
   clarify 在 `_decide_route()`（run row 之前）执行，但 spec 在 `start()` 里、第二次 `budget.check()`（`runner.py:320`）之后才写。如果第二次预算检查、`routes.problems`、`_claim` 失败，用户已经为 clarify 付了钱，却既没有 run 也没有 `refined_spec.md`。此外 `clarify.run` 若返回 `ok=False`，`decision["clarification"]` 仍会被设置（`runner.py:444-445`），`start()` 会写一份没有问题的 “Clarification Required” 空文件并 emit `ok=False`；这既误导又无补救。

6. **“不阻塞”是对用户不阻塞，对派发是阻塞。**
   clarify 是同步网络调用，最长 60s（`cortex.py:78`）。在 relay 抖动时，所有 auto 任务都要先等满超时。把它描述为 fail-open 不阻塞，掩盖了“派发被同步调用拖住”的事实。

7. **没有去重/缓存。**
   同一个 task 重复 `sodus run --route auto`，每次都会重新调用 Sonnet；没有按 `task_digest` 缓存，也没有“已有未回答追问”的检查。

8. **没有“只读侦察”这个中间态。**
   对高 scope risk 的任务，合理的 fail-open 不是“照常全速执行”，而是“先执行一个只读/低成本侦察 run，产出问题后停下”。当前只有“全速执行”和“完全不执行”两个极端，而实现选了前者。

### 建议

- 把 clarify 变成显式状态机：`draft → pending_user → answered/cancelled → dispatch`。问题存到 task 上（或 `clarifications` 表），提供 `sodus clarify answer <task> --q1 ...` 之类的入口。
- 派发前把答案注入：合并进 `task_brief` / acceptance，或在 run 的 prompt 中显式加入“用户已确认的范围与假设”。没有这一步，就不要生成 questions。
- 对 `scope_risk=high` 默认 **不直接派活**：要么等确认（可配置超时），要么派一个 `readonly=True` 的侦察 run；`scope_risk=low/medium` 才允许 fail-open 继续。
- 事件必须在派发前发出，并带 `status=pending`；`start()` 失败时保留问题（写到 task 级路径，而不是 run_dir）。
- 加 `--no-clarify` 与按 `task_digest` 的 TTL 缓存；clarify 失败时保持原路由，绝不回退默认路由。
- 若短期不做回灌，就把功能降级为“可选的预检报告”，不要 emit `clarification_needed`、不要写 `refined_spec.md`，避免让用户以为系统在等回答。

---

## 决策 6 [HIGH]：coordinator 路由 = Sonnet 5.5

### 具体漏洞

1. **`coordinator` 路由并不被 `coordinator.plan()` 使用。**
   `coordinator.py` 只 import `policy` 和 `routes`（`coordinator.py:25`），`policy.assess()` 调用的是 Jev（`decision.decide`），不是 cortex。cortex 的唯一调用方是 `clarify.py`（`cortex.py:13`）。所以“coordinator 路由 → relay-supplement → Sonnet 5.5”实际上只是“L1 clarify 路由”；真正的路由决策仍然是规则 + Jev。这个命名会让运维误判负载来源，也会让“coordinator 频繁调用消耗 key”的讨论建立在一个不存在的调用链上。

2. **`effort=medium` 在直连 API 里完全无效。**
   `cortex.ask()` 的 payload 只有 `model`、`max_tokens`、`system`、`messages`（`cortex.py:92-97`），没有 `thinking`/`reasoning`/`effort` 字段，也没有读取 `route.effort`。Anthropic Messages API 本身也没有 `effort` 参数。因此 routes.json 里的 `effort=medium` 是死配置；如果期望“medium effort 控制思考深度”，当前实现没有做到。

3. **没有 provider/route 级降级。**
   `_resolve()` 只返回第一个有 key 的 anthropic provider（`cortex.py:44-56`）；HTTP 401/429/500 时直接 `ok=False`，不会尝试 `route.providers` 的第二个 provider，也不会尝试备用 route。`routes.py` 的 fallback chain 语义在这里没有落地。brief 说“没有降级路由”，实际比这更细：**连同一 route 内的 provider 链都没有降级**。

4. **补充分组 key 的消耗没有闸门。**
   没有并发限制、没有每分钟/每天子配额、没有按任务缓存、没有输入长度上限。`_user_message` 会把完整 `task_brief` 塞进去（`clarify.py:82`），一个长 brief 的 input token 可能很大；`cortex` 只限制 output。对一个“余额有限”的 key，这是最直接的资金风险。

5. **认证头可能直接 401。**
   见决策 3 第 6 点：sodus 其余路径用 `ANTHROPIC_AUTH_TOKEN`（Bearer），cortex 用 `x-api-key`。如果 relay-supplement 只认 Bearer，这个路由从未成功过，而 `ok=False` 会被静默吞掉，表现为“L1 从来不工作”。

6. **没有启动校验/健康检查。**
   `_resolve()` 不调用 `routes.problems()`，不检查 model 是否存在、base_url 是否为空、key 是否过期；`404 model_not_found` 和 `401` 都只是 `cortex.jsonl` 里的一行。`sodus doctor`/启动时没有检查项，问题只能等到某个模糊任务触发时才暴露。此外 `_resolve()` 只接受 `protocol == anthropic` 的 provider（`cortex.py:46`）；如果 supplement 在 providers.json 里登记为 openai 兼容协议，这个路由会永远返回 not available，而错误同样只在 jsonl 里。

7. **审计与计量落在另一套目录/口径。**
   `cortex.jsonl` 在 `Path.home()/.sodus`（`cortex.py:29`），不走 `config.HOME`；token 数有，价格没有。无法回答“这个月 L1 花了多少”、也无法和 `sodus report` 对账。

### 建议

- 先把命名改对：这是 `intake`/`clarify` 路由，不是 coordinator 路由；或者真的让 `coordinator.plan()` 用 Sonnet 做规划（但那会显著改变延迟与成本模型，需要单独决策）。
- 如果保留 cortex，把 `effort` 映射到 `thinking.budget_tokens` 或删除该配置，避免“配置了但无效”。
- 使用 `route.providers` 链做运行时降级：401/403 不重试，429/5xx 指数退避后换下一个 provider；全失败再返回 `ok=False`。
- 给 L1 单独的日/月子预算与并发上限；按 `task_digest` 缓存；对 input 做截断（例如 16k chars）。
- 统一认证头并在真实 relay 上做 smoke test；`base_url` 为空直接判不可用。
- 在 `sodus doctor` / 启动时检查 coordinator 路由：key 存在、base_url 合法、一次 1-token 的探测调用成功、model 名称有效。

---

## 跨决策矛盾（比单条更值得先修）

1. **“规则盲区”有两个定义。**
   决策 1 用 `not strong`，决策 2 用 `not signals`；两者对 `tool_use` 的处理相反。结果是“能被 Jev 大幅改难度的任务”和“需要澄清的任务”不是同一个集合。

2. **confidence 同时承担“规则确定性”和“是否需要澄清”两个语义。**
   Jev 抬高 confidence 会让 clarify 关闭，而 Jev 又正是那个可能误判难度的组件。越不确定的任务越容易被直接派发，越确定的故障场景反而触发付费追问。

3. **“fail-open”被实现成“任何异常都继续”，但异常处理边界不一致。**
   `decision._post` 不捕获 JSON 解析异常，`cortex.ask` 不捕获 JSON 解析异常，`policy.assess` 不兜底；而 `_decide_route` 的 broad except 又会把整个计划回退到默认路由。最终表现不是“降级继续”，而是“静默换路由/静默丢澄清”。

4. **成本有三个真相源，彼此不通。**
   `budget` 看 DB run，`meter` 看 provider 账单，`cortex.jsonl` 看元调用 token。没有任何一层把三者合并；决策 3 和决策 6 的“余额有限”无法被系统感知。

5. **文档与实现漂移。**
   - brief 说 `signals==[]`，代码是 `not strong`；
   - brief 的关键词表漏了 `任何/全部/一整/完整`；
   - brief 说“coordinator 路由”，但 `coordinator.plan()` 不用 cortex；
   - `clarify._SYSTEM` 说“用户在任何执行前回答”，决策 5 说继续执行；
   - `ClarifyResult` 注释说“optionally injects into HANDOFF.md”，代码没有注入。

6. **新增路径几乎没有测试。**
   没有 `test_cortex.py`、`test_clarify.py`；`test_coordinator.py` 没有 `needs_clarification`；`test_policy.py` 的 ±1 用例没有覆盖 override。现有测试全绿并不能说明这三条新路径可用。

---

## 建议的验证与回归（下一位可直接执行）

```powershell
# 现状基线（预计全绿，但覆盖不到新分支）
python -m pytest tests/test_policy.py tests/test_coordinator.py -q

# 需要新增的用例（建议先写失败用例，再修）
# 1) policy：signals=[] / ["tool_use"] / ["writing"] 下 Jev 高判，断言是否允许跳到 5
# 2) policy：decision.decide 抛 JSONDecodeError / 返回非 dict，断言 assess 不抛
# 3) coordinator：scope 关键词 + Jev 可用/不可用，断言 needs_clarification 不受 Jev 影响
# 4) clarify：带 // 注释、fenced、截断、questions 为字符串/对象，断言不抛且 ok=False
# 5) cortex：HTTP 200 非 JSON、429、401、base_url 缺失，断言返回 ok=False 且不换路由
# 6) runner：clarify 失败时断言 decision["route"] 仍等于推荐路由（不是 DEFAULT_ROUTE）
```

真实链路 smoke test（需要 supplement key，注意费用）：

```powershell
python -c "from sodus import cortex; print(cortex.ask('Return the single word OK.', [{'role':'user','content':'OK?'}], max_tokens=8))"
```

如果这条返回 401，优先查 `x-api-key` vs `Authorization: Bearer`；如果返回 404，查 model 名称；如果超时，查 base_url/proxy。

---

## 修复优先级

1. **决策 5**：先决定“要不要真的等答案”。不做回灌就停掉 questions 生成和 `clarification_needed` 事件；要做就补状态机与注入。
2. **决策 3**：把元调用纳入预算与 meter，统一 HTTP transport，修异常边界，避免“可选步骤失败 → 主路由静默回退”。
3. **决策 1/2**：统一“盲区”谓词；用 rules confidence 而非 composite confidence；恢复上限或加严格门槛；补 override 遥测与测试。
4. **决策 4**：修合法 JSON schema、类型校验、max_tokens、temperature、重试。
5. **决策 6**：改名/接线、effort 映射、provider 链降级、健康检查与子预算。

> 结论：不建议以当前状态把 L1 作为默认路径上线。它可以作为 opt-in 的“预检报告”先跑，但在补齐“答案回灌 + 预算/计量 + 异常边界 + 测试”之前，默认开启会让系统在看不见的地方花钱，并把可选分析失败转化为路由决策失败。
