> 产品名 Sodus，实现名 nodus。命令一律仍写 `nodus`；`sodus` 是同一份 CLI 的第二个入口（切换见 [sodus-migration.md](sodus-migration.md)）。

# Sodus CLI

> 21 个顶层命令，`nodus --help` 一屏读完。低频命令和诊断命令收在 `nodus debug` 下，
> 统计报表收在 `nodus stats` 下。旧名字都还能用（隐藏别名），只是不列在帮助里。

## 顶层命令

| 命令 | 干什么 | 例子 |
|---|---|---|
| `handoff` | 项目接力控制台 | `sodus handoff show --json` |
| `nodus up` | 后台起 daemon，关终端不断 | `nodus up` |
| `nodus down` | 停掉本机 daemon | `nodus down` |
| `nodus status` | 最近探针 + 今日用量 + 余额 | `nodus status` |
| `nodus doctor` | 自检：目录、token、DB、agent、会话 | `nodus doctor` |
| `nodus task "目标"` | 建任务，打印 task-id | `nodus task "修 README 错别字" --accept "pytest 绿"` |
| `nodus run <task-id>` | 给任务派一次 run 并跟到底 | `nodus run task-abc123 --route auto` |
| `nodus runs` | 列 run | `nodus runs --task task-abc123` |
| `nodus show <run-id>` | 一个 run 的原始记录（JSON） | `nodus show run-abc123` |
| `nodus proposals` | 待合入的提案 | `nodus proposals --all` |
| `nodus merge <id>` | 把提案合进主树 | `nodus merge prop-abc123` |
| `nodus reject <id>` | 丢弃提案和 worktree | `nodus reject prop-abc123` |
| `nodus verify` | 跑检测到的测试命令 | `nodus verify --cwd .` |
| `nodus review <run-id>` | 跨 agent 审查一次 run | `nodus review run-abc123` |
| `nodus stats` | 今日概览：花费、用量、缓存命中 | `nodus stats` |
| `nodus mcp` | stdio MCP server（Claude Code / DSH 接） | `nodus mcp` |
| `nodus serve` | 前台跑 HTTP daemon | `nodus serve --port 4199` |
| `nodus migrate-home` | 把 `~/.nodus` 复制到 `~/.sodus` 并校验；默认 dry-run，`--apply` 才落地 | `nodus migrate-home` |
| `nodus debug <name>` | 31 个低频/诊断命令，见下 | `nodus debug lineage ses-abc123` |

## stats 明细

| 命令 | 干什么 | 例子 |
|---|---|---|
| `nodus stats cost` | 标定单价，以及和账单的漂移 | `nodus stats cost --runs 10` |
| `nodus stats usage` | 按日/月/全部 × route/model/provider/agent 的用量与成本 | `nodus stats usage --period month --by model` |
| `nodus stats bench` | 在 `eval/tasks` 上给一个 route 打分 | `nodus stats bench --route opencode-glm` |
| `nodus stats balance` | 中转站余额 | `nodus stats balance` |

## 隐藏别名（脚本不用改）

- `nodus cost|usage|bench|balance` ≡ `nodus stats <name>`，参数和输出不变。
- `nodus debug` 里的 31 个命令也都能直接敲：`nodus lineage`、`nodus report`、
  `nodus probe`、`nodus sessions`……和 `nodus debug <name>` 是同一个 handler。
- 这些名字不在 `--help` 里显示，但解析行为和以前完全一样。


### Memory consolidation and rollback (0.6)

```powershell
sodus memory dream --dry-run --json
sodus memory dream --now --limit 50 --json
sodus memory reindex --project A:\path\project
sodus memory rollback --to 2 --project A:\path\project
```

`dream --now` plans, extracts and counts evidence into staged candidates. The daemon
runs consolidation when no queued/running run exists, once per local calendar day
(persisted per workspace, including failed attempts). It then applies human-approved
adoptions/retirements through the same journalled L3 writer. Extraction uses
`routing.dream` (fallback `routing.default`); `memory.dream_daily_tokens` defaults to
100000. At the daily token ceiling no new batch starts; an ongoing run is not killed.
Failures are logged and do not stop the daemon. The scheduler checks every 300 seconds.

Rollback replays recorded ops through the requested global memory version for the
selected project's AGENTS.md, including the initial marker baseline, then performs
a new apply: versions keep increasing and old batches remain intact. Other sections
of the file are preserved. Invalid/unrecorded versions are rejected. Manual marker
drift refuses writes with a request to run `memory reindex` first. Rollback does not
require git. Versions predating recorded history cannot be reconstructed.

