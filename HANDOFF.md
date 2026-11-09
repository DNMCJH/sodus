---
title: "sodus 项目接力控制台"
date: "2026-11-09"
updated: "2026-11-10"
version: 2.1.0
type: project-handoff
status: active
snapshot_at: "2026-11-10 00:45:00 +08:00"
tags:
  - workflow/handoff
  - sodus
---

# sodus 项目接力控制台

> [!abstract] 唯一接力入口
> 本文件是 sodus 仓库的权威项目控制台（`A:\VScode\Code\Projects\nodus\HANDOFF.md`）。新会话先完整读取本文件，再刷新“实时状态”，然后继续。sodus 项目上下文极大（上一会话约 1036M token、多次压缩），不要试图从聊天记录重建决策和路径。详细内容通过索引跳转，不在这里重复。

> [!warning] 快照边界
> 这是 2026-11-10 00:45 的快照，Markdown 不自动同步。`snapshot_at` 之后的变化必须重新核验：git HEAD、tag、daemon 版本/pid、预算、relay 余额、宿主配置。

## 0. 三分钟启动区

### 项目一句话

Sodus（实现名 nodus，命令 `sodus`）是给 agent 用的任务验收层：单机 daemon（默认 `127.0.0.1:4199`）+ CLI + MCP over stdio + 只读看板；核心是把“派活、验证、评审、记忆、计划、项目接力”收在一个 HTTP 契约下，让 Claude Code / Codex / opencode / DSH 只当薄客户端。

### 当前唯一优先

> [!todo]
> **0.8.1 已发布：plan 物化/执行/replan、handoff 迁移、协议修复。M4–M6 计划已重做并获 sol `approve`（`plan-9d7318c01882`，reviewed），但人工批准不能代办，保持 pending。下一阶段：**① 你回来后用交互式 TTY `sodus plan approve plan-9d7318c01882 --by human` 后再 execute；② 预注册效率 A/B（12 题 ×10 ×2 = 240 对）；③ DSH 面板、DB 备份、verify 3221225786 根因。

### 硬规则（用户明确要求，改之前先问）

- 跑 sodus 不要弹终端窗口；console 子进程必须带 `CREATE_NO_WINDOW`。
- 重大/有争议的方案先让 sodus 派 opus + sol 并行评审，再综合；不要单模型拍板。
- 不做 n=1/n=3 的统计结论；效率结论必须来自预注册的配对实验（见 §5）。
- main 必须可回退；每个阶段跑全量 pytest + `scripts/release_check.ps1` 并留证据。
- 预算没有日硬上限；relay 余额是硬约束。模型族软上限：claude/opus ¥100、gpt ¥80、other ¥80（只拦新 run，`--allow-expensive` 可豁免）。
- 不把密钥、token、provider apiKey 写进本文档；只写路径。
- 重启 DSH Web GUI 会中断当前会话，先问用户；重启 sodus daemon 可以直接做。

### 实时状态（2026-11-09 23:45，Checked/Measured）

| 项 | 值 |
|---|---|
| 版本 / tag | `sodus 0.8.1`；main `1754491`（merge 0.8.1），tag `v0.8.1` |
| daemon | 0.8.1，pid 11324，单实例监听 4199；`auto_recover=false` |
| 发行门禁 | `scripts/release_check.ps1` PASS，exit 0；全量 pytest 绿 |
| DB | `~/.nodus/nodus.sqlite3`，schema v8（plan_node.run_id 等，只增表可回退） |
| 预算 | `budget.soft_families={claude:100,gpt:80,other:80}`；无 `daily_cny`/`soft_cny`；今日花费与 relay 余额用 `sodus status` 重查 |
| 四宿主 | codex / claude / dsh / opencode 均 configured；`sodus mcp install --check` exit 0 |
| 并发 | 4 个 run 在不同目录并行实测通过；同目录仍由写锁串行 |
| 交接台 | `sodus handoff` 六命令可用；`sync` 能自动给旧控制台插 auto 标记并迁移；`check` 支持 warnings；MCP 三个只读工具 |
| 安全现状 | B3：env 白名单 + plan/review 临时 worktree；codex 的 Windows sandbox helper 无法在 worktree 启动，已改为隔离 cwd + 常规 headless flags（残余：无 OS 级隔离）；B4：HTTP approve 关闭，CLI 仅 TTY 确认 |
| 未推送 | 所有 tag/commit 只在本地，未 push origin |

