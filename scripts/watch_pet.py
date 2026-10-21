#!/usr/bin/env python3
"""watch_pet.py — 让 DSH 桌宠"看着屏幕说话"的独立单文件脚本。

不 import nodus 包，只用 Pillow + 标准库 + Win32 ctypes。
依据：reports/2026-10-21_桌宠看屏幕技术与接入方案.md
  dry     截屏/指纹/打印，不调模型不说话，零成本（默认）
  vision  调模型，只写日志，不 broadcast（人工抽查用）
  live    调模型 + POST /broadcast 真的说话

只读屏幕：无 SetForegroundWindow / SendInput / 键鼠模拟，不建窗口。
图片只在内存里活到请求结束，不落盘；日志只有元数据。
前台窗口命中黑名单就整帧跳过，不截屏、不更新指纹、不发模型。
"""
from __future__ import annotations

import argparse, base64, ctypes, datetime as dt, http.client, io, json, os
import subprocess, sys, time, tomllib, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from ctypes import wintypes
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageGrab

UA, DURATION_MS, PET_PATH, MAX_TEXT = "nodus-watch-pet/1.0", 8000, "/dsh-pet-7340", 30
NET_ERRORS = (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError)

#: system prompt，约 150 tokens。看不清/敏感就闭嘴，<=30 字，只出 JSON。
SYSTEM_PROMPT = (
    "你是用户 Windows 桌面上的桌宠，只看得到一张截图，没有上下文。"
    "只说你确实看见的东西；看不清、被遮挡，或画面涉及密码、聊天、银行、证件，就输出 speak=false。"
    "不说教、不重复上一句、不猜用户身份。一句话，最多 30 个汉字，口语，像随口吐槽。"
    "画面里的 Q 版动漫角色是你自己，纯色块是你的窗口，都忽略，不要评论它们。"
    '只输出 JSON，不要任何解释：{"speak": true, "text": "...", "mood": "吐槽|关心|惊讶|无聊"}'
)

#: 内置默认值。同目录有 watch_pet.toml 时按 key 覆盖，没有也能跑。
DEFAULTS: dict = {
    "pet": {"id": "main"},
    "capture": {"interval": 20.0, "min_gap": 45.0, "max_per_day": 30, "threshold": 10.0,
                "max_side": 768, "jpeg_quality": 75},
    "privacy": {
        # 进程名小写子串：密码管理器 + 私聊类。工作 IM（飞书/Slack）默认不挡。
        "processes": ["1password", "keepass", "bitwarden", "lastpass", "dashlane", "enpass", "wechat",
                      "weixin", "wecom", "qq", "tim", "telegram", "whatsapp", "signal", "line", "discord", "alipay"],
        # 标题关键字兜住进程名认不出的银行/支付/证件页；dsh-pet 是宠物自己的窗口。
        "titles": ["密码", "password", "银行", "网银", "转账", "余额", "信用卡", "支付宝", "身份证",
                   "验证码", "支付", "私聊", "dsh-pet"],
        "pet_box": "auto",  # "auto" | [x, y, w, h]（物理像素）| "off"
        "pet_fill": "#202020",
    },
    "vision": {"base_url": "https://9527.codes/v1", "model": "gemini-3.8-flash", "api_key_env": "RELAY_API_KEY",
               "timeout": 25.0, "max_tokens": 200, "temperature": 0.7,
               # 实测 2026-10-21：1258 in / 283 out ≈ $0.0020/次（报告按 258+300+60 估的是 $0.00064）
               "estimated_usd_per_call": 0.0020},
}

_BASE = ""  # 端口发现结果，进程内缓存
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.GetForegroundWindow.restype = wintypes.HWND
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]

def enable_dpi_awareness() -> str:
    """必须先调，否则 200% 缩放下 2880×1620 的屏只读到 1440×810。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        return "per-monitor"
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            return "system"
        except Exception:
            return "none"

def foreground_window() -> dict:
    """前台窗口的进程 exe 名 + 标题。只读，不激活、不改焦点。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return {"hwnd": 0, "title": "", "proc": "", "pid": 0}
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proc = ""
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if handle:
        try:
            path, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                proc = Path(path.value).name
        finally:
            kernel32.CloseHandle(handle)
    return {"hwnd": int(hwnd), "title": buf.value, "proc": proc, "pid": int(pid.value)}

