# sodus 标志文件

定稿日期：2026-10-27。品牌名 **sodus**，标记用「竖向上的 S」校正版（见下）。

## 文件清单

| 文件 | 用途 | 说明 |
|---|---|---|
| `sodus-mark.svg` | **浅底主用** | 石墨 #2B3444 身体 + 天蓝 #4E9BD8 的 S 两条；透明底 |
| `sodus-mark-reverse.svg` | **深底主用** | 银色渐变（#FFFFFF→#DCE4EE→#8E9BAC）+ 天蓝 #6FC8F2；透明底 |
| `sodus-mark-mono.svg` | 单色（浅底） | 纯 #111319，无强调色，用于印刷/水印/单色场景 |
| `sodus-mark-ondark-mono.svg` | 单色（深底） | 纯 #F2F5FA |
| `sodus-mark-light-hole.svg` | 浅底「黑洞」变体 | 黑盘当中心球，银色水滴环绕，宣传/海报用 |
| `sodus-lockup.svg` | 横版组合（浅底） | 标记 + `sodus` 字标，间距已按视觉平衡调过 |
| `sodus-lockup-ondark.svg` | 横版组合（深底） | 同上，字标改浅色 |
| `sodus-mark-512.png` | 位图预览 | 512×512，透明底 |
| `sodus-mark-ondark-512.png` | 位图预览 | 512×512，深底 |
| `sodus-mark-16.svg` | **16px 专用** | 4 臂光学版，viewBox 256 / width 16 |
| `sodus-mark-16.png` / `-24` / `-32` / `-48` | 小尺寸位图 | 16/24/32/48，透明底 |
| `sodus-favicon.ico` | favicon | 16/24/32/48 四帧 |
| `sodus-app-icon.svg` | App 图标（深底） | 圆角 58/256，深底 + 银标 + 天蓝 S |
| `sodus-app-icon-256.png` / `-512` / `-1024` | App 图标位图 | 透明底，圆角方形 |
| `sodus-app-icon-light.svg` / `-256.png` / `-512.png` | App 图标（浅底） | 浅纸底 + 石墨标 |
| `sodus-icons-preview.png` | 图标一页看全 | 16–1024 与小尺寸对照 |
| `sodus-preview.png` | 一页看全 | 四种用法对照 |

## 几何参数（可复现）

- 八条水滴，等角螺线：`r = 26 · e^(bθ)`，`b = ln(96/26)/85° ≈ 0.867`，扫角 **85°**
- 宽度：根 **5** → 末 **25**，幂次 1.0，末端半圆头
- 中心：一颗 **r14** 的独立小球，与任何触手都不接触（负空间锚）
- 强调色：90°/270° 两条；整只标绕中心旋转 **+16.6°**，使蓝色那对的视觉主轴精确落在 **90°（竖直）**
- 整体外径 108 / viewBox 256（四周留 20 边距）；方向为顺时针（`MIRROR=True`）

生成器：`A:\AgentWorkspace\scripts\nodus_logo_final.py`
`build_svg(spec, fill=..., fill2=..., rotate=16.6)` 即可复现；`g.arm(...)` 造参数。

## 已完成（2026-10-27 补）

- **16px 专用版**：sodus-mark-16.svg（4 臂、加描边把根部补到可见、中心球放大），
  位图 16/24/32/48，favicon.ico 四帧。16px 下已能读出「上蓝下蓝、四臂风车」。
- **App 圆角图标**：sodus-app-icon.svg（深底银标 + 天蓝 S）与浅底变体，
  256/512/1024 位图。几何直接取自 sodus-mark-reverse.svg / sodus-mark.svg，不重新推导。
- 生成脚本：`A:\AgentWorkspace\scripts\sodus_mark_gen.py`（出 SVG 与渲染页）、
  `sodus_render.py`（Playwright + Edge 透明底截图）。改主标后跑这两个即可重出。

## 相关记录

- 商标/近似初筛：`A:\AgentWorkspace\reports\2026-10-27_nodus-trademark-prefilter.md`
  （结论：「Nodus」在软件/AI 领域已有多方在用，欧洲有已注册文字商标；改用 **Sodus** 规避，
  且 Sodus 源自「Assorodus = 银色的水」，与银身水滴图形呼应）
- 设计与评审过程（含两份 Claude 评审原文、几何演进）：`A:\AgentWorkspace\reports\nodus-logo\`
- 工作日志：`A:\AgentWorkspace\journal\2026-10-26.md`、`journal\2026-10-27.md`
