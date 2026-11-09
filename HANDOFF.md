---
title: "sodus 项目接力控制台"
date: "2026-11-09"
updated: "2026-11-09"
version: 1.0.0
type: project-handoff
status: active
snapshot_at: "2026-11-09 08:00:00 +08:00"
tags:
  - workflow/handoff
  - sodus
---

# sodus 项目接力控制台

> [!abstract] 唯一接力入口
> 本文件是 sodus 仓库的权威项目控制台（`A:\VScode\Code\Projects\nodus\HANDOFF.md`）。新会话先完整读取本文件，再刷新“实时状态”，然后继续。sodus 项目上下文极大（当前会话约 1036M token、多次压缩），不要试图从聊天记录重建决策和路径。详细内容通过索引跳转，不在这里重复。

> [!warning] 快照边界
> 这是 2026-11-09 08:00 的快照，Markdown 不自动同步。`snapshot_at` 之后的变化必须重新核验：git HEAD、tag、daemon 版本/pid、预算、relay 余额、宿主配置。

## 0. 三分钟启动区

### 项目一句话

Sodus（实现名 nodus，命令 `sodus`）是给 agent 用的任务验收层：单机 daemon（默认 `127.0.0.1:4199`）+ CLI + MCP over stdio + 只读看板；核心是把“派活、验证、评审、记忆、计划”收在一个 HTTP 契约下，让 Claude Code / Codex / opencode / DSH 只当薄客户端。

### 当前唯一优先

> [!todo]
> **保持 0.7.2 作为可放心使用的有人值守版本；下一阶段只做三件事：**① 把 sodus 的 session/brief 能力升级成项目级接力控制台（`sodus handoff`）；② 补无人值守前的 B3/B4 隔离（worker 沙箱/env 白名单、独立批准身份）；③ 把 M4–M6 计划按 sol review 的 revise 意见重做并批准执行。

### 硬规则（用户明确要求，改之前先问）

- 跑 sodus 不要弹终端窗口；console 子进程必须带 `CREATE_NO_WINDOW`。
- 重大/有争议的方案先让 sodus 派 opus + sol 并行评审，再综合；不要单模型拍板。
- 不做 n=1/n=3 的统计结论；效率结论必须来自预注册的配对实验（见 §7）。
- main 必须可回退；每个阶段跑全量 pytest + `scripts/release_check.ps1` 并留证据。
- 预算没有日硬上限；relay 余额是硬约束。模型族软上限：claude/opus ¥100、gpt ¥80、other ¥80（只拦新 run，`--allow-expensive` 可豁免）。
- 不把密钥、token、provider apiKey 写进本文档；只写路径。
- 重启 DSH Web GUI 会中断当前会话，先问用户；重启 sodus daemon 可以直接做。

### 实时状态（2026-11-09 08:00，Checked/Measured）

| 项 | 值 |
|---|---|
| 版本 / tag | `sodus 0.7.2`；main `b08a408`（merge `fcc75af`），tag `v0.7.2` |
| daemon | 0.7.2，pid 159100，单实例监听 4199；`auto_recover=false` |
| 发行门禁 | `scripts/release_check.ps1` PASS，exit 0；全量 pytest 绿 |
| DB | `~/.nodus/nodus.sqlite3`，schema v7（只增表可回退） |
| 预算 | `budget.soft_families={claude:100,gpt:80,other:80}`；无 `daily_cny`/`soft_cny`；今日花费约 ¥112（07:45 读数，重查 `sodus status`） |
| relay | 余额约 ¥105（快照，重查 `sodus status`） |
| 四宿主 | codex / claude / dsh / opencode 均 configured；opencode `mcp list` 显示 `sodus connected`；DSH MCP 已换成 0.7.2 新进程 |
| 并发 | 4 个 run 在不同目录并行实测通过；同目录仍由写锁串行 |
| 未推送 | 所有 tag/commit 只在本地，未 push origin |

## 1. 关键路径索引

