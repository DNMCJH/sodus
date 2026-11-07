# HANDOFF 0.6

## SUMMARY

W4 已完成并提交到 `feat/sodus-0.6`；main 仍是 0.5.1，未合并。合并前建议先 review 本分支。

版本已统一为 0.6.0（pyproject、包版本、OpenAPI、兼容测试）。新增 `sodus debug memory-stats` 和临时 DB 测试；新增 release_check、README、CHANGELOG、A/B 功效与 W4 报告。

运行：`A:\Anaconda\python.exe -m pytest -q`；`powershell -ExecutionPolicy Bypass -File scripts/release_check.ps1`；`sodus debug memory-stats --json`。release_check 构建 wheel/sdist、隔离安装、验证两个入口、doctor 和 MCP initialize/tools-list。

已知缺口：worker 直接 HTTP 仍可能绕过深度头；rollback 只记录后版本；每日 dream 失败会占用当日触发窗口；rot 的近重复目前报告为 0（候选表没有相似度索引），只统计 hash 冲突。合并 main 建议先 review，再 `merge --no-ff`；回滚用 revert 本次文件变更并重新运行版本/测试门禁。

## 验证证据

- `A:\Anaconda\python.exe -m pytest -q`：退出码 0（进度 100%，无失败输出）。
- `powershell -ExecutionPolicy Bypass -File scripts/release_check.ps1`：PASS，退出码 0；构建 `nodus-0.6.0` wheel+sdist，隔离 venv 内 `sodus/nodus --version` 均为 0.6.0，doctor 的 home/database/agents 检查 PASS，MCP serverInfo `sodus 0.6.0`、tools/list ≥10。
- 版本一致：pyproject / `nodus.__version__` / openapi.info.version / `tests/test_sodus_compat.py` 全部 0.6.0。
- 分支范围：`git log --oneline v0.5.1..feat/sodus-0.6`。
- 检查点：W3c 首轮全量 1006 tests、0 failures/errors/skips、退出码 0；W4 目标测试与 release_check 另见各自报告。
