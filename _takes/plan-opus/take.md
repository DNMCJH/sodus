# sodus 规划者架构评审（opus）

## 结论
选 (c) 混合：DSH 澄清需求，sodus 内的 `kind='plan'` run（opus）侦察并拆解，sol 交叉评审，人批准。plan 是带校验的 JSON 产物，不是聊天文本。

**前提修正**：①「DSH 最快」不成立：bench-hard 平均 DSH 203s、minimax 90s、codex 165s，简单题 DSH 41s、minimax 30s；DSH 胜在便宜且 3/3 全解（n=3，单次）。②「DeepSeek 不会规划」没有测过。昨晚 DSH 的 W0–W4 拆解能执行完，问题出在 spec（两份评审各指出 5–13 条）和代码（H1/H2）上。所以证据只能说明：规划必须有强模型评审和机械校验。说不上 DeepSeek 拆不了。③`assess` 先跑规则，再可选地让 Jev 用 LLM 复核一次，并非纯规则。

## Q1 归属
| | 质量 | 成本 | 延迟 | 故障域 | 审计 |
|-|-|-|-|-|-|
| a | 无校验 | 低 | 低 | 在 DSH 会话里 | 差 |
| b | 高 | 高 | 中 | sodus | 好，但缺需求澄清 |
| c | 高 | 中（单次规划约 ¥1–3） | 中 | sodus | 好 |
| d | 只适合同构任务 | 低 | 低 | — | 无拆解 |

c 的分层：DSH 管交互和约束，planner 管侦察、拆解和风险，sol 评审，人在门禁处批准。

## Q2 plan 契约
`plan.json` 必含：goal / non_goals、`base_sha`、recon（文件、测试命令、AGENTS.md hash）、milestones[]、nodes[]（id、deps、file_scope、acceptance 命令、difficulty、route_hint、budget）、risks、rollback、review_points。
存储：artifact 存文件，新增 `plan`（version、parent、status、hash）和 `plan_node`（node→task_id）两张表（schema v6）。
replan 生成新版本；已完成的节点冻结；只要动到已完成节点、预算超 20% 或改 non_goals，必须人批。批准后每个 node 生成一个 task（acceptance 照抄），写代码的 node 走 propose。

## Q3 模型与质量
planner 用 opus；plan 评审换 sol（异源模型）；executor 由 `policy.recommend` 选，取 planner 和规则两边 difficulty 的较大值；里程碑评审用 opus。
防幻觉：先机械侦察（`git ls-files`、`verify.detect`），把结果注入 prompt；validator 拒收不存在的路径、有环的 DAG、没有 acceptance 的节点。codegraph 可选。
指标：plan 原样通过率、返工率（每节点 fix 次数）、节点 verify 首次通过率、每里程碑成本。
预注册 A/B：12 道多文件题，executor 固定 sol，对比 DSH 规划和 opus 规划，随机交错跑，主指标是隐藏测试通过率。按功效报告，要检出 +15pp 需约 120 对；先跑 36 对作试点，明确标注功效不足。

## Q4 大项目
- 粒度：一个节点约 1 个 run（≤30 分钟、≤5 文件），每 5–15 个节点组成一个里程碑；采用滚动规划，只细拆当前里程碑。
- 并行：file_scope 不相交的节点各开 propose；merge 按拓扑序串行，每次 merge 后在主树跑全量 verify。
- 先崩的地方：热点文件（db 迁移、CLI 计数测试，昨晚改过 18→19）；proposal 基线过期导致冲突；auto-recover 抢写锁（05:15 事故）；codex exit=1 误报（共 3 次）；10 个以上并发写 SQLite 没测过（ADR-001）。
- 必须人介入：批准 plan、里程碑边界、schema/公开 API 节点、预算触到 hard 上限、replan、合入 main。

## 风险
单个 planner 会产生系统性偏差，靠异源评审缓解；plan 输出格式可能不合法，需要显式 failed 状态并允许重试（吸取 H1 的教训）。

## Q5 路线
- 0.7.0：schema v6、validator、`POST /plan` + `sodus plan`、只读的 plan run。验收：给 0.6.1 遗留项（M4–M6）生成 plan，零虚构路径；格式非法时进入 failed 并可重试。
- 0.7.1：approve 后把节点物化为 task，`sodus plan next` 列出依赖已满足的就绪节点，ledger 按里程碑汇总。验收：用 plan 跑完 M4–M6。
- 0.7.2：里程碑评审 + replan。验收：人为制造节点失败，触发新版本，已完成节点不变。
- 0.7.3：不相交节点并行跑 propose，串行 merge 队列；跑 A/B 试点。
- 不做：自动合入 main、agent swarm、planner 写代码、未经批准的 replan、LLM 调度器。

## SUMMARY
推荐 c：DSH 澄清，opus 侦察和规划，sol 评审，人批准，sodus 执行。plan 是带 base_sha 的版本化 JSON，经机械校验后物化为 task。用户前提里两点证据不足：DSH 并非最快；DeepSeek 不会规划也没测过，昨晚的失败点在 spec 和代码，不在拆解。0.7 先做契约和校验，再做物化、评审、replan，最后才做并行。
