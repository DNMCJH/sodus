#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Coopanion 形象包 → dsh-pet 动画（VP9 + alpha webm）。

Coopanion 的形象包不是逐帧序列，也不是精灵图，是「分层 PNG + 枢轴 + 运行时网格变形」：

    figure.json   名称 / 动作词表（18 动作 19 表情，带 seconds 时长）
    model.json    每个分片在 1024 空间的 box、z 序、父骨名，以及 15 个转动枢轴
    tex/*.png     分片，已按 box 精确裁切
    figure.js     参数化渲染器：draw(group, face, o) —— 只消费外部传进来的 o

关键限制（详见 COOPANION.md）：**18 个动作的编排不在包里**。figure.js 自己不算动作，
它从 `opts.kit`（Coopanion 应用本体）接收 `o = {t, sit, swing, look, gesture, legs, mode}`。
所以本脚本能做的是：用作者给的分片几何 / 枢轴 / 层级，按动作语义重写时间曲线。
产出的是「同一套皮、动作合理」的动画，不是 Coopanion 原版动作曲线的导出。

用法：
    python coopanion_to_dsh.py --pack <包目录> --out <输出目录> --actions idle,wave
    python coopanion_to_dsh.py --pack ... --list          # 只列包里的动作，不渲染
    python coopanion_to_dsh.py --pack ... --actions all --frames-dir <目录>

只写 --out 指定的目录，不碰 dsh-pet 的真实配置。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# 画布规格：与 dsh-pet 现有角色对齐
# ---------------------------------------------------------------------------
CANVAS_W, CANVAS_H = 960, 540
FPS = 24
# 角色高度占画布 75%（540 × 0.75 = 405），底边落在 y=495
CHAR_RATIO = 0.75
BASELINE_RATIO = 495 / 540
SUPERSAMPLE = 2  # 合成时 2× 超采样再降回目标尺寸，抗锯齿


def log(msg: str) -> None:
    print(f"[coopanion] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 2×3 仿射矩阵：(a, b, c, d, e, f) 作用为 (x,y) -> (a x + b y + c, d x + e y + f)
# ---------------------------------------------------------------------------
Mat = tuple


def mat_mul(m: Mat, n: Mat) -> Mat:
    """先作用 n，再作用 m。"""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (
        a * A + b * D, a * B + b * E, a * C + b * F + c,
        d * A + e * D, d * B + e * E, d * C + e * F + f,
    )


def mat_apply(m: Mat, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return a * x + b * y + c, d * x + e * y + f


def mat_inv(m: Mat) -> Mat:
    a, b, c, d, e, f = m
    det = a * e - b * d
    if abs(det) < 1e-12:
        raise ValueError("退化矩阵，无法求逆")
    ia, ib = e / det, -b / det
    id_, ie = -d / det, a / det
    return ia, ib, -(ia * c + ib * f), id_, ie, -(id_ * c + ie * f)


IDENT: Mat = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)


def bone_matrix(pivot, a=0.0, sx=1.0, sy=1.0, tx=0.0, ty=0.0) -> Mat:
    """绕 pivot 旋转 a 度、缩放 (sx, sy)，再平移 (tx, ty)。角度按屏幕坐标（y 向下）顺时针为正。"""
    px, py = pivot
    rad = math.radians(a)
    ca, sa = math.cos(rad), math.sin(rad)
    rot = (ca * sx, -sa * sy, 0.0, sa * sx, ca * sy, 0.0)
    m = mat_mul(rot, (1, 0, -px, 0, 1, -py))
    m = mat_mul((1, 0, px, 0, 1, py), m)
    return mat_mul((1, 0, tx, 0, 1, ty), m)


# ---------------------------------------------------------------------------
# 数值小工具（与 figure.js 里的 clamp / smooth 一致）
# ---------------------------------------------------------------------------
def clamp(x: float, a: float, b: float) -> float:
    return max(a, min(b, x))


def smooth(a: float, b: float, x: float) -> float:
    k = clamp((x - a) / (b - a), 0.0, 1.0)
    return k * k * (3 - 2 * k)


def lerp(a: float, b: float, k: float) -> float:
    return a + (b - a) * k


# ---------------------------------------------------------------------------
# 包加载
# ---------------------------------------------------------------------------
@dataclass
class Part:
    id: str
    tex: str            # 相对包根的贴图路径
    box: tuple          # (x, y, w, h) —— view 空间
    z: float
    bone: str           # 父骨名
    px_w: int
    px_h: int
    alpha: float = 1.0


@dataclass
class Pack:
    root: Path
    pid: str
    name: str
    vocab: list
    parts: list = field(default_factory=list)
    pivots: dict = field(default_factory=dict)      # bone -> (x, y) in view space
    bones: dict = field(default_factory=dict)       # bone -> {"parent": str|None}
    view_scale: float = 1.0                         # 分片像素 -> view 单位


# figure.js 里 deformers 的三种写法，按出现顺序匹配
RE_ROT_CALL = re.compile(r"(\w+)\s*:\s*rot\(\s*'([\w-]+)'(?:\s*,\s*'([\w-]+)')?\s*\)")
RE_WARP_CALL = re.compile(r"(\w+)\s*:\s*warp\(\s*'([\w-]+)'")
RE_OBJ = re.compile(r"(\w+)\s*:\s*\{\s*kind\s*:\s*'(rot|warp)'((?:[^{}])*)\}")
RE_PARENT = re.compile(r"parent\s*:\s*'([\w-]+)'")


def parse_bone_tree(figure_js: Path, pivots: dict) -> dict:
    """从 figure.js 的 deformers 块里抽出骨层级。抽不到就退回扁平结构。"""
    bones: dict[str, dict] = {name: {"parent": None} for name in pivots}
    if not figure_js.exists():
        return bones
    src = figure_js.read_text(encoding="utf-8", errors="replace")

    for m in RE_ROT_CALL.finditer(src):
        name, _piv, parent = m.group(1), m.group(2), m.group(3)
        bones.setdefault(name, {"parent": None})
        if parent:
            bones[name]["parent"] = parent
    for m in RE_WARP_CALL.finditer(src):
        name, parent = m.group(1), m.group(2)
        bones.setdefault(name, {"parent": None})
        bones[name]["parent"] = parent
    for m in RE_OBJ.finditer(src):
        name, _kind, body = m.group(1), m.group(2), m.group(3)
        bones.setdefault(name, {"parent": None})
        pm = RE_PARENT.search(body)
        if pm:
            bones[name]["parent"] = pm.group(1)

    # 父骨必须存在，否则挂到根上
    for name, info in bones.items():
        if info["parent"] and info["parent"] not in bones:
            log(f"  警告：骨 {name} 的父骨 {info['parent']} 未定义，按根骨处理")
            info["parent"] = None
    return bones


def load_pack(pack_dir: Path) -> Pack:
    fig_path = pack_dir / "figure.json"
    mdl_path = pack_dir / "model.json"
    if not fig_path.exists() or not mdl_path.exists():
        raise SystemExit(f"{pack_dir} 不像 Coopanion 形象包：缺 figure.json 或 model.json")

    fig = json.loads(fig_path.read_text(encoding="utf-8-sig"))
    mdl = json.loads(mdl_path.read_text(encoding="utf-8-sig"))

    pid = fig.get("id") or pack_dir.name
    name = (fig.get("name") or {}).get("zh") or pid
    vocab = fig.get("vocab") or []

    units = mdl.get("units") or {}
    # dragon 系：box 在 1024 像素空间，用 scale/centerX/feetY 映射到 view 空间
    # claude 系：box 已经在 view 空间（以 S/X0/FEET 表达），直接当 view 用
    if "source" in mdl and "scale" in units:
        S = float(units["scale"])
        cx = float(units.get("centerX", 0))
        fy = float(units.get("feetY", 0))

        def to_view(x, y):
            return 128.0 + (x - cx) * S, 256.0 + (y - fy) * S
        view_scale = S
    else:
        S = float(units.get("S", 1.0) or 1.0)
        def to_view(x, y):
            return float(x), float(y)
        view_scale = 1.0

    pack = Pack(root=pack_dir, pid=pid, name=name, vocab=vocab, view_scale=view_scale)

    # 枢轴
    raw_pivots = mdl.get("pivots") or {}
    for bone, xy in raw_pivots.items():
        pack.pivots[bone] = to_view(float(xy[0]), float(xy[1]))

    # 分片
    for p in mdl.get("parts") or []:
        tex = p.get("src") or p.get("tex")
        if not tex:
            continue
        if not tex.lower().endswith(".png"):
            tex = f"tex/{tex}.png"
        fpath = pack_dir / tex.replace("/", "\\")
        if not fpath.exists():
            log(f"  警告：{p.get('id')} 的贴图缺失：{tex}")
            continue
        with Image.open(fpath) as im:
            pw, ph = im.size
        bx, by, bw, bh = (float(v) for v in p["box"])
        x0, y0 = to_view(bx, by)
        x1, y1 = to_view(bx + bw, by + bh)
        bone = p.get("parent") or ""
        # features 是 figure.js 里画眼睛/嘴的离屏 canvas，挂在 head 上；
        # 它的 box 和这些分片的 box 同处一个坐标系，所以直接挂 head 等价。
        if bone == "features":
            bone = "head"
        pack.parts.append(Part(
            id=p.get("id") or Path(tex).stem,
            tex=tex,
            box=(x0, y0, x1 - x0, y1 - y0),
            z=float(p.get("z", 0)),
            bone=bone,
            px_w=pw,
            px_h=ph,
        ))

    if not pack.parts:
        raise SystemExit("model.json 里没有可用的分片")

    pack.bones = parse_bone_tree(pack_dir / "figure.js", pack.pivots)
    # 分片引用到的骨若不在树里，补一个根骨
    for part in pack.parts:
        pack.bones.setdefault(part.bone, {"parent": None})
    return pack


# ---------------------------------------------------------------------------
# 骨世界矩阵
# ---------------------------------------------------------------------------
def world_matrices(pack: Pack, pose: dict, root: Mat = IDENT) -> dict:
    """pose[bone] = {"a","sx","sy","tx","ty"}（view 单位），返回 bone -> 世界矩阵。"""
    out: dict[str, Mat] = {}
    visiting: set[str] = set()

    def resolve(bone: str) -> Mat:
        if bone in out:
            return out[bone]
        if bone in visiting:  # 环
            return root
        visiting.add(bone)
        info = pack.bones.get(bone) or {"parent": None}
        parent_mat = resolve(info["parent"]) if info["parent"] else root
        st = pose.get(bone) or {}
        local = bone_matrix(
            pack.pivots.get(bone, (0.0, 0.0)),
            a=float(st.get("a", 0.0)),
            sx=float(st.get("sx", 1.0)),
            sy=float(st.get("sy", 1.0)),
            tx=float(st.get("tx", 0.0)),
            ty=float(st.get("ty", 0.0)),
        )
        out[bone] = mat_mul(parent_mat, local)
        visiting.discard(bone)
        return out[bone]

    for bone in pack.bones:
        resolve(bone)
    return out


def role_of(bone: str) -> str:
    """把不同包的骨名归一到同一组角色上。"""
    n = bone.lower().replace("-", "").replace("_", "")
    if "wing" in n:
        return "wingFar" if ("far" in n or "back" in n) else "wingNear"
    if "tail" in n:
        return "tail"
    if "ahoge" in n:
        return "ahoge"
    if "hair" in n:
        return "hairBack" if "back" in n else "hairFront"
    if "arm" in n:
        return "armFar" if ("far" in n or "back" in n) else "armNear"
    if "leg" in n:
        return "legFar" if ("far" in n or "back" in n) else "legNear"
    if "skirt" in n and "sit" not in n:
        return "skirtFar" if "far" in n else "skirtNear"
    if "seat" in n or n.endswith("sit"):
        return "seat"
    if "head" in n:
        return "head"
    if "apron" in n:
        return "apron"
    if n in ("upper", "body", "waist", "torso", "hip", "root"):
        return "torso"
    return "other"


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------
# 期望：给定归一化进度 k∈[0,1] 和秒数 t，产出 kit 层参数 o
#   o = {swing, look(x,y), sit, mode, gesture{kind,k}, face, blink}
@dataclass
class Ctx:
    t: float
    k: float
    dur: float
    pack: Pack


def _gesture(kind: str, k: float) -> dict:
    return {"kind": kind, "k": k}


def a_idle(c: Ctx) -> dict:
    """dsh-pet 的 idle 槽要能无缝循环：4s 为周期，所有频率取 4s 的整数倍，首尾才接得上。"""
    t = c.t
    w = 2 * math.pi * t / c.dur
    return {
        "swing": 5.5 * math.sin(w) + 2.0 * math.sin(2 * w + 0.7),
        "look": (2.2 * math.sin(w + 0.4), 1.4 * math.sin(2 * w + 1.0)),
        "face": "neutral",
        "blink": 0.0,
    }


def a_neutral(c: Ctx) -> dict:
    return {"swing": 0.0, "look": (0.0, 0.0), "face": "neutral"}


def a_wave(c: Ctx) -> dict:
    """招手：figure.js 里 armNear 的公式就是 -wave*104°，把 k 交出去就行。"""
    t = c.t
    return {
        "swing": 4.0 * math.sin(t * 1.6),
        "look": (3.5, -1.0),
        "face": "happy",
        "gesture": _gesture("wave", c.k),
        "root_dy": 0.0,
    }


def a_nod(c: Ctx) -> dict:
    return {"swing": 0.0, "look": (0.0, 0.0), "face": "neutral",
            "gesture": _gesture("nod", c.k)}


def a_shake(c: Ctx) -> dict:
    return {"swing": 0.0, "look": (0.0, 0.0), "face": "neutral",
            "gesture": _gesture("shake", c.k)}


def a_bow(c: Ctx) -> dict:
    return {"swing": 0.0, "look": (0.0, 2.0), "face": "sleepy",
            "gesture": _gesture("bow", c.k)}


def a_sit(c: Ctx) -> dict:
    return {"swing": 0.0, "look": (0.0, 1.0), "face": "neutral", "sit": smooth(0.0, 0.75, c.k)}


def a_stand(c: Ctx) -> dict:
    return {"swing": 0.0, "look": (0.0, 0.0), "face": "neutral", "sit": 1.0 - smooth(0.15, 0.9, c.k)}


def a_sleep(c: Ctx) -> dict:
    return {"swing": 1.2 * math.sin(c.t * 0.7), "look": (0.0, 2.0),
            "face": "sleepy", "sit": 1.0, "mode": "sleep", "eye_close": 1.0}


def a_look(c: Ctx) -> dict:
    t = c.t
    return {"swing": 1.5 * math.sin(t * 1.2), "face": "neutral",
            "look": (11.0 * math.sin(t * 2.2), 1.5 * math.sin(t * 1.7))}


def a_jump(c: Ctx) -> dict:
    """原地跳：竖直位移是 kit 干的（挪窗口），包里没有对应钩子，这里自己补根位移。"""
    e = math.sin(math.pi * c.k)
    squash = 1.0 - 0.10 * max(0.0, 1 - c.k * 6.0)
    return {"swing": -6.0 * e, "look": (0.0, -2.0), "face": "surprised",
            "root_dy": -46.0 * e, "root_sy": 1.0 + 0.05 * e - (1 - squash)}


def a_hop(c: Ctx) -> dict:
    e = math.sin(math.pi * c.k)
    return {"swing": 0.0, "look": (0.0, -1.0), "face": "happy",
            "root_dy": -18.0 * e}


def a_spin(c: Ctx) -> dict:
    """转圈：包里的 figure.js 没有绕竖轴旋转的钩子，只能整身平面转 + 横向压扁假装透视。"""
    k = c.k
    ang = 360.0 * k
    depth = math.cos(math.radians(ang))
    return {"swing": 0.0, "look": (0.0, 0.0), "face": "happy",
            "root_rot": ang, "root_sx": max(0.18, abs(depth)) * (1 if depth >= 0 else -1)}


def a_turn(c: Ctx) -> dict:
    return {"swing": 0.0, "look": (0.0, 0.0), "face": "neutral",
            "root_sx": math.cos(math.pi * c.k)}


def a_shiver(c: Ctx) -> dict:
    t = c.t
    return {"swing": 3.0 * math.sin(t * 26.0), "look": (0.0, 0.5), "face": "scared",
            "shake_note": "手发抖用 swing 高频抖动表达"}


def a_flap(c: Ctx) -> dict:
    e = math.sin(math.pi * c.k)
    return {"swing": 2.0 * math.sin(c.t * 6.0), "look": (0.0, -1.5), "face": "excited",
            "gesture": _gesture("flap", c.k), "root_dy": -14.0 * e}


def a_dizzy(c: Ctx) -> dict:
    t = c.t
    return {"swing": 7.0 * math.sin(t * 2.6), "look": (6.0 * math.sin(t * 3.1), 0.0),
            "face": "confused", "mode": "dance"}


def a_dance(c: Ctx) -> dict:
    t = c.t
    return {"swing": 9.0 * math.sin(t * 3.4), "look": (3.0 * math.sin(t * 1.7), 0.0),
            "face": "happy", "mode": "dance"}


def _walk_like(c: Ctx, run: bool) -> dict:
    """腿的点由 kit 提供：o.legs = [[hipx,hipy,footx,footy], ...]（view 空间，脚跟 y 越大越往下）。"""
    t = c.t
    period = 0.44 if run else 0.62
    ph = t / period * 2 * math.pi
    amp = 17.0 if run else 12.0
    legs = []
    for i in (0, 1):
        p = ph + (math.pi if i else 0.0)
        hx = 124.0 + (i * 8)
        hy = 208.0
        fx = hx + amp * math.sin(p)
        fy = 256.0 - max(0.0, 9.0 * math.sin(p + math.pi / 2)) * (1.6 if run else 1.0)
        legs.append([hx, hy, fx, fy])
    return {"swing": (5.0 if run else 3.0) * math.sin(ph),
            "look": (0.0, 0.0), "face": "neutral",
            "mode": "run" if run else "walk", "legs": legs}


def a_walk(c: Ctx) -> dict:
    return _walk_like(c, run=False)


def a_run(c: Ctx) -> dict:
    return _walk_like(c, run=True)


ACTIONS = {
    # dsh-pet 槽名 / 语义名 -> (生成器, 是否内置, 秒数覆盖)
    "idle": (a_idle, True, 4.0),
    "neutral": (a_neutral, False, None),
    "wave": (a_wave, False, None),
    "nod": (a_nod, False, None),
    "shake": (a_shake, False, None),
    "bow": (a_bow, False, None),
    "sit": (a_sit, False, None),
    "stand": (a_stand, False, None),
    "sleep": (a_sleep, False, None),
    "look": (a_look, False, None),
    "jump": (a_jump, False, None),
    "hop": (a_hop, False, None),
    "spin": (a_spin, False, None),
    "turn": (a_turn, False, None),
    "shiver": (a_shiver, False, None),
    "flap": (a_flap, False, None),
    "dizzy": (a_dizzy, False, None),
    "dance": (a_dance, False, None),
    "walk": (a_walk, False, 6.0),
    "run": (a_run, False, 6.0),
}


@dataclass
class Motion:
    name: str
    seconds: float
    gen: object
    face: str = "neutral"


def resolve_actions(pack: Pack, want: list[str]) -> list[Motion]:
    """把 --actions 里的名字（包内 id / 语义名）解析成 Motion 列表。"""
    by_id = {v.get("id"): v for v in pack.vocab}
    out: list[Motion] = []
    seen = set()
    for raw in want:
        q = raw.strip()
        if not q:
            continue
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        meta = by_id.get(q)
        gen, builtin, override = ACTIONS.get(key, (None, False, None))
        if gen is None:
            log(f"  跳过 {q}：包里没有这个动作，转换器也没有对应实现")
            continue
        secs = override or (float(meta.get("seconds", 1.0)) if meta else 1.0)
        if meta and override:
            log(f"  {q}: 包内标注 {meta.get('seconds')}s，按 {secs}s 出片（{override}s 覆盖）")
        out.append(Motion(name=q, seconds=secs, gen=gen, face=meta.get("names", {}).get("zh", [""])[0] if meta else ""))
    return out


# ---------------------------------------------------------------------------
# figure.js draw() 的 state 计算，移植到 Python（view 空间）
# ---------------------------------------------------------------------------
class Rig:
    def __init__(self, pack: Pack, flip: bool = True):
        self.pack = pack
        self.S = pack.view_scale
        self.flip = flip
        self.springs = {"hair": [0.0, 0.0], "tail": [0.0, 0.0], "wing": [0.0, 0.0]}
        self.role_bone: dict[str, str] = {}
        for bone in pack.bones:
            r = role_of(bone)
            if r != "other" and r not in self.role_bone:
                self.role_bone[r] = bone
            elif r != "other":
                self.role_bone.setdefault(r, bone)

    def _spring(self, key: str, target: float, dt: float) -> float:
        v = self.springs[key]
        v[1] += ((target - v[0]) * 65 - v[1] * 13) * dt
        v[0] += v[1] * dt
        return v[0]

    def reset(self) -> None:
        for v in self.springs.values():
            v[0] = v[1] = 0.0

    def evaluate(self, o: dict, t: float, dt: float) -> tuple[dict, dict, Mat]:
        """返回 (pose, alpha_map, root_matrix)。o 是 kit 参数。"""
        S = self.S
        sit = clamp(float(o.get("sit", 0.0)), 0.0, 1.0)
        seat = smooth(0.46, 0.54, sit)
        unseat = 1.0 - smooth(0.54, 0.62, sit)
        low = sit * 54.0 * S

        mode = o.get("mode") or "idle"
        facial = o.get("face") or "neutral"
        g = o.get("gesture") or {}
        k = float(g.get("k", 0.0))
        env = math.sin(math.pi * k)
        kind = g.get("kind")
        nod = math.sin(k * math.pi * 4) * env if kind == "nod" else 0.0
        shake = math.sin(k * math.pi * 6) * env if kind == "shake" else 0.0
        bow = math.sin(math.pi * k) ** 2 if kind == "bow" else 0.0
        wave = (smooth(0.0, 0.16, k) * (1 - smooth(0.78, 1.0, k))) if kind == "wave" else 0.0
        flap = (math.sin(k * math.pi * 10) * env) if kind == "flap" else 0.0

        cheerful = facial in ("happy", "love", "excited")
        drooping = facial in ("sad", "sleepy", "sleep", "cry", "scared", "worried")
        swing = float(o.get("swing", 0.0))

        hair = self._spring("hair", clamp(swing * 0.055, -2, 2) + math.sin(t * 1.8) * 0.55, dt)
        wag = 0.15 if drooping else (1.0 if cheerful else 0.55)
        tail = (self._spring("tail", clamp(swing * 0.09, -3.5, 3.5) - (2.0 if drooping else 0.0), dt)
                + wag * 6.5 * math.sin(t * (2.3 + 2.3 * wag)) + math.sin(t * 0.85) * 1.8
                + flap * 14 * math.sin(t * 17))
        wing_mood = 1.0 if (cheerful or facial == "smug") else (-0.7 if drooping else 0.0)
        wing = (self._spring("wing", wing_mood * 8 + clamp(swing * 0.12, -4, 4), dt)
                + math.sin(t * 2.3) * 4.2 + math.sin(t * 0.72 + 1.1) * 1.7)

        look = o.get("look") or (0.0, 0.0)
        look_x = clamp(-float(look[0]) * 0.12, -0.9, 0.9)
        look_y = clamp(float(look[1]) * 0.14, -0.8, 0.8)
        nod_low = nod * 1.2 + (1.5 if mode == "sleep" else 0.0)
        walking = mode in ("walk", "run")
        running = mode == "run"

        # 角色 -> 骨名
        def B(role: str) -> str | None:
            return self.role_bone.get(role)

        pose: dict[str, dict] = {}

        def put(role: str, **kw) -> None:
            bone = B(role)
            if bone:
                pose.setdefault(bone, {}).update(kw)

        put("torso", ty=low, a=-bow * 15.0)
        put("head", a=nod * 2.8 + shake * 2.0 + (3.0 if facial == "confused" else 0.0),
            tx=look_x + shake * 0.7, ty=look_y + nod_low)
        # 头发在包里是 warp 变形（按网格横向推），最小合成器只做绕枢轴转 + 平移，形状有差
        put("hairBack", a=hair * 1.2, tx=hair * 0.35, ty=math.sin(t * 1.5) * 0.15)
        put("hairFront", a=hair * 0.9, tx=hair * 0.30)
        put("ahoge", a=hair * 2.5 + math.sin(t * 2.0) * 1.4 + flap * 5.0)
        put("tail", a=tail, ty=low * 0.25)
        put("wingFar", a=wing + flap * 9.0, sx=1.0 - abs(flap) * 0.2)
        put("wingNear", a=-wing - flap * 9.0, sx=1.0 - abs(flap) * 0.2)
        put("legFar", a=0.0)
        put("legNear", a=0.0)
        put("armFar", a=math.sin(t * 1.7) * 1.2)
        put("armNear", a=-wave * (104.0 + math.sin(k * math.pi * 12) * 9.0) - math.sin(t * 1.7) * 1.2)
        put("skirtFar", a=hair * 0.25, ty=low * 0.55)
        put("skirtNear", a=-hair * 0.25, ty=low * 0.55)
        put("apron", a=hair * 0.15, ty=low)
        put("seat", ty=low)

        alpha = {
            "sit": seat, "seat": seat,
            "tail": unseat, "leg-far": unseat, "leg-near": unseat,
            "skirt-far": unseat, "skirt-near": unseat, "apron": unseat,
            "wing-far": unseat, "wing-near": unseat,
        }

        if walking or mode in ("dance", "drag", "air"):
            legs = o.get("legs") or [[124, 208, 124, 256], [132, 208, 132, 256]]
            seg = [math.degrees(math.atan2(l[2] - l[0], max(4.0, l[3] - l[1]))) for l in legs]
            for i, role in enumerate(("legFar", "legNear")):
                leg = legs[i]
                ang = clamp(seg[i] * 0.8, -23, 23) if running else seg[i]
                put(role, a=ang * (1 - sit), ty=-max(0.0, 241.0 - leg[3]) * 0.2 * (1 - sit))
            if not wave:
                far = B("armFar")
                near = B("armNear")
                if far:
                    pose.setdefault(far, {})["a"] = pose.get(far, {}).get("a", 0.0) - seg[0] * 1.3
                if near:
                    pose.setdefault(near, {})["a"] = pose.get(near, {}).get("a", 0.0) - seg[1] * 1.3
            put("skirtFar", a=pose.get(B("skirtFar") or "", {}).get("a", 0.0) + pose.get(B("legFar") or "", {}).get("a", 0.0) * 0.15)
            put("skirtNear", a=pose.get(B("skirtNear") or "", {}).get("a", 0.0) + pose.get(B("legNear") or "", {}).get("a", 0.0) * 0.15)

        if running and B("armNear") and B("armFar"):
            pose[B("armNear")]["a"] = lerp(pose[B("armNear")].get("a", 0.0), -38.0, 1.0)
            pose[B("armFar")]["a"] = lerp(pose[B("armFar")].get("a", 0.0), -32.0, 1.0)

        # 根变换：包里没有，按需在动作里给（跳/转圈/转身靠它）
        rflip = -1.0 if self.flip else 1.0
        rx = float(o.get("root_dx", 0.0)) * rflip
        ry = float(o.get("root_dy", 0.0))
        rrot = float(o.get("root_rot", 0.0))
        rsx = float(o.get("root_sx", 1.0))
        rsy = float(o.get("root_sy", 1.0))
        base = (128.0, 256.0)  # view 空间的脚底中点
        root = mat_mul(
            (1, 0, rx, 0, 1, ry),
            bone_matrix(base, a=rrot, sx=rsx, sy=rsy),
        )
        return pose, alpha, root


# ---------------------------------------------------------------------------
# 合成
# ---------------------------------------------------------------------------
_tile_cache: dict[str, Image.Image] = {}


def load_tex(pack: Pack, part: Part) -> Image.Image:
    key = f"{pack.root}|{part.tex}"
    im = _tile_cache.get(key)
    if im is None:
        im = Image.open(pack.root / part.tex.replace("/", "\\")).convert("RGBA")
        _tile_cache[key] = im
    return im


def compose_frame(pack: Pack, pose: dict, alpha: dict, root: Mat,
                  fit: Mat, size: tuple[int, int]) -> Image.Image:
    W, H = size
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    world = world_matrices(pack, pose, root)

    for part in sorted(pack.parts, key=lambda p: p.z):
        # alpha：先按分片 id，再按骨名模糊匹配（包里的 alpha 键既可能是 id 也可能是角色名）
        a = alpha.get(part.id)
        if a is None:
            a = alpha.get(part.id.replace("_", "-"))
        if a is None:
            a = alpha.get(role_of(part.bone))
        a = 1.0 if a is None else float(a)
        if a <= 0.005:
            continue

        bx, by, bw, bh = part.box
        su = bw / part.px_w
        sv = bh / part.px_h
        m = mat_mul(fit, world.get(part.bone, IDENT))
        full = mat_mul(m, (su, 0.0, bx, 0.0, sv, by))

        corners = [mat_apply(full, u, v) for (u, v) in
                   ((0, 0), (part.px_w, 0), (0, part.px_h), (part.px_w, part.px_h))]
        xs = [p[0] for p in corners]
        ys = [p[1] for p in corners]
        x0 = max(0, int(math.floor(min(xs))))
        y0 = max(0, int(math.floor(min(ys))))
        x1 = min(W, int(math.ceil(max(xs))))
        y1 = min(H, int(math.ceil(max(ys))))
        if x1 <= x0 or y1 <= y0:
            continue

        inv = mat_inv(full)
        ia, ib, ic, id_, ie, if_ = inv
        coeffs = (
            ia, ib, ia * x0 + ib * y0 + ic,
            id_, ie, id_ * x0 + ie * y0 + if_,
        )
        tex = load_tex(pack, part)
        try:
            warped = tex.transform((x1 - x0, y1 - y0), Image.AFFINE, coeffs,
                                   resample=Image.BILINEAR, fillcolor=(0, 0, 0, 0))
        except Exception as exc:  # 极端角度下 Pillow 会抛，跳过这一片而不是整帧失败
            log(f"  警告：{part.id} 变换失败（{exc}），跳过")
            continue
        if a < 0.995:
            alpha_ch = warped.getchannel("A").point(lambda v: int(v * a))
            warped.putalpha(alpha_ch)
        canvas.alpha_composite(warped, (x0, y0))
    return canvas


# ---------------------------------------------------------------------------
# 拟合：把 view 空间的中立姿势量出 bbox，算到画布的缩放和偏移
# ---------------------------------------------------------------------------
def measure_neutral(pack: Pack, rig: Rig) -> tuple[float, float, float, float]:
    """返回中立姿势下 alpha>0 内容在 view 空间的 bbox (x0, y0, x1, y1)。"""
    pose, alpha, root = rig.evaluate({"face": "neutral"}, 0.0, 1 / 60)
    probe = mat_mul((4.0, 0.0, 0.0, 0.0, 4.0, 0.0), IDENT)
    size = (int(276 * 4), int(276 * 4))
    frame = compose_frame(pack, pose, alpha, root, probe, size)
    bb = frame.getbbox()
    if bb is None:
        raise SystemExit("中立姿势渲染出来是空的，分片几何可能不对")
    return bb[0] / 4.0, bb[1] / 4.0, bb[2] / 4.0, bb[3] / 4.0


def fit_matrix(bbox, canvas_w: int, canvas_h: int, ss: int, flip: bool = False) -> Mat:
    x0, y0, x1, y1 = bbox
    h = max(1e-6, y1 - y0)
    k = (canvas_h * CHAR_RATIO) / h
    if (x1 - x0) * k > canvas_w * 0.98:  # 太宽就按宽度收
        k = (canvas_w * 0.98) / (x1 - x0)
    cx = (x0 + x1) / 2.0
    bottom = canvas_h * BASELINE_RATIO
    ks = k * ss
    fit = (ks, 0.0, canvas_w * ss / 2.0 - cx * ks,
           0.0, ks, bottom * ss - y1 * ks)
    if flip:
        # Coopanion 运行时对整层做 translate(256 0) scale(-1 1)：原画朝左，屏幕上朝右。
        # 这里同样在最终画布上左右镜像，保持和它在应用里长一样。
        fit = mat_mul((-1.0, 0.0, canvas_w * ss, 0.0, 1.0, 0.0), fit)
    return fit


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------
def encode_webm(ffmpeg: str, frames_glob: str, out: Path, fps: int,
                crf: int = 26, threads: int = 2) -> None:
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-framerate", str(fps),
        "-i", frames_glob,
        "-c:v", "libvpx-vp9",
        "-pix_fmt", "yuva420p",
        "-auto-alt-ref", "0",
        "-b:v", "0", "-crf", str(crf),
        "-row-mt", "1",
        # 线程压到 2：本机可用内存 0.5~1.5 GB，libvpx 开满线程会 STATUS_STACK_OVERFLOW
        "-threads", str(threads),
        "-an",
        str(out),
    ]
    subprocess.run(cmd, check=True)


def make_strip(ffmpeg: str, webm: Path, out_png: Path, n_frames: int, fps: int,
               count: int = 8, scale: float = 0.5, cols: int = 4) -> Path | None:
    """从**成片 webm** 里解码若干帧，拼成带棋盘格的抽帧图。

    用成片而不是渲染时的中间帧，是因为这同时验证了 alpha 有没有过编码这一关
    （解码必须走 libvpx-vp9 + rgba，否则拿回来的是丢掉 alpha 的 RGB）。
    """
    if count <= 0 or n_frames <= 0:
        return None
    tmp = Path(tempfile.mkdtemp(prefix="coopanion-strip-"))
    try:
        picks = [int(round(i * (n_frames - 1) / max(1, count - 1))) for i in range(count)]
        ims = []
        for idx in picks:
            png = tmp / f"s_{idx:05d}.png"
            cmd = [ffmpeg, "-y", "-loglevel", "error",
                   "-c:v", "libvpx-vp9",           # 必须显式用 libvpx 解码，才有 alpha
                   "-i", str(webm),
                   "-vf", f"select=eq(n\\,{idx})",
                   "-vsync", "0", "-frames:v", "1",
                   "-pix_fmt", "rgba", str(png)]
            subprocess.run(cmd, check=True)
            if png.exists():
                ims.append((idx, Image.open(png).convert("RGBA")))
        if not ims:
            return None

        w = int(ims[0][1].width * scale)
        h = int(ims[0][1].height * scale)
        pad = 6
        cols = max(1, min(cols, len(ims)))
        rows = (len(ims) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * (w + pad) - pad, rows * (h + pad) - pad), (46, 46, 54))
        for j, (idx, im) in enumerate(ims):
            bg = Image.new("RGB", (w, h), (62, 62, 70))
            d = ImageDraw.Draw(bg)
            step = 12
            for yy in range(0, h, step):
                for xx in range(0, w, step):
                    if ((xx // step) + (yy // step)) % 2:
                        d.rectangle([xx, yy, xx + step - 1, yy + step - 1], fill=(50, 50, 58))
            small = im.resize((w, h), Image.LANCZOS)
            bg.paste(small, (0, 0), small)
            d.text((4, 3), f"f{idx} {idx / fps:.2f}s", fill=(235, 235, 245))
            sheet.paste(bg, ((j % cols) * (w + pad), (j // cols) * (h + pad)))
        out_png.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(out_png)
        return out_png
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 单个动作渲染
# ---------------------------------------------------------------------------
def render_action(pack: Pack, rig: Rig, motion: Motion, out_dir: Path, frames_dir: Path | None,
                  canvas_w: int, canvas_h: int, fps: int, ffmpeg: str,
                  crf: int, keep_frames: bool, stills: list[int], flip: bool = True) -> Path:
    rig.reset()
    n = max(2, int(round(motion.seconds * fps)))
    dt = 1.0 / fps
    ss = SUPERSAMPLE
    bbox = measure_neutral(pack, rig)
    rig.reset()
    fit = fit_matrix(bbox, canvas_w, canvas_h, ss, flip=flip)

    tmp = Path(tempfile.mkdtemp(prefix="coopanion-frames-"))
    try:
        for i in range(n):
            # 最后一个动作帧不重复起点：k 在 [0,1) 上采样，循环更顺
            k = i / max(1, n) if motion.name in ("idle", "walk", "run", "dance") else i / max(1, n - 1)
            t = k * motion.seconds if motion.name not in ("idle", "walk", "run", "dance") else i * dt
            ctx = Ctx(t=t, k=k, dur=motion.seconds, pack=pack)
            o = motion.gen(ctx)
            o.setdefault("face", motion.face or "neutral")
            pose, alpha, root = rig.evaluate(o, t, dt)
            # 内部按 dt 的 1/4 细分推进弹簧，避免 24fps 下弹簧发散
            frame = compose_frame(pack, pose, alpha, root, fit, (canvas_w * ss, canvas_h * ss))
            if ss != 1:
                frame = frame.resize((canvas_w, canvas_h), Image.LANCZOS)
            frame.save(tmp / f"f_{i:05d}.png")
            if stills and i in stills:
                still_path = tmp / f"still_{i:05d}.png"
                shutil.copyfile(tmp / f"f_{i:05d}.png", still_path)
            if i % 12 == 0:
                log(f"    {motion.name} 帧 {i + 1}/{n}")

        out = out_dir / f"{motion.name}.webm"
        encode_webm(ffmpeg, str(tmp / "f_%05d.png"), out, fps, crf=crf)
        log(f"  {out.name}  {n} 帧 @{fps}fps / {motion.seconds:.2f}s / {out.stat().st_size} bytes")

        if frames_dir:
            frames_dir.mkdir(parents=True, exist_ok=True)
            for s in (stills or []):
                src = tmp / f"still_{s:05d}.png"
                if src.exists():
                    shutil.copyfile(src, frames_dir / f"{motion.name}-frame{s:03d}.png")
        return out
    finally:
        if not keep_frames:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            log(f"    帧保留在 {tmp}")


# ---------------------------------------------------------------------------
# 配置片段
# ---------------------------------------------------------------------------
IN_SLOT_IDLE = {"idle", "look", "stand"}
IN_SLOT_TURN = {"turn", "bow"}
IN_SLOT_DRAG = {"flap"}
IN_SLOT_CLICK = {"wave", "jump", "hop", "spin", "nod", "shake", "dance", "shiver", "dizzy", "sit", "sleep"}


def config_snippet(kind: str, names: list[str], size: int = 462) -> dict:
    idle = [n for n in names if n.lower() in IN_SLOT_IDLE] or names[:1]
    turn = [n for n in names if n.lower() in IN_SLOT_TURN] or idle
    drag = [n for n in names if n.lower() in IN_SLOT_DRAG] or idle
    clicks = [n for n in names if n.lower() in IN_SLOT_CLICK] or names[1:2] or idle
    return {
        "pets": [{"id": kind, "name": kind, "size": size}],
        "animations": {
            "idle": idle,
            "turn": turn,
            "drag": drag,
            "clicks": clicks,
            "moves": {
                "default": {"minDist": 60, "maxDist": 240, "margin": 20,
                            "leadSec": 1, "tailSec": 1},
                "actions": [{"name": n} for n in (clicks[:1] or idle)],
            },
            "events": {
                "balance": idle,
                "workStatus": clicks + idle,
                "whisper": idle,
            },
            "edge": {side: idle[0] for side in ("left", "right", "top", "bottom")},
        },
        "_note": "把 pet/<kind>-animation/ 整个目录拷到 dsh-pet 的 pet/ 下，再把 animations 合并进 <kind>-config.json。"
                 "本脚本不写这个文件。",
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Coopanion 形象包 → dsh-pet 动画（VP9 + alpha webm）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--pack", help="Coopanion 包目录（含 figure.json / model.json）")
    ap.add_argument("--out", help="输出根目录，产物落在 <out>/pet/<kind>-animation/")
    ap.add_argument("--kind", help="dsh-pet 种类名，默认用包 id")
    ap.add_argument("--actions", default="idle",
                    help="要转的动作，逗号分隔；'all' 表示包内全部动作")
    ap.add_argument("--list", action="store_true", help="只列包里的动作词表，不渲染")
    ap.add_argument("--size", default=f"{CANVAS_W}x{CANVAS_H}",
                    help=f"画布尺寸，默认 {CANVAS_W}x{CANVAS_H}（可用 640x360）")
    ap.add_argument("--fps", type=int, default=FPS)
    ap.add_argument("--crf", type=int, default=26, help="VP9 质量，默认 26")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--strips", type=int, default=8,
                    help="从成片 webm 里解出多少帧拼成抽帧图（默认 8，0=不拼）")
    ap.add_argument("--flip", dest="flip", action="store_true", default=True,
                    help="左右镜像，让她朝右（默认开，和 Coopanion 应用里的显示一致）")
    ap.add_argument("--no-flip", dest="flip", action="store_false",
                    help="保留原画的朝左")
    ap.add_argument("--stills", default="", help="额外导出这些帧号到 --frames-dir，逗号分隔")
    ap.add_argument("--frames-dir", help="抽帧图输出目录（配合 --stills）")
    ap.add_argument("--keep-frames", action="store_true", help="保留逐帧 PNG（调试用）")
    ap.add_argument("--strip-cols", type=int, default=4, help="抽帧图每行几格，默认 4")
    ap.add_argument("--no-strips", action="store_true", help="不生成抽帧图")
    ap.add_argument("--config-out", help="把 animations 配置片段写到这个 json 文件，否则打印")
    ap.add_argument("--no-config", action="store_true", help="不打印配置片段")
    args = ap.parse_args(argv)

    if not args.pack:
        ap.error("--pack 必填")
    pack_dir = Path(args.pack).expanduser().resolve()
    if not pack_dir.is_dir():
        raise SystemExit(f"包目录不存在：{pack_dir}")

    pack = load_pack(pack_dir)
    kind = args.kind or pack.pid
    log(f"包 {pack.pid} / {pack.name}：{len(pack.parts)} 个分片 / {len(pack.bones)} 根骨 / "
        f"{len(pack.vocab)} 个动作表情")

    if args.list:
        log("figure.json 动作词表：")
        for v in pack.vocab:
            zh = "/".join((v.get("names") or {}).get("zh") or [])
            mark = "有实现" if v.get("id", "").lower() in ACTIONS else "无实现"
            log(f"  {v.get('kind','?'):10s} {v.get('id',''):10s} {zh:8s} "
                f"{v.get('seconds','?'):>5}s  {mark}")
        log("本脚本内置实现的语义名：" + ", ".join(sorted(ACTIONS)))
        return 0

    if not args.out:
        ap.error("要渲染就必须给 --out")
    try:
        cw, ch = (int(x) for x in args.size.lower().split("x"))
    except ValueError:
        raise SystemExit(f"--size 要写成 960x540，收到 {args.size}")
    if args.fps <= 0:
        raise SystemExit("--fps 必须为正")

    out_root = Path(args.out).expanduser().resolve()
    anim_dir = out_root / "pet" / f"{kind}-animation"
    anim_dir.mkdir(parents=True, exist_ok=True)
    stills = [int(x) for x in args.stills.split(",") if x.strip().isdigit()]
    frames_dir = Path(args.frames_dir).expanduser().resolve() if args.frames_dir else None

    if args.actions.strip().lower() == "all":
        want = [v["id"] for v in pack.vocab if v.get("kind") == "motion"]
    else:
        want = [a for a in args.actions.split(",") if a.strip()]
    motions = resolve_actions(pack, want)
    if not motions:
        raise SystemExit("没有可渲染的动作")

    rig = Rig(pack, flip=args.flip)
    made: list[str] = []
    log(f"输出 {anim_dir}（{cw}×{ch} @{args.fps}fps，角色高 {ch * CHAR_RATIO:.0f}px，底边 y={ch * BASELINE_RATIO:.0f}）")
    for m in motions:
        log(f"  渲染 {m.name}（{m.seconds:.2f}s）")
        render_action(pack, rig, m, anim_dir, frames_dir, cw, ch, args.fps,
                      args.ffmpeg, args.crf, args.keep_frames, stills, flip=args.flip)
        made.append(m.name)
        if args.strips and not args.no_strips:
            strip = make_strip(args.ffmpeg, anim_dir / f"{m.name}.webm",
                               out_root / "strips" / f"{kind}-{m.name}-strip.png",
                               int(round(m.seconds * args.fps)), args.fps, count=args.strips, cols=args.strip-cols)
            if strip:
                log(f"  抽帧图 {strip}")

    if not args.no_config:
        snippet = config_snippet(kind, made)
        text = json.dumps(snippet, ensure_ascii=False, indent=2)
        if args.config_out:
            Path(args.config_out).expanduser().resolve().write_text(text + "\n", encoding="utf-8")
            log(f"配置片段写到 {args.config_out}")
        else:
            print("\n--- animations 配置片段（自己合并进 dsh-pet 配置，本脚本不写）---")
            print(text)

    log(f"完成：{len(made)} 个动作 → {anim_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