## 1. 关键路径索引

| 用途 | 路径 |
|---|---|
| 主仓库 | `A:\VScode\Code\Projects\nodus` |
| 0.6/0.7/0.7.1/0.7.2 worktree | `A:\VScode\Code\Projects\nodus-0.6` … `nodus-0.7.2` |
| 0.8 分支 worktree | `A:\VScode\Code\Projects\nodus-0.8`（feat/sodus-0.8，已合并） |
| 0.8 安全 worktree | `A:\VScode\Code\Projects\nodus-0.8b`（feat/sodus-0.8-security，已合并） |
| 0.8 交付 | `reports/080a-report.md`（handoff）、`reports/080b1-report.md`（B3）、`reports/080b2-report.md`（B4）、`reports/080c-report.md`（迁移/协议）、`reports/080d-report.md`（plan 执行） |
| 旧版交接 | `reports/HANDOFF-0.7.2.md`、`reports/HANDOFF-0.7.1.md`、`reports/HANDOFF-0.7.md` |
| 复查原文 | `reports/review-0.7-opus-20261109.md`、`reports/review-0.7-sol-20261109.md` |
| 计划证据 | `reports/plan-M4-M6-20261110-v5.json`（plan `plan-9d7318c01882`，reviewed，verdict approve，待人工批准）；`reports/plan-review-M4-M6-20261110.json` |
| 本控制台 | `HANDOFF.md`（仓库根，随代码版本化）；Obsidian 入口 `B:\Obsidian\sessions\sodus\00_sodus_项目接力控制台.md` |
| 用户配置 | `~/.nodus/config.json`（备份 `config.json.bak-20261109-families`） |
| daemon 日志 | `~/.nodus/daemon.log` |
| token/DB | `~/.nodus/token`、`~/.nodus/nodus.sqlite3`（不要复制内容） |
| 多宿主配置 | `~/.codex/config.toml`、`~/.claude.json`、`A:\dsh\home\cordis.patch.yml`、`~/.config/opencode/opencode.json`（含密钥，勿外传） |
| 文档 | `docs/architecture.md`、`docs/mcp.md`、`docs/cli.md`、`docs/sodus-0.7-plan.md` |

## 2. 决策记录（为什么是现在这样）

- **0.7.0**：MCP 全部改成 daemon HTTP，daemon 是唯一 run 所有者；加只读 `GET /state`；planner 契约 + 机械 validator + `kind='plan'` 只读 run。
- **0.7.1**：正式启用前复查（opus+sol）后修 P1/P2——daemon 独占单实例、auto-recover 改配置默认关、Host 校验、verify 基础设施退出码重试、approve 前复核 HEAD/AGENTS/hash、installer 保留 env/原子写、review 异步+超时。
- **0.7.2**：按用户要求改成模型族软上限（Claude/opus 100、GPT 80、other 80），无日硬上限；补 opencode MCP 安装器；四终端并发文档与测试。
- **0.8.0**：项目级 `sodus handoff`（单项目一份 HANDOFF.md，工作区只放索引）；B3 env 白名单 + plan/review 只读（claude `--permission-mode plan`）+ 临时 worktree；B4 关闭 HTTP approve，CLI 仅 TTY 确认。
- **0.8.1**：plan 物化/执行/replan（schema v8）；handoff `sync` 自动迁移旧控制台、`check` warnings；修 HTTP 未授权 POST 残留 body 协议缺陷；午夜边界测试；codex 沙箱在 Windows worktree 启动失败→隔离 cwd 回退。
- **预算口径**：用户确认“没有定过预算”；relay 余额才是硬约束；族软上限只拦新 run。
- **并发口径**：多个宿主各自拉 MCP 进程，但都只是 HTTP 客户端；daemon 单实例、多线程；不同 cwd 可并行，同一 cwd 写锁串行；SQLite 单写者。
- **交接口径**：per-run handoff、session brief 都是单次快照；项目控制台是权威恢复入口，`sodus handoff` 只操作当前项目，不聚合内容。

