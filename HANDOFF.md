# Sodus HANDOFF

## 机器段
```
daemon: {version: "0.10.0", pid: 20060, addr: "127.0.0.1:4199"}
head: main @ 0912e05
```

## 当前活跃任务

### L1 追问层修复（进行中）

**背景：**
dsh 对 L1 实现做了完整批判性复查（见 `scripts/_review_findings.md`），发现 2 条 CRITICAL 和 4 条 HIGH 问题。

**执行计划（软工原则：先稳定边界，再扩功能，先 opt-in 再默认开）：**

#### P0：修异常边界（≤30min）
- `cortex.py`：捕获 `json.loads` 的 `JSONDecodeError` / `AttributeError`，统一返回 `{"ok": False, "unavailable": "..."}` 而不是抛出；确保 `ask()` 在任何情况下不会让 `_decide_route` 进入 broad except 路径
- `cortex.py`：`base_url` 为空时直接返回 `ok=False`（不把 relay key 发到 api.anthropic.com）
- `cortex.py`：日志路径改用 `config.HOME / "cortex.jsonl"` 与 decision.jsonl 保持一致
- `clarify.py`：`_validate(parsed)` 函数，校验 questions/acceptance_draft 类型，坏结构返回 `ok=False` 而不抛异常
- `clarify.py`：schema 示例改成合法 JSON（去 `//` 注释）
- `clarify.py`：`max_tokens` 从 1024 → 2048，检查 `stop_reason == "max_tokens"` 时标记截断

#### P1：L1 降级为显式 opt-in（≤30min）
- `runner.py`：`start()` 新增 `clarify: bool = False` 参数
- `runner.py`：只有 `clarify=True` 时才调用 `clarify.run()`，默认关闭
- `server.py`：POST /runs 传递 `clarify` 字段
- `cli/commands/tasks.py`：`run()` 加 `--clarify` flag
- 保留 `needs_clarification` 字段（用于 `sodus clarify` 显式命令），但不再自动触发

#### P2：统一盲区谓词（≤20min）
- `coordinator.py`：`needs_clarification` 的触发器 1 改用 `assessment["rules"]["confidence"]`（不用 composite）
- 两条触发器共享同一个 `rules_blind` 谓词：`not assessment.get("signals") and rules_conf < 0.5`

#### P3：补测试
- `tests/test_cortex.py`：HTTP 200 非 JSON、429、401、base_url 缺失 → 断言 `ok=False` 且不抛
- `tests/test_clarify.py`：带注释 JSON、截断 JSON、questions 为字符串 → 断言 `ok=False` 且不抛
- `tests/test_coordinator.py`：`needs_clarification` trigger 1/2 边界

**设计原则约束：**
- fail-open 要真正 fail-open：任何可选步骤失败必须保持原路由，不得触发 `_decide_route` broad except
- opt-in 优先：L1 默认关闭，显式 `--clarify` 开启；不影响非 clarify 路径的任何测试
- 记账先行：cortex 调用的 token 目前进 `~/.sodus/cortex.jsonl`，budget/meter 集成留 TODO 注释
- 不引入新依赖，不改 runner.py 的核心派发逻辑

## 未决决策

| 日期 | 编号 | 决定 | 状态 |
|------|------|------|------|
| 2026-11-28 | D-001 | L1 答案回灌状态机 vs 永久 opt-in 预检 | 待拍板 |

**D-001 背景：** dsh 指出 `refined_spec.md` 没有任何读取方，executor 执行时拿到的还是原始任务文本。两个选项：
- A）实现完整状态机（task 增加 `clarifications` 表，`sodus clarify answer`，派发前注入 spec）——有用但工作量大
- B）永久定位为"opt-in 预检报告"，不做回灌，`sodus clarify <text>` 只供人看再手动修 task 文本

推荐 B，待用户拍板。
