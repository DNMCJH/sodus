# 0.8.0-b2 brief：B4 批准门禁（CLI TTY + 关闭 HTTP approve）

## 边界
- 工作区 `A:\VScode\Code\Projects\nodus-0.8`（feat/sodus-0.8，包含 0.8.0-a handoff 改动）。**不 git commit / push / merge**。
- 只做 B4：批准入口收紧；不改 handoff、不改 B3（env/readonly 在另一 worktree，稍后合并）。
- 先读 `nodus/plan.py`（approve/validate_plan）、`nodus/server.py`（/plans/{id}/approve 路由）、`nodus/cli/commands/plan.py`、`openapi.yaml`、`docs/cli.md`、`tests/test_plan.py`、`tests/test_openapi.py`。

## 需求
1. **HTTP approve 关闭**：`POST /plans/{id}/approve` 一律返回 403 `{"error":"approve_requires_cli"}`；不再调用 `plan.approve`；openapi 删除该 path（或标 deprecated 403），测试覆盖 403、状态不变。
2. **CLI approve 收紧**：
   - 要求 `sys.stdin.isatty()` 且 `sys.stdout.isatty()`；非 TTY 直接失败并提示“approval requires an interactive terminal”；
   - 若 `NODUS_ROLE=='worker'` 或 `NODUS_RUN=='1'`，拒绝（防 worker 自动化）；
   - 显示 plan 摘要（id、status、review verdict、plan hash 前 12 位）并提示输入 plan id 确认；输入不匹配则取消；
   - 确认后**本地**调用 `plan.approve(plan_id, 'human')`（不再走 HTTP），输出 JSON/文本；保留 `--by human`；
   - 测试：非 TTY 拒绝；TTY+错误确认拒绝；TTY+正确确认成功；HTTP 403 状态不变。
3. **复核**：`plan.approve` 已有 plan hash/HEAD/AGENTS/validator 检查；确认错误信息清晰，不弱化；如发现遗漏补上。
4. **文档/残余风险**：docs/cli.md 写“批准只能交互式 CLI”；docs/architecture.md 安全段明确：同机同用户仍可伪造 TTY/读 token，这不是密码学身份隔离，只是提高无人值守/worker 自动批准的难度；不假装已解决。
5. **测试/报告**：`reports/080b2-report.md` 记录实现、测试、残余风险、SUMMARY。

## 验收
- 全量 `A:\Anaconda\python.exe -m pytest -q` 退出码 0；
- 非交互 `sodus plan approve` 实测失败；HTTP approve 实测 403；
- 080b2 报告存在。
