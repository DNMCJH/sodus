# Sodus Agent Interface Roadmap

## 背景

当前 sodus 有 46 个 CLI 命令，主要给 Agent 调用（非人类直接使用）。问题：
1. Agent system prompt token 开销大（要列全 46 个命令）
2. 语义冗余（task create / task new / add task）
3. 推理负担高（Agent 需要在 46 个命令里选择）

目标：收束到对 Agent 友好的接口，降低 token 开销和推理负担。

## 设计原则（Agent-first）

- **语义明确**：`create task` 优于 `add`（不依赖人类直觉）
- **结构化输出**：所有命令 `--out json` 返回标准格式
- **幂等性**：重复调用结果可预测
- **错误明确**：exit code + JSON 错误信息
- **路径即文档**：`read task/abc` 比 `show task abc` 更自解释

## 阶段 1：JSON-RPC API（3 个月）

### 实现

在现有 HTTP server 上增加 `/rpc` 端点：

```python
# POST /rpc
{
  "method": "task.create",
  "params": {"text": "...", "accept": "..."},
  "id": "req-123"
}

# Response
{
  "result": {"task_id": "task-abc", "status": "created"},
  "error": null,
  "id": "req-123"
}
```

### Method 列表（从 46 个命令映射）

**Task**
- `task.create` ← task create
- `task.list` ← task list
- `task.show` ← task show
- `task.update` ← task edit
- `task.delete` ← task rm

**Run**
- `run.start` ← run <task>
- `run.list` ← 新增（目前没有独立命令）
- `run.show` ← 通过 watch 实现
- `run.stop` ← stop
- `run.resume` ← resume（对 run）
- `run.watch` ← watch

**Session**
- `session.list` ← session list
- `session.show` ← session show
- `session.resume` ← resume（对 session）
- `session.branch` ← session branch
- `session.delete` ← session rm

**Review/Verify/Fix**
- `review.run` ← review run
- `verify.run` ← verify run
- `fix.run` ← fix run

**Propose**
- `propose.create` ← propose new
- `propose.list` ← propose list
- `propose.show` ← propose show
- `propose.merge` ← propose merge

**Budget**
- `budget.show` ← budget show
- `budget.set` ← budget set
- `budget.meter` ← budget meter

**System**
- `system.topology` ← topology
- `system.status` ← daemon status
- `system.providers` ← providers
- `system.routes` ← routes

**Handoff**
- `handoff.show` ← handoff show
- `handoff.append` ← handoff append
- `handoff.sync` ← handoff sync

**Clarify**
- `clarify.run` ← clarify <text>
- `clarify.answer` ← clarify answer（待实现）

### 迁移策略

1. **新 Agent 优先用 JSON-RPC**（Claude Code MCP 工具）
2. **旧 CLI 继续工作**（不破坏现有 Agent）
3. **CLI 内部改为调 HTTP**（逐步统一）

### 验收标准

- [ ] `/rpc` 端点实现并通过测试
- [ ] 覆盖现有 90% 命令功能
- [ ] MCP 工具改为调 JSON-RPC
- [ ] 文档：method 列表 + 参数 schema

## 阶段 2：CLI 收束到 6 个动词（6 个月）

### 新 CLI 设计

```bash
# CRUD 操作
sodus create <type> [--flags]      # task/run/session/proposal
sodus read <path> [--format json]  # 任何资源或列表
sodus update <path> [--flags]      # 修改资源
sodus delete <path>                # 删除资源

# 特殊动作
sodus action <path>/<verb>         # stop/resume/branch/merge

# 查询（可选）
sodus query <expr>                 # 结构化查询
```

### 映射表（46 → 6）

| 旧命令 | 新命令 |
|--------|--------|
| `task create` | `sodus create task` |
| `task list` | `sodus read tasks` |
| `task show abc` | `sodus read task/abc` |
| `task edit abc` | `sodus update task/abc` |
| `task rm abc` | `sodus delete task/abc` |
| `run task-x` | `sodus create run --task task-x` |
| `watch run-y` | `sodus read run/y --follow` |
| `stop run-y` | `sodus action run/y/stop` |
| `resume session-z` | `sodus action session/z/resume` |
| `review run run-y` | `sodus action run/y/review` |
| `verify run run-y` | `sodus action run/y/verify` |
| `topology` | `sodus read topology` |
| `budget show` | `sodus read budget` |
| `budget set` | `sodus update budget` |

### 兼容性

- 旧命令作为 alias 保留 6 个月
- 警告：`Warning: 'sodus task create' is deprecated, use 'sodus create task'`
- 最终移除旧命令

### 验收标准

- [ ] 新 CLI 实现并通过测试
- [ ] 所有旧命令有对应的新命令
- [ ] 文档更新
- [ ] 迁移指南

## 阶段 3：纯 JSON 入口（12 个月，可选）

### 终极简化

```bash
# 只有一个命令
sodus '{"method": "task.create", "params": {...}}'

# 或者从 stdin 读
echo '{"method": "task.create", "params": {...}}' | sodus
```

### 对 Agent 的价值

- System prompt 只需要列 method 名字表（不需要解释命令行语法）
- Agent 只需要构造 JSON（不需要理解 shell quoting / flag parsing）
- 自然映射到 MCP/HTTP（同一套 schema）

### 实现

```python
# sodus 变成 thin wrapper
def main():
    if len(sys.argv) > 1:
        request = json.loads(sys.argv[1])
    else:
        request = json.load(sys.stdin)
    
    response = http_client.post("http://localhost:4199/rpc", json=request)
    print(json.dumps(response.json()))
    sys.exit(0 if response.json()["error"] is None else 1)
```

### 验收标准

- [ ] `sodus <json>` 工作
- [ ] 所有功能可通过 JSON-RPC 访问
- [ ] 文档：JSON-RPC method 完整列表
- [ ] 旧 CLI 彻底移除（或作为独立 wrapper）

## 优先级

**P0（已完成）**：L1 intake 修复（CRITICAL + HIGH findings）

**P1（搁置）**：
- D-001：clarify 答案回灌状态机
- JSON-RPC API 实现
- CLI 收束

**搁置原因**：用户主线收束到六级和谷歌面试，sodus 后续有时间再开发。

## 下次启动时的 TODO

1. 读这份 roadmap
2. 确认优先级（JSON-RPC 还是 CLI 收束）
3. 如果做 JSON-RPC：先实现 10 个高频 method，再扩展
4. 如果做 CLI 收束：先写迁移脚本（扫描现有调用），评估影响面

## 附：Agent Interface 设计参考

- **Kubernetes**：`kubectl get/create/delete/apply` + 资源路径
- **Docker**：`docker ps/run/stop/rm` + 容器 ID
- **Git**：`git show/log/diff` + object hash
- **Harness**（你提到的）：read/write/run/shell

共同点：
- 动词少（3-6 个）
- 资源路径一致（object/id 或 object/id/subresource）
- 输出可机读（JSON / YAML）
- 幂等性明确
