# Opus review：sodus 0.7.0（8c01ff2 vs a6acd0c）

**结论：需修后合并**。所有权基本收口（MCP/CLI 只走 HTTP，`RUNNER.start` 仅在 daemon 进程内调用）；/state 鉴权正确。阻塞项是发行与 planner 生命周期。

## 阻塞（合并前修）
1. **mcp install 写死解释器** `cli/commands/mcp.py: COMMAND = A:\Anaconda\python.exe`：别的机器/venv 装出坏配置。修：`sys.executable`。测：断言写入 command==sys.executable。
2. **plan prompt 依赖仓库 docs** `plan.create` 读 `docs/sodus-0.7-plan.md`，wheel 不含 docs → OSError，且发生在 try 之前，plan 永久卡 `draft`。修：契约放包内资源，读失败写 failed。测：删/改路径后 create→failed。
3. **planning 永不终止** `_wait_run` 是 `while True`，`RUNNER.wait` 超时后仍 running 会无限循环；daemon 重启后后台线程丢失，plan 永远 `planning`；CLI `plan new --wait` 无超时轮询。修：总超时→failed；daemon 启动把 planning 且 run 已终止/孤儿的 plan 置 failed；CLI 加 deadline。测：fake run 永 running、模拟重启。

## 中
4. **/plan 绕过递归防护** server `/plan`、`/plans/*/review` 不查 `X-Sodus-Depth`/`NODUS_ROLE`，worker 可经 CLI/HTTP 起 planner。修：同 /ask 拒绝 depth>0。测：带 depth 头→403。
5. **review 同步占 handler 线程**，无超时，路由写死 `sol`。修：同 create 走 202+后台，或至少加超时。
6. **validator 漏检**：base_sha 只查可解析，不比对 HEAD 漂移；`agents_md` 哈希不核对真实文件；`is_command` 接受 "tests pass" 类空话（首词匹配正则即过）；路径不查可写。修：漂移给 warning、核对 sha256、acceptance 首词白名单。
7. **mcp install**：drift 时整条覆盖，丢用户手写的 `env`（如 SODUS_HOME）；默认 all hosts 且无 DSH_HOME 时 `--check`/install 退出 1；claude/DSH 整文件重序列化（DSH 丢注释，`.claude.json` 与 Claude Code 并发写有竞态）；写入非原子。修：保留未知键、dsh 缺失记 skip 不算失败、tmp+rename。

## 低
8. `state.snapshot` 版本写死 `"0.7.0"`；`unverifiable` 是 0/1 而非计数（需确认与 board 同义）；spend 部分每次重算不走缓存。
9. 文档 `sodus-0.7-plan.md` 首段生命周期写 running→done/aborted，与实现不一致。
10. approve 不绑定被审 hash（当前 plan_json 不可变，暂安全）。

## 已知缺口标注
只读无沙箱、by=human 仅声明、重启不恢复、import 直写 SQLite：已在报告/CHANGELOG 中如实说明；但第 3 条"重启后卡死、CLI 死等"的后果没写明，第 1、2 条没提。

## SUMMARY
需修后合并：先修 1–3（解释器写死、docs 未打包、planning 无超时/重启卡死），4 建议同版修；5–7 可放 0.7.1。所有权和 /state 鉴权通过；未改代码、未 commit。
