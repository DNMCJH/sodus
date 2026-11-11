---
title: 'sodus 项目接力控制台'
date: '2026-11-09'
updated: '2026-11-12'
version: 3.1.0
type: project-handoff
status: active
snapshot_at: '2026-11-12 02:35:00 +08:00'
tags:
  - workflow/handoff
  - sodus
---

# sodus 项目接力控制台

> [!abstract] 唯一接力入口
> 本文件是 sodus 仓库的权威项目控制台（A:\VScode\Code\Projects\sodus\HANDOFF.md）。新会话先完整读取本文件，再刷新“实时状态”，然后继续。sodus 项目上下文极大，不要试图从聊天记录重建决策和路径。详细内容通过索引跳转，不在这里重复。

> [!warning] 快照边界
> 这是 2026-11-12 02:35 的快照，Markdown 不自动同步。snapshot_at 之后的变化必须重新核验：git HEAD、tag、daemon 版本/pid、预算、relay 余额、宿主配置。

## 0. 三分钟启动区

### 项目一句话

Sodus 是给 agent 用的任务验收层：单机 daemon（默认 127.0.0.1:4199）+ CLI + MCP over stdio + 只读看板；核心是把“派活、验证、评审、记忆、计划、项目接力”收在一个 HTTP 契约下，让 Claude Code / Codex / opencode / DSH 只当薄客户端。0.9.0 起产品、包、CLI、状态目录、MCP 工具名只有 sodus 一个名字。

### 当前唯一优先

> [!todo]
> **0.9.0 已发布（2026-11-12）。** 更名、migrate-home、MCP sodus_*、Claude Code 冒烟、全量 pytest、release_check、CI（Python 3.11/3.12/3.13）全部通过；GitHub Release v0.9.0 已发布。接下来：① 0.8.2 路由收敛：route_meta 持久化、runner requirements 硬校验、difficulty 1–10；② 12-01 前复查 Carbon promotion，argon 回池排序；③ M4–M6 计划仍待人工 TTY 批准；④ 收尾：DSH 宠物插件命名、旧 ~/.nodus 归档。

### 硬规则（用户明确要求，改之前先问）

- 跑 sodus 不要弹终端窗口；console 子进程必须带 CREATE_NO_WINDOW。
- 重大/有争议的方案先让 sodus 派 opus + sol 并行评审，再综合；不要单模型拍板。
- 不做 n=1/n=3 的统计结论；效率结论必须来自预注册的配对实验（见 §5）。
- main 必须可回退；每个阶段跑全量 pytest + scripts/release_check.ps1 并留证据。
- 预算没有日硬上限；relay 余额是硬约束。模型族软上限：claude/opus ¥100、gpt ¥80、other ¥80（只拦新 run，--allow-expensive 可豁免）。
- 不把密钥、token、provider apiKey 写进本文档；只写路径。
- 重启 DSH Web GUI 会中断当前会话，先问用户；重启 sodus daemon 可以直接做。

### 实时状态（2026-11-11 23:40，Checked/Measured）

| 项 | 值 |
|---|---|
| 版本 / tag | sodus 0.9.0；main 3b3d641（pyyaml 修复）+ 本控制台提交；tag v0.9.0 指向 release 提交 |
| daemon | 0.9.0，pid 56388，单实例监听 4199，/health ok |
| 发行门禁 | release_check.ps1 PASS（构建/干净 venv 安装/doctor/MCP 握手/全量 pytest，日志 reports/release-0.9.0.log）；全量 pytest 1458 项 EXIT=0（2026-11-12） |
| CI | GitHub Actions windows-latest × Python 3.11/3.12/3.13 全绿（run 43077407300） |
| Claude Code 冒烟 | sodus run --route claude 成功：run-8745eebea739，输出 CLAUDE_OK，exit=0，cost=¥0.178631(calibrated) |
| DB | ~/.sodus/sodus.sqlite3，schema v8，tasks=333 runs=574（migrate-home 自 ~/.nodus，源未动） |
| 预算 | budget.soft_families={claude:100,gpt:80,other:80}；无 daily_cny/soft_cny；今日花费与 relay 余额用 sodus status 重查 |
| 四宿主 | codex / claude / dsh / opencode 均 configured；MCP 工具 12 个，全部 sodus_*；serverInfo.name=sodus 0.9.0 |
| 并发 | 4 个 run 在不同目录并行实测通过；同目录仍由写锁串行 |
| 交接台 | 控制台 3.0.0；sodus handoff sync/check 可用；检查前先 sync 一次刷新 HEAD/daemon |
| 未推送 | 无：main、tag v0.9.0 与 GitHub Release 已推送到 DNMCJH/sodus |

