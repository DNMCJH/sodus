# Sodus HANDOFF

## 机器段
```
daemon: {version: "0.10.0", pid: 35252, addr: "127.0.0.1:4199"}
head: main @ 7a92c41
```

## 当前状态

**L1 追问层修复完成** (2026-11-28)
- dsh 完整批判性复查：2 条 CRITICAL + 4 条 HIGH 问题全部修复
- 1152 tests passed
- commit 7a92c41: "fix: harden L1 intake per dsh review"

**核心改动**：
1. cortex.py：JSON 解析异常捕获、base_url 空值拒绝、日志路径统一
2. clarify.py：合法 JSON schema、类型校验、max_tokens 提升、触发原因修正
3. coordinator.py：needs_clarification 改用 rules_confidence（消除 Jev 逆向选择）
4. runner.py：L1 降级为 `--clarify` opt-in，默认路径零开销
5. 测试覆盖：test_cortex_clarify.py (44 assertions) + test_coordinator.py (4 cases)

## 未决决策

| 日期 | 编号 | 决定 | 状态 |
|------|------|------|------|
| 2026-11-28 | D-001 | L1 答案回灌状态机 vs 永久 opt-in 预检 | 搁置 |
| 2026-11-28 | D-002 | Agent Interface 收束（46 命令 → JSON-RPC） | 规划完成，待实施 |

**D-001**：clarify 答案注入（实现 clarifications 表 + answer 命令 + spec 注入），工作量 2-3 小时。

**D-002**：Agent Interface 收束路线图已写入 `docs/roadmap-agent-interface.md`，分三阶段：
- 阶段 1（3 个月）：JSON-RPC API (`/rpc` 端点)
- 阶段 2（6 个月）：CLI 收束到 6 个动词 (create/read/update/delete/action/query)
- 阶段 3（12 个月）：纯 JSON 入口 (`sodus <json>`)

## 搁置原因

用户主线收束到六级和谷歌面试，sodus 后续有时间再开发。

## 下次启动时

1. 读 `docs/roadmap-agent-interface.md`
2. 读 `scripts/_review_findings.md`（dsh 完整批判）
3. 确认优先级：D-001（答案回灌）还是 D-002（Agent Interface）
4. 如果做 D-002，先实现 10 个高频 JSON-RPC method

## 技术债

- [ ] SQLite 并发限制（10+ 并发未测试）
- [ ] Windows 子进程管道泄漏（2s 宽限兜底）
- [ ] cortex/clarify 的 token 未纳入 budget/meter（TODO 注释已添加）
- [ ] L1 答案回灌状态机（D-001）