| 用途 | 路径 |
|---|---|
| 主仓库 | `A:\VScode\Code\Projects\nodus` |
| 0.6 worktree | `A:\VScode\Code\Projects\nodus-0.6`（feat/sodus-0.6） |
| 0.7 worktree | `A:\VScode\Code\Projects\nodus-0.7`（feat/sodus-0.7） |
| 0.7.1 worktree | `A:\VScode\Code\Projects\nodus-0.7.1`（feat/sodus-0.7.1） |
| 0.7.2 worktree | `A:\VScode\Code\Projects\nodus-0.7.2`（feat/sodus-0.7.2） |
| 交付/交接 | `reports/HANDOFF-0.7.2.md`、`reports/HANDOFF-0.7.1.md`、`reports/HANDOFF-0.7.md` |
| 复查原文 | `reports/review-0.7-opus-20261109.md`、`reports/review-0.7-sol-20261109.md` |
| 0.7.2 实现 | `reports/070f-report.md` |
| 0.7.1 修复 | `reports/070d-report.md`、`reports/070e-report.md` |
| 计划证据 | `reports/plan-M4-M6-20261109.json` |
| OPS 记录 | `reports/OPS_STATUS-0.7.md`、`nodus-0.7/reports/OPS_STATUS.md` |
| 本控制台 | `HANDOFF.md`（仓库根，随代码版本化）；Obsidian 镜像入口见 `B:\Obsidian\sessions\sodus\00_sodus_项目接力控制台.md` |
| 用户配置 | `~/.nodus/config.json`（备份 `config.json.bak-20261109-families`） |
| daemon 日志 | `~/.nodus/daemon.log` |
| token/DB | `~/.nodus/token`、`~/.nodus/nodus.sqlite3`（不要复制内容） |
| 多宿主配置 | `~/.codex/config.toml`、`~/.claude.json`、`A:\dsh\home\cordis.patch.yml`、`~/.config/opencode/opencode.json`（含密钥，勿外传） |
| 文档 | `docs/architecture.md`、`docs/mcp.md`、`docs/cli.md`、`docs/sodus-0.7-plan.md` |

## 2. 决策记录（为什么是现在这样）

- **0.7.0**：MCP 全部改成 daemon HTTP，daemon 是唯一 run 所有者；加只读 `GET /state`；planner 契约 + 机械 validator + `kind='plan'` 只读 run。
- **0.7.1**：正式启用前复查（opus+sol）后修 P1/P2——daemon 独占单实例、auto-recover 改配置默认关、Host 校验、verify 基础设施退出码重试、approve 前复核 HEAD/AGENTS/hash、installer 保留 env/原子写、review 异步+超时。
- **0.7.2**：按用户要求改成模型族软上限（Claude/opus 100、GPT 80、other 80），无日硬上限；补 opencode MCP 安装器；四终端并发文档与测试。
- **预算口径**：用户确认“没有定过预算”；2026-11-05 的 config 备份里没有 `budget` 键，50/20 是后来开发时写进去的保守值，已移除。relay 余额才是硬约束。
- **并发口径**：多个宿主各自拉 MCP 进程，但都只是 HTTP 客户端；daemon 单实例、多线程；不同 cwd 可并行，同一 cwd 写锁串行；SQLite 单写者。
- **交接口径**：sodus 已有 per-run `handoff.md`、`sodus debug sessions/session/brief/lineage/resume/branch`、`debug desk`；缺的是项目级、持续维护、能压缩恢复的“接力控制台”。本文件是该项目的第一份。

## 3. 进度节点

| 日期 | 节点 | 状态 |
|---|---|---|
| 2026-11-09 | v0.7.0 发布：MCP/HTTP、/state、planner | ✅ |
| 2026-11-09 | v0.7.1 发布：启用前 P1/P2 修复、发行门禁 PASS | ✅ |
| 2026-11-09 | v0.7.2 发布：族软上限、opencode、四端并发 | ✅ |
| 2026-11-09 | M4–M6 真实 opus plan + sol review | ⚠️ plan `plan-70cceecf7ca5` = `reviewed`，verdict **revise**，未批准 |
| 待定 | 0.8.0：`sodus handoff` 项目控制台、B3/B4、plan 执行/物化 | ⏳ |
| 待定 | 效率 A/B（12 题 ×10 ×2 组 = 240 对） | ⏳ 设计已定，未跑 |

