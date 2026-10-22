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
import math
import re
import statistics
import time
from pathlib import Path

from . import config, db, decision, routes

SCHEMA_VERSION = 2

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

#: Local run history: how many finished runs a route is judged on, and how many
#: it takes before the numbers may call themselves measured. One lucky run is
#: not evidence, and neither is a benchmark that never shows a price.
HISTORY_WINDOW = 30
HISTORY_MIN = 3

#: Scoring constants. `score = capability_fit + measured_solve_bonus
#: - cost_penalty - time_penalty`; every term is written to the decision event
#: so a reader can re-add the numbers instead of trusting a total.
#:
#: - Solving matters more as the task gets harder; a 10x price gap must not
#:   decide an architecture question, so the price/time penalties are scaled
#:   down by `_PRICE_SENSITIVITY`.
#: - The bench's three tasks are small; their solve rate is discounted by
#:   `_OUT_OF_SCOPE` when the task is harder than anything the bench ran.
#: - A missing price is not zero: it gets the median penalty of the routes
#:   that do have one, so "unknown" sits in the middle of the field.
_SOLVE_WEIGHT = {1: 0.30, 2: 0.30, 3: 0.55, 4: 0.85, 5: 0.85}
_COST_WEIGHT = 0.12
_COST_CAP = 0.50
_TIME_WEIGHT = 0.10
_TIME_CAP = 0.35
_PRICE_SENSITIVITY = {1: 1.0, 2: 1.0, 3: 0.7, 4: 0.4, 5: 0.25}
_OUT_OF_SCOPE = 0.35
_CHEAP_RATIO = 1.5
_CHEAP_SOLVE = 2 / 3
_HIGH_SOLVE_GAP = 0.15


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


def assess(task_text: str, context: str = "", review: bool = True) -> dict:
    """Judge difficulty, kind and required capabilities.

    Rules run first and always produce an answer. Jev, when configured,
    reviews that answer in a single typed call; its verdict is recorded next
    to the rules verdict instead of replacing it silently.

    `review=False` is for the dispatch path where the operator already named a
    route: the assessment is recorded as context, and spending a network call
    on a decision nobody is going to use is not worth the latency. The result
    says so in `jev.unavailable` instead of pretending Jev said nothing.

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
    if not review:
        result["jev"] = {
            "available": True,
            "ok": False,
            "unavailable": "skipped: the route was chosen by the operator",
        }
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
        "strengths": (
            "与 claude 同 CLI/协议，routes.py 明确说是更弱的模型；"
            "bench 实测 3/3、¥0.0289/run"
        ),
        "source": "routes.py (declared weaker) + reports/2026-10-23_bench_minimax.json",
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

#: `opencode-gemini` is the only route the operator ledger assigns to images.
#: Declared, not measured: there is no vision benchmark in `reports/`.
_VISION_ROUTES = frozenset({"opencode-gemini"})

#: Declared fitness, *not* measurement: how well each route is said to fit a
#: task kind, and the difficulty band it is declared for. Derived from
#: `_ROUTE_CAPS` labels and `decision.AGENT_PROFILE`; scoring adds measured
#: data on top rather than replacing this. Outside the band the fit decays, so
#: a route declared for small tasks cannot win a refactor on price alone.
_ROUTE_FIT = {
    "claude": {
        "kinds": {"code": 1.0, "analysis": 1.0, "ops": 0.9, "writing": 0.7, "retrieval": 0.7},
        "floor": 2,
        "ceiling": 5,
    },
    "minimax": {
        "kinds": {"code": 0.8, "ops": 0.7, "analysis": 0.7, "writing": 0.6, "retrieval": 0.6},
        "floor": 1,
        "ceiling": 3,
    },
    "codex": {
        "kinds": {"code": 1.0, "ops": 0.9, "analysis": 0.6, "writing": 0.4, "retrieval": 0.6},
        "floor": 1,
        "ceiling": 3,
    },
    "dsh": {
        "kinds": {"writing": 1.0, "retrieval": 0.8, "analysis": 0.7, "code": 0.4, "ops": 0.3},
        "floor": 1,
        "ceiling": 3,
    },
    "opencode-glm": {
        "kinds": {"code": 0.9, "ops": 0.8, "retrieval": 0.7, "analysis": 0.6, "writing": 0.6},
        "floor": 1,
        "ceiling": 3,
    },
    "opencode-gemini": {
        "kinds": {"code": 0.85, "ops": 0.7, "retrieval": 0.7, "analysis": 0.6, "writing": 0.6},
        "floor": 1,
        "ceiling": 3,
    },
}


def _bench_evidence() -> dict[str, dict]:
    """Aggregate any `nodus bench` result files under `REPORTS`.

    Tolerant by design: the bench module owns the schema, this only reads the
    fields a recommendation needs. A malformed or unrelated JSON is skipped,
    never allowed to break dispatch.

    Two extra facts are derived, because the totals alone lie:

    - `priced_attempts` vs `attempts`: a missing model price is written as
      `cost: 0.0`, so a total that covers only one of three runs must not be
      averaged as if the route were cheap. Incomplete pricing stays unknown.
    - `tested_difficulty`: the hardest task title in the report, scored by the
      same rule table used for live tasks. It is how scoring knows that a
      solve rate measured on small tasks does not prove a hard one.
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
        rows = data.get("attempts")
        priced = 0
        unpriced = 0
        tested = 0
        durations: list[float] = []
        if isinstance(rows, list) and rows:
            for item in rows:
                if not isinstance(item, dict):
                    unpriced += 1
                    continue
                # Prefer the difficulty the task declares. Re-deriving it from
                # the title used to score a 521-line debugging task as a 2 and
                # made every "hard task" claim look unsupported.
                declared = _number(item.get("difficulty"))
                title = str(item.get("title") or "").strip()
                if declared is not None and declared > 0:
                    tested = max(tested, int(declared))
                elif title:
                    tested = max(tested, int(_rules(title, "")["difficulty"]))
                duration = _number(item.get("duration_s"))
                if duration is not None and duration >= 0:
                    durations.append(duration)
                cost = _number(item.get("cost"))
                if cost is not None and cost > 0:
                    priced += 1
                else:
                    # 0.0 is how a provider without a price table reports a
                    # run; it is not a free run.
                    unpriced += 1
        else:
            # No per-attempt rows: a non-zero total is the only evidence that
            # the run was priced at all.
            priced = attempts if cost_cny > 0 else 0
            unpriced = attempts - priced
        row = found.setdefault(
            route,
            {
                "attempts": 0,
                "solved": 0,
                "seconds": 0.0,
                "cost_cny": 0.0,
                "priced_attempts": 0,
                "unpriced_attempts": 0,
                "tested_difficulty": 0,
                "durations": [],
                "sources": [],
            },
        )
        row["attempts"] += attempts
        row["solved"] += solved
        row["seconds"] += mean_s * attempts
        row["cost_cny"] += cost_cny
        row["priced_attempts"] += priced
        row["unpriced_attempts"] += unpriced
        row["tested_difficulty"] = max(row["tested_difficulty"], tested)
        row["durations"].extend(durations)
        row["sources"].append(path.name)
    for row in found.values():
        durations = row.pop("durations")
        # The task asks for a median: one stuck run must not define the route.
        row["median_s"] = (
            round(statistics.median(durations), 1)
            if durations
            else round(row["seconds"] / row["attempts"], 1)
        )
    return found


