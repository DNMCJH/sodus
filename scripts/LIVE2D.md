# Live2D → dsh-pet webm（真模型渲染）

> 2026-10-23 首次跑通。结论先行：**能出图、有 alpha、不需要 license key**。
> 仓库 `ycwycw2020/GPT_Dragon_girl_transformation` 里已经 vendored 了官方 Core runtime，
> 所以"缺 live2dcubismcore 就卡死"那个风险不存在。

## 1. 素材在哪

稀疏克隆（代理走 127.0.0.1:7890，实测 3.4 s 拉完骨架）：

```powershell
git -c http.proxy=http://127.0.0.1:7890 -c https.proxy=http://127.0.0.1:7890 `
  clone --depth 1 --filter=blob:none --sparse `
  https://github.com/ycwycw2020/GPT_Dragon_girl_transformation.git $env:TEMP\live2d-dragon
cd $env:TEMP\live2d-dragon
git -c http.proxy=http://127.0.0.1:7890 -c https.proxy=http://127.0.0.1:7890 `
  sparse-checkout set outputs/live2d-model-v4 outputs/live2d-preview-v4
```

| 路径 | 内容 | 说明 |
|---|---|---|
| `outputs/live2d-model-v4/dragon-working.moc3` | 54,656 B，MOC3 v6 | 真 Cubism 5 模型 |
| `outputs/live2d-model-v4/dragon-working.model3.json` | 357 B | **只有 Moc + Textures + DisplayInfo** |
| `outputs/live2d-model-v4/dragon-working.2048/texture_00.png` | 2.2 MB | 图集 |
| `outputs/live2d-preview-v4/vendor/Core/live2dcubismcore.min.js` | 228,050 B | **官方 Core 6.0.1，必须有** |
| `outputs/live2d-preview-v4/vendor/Framework/**` | 完整 Framework 5-r.5 | motion / physics / renderer / shader |
| `outputs/live2d-preview-v4/vendor/Shaders/WebGL/*` | 15 个 shader 源 | 运行时按路径 fetch |
| `outputs/live2d-preview-v4/{server,app,render-frames}.mjs` | 自带渲染管线 | 直接能跑 |

`vendor/provenance.json` / `RedistributableFiles.txt` 记录 Cubism SDK 的可再分发文件清单。

## 2. 三个必须先知道的事实

1. **Core runtime 在仓库里**。`vendor/Core/live2dcubismcore.min.js` 是 228 KB 的混淆 JS，不依赖 wasm。
   页面只要 `<script src="vendor/Core/live2dcubismcore.min.js">` 再 `CubismFramework.startUp()` 就能 `CubismMoc.create()`。
   运行日志打 `Live2D Cubism SDK Core Version 6.0.1`。
2. **这个 moc3 里没有任何 motion / expression**。`model3.json` 没有 `Motions`、`Expressions` 字段，
   目录里也没有 `.motion3.json` / `.exp3.json`。所以"原版动作"只能来自**参数驱动的网格变形**，
   不是播放模型内置动画。想放 motion 得回 Cubism Editor 里导出。
3. **`Groups.EyeBlink` 的 `Ids` 是空数组**。官方 `CubismEyeBlink` 拿不到参数，直接用会全程不眨眼。
   仓库因此自己写了 `eye-blink.mjs`（程序化眨眼 + 左眼闭合的独立 GL 贴图层）。

### 模型实际能动的参数（`evidence.parameterInspection` 实测）

存在的（10 个）：
`ParamEyeLOpen` `ParamEyeROpen` `ParamEyeBallX` `ParamEyeBallY` `ParamBreath`
`ParamBodyAngleZ` `ParamDragonAhoge` `ParamDragonTailBase` `ParamDragonMouseX` `ParamDragonKeyIndex`

**不存在**：`ParamAngleX/Y/Z`（头部旋转）、`ParamDragonHairFront/Back`、`ParamDragonTassel`、`ParamDragonKeyMiddle`。

这就是动作幅度的天花板：没有头部旋转、没有手臂骨骼，只有呼吸、身体轻微侧摆、尾巴、呆毛、视线和敲键盘的手指。
所以呼吸在 960×540 上只有 1~2 px 位移，**肉眼靠眨眼和高光变化来感知**。这是模型本身的限制，不是渲染的问题。

## 3. 怎么渲染（确定性步进）

`outputs/live2d-preview-v4/server.mjs` 起一个只监听 127.0.0.1 的静态服务（带 CSP、路径穿越防护），
`index.html?model=working_thinking&view=pet` 是桌宠视图（`body.pet` 下 canvas 撑满 `100vw×100vh`，背景透明）。

页面把渲染状态挂在 `window.dragonPreview`，把测试钩子挂在 `window.dragonModelQA`。抓帧只用这两个：

| 钩子 | 作用 |
|---|---|
| `setAmbientSample(t, disabled=false)` | `paused=true` + 清空 manual + `ambientTestTime=t`。**这就是确定性步进**：rAF 循环里 `dt=0`，呼吸/摆动/眨眼全部按传入的 `t` 求值 |
| `setReactionSample(id, age)` | 钉一个 reaction 在 `age` 秒处。**age 单位是秒，不是毫秒**（传毫秒会瞬间跑完，channels 全 0） |
| `setPose(params)` | 静态姿势，会把 ambient 适配器整个关掉（`manual.size>0 && ambientTestTime===null` → `disabled`） |
| `reset()` | 清 reaction / 触摸 / manual，取消暂停 |

`reactionCatalog` 是**属性不是函数**（源码里是 `reactionCatalog: REACTIONS.map(...)`，一个数组），`page.evaluate(() => window.dragonModelQA.reactionCatalog)`。
`reactions` 只在显式 `trigger()` 后才非 idle，所以不设 `setReactionSample` 时 idle 是干净的。

抓帧用 `page.screenshot({ omitBackground: true })`：拿到的是**合成后的页面**，比 `canvas.toDataURL()`
多一层 `#reactionOverlay`（爱心、脸红、怒气符号都画在那张 2D canvas 上，纯 WebGL 抓不到）。

视角/分辨率：`newPage({ viewport: { width: 960, height: 540 }, deviceScaleFactor: 1 })`。
`deviceScaleFactor=2` 一帧要 1.4 s（1920×1080 的 omitBackground 编码），`=1` 降到 0.44 s/帧，而且省掉一次重采样。

### 串起来

三个脚本已随本文件存在 `nodus\scripts\live2d\`（`capture.mjs` 顶部的 `REPO`/`PW`/`OUT` 是常量，换机器改这三行）：

```powershell
node capture.mjs --fps 24 --idle 8 --react 3.9 --dsf 1   # 起服务 + 开 Chromium + 两段抓帧 → raw\idle, raw\react
python normalize.py                                      # 75% 高、底边 y=495 → frames\idle, frames\react
python sheet.py                                          # 抽帧对照图
```

Playwright 不装新包，直接借 npx 缓存里那份（版本 1.64-alpha，配 `ms-playwright\chromium-1243`）。
`chromium.launch({ executablePath })` 必须显式指到 `chromium-1243\chrome-win64\chrome.exe`，
否则它按 1.64 的版本号去找 `chromium_headless_shell-1247`，本机没有，直接报 "Executable doesn't exist"。

参数：`--fps 24 --idle 8 --react 3.9 --dsf 1 --only idle|react`。8 s ≈ 83 s（192 帧），reaction 94 帧 ≈ 40 s。

### 归一化规则

模型 neutral bbox 在 960×540 画布上是 `(258,32,703,508)`，即 445×476 —— 整张"书桌+人"才是主体。
`normalize.py` 用**同一套**裁剪框和缩放（0.8508）对两段一致处理：
按 bbox 高度缩放，使高度 = `0.75×540 = 405`，底边贴 `y=495`，水平居中 `x=480`，输出 960×540 RGBA。
裁剪框留 8% 边距（`crop=(220,0,741,540)`），避免尾巴甩动被切掉。

## 4. 转 webm

```powershell
ffmpeg -framerate 24 -i frames\idle\%04d.png -c:v libvpx-vp9 -pix_fmt yuva420p `
  -b:v 0 -crf 18 -row-mt 1 -threads 2 -auto-alt-ref 0 `
  -metadata:s:v:0 alpha_mode=1 -an idle.webm
```

要点：

- `-pix_fmt yuva420p` 保 alpha；`-auto-alt-ref 0` 必须加，否则 alpha 平面丢。
- `-metadata:s:v:0 alpha_mode=1` 写进 WebM 的 `TAG:ALPHA_MODE=1`，dsh-pet 认这个。
- `-threads 2` 控内存；`-b:v 0` 配合 `-crf` 走恒定质量。
- `-crf 18` vs `30`：文件 712 KB vs 433 KB（8 s idle），**alpha 精度几乎没差别**
  （两者内部 alpha 都是 250–254，只有 ~1.9% 的像素是 255），所以为了体积用 24~30 也够。
- **ffprobe 会报 `pix_fmt=yuv420p`，这是正常的**：VP9 的 alpha 是独立平面，不在 pix_fmt 里。
  要验 alpha，必须用 libvpx 解码器抽帧再量：

```powershell
ffmpeg -c:v libvpx-vp9 -i idle.webm -vf "select=eq(n\,111)" -frames:v 1 -pix_fmt rgba a.png
# 不指定 -c:v libvpx-vp9 会走原生 vp9 解码器，alpha 直接没了，输出 RGB —— 别被这个骗了
```

实测：透明 80.7%、半透明 2.0%、不透明 19.3%（对得上归一化后 frame 的 26.4%×0.85²）。

## 5. 成品

| 文件 | 内容 | ffprobe |
|---|---|---|
| `%TEMP%\live2d-out\idle.webm` | 呼吸 + 眨眼（4.51 s 处一次完整闭眼） | vp9, 960×540, 24/1, 8.000 s, 192 帧, 712 KB, ALPHA_MODE=1 |
| `%TEMP%\live2d-out\react.webm` | `tail-cozy-sway`，3.8 s：歪头 + 尾巴摆 + 闭眼 + 爱心 | vp9, 960×540, 24/1, 3.917 s, 94 帧, 499 KB, ALPHA_MODE=1 |

抽帧对照图：`sheet-idle.png` / `sheet-react.png`（棋盘格底，6 帧/段）。

## 6. 加新动作 / 批量

- **换 reaction**：`window.dragonModelQA.reactionCatalog` 里 `{id, duration, target, tier, unlock}`，
  `target` ∈ `leg|tail|head|horn|cheek|hand`，一共 30 个。抓帧时 `setReactionSample(id, t)`，`t` 用秒。
  想找幅度最大的，比 `dragonPreview.ambient.maxDisplacementPx`（idle 基线 ≈7.9，reaction 峰值 ≈42）。
- **换状态**：`setVisualState('idle_low_energy')` 之类，但三种 idle 走的是 `layered-runtime`
  （独立原画分层渲染，不是 moc3），别指望它有 Cubism 网格变形。
- **换模型**：`model-catalog.mjs` 的 `MODEL_CATALOG` 加条目，文件丢 `outputs/live2d-model-v4/`。
  `ambient-motion.mjs` 的形变映射是按 `CanvasWidth===2426 && CanvasHeight===2592` 硬门控的，
  换尺寸就只剩 Core 自身变形（`enabled=false`），动作会更小。
- **不想要仓库的运行时增强**（腿/眉/左眼补图 + 网格附加形变）：URL 加 `?textures=atlas` 走图集纹理，
  就不会加载 `assets/source-layers/`，`legRepair`/`browRepair`/`leftEyeBlink` 全部为 null。
  代价是分辨率低、“左眼闭合”变成靠参数，效果差一截。

## 7. 坑

1. `reactionTest.age` 是**秒**。传毫秒 → `progress=1` → channels 全 0，看起来"反应没生效"。
2. `reactionCatalog` 是属性；代码里写成 `reactionCatalog()` 会 `TypeError: not a function`。
3. `page.screenshot` 的 omitBackground 才带 alpha；`canvas.toDataURL` 带 alpha 但丢 overlay 特效。
4. 不要用 `--use-gl=swiftshader` 单独，配 `--use-angle=swiftshader --enable-unsafe-swiftshader` 才出图。
5. 模型加载 + 1254 母版 source-layers 大约 10 s（首次渲染到 `status==='rendering'` 实测 9.7 s，内存峰值没测）；一次只开一个 page，
   抓完立刻 `browser.close()`，否则连续跑两段会被 OOM kill。
6. `CubismMoc.create(buffer, true)` 第二个参数是 **shouldCheckMocConsistency**，`true` = 做一致性检查。
   这个 moc3 能通过（Core 返回非 null）；传了检查却被拒或返回 `null` 就说明 moc3 版本和 Core 不匹配。
7. 归一化必须两段共用同一 `crop + scale`，否则 idle 和 react 的角色大小会对不上。

## 8. 版权

- **模型**（`dragon-working.moc3` / 贴图 / `asset/source-layers` 的 1254 母版）是仓库作者的**粉丝二创**，
  角色原型与美术资产的权利不在本项目、也不在本机使用者手上。**只能自用，不得商用、不得再分发**。
- **Cubism SDK**（`vendor/Core` 与 `vendor/Framework`）是 Live2D Inc. 的专有许可，
  按 `vendor/RedistributableFiles.txt` 允许随应用再分发的那部分才可用；改 SDK 或商用要另外授权。
  `vendor/Framework-LICENSE.md` / `vendor/NOTICE.md` 是原始许可文本，别删。
- 产出的 webm 里含上述美术资产，同样**只做本机桌宠自用**，不要上传、不要再分发。
- 如果要给 dsh-pet 做公开分发的角色，必须换成自有版权或明确授权的模型。


