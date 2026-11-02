"""The one classification of a failed run: cause, class, kind.

Before this module the same error string was classified four times: `recover`
matched ten causes and two retry classes, the board's `_failure_kind` matched
five display classes, and its `_CAUSE_RULES` matched fourteen labels. They were
each defensible and they disagreed: `502 Bad Gateway` was `upstream` to
`recover`, `无法归类` on the board, and had no label; `exit code 1` was
`unknown`/retryable, `进程退出`, and `进程自己退出`. Adding one error pattern
meant editing four tables and the one you forgot became a row that said X in
one block and Y in another.

The vocabulary is now one table, and everything else is a projection of it:

``cause``
    the machine reason. Thirteen names, matched in a fixed order by the one
    regex table below. This is what retry decisions are made from.
``class``
    ``transient`` (the machine may try again), ``deterministic`` (the request
    must change first), ``human`` (a person has to look at the code or the
    record), or ``none`` (not a failure at all).
``kind`` / ``label``
    one sentence per cause, derived from the cause and never matched
    separately. `kind` is the machine's name for it; `label` is the same
    sentence exposed for the UI, so the two cannot drift.

Why not `lifecycle`? That module is the state machine: transitions and the
"does a person have to act" question, over facts the caller already has. It
has no error vocabulary and no business importing thirteen regexes; putting
them there would make every state question pay for failure text matching. This
module is one layer above the facts and below the display code, with no
database, network or clock: callers pass the run and what else they know
(``verify``, ``operations``, ``text``) and get a typed answer.

The match order is the old `recover._cause` order, unchanged on purpose:
deterministic reasons come before network ones, so a 401 wrapped in a
connection reset is still a rejected key. `exit-code` is the only new text
class; it is checked after the specific causes and before `unknown`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

#: Every machine reason, in the order the report groups them (not match order).
CAUSES: tuple[str, ...] = (
    "orphaned",
    "timeout",
    "network",
    "5xx",
    "rate-limit",
    "auth",
    "bad-request",
    "model-missing",
    "content-policy",
    "tests-red",
    "no-change",
    "exit-code",
    "unknown",
)

#: Every retry class `recover` needs, plus `none` for a run that did not fail.
CLASSES: tuple[str, ...] = ("transient", "deterministic", "human", "none")

#: The display sentence for a cause. `label` is the UI-facing name for the
#: same value; both functions read this dict, so they cannot disagree.
KINDS: dict[str, str] = {
    "orphaned": "进程被外部杀掉",
    "timeout": "卡住超时",
    "network": "网络断了",
    "5xx": "上游服务出错",
    "rate-limit": "被上游限流或额度用尽",
    "auth": "密钥无效或没有权限",
    "bad-request": "请求被拒绝",
    "model-missing": "模型名不对",
    "content-policy": "内容被安全策略拦截",
    "tests-red": "测试红",
    "no-change": "证据缺失",
    "exit-code": "进程自己退出",
    "unknown": "无法归类",
}

#: Retrying cannot fix these: the request, the key or the content has to change.
DETERMINISTIC: frozenset[str] = frozenset(
    {"auth", "bad-request", "model-missing", "content-policy"}
)

#: The reasons that need a person to change code or to look at the record.
HUMAN: frozenset[str] = frozenset({"tests-red", "no-change"})

#: The sentence a person reads instead of a stack trace, per cause.
PROBLEM: dict[str, str] = {
    "orphaned": "执行到一半，启动它的进程退出了（不是模型出错）。",
    "timeout": "跑太久没有响应，被当成卡死中止了。",
    "network": "网络断了（连接被重置或超时），这次执行没走完。",
    "5xx": "上游服务出错（5xx），不是任务本身的问题。",
    "rate-limit": "上游限流了，等一会儿可以再试。",
    "auth": "密钥被上游拒绝（401/403），重试不会好，需要换密钥或补权限。",
    "bad-request": "请求参数被拒绝（400），重试不会好，需要改配置或这次指令。",
    "model-missing": "模型名不存在或没有权限，重试不会好，需要换模型。",
    "content-policy": "内容被上游安全策略拦了，重试不会好。",
    "tests-red": "跑完了，但测试没过；重试不会改代码。",
    "no-change": "这次运行没有留下 tool_call、改动或测试输出，看不出它做了什么。",
    "exit-code": "执行失败，报错里只有退出码，没有能归因的文案。",
    "unknown": "执行失败，报错里没有能归因的模式。",
}

#: What to do about it, in the operator's terms.
SUGGESTION: dict[str, str] = {
    "orphaned": "不用你管：系统会从断点重新派一轮，有会话就接着会话。",
    "timeout": "不用你管：退避后会自动再试，连续失败才会来找你。",
    "network": "不用你管：退避后会自动再试，连续失败才会来找你。",
    "5xx": "不用你管：退避后会自动再试，连续失败才会来找你。",
    "rate-limit": "不用你管：退避后会自动再试，连续失败才会来找你。",
    "auth": "换好密钥或补权限后点「重跑」，任务本身不用改。",
    "bad-request": "检查这次指令/模型/参数，改完再重跑。",
    "model-missing": "换一个可用模型再重跑。",
    "content-policy": "换个说法或换个模型再派。",
    "tests-red": "先修失败的那条测试，改完再重跑。",
    "no-change": "先看日志确认它做了什么，再决定重跑还是自己接手。",
    "exit-code": "先看最后一次动作和报错尾部，再决定重跑还是自己接手。",
    "unknown": "先看最后一次动作和报错尾部，再决定重跑还是自己接手。",
}

#: Why a retry cannot help, in the operator's terms. Empty for a transient cause.
WHY_NOT_RETRIED: dict[str, str] = {
    "auth": "上游判定这次请求无权（401/403）。密钥和权限不变，再发一次还是同一个结果。",
    "bad-request": "请求本身不合法（400）。参数不改，重发只会得到同一个 400。",
    "model-missing": "模型名不存在或这个密钥没有该模型权限。换模型之前，重试没有意义。",
    "content-policy": "输入被上游安全策略拦下。同样的内容再发一次还是被拦。",
}

#: Failure signals, checked in this order. Same order as `recover` used before
#: this module existed; a 401 wrapped in a reset must stay `auth`.
_ORPHANED = re.compile(r"orphaned", re.I)
_AUTH = re.compile(
    r"\b(?:401|403)\b|unauthoriz|forbidden|invalid[\s_-]*api[\s_-]*key|"
    r"authenticat|authoriz|api[\s_-]*key|密钥|鉴权|未授权|认证失败|授权失败",
    re.I,
)
_BAD_REQUEST = re.compile(
    r"\b400\b|bad request|invalid[\s_-]?(?:request|argument|parameter|param|input)|"
    r"unrecognized|unknown (?:argument|parameter)|validation error|missing required|"
    r"参数错误|参数不合法|参数无效|参数缺失",
    re.I,
)
_CONTENT_POLICY = re.compile(
    r"content[\s_-]?policy|safety|content[\s_-]?filter|blocked by|违规|"
    r"内容[^\n]{0,10}(?:安全|审核|被拦|拦截)",
    re.I,
)
_MODEL_MISSING = re.compile(
    r"model[\s_-]?not[\s_-]?found|model_not_found|no such model|unknown model|"
    r"invalid model|model[^.\n]{0,40}(?:does not exist|not available|not found)|"
    r"模型[^\n。]{0,12}(?:不存在|下线|不可用|名不对|弃用)",
    re.I,
)
_TIMEOUT = re.compile(r"timed?[\s_-]?out|\btimeout\b|no output for|etimedout|超时", re.I)
_RATE_LIMIT = re.compile(
    r"rate[\s_-]?limit|\b429\b|too many requests|overloaded|slow down|"
    r"限流|频率限制|请求过多|太频繁",
    re.I,
)
_NETWORK = re.compile(
    r"econnreset|econnrefused|econnaborted|etimedout|\benotfound\b|eai_again|epipe|"
    r"ehostunreach|enetunreach|connection (?:reset|refused|closed|aborted|error)|"
    r"reset by peer|broken pipe|fetch failed|network|socket|dns|tls|ssl|"
    r"网络|连接(?:断|被重置|失败|超时)|断线|掉线",
    re.I,
)
_5XX = re.compile(
    r"\b5\d\d\b|internal server error|bad gateway|service unavailable|gateway timeout|"
    r"upstream|上游|服务(?:器)?(?:错误|不可用|异常)",
    re.I,
)
_EXIT_CODE = re.compile(r"\bexit code -?\d+", re.I)

_MATCHERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("orphaned", _ORPHANED),
    ("auth", _AUTH),
    ("bad-request", _BAD_REQUEST),
    ("content-policy", _CONTENT_POLICY),
    ("model-missing", _MODEL_MISSING),
    ("timeout", _TIMEOUT),
    ("rate-limit", _RATE_LIMIT),
    ("network", _NETWORK),
    ("5xx", _5XX),
    ("exit-code", _EXIT_CODE),
)


@dataclass(frozen=True)
class Diagnosis:
    """One run's failure answer, typed.

    ``cause`` is ``""`` and ``failure_class`` is ``"none"`` for a run that has
    not failed. ``matched`` is the substring the regex matched, so a display
    layer can quote the line it used instead of inventing one; it is empty for
    a cause inferred from facts rather than text.
    """

    cause: str
    failure_class: str
    kind: str
    label: str
    matched: str = ""


def _none() -> Diagnosis:
    return Diagnosis(cause="", failure_class="none", kind="", label="", matched="")


def _answer(cause: str, matched: str = "") -> Diagnosis:
    return Diagnosis(
        cause=cause,
        failure_class=class_of(cause),
        kind=kind(cause),
        label=label(cause),
        matched=matched,
    )


def class_of(cause: str) -> str:
    """The retry class of a cause. Unknown causes are treated as transient.

    `unknown` has to be judged by the other four retry questions rather than
    hidden, so it is `transient`: the old `recover` did the same.
    """
    if cause in DETERMINISTIC:
        return "deterministic"
    if cause in HUMAN:
        return "human"
    if cause == "":
        return "none"
    return "transient"


def kind(cause: str) -> str:
    """The one sentence for `cause`; `无法归类` for anything unrecognised."""
    return KINDS.get(str(cause or ""), KINDS["unknown"])


def label(cause: str) -> str:
    """The UI's name for `cause`. Same table as `kind`, deliberately."""
    return kind(cause)


