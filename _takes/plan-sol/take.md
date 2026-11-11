结论：选(c)：DSH澄清，sodus强模型规划，人授权。单次成功不能判定DeepSeek规划能力。

归属／Q1（质量/成本/延迟/故障域/上下文/审计）：a快省但依赖lead、上下文散、恢复难；b证据集中便审计，但昂贵、交互不足；c多一次交接，需求/仓库分工、隔离故障、持久审计；d便宜快速可靠，但仅适用固定流程。推荐c，小任务保留d。

Q2：JSON Schema为契约，plan.md由其渲染；含目标/非目标、base SHA、文件行号/hash、测试/AGENTS证据、任务ID/DAG、scope、acceptance/verify、并行限制、风险、预算、回滚及评审节点。artifact存不可变版本，新plan/version/node/edge表存状态、审批人与hash。draft→review→approved→running→done/blocked；节点映射task，尝试映射run，复用tdd/review/fix及propose。漂移/阻断/超预算生成新版本、冻结受影响下游；重大变更人批，低风险DSH按授权批。

Q3：难规划用opus，sol/codex执行，独立模型审计划/里程碑；policy按难度路由，记录实际model/provider与降级。强制先只读侦察、校验引用和测试。预注册A/B：同SHA/需求，a对c，执行器和预算相同，隔离运行、盲评；按难度配对，按最小效应算样本量及停止规则。测接受率、返工率、验收通过率、里程碑总成本/耗时。

Q4：100+任务按里程碑滚动细化，每节点一次可验收改动；拓扑就绪且scope无冲突才并行。worktree提案串行进集成分支，新SHA复验、人批合入；节点verify/scope门禁，集成全量测试。看板关联plan/version/node/run与ledger。需求变更、破坏迁移、超授权预算、发布须人介入。

风险：policy.py:404有可选Jev复核，brief“不是LLM”不准确。jobs.py:132重启仅标失败；propose.py:136合并无测试门禁。OPS_STATUS记录旧run恢复占锁：恢复、共享文件冲突、基线过期先崩。db无通用plan/DAG。HANDOFF称旧opus报告H1/H2已修。

路线／Q5：0.7.0加POST /plan、sodus plan、kind=plan及schema，验收非法输出可重试、引用可核；0.7.1加版本审批/幂等调度，验收拒环、去重、重启对账；0.7.2加milestone review/replan与集成门禁，验收漂移阻断、旧审批失效、预算并发预留。保留日soft/hard额度；不做自动merge、swarm、planner写代码。

SUMMARY：先契约与恢复，再强模型规划，再扩并行；模型由实验判定。