## 3. 进度节点

| 日期 | 节点 | 状态 |
|---|---|---|
| 2026-11-09 | v0.7.0 发布：MCP/HTTP、/state、planner | ✅ |
| 2026-11-09 | v0.7.1 发布：启用前 P1/P2 修复、发行门禁 PASS | ✅ |
| 2026-11-09 | v0.7.2 发布：族软上限、opencode、四端并发 | ✅ |
| 2026-11-09 | v0.8.0 发布：项目 handoff、B3 隔离、B4 CLI 批准；release_check PASS | ✅ |
| 2026-11-10 | M4–M6 重做计划 + 多轮 sol review | ✅ `plan-9d7318c01882` reviewed，verdict **approve**；待人工 TTY 批准 |
| 2026-11-10 | v0.8.1 发布：plan 物化/执行/replan、handoff 迁移 | ✅ |
| 待定 | M4–M6 人工批准后实际执行 | ⏳ |
| 待定 | 效率 A/B（12 题 ×10 ×2 组 = 240 对） | ⏳ 设计已定，未跑 |

## 4. 未决与风险

- **B3 残余**：env 白名单 + 只读参数 + 临时 worktree 已做；但没有 OS 级沙箱，同用户仍可读 token/凭据；DSH/opencode 无原生只读开关，隔离失败会回退原 cwd 并记 warning；git worktree 是 HEAD 快照，不含未提交修改。
- **B4 残余**：HTTP approve 已关闭，CLI 仅交互式 TTY 确认并拒绝 worker 标记；但同用户可伪造 TTY、去掉环境标记或直连 SQLite，这不是身份隔离，只是提高无人值守自动批准的门槛。
- **plan**：M4–M6 计划已按 finding 重做并获 sol approve（`plan-9d7318c01882`），但人工批准不能代办；执行功能 0.8.1 已实现（materialize/execute/replan），尚未执行该计划。
- **B3 codex 回退**：Windows 下 codex sandbox helper 在 sodus 临时 worktree 启动失败（ShellExecuteExW 1223），已改为隔离 cwd + 正常 headless flags；这削弱了“只读”保证，主树仍由临时 cwd 隔离。
- **测试抖动**：0.8.0 期间全量/发行检查出现过 Windows `ConnectionAbortedError 10053`（loopback HTTP 测试），复跑通过；根因未查，暂视为测试基础设施抖动，不掩盖。
- **DSH 事故**：历史 daemon 在杀 MCP 进程时无日志退出，已多次恢复；当前 daemon 11324，继续观察。
- **其他**：DSH YAML 注释保留、DB 自动备份、verify `3221225786` 根因、DSH 面板、效率实验未做。
- **软上限语义**：按已记账成本检查，不预留在途/并发成本；多个 run 同时起跑可能一起越线，relay 余额仍是硬约束。

## 5. 下一步（按顺序）

1. 用户回来后在 TTY 执行 `sodus plan approve plan-9d7318c01882 --by human`，再 `sodus plan execute plan-9d7318c01882`；失败时用 `plan replan`。
2. 预注册并跑效率 A/B（12 题 ×10 ×2 = 240 对；exact McNemar，非劣界 −5pp；成本/时间配对 bootstrap，界 +20%）。
3. 补 DSH 面板（只读）、DB 自动备份、DSH YAML 注释、verify 根因、测试网络抖动、codex 只读回退的长期方案。
4. `sodus handoff sync` 会自动迁移旧控制台并在 `check` 中给 warnings；本项目控制台已迁移。