## 4. 未决与风险

- **B3（高）**：worker 仍在 bypass sandbox 模式跑，plan/review 的只读只靠提示词；worker 继承 daemon 环境变量（未做 env 白名单）。修好前不要无人值守。
- **B4（中高）**：批准身份仍是 `--by human` 声明；同机同用户可读 token/文件，`X-Sodus-Depth` 可伪造。需要独立批准凭据或 TTY 确认。
- **plan**：M4–M6 的 sol review 结论是 revise，三条 finding（episode rowid 复用、补偿队列不触发、approved 候选从看板消失）要在重做计划时吸收；plan 节点执行/物化/预算派发/replan 未实现。
- **DSH 事故**：重启 MCP 进程时原 daemon 165116 无日志死亡，已恢复为 159100；怀疑 DSH Windows Job 清理，正常终端启动不受影响，需继续观察。
- **其他**：DSH YAML 注释保留、DB 自动备份、verify `3221225786` 根因、DSH 面板、效率实验未做。
- **软上限语义**：按已记账成本检查，不预留在途/并发成本；多个 run 同时起跑可能一起越线，relay 余额仍是硬约束。

## 5. 下一步（按顺序）

1. 实现 `sodus handoff` MVP：项目级文件（workspace 根或配置路径）+ `show/append/sync/check`；把 task/run/plan/release/daemon/budget 状态同步进去，人工 curate 的部分不被覆盖。
2. 把 B3/B4 做成 0.8.0 的阻塞项：worker env 白名单、plan/review 只读沙箱/临时 worktree、独立批准身份；完成后才允许无人值守。
3. 按 sol findings 重做 M4–M6 计划并走 approve；实现 plan 节点物化/执行/replan。
4. 预注册并跑效率 A/B（12 题 ×10 ×2 = 240 对；exact McNemar，非劣界 −5pp；成本/时间配对 bootstrap，界 +20%）。
5. 补 DSH 面板（只读）、DB 备份、DSH YAML 注释、verify 根因。

## 6. 会话/交接机制（sodus 已有）

- `sodus debug sessions --limit 20 [--query ...] [--vendor ...]`：扫 Claude/Codex/DSH 三家会话。
- `sodus debug session <id> --tail 40`：看某一个会话原话。
- `sodus debug brief <id> --task "..." --tail 12 --out PATH`：生成跨会话交接（2026-11-09 实测可用，输出含主题/来源/最近对话原话）。
- `sodus debug lineage <id> [--json]`：会话→run→task→proposal 血缘。
- `sodus debug resume/branch`：继续/分叉会话（跨 vendor）。
- `sodus debug desk --out PATH`：把任务进度渲染成 markdown（vault dashboard 用）。

**缺口**：以上是 per-session、一次性快照；项目级控制台需要“持续维护 + 状态同步 + 人工策展 + 快照校验”。这就是 `sodus handoff` 要做的事。

## 7. 命令速查

```powershell
A:\Anaconda\python.exe -m sodus status
A:\Anaconda\python.exe -m sodus runs
A:\Anaconda\python.exe -m sodus debug sessions --query nodus
A:\Anaconda\python.exe -m sodus debug brief <session-id> --task "..." --out "$env:TEMP\brief.md"
A:\Anaconda\python.exe -m sodus plan list
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\release_check.ps1
git -C A:\VScode\Code\Projects\nodus log --oneline -5
```

## 8. 更新规则

- 谁更新：当班会话的 agent；每次发布、阶段结束、关键决策、重大事故后更新。
- 怎么更：只追加/修改对应节；`snapshot_at` 改成当前时间；涉及数字的写清来源（Checked/Measured/User-provided/Unverified）。
- 不复制：长报告、评审原文、日志、密钥只放链接/路径。
- 校验：`git HEAD`、tag、daemon pid/version、config、relay 余额在每次新会话开始时重查。
- 一份活的控制台，不并行维护多份；发现旧结论错了，直接改并留下“原判断/为什么错”。