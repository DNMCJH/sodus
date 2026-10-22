# Coopanion 形象包 → dsh-pet 动画

转换器：`scripts/coopanion_to_dsh.py`
第一次跑通：2026-10-23，包 `gpt-dragon` v1.1.0，产出 `idle` / `wave`。

---

## 结论先放前面

**Coopanion 包不是逐帧，也不是精灵图，是「分层 PNG + 枢轴骨架 + 运行时网格变形」。**
而且比这更麻烦的一点：**18 个动作的时间曲线不在包里**。

`figure.js` 自己不算动作，它导出 `createDragonBody(base, opts)`，内部调
`opts.kit.createRig(...)` / `opts.kit.createBody(...)`，渲染函数 `draw(group, face, o)`
只消费外面传进来的 `o`：

```js
o = { t, sit, swing, look:[x,y], mode:'walk'|'run'|'dance'|'sleep'|..., gesture:{kind,k}, legs:[[x1,y1,x2,y2],...] }
```

`kit` 是 Coopanion 应用本体（`packages/cortico-world-desktop-pet`），包里的 README 也写了
「声音和身体运动使用应用提供的 kit，包内不附带音频」。

所以这个转换器能做到的是：**用作者给的分片几何 / 枢轴 / 层级 / z 序，按动作语义自己写时间曲线**。
产出的不是「Coopanion 原版动作导出」，是「同一套皮、动作合理」。别对外说成前者。

---

## 一、包的实际结构

### 1.1 gpt-dragon（`coopanion-gpt-dragon` v1.1.0，1.78 MB）

```
gpt-dragon/
├─ figure.json    13 KB   名称、版本、入口、37 条词表（19 表情 + 18 动作，每条带 seconds）
├─ model.json     6.8 KB  30 个分片的 box / z / 父骨名 + 15 个枢轴 + units
├─ figure.js      17 KB   参数化渲染器（导出的入口是 createDragonBody）
├─ tex/           30 张  透明 PNG，**已按 box 精确裁切**（实测 30/30 尺寸与 box 相等）
└─ thumb.png      装扮页缩略图（1024 空间下所有分片叠起来的样子，可当合成正确性的参照）
```

`model.json` 里三个关键量：

```json
"units": { "scale": 0.245, "centerX": 445, "feetY": 969 }
"parts": [ { "id":"hair-back", "src":"tex/hair-back.png",
             "box":[231,127,509,615], "parent":"hairBack", "z":0 }, ... ]
"pivots": { "upper":[425,574], "head":[425,460], "tail":[526,683], ... }
```

`box` 和 `pivots` 都在 **1024×1024 的原画坐标系**里；`units` 把源坐标映射到运行时视口：

```
U(x) = 128 + (x - 445) * 0.245      V(y) = 256 + (y - 969) * 0.245
视口 = 276 × 263（figure.js 里 viewBox 写的是 [0,15,276,278]）
```

角色在视口里只有 **221.5 单位高**，脚底在 y=256。这个 0.245 决定了「54 源像素的下沉 = 13.2 视口单位」这类常数。

### 1.2 z 序与骨

30 个分片按 `z` 从 0 到 27 叠：`hair-back(0) → wing-far(1) → tail(2) → leg-far(3) → leg-near(4)
→ skirt-far/near(5) → arm-far(6) → neck(7) → torso(8) → sit(9) → apron(10) → arm-near(11)
→ ear(12/13) → face(14) → nose(15) → mouth(16) → 眼白/虹膜(17~20) → horns(21) → hair-front(22)
→ ahoge(23) → 睫毛(24/25) → 眉毛(26/27)`。

骨层级不在 JSON 里，只写在 `figure.js` 的 `deformers` 块中，三种写法并存：

```js
upper: rot('upper'),                       // 根
head:  rot('head','upper'),                // 子骨
hairBack: warp('head',[230,220,740,742]),  // 网格变形，父骨 head
seat: {kind:'rot', pivot: xy([425,570])},  // 枢轴是字面量，model.json 的 pivots 里没有
```

转换器用正则把三种都抠出来 → 16 根骨（`upper` 为根，`head/armFar/armNear/wingFar/wingNear`
挂 `upper`，`hairBack/hairFront/ahoge/ear*` 挂 `head`，`tail/leg*/skirt*/apron/seat` 为根骨）。
`seat` 那个字面量枢轴要单独认，漏了就会退化成绕 (0,0) 旋转 —— 画面上是整个人飞出画布。

### 1.3 表情怎么组织