## 1. 关键路径索引

| 用途 | 路径 |
|---|---|
| 主仓库 | A:\VScode\Code\Projects\sodus |
| 旧版 worktree（已删） | 9 个 feat worktree 于 2026-11-11 删除（分支全部并入 main），备份 A:\VScode\Code\Projects\_archive\nodus-worktrees-20261111.zip |
| 路由元数据 | sodus/route_meta.py、sodus/policy.py、sodus/decision.py、tests/test_policy.py |
| 改名迁移 | docs/sodus-migration.md（从 nodus 到 sodus 0.9.0 的步骤与回滚） |
| 0.8 交付 | reports/080a-report.md、reports/080b1-report.md、reports/080b2-report.md、reports/080c-report.md、reports/080d-report.md |
| 旧版交接 | reports/HANDOFF-0.7.2.md、reports/HANDOFF-0.7.1.md、reports/HANDOFF-0.7.md |
| 复查原文 | reports/review-0.7-opus-20261109.md、reports/review-0.7-sol-20261109.md |
| 计划证据 | reports/plan-M4-M6-20261110-v5.json（plan-9d7318c01882，reviewed，verdict approve，待人工批准）；reports/plan-review-M4-M6-20261110.json |
| 本控制台 | HANDOFF.md（仓库根，随代码版本化）；Obsidian 入口 B:\Obsidian\sessions\sodus\00_sodus_项目接力控制台.md |
| 用户配置 | ~/.sodus/config.json（旧 ~/.nodus 保留作回滚；备份 config.json.bak-20261109-families） |
| daemon 日志 | ~/.sodus/daemon.log |
| token/DB | ~/.sodus/token、~/.sodus/sodus.sqlite3（不要复制内容） |
| 多宿主配置 | ~/.codex/config.toml、~/.claude.json、A:\dsh\home\cordis.patch.yml、~/.config/opencode/opencode.json（含密钥，勿外传） |
| 文档 | docs/architecture.md、docs/mcp.md、docs/cli.md |

## 2. 决策记录（为什么是现在这样）

- **0.7.0**：MCP 全部改成 daemon HTTP，daemon 是唯一 run 所有者；加只读 GET /state；planner 契约 + 机械 validator + kind='plan' 只读 run。
- **0.7.1**：正式启用前复查（opus+sol）后修 P1/P2——daemon 独占单实例、auto-recover 改配置默认关、Host 校验、verify 基础设施退出码重试、approve 前复核 HEAD/AGENTS/hash、installer 保留 env/原子写、review 异步+超时。
- **0.7.2**：按用户要求改成模型族软上限（Claude/opus 100、GPT 80、other 80），无日硬上限；补 opencode MCP 安装器；四终端并发文档与测试。
- **0.8.0**：项目级 sodus handoff（单项目一份 HANDOFF.md，工作区只放索引）；B3 env 白名单 + plan/review 只读 + 临时 worktree；B4 关闭 HTTP approve，CLI 仅 TTY 确认。
- **0.8.1**：plan 物化/执行/replan（schema v8）；handoff sync 自动迁移旧控制台、check warnings；修 HTTP 未授权 POST 残留 body 协议缺陷；午夜边界测试；codex 沙箱在 Windows worktree 启动失败→隔离 cwd 回退。
- **2026-11-11 路由建模**：claude 是 CLI/agent，不是模型；opus/fable/sonnet 是模型，-high/-xhigh/-max 只是 effort。自动分配只从 canonical 模型路由里选，effort 是独立属性；claude 和 effort alias 不参与候选。policy/decision 的候选列表改为从 ~/.sodus/routes.json 动态加载，不再硬编码。
- **2026-11-11 Carbon 首发价**：官方 2026-11-30 前 $2/M 输入、$10/M 输出，12-01 起 $4/$20。promotion 窗口内 carbon cost_tier 降为 2，prefer_over=[argon] 通过 dated-promotion guardrail 把 argon 移出候选池；这是“carbon 优先于 argon”，不是压过所有路线。
- **2026-11-11 0.9.0 全面更名**：删掉 nodus 双入口和 sodus 入口壳，nodus 包整体改名为 sodus；默认状态目录 ~/.sodus、数据库 sodus.sqlite3；MCP 工具名全部 sodus_*；环境变量 NODUS_* → SODUS_*；migrate-home 只复制 ~/.nodus 并把数据库改名，源目录不动。理由：用户明确要求全部替换；保留 ~/.nodus 仅作回滚来源。
- **预算口径**：用户确认“没有定过预算”；relay 余额才是硬约束；族软上限只拦新 run。
- **并发口径**：多个宿主各自拉 MCP 进程，但都只是 HTTP 客户端；daemon 单实例、多线程；不同 cwd 可并行，同一 cwd 写锁串行；SQLite 单写者。
- **交接口径**：per-run handoff、session brief 都是单次快照；项目控制台是权威恢复入口，sodus handoff 只操作当前项目，不聚合内容。