def _usage_cost(usage_json: str | None) -> tuple[float, str, str] | None:
    """(cost, currency, basis) when the run recorded a numeric price."""
    try:
        usage = json.loads(usage_json or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(usage, dict):
        return None
    cost = usage.get("cost")
    if not isinstance(cost, (int, float)) or isinstance(cost, bool):
        return None
    currency = str(usage.get("currency") or "unknown").strip() or "unknown"
    return float(cost), currency, str(usage.get("cost_basis") or "").strip()


def _run_history(route: str, limit: int = HISTORY_WINDOW) -> dict:
    """What this workspace actually saw on `route`, from the run table.

    Only finished runs count. A run is a *success* only when it completed and
    its tests went green: exit code 0 with nothing verified is the exact thing
    this project exists to stop calling success. Costs stay in their own
    currency — converting CNY and USD at today's rate would invent a number
    nobody measured, so the caller gets one bucket per currency.

    Never raises: a broken database must degrade the recommendation to
    "unknown", not stop a dispatch.
    """
    window = max(1, int(limit))
    out = {
        "status": "none",
        "samples": 0,
        "window": window,
        "stats": {},
        "note": "本工作区没有这条路由的历史 run",
    }
    try:
        with db.connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, status, started_at, ended_at, usage_json FROM run"
                    " WHERE workspace_id=? AND status IN ('completed','failed')"
                    " AND (route=? OR ((route IS NULL OR route='') AND agent=?))"
                    " ORDER BY started_at DESC LIMIT ?",
                    (config.workspace_id(), route, route, window),
                ).fetchall()
            ]
        marks = db.verify_states([row["id"] for row in rows]) if rows else {}
    except Exception as exc:  # noqa: BLE001 - evidence must never break dispatch
        out["note"] = f"本地历史读取失败，按无数据处理：{type(exc).__name__}: {exc}"
        return out
    if not rows:
        return out

    completed = sum(1 for row in rows if row["status"] == "completed")
    green = sum(
        1
        for row in rows
        if row["status"] == "completed" and marks.get(row["id"]) == "ok"
    )
    durations: list[float] = []
    for row in rows:
        started = row.get("started_at")
        ended = row.get("ended_at")
        if isinstance(started, (int, float)) and isinstance(ended, (int, float)):
            if ended >= started:
                durations.append(float(ended) - float(started))

    costs: dict[str, dict] = {}
    for row in rows:
        priced = _usage_cost(row.get("usage_json"))
        if priced is None:
            continue
        cost, currency, basis = priced
        bucket = costs.setdefault(
            currency, {"runs": 0, "total": 0.0, "mean": 0.0, "basis": []}
        )
        bucket["runs"] += 1
        bucket["total"] += cost
        if basis and basis not in bucket["basis"]:
            bucket["basis"].append(basis)
    for bucket in costs.values():
        bucket["total"] = round(bucket["total"], 6)
        bucket["mean"] = round(bucket["total"] / bucket["runs"], 6)
        bucket["basis"].sort()

    samples = len(rows)
    enough = samples >= HISTORY_MIN
    # Two different questions, and conflating them made every route look
    # useless: "did the process finish" is about the route, "did the tests go
    # green" is about the project having tests at all. A repo with no test
    # command produces verify=none forever, so green/samples reads 0% even
    # when every run did its job.
    checked = [row for row in rows if marks.get(row["id"]) in ("ok", "red")]
    checked_ok = sum(1 for row in checked if marks.get(row["id"]) == "ok")
    durations.sort()
    median_s = None
    if durations:
        mid = len(durations) // 2
        median_s = (
            durations[mid]
            if len(durations) % 2
            else (durations[mid - 1] + durations[mid]) / 2
        )
    out.update(
        {
            "status": "measured" if enough else "insufficient",
            "samples": samples,
            "stats": {
                "runs": samples,
                "window": window,
                "completed": completed,
                "verified_green": green,
                "success_rate": round(green / samples, 3),
                "finished_rate": round(completed / samples, 3),
                "verified_samples": len(checked),
                "verified_ok_rate": (
                    round(checked_ok / len(checked), 3) if checked else None
                ),
                "duration_runs": len(durations),
                "mean_s": round(sum(durations) / len(durations), 1) if durations else None,
                # One stuck run drags a mean to hours; the median is how long
                # this route usually takes.
                "median_s": round(median_s, 1) if median_s is not None else None,
                "cost": costs,
            },
            "note": (
                f"本地历史：最近 {samples} 次已结束 run；完成 {completed}/{samples}；"
                f"有测试证据的 {len(checked)} 次里绿 {checked_ok} 次"
                f"（全量绿 {green}/{samples}，含没有测试命令的项目）；"
                "耗时给中位数（均值会被卡住的 run 拉爆）；"
                "成本按 currency 分组，不换算汇率。"
                + ("" if enough else f" 样本 < {HISTORY_MIN}，只标记不下结论。")
            ),
        }
    )
    return out