**dragon 包没有独立的表情图。** 眼睛、嘴、腮红、泪、汗、问号全是 `figure.js` 里用
Canvas 2D 现画的（`paintFace()`，约 80 行），只有 `mouth.png` / `nose.png` / 眼白 / 虹膜 / 睫毛
是分片。19 个表情 = 19 组 `face` 参数（`shape: lid/up/down/gt/lt/heart/spiral`、`brows`、`blush`、
`streams`、`sparkle`、`sweat`、`anger`、`question`、`bang`）。

这部分**没有移植**（见限制 4）。

### 1.4 gpt-claude 是另一套 schema

同版本号，字段完全不同，不能当同一份代码读：

| | gpt-dragon | gpt-claude |
|---|---|---|
| 坐标空间 | 1024 原画像素 + `units.scale/centerX/feetY` | 已在视口空间 + `units.S/X0/FEET` |
| 分片字段 | `parts[].src`（含 `tex/` 前缀） | `parts[].tex`（只有名字） |
| 分片尺寸 | PNG 尺寸 == box 尺寸 | PNG 是 box 的 1/0.2474 ≈ 4.04 倍 |
| 表情 | 运行时现画 | 单独的 `feat/` + `model.json` 里的 `feat.sprites` 坐标表 |
| 骨名 | `upper/legNear/skirtNear...` | `body/waist/neck/legFront/legBack/hairSway...` |

转换器的解法规避了大部分差异：**不猜单位，逐片量 `png宽 / box宽` 当缩放系数**，所以在哪种坐标系下都能合。
骨名靠 `role_of()` 做归一（`legBack→legFar`、`skirtSit→seat`、`hairSway→hairFront`）。

实测 claude 包能读进来：**19 分片 / 17 骨**，角色映射通了。
但**没跑出片、没验收过**，只验证了加载这一步。

### 1.5 Asryto 版龙娘（`GPT-Dragon-Asryto-1.0.2`，20.6 MB）

结构更复杂，不是同一代格式：多了 `expressions.js` / `hair-motion.js` / `tail.js` / `secondary.js` /
`part-registration.json` / `SOURCE.json`，`tex/` 51 张里有 `head-open` / `head-closed` /
`head-wink` / `head-flustered` 这种**把表情烘焙进贴图**的变体。
这次没碰，只记下它存在。

---

## 二、转换原理

```
load_pack   读 figure.json + model.json，逐片量 png/box 比例 → 归一到「视口空间」
            ↓
parse_rig   从 figure.js 正则抽 deformers → 骨树；补字面量枢轴
            ↓
Rig.evaluate  把 figure.js draw() 里算 state 的那段（约 60 行）移植成 Python
            ↓
motion      每个动作给出 kit 级参数 o（swing / look / sit / gesture / mode / legs / root_*）
            ↓
compose     逐片算世界矩阵 → 绕枢轴 旋转/缩放 + 平移 → Pillow AFFINE + alpha_composite
            ↓
fit         中立姿势量 bbox → 缩放使角色高 = 画布 75%，底边对齐 y=495
            ↓
ffmpeg      PNG 序列 → VP9 yuva420p，threads 2
```

### 2.1 state 计算是从 figure.js 移植的，不是瞎编

`draw()` 里这几条原样搬过来（角度/位移仍是作者的数）：

| 骨 | 公式 |
|---|---|
| `upper` | `a = -bow*15`，`ty = low`（坐下下沉） |
| `head` | `a = nod*2.8 + shake*2 + (confused?3)`，`tx = lookX + shake*.7`，`ty = lookY + nodLow` |
| `armNear` | `a = -wave*(104 + sin(k·π·12)·16) - sin(t·1.7)·1.2` |
| `wingFar/wingNear` | `a = ±wing + flap*9`，`sx = 1 - |flap|*0.2` |
| `tail` | 弹簧 `k=65 c=13` + `wag*6.5*sin(t*(2.3+2.3*wag))` + `sin(t*.85)*1.8` |
| `ahoge` | `a = hair*2.5 + sin(2t)*1.4 + flap*5` |
| `skirtFar/Near` | `a = ±hair*0.25`，`ty = low*0.55` |

只有两处主动改过，都写在注释里：
1. `low` 从 `sit*54*S` 改成 `sit*0.0595*char_h`（同值 13.2 视口单位，但不绑死坐标系）；
2. 招手的摆动幅度 `±9° → ±16°`（24fps 下 9° 落在 6~7 帧一个周期，肉眼看不出）。

弹簧用 4 个子步推进（1/96s），逼近 figure.js 跟 vblank 的 1/60s 手感。

### 2.2 合成

每片：

```
src 像素 → 乘 (box宽/png宽) → box 坐标 → 骨世界矩阵 → fit 矩阵 → 画布像素
```

用 `Image.transform(size, AFFINE, 逆矩阵)` 直接采样，`alpha_composite` 按 z 叠。
**2× 超采样**（960×540 的画在 1920×1080 上合成）再 LANCZOS 降回，边缘才不毛。

