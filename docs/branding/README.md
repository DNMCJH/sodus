# 品牌：Sodus

2026-10-27 起对外产品名由 Nodus 改为 Sodus。仓库、Python 包、CLI 命令和状态目录目前仍是 nodus / ~/.nodus，代码层改名按阶段做，别做一半。

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

资产来源：A:\AgentWorkspace\reports\sodus-logo\（过程稿在 reports\nodus-logo\）。

## 改名阶段

- 阶段 0（已完成）：品牌资产入仓（docs/branding/sodus/），对外文案开始用 Sodus。
- 阶段 1（未做）：board UI 的 title/brand 区、CLI 显示名、README 抬头、报告模板。
- 阶段 2（未做，需单独计划、备份与回滚）：Python 包名 nodus → sodus、console script 加 sodus 别名（nodus 保留至少一个版本）、~/.nodus → ~/.sodus 迁移、DB/daemon.json/token 路径、测试与文档同步。

## 未做

- 16px 专用图标（现在 24px 以下会糊）。
- App 圆角图标（现有那批是旧几何，不能用）。
