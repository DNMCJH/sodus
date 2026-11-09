# 0.8.0-a brief：`sodus handoff` 项目级控制台 MVP

## 边界
- 工作区 `A:\VScode\Code\Projects\nodus-0.8`（feat/sodus-0.8，基于 main c27dc25）。**不 git commit / push / merge**。
- 只做本项目 handoff 的 MVP；不做内容聚合、不做自动 hook、不做 B3/B4。
- 先读仓库根 `HANDOFF.md`、`reports/HANDOFF-0.7.2.md`、`nodus/cli/` 现有命令模式、`nodus/server.py` 路由、`nodus/mcp.py` 工具列表、`tests/` 相关测试。

## 需求
1. **项目级控制台文件**：当前 workspace 解析出项目根（git root 优先，否则 cwd）；默认控制台 `HANDOFF.md`，可由 config `handoff.path` 覆盖（相对项目根或绝对路径）。
2. **命令**（顶层 `sodus handoff ...`，不依赖 daemon；`needs_daemon=False`）：
   - `init [--force]`：创建模板，已存在且无 `--force` 时拒绝；
   - `show [--json]`：打印路径/内容/解析出的 snapshot/HEAD；
   - `append <text> [--section 最近变更]`：在指定节追加带日期 bullet；更新 `snapshot_at`；
   - `sync`：只更新 `<!-- sodus:auto:start --> ... <!-- sodus:auto:end -->` 之间的机器段（git HEAD/tag/分支、daemon 版本与 pid、当前任务/最近 run/plan 摘要、预算族上限），人工段落一律不动；
   - `check [--json]`：校验控制台存在、`snapshot_at` 可解析且不过期（默认 7 天）、控制台内记录 HEAD 与当前 HEAD 一致、关键路径存在、daemon 版本一致；失败返回非 0；
   - `list [--json]`：扫描已知项目根（从 task/run 的 cwd 去重 + 当前 cwd；若 DB 不可用则只当前 cwd），列出项目名/根/控制台路径/snapshot/status/下一动作；只读元数据。
3. **模板**：frontmatter（title/date/updated/version/type project-handoff/status/snapshot_at/tags），正文至少含：项目一句话、当前唯一优先、硬约束/禁止事项、实时状态、关键路径、决策记录、进度节点、未决/风险、下一步、最近变更、更新规则。控制在合理长度（≤200 行），不复制长文。
4. **HTTP 只读**：`GET /handoff?cwd=...` 返回 `{path, project_root, exists, snapshot_at, head, content}`（token 鉴权；cwd 必须解析到允许的 project root，禁止任意文件读取）；`GET /handoffs` 返回 list 元数据。**不加写端点**。
5. **MCP 只读**：`nodus_handoff_show`、`nodus_handoff_list`、`nodus_handoff_check`（只读，不允许 agent 自动改控制台）；更新工具数量/文档/测试。
6. **注册表**：不新增全局正文库；`list` 只根据已有 task/run cwd + 文件存在性生成元数据；可留 `~/.nodus/handoffs.json` 缓存但不得作为权威来源（MVP 可不写缓存）。
7. **测试**：临时 git 仓库；init/show/append/sync/check（HEAD 漂移、路径缺失、snapshot 过期）、list、HTTP 鉴权与 cwd 越界、MCP 工具列表；文件内容只读/原子写（temp+replace）。
8. **文档**：`docs/cli.md`、`docs/mcp.md`、`README.md` 补一段；`CHANGELOG.md` 不加版本号（由 Lead bump）。

## 验收
- 目标测试 + 全量 `A:\Anaconda\python.exe -m pytest -q` 退出码 0；
- `sodus handoff init/show/append/sync/check/list` 在临时仓库手动跑通；
- `docs/` 与 MCP 工具列表同步；
- `reports/080a-report.md`：实现/测试/手动证据/遗留；末尾 SUMMARY。
