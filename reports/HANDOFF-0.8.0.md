# HANDOFF 0.8.0（2026-11-09）

## SUMMARY

0.8.0 已发布：main merge `3eef7fb`、tag `v0.8.0`，daemon 0.8.0（pid 173512，单实例）。三块交付：项目级 `sodus handoff`、B3 worker 环境/只读隔离、B4 批准门禁。`release_check.ps1` PASS，exit 0。

## 交付

- **项目级 handoff**：解析项目根（git root 优先），默认 `HANDOFF.md`（config `handoff.path` 可覆盖）；`init/show/append/sync/check/list`；`sync` 只更新 `<!-- sodus:auto:start/end -->` 机器段；`check` 校验 snapshot/HEAD/关键路径/daemon 版本；HTTP `GET /handoff`、`GET /handoffs` 与 MCP `nodus_handoff_show/list/check` 只读。单项目一份，不做内容聚合。
- **B3**：`agents.build_env` 改白名单，不再继承 daemon 全部环境；plan/plan_review 使用只读参数（codex `--sandbox read-only`、claude `--permission-mode plan`），并在临时 git worktree/过滤副本中运行，结束清理；隔离失败回退原 cwd 并记 warning。
- **B4**：HTTP `POST /plans/{id}/approve` 一律 403；`sodus plan approve` 仅在交互式 TTY 中显示摘要并要求输入 plan id 确认，拒绝 `NODUS_ROLE=worker`/`NODUS_RUN=1`；确认后本地调用 `plan.approve`，保留 plan hash/HEAD/AGENTS/validator 复核。

## 验证

- `scripts/release_check.ps1`：PASS，exit 0（构建 wheel/sdist、隔离安装 0.8.0、doctor、MCP、DB 升级、全量 pytest）。
- 全量 pytest：0.8 分支最终 1179 项通过；handoff 目标 96 项、B1/B2 定向测试通过。
- 真实 smoke：0.8.0 daemon 下 sol、dsh、claude、opencode-glm 各跑一个简单任务，均 completed（约 ¥0.006 / ¥0.07 / ¥0.113 / ¥0.173），证明 env 白名单没有切断四种 worker 的认证。
- `sodus handoff show --json` 能正确解析仓库控制台；`check` 对尚无 auto 标记的手工控制台会报 `head_mismatch` / `daemon_unavailable`，这是预期的迁移提示（见遗留）。
- `sodus mcp install --check`：四宿主 configured，exit 0。

## 遗留/风险

- **B3 残余**：无 OS 级沙箱；同用户仍可读 token/凭据；DSH/opencode 无原生只读开关，隔离失败回退；git worktree 是 HEAD 快照，不含未提交修改。
- **B4 残余**：同用户可伪造 TTY、去掉环境标记或直连 SQLite；这是提高门槛，不是密码学身份隔离。
- **现有手工控制台迁移**：`sync` 要求恰好一对有序 auto 标记；已有 `HANDOFF.md` 需手工加标记后 `sync`。仓库自己的控制台目前仍是手工维护（未加标记）。
- **测试抖动**：全量/发行检查期间见到 Windows `ConnectionAbortedError 10053`（loopback HTTP 测试），复跑通过；根因未查。
- **plan/效率**：M4–M6 仍是 review=revise；plan 执行/物化/replan 未做；效率 A/B 未跑。
- **其他**：DSH YAML 注释、DB 自动备份、verify 3221225786 根因、DSH 面板未做。

## 回滚

- `git revert -m1 3eef7fb`（或回 `v0.7.2`），重启 daemon；DB schema v7 未变（0.8.0 未加表）。
- 旧控制台/报告在 `reports/HANDOFF-0.7.2.md`；0.8 各任务报告 `reports/080a-report.md`、`080b1-report.md`、`080b2-report.md`。