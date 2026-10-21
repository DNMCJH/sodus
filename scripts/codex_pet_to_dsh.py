#!/usr/bin/env python3
"""codex-pets 图集 → dsh-pet 种类：把 Claude / GPT 桌宠接到同一层桌面上。

用法：
  python scripts/codex_pet_to_dsh.py --list claude
  python scripts/codex_pet_to_dsh.py --pet claude-chan --kind claude --name "Claude小姐"
  python scripts/codex_pet_to_dsh.py --pet gpt-maiden-refined --kind gpt --name "GPT娘" --corner bottom-left

做什么：
  1. 从 codex-pets.net 取 pet.json + spritesheet.webp（缓存在 %TEMP%\\nodus-codex-pets）
  2. 按 192×208 切格，只取 v1/v2 的 9 个状态行（v2 的第 10/11 行是 16 向注视格，丢弃）
  3. 每格贴到 640×360 的 16:9 画布上（dsh-pet 的 <video> 就是这个比例，否则会被压扁）
  4. ffmpeg → VP9 + alpha（ALPHA_MODE=1）、24fps，规格与包内素材一致
  5. 写 $DSH_HOME/dsh-pet/pet/<kind>-config.json 和 <kind>-animation/webm/*.webm

为什么绕这一圈：
  dsh-pet 的桌面模式（Electron 透明置顶窗）只吃 webm；codex-pets 只发 webp 图集。
  直接装 @signalight/dsh-codex-pet 也能显示这些宠物，但那是**页内** overlay —— 只能在
  DSH 网页里飘，贴不到别的窗口上面。转成种类之后，Claude/GPT 和 DeepSeek 娘住在同一层
  桌面上，拖动/抛掷/工作状态联动全部复用，不用装插件、不用重启 DSH（改配置 + 刷新即可）。

不动什么：
  不碰 main-config.jsonc。宠物实例写在种类文件自己的 pets[] 里，DeepSeek 娘的配置原样保留。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

API = "https://codex-pets.net"
CELL_W, CELL_H = 192, 208
COLS = 8
CANVAS_W, CANVAS_H = 640, 360  # 包内素材同尺寸；dsh-pet 的 size 只给宽度，高度按 9/16 算
CANVAS_W, CANVAS_H = 768, 432  # 2× 包内素材的 640×360：桌宠按 size 缩下去，源大一点才不糊
FPS = 24
# 画布里角色的目标高度和脚下基线：照包内素材量的（内置 webm 里角色占 640×360 的
# 213×270，底边 y=330）。等比换算到 768×432 就是 324/396——同样的 size 下新宠物
# 和 DeepSeek 娘看起来一样大。
CHAR_HEIGHT = 324
BASELINE_Y = 396

# codex-pets atlas 的 9 个状态行（codex-pet-web/dist/constants.js 的定义，v1/v2 共用前 9 行）
ATLAS_STATES = [
    ("idle", 0, 6),
    ("running-right", 1, 8),
    ("running-left", 2, 8),
    ("waving", 3, 4),
    ("jumping", 4, 5),
    ("failed", 5, 8),
    ("waiting", 6, 6),
    ("running", 7, 6),
    ("review", 8, 6),
]

# dsh-pet 侧动画名（中文随便起，文件名对上就行）
A_IDLE = "待机呼吸"
A_WALK = "向右走"
A_WALK_BACK = "向左走"
A_WAVE = "挥手回应"
A_JUMP = "跳跃欢呼"
A_FAIL = "垂头失败"
A_WAIT = "等待回复"
A_RUN = "忙碌工作"
A_REVIEW = "思考凝视"

# 每个 dsh-pet 动画：来源 atlas 状态、播放帧率、成品时长（秒）
#
# 帧率必须贴着 atlas 的原始节奏（一行就 4~8 格，原本是 8~12fps 的手绘循环）。
# 早期版本一律 24fps 再靠重复帧凑时长 —— 6 格 24fps 等于每格停 4 帧，
# 实际观感是 6fps 幻灯片，就是「卡顿」的来历。现在 fps 跟格子数走，
# 不够长就整段重复（循环本来就连续，重复 pass 不会顿）。
ANIM_SPEC = {
    A_IDLE: ("idle", 8, 2.4),
    A_WALK: ("running-right", 12, 2.0),
    A_WALK_BACK: ("running-left", 12, 2.0),
    A_WAVE: ("waving", 10, 1.6),
    A_JUMP: ("jumping", 12, 1.0),
    A_FAIL: ("failed", 8, 1.8),
    A_WAIT: ("waiting", 8, 1.6),
    A_RUN: ("running", 10, 1.6),
    A_REVIEW: ("review", 8, 2.4),
}

CONFIG_TEMPLATE = {
    "pets": [
        {
            "id": None,
            "name": None,
            "size": 462,  # 与内置 main 同宽，角色看起来一样大
            "balanceEnabled": False,
            "whisperEnabled": True,
            "workStatusEnabled": True,
            "fixedEnabled": False,
            "display": "desktop",
            "position": {"corner": "top-left", "marginX": 40, "marginY": 120},
        }
    ],
    "animations": {
        "idle": [A_IDLE],
        "turn": [A_WALK_BACK],
        "drag": [A_JUMP],
        "clicks": [A_WAVE, A_JUMP],
        "moves": {
            "default": {"minDist": 60, "maxDist": 240, "margin": 20, "leadSec": 2, "tailSec": 2},
            "actions": [{"name": A_WALK}],
        },
        "categories": [
            {"id": "小动作", "weight": 75, "actions": [A_IDLE, A_WAVE, A_REVIEW]},
        ],
        "events": {
            # 这几种宠物不开余额功能，但这 6 档必须填（空字符串非法）
            "balance": [A_IDLE] * 6,
            "whisper": [A_IDLE, A_WAVE, A_REVIEW],
            # 档位顺序固定：thinking / working / result / waiting / success / error
            "workStatus": [A_REVIEW, A_RUN, A_IDLE, A_WAIT, A_JUMP, A_FAIL],
        },
    },
    # idle + turn + move + 分类 = 100
    "animationWeights": {"idle": 10, "turn": 5, "move": 10},
}

WHISPER_PROMPTS = {
    "claude": (
        "你是主人桌面上的 Claude 小姐：橙金色长发、琥珀色眼睛，说话温柔知性、沉稳细腻，"
        "会捧着书认真想事情。碎碎念要随意自然、短短一句（20字以内），说人话不啰嗦，"
        "不要解释你自己，不要提你是 AI。"
    ),
    "gpt": (
        "你是主人桌面上的 GPT 娘：白紫色头发的龙娘，机灵、聪明、带点骄傲。"
        "碎碎念要随意自然、短短一句（20字以内），说人话不啰嗦，不要解释你自己，不要提你是 AI。"
    ),
}


def log(msg: str) -> None:
    print(msg, flush=True)


# codex-pets.net 对没有 User-Agent 的请求直接 403（urllib 默认就带 python-urllib/3.x，
# 被 Cloudflare 一类的前置挡掉）。带上明确 UA 才能取到 JSON。
UA = {"User-Agent": "nodus-codex-pet/1.0 (+https://github.com/DNMCJH/nodus)"}


def http_json(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def http_bytes(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
        return r.read()


def cache_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "nodus-codex-pets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch_pet(pet_id: str) -> tuple[dict, Path]:
    """取 pet.json + spritesheet，返回 (manifest, spritesheet 路径)。已缓存则直接复用。"""
    d = cache_dir() / pet_id
    manifest_file = d / "pet.json"
    sheet = d / "spritesheet.webp"
    if manifest_file.exists() and sheet.exists():
        log(f"缓存命中：{d}")
        return json.loads(manifest_file.read_text("utf-8")), sheet

    log(f"下载 {pet_id} …")
    blob = http_bytes(f"{API}/api/pets/{pet_id}/download")
    zip_path = cache_dir() / f"{pet_id}.zip"
    zip_path.write_bytes(blob)
    d.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if "pet.json" not in names or "spritesheet.webp" not in names:
            raise SystemExit(f"下载内容缺少 pet.json / spritesheet.webp：{names}")
        z.extract("pet.json", d)
        z.extract("spritesheet.webp", d)
    log(f"落到 {d}（{len(blob)} bytes）")
    return json.loads(manifest_file.read_text("utf-8")), sheet


def cell_is_empty(cell) -> bool:
    return cell.getbbox() is None


def slice_row(im, row: int, expected: int):
    frames = []
    last = -1
    for c in range(COLS):
        cell = im.crop((c * CELL_W, row * CELL_H, (c + 1) * CELL_W, (row + 1) * CELL_H))
        if not cell_is_empty(cell):
            last = c
            frames.append(cell)
        elif last >= 0:
            # 行内出现空洞就停：后面的是别的状态，不是这一行的尾巴
            break
    if not frames:
        raise SystemExit(f"第 {row} 行整行为空，图集可能不是标准 codex 格式")
    if len(frames) < expected:
        log(f"  第 {row} 行只检出 {len(frames)} 格（期望 {expected}），按实际帧数编码")
    return frames


def encode_webm(frames, out: Path, fps: int, seconds: float, ffmpeg: str) -> None:
    from PIL import Image

    # 用全集 bbox 决定缩放和基线，避免逐帧居中导致的抖动
    bbox = None
    for f in frames:
        b = f.getbbox()
        if b is None:
            continue
        bbox = b if bbox is None else (
            min(bbox[0], b[0]), min(bbox[1], b[1]), max(bbox[2], b[2]), max(bbox[3], b[3])
        )
    if bbox is None:
        raise SystemExit("全部帧为空")
    content_h = max(1, bbox[3] - bbox[1])
    content_w = max(1, bbox[2] - bbox[0])
    scale = min(CHAR_HEIGHT / content_h, (CANVAS_W - 40) / content_w)

    reps = max(1, round(seconds * fps / len(frames)))
    tmp = Path(tempfile.mkdtemp(prefix="nodus-frames-"))
    try:
        idx = 0
        for _ in range(reps):
            for f in frames:
                canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
                cropped = f.crop(bbox)
                new_w = max(1, round(cropped.width * scale))
                new_h = max(1, round(cropped.height * scale))
                scaled = cropped.resize((new_w, new_h), Image.LANCZOS)
                x = (CANVAS_W - new_w) // 2
                y = BASELINE_Y - new_h
                canvas.alpha_composite(scaled, (x, y))
                canvas.save(tmp / f"f_{idx:04d}.png")
                idx += 1

        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-framerate", str(fps),
            "-i", str(tmp / "f_%04d.png"),
            "-c:v", "libvpx-vp9",
            "-pix_fmt", "yuva420p",
            "-auto-alt-ref", "0",
            # crf 23：源只有 192×208，压太狠（32）会先把线稿糊掉，再放大就脏了
            "-b:v", "0", "-crf", "23",
            "-row-mt", "1",
            # 线程压到 2：本机可用内存在 1 GB 上下，libvpx 默认开满线程时会
            # STATUS_STACK_OVERFLOW（0xC00000FD）直接崩，编码不报错也没产物。
            "-threads", "2",
            "-an",
            str(out),
        ]
        subprocess.run(cmd, check=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    log(f"  {out.name}  {idx} 帧 @{fps}fps / {idx / fps:.2f}s / {out.stat().st_size} bytes")


def dsh_home(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    env = os.environ.get("DSH_HOME")
    if env:
        return Path(env)
    return Path.home() / ".dsh"


def cmd_list(query: str) -> None:
    data = http_json(f"{API}/api/pets?q={urllib.parse.quote(query)}")
    pets = data.get("pets", [])
    log(f"共 {data.get('total', len(pets))} 个，列出前 {len(pets)} 个：\n")
    for p in pets:
        v = p.get("spriteVersionNumber", "?")
        log(f"  {p['id']:<24} v{v}  {p.get('displayName', '')}  ({p.get('viewCount', 0)} 浏览 / {p.get('likeCount', 0)} 赞)")
        desc = (p.get("description") or "").strip().replace("\n", " ")
        if desc:
            log(f"  {'':<24} {desc[:70]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="codex-pets 图集 → dsh-pet 种类")
    ap.add_argument("--list", metavar="QUERY", help="搜索 codex-pets 上的宠物，不写文件")
    ap.add_argument("--pet", help="codex-pets 的 pet id，如 claude-chan")
    ap.add_argument("--kind", help="dsh-pet 种类名（= 配置文件名前缀 = 素材根），如 claude")
    ap.add_argument("--name", help="桌面上显示的名字")
    ap.add_argument("--size", type=int, default=462, help="宠物宽度 px（默认 462，与内置 main 同宽）")
    ap.add_argument("--corner", default="top-left",
                    choices=["top-left", "top-right", "bottom-left", "bottom-right"])
    ap.add_argument("--margin-x", type=int, default=40)
    ap.add_argument("--margin-y", type=int, default=120)
    ap.add_argument("--display", default="desktop", choices=["desktop", "web", "both", "none"])
    ap.add_argument("--dsh-home", help="DSH_HOME（默认读环境变量，再退回 ~/.dsh）")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--dry-run", action="store_true", help="只编码到临时目录，不写进 DSH")
    args = ap.parse_args()

    if args.list:
        cmd_list(args.list)
        return 0
    if not (args.pet and args.kind and args.name):
        ap.error("要么 --list QUERY，要么同时给 --pet / --kind / --name")

    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        raise SystemExit("需要 Pillow：python -m pip install pillow")

    from PIL import Image

    manifest, sheet = fetch_pet(args.pet)
    im = Image.open(sheet).convert("RGBA")
    rows = im.height // CELL_H
    cols = im.width // CELL_W
    if cols != COLS:
        raise SystemExit(f"图集宽 {im.width} 不是 {COLS} 列 × {CELL_W}：可能不是 codex 图集")
    if rows < 9:
        raise SystemExit(f"图集只有 {rows} 行，少于 9 个状态行")

    sl = "v2（含 16 向注视格，已丢弃第 10/11 行）" if rows >= 11 else "v1"
    log(f"{args.pet}「{manifest.get('displayName', '')}」{im.width}×{im.height}，{rows} 行 → {sl}")

    home = dsh_home(args.dsh_home)
    pet_dir = home / "dsh-pet" / "pet"
    # 平铺，不要 webm/ 子目录：host 侧对 pet pack 走
    #   extraAnimDir = resolveAsset(petConfigDir, petId + '-animation')
    #   resolveExisting(extraAnimDir, fileName)
    # 文件名直接拼在 <种类>-animation/ 下。放到 <种类>-animation/webm/ 里 URL 会 404
    # ——配置注释里的「放 webm/ 子目录」只对 main-animation 成立，pet pack 不适用。
    anim_dir = pet_dir / f"{args.kind}-animation"

    # 先编码到临时目录再搬过去：桌宠窗口正在播的 webm 会被 Electron 占着，
    # 直接覆盖写会 EBUSY（ffmpeg 退出码 3199971767）。临时目录编码完再 os.replace，
    # 失败时至少不会留下半个文件把正在播的动画弄坏。
    if args.dry_run:
        out_dir = Path(tempfile.mkdtemp(prefix=f"nodus-pet-{args.kind}-"))
        log(f"dry-run：编码到 {out_dir}")
    else:
        anim_dir.mkdir(parents=True, exist_ok=True)
        out_dir = Path(tempfile.mkdtemp(prefix=f"nodus-pet-{args.kind}-"))

    row_frames = {}
    for state, row, expected in ATLAS_STATES:
        log(f"切行 {row} = {state}")
        row_frames[state] = slice_row(im, row, expected)

    for anim_name, (state, fps, seconds) in ANIM_SPEC.items():
        encode_webm(row_frames[state], out_dir / f"{anim_name}.webm", fps, seconds, args.ffmpeg)

    if not args.dry_run:
        locked = []
        for f in sorted(out_dir.glob("*.webm")):
            try:
                # 不能 os.replace：临时目录在 C:、DSH_HOME 常在别的盘，
                # 跨卷 rename 直接 OSError（系统无法将文件移到不同的磁盘驱动器）。
                shutil.move(str(f), str(anim_dir / f.name))
            except OSError as e:
                locked.append(f"{f.name}（{e.strerror or e}）")
        if locked:
            log("")
            log("这些文件被占用，没换成功（桌宠正在播它们）：")
            for name in locked:
                log("  " + name)
            log("先停掉桌宠再重跑，或者直接重启 DSH 页面后重跑。")
            return 1
        shutil.rmtree(out_dir, ignore_errors=True)

    cfg = json.loads(json.dumps(CONFIG_TEMPLATE))  # 深拷贝
    pet = cfg["pets"][0]
    pet["id"] = args.kind
    pet["name"] = args.name
    pet["size"] = args.size
    pet["display"] = args.display
    pet["position"] = {"corner": args.corner, "marginX": args.margin_x, "marginY": args.margin_y}
    prompt = WHISPER_PROMPTS.get(args.kind)
    if prompt:
        cfg = {"whisperPrompt": prompt, **cfg}

    if args.dry_run:
        (out_dir / f"{args.kind}-config.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", "utf-8")
        log(f"dry-run 完成：{out_dir}")
        return 0

    cfg_path = pet_dir / f"{args.kind}-config.json"
    # 先写临时文件再改名：dsh-pet 每次请求都重读配置，写一半会被解析成「配置损坏」
    tmp_cfg = cfg_path.with_suffix(".tmp")
    tmp_cfg.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(tmp_cfg, cfg_path)

    total = sum(f.stat().st_size for f in anim_dir.glob("*.webm"))
    log("")
    log(f"配置：{cfg_path}")
    log(f"素材：{anim_dir}（{len(list(anim_dir.glob('*.webm')))} 个 webm，{total / 1e6:.2f} MB）")
    log(f"来源：{API}/pets/{args.pet}（{manifest.get('displayName', '')}）")
    log("")
    log("生效方式：刷新 DSH 页面即可（dsh-pet 每次请求重读配置），不用重启。")
    log(f"验证：curl http://127.0.0.1:3080/dsh-pet-7340/config.jsonc   # 应出现 {args.kind} 条目")
    return 0


if __name__ == "__main__":
    sys.exit(main())
