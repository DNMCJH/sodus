"""What the pet should show: one small state any widget can poll.

A desktop pet is an ambient status light, and the reason it fits Nodus is that
the interesting question ("is anything waiting for me?") is answered by two
numbers and a mood. So this module computes that, and the bridge below pushes
the same facts into a pet that can be driven over HTTP.

The pet is optional in both directions: `/pet` is a plain JSON endpoint with no
dependency on any particular widget, and the push is best-effort — a missing or
broken pet never delays or fails a run.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from . import config, db

#: How long a failure keeps the pet looking upset.
RECENT = 600.0

#: The pet's own work-status animation pool, from its shipped config. These are
#: the names its right-click menu shows, so this is driving the pet through a
#: supported door rather than poking at internals.
ANIM = {
    "thinking": "工作状态-思考冒泡",
    "working": "工作状态-忙碌点按",
    "tidying": "工作状态-清点归档",
    "waiting": "工作状态-原地踱步张望",
    "done": "工作状态-雀跃庆祝",
    "error": "工作状态-垂头叹气冒汗",
}


def _usage(run: dict) -> dict:
    try:
        return json.loads(run.get("usage_json") or "{}")
    except json.JSONDecodeError:
        return {}


def state(now: float | None = None) -> dict:
    """The mood, one line of text, and the numbers behind them."""
    now = now or time.time()
    runs = db.list_runs(limit=50)
    running = [r for r in runs if r.get("status") in ("running", "queued")]
    recent = [r for r in runs if (r.get("ended_at") or 0) > now - RECENT]
    failed = [r for r in recent if r.get("status") == "failed"]
    states = db.verify_states([r["id"] for r in recent]) if recent else {}
    red = [r for r in recent if states.get(r["id"]) == "red"]
    proposals = db.list_proposals("open")

    costs: dict[str, float] = {}
    for run in runs:
        usage = _usage(run)
        cost = usage.get("cost")
        if isinstance(cost, (int, float)):
            currency = usage.get("currency") or "USD"
            costs[currency] = round(costs.get(currency, 0.0) + float(cost), 6)

    if failed or red:
        mood = "error"
        what = "测试红了" if red else "失败了"
        target = (red or failed)[0]
        bubble = f"{(target.get('route') or target.get('agent') or 'run')} {what}：{target['id'][4:12]}"
    elif proposals:
        mood = "attention"
        bubble = f"{len(proposals)} 个提案等你决定"
    elif running:
        mood = "working"
        bubble = f"{len(running)} 个在跑"
    else:
        mood = "idle"
        bubble = "闲着"

    return {
        "mood": mood,
        "bubble": bubble,
        "running": len(running),
        "failed": len(failed),
        "red": len(red),
        "proposals": len(proposals),
        "cost": costs,
        "updated_at": now,
    }


def describe(run: dict, verify_mark: str = "none") -> str:
    """One line about a finished run, for a bubble."""
    route = run.get("route") or run.get("agent") or "run"
    label = {"ok": "测试通过", "red": "测试红了", "none": "跑完但没验证"}.get(
        verify_mark, verify_mark
    )
    usage = _usage(run)
    cost = usage.get("cost")
    money = ""
    if isinstance(cost, (int, float)):
        symbol = "¥" if usage.get("currency") == "CNY" else (usage.get("currency") or "") + " "
        money = f"，{symbol}{cost:.4f}"
    return f"{route} {label}{money}"


# ---- the optional push ------------------------------------------------


def bridge() -> dict:
    raw = config.load_config().get("pet_bridge") or {}
    return raw if isinstance(raw, dict) else {}


_DISCOVERED = ""


def _alive(base: str) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base + "/state", timeout=0.6) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def base_url() -> str:
    """Where the pet listens.

    `auto` (the default) probes the usual ports instead of hardcoding 3080:
    the desktop client is its own host and does not have to choose the same
    port as `dsh web`, and a bridge that silently stops working after a switch
    is worse than one that searches for two seconds.
    """
    global _DISCOVERED
    cfg = bridge()
    raw = str(cfg.get("base") or "auto")
    if raw != "auto":
        return raw.rstrip("/")
    if _DISCOVERED:
        return _DISCOVERED
    for port in (3080, 3081, 3082, 3083, 8080):
        candidate = f"http://127.0.0.1:{port}/dsh-pet-7340"
        if _alive(candidate):
            _DISCOVERED = candidate
            return candidate
    return "http://127.0.0.1:3080/dsh-pet-7340"


def _call(method: str, path: str, body: dict | None = None, timeout: float = 3.0) -> dict | None:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base_url() + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None


def push(text: str, duration_ms: int = 12000) -> bool:
    """Make the pet say one line, best effort.

    Newer builds expose an occupancy lock so the pet cannot be interrupted
    mid-show; 0.3.7 does not have that route at all and rejects /lock, so the
    lock is attempted and fallen back from rather than required. The pet is a
    light on the desk, not a dependency: any failure here costs one sentence.
    """
    cfg = bridge()
    if not cfg.get("enabled"):
        return False
    pet_id = str(cfg.get("pet") or "main")
    lock_id = ""
    lock = _call("POST", f"/lock?pet={pet_id}", {})
    if isinstance(lock, dict) and lock.get("lockId"):
        lock_id = str(lock["lockId"])
    query = f"?pet={pet_id}" + (f"&lock={lock_id}" if lock_id else "")
    try:
        result = _call(
            "POST", f"/broadcast{query}", {"text": text, "duration": duration_ms, "anim": True}
        )
        return bool(result and result.get("ok", True))
    finally:
        if lock_id:
            _call("POST", f"/lock/release?pet={pet_id}", {"lockId": lock_id})


def animate(name: str) -> bool:
    """Play one animation from the pet's own pool."""
    if not bridge().get("enabled") or not name:
        return False
    return bool(_call("POST", f"/anim?pet={bridge().get('pet') or 'main'}", {"name": name}))


def notify(text: str, duration_ms: int = 12000) -> None:
    """Push in the background so a slow pet cannot slow a run down."""
    if not bridge().get("enabled"):
        return
    threading.Thread(target=push, args=(text, duration_ms), daemon=True).start()


def narrate(text: str = "", mood: str = "") -> None:
    """Say something and, if a mood is given, look like it."""
    if not bridge().get("enabled"):
        return

    def work() -> None:
        animation = ANIM.get(mood, "")
        if animation:
            animate(animation)
        if text:
            push(text)

    threading.Thread(target=work, daemon=True).start()
