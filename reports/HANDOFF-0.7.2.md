# HANDOFF 0.7.2（2026-11-09）

## SUMMARY

0.7.2 已发布：main merge `fcc75af`、tag `v0.7.2`，daemon 0.7.2（pid 159100，单实例）。新增按模型族软上限、opencode MCP 入口、四终端并发覆盖。release_check PASS。

## 这版交付

- **模型族软预算**（用户要求）：`budget.soft_families = {claude:100, gpt:80, other:80}`（CNY，0/缺省=off）。按当天已记账 CNY 成本聚合到族（claude/opus/sonnet/haiku → claude；gpt/o1/o3/o4/o5/codex → gpt；其余 → other），到线只拦该族新 run，`--allow-expensive` 可豁免；没有日硬上限，relay 余额是硬约束。已写入 `~/.nodus/config.json`（备份 `.bak-20261109-families`）。
- **opencode MCP**：`sodus mcp install --host opencode` 写 `~/.config/opencode/opencode.json` 的 `mcp.sodus`（local stdio），保留 provider/其他字段、原子写+备份、check/uninstall。已实装，`opencode mcp list` 显示 `sodus connected`。四宿主（Claude Code / Codex / opencode / DSH）入口齐。
- **四终端并发**：文档 + in-process 四线程 HTTP 测试；实测 4 个 run 在 4 个独立目录并行（sol 路由）全部 completed，文件验收通过，无锁残留。
- **预算实机状态**（截至 07:45）：今日合计 ¥112.26；claude 族 ¥45.14/100、gpt 族 ¥50.68/80、other ¥16.44/80，均未到线。

## 验证证据

- `release_check.ps1`：PASS，退出码 0（构建 wheel/sdist、隔离安装 0.7.2、doctor、MCP、DB 升级、全量 pytest）。
- 全量 pytest：0.7.2 分支/合并后均 exit 0（release_check 内覆盖）。
- 四并发实测：tasks 09fe/fe65/0ffd/c0f8，runs run-a7746821f359、run-fd0d2a9a00c7、run-13236f0e21a6、run-7f47570e900c 全部 completed，各自 cwd 下 hello.txt 内容正确，无写锁残留。
- opencode：`opencode mcp list` → `✓ sodus connected`；`sodus mcp install --check` 四宿主 configured，exit 0。
- 预算：`budget.check()` 对 opus/sol/dsh 分别返回 family claude/gpt/other、limit 100/80/80、gated=false（当前未到线）。

## 重启与事故记录

- 重启了 sodus daemon（0.7.1 → 0.7.2）。
- 重启 DSH 侧旧 MCP 进程（03:41 启动、加载 0.6 代码）时，DSH 重新拉起了新的 sodus mcp 客户端（07:39，0.7.2 代码）；期间原 daemon（pid 165116）意外退出且 daemon.log 无 crash 记录，已用 `sodus up` 恢复为 pid 159100；随后 `/health` 0.7.2、MCP 工具调用正常。这个自愈路径要持续观察：怀疑是在 DSH 的 Windows Job 内启动 daemon 时，杀 MCP 子进程触发了 Job 清理；正常从终端启动不受影响。
- 当前 DSH 会话的 MCP 工具已可用（新客户端），`nodus_cost_today` 返回正常。

## 已知边界

- 软上限按已记账成本检查，不预留在途/并发启动成本：多个 run 同时起跑可能在到线前一起越过上限；relay 余额仍是硬约束。
- 同目录并发仍由写锁串行（ADR-012）；不同目录可并行。
- 无人值守/自动批准执行仍被 0.7.2 之前列出的 B3/B4 阻塞（worker 无沙箱/env 白名单、批准身份声明式）；plan 节点执行/物化/replan 未实现。
- DSH 的 MCP 进程可能有多份（DSH 按会话/重连拉起），都是 HTTP 客户端；不影响 daemon 单写者，但会占少量进程。

## 回滚

- `git revert -m1 fcc75af`（或回到 `v0.7.1` tag），重启 daemon；DB schema v7 只增表，旧代码可读。
- 预算备份：`~/.nodus/config.json.bak-20261109-families`；opencode 配置备份由 installer 自动生成（同目录 `.bak-*`）。