def evidence_for(
    route: str,
    need: str | None = None,
    history: dict | None = None,
    bench: dict | None = None,
) -> dict:
    """What do we actually know about this route for this task?

    `status` is one of ``measured``, ``unknown``, ``unsupported`` or
    ``insufficient``. Only a benchmark row, the recorded A/B, or at least
    `HISTORY_MIN` local runs earn ``measured``; a docstring about a model's
    strengths does not, and neither does one lucky dispatch.

    Precedence is **bench, then local history, then A/B**. A bench compares the
    routes on the same tasks, which is the only fair way to rank them; local
    history is this workspace's real work but each route saw different tasks,
    so an easy-run average must not outvote a controlled comparison. History
    is still echoed under `history` for cost/speed context and fills the gap
    when no bench row exists.

    `history` and `bench` let a caller that is already judging every route pass
    the computed blocks in, so one recommendation does not re-read the same
    table or the same JSON six times.
    """
    if route not in ROUTE_NAMES:
        raise ValueError(f"unknown route: {route}")

    hist = _run_history(route) if history is None else history
    bench_table = _bench_evidence() if bench is None else bench
    row = bench_table.get(route)
    ab = _AB.get(route)

    def with_sources(block: dict) -> dict:
        block["route"] = route
        block["history"] = hist
        if row:
            block["bench"] = {
                "attempts": row["attempts"],
                "solved": row["solved"],
                "sources": list(row["sources"]),
                "tested_difficulty": row["tested_difficulty"],
                "priced_attempts": row["priced_attempts"],
            }
        if ab:
            block["ab"] = dict(ab)
        return block

    # Vision is a declared ability, not a measurement: the ledger assigns
    # multimodal work to one route. Saying "unknown" for the others would let
    # scoring dispatch a screenshot to a text-only route.
    if need == "vision" and route not in _VISION_ROUTES:
        return with_sources(
            {
                "status": "unsupported",
                "measured_by": None,
                "source": "route declaration",
                "scope": "no declared vision path",
                "stats": {},
                "note": "未声明视觉能力（这是能力声明缺失，不是测量结论）",
            }
        )

    if row:
        attempts = row["attempts"]
        solved = row["solved"]
        mean_s = round(row["seconds"] / attempts, 1) if attempts else 0.0
        complete = attempts > 0 and row["priced_attempts"] >= attempts and row["cost_cny"] > 0
        return with_sources(
            {
                "status": "measured",
                "measured_by": "bench",
                "source": ", ".join(row["sources"]),
                "scope": (
                    f"repo bench: {attempts} attempts, solve {solved}/{attempts},"
                    f" mean {mean_s}s"
                ),
                "stats": {
                    "attempts": attempts,
                    "solved": solved,
                    "solve_rate": round(solved / attempts, 3),
                    "mean_s": mean_s,
                    "median_s": row["median_s"],
                    "cost_cny": round(row["cost_cny"], 4),
                    "cost_complete": complete,
                    "priced_attempts": row["priced_attempts"],
                    "tested_difficulty": row["tested_difficulty"],
                },
                "note": (
                    "measured on this repo's eval set"
                    + ("" if complete else "；价格数据不完整，成本按未知处理")
                ),
            }
        )

    # Local history is the only source that knows what this repo's tasks cost.
    # It is still not a vision test (handled above), and its success rate only
    # means "tests went green", so it is used when no controlled benchmark row
    # exists for this route.
    if hist["status"] == "measured":
        stats = hist["stats"]
        return with_sources(
            {
                "status": "measured",
                "measured_by": "history",
                "source": f"local run history (last {stats['runs']} finished runs)",
                "scope": (
                    f"本地历史：{stats['runs']} 次已结束 run，"
                    f"有测试证据的 {stats['verified_samples']} 次里绿 "
                    f"{stats['verified_green']} 次，中位耗时 {stats['median_s']}s"
                ),
                "stats": dict(stats),
                "note": hist["note"],
            }
        )

    if ab:
        stats = dict(ab)
        if need == "vision":
            return with_sources(
                {
                    "status": "unknown",
                    "measured_by": None,
                    "source": _AB_SOURCE,
                    "scope": "no vision benchmark",
                    "stats": stats,
                    "note": "A/B 只测了文本/代码小任务；多模态只是总账里的操作约定，不是实测",
                }
            )
        return with_sources(
            {
                "status": "measured",
                "measured_by": "ab",
                "source": _AB_SOURCE,
                "scope": _AB_SCOPE,
                "stats": stats,
                "note": "能力和速度有实测，但样本只有小任务；仓库级任务仍是未知",
            }
        )

    caps = _ROUTE_CAPS[route]
    if need == "vision":
        note = "没有视觉评测；不能假设这条路由能看图"
    else:
        note = (
            "本仓库 reports/ 没有这条路由的实测；"
            f"能力描述来自 {caps['source']}，不是测量结果"
        )
    status = "insufficient" if hist["status"] == "insufficient" else "unknown"
    return with_sources(
        {
            "status": status,
            "measured_by": None,
            "source": caps["source"],
            "scope": (
                f"only {hist['samples']} local run(s), below the {HISTORY_MIN} needed"
                if hist["status"] == "insufficient"
                else "no measured rows"
            ),
            "stats": {},
            "note": note,
        }
    )


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


