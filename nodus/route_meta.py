"""Declared route profiles: capability, price, requirements, prompt hints.

This is deliberately *declaration*, not measurement. policy may use these
fields to break a tie or to describe a route, but only bench/history evidence
can make a route "measured". Every route that is not listed here still gets a
profile inferred from its agent/model, so a new routes.json entry does not
disappear from the policy candidate set.

The old _ROUTE_CAPS / _ROUTE_FIT tables in policy.py remain for backward
compatibility and tests; this module is the source of the richer descriptions
and the dynamic fallback for custom routes.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Iterable


@dataclass(frozen=True)
class RouteMeta:
    name: str
    label: str
    strengths: tuple[str, ...]
    weaknesses: tuple[str, ...]
    kinds: dict[str, float]
    floor: int
    ceiling: int
    context: str
    tools: bool
    vision: bool
    cost_tier: int | None
    price: str
    value: str
    requirements: tuple[str, ...]
    prompt_hints: tuple[str, ...]
    source: str = "declared"
    efforts: tuple[str, ...] = ()
    alias_of: str | None = None
    promotion: dict | None = None

    @property
    def canonical(self) -> bool:
        return self.alias_of is None

    def caps(self) -> dict:
        return {
            "label": self.label,
            "cost_tier": self.cost_tier,
            "strengths": "；".join(self.strengths),
            "weaknesses": list(self.weaknesses),
            "context": self.context,
            "tools": self.tools,
            "vision": self.vision,
            "price": self.price,
            "value": self.value,
            "requirements": list(self.requirements),
            "prompt_hints": list(self.prompt_hints),
            "efforts": list(self.efforts),
            "alias_of": self.alias_of,
            "canonical": self.canonical,
            "promotion": dict(self.promotion) if self.promotion else None,
            "source": self.source,
        }

    def fit(self) -> dict:
        return {"kinds": dict(self.kinds), "floor": self.floor, "ceiling": self.ceiling}

    def public(self) -> dict:
        return {
            "route": self.name,
            "label": self.label,
            "strengths": list(self.strengths),
            "weaknesses": list(self.weaknesses),
            "floor": self.floor,
            "ceiling": self.ceiling,
            "context": self.context,
            "tools": self.tools,
            "vision": self.vision,
            "cost_tier": self.cost_tier,
            "price": self.price,
            "value": self.value,
            "requirements": list(self.requirements),
            "prompt_hints": list(self.prompt_hints),
            "efforts": list(self.efforts),
            "alias_of": self.alias_of,
            "canonical": self.canonical,
            "promotion": dict(self.promotion) if self.promotion else None,
            "source": self.source,
        }


def _meta(name: str, label: str, strengths: Iterable[str], weaknesses: Iterable[str],
          kinds: dict[str, float], floor: int, ceiling: int, context: str,
          tools: bool, vision: bool, cost_tier: int | None, price: str,
          value: str, requirements: Iterable[str] = (),
          prompt_hints: Iterable[str] = (), source: str = "declared",
          promotion: dict | None = None) -> RouteMeta:
    return RouteMeta(name, label, tuple(strengths), tuple(weaknesses), dict(kinds),
                     floor, ceiling, context, tools, vision, cost_tier, price,
                     value, tuple(requirements), tuple(prompt_hints), source, (),
                     None, promotion)


_BUILTIN = {
    "claude": _meta(
        "claude", "强代码/长上下文",
        ("跨文件改写", "长上下文推理", "设计权衡", "工具使用成熟"),
        ("价格中高", "小任务容易过度设计"),
        {"code": 1.0, "analysis": 1.0, "ops": 0.9, "writing": 0.7, "retrieval": 0.7},
        2, 5, "大", True, False, 3,
        "relay 按 token 计费；MiniMax 可作同协议兜底",
        "质量优先的代码/分析；难任务值得用",
        ("provider: relay（或 minimax 兜底）", "需要 read/edit/bash 工具"),
        ("复杂任务写清验收标准和测试命令", "简单任务不要用 claude 硬扛"),
    ),
    "minimax": _meta(
        "minimax", "claude 同协议低成本备用",
        ("便宜", "与 claude 同 CLI/协议，可兜底", "短任务响应快"),
        ("能力明显弱于 claude", "复杂设计/长上下文不可靠"),
        {"code": 0.8, "ops": 0.7, "analysis": 0.7, "writing": 0.6, "retrieval": 0.6},
        1, 3, "中等", True, False, 1,
        "站内余额计价，实测 ¥0.0289/run 量级",
        "便宜的小任务/兜底；不要用于难题",
        ("provider: minimax", "保留 claude 协议"),
        ("只用于低难度或兜底",),
    ),
    "codex": _meta(
        "codex", "单点修复/命令验证",
        ("代码修改", "命令验证", "快速迭代", "上下文窗口大"),
        ("写作弱", "跨模块设计一般"),
        {"code": 1.0, "ops": 0.9, "analysis": 0.6, "writing": 0.4, "retrieval": 0.6},
        1, 3, "大", True, False, 2,
        "relay-openai 按 token 计费；价格中低",
        "小中代码任务的性价比高",
        ("provider: relay-openai", "需要 shell/测试工具"),
        ("给出失败复现和验收命令",),
    ),
    "dsh": _meta(
        "dsh", "低成本中文写作",
        ("中文写作/整理", "检索归纳", "长文档笔记"),
        ("代码/运维能力弱", "不适合复杂工程改动"),
        {"writing": 1.0, "retrieval": 0.8, "analysis": 0.7, "code": 0.4, "ops": 0.3},
        1, 3, "大", True, False, 1,
        "DeepSeek 官方计价，通常最低档",
        "中文/写作的性价比最高；代码任务别用",
        ("provider: deepseek",),
        ("提供资料结构和目标读者",),
    ),
    "opencode-glm": _meta(
        "opencode-glm", "小任务快速执行",
        ("速度快", "严格格式执行", "小改动便宜"),
        ("复杂推理弱", "跨模块能力有限", "没有视觉声明"),
        {"code": 0.9, "ops": 0.8, "retrieval": 0.7, "analysis": 0.6, "writing": 0.6},
        1, 3, "中等", True, False, 1,
        "opencode 自带 provider 计费；A/B 里很便宜",
        "小任务/格式转换的性价比高",
        ("provider: relay-openai", "opencode 自己读 ~/.config/opencode/opencode.json"),
        ("把格式和边界写死",),
    ),
    "opencode-gemini": _meta(
        "opencode-gemini", "小任务 + 多模态",
        ("视觉输入", "小任务快", "严格格式"),
        ("复杂代码/长上下文弱",),
        {"code": 0.85, "ops": 0.7, "retrieval": 0.7, "analysis": 0.6, "writing": 0.6},
        1, 3, "中等", True, True, 1,
        "opencode 自带 provider 计费",
        "看图/小任务；不要用于架构决策",
        ("provider: relay-openai", "视觉任务优先"),
        ("附图片路径/截图，说明要看什么",),
    ),
    "opus": _meta(
        "opus", "旗舰架构/最强推理",
        ("复杂架构", "长上下文", "跨模块重构", "设计与权衡"),
        ("贵", "小任务浪费", "不应当默认用"),
        {"code": 1.0, "analysis": 1.0, "ops": 0.95, "writing": 0.8, "retrieval": 0.8},
        3, 5, "超大", True, False, 5,
        "relay opus 计价；明显高于 fable/sol",
        "能力上限高但性价比低；难题才用",
        ("provider: relay", "昂贵：低难度不要用", "effort 用 high/xhigh，max 仅特殊需要"),
        ("明确写出难点和验收标准", "不要让 opus 做机械改名"),
    ),
    "fable": _meta(
        "fable", "比 opus 高一级的替代（med/high 甜区）",
        ("复杂任务", "长上下文", "接近或超过 opus 的质量", "比 opus 便宜的档位"),
        ("xhigh 成本高", "默认/自动路由不要用它跑小任务", "需要 relay 支持"),
        {"code": 1.0, "analysis": 1.0, "ops": 0.95, "writing": 0.8, "retrieval": 0.8},
        3, 5, "超大", True, False, 3,
        "relay claude-fable-5-2；成本约 opus 的 80% 量级（按实际 token）",
        "高难任务的性价比选择；med/high 优先",
        ("provider: relay", "model: claude-fable-5-2", "xhigh 需要明确要求"),
        ("复杂任务给足上下文和验收", "默认用 high，不随便上 xhigh"),
    ),
    "sol": _meta(
        "sol", "长上下文规划/代码",
        ("长上下文", "路径规划", "代码实现", "成本低于 opus"),
        ("不属于视觉模型", "写作不如 dsh"),
        {"code": 1.0, "analysis": 0.95, "ops": 0.9, "writing": 0.7, "retrieval": 0.8},
        3, 5, "超大", True, False, 2,
        "relay-openai gpt-6.1-sol；价格中档",
        "高难代码/规划的高性价比选择",
        ("provider: relay-openai", "agent: codex"),
        ("复杂任务先让它做计划再改",),
    ),
    "terra": _meta(
        "terra", "codex 系通用/推理",
        ("推理", "代码", "规划", "成本低于 opus"),
        ("视觉弱", "写作一般"),
        {"code": 0.95, "analysis": 0.85, "ops": 0.85, "writing": 0.6, "retrieval": 0.7},
        2, 4, "大", True, False, 2,
        "relay-openai gpt-6.1-terra；中档",
        "中高难度的通用选择",
        ("provider: relay-openai", "agent: codex"),
        ("给出约束和验证方式",),
    ),
    "sonnet": _meta(
        "sonnet", "claude 中档",
        ("代码", "写作", "速度/价格平衡"),
        ("复杂架构不如 opus/fable",),
        {"code": 0.9, "analysis": 0.8, "ops": 0.8, "writing": 0.8, "retrieval": 0.7},
        2, 4, "大", True, False, 2,
        "relay claude-sonnet；中档",
        "日常代码/文档的平衡选择",
        ("provider: relay", "model: claude-sonnet-5-5"),
        ("明确改动范围和测试",),
    ),
    "argon": _meta(
        "argon", "gemini 视觉/多模态",
        ("视觉", "长上下文", "多模态"),
        ("工程改动不一定强",),
        {"code": 0.8, "analysis": 0.85, "ops": 0.7, "writing": 0.7, "retrieval": 0.8},
        2, 4, "超大", True, True, 2,
        "relay-openai gemini-4-argon；中档",
        "看图/多模态任务的候选",
        ("provider: relay-openai", "视觉任务优先"),
        ("附图片并说明要看什么",),
    ),
    "carbon": _meta(
        "carbon", "Gemini 4 Carbon 高智能多模态",
        ("高智能", "长上下文", "文本/图像/音频/视频输入", "强推理"),
        ("贵", "慢", "输出冗长", "普通代码任务不划算"),
        {"code": 0.9, "analysis": 1.0, "ops": 0.8, "writing": 0.85, "retrieval": 0.9},
        3, 5, "1M", True, True, 5,
        "$2/M 输入、$10/M 输出（到 2026-11-30）；12/1 起 $4/$20；缓存折扣 95%（官方价）",
        "高难多模态/长上下文；价格高，别用于日常小任务",
        ("provider: relay-openai", "model: codes9527/gemini-4-carbon", "opencode 模型表需登记"),
        ("把图片/音视频路径附上，说明要看什么", "不要让它做机械改名"),
        promotion={
            "until": "2026-12-01",
            "cost_tier": 2,
            "prefer_over": ["argon"],
            "note": "首发价 $2/M 输入、$10/M 输出；12 月 1 日前优先于 argon",
        },
    ),
}

# Canonical model-level routes expose effort as a separate choice. Effort
# variants are not independent models; they are aliases excluded from auto
# allocation and kept only for explicit --route dispatch.
_EFFORTS = {
    "opus": ("high", "xhigh"),
    "fable": ("medium", "high"),
    "sonnet": ("high",),
    "sol": ("high", "xhigh"),
    "terra": ("high",),
}
for _name, _efforts in _EFFORTS.items():
    _BUILTIN[_name] = replace(_BUILTIN[_name], efforts=_efforts)

# `claude` is the Claude Code CLI entry point, not a model. In this account its
# default model is opus, with a MiniMax fallback; treat it as an explicit
# compatibility alias, never as a separate model candidate.
_BUILTIN["claude"] = replace(
    _BUILTIN["claude"],
    label="Claude CLI 默认入口（当前 opus）+ minimax 兜底",
    alias_of="opus",
    source="alias of opus (CLI default model)",
)

# Alias routes added by the operator; exact entries keep their effort in the
# name but share the underlying model's capability profile.
_ALIAS = {
    "fable-med": "fable",
    "fable-high": "fable",
    "opus-high": "opus",
    "opus-xhigh": "opus",
    "opus-max": "opus",
}

_FALLBACK = {
    "claude": "claude",
    "codex": "codex",
    "dsh": "dsh",
    "opencode-glm": "opencode-glm",
    "opencode-gemini": "opencode-gemini",
}


def _generic(name: str, agent: str, model: str | None) -> RouteMeta:
    kinds = {"code": 0.7, "analysis": 0.6, "writing": 0.6, "retrieval": 0.6, "ops": 0.6}
    floor, ceiling, tier = 1, 4, None
    label = f"{agent} 自定义路线"
    strengths = (f"由 {agent} 执行",)
    weaknesses = ("没有声明能力/价格元数据",)
    price = "未声明价格"
    value = "未知"
    if agent == "claude":
        floor, ceiling, tier = 2, 5, 3; label = "claude 系自定义路线"
    elif agent == "codex":
        floor, ceiling, tier = 2, 4, 2; label = "codex 系自定义路线"
    elif agent == "dsh":
        floor, ceiling, tier = 1, 3, 1; label = "deepseek 系自定义路线"
    elif agent.startswith("opencode"):
        floor, ceiling, tier = 1, 3, 1; label = "opencode 系自定义路线"
    if model:
        strengths = strengths + (f"model: {model}",)
    return RouteMeta(name, label, strengths, weaknesses, kinds, floor, ceiling,
                     "未知", True, False, tier, price, value,
                     ("未声明 provider/代理要求",), ("未声明临时提示词要求",),
                     "inferred")


def active_promotion(name: str, route=None, today: date | None = None) -> dict | None:
    """The route's promotion if today is before its exclusive end date."""
    promotion = meta_for(name, route).promotion
    if not promotion:
        return None
    try:
        until = date.fromisoformat(str(promotion["until"]))
    except (KeyError, ValueError):
        return None
    now = today or date.today()
    return dict(promotion) if now < until else None


