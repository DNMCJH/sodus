# 0.8.1-exec brief：plan 节点物化/执行/replan

## 边界
- 工作区 `A:\VScode\Code\Projects\nodus-0.8.1`（feat/sodus-0.8.1，已含 plan/handoff/loopback 修复 + M4–M6 reviewed plan 证据）。**不 git commit / push / merge**。
- 只做计划执行能力；不改 M4–M6 计划节点本身，不执行真实 M4–M6（它仍需人工批准）。
- 先读 `nodus/plan.py`、`nodus/db.py`（plan/plan_node schema、迁移）、`nodus/server.py`（/plan 路由）、`nodus/cli/commands/plan.py`、`tests/test_plan.py`、`tests/test_openapi.py`、`openapi.yaml`、`docs/cli.md`、`docs/architecture.md`。

## 需求
1. **schema v8**：`plan_node` 增加 `run_id TEXT`（以及执行需要的 `error`/时间字段仅在确有需要时加）；`db.SCHEMA_VERSION=8`，新增 `_migrate_v8`，旧 v7 数据库升级测试。
2. **materialize(plan_id)**：只接受 `status='approved'`（或已 approved 且未执行）；按 DAG 拓扑序为每个节点 `db.create_task`（title=节点 title，goal=plan goal + 节点 description + file_scope，acceptance=节点 acceptance 命令拼接），写回 `task_id`，节点状态 `materialized`；幂等（重复调用不重复建 task）。
3. **execute(plan_id, route=None, wait=True)**：只接受 approved（materialize 后也允许）；后台线程按 DAG 顺序启动节点 run（`runner.RUNNER.start(task_id, route=节点 route_hint or 指定 route, cwd=plan.cwd, kind='work', verify_run=True)`），记录 run_id；节点 completed→done，failed→failed 并停止下游（plan 状态 `failed`/`partial`）；全部完成→`done`；每一步做预算检查；同一 plan 不允许并发执行；daemon 重启时 `plan.reconcile()` 对 `executing` 的处理要明确（建议置 failed 并保留已完成节点）。
4. **replan(plan_id, feedback)**：创建新 plan 记录（parent_id=旧 id、version+1、status=planning），用旧 plan_json + feedback 拼 prompt，异步 opus planner run；完成走现有 `_finish_create` 逻辑；旧 plan 保持不变。
5. **HTTP**：`POST /plans/{id}/execute`（需要已 approved，返回 202/400）、`POST /plans/{id}/replan`（body feedback，返回 202）；鉴权/递归防护沿用；openapi 更新。
6. **CLI**：`sodus plan execute <id> [--route] [--wait|--no-wait]`、`sodus plan replan <id> --feedback TEXT`；经 daemon HTTP；帮助文本说明执行必须已人工批准。
7. **测试**：materialize 幂等/拓扑序；fake runner 执行顺序、依赖失败停止、预算检查、并发保护；未 approved 拒绝；replan 新版本/parent/planner 调用；schema v7→v8 升级；HTTP 202/400；CLI 参数与调用。
8. **文档**：`docs/cli.md`、`docs/architecture.md`、`openapi.yaml`、`CHANGELOG.md` 记 0.8.1 条目（版本号由 Lead bump）；`reports/080d-report.md`。

## 验收
- 全量 `A:\Anaconda\python.exe -m pytest -q` 退出码 0；
- 不执行真实 M4–M6；用 fake runner/临时 plan 验证；
- 080d 报告含证据/遗留/SUMMARY。
