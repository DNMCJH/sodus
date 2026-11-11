# 评审：spec-sodus-memory v0.1

## 入口
1. 递归：DSH 既是入口又是执行/dream 路由，run 内可再调 sodus_ask。应在 run 内禁用它，或加 depth 字段。
2. MCP 没有推送，DSH 也没有会话间通道（ADR-005），"下一条附带"做不到。应改为 sodus_status 轮询。
3. answer 缺少升级为 task 的路径。request_id 同 id 不同 text 应返回 409，并定义 TTL。
4. 任务状态机是第五份定义，违反 ADR-018。应先加进 lifecycle.py，并写明与 run 状态的映射。

## 记忆
5. lineage 文档已写明"记忆不再有 SQLite 表"。重新引入需要一份 ADR，并指定唯一真源：AGENTS.md 被手改时如何检测漂移。
6. exit=0 不等于成功（ADR-014），应以 verify 通过为门槛。失败 episode 也要写，否则"重复失败率"无从统计。lessons 要标 provenance。
7. FTS5 默认分词器不切中文，应改用 trigram。
8. 读文件的 harness 不走注入，hits 无从采集，退役规则会误杀条目。

## dream
9. "≥3 次"应按不同 task 计，重试不算。
10. 写 L3 时只动 marker 区块；回滚以 DB 版本为准，不依赖 git。
11. 单写者应复用 write_lock，覆盖 dream 与看板采纳同时写入的情况。

## 门槛
12. 缺 schema_version 与迁移测试；"其它会话可用"应做成脚本化 e2e。
13. bench 只有 6 题，A/B 功效不足，W1 就要算出所需 n。

## SUMMARY
最大的风险有五项：状态重复定义、与"去 SQLite"的决定冲突、把 exit=0 当成功、hits 采集不到、DSH 递归。动工前应先补 ADR、扩展 lifecycle、把门槛改为 verify、改用 trigram、定义轮询与防递归，并补上迁移测试和 e2e。
