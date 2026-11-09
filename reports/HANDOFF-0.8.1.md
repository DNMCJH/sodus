# HANDOFF 0.8.1（2026-11-10）

## SUMMARY

0.8.1 已发布：main merge `1754491`、tag `v0.8.1`，daemon 0.8.1（pid 11324，单实例）。交付：plan 物化/执行/replan（schema v8）、handoff 旧控制台自动迁移与 check warnings、HTTP 未授权 POST 残留 body 协议修复、午夜边界测试修复。release_check PASS。

## 交付

- **plan 执行链**：`materialize` 把 approved plan 节点幂等物化成 task DAG；`execute` 按拓扑序启动 run、记录 run_id、失败停止、预算检查、并发保护；daemon 重启把未完成执行标记 failed 并保留已完成节点；`replan` 生成 parent-linked 新版本（version+1）并由 opus planner 异步生成；HTTP `POST /plans/{id}/execute|replan`、CLI `sodus plan execute|replan`。
- **handoff 迁移**：`sync` 在无 auto 标记的旧控制台里自动插入标记并迁移（备份 + 原子写 + CRLF 保留）；`check` 增加 warnings，HEAD 漂移和无 auto 段降级为 warning，真实错误仍 error；本项目 `HANDOFF.md` 已迁移并通过 `handoff check`。
- **协议修复**：HTTP 未授权 POST 在读取 body 前返回 401，残留 JSON 会污染同一连接的下一次请求；改为先读 body 再鉴权/拒绝。
- **测试**：午夜边界测试改为以 `budget.day_start()` 锚定；全量 1208 项 pytest 通过。

## 计划状态（重要）

- M4–M6 计划重做完成：`plan-9d7318c01882`，status `reviewed`，sol verdict **approve**，findings 空，base_sha 157654c（0.8.1 分支）。
- 证据：`reports/plan-M4-M6-20261110-v5.json`、`reports/plan-review-M4-M6-20261110.json`。
- **尚未人工批准**；B4 要求交互式 TTY。用户回来后执行：`sodus plan approve plan-9d7318c01882 --by human`（TTY 环境），再 `sodus plan execute plan-9d7318c01882 --wait`。

## 验证

- `scripts/release_check.ps1`：PASS，exit 0（构建/隔离安装 0.8.1、doctor、MCP、DB 升级、全量 pytest）。
- 全量 pytest：1208 项通过（plan 执行任务验证 + Lead 复跑）。
- `sodus handoff sync` 实机迁移本项目控制台；`sodus handoff check` 返回 ok=true、errors=[]、warnings=[]（提交前 HEAD）。
- `sodus mcp install --check`：四宿主 configured，exit 0。

## 遗留/风险

- M4–M6 未执行（需人工批准）；plan 执行是串行、失败整条 DAG 停止、无自动重试；节点 budget 是计划元数据，实际用现有预算门禁。
- B3：codex 的 Windows sandbox helper 在临时 worktree 启动失败（ShellExecuteExW 1223），改为隔离 cwd + 常规 headless flags；主树由临时 cwd 隔离，但不再是 OS 级只读。
- B4 残余：同用户可伪造 TTY/读 token/直连 SQLite。
- 测试 10053 的协议触发已修，Windows 网络抖动残余未完全排除。
- DSH 面板、DB 自动备份、verify 3221225786 根因、效率 A/B 未做。

## 回滚

- `git revert -m1 1754491`（或回 `v0.8.0`），重启 daemon；schema v8 只增列（plan_node.run_id），旧代码可读。
- 计划执行 API 默认拒绝未批准 plan，回滚不会自动执行任何计划。
