"""Jev (TypeSafe System One) decision client for the orchestrator.

Design rules:
- Advice, never authority. A Jev answer never completes a task, grants a
  permission, or replaces a test. It only routes attention.
- Fail open. Missing key, proxy failure, timeout or 5xx all return
  `{"ok": False, "unavailable": ...}` and the caller continues as if absent.
- Every decision is appended to `~/.nodus/decisions.jsonl`, so the value of
  Jev can be measured later instead of assumed.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from . import config, route_meta, routes

DEFAULT_BASE = "https://api.typesafe.ai"
DEFAULT_PROXY = "http://127.0.0.1:7890"
DEFAULT_MODEL = "jev-latest"


def _settings() -> dict:
    secrets = config.load_secrets()
    return {
        "key": os.environ.get("TYPESAFE_API_KEY") or secrets.get("TYPESAFE_API_KEY", ""),
        "proxy": os.environ.get("TYPESAFE_PROXY")
        or secrets.get("TYPESAFE_PROXY", DEFAULT_PROXY),
        "base": os.environ.get("TYPESAFE_BASE_URL")
        or secrets.get("TYPESAFE_BASE_URL", DEFAULT_BASE),
    }


def available() -> tuple[bool, str]:
    s = _settings()
    if not s["key"]:
        return False, "TYPESAFE_API_KEY not set (env or ~/.nodus/secrets.json)"
    return True, "ok"


def _opener(proxy: str):
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(path: str, payload: dict, timeout: float = 30.0) -> dict:
    s = _settings()
    if not s["key"]:
        return {"ok": False, "unavailable": "no TYPESAFE_API_KEY"}
    req = urllib.request.Request(
        s["base"].rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {s['key']}",
            "Content-Type": "application/json",
            "User-Agent": "nodus/0.3",
        },
    )
    started = time.time()
    try:
        with _opener(s["proxy"]).open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        hint = " (451: 地区限制，检查代理出口)" if exc.code == 451 else ""
        return {"ok": False, "unavailable": f"HTTP {exc.code}{hint}", "detail": detail}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "unavailable": f"network: {exc}", "proxy": s["proxy"]}
    return {"ok": True, "body": body, "latency_ms": int((time.time() - started) * 1000)}


def decide(state: str, questions: dict, model: str = DEFAULT_MODEL) -> dict:
    """Ask Jev. Returns {"ok": True, "answers": ..., "usage": ..., "model": ...}
    or {"ok": False, "unavailable": ...}. Never raises."""
    if not questions:
        return {"ok": False, "unavailable": "empty questions"}
    out = _post("/v1/systemone", {"model": model, "state": state, "questions": questions})
    if not out.get("ok"):
        return out
    body = out["body"]
    return {
        "ok": True,
        "answers": body.get("answers", {}),
        "usage": body.get("usage", {}),
        "model": body.get("model", model),
        "latency_ms": out["latency_ms"],
        "state_hash": hashlib.sha256(state.encode("utf-8")).hexdigest()[:12],
    }


def log(kind: str, result: dict, task_id: str = "", run_id: str = "", extra: dict | None = None) -> None:
    config.ensure_dirs()
    record = {
        "ts": time.time(),
        "kind": kind,
        "task_id": task_id,
        "run_id": run_id,
        **(extra or {}),
        "ok": result.get("ok", False),
        "model": result.get("model"),
        "answers": result.get("answers"),
        "usage": result.get("usage"),
        "latency_ms": result.get("latency_ms"),
        "unavailable": result.get("unavailable"),
    }
    with (config.HOME / "decisions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


TRIAGE_CRITERIA = [
    "明显没完成，或结果与验收标准无关",
    "部分完成，有明显缺口或未验证的断言",
    "基本完成，需要抽查关键文件",
    "完成且证据充分，可以不看",
]


def triage_run(run: dict, task: dict, final_text: str, artifacts: list[dict]) -> dict:
    """Score how much human attention a finished run needs."""
    state = json.dumps(
        {
            "goal": task.get("goal", ""),
            "acceptance": task.get("acceptance", ""),
            "agent": run.get("agent"),
            "status": run.get("status"),
            "final": (final_text or "")[:6000],
            "artifacts": [a.get("path") for a in artifacts][:30],
        },
        ensure_ascii=False,
    )
    questions = {
        "completion": {
            "type": "score",
            "instructions": "按验收标准判断这次执行完成到什么程度。只依据给出的最终回复和改动文件，不要假设未展示的工作。",
            "criteria": TRIAGE_CRITERIA,
        }
    }
    result = decide(state, questions)
    log("triage", result, task.get("id", ""), run.get("id", ""), {"score_scale": TRIAGE_CRITERIA})
    return result


AGENT_PROFILE = {
    "claude": {
        "cost_tier": 3,
        "strengths": "跨文件改写、长上下文推理、设计权衡",
        "efforts": ["low", "medium", "high", "xhigh", "max"],
    },
    "codex": {
        "cost_tier": 2,
        "strengths": "单点修复、命令验证、快速迭代",
        "efforts": ["low", "medium", "high"],
    },
    "dsh": {
        "cost_tier": 1,
        "strengths": "中文写作、知识整理、长文档笔记",
        "efforts": [],
    },
}

EFFORT_CRITERIA = {
    "low": "机械改动，照着现有结构补，不需要设计",
    "medium": "要读几个文件并保持一致，有一定判断",
    "high": "跨模块推理、权衡设计或排查不确定行为",
}


def route_task(task: dict, agents: list[str]) -> dict:
    """Pick a route and a reasoning effort. Returns both in `answers`.

    Candidates come from the live route table, not a hardcoded agent list, so
    routes added in routes.json (opus/fable/sol/terra/...) are selectable. The
    criteria carry capability, price and requirements, not just a name.
    """
    table = routes.load()
    ready = set(agents or [])
    candidates = {
        name: route for name, route in table.items()
        if not ready or route.agent in ready
    }
    if len(candidates) < 2:
        return {"ok": False, "unavailable": "need at least two ready routes to route"}
    criteria = {
        name: route_meta.criterion(name, route) for name, route in candidates.items()
    }
    state = json.dumps(
        {
            "goal": task.get("goal", ""),
            "acceptance": task.get("acceptance", ""),
            "cwd_hint": task.get("title", ""),
        },
        ensure_ascii=False,
    )
    questions = {
        "route": {
            "type": "choice",
            "instructions": (
                "选择最适合独立完成这个任务的路线。按任务形态、能力上下限、价格/性价比、"
                "以及列出的特殊要求判断；成本档低的不一定够用，贵的不一定更好。"
            ),
            "criteria": criteria,
        },
        "effort": {
            "type": "choice",
            "instructions": "这个任务需要多深的推理？够用即可，不要拔高。",
            "criteria": EFFORT_CRITERIA,
        },
    }
    result = decide(state, questions)
    log("route", result, task.get("id", ""), "", {"criteria": criteria})
    return result