`measure_neutral()` 先渲染一帧中立姿势量真实 alpha bbox（不是拿 box 并集估），
所以「高 405px、底边 y=495」是按实际剪影算的，不是按分片外框。

### 2.3 根变换是补的

包里的 `figure.js` 只能绕枢轴转分片，**没有整体位移/旋转/翻转的钩子**（跳、转圈、转身这些
在应用里是挪窗口做的）。这些动作的根变换（`root_dx/root_dy/root_rot/root_sx/root_sy`）
是转换器自己加的，并且会被钳制：最多顶到画布上沿留 4px，不会把头裁掉。

镜像也是补的：原画朝左，应用里 `translate(256 0) scale(-1 1)` 翻成朝右。转换器默认同样翻转
（`--no-flip` 可关），所以 webm 里她朝右。

### 2.4 编码

```
ffmpeg -framerate 24 -i f_%05d.png -c:v libvpx-vp9 -pix_fmt yuva420p \
       -auto-alt-ref 0 -b:v 0 -crf 26 -row-mt 1 -threads 2 -an out.webm
```

`-threads 2` 不能省：本机可用内存 0.5~1.5 GB，libvpx 开满线程直接 `STATUS_STACK_OVERFLOW (0xC00000FD)`，
不报错也没产物。

---

## 三、动作表

包里有 18 个动作，转换器实现了 18 个 + 2 个辅助：

| 包里 id | 时长 | 实现方式 | 备注 |
|---|---|---|---|
| `stand` | 1.2s | `sit: 1 → 0` | |
| `jump` | 1.2s | `root_dy` + 挤压/拉伸 | 根位移是补的 |
| `hop` | 0.9s | `root_dy` | 同上 |
| `look` | 2.7s | `look[0]` 左右扫 | |
| `turn` | 0.4s | `root_sx` 压扁到 -1 | 包里没有绕竖轴旋转，只能这样 |
| `nod` | 0.8s | `gesture{nod}` | |
| `shake` | 0.8s | `gesture{shake}` | |
| `spin` | 0.8s | `root_rot` 360° + `root_sx` 假装透视 | 近似 |
| `sit` | 0.8s | `sit: 0 → 1`，坐姿分片交叉淡化 | 中间会有双影，包的设计如此 |
| `sleep` | 0.8s | `sit=1` + `mode:sleep` | |
| `dizzy` | 3.2s | 高频 swing + look 旋涡 | |
| `walk` | 12s（实现 6s） | `mode:walk` + 合成腿点 | 腿是 kit 给的，这里是编的 |
| `run` | 12s（实现 6s） | `mode:run` + 手臂后甩 | 同上 |
| `wave` | 1.7s | `gesture{wave}` | **已转，已验收** |
| `bow` | 1.7s | `gesture{bow}` → `upper.a = -15·sin²(πk)` | |
| `shiver` | 1.9s | 26Hz swing 抖动 | |
| `flap` | 1.5s | `gesture{flap}` + `root_dy` | |
| `dance` | 3.4s | `mode:dance` + swing 9 | |

额外两个内置：`idle`（4s 无缝循环，dsh-pet 的 idle 槽要有循环，包里的 `neutral` 只有 0.9s 单发太短）、`neutral`（0.9s 静态）。

自测：包里 18 个 motion 全跑过一遍（640×360、`--crf 36`），加上 `neutral` 共 18 条片子，
零报错、零跳过，81 秒跑完。**能出片不等于好看**，`turn` / `spin` / `shiver` 那几条看之前先读限制 7、8。

---

## 四、已知限制

按严重程度排，别只看前三条就以为没事了：

1. **动作曲线不是原版。** 编排在 Coopanion 应用里，包里没有。现在的曲线是按动作语义重写的，
   节奏对了，细节（重心转移、预备动作、尾巴的延迟跟随）全是近似的。要原版得把 kit 拿过来跑。
2. **warp 层被降级成旋转。** `hairBack` / `hairFront` 在包里是网格变形（形状随
   `fn(u,v)` 弯），转换器只做绕枢轴转 + 平移。头发摆动看起来是整体的，不是弯曲的。
3. **手臂是单块刚体。** `arm-near` 只有一根骨，没有肘关节。抬到 104° 的时候手落在脸颊旁边
   而不是伸到身体外侧，看起来像「摸脸」多过「挥手」。这是素材决定的，不是 bug。
4. **表情没接。** `paintFace()` 那 80 行（眨眼、爱心眼、旋涡眼、口型、泪、汗、问号）**没有移植**。
   所有片子的眼睛和嘴是静止的。`face` 参数只影响尾巴/翅膀的情绪幅度和 `confused` 时头偏 3°。
   想要表情得再写一轮。
