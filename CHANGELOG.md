# 更新日志

## 0.5.1（2026-11-08）

0.5.0 之后的稳定化版本：不新增子系统，补默认路由、预算软闸门、opencode 权限与 daemon 状态一致性，让 sodus 可以交给其它会话稳定使用。

### 默认路由
- `config.routing.default` 决定不带 `--route` 时的 route（当前 `sol`），`sodus run` 不再硬编码 claude；`config.default_route()` 缺失时退回 claude，老配置行为不变。
- `server.py` 的默认 route 同样从配置读取；新增 `tests/test_default_route.py`。

### 预算软闸门
- `budget.soft_cny`：当日花费过软线后，`opus` / `claude` 两类 route 和 `claude-opus*` 模型需要显式 `--allow-expensive` 才会派发；硬上限 `daily_cny` 语义不变。
- `sodus run --allow-expensive`、`POST /runs` 的 `allow_expensive`、`sodus status` 的 `soft=` 行同步加上；新增 4 个预算测试。

### opencode
- `opencode run` 加 `--auto`：headless 下读取类工具不再被静默拒绝（此前表现为「无输出」而不是权限错误）。

### daemon 状态
- CLI 自动拉起 daemon 时把实际 pid 写回 `daemon.json`；此前 `ensure_daemon` 只启动不记账，`down`/`up` 会指向已死 pid。新增回归测试。

### harness 统计
- 新增只读的 harness 指纹与统计：`nodus debug harness-stats`。
  - `nodus/harness.py`：brief 模板哈希（`version_of`）、run 的 harness 指纹（agent/route/model/provider/brief_version）、按 (harness 组件, cause) 聚合的统计。纯函数，不写库、不调度。
  - `nodus/runner.py`：worker prompt 抽成 `PROMPT_TEMPLATE` 常量，版本是模板的哈希（改文案就换版本）；每个新 run 追加一条 `run.harness` 事件记录版本，历史 run 没这条事件时退回「未记录」，不猜。
  - `nodus debug harness-stats [--by route|agent|model|provider|brief_version] [--kind work|chat|all] [--limit N]`：看「某条路线遇到某类失败时，跑完并且验证通过的比例」。**unverified 不进分母**（退出码不是验收证据），在跑的 run 不计。
  - `nodus/db.py`：`event_payloads`（一次批量取某类事件）与 `count_runs`。
  - 这条命令只提供计数，不做推荐、不改行为；候选 harness 的自动搜索没有做。

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