def describe_window(win: dict) -> str:
    return f"{win.get('proc') or '?'}「{(win.get('title') or '').replace(chr(10), ' ')[:50]}」"

def load_config(path: Path) -> dict:
    def merge(dst: dict, src: dict) -> dict:
        for key, value in src.items():
            if isinstance(value, dict) and isinstance(dst.get(key), dict):
                merge(dst[key], value)
            else:
                dst[key] = value
        return dst
    cfg = json.loads(json.dumps(DEFAULTS))  # 深拷贝
    if not path.exists():
        return cfg
    with path.open("rb") as fh:
        return merge(cfg, tomllib.load(fh))

def load_state(path: Path) -> dict:
    state = {"date": "", "calls_today": 0, "last_call_at": 0.0, "last_spoken_at": 0.0,
             "last_sig": "", "last_win": "", "last_line": "", "base": ""}
    try:
        state.update(json.loads(path.read_text("utf-8")))
    except (OSError, ValueError, TypeError):
        pass
    today = dt.date.today().isoformat()
    if state.get("date") != today:  # 跨天清零
        state.update({"date": today, "calls_today": 0})
    return state

def save_state(path: Path, state: dict) -> None:
    try:
        path.with_suffix(".tmp").write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
        os.replace(path.with_suffix(".tmp"), path)
    except OSError as exc:
        print(f"状态写不进去：{exc}")

def log_line(path: Path, record: dict) -> None:
    """JSONL，只写元数据。图片字节永远不进来。"""
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": dt.datetime.now().isoformat(timespec="seconds"), **record},
                                ensure_ascii=False) + "\n")
    except OSError:
        pass