## 3. 进度节点

| 日期 | 节点 | 状态 |
|---|---|---|
| 2026-11-09 | v0.7.0 / v0.7.1 / v0.7.2 发布 | ✅ |
| 2026-11-09 | v0.8.0 发布：项目 handoff、B3 隔离、B4 CLI 批准 | ✅ |
| 2026-11-10 | M4–M6 重做计划 + 多轮 sol review：plan-9d7318c01882 reviewed，verdict approve，待人工 TTY 批准 | ✅ |
| 2026-11-10 | v0.8.1 发布：plan 物化/执行/replan、handoff 迁移 | ✅ |
| 2026-11-11 | 路由建模 + 动态路由元数据 + Carbon 首发价 guardrail（commits 96b0fb3/48fc33c/eb8db36/375722f） | ✅ |
| 2026-11-11 | **v0.9.0 全面更名**：包/CLI/MCP 工具/状态目录/数据库/仓库目录统一 sodus；migrate-home 迁移 333 tasks / 574 runs | ✅ |
| 2026-11-11 | v0.9.0 push + tag 完成（GitHub DNMCJH/sodus） | ✅ |
| 2026-11-12 | 0.9.0 release_check PASS + CI 全绿 + Claude Code 冒烟 + GitHub Release v0.9.0 | ✅ |
| 待定 | 宿主重启确认 sodus_* 工具（DSH/Claude/Codex/opencode） | ⏳ |
| 待定 | 0.8.2：route_meta 持久化、runner requirements 硬校验、difficulty 1–10 | ⏳ |
| 待定 | 效率 A/B（12 题 ×10 ×2 组 = 240 对） | ⏳ 设计已定，未跑 |

## 4. 未决与风险

- **B3 残余**：env 白名单 + 只读参数 + 临时 worktree 已做；但没有 OS 级沙箱，同用户仍可读 token/凭据；DSH/opencode 无原生只读开关，隔离失败会回退原 cwd 并记 warning；git worktree 是 HEAD 快照，不含未提交修改。
- **B4 残余**：HTTP approve 已关闭，CLI 仅交互式 TTY 确认并拒绝 worker 标记；但同用户可伪造 TTY、去掉环境标记或直连 SQLite，这不是身份隔离，只是提高无人值守自动批准的门槛。
- **plan**：M4–M6 计划已按 finding 重做并获 sol approve（plan-9d7318c01882），但人工批准不能代办；执行功能 0.8.1 已实现，尚未执行该计划。
- **B3 codex 回退**：Windows 下 codex sandbox helper 在 sodus 临时 worktree 启动失败（ShellExecuteExW 1223），已改为隔离 cwd + 正常 headless flags；这削弱了“只读”保证，主树仍由临时 cwd 隔离。
- **测试抖动**：0.8.0 期间全量/发行检查出现过 Windows ConnectionAbortedError 10053（loopback HTTP 测试），复跑通过；根因未查。
- **DSH 事故**：历史 daemon 在杀 MCP 进程时无日志退出，已多次恢复；当前 daemon 56388，继续观察。
- **CI 依赖**：0.9.0 首次干净环境 CI 暴露 DSH YAML 合并缺 PyYAML；已把 pyyaml 加入运行时依赖并修复（3b3d641）。以后新增可选宿主依赖要先在干净 venv 验证。
- **Carbon 促销到期**：2026-12-01 起 $4/$20，promotion 自动失效；要复查 argon 回池后的排序和测试是否仍绿。
- **路由元数据未持久化**：route_meta 只在代码里，routes.json 自定义路由的 load/save 不携带能力/价格/requirements；runner 也没有对 requirements 硬校验或在 provider fallback 时重校验。
- **Jev 依赖**：官方 key 已停用，DSH jev MCP 走 Netlify 函数代理（https://dnmcjh.netlify.app，key 在 %APPDATA%\jev-netlify-mcp\config.env）；代理挂了 Jev 不可用，policy 本身 fail-open。
- **Agent Reach**：1.6.1 已装（C:\Users\35336\.local\bin\agent-reach.exe），旧 1.5 venv 改名备份；微信文章走 JustOneAPI，不走 Jina。
- **旧 ~/.nodus 保留**：迁移后源目录 1.2GB 未删，作回滚来源；确认稳定后可删除或归档。
- **DSH 宠物插件仍是 nodus-pet**：A:\dsh\home\nodus-pet 是本地 DSH 插件包，配置与脚本内仍用 nodus 命名；产品代码已不依赖它，是否改名待确认。