def _capability_fit(route: str, difficulty: int, kind: str) -> float:
    """How well the route's *declared* profile fits this task, 0-1.

    Declared, not measured: this is the `_ROUTE_FIT` table. Outside the band a
    route is declared for the fit decays, so a small-task route does not win a
    refactor just because it is cheap.
    """
    spec = _ROUTE_FIT[route]
    fit = float(spec["kinds"].get(kind, 0.3))
    if difficulty > spec["ceiling"]:
        fit *= max(0.25, 1.0 - 0.35 * (difficulty - spec["ceiling"]))
    elif difficulty < spec["floor"]:
        fit *= max(0.5, 1.0 - 0.15 * (spec["floor"] - difficulty))
    return round(fit, 3)


def _measure(route: str, evidence: dict) -> dict:
    """The numbers scoring is allowed to use, and where each came from.

    Bench numbers win over history when both exist: only the bench ran the
    routes on the same tasks. `solve_rate` is left `None` unless the sample is
    big enough to mean something, which is what keeps one bad run from
    eliminating a route. A price is known only when it is positive and
    complete; `cost: 0.0` from a provider without a price table is unknown,
    not free.
    """
    caps = _ROUTE_CAPS[route]
    out = {
        "route": route,
        "status": evidence.get("status") or "unknown",
        "measured_by": evidence.get("measured_by") or "",
        "solve_rate": None,
        "solved": None,
        "attempts": None,
        "seconds": None,
        "cost_cny": None,
        "cost_known": False,
        "tested_difficulty": None,
        "declared_cost_tier": caps.get("cost_tier"),
        "note": "",
    }
    if out["status"] == "unsupported":
        out["note"] = evidence.get("note") or "unsupported"
        return out

    stats = evidence.get("stats") or {}
    source = out["measured_by"]

    if source == "bench":
        notes: list[str] = []
        out["solve_rate"] = _number(stats.get("solve_rate"))
        out["solved"] = stats.get("solved")
        out["attempts"] = stats.get("attempts")
        out["seconds"] = _number(stats.get("median_s")) or _number(stats.get("mean_s"))
        out["tested_difficulty"] = stats.get("tested_difficulty") or None
        attempts = int(out["attempts"] or 0)
        if attempts and attempts < HISTORY_MIN:
            # Same guard as history: one sample is not a solve rate.
            out["solve_rate"] = None
            notes.append(f"bench 只有 {attempts} 次，solve 按未知处理")
        total = _number(stats.get("cost_cny"))
        if stats.get("cost_complete") and attempts and total and total > 0:
            out["cost_cny"] = round(total / attempts, 6)
            out["cost_known"] = True
        elif total is not None:
            notes.append("价格数据不完整，成本按未知处理")
        if notes:
            out["note"] = "；".join(notes)
    elif source == "history":
        verified = int(stats.get("verified_samples") or 0)
        rate = _number(stats.get("verified_ok_rate"))
        # One green/red run is not a solve rate. Below HISTORY_MIN the run
        # history stays context, not a reason to drop the route.
        if rate is not None and verified >= HISTORY_MIN:
            out["solve_rate"] = rate
            out["solved"] = round(rate * verified)
            out["attempts"] = verified
        out["seconds"] = _number(stats.get("median_s")) or _number(stats.get("mean_s"))
        cny = (stats.get("cost") or {}).get("CNY") or {}
        mean = _number(cny.get("mean"))
        if mean is not None and mean > 0:
            out["cost_cny"] = mean
            out["cost_known"] = True
    elif source == "ab":
        # The A/B recorded speed and format checks, not a solve count.
        out["seconds"] = _number(stats.get("mean_s"))
    return out


