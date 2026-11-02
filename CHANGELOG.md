# 更新日志

## 0.5.0（2026-11-03）

产品名 Sodus，实现名 nodus。这一版只改**显示名和入口面**，不动模块位置、仓库目录和默认状态目录。

### 阶段 B：显示名与对外接口名

- docs（`cli.md`、`mcp.md`、`vision.md`、`routing-memory-lineage.md`、`branding/README.md`、`branding/sodus/README.md` 及历史报告）：正文称呼统一为 Sodus，路径和命令仍写 nodus；每份首行注明「产品名 Sodus，实现名 nodus」。
- `docs/branding/README.md` 的改名阶段按 A/B/C/D 重写：A 已完成，B 与 C1 本版完成，C 的模块搬迁和 D 的仓库改名还没做。
- `openapi.yaml`：`info.title` 改 `Sodus HTTP API`，`summary`/描述改称 Sodus；路径、字段、`info.version` 不动。
- `nodus/mcp.py`：`SERVER_NAME` 改 `sodus`（`serverInfo.name`）；**工具名仍全部是 `nodus_*`**，客户端配置不用改。
- `nodus/cli/__init__.py`：描述行改 Sodus 的产品描述；`--version` 打印实际入口名（`nodus 0.5.0` / `sodus 0.5.0`），usage 行跟随入口名。
- `nodus/pet.py`、`nodus/ui.py`：检查后没有残留的用户可见 "Nodus"（阶段 A 已改完看板），只剩注释；`pet_bridge` 配置键和 `nodus-theme` localStorage 键不动。

### 阶段 C1：入口面与状态目录

- 新增 `sodus/` 包：`sodus/__init__.py`（版本 re-export）、`sodus/cli.py`（转发 `nodus.cli:main`，只改 usage 里的名字）、`sodus/__main__.py`。
- `pyproject.toml`：`nodus = "nodus.cli:main"` 保留，`sodus = "sodus.cli:main"`；打包 `include = ["nodus*", "sodus*"]`；版本 0.5.0。
- 版本号统一：`nodus/__init__.py` 的 `__version__`、`config.VERSION`、`pyproject.toml` 都是 0.5.0（此前包内是 0.2.0，发行版是 0.4.0）。
- 状态目录解析（`nodus/config.py`）：`SODUS_HOME` > `NODUS_HOME` > 已存在的 `~/.sodus` > `~/.nodus`。不自动建 `~/.sodus`，不自动搬数据。
- 新增 `nodus migrate-home`：把 `~/.nodus` 复制到 `~/.sodus`，校验 sqlite `integrity_check` 与 task/run/artifact 行数；默认 dry-run，`--apply` 才落地，失败删除半成品，绝不移动或删除原目录。
- 新增 `docs/sodus-migration.md`（切换与回滚说明）和本文件。

### 没改的东西

- 模块仍全在 `nodus/` 包里，没有搬迁（C2/C3）。
- 仓库目录仍叫 `nodus`，远程和 CI 不动（阶段 D）。
- MCP 工具名仍是 `nodus_*`，`serverInfo.name` 之外没有任何协议变化。
- 默认状态目录仍是 `~/.nodus`；只有手动跑 `migrate-home --apply` 之后 `sodus` 才会自动读 `~/.sodus`。
- 托盘 / 桌宠 / MCP 客户端配置不需要改。
