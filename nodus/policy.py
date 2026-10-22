"""Policy: what kind of task is this, and which route should take it?

The existing `routes` layer is a *fallback* chain: when an attempt provably did
nothing, the next provider gets a turn. That is reliability, not allocation.
This module answers the question that comes before dispatch:

    task text -> assess(difficulty, kind, needs) -> recommend(route, why)

Two rules keep it honest:

1. **Rules first, Jev second.** The keyword/length pass runs with no network
   and no model. When a Jev key is configured, `decision.decide` reviews that
   verdict. Without it the result says ``basis="rules-only"`` — it never
   pretends a model looked at the task.
2. **No data means `unknown`.** Route metadata (`routes.py`,
   `decision.AGENT_PROFILE`) is a *description*, not a measurement. Only the
  2026-10-21 A/B (reports/ in the operator workspace) and any `nodus bench`
   results dropped into this repo's `reports/` count as measured. Everything
   else carries ``evidence.status="unknown"`` so the recommendation reads as
   the weak guess it is.

`record` writes the judgement into the run's event stream and a sidecar note,
so "why did it go to that route?" is answerable after the fact.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from . import config, db, decision, routes

SCHEMA_VERSION = 1

#: The six routes that actually exist in `routes._builtin()`. A recommendation
#: is only allowed to name one of these.
ROUTE_NAMES = (
    "claude",
    "minimax",
    "codex",
    "dsh",
    "opencode-glm",
    "opencode-gemini",
)

KINDS = ("code", "analysis", "writing", "retrieval", "ops")
NEEDS = ("tool_use", "long_context", "reasoning", "vision")

#: Where `nodus bench` / operator measurements are dropped. A JSON object with
#: `{"route": ..., "totals": {"attempts", "solved", "mean_s", ...}}` is read as
#: measured evidence for that route. Files that do not match are ignored.
REPORTS = Path(__file__).resolve().parent.parent / "reports"


# ---- rule signals ------------------------------------------------------

_CODE_PATH = re.compile(
    r"(?<![\w.])(?:[\w.-]+/)*(?:[\w-]+\.(?:py|js|tsx?|jsx|go|rs|java|rb|php|cs|cpp|c|h|"
    r"sql|sh|ps1|ya?ml|toml|json|html|css))\b",
    re.IGNORECASE,
)
_CODE_FENCE = re.compile(r"```|`[^`\n]+`")
_CODE_STRONG = re.compile(
    r"(函数|类|接口|脚本|算法|数据库|API|CLI|命令行|编译|调试|单测|单元测试|pytest|"
    r"bug|报错|崩溃|栈|git|commit|PR|merge|重构|实现|修复|封装|抽离|替换|"
    r"重命名|rename|refactor|fix|implement)",
    re.IGNORECASE,
)
_CODE_WEAK = re.compile(r"(代码|测试|配置|依赖|模块|文件|目录|日志|版本|环境变量)", re.IGNORECASE)
_MUTATE = re.compile(
    r"(修改|改动|重构|修复|实现|添加|新增|删除|替换|迁移|部署|升级|创建|安装|重命名|"
    r"fix|refactor|implement|add|remove|update|migrate)",
    re.IGNORECASE,
)
_READ_ONLY = re.compile(
    r"(查一下|查查|搜索|检索|找出|列出|读取|浏览|看看|找一下|翻一下|lookup|search)",
    re.IGNORECASE,
)
_EXECUTE = re.compile(r"(pytest|运行|执行|跑一遍|跑一下|跑全量)", re.IGNORECASE)

_REASONING = re.compile(
    r"(重构|架构|设计|权衡|根因|排查|为什么|并发|一致性|事务|锁|性能|迁移|"
    r"跨模块|多个文件|多模块|安全|鉴权|越权|权限|抽象|失效|缓存|分布式|"
    r"幂等|回滚|状态机|调度|协议|兼容|边界条件|取舍)",
    re.IGNORECASE,
)
_ANALYSIS = re.compile(
    r"(分析|评估|对比|比较|诊断|调研|研究|选型|方案|结论|检查|审计|解释|"
    r"benchmark|复盘|归因|预测)",
    re.IGNORECASE,
)
_WRITING = re.compile(
    r"(写|撰写|文档|README|说明书|说明|总结|笔记|报告|邮件|文案|翻译|润色|"
    r"标题|教程|指南|博客|文章|改成|更新一下)",
    re.IGNORECASE,
)
_RETRIEVAL = re.compile(
    r"(查|搜索|检索|找出|列出|读取|浏览|收集|调研|找一下|翻一下|原文|"
    r"lookup|search|find)",
    re.IGNORECASE,
)
_OPS = re.compile(
    r"(部署|上线|重启|启动|停止|服务|端口|daemon|守护进程|服务器|运维|docker|"
    r"systemd|k8s|定时|cron|磁盘|监控|发布|环境变量)",
    re.IGNORECASE,
)
_VISION = re.compile(r"(截图|图片|图像|看图|视觉|界面|UI|照片|视频|OCR|录屏|像素)", re.IGNORECASE)
_TOOL = re.compile(
    r"(修改|改动|重构|修复|实现|运行|执行|检查|审计|测试|部署|提交|写入|创建|删除|安装|"
    r"升级|更新|跑|pytest|git|命令|文件|数据库|调研|检索|统计|查询|数据)",
    re.IGNORECASE,
)
_LONG = re.compile(
    r"(长文|全文|整篇|整个仓库|全仓|全部|多个文件|多模块|跨模块|大规模|全量|"
    r"几百|上千|万字|3000|5000)",
    re.IGNORECASE,
)
_BROAD = re.compile(r"(整个|全仓|全部|所有|跨模块|多个文件|多模块|全量|大规模)", re.IGNORECASE)
_TRIVIAL = re.compile(
    r"(错别字|错字|typo|改名|重命名|改个名字|格式化|缩进|空格|注释|排版|"
    r"bump|版本号)",
    re.IGNORECASE,
)
_TEST_GOAL = re.compile(
    r"(保证.*测试|测试.*全绿|测试通过|全部通过|测试全绿|保持.*绿|绿)", re.IGNORECASE
)
_BUG = re.compile(r"(bug|报错|崩溃|不工作|复现|排查|失败|异常)", re.IGNORECASE)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ---- assess ------------------------------------------------------------

def _score_kind(text: str, signals: list[str]) -> tuple[str, dict[str, int], float]:
    """Name the task shape and how sure the rules are about it."""
    scores = dict.fromkeys(KINDS, 0)

    code = 0
    if _CODE_PATH.search(text):
        code += 3
        signals.append("code_path")
    if _CODE_FENCE.search(text):
        code += 2
        signals.append("code_fence")
    if _CODE_STRONG.search(text):
        code += 3
        signals.append("code")
    elif _CODE_WEAK.search(text):
        code += 1
        signals.append("code_weak")
    if code and _MUTATE.search(text):
        code += 1
    scores["code"] = code

    if _ANALYSIS.search(text):
        scores["analysis"] = 3
        signals.append("analysis")
    if _WRITING.search(text):
        scores["writing"] = 2
        signals.append("writing")
    if _RETRIEVAL.search(text):
        scores["retrieval"] = 2
        signals.append("retrieval")
    if _OPS.search(text):
        scores["ops"] = 3
        signals.append("ops")
    if _VISION.search(text):
        signals.append("vision")
    if _TOOL.search(text):
        signals.append("tool_use")
    if _LONG.search(text) or _BROAD.search(text) or len(text) >= 1200:
        signals.append("long_context")

    trivial = bool(_TRIVIAL.search(text)) and not _BROAD.search(text) and len(text) < 160
    if trivial:
        scores["writing"] = max(scores["writing"], 2) + 1
        signals.append("trivial")

    # A pure read-only lookup is retrieval even when it names a source file.
    # `查 db.py 的函数` should not be dispatched as a refactor. Executing a
    # suite, or asking for analysis/a judgement, is work, not a lookup.
    read_only = (
        bool(_READ_ONLY.search(text))
        and not _MUTATE.search(text)
        and not _OPS.search(text)
        and not _ANALYSIS.search(text)
        and not _REASONING.search(text)
        and not _EXECUTE.search(text)
    )
    if read_only:
        return "retrieval", scores, 0.75

    # Fixed tie-break order. `analysis` > `ops` > `writing` > `retrieval` >
    # `code`; code usually wins outright because path/verb signals stack.
    order = {"analysis": 5, "ops": 4, "writing": 3, "retrieval": 2, "code": 1}
    kind = max(KINDS, key=lambda name: (scores[name], order[name]))
    top = scores[kind]
    second = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0
    if top >= 4 and top >= second + 2:
        kind_confidence = 0.75
    elif top >= 2:
        kind_confidence = 0.6
    else:
        kind_confidence = 0.45
    if top == second and top:
        kind_confidence = 0.4
    return kind, scores, kind_confidence


def _score_difficulty(text: str, kind: str, signals: list[str]) -> tuple[int, list[str]]:
    trivial = "trivial" in signals
    if trivial:
        return 1, ["trivial"]

    reasons: list[str] = []
    difficulty = 1
    if kind in ("code", "ops", "analysis"):
        difficulty += 1
        reasons.append(f"{kind} baseline")
    elif kind == "writing":
        difficulty += 1
        reasons.append("writing baseline")

    reasoning = bool(_REASONING.search(text))
    broad = bool(_BROAD.search(text))
    if reasoning:
        if broad:
            difficulty += 2
            reasons.append("cross-module reasoning")
        else:
            difficulty += 1
            reasons.append("reasoning")
    if broad:
        difficulty += 1
        reasons.append("broad scope")
    if kind == "analysis" and _ANALYSIS.search(text) and not reasoning:
        difficulty += 1
        reasons.append("analysis")
    if kind == "ops" and len(text) >= 25:
        difficulty += 1
        reasons.append("multi-step ops")
    if _TEST_GOAL.search(text) and difficulty < 4:
        difficulty += 1
        reasons.append("tests must stay green")
    if _BUG.search(text) and kind == "code" and difficulty < 3:
        difficulty += 1
        reasons.append("bug to locate")
    if "long_context" in signals and kind in ("writing", "analysis", "retrieval"):
        if difficulty < 3:
            difficulty += 1
            reasons.append("long material")
    if len(text) >= 600:
        difficulty += 1
        reasons.append("long brief")
    if len(text) >= 1600:
        difficulty += 1
        reasons.append("very long brief")

    return int(_clamp(difficulty, 1, 5)), reasons


def _rules(task_text: str, context: str) -> dict:
    text = task_text.strip()
    probe = f"{text}\n{context.strip()}" if context.strip() else text
    signals: list[str] = []
    kind, scores, kind_confidence = _score_kind(probe, signals)
    difficulty, reasons = _score_difficulty(probe, kind, signals)

    needs: list[str] = []
    if kind in ("code", "ops") or _TOOL.search(probe) or _CODE_PATH.search(probe):
        needs.append("tool_use")
    if "long_context" in signals:
        needs.append("long_context")
    reasoning_signal = bool(_REASONING.search(probe))
    if difficulty >= 4 or (reasoning_signal and "trivial" not in signals):
        needs.append("reasoning")
    if _VISION.search(probe):
        needs.append("vision")
    needs = [name for name in NEEDS if name in needs]

    signal_count = len(set(signals) - {"tool_use", "long_context"})
    confidence = 0.5
    if signal_count >= 2:
        confidence += 0.15
    elif signal_count == 0:
        confidence -= 0.1
    if "trivial" in signals:
        confidence += 0.05
    confidence = round(_clamp(confidence, 0.3, 0.75), 2)

    return {
        "difficulty": difficulty,
        "kind": kind,
        "needs": needs,
        "confidence": confidence,
        "kind_confidence": kind_confidence,
        "signals": sorted(set(signals)),
        "scores": scores,
        "reasons": reasons,
    }


def _jev_questions() -> dict:
    return {
        "difficulty": {
            "type": "score",
            "instructions": "这个任务需要多深的推理和执行能力？按任务本身判断，不要按执行者偏好。",
            "criteria": [
                "1 机械改动：错别字、改名、格式化，照现有结构补",
                "2 单点小改：一个文件内的局部修改，或一次简单查询",
                "3 需要读几个文件保持一致，有明确判断但路径清楚",
                "4 跨文件/跨模块改动，或需要排查不确定行为、权衡设计",
                "5 跨模块重构、架构/并发/安全判断，结果难自证",
            ],
        },
        "kind": {
            "type": "choice",
            "instructions": "这个任务主要属于哪一类？按完成它需要的主要工作判断，不要按最终产出格式判断。",
            "criteria": {
                "code": "改代码、修 bug、重构、写脚本、改工程配置",
                "analysis": "分析、对比、评估、诊断，最后要给出结论",
                "writing": "写或改文档、README、报告、文案、翻译",
                "retrieval": "查资料、找文件、读代码后回答，不修改任何东西",
                "ops": "部署、重启、看日志、改运行环境",
            },
        },
        "needs_tool_use": {
            "type": "noul",
            "instructions": "完成这个任务，是否必须实际调用工具（改文件、跑测试、执行命令、访问仓库），而不是只靠已有上下文作答？",
        },
        "needs_long_context": {
            "type": "noul",
            "instructions": "是否必须同时容纳大量材料（整个仓库、长文档、多个文件）才能做对？",
        },
        "needs_reasoning": {
            "type": "noul",
            "instructions": "是否需要多步推理、设计权衡或根因判断，而不是照现有结构机械修改？",
        },
        "needs_vision": {
            "type": "noul",
            "instructions": "是否必须看图片/截图/界面/视频才能完成？",
        },
    }


def _prob(answer: dict | None) -> float:
    if not isinstance(answer, dict):
        return 0.0
    for key in ("noul", "probability", "score"):
        value = answer.get(key)
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def assess(task_text: str, context: str = "") -> dict:
    """Judge difficulty, kind and required capabilities.

    Rules run first and always produce an answer. Jev, when configured,
    reviews that answer in a single typed call; its verdict is recorded next
    to the rules verdict instead of replacing it silently.

    Raises `ValueError` for an empty task: a recommendation without a task is
    worse than no recommendation.
    """
    text = (task_text or "").strip()
    if not text:
        raise ValueError("task text is empty")

    rules = _rules(text, context)
    result: dict = {
        "difficulty": rules["difficulty"],
        "kind": rules["kind"],
        "needs": list(rules["needs"]),
        "confidence": rules["confidence"],
        "basis": "rules-only",
        "task_digest": _digest(text),
        "chars": len(text),
        "signals": list(rules["signals"]),
        "reasons": list(rules["reasons"]),
        "rules": {
            "difficulty": rules["difficulty"],
            "kind": rules["kind"],
            "needs": list(rules["needs"]),
            "confidence": rules["confidence"],
            "scores": rules["scores"],
        },
        "jev": {
            "available": False,
            "ok": False,
            "unavailable": "not configured",
        },
    }

    available, why = decision.available()
    if not available:
        result["jev"] = {"available": False, "ok": False, "unavailable": why}
        return result

    state = json.dumps(
        {
            "task": text[:4000],
            "context": context[:1200],
            "rules": {
                "difficulty": rules["difficulty"],
                "kind": rules["kind"],
                "needs": rules["needs"],
            },
        },
        ensure_ascii=False,
    )
    reviewed = decision.decide(state, _jev_questions())
    decision.log(
        "policy.assess",
        reviewed,
        extra={"task_digest": result["task_digest"], "rules": result["rules"]},
    )
    if not reviewed.get("ok"):
        result["jev"] = {
            "available": True,
            "ok": False,
            "unavailable": reviewed.get("unavailable") or "decision failed",
            "detail": reviewed.get("detail"),
        }
        return result

    answers = reviewed.get("answers") or {}
    difficulty_answer = answers.get("difficulty") or {}
    kind_answer = answers.get("kind") or {}

    jev_score = _number(difficulty_answer.get("score"))
    jev_difficulty = (
        int(_clamp(int(jev_score + 0.5) + 1, 1, 5)) if jev_score is not None else None
    )
    jev_difficulty_confidence = _number(difficulty_answer.get("confidence")) or 0.0

    jev_kind = str(kind_answer.get("choice") or "").strip().lower() or None
    if jev_kind not in KINDS:
        jev_kind = None
    jev_kind_confidence = _number(kind_answer.get("confidence")) or 0.0

    jev_needs = [
        name for name in NEEDS if _prob(answers.get(f"needs_{name}")) >= 0.6
    ]

    used = jev_difficulty is not None or jev_kind is not None or bool(jev_needs)
    if not used:
        result["jev"] = {
            "available": True,
            "ok": True,
            "model": reviewed.get("model"),
            "latency_ms": reviewed.get("latency_ms"),
            "unavailable": "answers were empty or unparseable",
        }
        return result

    agreement = {
        "difficulty": jev_difficulty == rules["difficulty"],
        "kind": jev_kind == rules["kind"],
        "needs": sorted(set(jev_needs) & set(rules["needs"])),
    }

    # Rules stay the backbone. Jev may move difficulty by one step, and only
    # downwards when the rules did not catch a hard signal; it can name a
    # different kind when the rules themselves were not confident.
    final_difficulty = rules["difficulty"]
    strong = bool(
        set(rules["signals"])
        & {"code", "code_path", "analysis", "ops", "trivial", "long_context"}
    )
    if jev_difficulty is not None and jev_difficulty_confidence >= 0.55:
        if jev_difficulty > rules["difficulty"]:
            final_difficulty = min(5, rules["difficulty"] + 1)
        elif jev_difficulty < rules["difficulty"] and not strong:
            final_difficulty = max(1, rules["difficulty"] - 1)

    final_kind = rules["kind"]
    if (
        jev_kind is not None
        and jev_kind_confidence >= 0.65
        and rules["kind_confidence"] < 0.7
    ):
        final_kind = jev_kind

    final_needs = [name for name in NEEDS if name in set(rules["needs"]) | set(jev_needs)]
    confidences = [c for c in (jev_difficulty_confidence, jev_kind_confidence) if c]
    jev_confidence = sum(confidences) / len(confidences) if confidences else 0.5
    if agreement["difficulty"] and agreement["kind"]:
        confidence = (rules["confidence"] + jev_confidence) / 2 + 0.05
    else:
        confidence = (rules["confidence"] + jev_confidence) / 2 - 0.05

    result.update(
        {
            "difficulty": final_difficulty,
            "kind": final_kind,
            "needs": final_needs,
            "confidence": round(_clamp(confidence, 0.3, 0.9), 2),
            "basis": "rules+jev",
            "jev": {
                "available": True,
                "ok": True,
                "model": reviewed.get("model"),
                "latency_ms": reviewed.get("latency_ms"),
                "difficulty": jev_difficulty,
                "difficulty_confidence": jev_difficulty_confidence,
                "kind": jev_kind,
                "kind_confidence": jev_kind_confidence,
                "needs": jev_needs,
                "agreement": agreement,
                "unavailable": None,
            },
        }
    )
    return result


# ---- route evidence ----------------------------------------------------

#: The only measured comparison available when this module was written:
#: 3 small code/format tasks x 2 repetitions per model.
#: Source: A:\AgentWorkspace\reports\2026-10-21_model-ab2.json and the
#: 2026-10-23 ledger. It is *not* a repo-scale benchmark; scope matters.
_AB_SOURCE = "reports/2026-10-21_model-ab2.json (workspace, 3 tasks x 2 reps)"
_AB = {
    "opencode-glm": {
        "model": "glm-5.3-flash",
        "mean_s": 12.4,
        "key_points": "10/12",
        "strict_format": "2/2",
        "failures": "0/6",
        "speedup_vs_gemini_pct": 17,
    },
    "opencode-gemini": {
        "model": "gemini-3.8-flash",
        "mean_s": 15.0,
        "key_points": "10/12",
        "strict_format": "2/2",
        "failures": "0/6",
    },
}
_AB_SCOPE = "small code/format tasks (3 tasks x 2 reps = 6/model); not repo-scale refactors"

_PROFILE = decision.AGENT_PROFILE
_ROUTE_CAPS = {
    "claude": {
        "label": "强代码/长上下文",
        "cost_tier": _PROFILE["claude"]["cost_tier"],
        "strengths": _PROFILE["claude"]["strengths"],
        "source": "decision.AGENT_PROFILE (declared) + routes.py",
    },
    "minimax": {
        "label": "claude 同协议备用",
        "cost_tier": None,
        "strengths": "与 claude 同 CLI/协议，routes.py 明确说是更弱的模型；没有价格数据",
        "source": "routes.py (declared weaker; no cost data)",
    },
    "codex": {
        "label": "单点修复/命令验证",
        "cost_tier": _PROFILE["codex"]["cost_tier"],
        "strengths": _PROFILE["codex"]["strengths"],
        "source": "decision.AGENT_PROFILE (declared) + routes.py",
    },
    "dsh": {
        "label": "低成本中文写作",
        "cost_tier": _PROFILE["dsh"]["cost_tier"],
        "strengths": _PROFILE["dsh"]["strengths"],
        "source": "decision.AGENT_PROFILE (declared) + routes.py",
    },
    "opencode-glm": {
        "label": "小任务快速执行",
        "cost_tier": None,
        "strengths": "A/B 12.4s vs gemini 15.0s，关键点 10/12，严格格式 2/2，失败 0/6",
        "source": _AB_SOURCE,
    },
    "opencode-gemini": {
        "label": "小任务 + 多模态",
        "cost_tier": None,
        "strengths": "A/B 15.0s，关键点 10/12，严格格式 2/2，失败 0/6；总账指定给多模态/看屏幕",
        "source": _AB_SOURCE,
    },
}


def _bench_evidence() -> dict[str, dict]:
    """Aggregate any `nodus bench` result files under `REPORTS`.

    Tolerant by design: the bench module owns the schema, this only reads the
    fields a recommendation needs. A malformed or unrelated JSON is skipped,
    never allowed to break dispatch.
    """
    found: dict[str, dict] = {}
    if not REPORTS.is_dir():
        return found
    for path in sorted(REPORTS.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        route = data.get("route")
        totals = data.get("totals")
        if route not in ROUTE_NAMES or not isinstance(totals, dict):
            continue
        try:
            attempts = int(totals.get("attempts") or 0)
            solved = int(totals.get("solved") or 0)
            mean_s = float(totals.get("mean_s") or 0)
            cost_cny = float(totals.get("cost_cny") or 0)
        except (TypeError, ValueError):
            continue
        if attempts <= 0:
            continue
        row = found.setdefault(
            route,
            {"attempts": 0, "solved": 0, "seconds": 0.0, "cost_cny": 0.0, "sources": []},
        )
        row["attempts"] += attempts
        row["solved"] += solved
        row["seconds"] += mean_s * attempts
        row["cost_cny"] += cost_cny
        row["sources"].append(path.name)
    return found


def evidence_for(route: str, need: str | None = None) -> dict:
    """What do we actually know about this route for this task?

    `status` is one of ``measured``, ``unknown`` or ``unsupported``. Only a
    benchmark row or the recorded A/B earns ``measured``; a docstring about a
    model's strengths does not.
    """
    if route not in ROUTE_NAMES:
        raise ValueError(f"unknown route: {route}")

    bench = _bench_evidence().get(route)
    if bench:
        attempts = bench["attempts"]
        solved = bench["solved"]
        mean_s = round(bench["seconds"] / attempts, 1) if attempts else 0.0
        return {
            "status": "measured",
            "source": ", ".join(bench["sources"]),
            "scope": f"repo bench: {attempts} attempts, solve {solved}/{attempts}, mean {mean_s}s",
            "stats": {
                "attempts": attempts,
                "solved": solved,
                "solve_rate": round(solved / attempts, 3),
                "mean_s": mean_s,
                "cost_cny": round(bench["cost_cny"], 4),
            },
            "note": "measured on this repo's eval set",
        }

    if route in _AB:
        stats = dict(_AB[route])
        if need == "vision":
            return {
                "status": "unknown",
                "source": _AB_SOURCE,
                "scope": "no vision benchmark",
                "stats": stats,
                "note": "A/B 只测了文本/代码小任务；多模态只是总账里的操作约定，不是实测",
            }
        return {
            "status": "measured",
            "source": _AB_SOURCE,
            "scope": _AB_SCOPE,
            "stats": stats,
            "note": "能力和速度有实测，但样本只有小任务；仓库级任务仍是未知",
        }

    caps = _ROUTE_CAPS[route]
    if need == "vision":
        note = "没有视觉评测；不能假设这条路由能看图"
    else:
        note = (
            "本仓库 reports/ 没有这条路由的实测；"
            f"能力描述来自 {caps['source']}，不是测量结果"
        )
    return {
        "status": "unknown",
        "source": caps["source"],
        "scope": "no measured rows",
        "stats": {},
        "note": note,
    }


def _present_routes() -> list[str]:
    table = routes.load()
    names = [name for name in ROUTE_NAMES if name in table]
    return names or list(ROUTE_NAMES)


def _route_spec(name: str) -> dict:
    route = routes.resolve(name)
    if route is None:
        return {}
    return {
        "agent": route.agent,
        "providers": list(route.providers),
        "model": route.model,
    }


# ---- recommend ---------------------------------------------------------


def _choose(difficulty: int, kind: str, needs: list[str]) -> tuple[str, str]:
    """The rule table. Returns (route, why-this-route)."""
    if "vision" in needs:
        return (
            "opencode-gemini",
            "任务需要看图/截图；六条路由里只有 opencode-gemini 被总账指定给多模态，"
            "但那是操作约定，本仓库没有视觉评测。",
        )
    if difficulty >= 4:
        if kind == "ops":
            return (
                "claude",
                "高难度 ops 需要判断影响面，claude 的元数据定位是跨文件改写/长上下文/设计权衡；"
                "没有实测数据，属于值得先测的推荐。",
            )
        if kind in ("code", "analysis", "writing", "retrieval"):
            return (
                "claude",
                "高难度任务需要跨文件推理和取舍，claude 的元数据定位正好是跨文件改写/"
                "长上下文/设计权衡；但六条路由都没有高难度实测，这条依据薄弱。",
            )
        return "claude", "高难度任务优先选元数据上最强的路由。"
    if difficulty == 3:
        if kind in ("code", "ops"):
            return (
                "codex",
                "中等难度代码/ops 需要边改边跑命令，codex 的元数据定位是单点修复/"
                "命令验证/快速迭代，成本档 2/3 比 claude 低。",
            )
        if kind == "analysis" and "reasoning" in needs:
            return (
                "claude",
                "中等难度分析且需要推理，claude 的长上下文推理定位比便宜路由更合适；"
                "但无实测。",
            )
        return (
            "dsh",
            "中等难度的写作/整理/检索不需要强代码能力；dsh 是已知成本档最低的路由，"
            "元数据定位中文写作/知识整理/长文档。",
        )
    if kind == "code":
        return (
            "codex",
            "低难度单点改动，codex 的元数据定位是单点修复/命令验证/快速迭代，"
            "成本档 2/3；比 claude 便宜，比没测过的路由有更明确的定位。",
        )
    if kind == "ops":
        return (
            "codex",
            "低难度 ops 需要执行命令并看结果，codex 的命令验证定位最贴合，成本档 2/3。",
        )
    if kind == "analysis":
        return (
            "dsh",
            "低难度分析/整理不需要强代码能力；dsh 成本档最低（1/3），"
            "元数据定位中文写作/知识整理/长文档。",
        )
    return (
        "dsh",
        "低难度写作/检索不需要强代码能力；dsh 成本档最低（1/3），"
        "元数据定位中文写作/知识整理/长文档。",
    )


def _alternative_pool(primary: str, kind: str, needs: list[str]) -> list[str]:
    if "vision" in needs:
        return [name for name in ("opencode-glm", "claude", "codex", "dsh", "minimax") if name != primary]
    if primary == "claude":
        if kind in ("code", "ops"):
            return ["codex", "opencode-glm", "minimax", "dsh", "opencode-gemini"]
        return ["dsh", "opencode-glm", "codex", "minimax", "opencode-gemini"]
    if primary == "codex":
        return ["opencode-glm", "claude", "opencode-gemini", "dsh", "minimax"]
    if primary == "dsh":
        if kind in ("writing", "retrieval"):
            return ["opencode-glm", "opencode-gemini", "minimax", "codex", "claude"]
        return ["opencode-glm", "claude", "codex", "minimax", "opencode-gemini"]
    if primary == "opencode-glm":
        return ["opencode-gemini", "codex", "dsh", "claude", "minimax"]
    if primary == "opencode-gemini":
        return ["opencode-glm", "claude", "codex", "dsh", "minimax"]
    return ["claude", "codex", "dsh", "opencode-glm", "opencode-gemini"]


def _capability(route: str) -> str:
    cap = _ROUTE_CAPS[route]
    tier = f"成本档 {cap['cost_tier']}/3" if cap["cost_tier"] else "成本档未知"
    return f"{cap['label']}；{tier}；元数据：{cap['strengths']}"


def _why_alternative(route: str, need: str | None) -> str:
    evidence = evidence_for(route, need)
    if evidence["status"] == "measured":
        return f"{_capability(route)}。有实测：{evidence['scope']}，注意样本范围。"
    return f"{_capability(route)}。无实测（unknown）：{evidence['note']}。"


def _difficulty_of(assessment: dict) -> int:
    try:
        value = int(float(assessment.get("difficulty") or 1))
    except (TypeError, ValueError):
        value = 1
    return int(_clamp(value, 1, 5))


def _recommendation_confidence(assessment: dict, evidence: dict) -> float:
    difficulty = _difficulty_of(assessment)
    if evidence["status"] == "measured" and difficulty <= 2:
        base = 0.7
    elif evidence["status"] == "measured":
        base = 0.55
    elif difficulty <= 2:
        base = 0.5
    else:
        base = 0.4
    rule_confidence = float(assessment.get("confidence") or 0.5)
    return round(_clamp(0.6 * base + 0.4 * rule_confidence, 0.3, 0.8), 2)


def recommend(assessment: dict) -> dict:
    """Pick a route for an assessment, with the evidence that supports it.

    The chosen route is always one of `ROUTE_NAMES` (and one that exists in
    `routes.load()`). Every alternative carries its own evidence block, so a
    caller never has to guess which claims are measured and which are not.
    """
    if not isinstance(assessment, dict):
        raise ValueError("assessment must be a dict")
    difficulty = _difficulty_of(assessment)
    kind = str(assessment.get("kind") or "code").lower()
    if kind not in KINDS:
        kind = "code"
    needs = [name for name in NEEDS if name in set(assessment.get("needs") or [])]
    need = "vision" if "vision" in needs else None

    present = _present_routes()
    primary, fit = _choose(difficulty, kind, needs)
    if primary not in present:
        primary = present[0]

    evidence = evidence_for(primary, need)
    need_text = ", ".join(needs) if needs else "无特殊能力"
    rationale = f"难度 {difficulty}/5，类型 {kind}，需要 {need_text}。{fit}"
    if evidence["status"] == "measured":
        rationale += f" 实测范围：{evidence['scope']}。"
    else:
        rationale += " 这条路由在本仓库没有实测数据，属于弱证据推荐，值得先测。"

    alternatives = []
    for route in _alternative_pool(primary, kind, needs):
        if route == primary or route not in present:
            continue
        alternatives.append(
            {
                "route": route,
                "why": _why_alternative(route, need),
                "evidence": evidence_for(route, need),
            }
        )
        if len(alternatives) >= 3:
            break

    unmeasured = [
        name for name in present if evidence_for(name, need)["status"] != "measured"
    ]
    return {
        "route": primary,
        "rationale": rationale,
        "alternatives": alternatives,
        "confidence": _recommendation_confidence(assessment, evidence),
        "evidence": evidence,
        "unmeasured": unmeasured,
        "route_spec": _route_spec(primary),
        "basis": assessment.get("basis") or "rules-only",
        "assessment": {
            "difficulty": difficulty,
            "kind": kind,
            "needs": needs,
            "confidence": assessment.get("confidence"),
        },
    }


# ---- trace -------------------------------------------------------------


def record(run_id: str, assess_result: dict, recommend_result: dict) -> dict:
    """Persist why this task went to this route.

    Writes three things, cheapest first:
    - an event on the run's stream, type ``policy.decision`` (when the run
      exists in this workspace);
    - a sidecar note at ``<home>/runs/<run_id>/policy.json``;
    - one line in ``<home>/policy.jsonl`` even when there is no run yet.

    A broken database must not lose the ledger line, so the two writes are
    independent and the return value says what actually happened.
    """
    config.ensure_dirs()
    payload = {
        "version": SCHEMA_VERSION,
        "ts": time.time(),
        "run_id": run_id,
        "assess": assess_result,
        "recommend": recommend_result,
    }
    result = {
        "ok": True,
        "run_id": run_id,
        "task_id": "",
        "event": "",
        "seq": 0,
        "note": "",
        "ledger": str(config.HOME / "policy.jsonl"),
        "error": "",
    }

    run: dict = {}
    if run_id:
        try:
            run = db.get_run(run_id)
        except Exception as exc:  # noqa: BLE001 - record must still leave a trace
            result["ok"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"
        if not run and not result["error"]:
            result["ok"] = False
            result["error"] = f"unknown run: {run_id}"
        if run:
            payload["task_id"] = run.get("task_id") or ""
            result["task_id"] = payload["task_id"]
            try:
                seq = db.next_seq(run_id)
                db.append_event(run_id, seq, "policy.decision", payload)
                result["event"] = "policy.decision"
                result["seq"] = seq
            except Exception as exc:  # noqa: BLE001
                result["ok"] = False
                result["error"] = f"{type(exc).__name__}: {exc}"
            note = config.HOME / "runs" / run_id / "policy.json"
            try:
                note.parent.mkdir(parents=True, exist_ok=True)
                note.write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                result["note"] = str(note)
            except OSError as exc:
                result["ok"] = False
                result["error"] = f"{type(exc).__name__}: {exc}"

    try:
        with (config.HOME / "policy.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"

    return result