def _covers(measure: dict, difficulty: int) -> bool:
    """Does this measurement actually cover a task this hard?"""
    if measure.get("measured_by") == "bench":
        return int(measure.get("tested_difficulty") or 0) >= difficulty
    if measure.get("measured_by") == "history":
        # History has no difficulty label; it can speak to small work only.
        return difficulty <= 2
    return False


def _is_cheap(row: dict, cheapest: float | None) -> bool:
    if row["cost_known"] and cheapest:
        return row["cost_cny"] <= cheapest * _CHEAP_RATIO
    tier = row["declared_cost_tier"]
    return row["cost_cny"] is None and tier is not None and tier <= 2


def _score_candidates(
    difficulty: int, kind: str, measures: dict[str, dict]
) -> tuple[list[dict], list[dict]]:
    """Score every route and apply the guardrails. Pure: no DB, no files.

    `score = capability_fit + measured_solve_bonus - cost_penalty
    - time_penalty`. Cost and time are relative to the cheapest/fastest route
    in this same pool; an unknown price takes the median penalty of the routes
    that do have one, so "unknown" is neither free nor the worst.
    """
    rows: list[dict] = []
    for route, measure in measures.items():
        cap = _capability_fit(route, difficulty, kind)
        rows.append(
            {
                "route": route,
                "score": cap,
                "capability_fit": cap,
                "measured_solve_bonus": 0.0,
                "cost_penalty": 0.0,
                "time_penalty": 0.0,
                "solve_rate": measure.get("solve_rate"),
                "solved": measure.get("solved"),
                "attempts": measure.get("attempts"),
                "seconds": measure.get("seconds"),
                "cost_cny": measure.get("cost_cny"),
                "cost_known": bool(measure.get("cost_known")),
                "measured_by": measure.get("measured_by") or "",
                "tested_difficulty": measure.get("tested_difficulty"),
                "covers_difficulty": _covers(measure, difficulty),
                "declared_cost_tier": measure.get("declared_cost_tier"),
                "cost_basis": "unknown",
                "time_basis": "unknown",
                "excluded": "",
                "pool": False,
                "note": measure.get("note") or "",
            }
        )
        if measure.get("status") == "unsupported":
            rows[-1]["excluded"] = measure.get("note") or "unsupported"

    eligible = [row for row in rows if not row["excluded"]]
    known_costs = [row["cost_cny"] for row in eligible if row["cost_known"] and row["cost_cny"]]
    known_times = [row["seconds"] for row in eligible if row["seconds"] and row["seconds"] > 0]
    cheapest = min(known_costs) if known_costs else None
    fastest = min(known_times) if known_times else None

    raw_cost: dict[str, float] = {}
    raw_time: dict[str, float] = {}
    for row in eligible:
        if row["cost_known"] and cheapest:
            raw_cost[row["route"]] = min(
                _COST_CAP, _COST_WEIGHT * math.log2(row["cost_cny"] / cheapest)
            )
        if row["seconds"] and fastest:
            raw_time[row["route"]] = min(
                _TIME_CAP, _TIME_WEIGHT * math.log2(row["seconds"] / fastest)
            )
    # A missing number takes the middle of the measured field, not zero.
    neutral_cost = statistics.median(raw_cost.values()) if raw_cost else 0.0
    neutral_time = statistics.median(raw_time.values()) if raw_time else 0.0

    sensitivity = _PRICE_SENSITIVITY.get(difficulty, 0.4)
    solve_weight = _SOLVE_WEIGHT.get(difficulty, 0.55)
    for row in eligible:
        row["cost_penalty"] = round(
            raw_cost.get(row["route"], neutral_cost) * sensitivity, 3
        )
        row["time_penalty"] = round(
            raw_time.get(row["route"], neutral_time) * sensitivity, 3
        )
        if row["cost_known"]:
            row["cost_basis"] = "measured"
        elif known_costs:
            row["cost_basis"] = "unknown (median of measured)"
        else:
            row["cost_basis"] = "unknown (no measured cost)"
        if row["seconds"]:
            row["time_basis"] = "measured"
        elif known_times:
            row["time_basis"] = "unknown (median of measured)"
        else:
            row["time_basis"] = "unknown (no measured time)"
        rate = row["solve_rate"]
        if rate is None:
            bonus = 0.0
        else:
            discount = 1.0 if row["covers_difficulty"] else _OUT_OF_SCOPE
            bonus = solve_weight * float(rate) * discount
        row["measured_solve_bonus"] = round(bonus, 3)
        row["score"] = round(
            row["capability_fit"] + bonus - row["cost_penalty"] - row["time_penalty"], 3
        )

    rows.sort(
        key=lambda row: (
            -round(row["score"], 3),
            0 if row["measured_by"] else 1,
            row["route"],
        )
    )

    # ---- guardrails ----------------------------------------------------
    guardrails: list[dict] = []
    pool = list(eligible)

    if difficulty <= 2:
        qualified = [
            row
            for row in eligible
            if row["solve_rate"] is not None
            and float(row["solve_rate"]) >= _CHEAP_SOLVE
            and row["capability_fit"] >= 0.5
            and _is_cheap(row, cheapest)
        ]
        if qualified:
            qualified_routes = {row["route"] for row in qualified}
            dropped = [
                row["route"] for row in eligible if row["route"] not in qualified_routes
            ]
            pool = qualified
            guardrails.append(
                {
                    "rule": "low-difficulty-cheap",
                    "reason": (
                        f"难度 {difficulty}/5：solve 至少 {_CHEAP_SOLVE:.2f} 的便宜路由优先，"
                        "不用贵路由改错别字"
                    ),
                    "kept": sorted(qualified_routes),
                    "dropped": dropped,
                }
            )

    if difficulty >= 4 and pool:
        covered = [
            row
            for row in pool
            if row["solve_rate"] is not None and row["covers_difficulty"]
        ]
        if len(covered) >= 2:
            covered.sort(key=lambda row: float(row["solve_rate"]), reverse=True)
            top = float(covered[0]["solve_rate"])
            second = float(covered[1]["solve_rate"])
            if top - second >= _HIGH_SOLVE_GAP:
                proven_routes = {
                    row["route"]
                    for row in covered
                    if float(row["solve_rate"]) >= top - 1e-9
                }
                narrowed = [row for row in pool if row["route"] in proven_routes]
                if narrowed:
                    dropped = [
                        row["route"]
                        for row in pool
                        if row["route"] not in proven_routes
                    ]
                    pool = narrowed
                    guardrails.append(
                        {
                            "rule": "high-difficulty-proven",
                            "reason": (
                                f"难度 {difficulty}/5：{covered[0]['route']} 实测 solve "
                                f"{top:.2f}，比次高 {second:.2f} 高 "
                                f"{top - second:.2f}，贵也选它"
                            ),
                            "kept": sorted(proven_routes),
                            "dropped": dropped,
                        }
                    )

    if not pool:
        # Every route was excluded (should not happen with the built-ins). Fall
        # back to declaration order rather than raising during dispatch.
        pool = eligible or rows
        guardrails.append(
            {
                "rule": "fallback",
                "reason": "没有可用候选，退回声明顺序",
                "kept": [row["route"] for row in pool],
                "dropped": [],
            }
        )

    kept = {row["route"] for row in pool}
    for row in rows:
        row["pool"] = row["route"] in kept
    return rows, guardrails