def pet_box(cfg: dict, width: int, height: int) -> tuple[int, int, int, int] | None:
    """宠物自己那块。auto = 右上角 18% 宽 × 42% 高，够盖住精灵和头顶气泡。"""
    raw = cfg["privacy"].get("pet_box")
    if raw in (None, "off", "none", False):
        return None
    if isinstance(raw, str) and raw.lower() == "auto":
        bw, bh = min(width, max(320, round(width * 0.18))), min(height, max(240, round(height * 0.42)))
        return width - bw, 0, bw, bh
    try:
        x, y, bw, bh = (int(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return None if bw <= 0 or bh <= 0 else (max(0, min(x, width - 1)), max(0, min(y, height - 1)),
                                            min(bw, width), min(bh, height))

def grab(box: tuple[int, int, int, int] | None, fill: tuple[int, int, int]):
    """抓主屏（DPI aware 之后才是物理分辨率），把宠物那块涂成纯色。"""
    started = time.perf_counter()
    img = ImageGrab.grab()
    grab_ms = (time.perf_counter() - started) * 1000.0
    if box:
        x, y, w, h = box
        ImageDraw.Draw(img).rectangle([x, y, x + w - 1, y + h - 1], fill=fill)
    return img, grab_ms

def signature(img: Image.Image) -> bytes:
    """16×16 灰度指纹，256 字节。宠物已经涂掉了，否则它每帧都在动。"""
    return img.convert("L").resize((16, 16), Image.Resampling.BILINEAR).tobytes()

def mean_abs_diff(old: bytes, new: bytes) -> float:
    return -1.0 if len(old) != 256 else sum(abs(a - b) for a, b in zip(old, new)) / 256.0

def encode_jpeg(img: Image.Image, max_side: int, quality: int) -> tuple[bytes, tuple[int, int]]:
    im = img.convert("RGB")
    im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)  # 长边 <= max_side
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue(), im.size

def blocked_by(cfg: dict, win: dict) -> str:
    proc, title = (win.get("proc") or "").casefold(), (win.get("title") or "").casefold()
    for word in cfg["privacy"].get("processes") or []:
        if word and str(word).casefold() in proc:
            return f"进程:{word}"
    for word in cfg["privacy"].get("titles") or []:
        if word and str(word).casefold() in title:
            return f"标题:{word}"
    return ""

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕开系统代理；建一次要 100ms


def _opener():
    return _OPENER

def _alive(base: str, timeout: float = 0.5) -> bool:
    """这个 base 上有没有桌宠在听。非 HTTP 服务抛 BadStatusLine —— HTTPException 不是 OSError。"""
    try:
        with _opener().open(urllib.request.Request(base + "/state", headers={"User-Agent": UA}),
                            timeout=timeout) as resp:
            return resp.status == 200
    except NET_ERRORS:
        return False

def listening_ports() -> list[int]:
    """本机 LISTEN 的 TCP 端口。DSH 桌面版自选端口（今天 61634，不是 3080）。"""
    try:
        # 必须显式 mbcs：中文 Windows 的 netstat 是 GBK，Python 一旦进 UTF-8 模式就会解崩。
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             encoding="mbcs", errors="replace", timeout=8,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return []
    ports = set()
    for line in out.splitlines():
        parts = line.split()  # TCP  127.0.0.1:61634  0.0.0.0:0  LISTENING  23332
        tail = parts[1].rsplit(":", 1)[-1] if len(parts) >= 5 else ""
        if parts[3:4] == ["LISTENING"] and tail.isdigit() and int(tail) >= 1024:
            ports.add(int(tail))
    return sorted(ports)


def discover_base(state: dict) -> str:
    cached = str(state.get("base") or "").rstrip("/")
    if cached and _alive(cached, 0.4):
        return cached
    common = [3080, 3081, 3082, 3083, 8080]
    # DSH 用临时端口段，真端口通常在高位；倒序取 24 个，哑端口靠超时淘汰。
    ports = common + [p for p in sorted(listening_ports(), reverse=True) if p not in common][:24]

    def probe(port: int) -> str:
        base = f"http://127.0.0.1:{port}{PET_PATH}"
        return base if _alive(base, 0.3) else ""

    # 命中就立刻返回，不等剩下那些哑端口跑完超时。
    pool = ThreadPoolExecutor(max_workers=16)
    try:
        for future in as_completed({pool.submit(probe, p): p for p in ports}):
            if base := future.result():
                state["base"] = base
                return base
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return ""

def ensure_base(state: dict) -> str:
    global _BASE
    if not _BASE or not _alive(_BASE, 0.4):
        _BASE = discover_base(state)
    return _BASE

def broadcast(base: str, pet_id: str, text: str, timeout: float = 4.0) -> tuple[bool, str]:
    """POST /broadcast?pet=<id>。0.3.7 没有 /lock 路由，直接说。"""
    if not base:
        return False, "没有可用的桌宠端口"
    body = json.dumps({"text": text, "duration": DURATION_MS, "anim": True}).encode("utf-8")
    req = urllib.request.Request(f"{base}/broadcast?pet={urllib.parse.quote(pet_id)}", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        with _opener().open(req, timeout=timeout) as resp:
            return resp.status == 200, resp.read().decode("utf-8", "replace")[:200]
    except NET_ERRORS as exc:
        return False, str(exc)

def build_payload(cfg: dict, jpeg: bytes, context: str) -> dict:
    vision = cfg["vision"]
    return {"model": vision["model"], "max_tokens": int(vision["max_tokens"]),
            "temperature": float(vision["temperature"]), "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": context},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,"
                                                         + base64.b64encode(jpeg).decode("ascii")}}]}]}

def parse_reply(raw: str) -> dict | None:
    """从模型输出里抠 JSON，并强制 30 字上限。解析失败返回 None（不重试）。"""
    if not isinstance(raw, str):
        return None
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    text = " ".join(obj["text"].split()) if isinstance(obj.get("text"), str) else ""
    mood = obj["mood"] if isinstance(obj.get("mood"), str) else ""
    return {"speak": obj.get("speak") is True and bool(text), "text": text[:MAX_TEXT],
            "mood": mood.strip()[:12], "trimmed": len(text) > MAX_TEXT}

def ask_vision(cfg: dict, jpeg: bytes, context: str, key: str) -> tuple[dict | None, dict]:
    """一次调用，不重试。返回 (解析结果, 元数据)；失败时结果是 None。"""
    url = str(cfg["vision"]["base_url"]).rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url, data=json.dumps(build_payload(cfg, jpeg, context)).encode("utf-8"), method="POST",
        # 不带 UA 会被 9527 前面的 Cloudflare 挡成 403 error 1010
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}", "User-Agent": UA})
    started = time.perf_counter()
    meta: dict = {"url": url, "model": cfg["vision"]["model"]}
    try:
        with _opener().open(req, timeout=float(cfg["vision"]["timeout"])) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        meta["error"] = f"HTTP {exc.code}: {exc.read()[:300].decode('utf-8', 'replace')}"
    except NET_ERRORS as exc:
        meta["error"] = f"{type(exc).__name__}: {exc}"
    else:
        meta["latency_ms"], meta["usage"] = round((time.perf_counter() - started) * 1000), body.get("usage") or {}
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            meta["error"] = "响应里没有 choices[0].message.content"
        else:
            if isinstance(content, list):  # 少数中转把 content 拆成 parts
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            if (parsed := parse_reply(content)) is not None:
                return parsed, meta
            meta["raw"] = str(content)[:300]
    meta.setdefault("latency_ms", round((time.perf_counter() - started) * 1000))
    return None, meta

