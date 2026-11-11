结论：Sodus加薄对话入口与可审计巩固；DSH并存，保留执行及会话，不重写harness。

1）入口分层
壳管输入、进度、待决；daemon内Router复用policy，小问短答，大事建task/run。task持久化目标、预算、状态；worker经runner/routes调用CLI，复用verify/review/fix、锁和recover。以channel扩展入口。

2）记忆与巩固
沿用harvest/conflicts/adopt，不恢复旧事实表。四层：只读会话及run证据；任务摘要（决策、教训、未决、偏好候选）；项目AGENTS.md事实/playbook；可重建索引。新增SQLite候选表(id,workspace,kind,key,text,source,hash,status,version,expires,supersedes)。摘要引用证据；playbook标条件、日期、证据。
任务结束入队，空闲每日按增量、预算抽取→去重→查矛盾/过期→提案→人工adopt；版本可回滚。冲突挂起、过期退役，自述不升格事实，隔离项目。

3）2–4周MVP
首周先建候选模型、回放历史；次周加POST /chat，以request_id幂等派活；第三周加POST /memory/consolidate及adopt审核；第四周固定任务对照有/无记忆。验证重启不丢任务、不重复派活、巩固幂等、冲突不误覆盖；比较隐藏测试通过率、重复犯错率、过期误用率及token/成本。

4）取舍与风险
取Comma的Task中心、上下文隔离、事件唤醒，取dreaming异步整理和文本审计；不搬多租户、云VM、eBPF、形式验证。误路由、投毒、双写、超支分别以决策留痕、人工采纳、单写锁、限额约束。

SUMMARY
设计待实现。
