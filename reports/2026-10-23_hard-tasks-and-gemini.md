# 难度 4-5 评测题与 gemini solve 数据

2026-10-23 · 范围：`eval/tasks/` 三道新题、两次 bench、一次诊断跑、policy 复核

## 三道新题

| id | 难度 | 结构 | 隐藏判定 |
|---|---|---|---|
| debug-day-boundary | 4 | cli → report → ledger → parsing，4 个模块 410 行 + 160 行测试 | 8 条：3/1 与 3/2 日结、3 月月结、本地 00:00 与 23:59:59.999999 边界、`in_range`/`end_of_day`/`sum_between` 契约、`pytest tests/` 全绿 |
| interface-first-rules | 5 | 空目录起手，base/engine/rules 三个文件；题面要求先接口后行为 | 19 条：ABC/签名/frozen dataclass/异常属性 + 优先级/平手/trace/只读 ctx/无匹配/异常包装/两条内置规则 |
| budget-currency-admit | 4 | 真实 `nodus/budget.py` 摘录 + 本地 config/db 接线 | 10 条：`spent_breakdown`/`admit` 验收 + `check`/`refuse_message`/`spent` 回归 + 既有测试 |

第一题的陷阱：`report` 把 `end_of_day()` 的独占上界原样传给 `sum_between()` 的闭区间，日结多算次日 00:00 一笔。正确修法在 `ledger.daily_totals/monthly_totals`；改 `sum_between`/`in_range`/`end_of_day` 任一公共函数都会弄红既有契约测试。

本地验证：三题 `start/` 的既有测试全绿；未修改时 hidden 分别失败（task 1 4/8、task 2 包不存在、task 3 7/10）；按参考实现修复后 hidden 全绿（8/19/10）。

## 跑分（每路由 3 次，成本 CNY）

**opencode-glm · 3 道新题**（`reports/2026-10-23_bench_opencode-glm-hard2.json`）

| task | verdict | time | cost |
|---|---|---|---|
| budget-currency-admit | solve | 38.5s | ¥0.0093 |
| debug-day-boundary | solve | 101.1s | ¥0.0206 |
| interface-first-rules | tests_red | 42.7s | ¥0.0086 |

solve 2/3 · 平均 60.8s · ¥0.0386。唯一失败是 interface-first-rules：19 条里过 18，包装非 `RuleError` 异常时忘了 `rule=code`，`RuleError.rule` 是 `None`。GLM 在这三题上第一次不是满分（此前 9 道题 9/9）。

**opencode-gemini · 旧 3 题**（`reports/2026-10-23_bench_opencode-gemini-old3.json`）

| task | verdict | time | cost |
|---|---|---|---|
| fix-range-parser | no_change | 19.8s | ¥0.0153 |
| unify-slugs | solve | 38.5s | ¥0.0197 |
| unit-cli | solve | 47.9s | ¥0.0641 |

solve 2/3 · 平均 35.4s · ¥0.0990（¥0.033/run，中位 38.5s）。

fix-range-parser 的 `no_change` 是抖动：同题单跑一次 solve（79.2s，¥0.0282）；该跑只用于诊断，没有写进 `reports/`，避免 1 次样本混进 policy 的聚合。

**诊断：gemini 跑 interface-first-rules**（未入 policy）：solve，238.7s，¥0.0883。同一道 GLM 掉分的题 gemini 19/19 过了，但慢 5.6 倍、贵 10 倍（GLM 42.7s / ¥0.0086）。

## policy 是否读到

`python -m nodus route-for "把 nodus/db.py 连接管理重构成上下文管理器" --json`（快照：`reports/2026-10-23_route-for-db-refactor.json`）：

- winner 变成 `opencode-glm` 1.31（次选 codex 1.04，gemini 0.87）
- GLM evidence：6 attempts / 5 solved / **tested_difficulty 5** / 两份 bench JSON / ¥0.0173 per run / median 48.7s
- Gemini evidence：3 attempts / 2 solved / tested_difficulty 2 / ¥0.033 per run / median 38.5s

新数据确实被读到了，但 `tested_difficulty` **不是**从 `task.json.difficulty` 来的。`bench.execute` 产出的 attempt row 里没有 difficulty 字段；`policy._bench_evidence` 用 `item["title"]` 再过 `_rules(title, "")` 打分。把标题写成"三层调用里的日结边界错误"这种朴素形式，同一道题只有 2 分。现在能到 5，是因为标题里恰好命中"根因/多个文件/设计/测试全绿"。

要真正修（本次未动）：

1. `nodus/bench.py`：`Task`（L58-65）加 `difficulty`，`load_task` 读 `task.json`，`execute` 的 row（L285-300）带上它；
2. `nodus/policy.py` L739-741：优先用 `item["difficulty"]`，缺失才回退 `_rules(title)`；
3. 附带 bug：`policy._compare` L1446-1451 在 winner 覆盖、rival 不覆盖时输出"bench 只测到难度 5，solve 已按 0.35 折减"，实际只折减 rival，措辞误导。

## 花费与结论

总花费 **¥0.254**（GLM 0.0386 + gemini 旧 3 0.0990 + 重跑 0.0282 + gemini 硬题诊断 0.0883），预算 ¥3 未触线。

能区分一点了：GLM 在接口契约题上掉分，gemini 在同一题上通过，说明题面本身可学、不是含糊；gemini 也有了 measured 行。但难度 4-5 目前只有 GLM 的正式记录，codex/minimax 仍停在 difficulty 2，硬题横评还不成立；policy 把难度 3 的重构翻给 GLM，依据是 6 次样本 + 标题推出来的难度。建议先修 difficulty 数据流，再用同一批新题把 codex/minimax 各跑一遍。