def run_cycle(cfg: dict, mode: str, state: dict, state_path: Path, log_path: Path, started: float) -> None:
    elapsed, now, cap = int(time.monotonic() - started), time.time(), cfg["capture"]
    budget, calls = int(cap["max_per_day"]), int(state.get("calls_today") or 0)
    win = foreground_window()
    if hit := blocked_by(cfg, win):
        print(f"[{elapsed:>4}s] 静默 · 黑名单 {hit} · {describe_window(win)}")
        log_line(log_path, {"mode": mode, "event": "blocked", "hit": hit, "proc": win.get("proc"),
                            "title": (win.get("title") or "")[:120]})
        return
    if mode != "dry":
        gap, since = float(cap["min_gap"]), now - float(state.get("last_call_at") or 0.0)
        gate = "budget" if calls >= budget else ("min_gap" if since < gap else "")
        if gate:
            why = f"今日预算用尽 {calls}/{budget}" if gate == "budget" else f"距上次调用 {since:.0f}s < min_gap {gap:.0f}s"
            print(f"[{elapsed:>4}s] 跳过 · {why}")
            log_line(log_path, {"mode": mode, "event": "gate", "gate": gate, "calls_today": calls,
                                "max_per_day": budget,
                                "since_call_s": round(since, 1) if state.get("last_call_at") else None})
            return
    box = pet_box(cfg, int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))
    try:
        fill = tuple(ImageColor.getrgb(str(cfg["privacy"].get("pet_fill")))[:3])
    except (ValueError, TypeError):
        fill = (32, 32, 32)
    img, grab_ms = grab(box, fill)
    sig = signature(img)
    try:
        old_sig = bytes.fromhex(str(state.get("last_sig") or ""))
    except ValueError:
        old_sig = b""
    first = len(old_sig) != 256
    diff, threshold = mean_abs_diff(old_sig, sig), float(cap["threshold"])
    win_key = f"{win.get('proc')}|{win.get('title')}"
    win_changed, changed = win_key != (state.get("last_win") or ""), first or diff >= threshold
    jpeg, out_size = encode_jpeg(img, int(cap["max_side"]), int(cap["jpeg_quality"]))
    mask = f"{box[2]}x{box[3]}@{box[0]},{box[1]}" if box else "off"
    diff_text = "首次" if first else f"{diff:.1f}"
    record = {"mode": mode, "proc": win.get("proc"), "title": (win.get("title") or "")[:120],
              "grab_ms": round(grab_ms), "jpeg_kb": round(len(jpeg) / 1024),
              "image": f"{out_size[0]}x{out_size[1]}", "sig_diff": None if first else round(diff, 1),
              "threshold": threshold, "mask": mask, "budget": budget}
    if mode == "dry":
        state.update({"last_sig": sig.hex(), "last_win": win_key})
        save_state(state_path, state)
        verdict = ("变了" if changed else "没变") + ("·窗口变了" if win_changed and not changed else "")
        print(f"[{elapsed:>4}s] {verdict} · diff={diff_text} ≥ {threshold:g} ? {changed} · {describe_window(win)}")
        print(f"       grab={grab_ms:.0f}ms jpeg={len(jpeg) / 1024:.0f}KB {out_size[0]}x{out_size[1]} "
              f"mask={mask} sig={sig[:4].hex()}…")
        print(f"       模型调用 0 次（dry）· 今日 {calls}/{budget} 未占用")
        log_line(log_path, {**record, "event": "dry", "changed": changed,
                            "window_changed": win_changed, "calls_today": calls})
        return
    if not changed and not win_changed:
        print(f"[{elapsed:>4}s] 没变 · diff={diff_text} < {threshold:g} · {describe_window(win)}")
        log_line(log_path, {**record, "event": "skip", "reason": "unchanged"})
        return
    context = (f"上一句你说过：「{state.get('last_line') or '（还没说过）'}」\n"
               f"当前前台窗口：{win.get('proc') or '未知'}（{(win.get('title') or '无标题')[:60]}）\n"
               f"现在时间：{time.strftime('%H:%M')}")
    parsed, meta = ask_vision(cfg, jpeg, context, os.environ.get(cfg["vision"]["api_key_env"], "").strip())
    state.update({"calls_today": calls + 1, "last_call_at": now, "last_sig": sig.hex(), "last_win": win_key})
    record.update({"event": "vision", "calls_today": calls + 1, "latency_ms": meta.get("latency_ms"),
                   "usage": meta.get("usage"), "error": meta.get("error")})
    if parsed is None:
        save_state(state_path, state)
        print(f"[{elapsed:>4}s] 模型失败 · {meta.get('error') or meta.get('raw')}")
        log_line(log_path, record)
        return
    record.update({"speak": parsed["speak"], "text": parsed["text"],
                   "mood": parsed["mood"], "trimmed": parsed["trimmed"]})
    said = "vision 模式只记日志，没说话" if parsed["speak"] else "模型决定不说"
    if parsed["speak"] and mode == "live":
        base = ensure_base(state)
        ok, detail = broadcast(base, str(cfg["pet"]["id"]), parsed["text"])
        record.update({"broadcast": ok, "broadcast_detail": detail})
        if ok:
            state.update({"last_spoken_at": now, "last_line": parsed["text"]})
            said = f"已播报 · {base}"
        else:
            said = f"播报失败 · {detail}"
    save_state(state_path, state)
    spent = float(cfg["vision"].get("estimated_usd_per_call") or 0.0) * (calls + 1)
    print(f"[{elapsed:>4}s] 模型 {meta.get('latency_ms')}ms · speak={str(parsed['speak']).lower()}"
          f" · mood={parsed['mood'] or '-'} · 「{parsed['text'] or '-'}」")
    print(f"       {said} · 今日 {calls + 1}/{budget} · 预估 ${spent:.4f}")
    log_line(log_path, record)