5. **没有眨眼。** 同上。
6. **坐姿交叉淡化有双影。** figure.js 故意让「坐姿件先淡到满、站姿件再淡出」，中间约
   0.08 的 `sit` 区间里两套腿同时可见。这是作者的选择，照搬了。
7. **`turn` / `spin` 是糊过去的。** 2D 分片没有绕竖轴的旋转钩子，只能整身平面转或横向压扁。
8. **24fps 采样。** 应用是跟 vblank（约 60fps）跑的。3.5Hz 以上的抖动（比如 `shiver` 的 26Hz）
   在 24fps 下会被采样成不规则跳动。
9. **claude 包没验收。** 能读、能映射骨、能出片管线，但动作参数是照 dragon 校准的，没跑过。
10. **没有音频。** 包里本来就没有。

---

## 五、怎么跑

```powershell
# 先看包里有什么（不渲染、不写文件）
python coopanion_to_dsh.py --pack <包目录> --list

# 单个动作
python coopanion_to_dsh.py --pack <包目录> --out <输出目录> --kind gpt-dragon --actions wave

# 两个动作 + 抽帧图
python coopanion_to_dsh.py --pack <包目录> --out <输出目录> --kind gpt-dragon `
  --actions idle,wave --strips 6 --strip-cols 3

# 包里 18 个动作全转（12s 的 walk/run 按 6s 出，比较慢）
python coopanion_to_dsh.py --pack <包目录> --out <输出目录> --actions all

# 640×360（和现存 claude/gemini 素材同尺寸）
python coopanion_to_dsh.py --pack <包目录> --out <输出目录> --size 640x360 --actions all
```

产物：

```
<out>/pet/<kind>-animation/
    idle.webm          96 帧 / 4.00s / 432 KB
    wave.webm          41 帧 / 1.71s / 219 KB
<out>/strips/          从成片 webm 解码回来的抽帧图（同时验证 alpha 过没过编码）
<out>/animations.snippet.json   给 dsh-pet 的配置片段
```

**两条命令之外什么都不写。** 不碰 `A:\dsh\home\dsh-pet\`，不改插件，不重启客户端。

配置片段只有 `pets` + `animations`，合并进 `<kind>-config.json` 即可；
`clicks` / `categories` / `events` 里的槽位是**按动作语义猜的**（`wave` 进 clicks、`idle` 进 idle 槽），
实际用哪条自己调。

### 常用参数

| 参数 | 说明 |
|---|---|
| `--pack` | 包目录（含 `figure.json` / `model.json`） |
| `--out` | 输出根目录 |
| `--kind` | dsh-pet 种类名，默认用包 id |
| `--actions` | 逗号分隔，`all` = 包里全部 motion |
| `--size` | 默认 `960x540`，可 `640x360` |
| `--fps` | 默认 24 |
| `--crf` | 默认 26（越小越清晰越大） |
| `--flip / --no-flip` | 默认翻转成朝右，和 Coopanion 显示一致 |
| `--strips / --strip-cols` | 抽帧图格数和列数 |
| `--config-out` | 配置片段写文件，否则打印 |
| `--keep-frames` | 保留逐帧 PNG（调试） |

---

## 六、验收（2026-10-23 实测）

`ffprobe` 对两个成品的输出：

```
idle.webm   442,799 bytes
  vp9 / 960x540 / 24/1 / duration 4.000000 / ALPHA_MODE=1
wave.webm   224,549 bytes
  vp9 / 960x540 / 24/1 / duration 1.709000 / ALPHA_MODE=1
```

另核过：

- **alpha 真的活下来了**：从成片 webm 解码（必须 `-c:v libvpx-vp9 -pix_fmt rgba`，
  默认解码器会给丢 alpha 的 RGB）取帧，alpha 范围 0~255，内容外全透明。
- **几何没跑偏**：中立姿势在 1024 空间重合成一遍，和作者给的 `thumb.png` 结构一致（分片、z 序、朝向都对）。
- **底边对齐**：alpha ≥ 16 的真实内容底边 = 330/495，正好压在基线上；330~360 之间只有
  alpha 1~15 的编码噪点（现存 dsh-pet 素材整帧都盖着一层 alpha=1，比这个更重）。
- **idle 首尾能接**：f0 和 f95 的 alpha 差异 < 1%，所有频率取 4s 的整数倍。
- **jump 不裁头**：29 帧里内容 top 最小 10px，没碰到画布上沿。

---

## 七、版权

包内 README 原话：形象来自网络、**未获授权**、**禁止商用**，仅限个人使用与分享。
转换出来的 webm 继承同样的限制。角色原案 ZipZipPipe（B站 uid 4168597）。
