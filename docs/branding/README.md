> 产品名 Sodus。

# 品牌：Sodus

2026-10-27 起对外产品名由 Sodus 改为 Sodus。仓库目录、Python 包、CLI 命令和默认状态目录目前仍是 sodus / ~/.sodus；改名按下面的阶段走，别做一半。

图形：竖向上、下 S，整只标绕中心旋转 +16.6°，蓝色主轴精确落在 90°；八条水滴、等角螺线 r 26→96、扫角 85°、根宽 5→末宽 25；中心 r14 独立球不接触触手；方向顺时针。完整参数见同目录 README.md。

## 文件

| 文件 | 用途 |
|---|---|
| sodus-mark.svg | 浅底主用，石墨 + 天蓝 S，透明底 |
| sodus-mark-reverse.svg | 深底主用，银渐变 + 天蓝 S |
| sodus-mark-mono.svg | 单色（浅底），印刷/水印 |
| sodus-mark-ondark-mono.svg | 单色（深底） |
| sodus-mark-light-hole.svg | 浅底“黑洞”变体，宣传用 |
| sodus-lockup.svg / sodus-lockup-ondark.svg | 横版组合，浅底/深底 |
| sodus-mark-512.png / sodus-mark-ondark-512.png | 512 位图 |
| sodus-preview.png | 一页看全 |

资产来源：A:\AgentWorkspace\reports\sodus-logo\（过程稿在 reports\sodus-logo\）。

## 改名阶段

- **阶段 A（2026-10-27，已完成）**：品牌资产入仓（docs/branding/sodus/）；看板 `<title>Sodus</title>` 与页头 SODUS；`sodus` console script（与 sodus 共用一个模块）；README 抬头改 `# Sodus`。
- **阶段 B（2026-11-03，本轮完成）**：显示名与对外接口名。docs 正文称呼、OpenAPI `info.title`、MCP `serverInfo.name`、`--help` 的描述行、README 正文都改用 Sodus；路径、命令、包名、`sodus_*` MCP 工具名不动。
- **阶段 C（进行中）**：
  - C1（2026-11-03，本轮完成）：新增 `sodus/` 入口包（`sodus.cli` 转发到 `sodus.cli`）；版本统一 0.5.0；`SODUS_HOME` / 已存在的 `~/.sodus` 参与状态目录解析；新增 `migrate-home`（默认 dry-run，`--apply` 才复制并校验）。
  - C2/C3（未做）：模块搬迁（`sodus/` 下的实现搬进 `sodus/`）、默认状态目录切到 `~/.sodus`。
- **阶段 D（未做）**：仓库改名（包括远程、CI、安装文档）。要单独计划，先备份、先写回滚。

## 图标（2026-10-27 补齐）

- 16px 光学版：sodus/sodus-mark-16.svg + 16/24/32/48 PNG + sodus-favicon.ico。
- App 圆角图标：sodus/sodus-app-icon.svg（深底）与 sodus-app-icon-light.svg（浅底），
  256/512/1024 PNG。
- 生成脚本在 AgentWorkspace：scripts/sodus_mark_gen.py + scripts/sodus_render.py，
  改主标后重跑即可；几何取自定稿 SVG，不重新推导。
