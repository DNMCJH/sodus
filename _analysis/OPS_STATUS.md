# OPS STATUS — sodus 0.8.x goal

- goal: `goal-65a82bde-9e23-4608-9d31-25dc5b605887`
- 最近更新: 2026-11-10 00:55 +08:00
- 状态: 0.8.1 已发布；M4–M6 plan reviewed approve，等待人工 TTY 批准；无运行中任务

## 已完成

- v0.8.1：main 1754491，tag v0.8.1，daemon 11324；release_check PASS；全量 pytest 1208 项通过。
- plan 执行链：materialize/execute/replan，schema v8；HTTP/CLI；失败停止/预算/并发保护/重启 reconcile。
- handoff 自动迁移 + check warnings；HTTP 未授权 POST 残留 body 协议修复；午夜边界测试。
- M4–M6 计划：`plan-9d7318c01882` reviewed，sol verdict approve；证据 plan-M4-M6-20261110-v5.json / plan-review-M4-M6-20261110.json。
- A/B 预注册文档：`reports/ab-preregistration-20261110.md`（未跑）。

## 下一步（无人工时的可做项）

1. 合成小计划端到端 smoke（临时 repo，非 M4–M6）：import→内部 approve 测试桩→execute→verify；不冒充 M4 批准。
2. DSH 面板只读展示 handoff/plan 状态；DB 自动备份；verify 3221225786 根因。
3. 效率 A/B 先做 harness + 小 pilot，不拿小样本下结论。

## 需要用户（回来后）

- 在交互式 TTY：`sodus plan approve plan-9d7318c01882 --by human`，再 `sodus plan execute plan-9d7318c01882 --wait`。

## 风险 / 约束

- relay 余额 ~¥90；不重启 DSH GUI；B3/B4 残余风险；plan 执行串行无自动重试。