## 5. 下一步（按顺序）

1. 0.8.2 路由：route_meta 持久化到 routes.json/Route（load/save 往返），runner 对 requirements 硬校验 + provider fallback 重校验，difficulty 1–5→1–10（带迁移和测试）。
2. 12-01 前复查 Carbon promotion：argon 回池排序、测试仍绿、价格改 $4/$20。
3. M4–M6 计划仍待人工 TTY 批准（plan-9d7318c01882）。
4. 收尾：DSH 宠物插件 nodus-pet 是否改名；旧 ~/.nodus 1.2GB 归档/删除；宿主重启后工具名核对。

## 6. 会话/交接机制

- sodus handoff init/show/append/sync/check/list：项目级控制台；sync 只更新 auto 标记之间，check 校验 snapshot/HEAD/路径/daemon 版本。
- sodus debug sessions --limit 20 [--query ...] [--vendor ...]：扫 Claude/Codex/DSH 三家会话。
- sodus debug session <id> --tail 40：看某一个会话原话。
- sodus debug brief <id> --task "..." --tail 12 --out PATH：生成跨会话交接。
- sodus debug lineage <id> [--json]：会话→run→task→proposal 血缘。
- sodus debug resume/branch：继续/分叉会话（跨 vendor）。
- sodus debug desk --out PATH：把任务进度渲染成 markdown（vault dashboard 用）。

## 7. 命令速查

```powershell
A:\Anaconda\python.exe -m sodus status
A:\Anaconda\python.exe -m sodus route-for --task "..." --json
A:\Anaconda\python.exe -m sodus handoff sync
A:\Anaconda\python.exe -m sodus handoff check --json
A:\Anaconda\python.exe -m sodus debug sessions --query sodus
A:\Anaconda\python.exe -m sodus plan list
A:\Anaconda\python.exe -m sodus plan approve plan-9d7318c01882 --by human
A:\Anaconda\python.exe -m sodus plan execute plan-9d7318c01882 --wait
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\release_check.ps1
git -C A:\VScode\Code\Projects\sodus log --oneline -5
```

## 8. 更新规则

- 谁更新：当班会话的 agent；每次发布、阶段结束、关键决策、重大事故后更新。
- 怎么更：只追加/修改对应节；snapshot_at 改成当前时间；涉及数字的写清来源（Checked/Measured/User-provided/Unverified）。
- 不复制：长报告、评审原文、日志、密钥只放链接/路径。
- 校验：git HEAD、tag、daemon pid/version、config、relay 余额在每次新会话开始时重查。
- 一份活的控制台，不并行维护多份；发现旧结论错了，直接改并留下“原判断/为什么错”。

<!-- sodus:auto:start -->
head: 3b3d641ec1b907709ebd69722b4788c0fa79a74a
```json
{
  "head": "3b3d641ec1b907709ebd69722b4788c0fa79a74a",
  "branch": "main",
  "tag": "v0.9.0-2-g3b3d641",
  "daemon": {
    "version": "0.9.0",
    "pid": 56388
  },
  "tasks": [],
  "runs": [
    {
      "id": "run-9c9ac8b527f1",
      "status": "completed",
      "kind": "work",
      "task_id": "task-unv"
    },
    {
      "id": "run-d9f01cf6e471",
      "status": "completed",
      "kind": "work",
      "task_id": "task-unv"
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
