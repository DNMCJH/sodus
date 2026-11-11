# Opus review brief：feat/sodus-0.7（0.7.0）vs main（a6acd0c）

## 任务
只读评审 `A:\VScode\Code\Projects\nodus-0.7` 分支 `feat/sodus-0.7`（HEAD 8c01ff2，tag 未打）相对 main（a6acd0c）的 0.7.0 改动；**不改代码、不 commit**。

## 输入
- `git -C A:\VScode\Code\Projects\nodus-0.7 log --oneline a6acd0c..HEAD`；
- `docs/sodus-0.7-plan.md`、`docs/architecture.md`、`docs/mcp.md`、`docs/cli.md`、`reports/070a-report.md`、`070b-report.md`、`070c1-report.md`、`070c2a-report.md`、`070c2b-report.md`；本轮目标（brief）：MCP 全转 HTTP + host installer、/state + 展示层外移、plan 契约/validator/planner run/review/approve。

## 重点
1. **所有权**：是否仍有适配层/CLI/MCP 直接 `runner.RUNNER.start` 或直接写 run 表（尤其 `nodus/plan.py` 的 daemon 后台线程、`server.py` 的 /plan、`nodus/cli/commands/plan.py`、`nodus/mcp.py`）；daemon 唯一所有者是否成立。
2. **/state**：鉴权、字段契约、缓存一致性（running/asks 新鲜 vs counts/today 缓存）、p95 270ms 测试是否可信；board 输出是否真不变。
3. **planner**：plan 状态机（planning→validated/failed→reviewed→approved）是否完整；validator 漏检（路径存在但不可写、base_sha 漂移、acceptance 空话、node 无 milestone、哈希）；最后一条 assistant message 抽取/围栏解析的鲁棒性（多消息、非法 JSON、超长）；review/approve 门禁（`by=human` 只是声明）；HTTP `/plan` 后台线程在 daemon 重启后的行为；plan 只读无法强制；超时/预算。
4. **mcp install**：幂等、备份、损坏配置不写、`--check` 退出码、DSH YAML insert 语义、codex/claude 配置格式、跨平台路径、卸载只删自己；对本机已手工写入条目的兼容。
5. **发行**：版本一致、openapi/测试、release_check、文档准确性；哪些必须合并前修，哪些可列 0.7.1。
6. **安全**：新端点 token、path traversal（plan file_scope/cwd）、prompt 注入、递归防护是否被 plan 绕过。

## 输出
写到 `reports/opus-review-0.7.md`，中文 ≤800 字：结论（可合并/需修）→ 按严重度列问题（文件/函数、最小修法、必须补的测试）→ 已知缺口是否诚实标注 → SUMMARY。只读仓库。