## 6. 会话/交接机制

- `sodus handoff init/show/append/sync/check/list`：项目级控制台；`sync` 只更新 auto 标记之间，`check` 校验 snapshot/HEAD/路径/daemon 版本。
- `sodus debug sessions --limit 20 [--query ...] [--vendor ...]`：扫 Claude/Codex/DSH 三家会话。
- `sodus debug session <id> --tail 40`：看某一个会话原话。
- `sodus debug brief <id> --task "..." --tail 12 --out PATH`：生成跨会话交接。
- `sodus debug lineage <id> [--json]`：会话→run→task→proposal 血缘。
- `sodus debug resume/branch`：继续/分叉会话（跨 vendor）。
- `sodus debug desk --out PATH`：把任务进度渲染成 markdown（vault dashboard 用）。

## 7. 命令速查

```powershell
A:\Anaconda\python.exe -m sodus status
A:\Anaconda\python.exe -m sodus runs
A:\Anaconda\python.exe -m sodus handoff show --json
A:\Anaconda\python.exe -m sodus handoff check --json
A:\Anaconda\python.exe -m sodus debug sessions --query nodus
A:\Anaconda\python.exe -m sodus plan list
A:\Anaconda\python.exe -m sodus plan approve plan-9d7318c01882 --by human
A:\Anaconda\python.exe -m sodus plan execute plan-9d7318c01882 --wait
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\release_check.ps1
git -C A:\VScode\Code\Projects\nodus log --oneline -5
```

## 8. 更新规则

- 谁更新：当班会话的 agent；每次发布、阶段结束、关键决策、重大事故后更新。
- 怎么更：只追加/修改对应节；`snapshot_at` 改成当前时间；涉及数字的写清来源（Checked/Measured/User-provided/Unverified）。
- 不复制：长报告、评审原文、日志、密钥只放链接/路径。
- 校验：`git HEAD`、tag、daemon pid/version、config、relay 余额在每次新会话开始时重查。
- 一份活的控制台，不并行维护多份；发现旧结论错了，直接改并留下“原判断/为什么错”。
<!-- sodus:auto:start -->
head: 175449112460e5d90445fc3e45539f39c05cf589
```json
{
  "head": "175449112460e5d90445fc3e45539f39c05cf589",
  "branch": "main",
  "tag": "v0.8.1",
  "daemon": {
    "version": "0.8.1",
    "pid": 11324
  },
  "tasks": [
    {
      "id": "task-08af48a5bdda",
      "title": "规划者架构 opus",
      "status": "open"
    },
    {
      "id": "task-752922796ef7",
      "title": "规划者架构 sol",
      "status": "open"
    },
    {
      "id": "task-3290c4d4bf9a",
      "title": "opus review sodus0.7",
      "status": "open"
    },
    {
      "id": "task-a2d2f16dafdc",
      "title": "0.7正式启用复查 opus",
      "status": "open"
    },
    {
      "id": "task-161e87a664e3",
      "title": "0.7正式启用复查 sol",
      "status": "open"
    }
  ],
  "runs": [
    {
      "id": "run-a2c009e67b01",
      "status": "completed",
      "kind": "work",
      "task_id": "task-752922796ef7"
    },
    {
      "id": "run-b2da0886cbbe",
      "status": "completed",
      "kind": "work",
      "task_id": "task-08af48a5bdda"
    },
    {
      "id": "run-34062f672382",
      "status": "completed",
      "kind": "work",
      "task_id": "task-3290c4d4bf9a"
    },
    {
      "id": "run-52f1c0a9e511",
      "status": "completed",
      "kind": "work",
      "task_id": "task-161e87a664e3"
    },
    {
      "id": "run-41d4f728d2b3",
      "status": "completed",
      "kind": "work",
      "task_id": "task-a2d2f16dafdc"
    }
  ],
  "plans": [],
  "budget_soft_families": {
    "claude": 100,
    "gpt": 80,
    "other": 80
  }
}
```
<!-- sodus:auto:end -->