def _compare(winner: dict, rival: dict) -> str:
    """One sentence comparing the winner with the next-best route."""
    clauses: list[str] = []
    if abs(winner["capability_fit"] - rival["capability_fit"]) >= 0.05:
        clauses.append(
            f"能力声明 {winner['capability_fit']:.2f} vs {rival['capability_fit']:.2f}"
        )
    measured: list[str] = []
    if winner["seconds"] and rival["seconds"]:
        ratio = rival["seconds"] / winner["seconds"]
        if ratio >= 1.05:
            measured.append(f"快 {ratio:.1f} 倍")
        elif ratio <= 0.95:
            measured.append(f"慢 {1 / ratio:.1f} 倍")
        else:
            measured.append("耗时相近")
    if winner["cost_known"] and rival["cost_known"]:
        ratio = rival["cost_cny"] / winner["cost_cny"]
        if ratio >= 1.05:
            measured.append(f"便宜 {ratio:.1f} 倍")
        elif ratio <= 0.95:
            measured.append(f"贵 {1 / ratio:.1f} 倍")
        else:
            measured.append("价格相近")
    if winner["solve_rate"] is not None and rival["solve_rate"] is not None:
        if abs(winner["solve_rate"] - rival["solve_rate"]) < 0.01:
            measured.append(f"同样 {winner['solved']}/{winner['attempts']}")
        else:
            measured.append(
                f"solve {winner['solved']}/{winner['attempts']}"
                f" vs {rival['solved']}/{rival['attempts']}"
            )
    if measured:
        clauses.append(f"{winner['route']} " + "、".join(measured))
    scope = max(
        int(winner.get("tested_difficulty") or 0), int(rival.get("tested_difficulty") or 0)
    )
    if (
        winner["solve_rate"] is not None
        and not (winner["covers_difficulty"] and rival["covers_difficulty"])
        and scope
    ):
        clauses.append(f"bench 只测到难度 {scope}，solve 已按 {_OUT_OF_SCOPE:.2f} 折减")
    if not clauses:
        clauses.append(
            f"{winner['route']} 与 {rival['route']} 都缺同口径实测，按能力声明与成本档排序"
        )
    return "；".join(clauses)