def meta_for(name: str, route=None) -> RouteMeta:
    """Exact known metadata, alias metadata, or an inferred profile."""
    if name in _BUILTIN:
        return _BUILTIN[name]
    if name in _ALIAS:
        base = _BUILTIN[_ALIAS[name]]
        return replace(base, name=name, label=f"{base.label}（{name}）",
                       source="declared alias", efforts=(), alias_of=base.name)
    import re as _re
    m = _re.match(r"^(opus|fable|sonnet|sol|terra)-(low|medium|med|high|xhigh|max)$", name)
    if m and m.group(1) in _BUILTIN:
        base = _BUILTIN[m.group(1)]
        return replace(
            base, name=name, label=f"{base.label}（{m.group(2)}）",
            source="effort alias", efforts=(), alias_of=base.name,
            requirements=base.requirements + (f"effort: {m.group(2)}",),
        )
    agent = str(getattr(route, "agent", "") or "")
    model = str(getattr(route, "model", "") or "")
    low = model.lower()
    for needle, key in (("fable", "fable"), ("opus", "opus"), ("sol", "sol"),
                        ("terra", "terra"), ("sonnet", "sonnet"),
                        ("gemini", "opencode-gemini"), ("glm", "opencode-glm")):
        if needle in low:
            base = _BUILTIN[key]
            return replace(base, name=name, label=f"{base.label}（{name}）",
                           source="inferred from model")
    return _generic(name, agent or "unknown", model or None)


def caps(name: str, route=None) -> dict:
    return meta_for(name, route).caps()


def fit(name: str, route=None) -> dict:
    return meta_for(name, route).fit()


def public(name: str, route=None) -> dict:
    return meta_for(name, route).public()


def criterion(name: str, route=None) -> str:
    """One line Jev can compare routes on: capability + price + requirements."""
    m = meta_for(name, route)
    cost = f"成本档 {m.cost_tier}/5" if m.cost_tier else "成本未知"
    req = "；".join(m.requirements) if m.requirements else "无额外要求"
    hints = "；".join(m.prompt_hints) if m.prompt_hints else "无"
    efforts = "/".join(m.efforts) if m.efforts else "无（固定）"
    alias = f"；别名 of {m.alias_of}" if m.alias_of else ""
    return (
        f"{m.label}{alias}；{cost}；可选 effort：{efforts}；"
        f"优点：{'/'.join(m.strengths)}；缺点：{'/'.join(m.weaknesses)}；"
        f"{req}；提示词要求：{hints}"
    )


def known_names() -> tuple[str, ...]:
    return tuple(_BUILTIN) + tuple(_ALIAS)
