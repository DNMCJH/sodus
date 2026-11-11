# sodus 规划者架构评审 brief（2026-11-08）

## 用户前提（要正面回应，不要绕）
「DSH 是最快的本地执行器」这点用户认同；但用户认为 **DeepSeek（当前 DSH 主模型）不是足够好的组织者和规划者，大项目代码开发力有未逮**。所以：难任务派给 sodus 是理所当然的，问题变成——**规划（planner）该由谁做、怎么落到 sodus 里**。如果你的证据与这个前提冲突，直接说。

## 已核对的事实（0.6 分支）
- 已有：task/run 状态机、routes 回退链、`policy.py` 的轻量难度/类型评分（`assess(difficulty, kind, needs)` + `recommend(route, why)`，规则不是 LLM）、`runner` 起 worker、`propose` worktree、`verify`、ledger/budget、`jobs.py` 只有 `tdd/review/fix` 三个固定流水线、`ask→promote`。
- **没有**：规划产物（plan.md/plan JSON）、规划 run/路由、任务 DAG、依赖调度、计划审批、里程碑评审、计划修订（replan）。
- 昨晚 0.6 的实现是反例也是正例：DeepSeek（DSH）当 lead 做了 W0–W4 拆解、写 brief、派 opus/sol、监控；但设计 spec 的修订和建议实际来自 opus/sol 两份评审（它们抓出 2 个阻断项）。也就是说这次「规划」能成立，靠的是设计文档 + 双模型评审，不是 DeepSeek 自由发挥。
- 成本：opus 路线可用（用户允许昂贵路线），日 soft/hard 额度照常。

## 问题
Q1 **规划归属**：四种选项 (a) DSH/DeepSeek 规划、sodus 执行（现状）；(b) sodus 内置 planner（强模型，如 opus）规划，DSH 只审批；(c) 混合：DSH 负责交互澄清需求/约束，sodus planner 做仓库侦察+拆解+风险，DSH/人审批；(d) 不做 planner，只扩固定流水线。按质量、成本、延迟、故障域、上下文、可审计性对比，给出推荐。
Q2 **规划产物与生命周期**：plan 应该是什么（JSON schema / plan.md）？至少包含什么：目标/非目标、仓库侦察证据（文件、测试、AGENTS.md）、任务 DAG、每任务 file scope 与 acceptance、依赖与并行、风险、预算、回滚、评审节点。存在哪（artifact / 新表 / task note）？版本化与 replan 规则？谁批准（自动/人/DSH）？怎么映射到现有 task/run/propose/pipeline？
Q3 **模型与质量**：planner 用 opus？executor 用 sol/codex？reviewer 用 opus？要不要按难度路由（policy 已有 primitive）？如何防止 planner 幻觉仓库状态（强制先侦察、引用文件与测试、可能用 codegraph）？怎么量规划质量（计划接受率、返工率、任务通过率、每里程碑成本）？给出可预注册的 A/B 方案。
Q4 **大项目机制**：100+ 任务时拆解粒度、依赖排序、并行 worktree/提案、集成与合并策略、每任务 verify 门禁、进度与账本可见性、人在哪些点必须介入；哪些会先崩。
Q5 **落地路线**：0.7.x 具体步骤（例如 `POST /plan` + `sodus plan` + plan 审批 + `kind='plan'` run + milestone review + replan），每步验收；明确不做（如全自动 merge、agent swarm、planner 直接写代码）。

## 输出
当前目录 `take.md`，中文 ≤1200 字；结构：结论 → 规划者归属与分层 → Q1–Q5 → 风险 → 路线；末尾 SUMMARY。只读仓库，不改代码。

