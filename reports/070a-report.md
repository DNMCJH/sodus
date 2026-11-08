# 0.7.0-a 实施报告

## 实现

- `nodus/mcp.py` 的任务、运行、状态、提案、成本、记忆工具全部经 `nodus.cli.client.call` 访问 daemon HTTP；移除 runner/db/memory/ledger 进程内路径。`wait=true` 轮询 `/runs/{id}`，并报告 daemon 不可用提示。
- daemon 新增 `/cost/today`、`/memory/remember`、`/memory/harvest`，并在 OpenAPI 中声明。
- 新增 `sodus mcp install`，支持 codex/claude/dsh、check/uninstall/dry-run、幂等合并、损坏配置保护和时间戳备份。
- 更新 MCP/CLI 文档；补充 HTTP 和 installer 测试。
- `/runs/{id}` 增加 `mcp` 兼容状态对象（含 usage、verify、handoff）；原有 HTTP 字段保留，未知 run 返回 404。
- 原有 MCP 数据行为测试改为隔离的真实本地 HTTP 服务，不连接当前运行 daemon；新增 client.call monkeypatch 映射、wait 超时和 daemon 错误提示测试。
- installer 测试覆盖三宿主首次安装、零 diff 重复安装、备份、dry-run、check、卸载、其他 MCP 条目保留、坏配置拒写以及 TOML 数组表保留。

## 测试证据

- `A:\Anaconda\python.exe -m pytest -q`：最终全量完成至 100%，退出码 **0**；日志 `reports/070a-pytest.log` 末尾为 `PYTEST_EXIT_CODE=0`。
- `A:\Anaconda\python.exe -m sodus mcp install --check`：可运行；当前 codex/claude configured，DSH missing 时返回 1；日志 `reports/070a-install-check.log`。临时三宿主全齐的 check 退出 0 已由 installer 测试覆盖。
- `rg 'runner|RUNNER|db\.' nodus/mcp.py`：无结果。

- `git diff --check`：退出 0（仅 CRLF/LF 提示）。

## 遗留

当前真实宿主 check 未写配置：codex/claude configured，`A:\dsh\home\cordis.patch.yml` 的 Sodus 条目 missing，故退出 1（符合全齐才为 0 的约定）。没有执行真实宿主 install/uninstall；测试均使用临时配置。DSH YAML 编辑需要可选 PyYAML，缺少时明确报错且不写入。

未执行 git commit/push/merge；保留原有 ops_monitor.log、reports/OPS_STATUS.md 与任务简报。

SUMMARY: 0.7.0-a 完成；MCP 已完全 HTTP 化且无 runner/RUNNER 引用，daemon 为唯一运行所有者；宿主安装器、服务端端点、OpenAPI、文档和测试已补齐，全量 pytest 退出 0。下一位可审查工作区 diff，若需真实 DSH 接入则执行对应 host install；未提交 git commit。