def problem(cause: str) -> str:
    """The one-sentence explanation for `cause`."""
    return PROBLEM.get(str(cause or ""), PROBLEM["unknown"])


def suggestion(cause: str) -> str:
    """What to do about `cause`."""
    return SUGGESTION.get(str(cause or ""), SUGGESTION["unknown"])


def why_not_retried(cause: str) -> str:
    """Why a deterministic cause must change before it can run again."""
    return WHY_NOT_RETRIED.get(str(cause or ""), "")


def match(text: Any) -> tuple[str, str]:
    """(cause, matched substring) for an error string, or ("unknown", "")."""
    value = str(text or "")
    if not value.strip():
        return "unknown", ""
    for cause, pattern in _MATCHERS:
        found = pattern.search(value)
        if found:
            return cause, found.group(0)
    return "unknown", ""


def diagnose(run: Mapping[str, Any] | None, facts: Mapping[str, Any] | None = None) -> Diagnosis:
    """Classify one run from its error/status and the facts the caller has.

    ``facts`` is what the caller already knows; nothing here is looked up:

    ==============  =====================================================
    ``verify``      ``"red"`` / ``"ok"`` / ``"none"`` from `db.verify_states`.
                    A red test is `tests-red` before any error matching: the
                    run exited 0 and the acceptance failed.
    ``text``        the error text to match. Defaults to ``run["error"]``;
                    the board passes the evidence tail + action so a timeout
                    recorded in a tool result is still seen.
    ``operations``  ``False`` when the run recorded no tool_call, change or
                    test. With no error text either, the cause is `no-change`
                    (the record cannot answer, which is itself an answer).
    ==============  =====================================================

    A queued/running run, or a completed run whose tests are not red, is not a
    failure: ``cause`` is ``""`` and ``failure_class`` is ``"none"``. A mapping
    with an error and no status is still classified -- callers hand `recover`
    synthetic rows in tests.
    """
    run = run or {}
    facts = facts or {}
    status = str(run.get("status") or "")
    verify = str(facts.get("verify") or "none")

    if verify == "red":
        return _answer("tests-red")

    if status in ("queued", "running") or (status == "completed" and verify != "red"):
        return _none()

    text = facts.get("text")
    if text is None:
        text = run.get("error")
    cause, found = match(text)
    if cause == "unknown" and not str(text or "").strip():
        if facts.get("operations") is False:
            return _answer("no-change")
    return _answer(cause, found)


def cause(run: Mapping[str, Any] | None, facts: Mapping[str, Any] | None = None) -> str:
    """The machine reason for a run; ``""`` when it has not failed."""
    return diagnose(run, facts).cause


def classify(run: Mapping[str, Any] | None, facts: Mapping[str, Any] | None = None) -> str:
    """The retry class for a run: transient / deterministic / human / none."""
    return diagnose(run, facts).failure_class
