# Nodus CLI

> 17 个顶层命令，`nodus --help` 一屏读完。低频命令和诊断命令收在 `nodus debug` 下，
> 统计报表收在 `nodus stats` 下。旧名字都还能用（隐藏别名），只是不列在帮助里。

## 顶层命令

| 命令 | 干什么 | 例子 |
|---|---|---|
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