def _explain(rows: list[dict], winner: dict, rival: dict | None) -> str:
    scores = " / ".join(f"{row['route']} {row['score']:.2f}" for row in rows if not row["excluded"])
    text = scores + (f"（{_compare(winner, rival)}）" if rival else "")
    excluded: dict[str, list[str]] = {}
    for row in rows:
        if row["excluded"]:
            excluded.setdefault(row["excluded"], []).append(row["route"])
    if excluded:
        text += "；" + "、".join(
            f"{'/'.join(routes)}（排除：{reason}）" for reason, routes in excluded.items()
        )
    return text


def _fit_sentence(winner: dict, rival: dict | None) -> str:
    text = (
        f"打分 {winner['route']} {winner['score']:.2f}"
        f"（能力 {winner['capability_fit']:.2f} + solve {winner['measured_solve_bonus']:.2f}"
        f" - 成本 {winner['cost_penalty']:.2f} - 耗时 {winner['time_penalty']:.2f}）"
    )
    if rival:
        text += f"；次选 {rival['route']} {rival['score']:.2f}"
    return text


def _live_measures(
    need: str | None = None, present: list[str] | None = None
) -> tuple[dict[str, dict], dict[str, dict]]:
    """(scoring facts, evidence blocks) for every route present on this machine."""
    names = present or _present_routes()
    bench = _bench_evidence()
    measures: dict[str, dict] = {}
    table: dict[str, dict] = {}
    for name in names:
        history = _run_history(name)
        evidence = evidence_for(name, need, history, bench)
        table[name] = evidence
        measures[name] = _measure(name, evidence)
    return measures, table


