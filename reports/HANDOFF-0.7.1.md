# HANDOFF 0.7.1（2026-11-09）

## SUMMARY

0.7.1 已合并进 main（merge `cb476fb`，tag `v0.7.1`），daemon 已重启为 0.7.1（单实例）。release_check PASS；启用前复查的 P1/P2 修复已落地。判定：**有人值守可以放心作为日常主版本；无人值守/自动批准执行仍被 0.7.2 的隔离项阻塞**。

## 这版修了什么

- **daemon 单实例**：独占 bind（Windows `SO_EXCLUSIVEADDRUSE`）+ 先 bind 后 reconcile/recover + 启动锁；第二个实例非 0 退出且不改变 run 状态。
- **auto-recover 默认关**：改为 `daemon.auto_recover`（默认 false，env `NODUS_AUTO_RECOVER` 可覆盖）；隐式 ensure_daemon 启动也遵守。
- **安全**：Host 头只接受 loopback（防 DNS rebinding）；approve 前复核 HEAD/AGENTS 哈希与 plan hash；plan/review 超时会杀 worker 进程树；`mcp install` 合并用户 env、原子写、DSH 缺失记 skipped。
- **质量信号**：Windows 控制台终止码（3221225786 等）归为 infra 失败并自动重试 verify 一次，不再误判测试红；review 异步化+超时。
- **planner 完善**：validator 增加 base_sha 漂移 warning、AGENTS.md 哈希、acceptance 命令白名单；plan_execution 走 schema v7；CLI reconcile 经 daemon。
- **文档/数据**：回滚改为 `git revert -m1`；更正 `2026-11-08_ab-power.md` 的 p10/p01 含义。

## 启用 checklist（实测）

| 项 | 结果 |
|---|---|
| 单实例 | ✅ 第二个 `sodus serve --port 4199` 退出 1，监听仍只有原 daemon，run 状态不变 |
| 隐式拉起 | ✅ kill 后 `sodus status` 自动拉起新 daemon，日志 `auto-recover disabled`，单监听 |
| Host 校验 | ✅ `Host: evil.example` → 403；127.0.0.1 正常 200 |
| `/state` | ✅ version=0.7.1，字段完整 |
| MCP 安装 | ✅ codex/claude/dsh 全部 configured，exit 0 |
| reconcile | ✅ 经 daemon 执行返回 `0 run(s) marked failed` |
| 预算 | ✅ 用户未定过预算：已移除 daily/soft 上限（无日上限），relay 余额是唯一硬约束；daemon.auto_recover=false |
| release_check | ✅ PASS（构建、隔离安装、doctor、MCP、DB 升级、全量 pytest） |

## 已知缺口（0.7.2，阻塞无人值守）

- worker 仍在关闭沙箱模式运行，plan/review 的“只读”只靠提示词；worker 继承 daemon 环境（未做 env 白名单）——B3。
- 批准身份仍是 `--by human` 声明，同机同用户可读 token/文件，无法独立证明操作者——B4；0.7.1 只加了 plan hash/HEAD/AGENTS 复核。
- plan 节点执行/物化/预算派发/replan 未实现；M4–M6 的 plan 仍是 review=revise，需修订后再批准。
- DSH 的 MCP 条目已写入配置，但当前 DSH GUI 未重启，需重启生效。
- 预算：用户确认没有定过预算。旧配置（2026-11-05 备份）里本来没有 budget；50/20 是后来开发 0.5.1 时写进 config 的保守值，0.7.1 已移除（daily/soft=0，无上限），relay 余额（当前约 105.6）是唯一硬约束。
- `sodus serve` 第二个实例的报错是 instance_lock 的 PermissionError（行为正确、文案不理想）；DSH YAML 注释保留、DB 自动备份、verify 3221225786 根因仍在 0.7.2 清单。

## 高效性（尚无结论）

没有直接 agent vs sodus 的配对数据，不能声称更省/更快/更好。预注册方案：12 道中等题（避开满分天花板）× 每题每组 10 次 × 2 组 = 240 对；主指标隐藏测试通过率（exact McNemar，非劣界 −5pp），成本/时间用配对中位差 bootstrap（界 +20%）；先导 36 对只能热身。当前可观测：今日 29 runs，中位成本 ¥1.48、中位耗时 262s，但 24 个 completed 里仅 7 个有 verify 证据，质量提升暂无统计证据。

## 回滚

- `git revert -m1 cb476fb`；回退前备份 `~/.nodus/nodus.sqlite3`（schema 已到 v7，旧代码只增表可读）。
- 预算/daemon 配置备份：`~/.nodus/config.json.bak-20261109-final`。