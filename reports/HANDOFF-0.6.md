# HANDOFF 0.6

## SUMMARY

sodus 0.6.0 已在 `feat/sodus-0.6` 分支完成并经过一轮 opus 评审 + 修复；main 仍是 0.5.1、未合并。合并前建议你过一遍 `reports/opus-review-0.6.md` 与下面的遗留清单，然后决定是按现状合并，还是先补 0.6.1 项。

## 这版做了什么

- **单入口**：`POST /ask` / `GET /ask/{id}` / `POST /ask/{id}/promote`，MCP `sodus_ask`/`sodus_ask_status`（+ `nodus_ask*` 别名）。ask 表带 request_id 幂等、409 冲突、24h TTL/410；回答走 `kind='ask'` run，git 仓库用临时 worktree 不改主树，worker 侧递归拒绝。
- **记忆层**：schema v2–v5（`memory_episode` / `memory_candidate` / `memory_exposure` / `candidate_evidence` / FTS5 trigram），episode 自动落库 + 历史回放；`sodus dream --dry-run`；dream plan/extract/count（kind='dream' run、便宜路由、日 token 上限）；L3 `AGENTS.md` marker + 漂移检测 + journal/staging/原子替换 + 启动恢复 + `sodus memory rollback --to`；审核 API `GET/POST /memory/candidates`（CAS 409、edit provenance）与看板动作；daemon 每日 dream 触发。
- **派生状态**：`lifecycle.task_state/ask_state` 按 ADR-020 派生；`task.accepted_by`。
- **发行工具**：版本 0.6.0、CHANGELOG/README、`sodus debug memory-stats`（rot 指标）、`scripts/release_check.ps1`（构建、隔离安装、doctor、MCP、0.5.1 DB 升级冒烟、全量 pytest、每步退出码检查）、A/B 功效报告、各阶段报告。

## 评审与修复

opus 评审结论「修完再合并」。已修并补回归测试：

- **H1** dream 输出解析：支持 ```json 围栏、校验 schema；非法输出 → batch `failed`，同区间可重新 plan/重试，不再永久卡死。
- **H2** `recover_journals` 三种崩溃分支（before/after/第三方改动）测试，每种重复两次验证幂等。
- **M1** ask 在 run 终态（failed/stalled）时返回 failed，不再卡 running。
- **M2** 看板合并 staged dream 候选与 0.5.1 harvest 候选，不再互相顶掉。
- **M3** `GET /memory/candidates` 默认 workspace 与 evidence_count 口径统一到 candidate_evidence。
- **发行门禁** release_check 已修：`Invoke-Checked` 参数名 `$Args` 与自动变量冲突（会开裸 python REPL 挂住）、python -c 引号、SODUS_HOME 泄漏进 pytest、counts 按行输出；现 PASS。

## 已知缺口（未修，建议 0.6.1）

- worker 直接带 token 打 HTTP 且不带深度头时，HTTP 侧拦不住（MCP 侧会拒绝）。
- rollback 只回放记录之后的版本；`reindex` 吸收的手改内容会在 rollback 时被丢弃。
- 每日 dream 只覆盖 daemon 所在 workspace；失败尝试也占用当日触发窗口；审核通过的候选最晚隔天落盘。
- 非 git 目录的 ask 可以写文件，只会被标记 `wrote_files`；git 目录的临时 worktree 补丁直接丢弃。
- 手改 facts 区块后旧的 adopt/harvest 会报错（M4）；run 事后 verify 转绿不会进 dream 证据集（M6）；rot 的 near-duplicate 恒为 0（只统计 hash 冲突）。
- 评审低危：哈希口径（原文 sha256 vs `_norm`）、edit 两事务可能留孤儿候选、`ask_sweep` 无调用者、`ask_promote` 的 route 只进 goal 文本。

## 合并与回滚

- 合并：review 后 `git checkout main && git merge --no-ff feat/sodus-0.6`，然后跑 `scripts/release_check.ps1` 与全量 pytest；不要直接 fast-forward 掉评审记录。
- 回滚：对合并 commit `git revert -m 1 <merge>`，或回到 `v0.5.1` 重新跑 release_check；`~/.nodus` 数据库由 0.6.0 启动时迁移到 v5，0.5.1 代码仍能读（只增表，不删列），但回退前建议备份 `~/.nodus/nodus.sqlite3`。

## 验证证据

- `A:\Anaconda\python.exe -m pytest -q`：退出码 0（1017 tests collected，全量通过，含本次新增回归测试）。
- `powershell -ExecutionPolicy Bypass -File scripts/release_check.ps1`：**PASS，退出码 0**（最终版）；构建 `nodus-0.6.0` wheel+sdist，隔离 venv 两入口 0.6.0，doctor home/database/agents PASS，MCP serverInfo `sodus 0.6.0` tools/list ≥10，0.5.1 DB 复制升级到 schema v5 且 task/run 行数不变（源库只读）。
- 版本一致：pyproject / `nodus.__version__` / openapi.info.version / `tests/test_sodus_compat.py` = 0.6.0。
- 分支：`git log --oneline v0.5.1..feat/sodus-0.6`；关键提交：设计 `9fbb308`、W1a `7612fa6`、W1b `c6a6324`、W1c `f18ac3d`、W2a `dada068`、W2b `b85b5ad`、W3a `3960d1c`、W3b `6431a25`、W3c `b333e41`、0.6.0 发行 `181f5fd`、评审修复 `5783bc5`。
- 各阶段证据：`reports/W1a..W4-report.md`、`reports/opus-review-0.6.md`、`reports/FIX-report.md`、`reports/FIX-tests-report.md`、`reports/2026-11-08_ab-power.md`。
