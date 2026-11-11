# 从 nodus 改名到 sodus（0.9.0）

0.9.0 起只有一个名字：sodus。包名、CLI 命令、状态目录、数据库文件、MCP 工具名、环境变量、仓库目录全部改掉；nodus 只作为迁移来源保留。

## 改了什么

| 旧 | 新 |
|---|---|
| pip 包 nodus / Python 包 nodus | sodus |
| 命令 nodus（双入口） | sodus（唯一入口） |
| 状态目录 ~/.nodus | ~/.sodus |
| 数据库 nodus.sqlite3 | sodus.sqlite3 |
| MCP 工具 nodus_task_create 等 | sodus_task_create 等 |
| 环境变量 NODUS_* | SODUS_* |
| 仓库 A:\VScode\Code\Projects\nodus | A:\VScode\Code\Projects\sodus |

## 迁移步骤

1. 停 daemon：sodus down（旧版本用 nodus down）。
2. 预览：sodus migrate-home。默认源是 ~/.nodus，目标 ~/.sodus，只打印计划。
3. 落地：sodus migrate-home --apply。复制并校验 task/run/artifact 行数与 sqlite integrity_check；数据库改名为 sodus.sqlite3；源目录不动。
4. 重装：pip install -e .（旧 nodus 包先 pip uninstall -y nodus）。
5. 重配宿主：sodus mcp install（Claude Code / Codex / DSH / opencode 都跑一遍）。
6. 启动验证：sodus up；sodus doctor；sodus status 的 tasks/runs 数字应与迁移前一致。

## 回滚

Remove-Item ~/.sodus -Recurse，再装回旧版本或旧 tag。源 ~/.nodus 一直在，未迁移前也随时可以回退。

## 说明

- SODUS_HOME 仍然优先；设置在它下面的目录不会被迁移命令覆盖。
- 旧 ~/.nodus 不会被隐式读取：没有 ~/.sodus 时命令读空库，所以先迁移。
- MCP 工具名变了，旧的工具调用记录/提示词要改成 sodus_*；客户端配置本身由 mcp install 重写。
- 历史 reports/ 和 eval/ 里的 nodus 字样保留，作为当时的记录和评测夹具，不再刷新。