def _choose(
    difficulty: int, kind: str, measures: dict[str, dict] | None = None
) -> tuple[str, str, dict]:
    """Score the routes, apply guardrails, return (route, why, scoring).

    `measures` maps route name to one fact dict (`_measure`'s output). A caller
    with the old `(difficulty, kind, needs)` shape may pass the needs list; it
    is ignored and the live facts are read, because needs are already reflected
    in each route's evidence status.
    """
    if isinstance(measures, (list, tuple, set)):
        measures = None
    if measures is None:
        measures, _ = _live_measures()
    rows, guardrails = _score_candidates(difficulty, kind, measures)
    if not rows:
        raise ValueError("no routes to score")
    pool = [row for row in rows if row["pool"]]
    if not pool:
        pool = [row for row in rows if not row["excluded"]] or rows
    winner = pool[0]
    rival = next((row for row in pool if row["route"] != winner["route"]), None)
    scoring = {
        "version": 1,
        "formula": (
            "score = capability_fit + measured_solve_bonus - cost_penalty - time_penalty"
        ),
        "weights": {
            "solve_by_difficulty": dict(_SOLVE_WEIGHT),
            "cost_per_log2": _COST_WEIGHT,
            "cost_cap": _COST_CAP,
            "time_per_log2": _TIME_WEIGHT,
            "time_cap": _TIME_CAP,
            "price_sensitivity_by_difficulty": dict(_PRICE_SENSITIVITY),
            "out_of_scope_solve": _OUT_OF_SCOPE,
            "cheap_ratio": _CHEAP_RATIO,
            "cheap_solve": round(_CHEAP_SOLVE, 3),
        },
        "candidates": rows,
        "guardrails": guardrails,
        "winner": winner["route"],
        "explain": _explain(rows, winner, rival),
    }
    return winner["route"], _fit_sentence(winner, rival), scoring


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


def _why_alternative(route: str, evidence: dict) -> str:
    if evidence["status"] == "measured":
        return f"{_capability(route)}。有实测：{evidence['scope']}，注意样本范围。"
    if evidence["status"] == "unsupported":
        return f"{_capability(route)}。不适用：{evidence['note']}。"
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
    # One query per route, not one per mention: `unmeasured` alone would ask
    # for every route and then the alternatives would ask again.
    measures, table = _live_measures(need, present)
    primary, fit, scoring = _choose(difficulty, kind, measures)
    if primary not in present:
        primary = present[0]

    evidence = table[primary]
    need_text = ", ".join(needs) if needs else "无特殊能力"
    rationale = f"难度 {difficulty}/5，类型 {kind}，需要 {need_text}。{fit}。"
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
                "why": _why_alternative(route, table[route]),
                "evidence": table[route],
            }
        )
    # Same order as the score line: the strongest alternative is the one a
    # reader wants to compare against, not whichever the declaration list
    # happens to name first.
    scored = {row["route"]: row for row in scoring.get("candidates") or []}
    alternatives.sort(
        key=lambda alt: (-(scored.get(alt["route"]) or {}).get("score", -1.0), alt["route"])
    )
    alternatives = alternatives[:3]

    unmeasured = [
        name for name in present if table[name]["status"] != "measured"
    ]
    return {
        "route": primary,
        "rationale": rationale,
        "alternatives": alternatives,
        "confidence": _recommendation_confidence(assessment, evidence),
        "evidence": evidence,
        "unmeasured": unmeasured,
        "scoring": scoring,
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


def record(
    run_id: str,
    assess_result: dict,
    recommend_result: dict,
    chosen_by: str = "policy",
    chosen_route: str = "",
    requested_route: str = "",
    fallback: dict | None = None,
) -> dict:
    """Persist why this task went to this route.

    Writes three things, cheapest first:
    - an event on the run's stream, type ``policy.decision`` (when the run
      exists in this workspace);
    - a sidecar note at ``<home>/runs/<run_id>/policy.json``;
    - one line in ``<home>/policy.jsonl`` even when there is no run yet.

    `chosen_by` is the answer to "who decided": ``policy`` when `--route auto`
    picked it, ``operator`` when a person named the route and the assessment
    only supplies context. `chosen_route` is what actually ran, which can
    differ from both the request and the recommendation when the strategy
    layer failed over.

    A broken database must not lose the ledger line, so the two writes are
    independent and the return value says what actually happened.
    """
    config.ensure_dirs()
    payload = {
        "version": SCHEMA_VERSION,
        "ts": time.time(),
        "run_id": run_id,
        "chosen_by": chosen_by or "policy",
        "chosen_route": chosen_route
        or (recommend_result or {}).get("route")
        or "",
        "requested_route": requested_route or chosen_route or "",
        "assess": assess_result,
        "recommend": recommend_result,
        # First-class copy of the scoring table: "why not codex?" is a query
        # against this, not a re-run of the recommendation.
        "scoring": (recommend_result or {}).get("scoring") or {},
    }
    if fallback:
        payload["fallback"] = fallback
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