The memory board exposes candidate status, independent evidence count, and
adopt/reject/edit/retire actions. Edits carry evidence to the replacement candidate;
stale versions return HTTP 409 and ask the operator to refresh. Approval queues the
L3 change for the daemon; it never allows a model to approve its own facts.

### mcp install

`sodus mcp install --host codex|claude|dsh|opencode` 配置 MCP 宿主；可逗号分隔或重复 `--host`。`--check` 检查状态，`--uninstall` 删除 Sodus 条目，`--dry-run` 预览。

### Plan generation and approval (0.7.0-c2a)

- `sodus plan new <goal> --cwd PATH [--accept COMMAND] [--route ROUTE] [--wait|--no-wait]`
- `sodus plan review <id>` runs independent sol review and shows its JSON verdict.
- `sodus plan approve <id> --by human` requires an approving review. Approval requires interactive CLI with both stdin/stdout TTY. It shows id, status, review verdict and the first 12 hash characters, then requires the exact plan id; mismatch or EOF cancels. `NODUS_ROLE=worker` / `NODUS_RUN=1` processes are rejected.
- `sodus plan show <id> [--json]` / `sodus plan list` show status, run and review.

Start the daemon with `sodus up`. New and review use daemon HTTP;
generation waits by default, while `--no-wait` returns immediately
after run submission. Failures persist with errors; repeat `new` for a fresh attempt.
Approval runs locally via `plan.approve(id, "human")`, retaining hash/HEAD/AGENTS/validator checks. HTTP `POST /plans/{id}/approve` is disabled (403 `approve_requires_cli`).
Validate, import, show and list run locally; import writes directly to local SQLite
as a transitional behavior.
The default planner route is `opus`, configurable as `routing.planner` in config.json.
Planning/review request read-only behavior but do not enforce a filesystem sandbox.

`mcp install` 默认包含 opencode；`--opencode-config PATH` 覆盖其配置路径，也支持 `OPENCODE_CONFIG` / `XDG_CONFIG_HOME`。详见 [MCP 文档](mcp.md)。

## 项目接力控制台

`sodus handoff` 无需启动 daemon；项目根优先取 git root，否则取 cwd。
默认文件为根目录 `HANDOFF.md`；用户配置 `handoff.path` 可设相对项目根或绝对路径。

```text
sodus handoff init [--force]
sodus handoff show [--json]
sodus handoff append "完成阶段验收" [--section 最近变更]
sodus handoff sync
sodus handoff check [--json]
sodus handoff list [--json]
```

`init` 默认拒绝覆盖；`append` 在已有二级标题节追加日期 bullet 并刷新
`snapshot_at`。写文件采用临时文件 + replace。`sync` 仅替换
`<!-- sodus:auto:start -->` 与 `<!-- sodus:auto:end -->` 之间的机器段，
包含 HEAD/tag/分支、daemon 版本/pid、项目 task/run/plan 摘要及预算族上限；
首次对无标记的已有文件执行 `sodus handoff sync`，会先在同目录备份为
`HANDOFF.md.bak-*`，再在“实时状态”标题后插入标记；缺少该节时追加到末尾并在 stderr 提示。
不完整、逆序或重复标记仍拒绝执行。原子替换保留人工段、原有换行（包括 CRLF）和 snapshot。

`check` 检查文件、snapshot（默认 7 天，可配 `handoff.max_age_days`）、HEAD、
“关键路径”节反引号中的路径和 daemon 版本。结果包含 `errors` 和 `warnings`：
HEAD 漂移（包括正常提交后的祖先 HEAD）为 `head_mismatch` warning；无自动段时为
`auto_block_missing` warning，提示先运行 sync 迁移，不报 daemon_unavailable。
缺文件、异常标记、snapshot 无效/过期、关键路径缺失、有自动段但 daemon 不可用或版本不一致仍为 error。
`ok` 和退出码仅由 errors 决定：无 error 返回 0，否则 1。
`sync/check` 仅尝试读取 daemon，不会自动启动。`list` 从当前 cwd 和现有 DB 的
task/run cwd 去重，只输出元数据；DB 不可用时退回当前项目，不创建正文库或缓存。
旧逐 run 交接仍为 `sodus debug handoff <run>`。

HTTP 只读 `GET /handoff?cwd=...`、`GET /handoffs`、`GET /handoff/check?cwd=...`
均需 bearer token；cwd 必须解析到当前或已有 task/run 的项目根。
相对控制台路径禁止通过 symlink/上级路径逃到项目外；显式绝对配置路径由用户信任。
不提供写端点、内容聚合或自动 hook。
