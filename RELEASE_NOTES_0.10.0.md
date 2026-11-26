# sodus v0.10.0 Release Notes

**发布日期：** 2026-11-25  
**代号：** Phase 2 - 智能协调器与分级审查  

---

## 🎯 核心亮点

### 1. **智能路由协调器（coordinator.py）**

全新的 `coordinator.plan()` 实现任务难度感知的智能路由规划：

- **自动分级审查门控**：
  - 难度 1-2（简单修改）→ 无审查
  - 难度 3（中等复杂度）→ advisory 建议性审查
  - 难度 4-5（架构级改动）→ blocking 强制审查
  
- **跨 family 审查者选择**：
  - 确保 reviewer 来自不同的 agent family（claude ≠ gpt ≠ other）
  - 防止同一 agent 审查自己的工作
  - 自动 fallback 时重新计算合适的 reviewer

- **basis 追踪**：区分 rules-only（纯规则评估）vs rules+jev（含 Jev 增强评估）

**示例：**
```python
from sodus import coordinator

plan = coordinator.plan("重构 auth 模块的跨模块状态管理")
# → CoordinatorPlan(
#     executor_route="claude",
#     reviewer_route="codex",    # 不同 family
#     review_kind="blocking",    # 难度 4-5
#     difficulty=4
#   )
```

### 2. **A-plane 临时上下文（aplane.py）**

executor 完成任务后，reviewer 获得完整的执行上下文：

- **关键信息注入**：
  - 验证状态（verify_state: ok/red/none）
  - Diff 统计（changed files, +/- lines）
  - 执行路由、审查类型、任务摘要
  
- **持久化与恢复**：
  - `aplane.save(ctx)` → `~/.sodus/aplane/run-{id}.json`
  - `aplane.load(run_id)` → 恢复完整上下文
  - 容错设计：损坏数据返回 None，不抛异常

**示例：**
```python
from sodus import aplane

ctx = aplane.build(
    run_id="run-abc123",
    task={"title": "fix auth bug"},
    plan=coord_plan,
    verify_state="ok",
    diff_stat="2 files changed, 45 insertions(+), 12 deletions(-)"
)
aplane.save(ctx)
# reviewer 在 prompt 中看到：
# 「executor claude 完成任务，verify: ok, 改动 2 文件...」
```

### 3. **runner.py 自动审查集成**

- **`--route auto` 智能化**：
  - 旧：直接调用 `policy.assess + recommend`
  - 新：通过 `coordinator.plan()` 获得完整规划（含审查策略）
  
- **`_finalize()` 自动触发审查**：
  - 任务执行完成后，自动检查 coordinator plan
  - review_kind ≠ none → 派发审查任务到指定 reviewer
  - A-plane 上下文自动注入 reviewer prompt

- **blocking review gating**：
  - verdict = revise/reject → emit `coordinator.review_blocking_failed` 事件
  - CI/merge gate 可监听此事件阻止合并

---

## 🏗️ 架构优化（Phase 0-1.5）

### 退役旧模块

移除以下不再使用的模块，简化代码库：
- `board.py` / `ui.py` / `pet.py` / `mood.py` / `statuslight.py` / `desk.py` / `llm.py`
- 对应测试文件同步移除
- **净减少代码**：-3765 行

### 项目级决策收件箱

新增 `sodus decision` 子命令族，三端统一操作：

```bash
# CLI
sodus decision inbox                    # 列出待决策
sodus decision open --title "架构改动" --body "..." --tags phase2
sodus decision defer D-001 "等待 PM 确认"
sodus decision close D-001 "accept"

# HTTP API
GET  /decisions
POST /decisions
PUT  /decisions/{id}/defer
PUT  /decisions/{id}/close

# MCP (Claude Desktop / API)
sodus_decision_inbox()
sodus_decision_open(...)
```

**零 git 依赖**：决策数据存储在 `~/.sodus/decisions/`，不污染项目 git history。

### 项目注册表

- `~/.sodus/projects.json`：记录所有已知项目及其 git root
- `sodus handoff init` 自动登记当前项目
- 为未来的跨项目协作打基础

---

## 🐛 修复

1. **relative cwd leak**：修复 worker 子进程泄露 daemon cwd 的问题
2. **aplane.load() 崩溃**：非 dict JSON（如 `[]`）现返回 None 而非 AttributeError
3. **reviewer fallback contamination**：executor 被 fallback 时重新选 reviewer，防止 executor == reviewer
4. **blocking review 无 gating**：blocking review 失败时 emit 专用事件，供 CI 消费

---

## 📊 测试覆盖

- **新增测试**：33 个（test_coordinator.py: 20 + test_aplane.py: 13）
- **总计**：1128 tests
- **通过率**：100%（1 个预存在的 Windows file-lock flake，与本次改动无关）

---

## 🚀 升级指南

### 从 v0.9.0 升级

1. **安装新版本**：
   ```bash
   pip install --upgrade sodus
   ```

2. **验证版本**：
   ```bash
   sodus --version  # 应显示 0.10.0
   ```

3. **无需数据迁移**：本次升级无 schema 变更，现有数据库自动兼容

4. **新功能自动生效**：
   - `sodus run --route auto` 自动使用 coordinator
   - 审查门控按难度自动分级
   - 无需修改现有脚本或配置

### 验证新功能

```bash
# 1. 测试智能路由 + 审查
sodus run --goal "重构 runner.py 的错误处理" --route auto

# 2. 查看 coordinator 决策
sodus show run-{id} | jq .coordinator_plan

# 3. 检查审查结果（blocking review 时）
sodus events run-{id} | grep coordinator.review

# 4. 使用决策收件箱
sodus decision inbox
```

---

## 🔮 下一步（Phase 3+）

- **B-plane 多阶段流水线**：plan → execute → verify → review 完整链路
- **多轮迭代**：review 反馈 → 自动修复 → 重新验证
- **跨项目协作**：基于 projects.json 的多仓库任务编排
- **预算与配额**：family-level 软限制，防止单一 agent 过度使用

---

## 📝 相关链接

- **CHANGELOG**：[CHANGELOG.md](CHANGELOG.md)
- **架构文档**：[docs/architecture.md](docs/architecture.md)
- **项目交接文档**：[HANDOFF.md](HANDOFF.md)
- **GitHub Release**：https://github.com/DNMCJH/sodus/releases/tag/v0.10.0

---

**感谢所有贡献者！** 🎉

特别鸣谢：
- Claude Opus 5.5：Phase 2 核心实现与 sol review
- 社区反馈：帮助我们发现并修复 3 个关键问题
