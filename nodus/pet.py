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

import http.client
import json
import subprocess
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

# 别的角色的动作名跟蓝鱼娘完全不同（橙毛大小姐没有「工作状态-雀跃庆祝」，她只有
# 「欢快舞蹈」）。所以每个宠物可以带一份自己的 mood → 动作名表，写在
# `pet_bridge.anim_map` 里；没写的沿用上面的 ANIM。
#
# 为什么放配置不放代码：角色是素材，加一只不该动 Python。名字对不上时
# `/anim` 会回 unknown-animation，表现成「宠物不跟着动」——所以宁可留空
# 走默认，也不要猜。


def anim_for(mood: str, agent: str = "") -> str:
    """这个 mood 在目标宠物上该播哪一段。

    每次现读配置：`bridge()` 不缓存，改 config.json 立刻生效，不用重启。
    """
    pet_id = pet_for(agent)
    mapping = bridge().get("anim_map")
    if isinstance(mapping, dict):
        per_pet = mapping.get(pet_id)
        if isinstance(per_pet, dict) and per_pet.get(mood):
            return str(per_pet[mood])
    return ANIM.get(mood, "")


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
    """这个 base 上是不是真有个桌宠在听。

    扫端口时一定会碰到非 HTTP 的服务（数据库、Electron 调试口、别的插件的
    裸 TCP）。它们回的不是 HTTP，urllib 会抛 `http.client.BadStatusLine`，
    那是 HTTPException 而不是 OSError——只捕 OSError 会让整次扫描炸掉。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base + "/state", timeout=0.6) as resp:
            return resp.status == 200
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        http.client.HTTPException,
        TimeoutError,
        OSError,
    ):
        return False


def _listening_ports() -> list[int]:
    """本机正在监听的 TCP 端口（Windows netstat）。

    为什么不用固定端口表：DSH 桌面客户端自选端口，实测会落到 61634 这种
    临时端口上。硬编码一张表，换一次宿主就静默失效——正是要避免的坏味道。
    """
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=5, check=False,
            creationflags=config.NO_WINDOW,
).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    ports: set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        # 形如: TCP  127.0.0.1:61634  0.0.0.0:0  LISTENING  23332
        if len(parts) >= 4 and parts[3].upper() == "LISTENING":
            port = parts[1].rsplit(":", 1)[-1]
            if port.isdigit():
                ports.add(int(port))
    return sorted(ports)


def base_url() -> str:
    """Where the pet listens.

    `auto` (the default) probes instead of hardcoding 3080: the desktop client
    is its own host and does not have to choose the same port as `dsh web`
    (实测 61634），a bridge that silently stops working after a switch is worse
    than one that searches for two seconds.

    探测顺序：常见端口 → 本机所有监听端口。整体有上限，最坏情况约 2 秒。
    """
    global _DISCOVERED
    cfg = bridge()
    raw = str(cfg.get("base") or "auto")
    if raw != "auto":
        return raw.rstrip("/")
    if _DISCOVERED:
        return _DISCOVERED

    tried: list[int] = []
    for port in (3080, 3081, 3082, 3083, 8080):
        tried.append(port)
        candidate = f"http://127.0.0.1:{port}/dsh-pet-7340"
        if _alive(candidate):
            _DISCOVERED = candidate
            return candidate

    for port in _listening_ports():
        if port in tried or port < 1024:
            continue
        tried.append(port)
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


def pet_for(agent: str = "") -> str:
    """Which desktop pet speaks for this agent.

    `pet_bridge.pet_agents` maps an agent/route name to a pet id, so a finished
    claude run can light up 橙毛大小姐 while a dsh run lights up the whale. The
    mapping lives in config rather than in code because pets are data: adding
    a character is dropping files in `dsh-pet/pet/`, not shipping a release.

        "pet_agents": {"dsh": "main", "claude": "claude", "codex": "gpt"}

    Anything unmapped falls back to `pet` (the old single-pet field), which
    keeps existing configs working unchanged.
    """
    cfg = bridge()
    mapping = cfg.get("pet_agents")
    if agent and isinstance(mapping, dict):
        target = mapping.get(agent)
        if isinstance(target, str) and target:
            return target
    return str(cfg.get("pet") or "main")


def push(text: str, duration_ms: int = 12000, agent: str = "") -> bool:
    """Make the pet say one line, best effort.

    Newer builds expose an occupancy lock so the pet cannot be interrupted
    mid-show; 0.3.7 does not have that route at all and rejects /lock, so the
    lock is attempted and fallen back from rather than required. The pet is a
    light on the desk, not a dependency: any failure here costs one sentence.
    """
    cfg = bridge()
    if not cfg.get("enabled"):
        return False
    pet_id = pet_for(agent)
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


def animate(name: str, agent: str = "") -> bool:
    """Play one animation from the pet's own pool."""
    if not bridge().get("enabled") or not name:
        return False
    return bool(_call("POST", f"/anim?pet={pet_for(agent)}", {"name": name}))


def notify(text: str, duration_ms: int = 12000, agent: str = "") -> None:
    """Push in the background so a slow pet cannot slow a run down."""
    if not bridge().get("enabled"):
        return
    threading.Thread(target=push, args=(text, duration_ms, agent), daemon=True).start()


def narrate(text: str = "", mood: str = "", agent: str = "") -> None:
    """Say something and, if a mood is given, look like it.

    `agent` picks which pet speaks (see `pet_for`); leaving it empty keeps the
    old single-pet behaviour.
    """
    if not bridge().get("enabled"):
        return

    def work() -> None:
        # Late import: mood imports this module for pet_for/anim_for, so a
        # top-level import would be circular.
        from . import mood as mood_mod

        beats = mood_mod.plan(mood, agent) if mood else []
        if len(beats) > 1:
            # A character with a choreography: play the beat list instead of
            # one clip so a success and a failure stop looking identical.
            for name, hold in beats:
                animate(name, agent)
                time.sleep(max(0.0, hold))
        elif beats:
            animate(beats[0][0], agent)
        else:
            animation = anim_for(mood, agent)
            if animation:
                animate(animation, agent)
        if text:
            push(text, agent=agent)

    threading.Thread(target=work, daemon=True).start()