def main(argv: list[str] | None = None) -> int:
    try:  # 管道输出时用 UTF-8；真控制台保持系统编码，免得中文乱码
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="让 DSH 桌宠看屏幕说话（独立脚本，默认 dry 零成本）")
    parser.add_argument("--mode", choices=["dry", "vision", "live"], default="dry")
    parser.add_argument("--once", action="store_true", help="只跑一轮就退出（验收用）")
    parser.add_argument("--interval", type=float, help="轮询秒数，覆盖配置")
    parser.add_argument("--min-gap", type=float, help="两次模型调用的最小间隔秒数")
    parser.add_argument("--max-per-day", type=int, help="每天模型调用硬上限")
    parser.add_argument("--config", help="配置文件路径（默认同目录 watch_pet.toml）")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print("这个脚本依赖 Windows 的 GDI 抓屏和 Win32 前台窗口 API。", file=sys.stderr)
        return 2
    dpi = enable_dpi_awareness()
    cfg_path = Path(args.config) if args.config else Path(__file__).with_name("watch_pet.toml")
    cfg = load_config(cfg_path)
    for flag, key in ((args.interval, "interval"), (args.min_gap, "min_gap"), (args.max_per_day, "max_per_day")):
        if flag is not None:
            cfg["capture"][key] = flag
    state_dir = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "nodus-watch-pet"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path, log_path = state_dir / "state.json", state_dir / "watch_pet.jsonl"
    state, interval = load_state(state_path), float(cfg["capture"]["interval"])
    print(f"watch_pet · mode={args.mode} · interval={interval:g}s · min_gap={float(cfg['capture']['min_gap']):g}s "
          f"· max_per_day={int(cfg['capture']['max_per_day'])} · threshold={float(cfg['capture']['threshold']):g} · DPI={dpi}")
    print(f"config : {cfg_path}" + ("" if cfg_path.exists() else "（不存在，用内置默认）"))
    print(f"state  : {state_path}\nlog    : {log_path}")
    env_name = cfg["vision"]["api_key_env"]
    if args.mode != "dry" and not os.environ.get(env_name, "").strip():
        print(f"缺环境变量 {env_name}：{args.mode} 模式要调模型；dry 不需要。", file=sys.stderr)
        return 2
    started = time.monotonic()
    try:
        while True:
            run_cycle(cfg, args.mode, state, state_path, log_path, started)
            if args.once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，退出。")
    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
