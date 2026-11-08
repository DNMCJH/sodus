# plan.json v1（0.7.0-c1）

plan 是独立的执行提案，不是 task 完成状态；遵守 ADR-020，不写 task.status。
生命周期为 draft → validated → reviewed → approved → running → done/aborted。
c1 文件即 draft，只有校验成功后才能导入为 validated；c2a 执行链和 HTTP 见下文。

## JSON 契约

顶层对象字段：schema_version（整数 1）、goal（非空字符串）、non_goals（字符串数组）、
base_sha（目标仓库可解析的 40 位十六进制 commit）、recon（对象）、milestones（非空数组）、
nodes（数组）、risks（非空字符串数组）、rollback（非空字符串）、review_points（非空字符串数组）。
recon 包含 files（相对路径数组）、tests（命令数组）、agents_md（sha256 或 null）。
milestone 包含 id（唯一非空字符串）、title（非空字符串）、exit_criteria（非空字符串数组）。
node 包含 id（唯一非空字符串）、milestone（已定义的 milestone id）、title、description（非空字符串）、
deps（node id 数组）、file_scope（相对路径数组）、acceptance（非空命令数组）、
difficulty（整数 1–5）、route_hint（非空字符串，例如 sol）、budget_cny（有限非负数字）。

路径必须已存在于 cwd 内，允许目录，禁止绝对路径、上级逃逸和指向外部的符号链接。
新文件应把已存在的父目录列入 file_scope；校验不会创建文件。
deps 必须构成 DAG，每个 milestone 必须有节点。
acceptance 只做命令语法检查，拒绝“完成即可”等自然语言；不会执行，也不代表验收已通过。
可接受 python/pytest/npm 等常见命令或显式相对/绝对可执行路径及 shell 命令。
不检查命令是否安装、不调用模型、不执行 acceptance。

## 存储与接口

schema v6 additive 增加 plan 和 plan_node；迁移在既有事务中完成并幂等。
导入原子写入两表，plan.status=validated，version=1，approved_by/approved_at=NULL；
节点 status=pending，不派发任务。hash 为 UTF-8、sort_keys、紧凑分隔 JSON 的 SHA256。
plan_json 保留完整输入，errors_json=[]；id 由服务生成，重复导入产生独立记录。
workspace_id 取当前配置，show/list 仅展示当前工作区。

`sodus plan validate <file> [--cwd PATH] [--json]` 返回错误列表与 valid，退出码 0/1。
`sodus plan import <file> [--cwd PATH]` 校验后返回 plan id。
`sodus plan show <id> [--json]` 返回元数据、解码后的 plan_json 和节点。
`sodus plan list` 返回当前工作区摘要。这些检查和导入命令在本地执行；import 直接写入本地 SQLite 是过渡行为。
validate_plan 返回结构化错误列表（kind/path/message）；无错误即 []，输入异常转为 schema 错误。

## Planner execution (0.7.0-c2a)

`routing.planner` in config.json selects the planner route (default `opus`);
`--route` overrides it. `plan new` creates a kind=plan run against the target cwd.
The prompt requires read-only repository reconnaissance (`git rev-parse HEAD`,
`git ls-files`, AGENTS.md and test commands) and the v1 JSON object as its last
assistant message. Only that message is parsed; optional JSON fences are removed.
The c1 validator runs before storing canonical JSON/hash/base_sha and node rows.
A failed run, parse or validation leaves a failed attempt with errors_json and
its run_id; repeat `plan new` to create a fresh retry without losing evidence.

Lifecycle: draft -> planning -> validated -> reviewed -> approved; creation failures -> failed.
Review uses a separate kind=plan_review run on route `sol`, with plan JSON and
repository cwd. review_json contains verdict (approve/revise/reject) and findings;
review_run_id preserves its evidence. A fresh review clears the prior verdict;
invalid review output cannot retain approval eligibility. `approve --by human`
requires status=reviewed and verdict=approve, and writes approved_by/approved_at.
This parameter records an explicit human assertion, not independently verified identity.
No task or plan execution is dispatched by approval.

Plan new, review and approve run through the daemon; start it with `sodus up`.
New waits by polling by default; `--no-wait` returns immediately. POST /plan returns 202 with plan_id/run_id/status and background
collection; poll GET /plans/{id}. POST /plans/{id}/review is synchronous;
POST /plans/{id}/approve accepts {"by":"human"}. All four require bearer auth.
Workspace isolation follows the existing plan API. Additive plan_execution
metadata stores cwd/run_id/review_run_id/review_json alongside the v6 tables.
Read-only prompts, run notes and disabled automatic verification are advisory;
there is no enforced filesystem sandbox. No real model was called for validation.
