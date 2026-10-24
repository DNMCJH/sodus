"""Memory a human can read: summarise the files each harness already keeps.

Every harness Nodus drives already writes its own instructions to a plain text
file — Claude Code reads `CLAUDE.md`, Codex and DSH read `AGENTS.md` — and the
agent appends to those files while it works. The problem is not storage, it is
*summary*: one fact gets written in three places, sometimes twice with opposite
meanings, and nobody has reviewed the result.

So there is no second store here. `harvest` reads those files, `conflicts` finds
the candidates that disagree, and `adopt` appends the reviewed ones to the
project's `AGENTS.md` under `## 已验证事实`. Claude Code picks that file up
through a single `@AGENTS.md` line in its own `CLAUDE.md`, which is why one file
can serve three harnesses.

Extraction is rule-based on purpose. A model call here would invent facts nobody
wrote and could not be diffed; a rule that is too stupid misses facts, which is
visible, while a model that is too clever fabricates them, which is not. The
rules are: markdown list items, `##` headings, and sentences that carry an
imperative cue. That is crude, and `conflicts()` below is cruder still.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

from . import config

#: Where adopted facts live in the project's AGENTS.md. One heading, matched
#: exactly, because a human is expected to edit this file by hand.
SECTION = "## 已验证事实"

#: Same fact, said twice. Above this the two candidates are merged and the
#: second source is remembered instead of being shown as a duplicate row.
NEAR_DUPLICATE = 0.85

#: A brief, a board row, an AGENTS.md line: none of them wants a paragraph.
MAX_TEXT = 240

#: Sub-directories that never carry a project's instructions, only debris.
SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build",
    ".pytest_cache", "target", "vendor", ".next", ".cache",
}

#: Tools that are alternatives to each other. Two candidates that pick different
#: members of one family are a contradiction ("用 pnpm" vs "用 npm") even though
#: the topic word never appears verbatim in both. Crude, and wrong whenever the
#: two lines are talking about different jobs.
FAMILIES: dict[str, tuple[str, ...]] = {
    "包管理器": ("npm", "pnpm", "yarn", "bun", "cnpm"),
    "Python 环境": ("pip", "pip3", "conda", "uv", "poetry", "virtualenv"),
    "测试命令": ("pytest", "unittest", "tox", "nox", "jest", "vitest", "mocha", "nose"),
    "代码检查": ("ruff", "flake8", "pylint", "eslint", "prettier", "black", "biome"),
    "版本控制": ("git", "svn", "jj", "hg"),
    "搜索命令": ("rg", "grep", "ag", "ack", "findstr"),
    "系统包管理": ("apt", "apt-get", "brew", "choco", "winget", "scoop"),
}

#: Other tool names worth a topic of their own. A bare word here is enough to
#: make two lines "about the same thing"; it is not enough to make them clash.
_TOOLS = tuple(
    sorted(
        {member for members in FAMILIES.values() for member in members}
        | {
            "python", "python3", "node", "docker", "curl", "make", "cargo",
            "dotnet", "java", "pwsh", "powershell", "bash", "npm", "pytest",
        }
    )
)

_CUE_WORDS = re.compile(
    r"\b(never|don't|dont|avoid|must|always|prefer|use|run|only)\b", re.I
)
_CUE_CJK = (
    "不要", "别", "必须", "禁止", "务必", "记得", "避免", "统一", "先",
    "用", "安装", "放在", "位于", "默认", "只",
)
_INSTRUCTION = re.compile(
    r"(用|使用|统一|换成|改成|跑|执行|安装|install|use|run|switch|prefer|manage)",
    re.I,
)

#: "don't" in one place, "do it" in the other. 别 stays a regex because a bare
#: substring would also match 特别 / 区别 / 类别, which are not negations.
_NEGATION_RE = re.compile(
    r"不要|禁止|禁用|勿|不许|避免|不能|不用|(?<![特区分类个性])别"
    r"|\b(?:never|don'?t|avoid|not\s+use|stop\s+using|do\s+not)\b",
    re.I,
)

#: Words that carry no subject. Without this list two unrelated lines look
#: related because both say "use" or "不要", and every pair becomes a conflict.
_STOP_WORDS = {
    "the", "and", "with", "that", "this", "for", "not", "use", "using", "used",
    "run", "runs", "only", "all", "are", "was", "has", "have", "from", "into",
    "before", "after", "when", "then", "than", "you", "your", "its", "our",
    "their", "each", "every", "same", "must", "should", "will", "can", "may",
}
_STOP_BIGRAMS = {
    "不要", "可以", "如果", "需要", "以及", "一个", "这个", "没有", "不是",
    "就是", "或者", "因为", "所以", "已经", "可能", "应该", "并且", "但是",
}

_BULLET = re.compile(r"^(?:[-*+]|\d+[.)])\s+(.+)$")
_HEADING = re.compile(r"^(#{2,4})\s+(.+?)\s*#*$")
_CHECKBOX = re.compile(r"^\[[ xX]\]\s*")
_PLACEHOLDER = re.compile(r"<[A-Za-z][^<>\n]{0,40}>")
_TABLE_ROW = re.compile(r"^\|")
_PUNCT = re.compile(
    r"[\s`*_~#>\[\](){}（）「」【】“”\"'‘’·、，。；：！？,.;:!?/\\|—–-]+"
)
_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\[^\s，。；、）)】\"]+")
_POSIX_PATH = re.compile(r"(?<![\w/])~?/[\w.-]+(?:/[\w.~{}-]+)+")
#: One alternation instead of ~55 separate searches per candidate: a board load
#: harvests a few hundred candidates and the per-word regexes dominated it.
_TOOL_RE = re.compile(
    r"(?<![a-z0-9_-])("
    + "|".join(sorted((re.escape(word) for word in _TOOLS), key=len, reverse=True))
    + r")(?![a-z0-9_-])"
)
_FAMILY_OF = {
    member: family for family, members in FAMILIES.items() for member in members
}
#: `- 2026-10-24（来源 AGENTS.md）构建用 pnpm`, the line `adopt` writes.
_FACT_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}\s*（来源[^）]*）\s*(.+)$")
#: `_harness` is asked twice per candidate and resolves the path each time.
_HARNESS_CACHE: dict[str, str] = {}


# ---- where the sources are ---------------------------------------------


def _user_home() -> Path:
    """The home directory holding the global harness files. Patched by tests."""
    return Path.home()


def _project(project_dir: str | os.PathLike[str] | None = None) -> Path:
    """Which project the caller means: the argument, `$NODUS_PROJECT`, or cwd.

    cwd is the default on purpose. `nodus debug memory harvest` must look at the
    directory you are standing in, not at whatever was reviewed last time.
    """
    if project_dir:
        return Path(project_dir).expanduser().resolve()
    env = os.environ.get("NODUS_PROJECT")
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd()


def saved_project() -> Path:
    """Which project the board should scan.

    The daemon is started inside the state directory, so its cwd cannot answer
    "which project is the operator reviewing". The last `debug memory` command
    records one in config.json, and `$NODUS_PROJECT` overrides that.
    """
    env = os.environ.get("NODUS_PROJECT")
    if env:
        return Path(env).expanduser().resolve()
    saved = config.load_config().get("project_dir")
    if saved:
        return Path(str(saved)).expanduser().resolve()
    return Path.cwd()


def _harness(path: Path) -> str:
    """Which harness owns this file. Guesswork, and it is labelled as such."""
    key = str(path)
    if key in _HARNESS_CACHE:
        return _HARNESS_CACHE[key]
    parts = [part.lower() for part in path.parts]
    if path.name.lower() == "claude.md" or ".claude" in parts:
        chosen = "claude"
    elif ".codex" in parts:
        chosen = "codex"
    else:
        chosen = "other"
        dsh = os.environ.get("DSH_HOME")
        if dsh:
            try:
                path.resolve().relative_to(Path(dsh).expanduser().resolve())
                chosen = "dsh"
            except (ValueError, OSError):
                pass
    _HARNESS_CACHE[key] = chosen
    return chosen


def _project_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for name in ("AGENTS.md", "CLAUDE.md"):
        candidate = root / name
        if candidate.is_file():
            out.append(candidate)
    try:
        children = sorted(root.iterdir())
    except OSError:
        return out
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name in SKIP_DIRS:
            continue
        candidate = child / "AGENTS.md"
        if candidate.is_file():
            out.append(candidate)
    return out


def _global_files() -> list[Path]:
    home = _user_home()
    out = [home / ".claude" / "CLAUDE.md", home / ".codex" / "AGENTS.md"]
    dsh = os.environ.get("DSH_HOME")
    if dsh:
        out.append(Path(dsh).expanduser() / "AGENTS.md")
    out.append(home / ".nodus" / "AGENTS.md")
    return [path for path in out if path.is_file()]


def _files(project_dir: str | os.PathLike[str] | None = None) -> list[tuple[Path, str]]:
    root = _project(project_dir)
    out = [(path, "project") for path in _project_files(root)]
    out += [(path, "global") for path in _global_files()]
    return out


def project_root(project_dir: str | os.PathLike[str] | None = None) -> str:
    """The project these files were read from, for a CLI or MCP reply."""
    return str(_project(project_dir))


def sources(project_dir: str | os.PathLike[str] | None = None) -> list[dict]:
    """Every memory file that exists, with its scope, owner and size.

    Missing files are not reported: there is nothing to review about a file
    that was never written.
    """
    out: list[dict] = []
    for path, scope in _files(project_dir):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = sum(1 for _ in handle)
        except OSError:
            continue
        out.append(
            {
                "path": str(path),
                "scope": scope,
                "harness": _harness(path),
                "lines": lines,
            }
        )
    return out


# ---- extraction --------------------------------------------------------


def _clean(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text.strip())
    text = text.replace("`", "")
    text = re.sub(r"^[-*+]\s+", "", text)
    return " ".join(text.split())


def _usable(text: str, minimum: int, kind: str = "bullet") -> bool:
    if len(text) < minimum:
        return False
    if _CHECKBOX.match(text) or _TABLE_ROW.match(text):
        return False
    if _PLACEHOLDER.search(text):
        return False
    # A bare path or project name under a "Paths:" heading is a list entry,
    # not a fact: "scripts/clean_merge.py" tells nobody what to do.
    if kind == "bullet" and " " not in text and len(text) < 40:
        if not any("\u4e00" <= char <= "\u9fff" for char in text):
            return False
    return True


def _sentences(line: str) -> list[str]:
    return [part for part in re.split(r"(?<=[。；;！!？?])\s*", line) if part.strip()]


def _imperative(text: str) -> bool:
    return bool(_CUE_WORDS.search(text)) or any(cue in text for cue in _CUE_CJK)


def _candidate(text: str, kind: str, path: Path, scope: str) -> dict:
    text = text[:MAX_TEXT]
    digest = _hash(_norm(text))
    return {
        "id": f"mem-{digest}",
        "text": text,
        "hash": digest,
        "kind": kind,
        "scope": scope,
        "harness": _harness(path),
        "source_path": str(path),
        "sources": [{"path": str(path), "scope": scope, "harness": _harness(path)}],
        "conflict": False,
        "conflict_with": [],
        "adopted": False,
    }


def _extract(text: str, path: Path, scope: str) -> list[dict]:
    """Read a memory file line by line. No fence tracking on purpose.

    A real AGENTS.md on this machine opens with ```markdown and keeps 170 lines
    of actual memory inside it — someone pasted the file through a chat client.
    Skipping fenced blocks would then skip the file. What a fence usually
    contains (a review template, a command example) is a checkbox row or a
    `<placeholder>`, and `_usable` already drops those.
    """
    out: list[dict] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        heading = _HEADING.match(stripped)
        if heading:
            # The section `adopt` writes is bookkeeping, not a fact to review.
            if stripped == SECTION:
                continue
            body = _clean(heading.group(2))
            if _usable(body, 2, "heading"):
                out.append(_candidate(body, "heading", path, scope))
            continue
        bullet = _BULLET.match(stripped)
        if bullet:
            body = _clean(bullet.group(1))
            # A line `adopt` wrote carries its own date and source. Read the
            # fact out of the bookkeeping, or the line comes back as a brand
            # new candidate the moment it is written.
            written = _FACT_PREFIX.match(body)
            if written:
                body = written.group(1).strip()
            if _usable(body, 4, "bullet"):
                out.append(_candidate(body, "bullet", path, scope))
            continue
        for sentence in _sentences(stripped):
            body = _clean(sentence)
            if _usable(body, 6, "sentence") and _imperative(body):
                out.append(_candidate(body, "sentence", path, scope))
    return out


# ---- dedupe ------------------------------------------------------------


def _norm(text: str) -> str:
    """Punctuation, case and spacing are noise: 「构建用 pnpm。」and
    "构建用 PNPM" are the same fact for the purpose of a hash."""
    return _PUNCT.sub("", text.lower())


def _hash(normalized: str) -> str:
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]


def _tokens(text: str) -> list[str]:
    """Cheap buckets for the near-duplicate scan: latin words first (they
    discriminate), then CJK bigrams (they at least narrow the field)."""
    latin = re.findall(r"[a-z0-9]{3,}", text.lower())
    cjk = [
        text[i : i + 2]
        for i in range(len(text) - 1)
        if "\u4e00" <= text[i] <= "\u9fff" and "\u4e00" <= text[i + 1] <= "\u9fff"
    ]
    seen: list[str] = []
    for token in [*latin, *cjk]:
        if token not in seen:
            seen.append(token)
    return seen


def _similar(left: str, right: str) -> float:
    """Similarity, with the cheap upper bounds checked before the O(n*m) pass."""
    if not left or not right:
        return 0.0
    if abs(len(left) - len(right)) > max(16, 0.35 * max(len(left), len(right))):
        return 0.0
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    if matcher.real_quick_ratio() <= NEAR_DUPLICATE:
        return 0.0
    if matcher.quick_ratio() <= NEAR_DUPLICATE:
        return 0.0
    return matcher.ratio()


def _add_source(target: dict, other: dict) -> None:
    known = {(item["path"], item["scope"]) for item in target["sources"]}
    for item in other["sources"]:
        if (item["path"], item["scope"]) not in known:
            target["sources"].append(dict(item))


def _merge(raw: list[dict]) -> list[dict]:
    """Exact duplicates first (a hash lookup), then near duplicates (a scan
    restricted to candidates sharing a token, capped so a 300-line file cannot
    turn the board into an O(n^2) job)."""
    exact: dict[str, dict] = {}
    order: list[str] = []
    for cand in raw:
        if cand["hash"] in exact:
            _add_source(exact[cand["hash"]], cand)
            continue
        exact[cand["hash"]] = cand
        order.append(cand["hash"])

    # Norm, tokens and family members are computed once per candidate: the
    # pair-wise scan calls them thousands of times, and the regexes behind them
    # cost more than the string comparison they guard.
    cache: dict[int, tuple[str, list[str], frozenset, bool]] = {}

    def facts(cand: dict) -> tuple[str, list[str], frozenset, bool]:
        key = id(cand)
        if key not in cache:
            text = cand["text"]
            cache[key] = (
                _norm(text),
                _tokens(text),
                frozenset(_family_members(text)),
                _has_negation(text.lower()),
            )
        return cache[key]

    index: dict[str, list[int]] = {}
    out: list[dict] = []
    for cand in (exact[digest] for digest in order):
        match = _neighbour(facts(cand), facts, index, out)
        if match is not None:
            _add_source(match, cand)
            continue
        for token in set(facts(cand)[1]):
            index.setdefault(token, []).append(len(out))
        out.append(cand)
    return out


def _neighbour(facts, lookup, index: dict[str, list[int]], kept: list[dict]) -> dict | None:
    left, tokens, families, negated = facts
    positions: list[int] = []
    for token in tokens:
        for pos in index.get(token, ()):
            if pos not in positions:
                positions.append(pos)
        if len(positions) >= 24:
            break
    for pos in positions[:24]:
        other = kept[pos]
        other_norm, _, other_families, other_negated = lookup(other)
        # "构建用 pnpm" and "构建用 npm" are 92% the same string and mean the
        # opposite thing. Near-duplicate merging must never swallow a conflicting
        # choice: the whole point of the review is to see both.
        if families != other_families:
            continue
        # "用 npm 装依赖" and "不要用 npm 装依赖" are one edit apart and mean the
        # opposite thing; merging them would hide the disagreement entirely.
        if negated != other_negated:
            continue
        # Two 240-character reflections are never worth a 3ms diff: the merge
        # exists for one-line rules written twice, not for prose.
        if max(len(left), len(other_norm)) > 160:
            continue
        if _similar(left, other_norm) > NEAR_DUPLICATE:
            return other
    return None


# ---- conflicts ---------------------------------------------------------


def _has_negation(window: str) -> bool:
    return bool(_NEGATION_RE.search(window))


def _topic_words(topic: str) -> list[str]:
    kind, _, value = topic.partition(":")
    if kind == "家族":
        return list(FAMILIES.get(value, ()))
    return [value]


def _negated_near(text: str, topic: str) -> bool:
    """Does this candidate say *don't* about the topic, rather than about
    something else in the same sentence?

    "这里没有挂载任何外部目录，grep 默认只扫这个工作区…别误判" contains both a
    tool name and a negation, but the negation is about the reader's conclusion,
    not about using grep. Requiring the marker to sit within a few characters
    before the topic word, or to open the line, keeps that pair out.
    """
    low = text.lower()
    for word in _topic_words(topic):
        needle = re.escape(word.lower())
        for match in re.finditer(rf"(?<![a-z0-9_-]){needle}(?![a-z0-9_-])", low):
            if _has_negation(low[max(0, match.start() - 14) : match.start()]):
                return True
    return bool(_NEGATION_RE.match(low))


def _instructional(text: str) -> bool:
    return bool(_INSTRUCTION.search(text))


def _tools_in(text: str) -> set[str]:
    return {match.lower() for match in _TOOL_RE.findall(text.lower())}


def _path_topics(text: str) -> set[str]:
    found = set(_WINDOWS_PATH.findall(text)) | set(_POSIX_PATH.findall(text))
    return {"路径:" + item.lower().rstrip("\\/") for item in found if len(item) > 3}


def _topics(text: str) -> set[str]:
    """What this candidate is about: a tool family, a named tool, a file path.

    Deliberately narrow. Subject words in prose ("构建", "发布") would make
    almost any two rules look related, and a conflict flag on unrelated facts
    teaches the reviewer to ignore the flag.
    """
    tools = _tools_in(text)
    out = {"工具:" + tool for tool in tools}
    out |= {"家族:" + _FAMILY_OF[tool] for tool in tools if tool in _FAMILY_OF}
    return out | _path_topics(text)


def _family_members(text: str) -> set[tuple[str, str]]:
    return {
        (_FAMILY_OF[tool], tool) for tool in _tools_in(text) if tool in _FAMILY_OF
    }


def _subjects(text: str) -> set[str]:
    """What is being talked about, ignoring tool names and filler words.

    Used to keep "用 pnpm 构建" and "用 npm 构建" together (both 构建) while
    keeping "用 rg 读文件" and "用 grep 查远程逻辑" apart, even though rg and
    grep are alternatives.
    """
    low = text.lower()
    words = set(re.findall(r"[a-z0-9_]{3,}", low)) - _STOP_WORDS
    for members in FAMILIES.values():
        words -= set(members)
    grams = {
        text[i : i + 2]
        for i in range(len(text) - 1)
        if "\u4e00" <= text[i] <= "\u9fff" and "\u4e00" <= text[i + 1] <= "\u9fff"
    }
    return words | (grams - _STOP_BIGRAMS)


def _disagree(left: dict, right: dict, topic: str) -> bool:
    if left is right:
        return False
    if topic.partition(":")[0] == "家族":
        # Two different tools from one family, both phrased as an instruction,
        # about the same thing: "构建用 pnpm" against "构建用 npm". Polarity is
        # *not* compared here: "不要用 npm" and "用 pnpm" agree with each other,
        # and the per-tool topic below is where a real do/don't clash is caught.
        only_left = _family_members(left["text"]) - _family_members(right["text"])
        only_right = _family_members(right["text"]) - _family_members(left["text"])
        if not (only_left and only_right):
            return False
        shared = _subjects(left["text"]) & _subjects(right["text"])
        return bool(shared) and _instructional(left["text"]) and _instructional(right["text"])
    # One says do it, the other says don't, about this exact tool or path.
    return _negated_near(left["text"], topic) != _negated_near(right["text"], topic)


def conflicts(candidates: list[dict]) -> list[list[str]]:
    """Group candidates that contradict each other; mark every member.

    Sets `conflict` / `conflict_with` on the candidates in place and returns the
    groups as ids, which is what the board and the CLI print.
    """
    for cand in candidates:
        cand["conflict"] = False
        cand["conflict_with"] = []
    buckets: dict[str, list[dict]] = {}
    for cand in candidates:
        for topic in _topics(cand["text"]):
            buckets.setdefault(topic, []).append(cand)

    groups: list[list[dict]] = []
    for topic, members in buckets.items():
        if len(members) < 2:
            continue
        found: list[dict] = []
        for i, left in enumerate(members):
            for right in members[i + 1 :]:
                if _disagree(left, right, topic):
                    found.extend((left, right))
        unique: list[dict] = []
        for cand in found:
            if all(cand is not other for other in unique):
                unique.append(cand)
        if len(unique) > 1:
            groups.append(unique)

    for group in groups:
        ids = [cand["id"] for cand in group]
        for cand in group:
            cand["conflict"] = True
            cand["conflict_with"] = [other for other in ids if other != cand["id"]]
    return [[cand["id"] for cand in group] for group in groups]


# ---- harvest -----------------------------------------------------------


def harvest(project_dir: str | os.PathLike[str] | None = None) -> list[dict]:
    """Candidates read out of every source file, deduped and conflict-marked.

    `adopted` is decided by looking at the project's own `## 已验证事实`
    section, so a fact already written down is shown as reviewed rather than
    offered again.
    """
    root = _project(project_dir)
    raw: list[dict] = []
    for path, scope in _files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        raw.extend(_extract(text, path, scope))
    merged = _merge(raw)
    seen = _fact_hashes(_read(root / "AGENTS.md"))
    labels: dict[str, str] = {}
    for cand in merged:
        cand["adopted"] = cand["hash"] in seen
        for source in cand["sources"]:
            if source["path"] not in labels:
                labels[source["path"]] = short_path(source["path"], root)
            source["label"] = labels[source["path"]]
    conflicts(merged)
    # Rules first, headings last, the project's own files before the global
    # ones: the reviewer's eye should land on what this project decided.
    merged.sort(key=_review_order)
    return merged


def _review_order(cand: dict) -> tuple:
    rank = {"bullet": 0, "sentence": 1, "heading": 2}.get(cand["kind"], 3)
    scope = 0 if cand["sources"][0]["scope"] == "project" else 1
    return (not cand["conflict"], rank, scope, cand["id"])


# ---- adopt -------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _section(text: str, heading: str = SECTION) -> tuple[int, int] | None:
    """Line span of the `## 已验证事实` section: heading included, trailing
    blank lines excluded. The next heading of level 1 or 2 ends it."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == heading), None)
    if start is None:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^#{1,2}\s", lines[i]):
            end = i
            break
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def _fact_body(line: str) -> str:
    stripped = line.strip()
    if not stripped.startswith("-"):
        return ""
    body = stripped[1:].strip()
    match = _FACT_PREFIX.match(body)
    return (match.group(1) if match else body).strip()


def _fact_hashes(text: str) -> set[str]:
    span = _section(text)
    if span is None:
        return set()
    start, end = span
    out: set[str] = set()
    for line in text.splitlines()[start + 1 : end]:
        body = _fact_body(line)
        if body:
            out.add(_hash(_norm(body)))
    return out


def short_path(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> str:
    """How a source is named in a fact line: `AGENTS.md`, `sub/AGENTS.md`,
    `~/.codex/AGENTS.md`. Short enough to read, specific enough to trace."""
    target = Path(path)
    try:
        return target.resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        pass
    try:
        return "~/" + target.resolve().relative_to(_user_home().resolve()).as_posix()
    except (ValueError, OSError):
        return str(path)


def _source_label(cand: dict, root: Path) -> str:
    labels = [
        item.get("label") or short_path(item["path"], root) for item in cand["sources"]
    ]
    return "、".join(labels) if labels else "unknown"


def _append(root: Path, items: list[tuple[str, str]]) -> dict:
    """Write the facts that are not there yet. Never rewrites other lines.

    The section is appended to, not rebuilt: a human's edits above it, the date
    format, the trailing newline — all of it stays exactly as it was.
    """
    path = root / "AGENTS.md"
    text = _read(path)
    known = _fact_hashes(text)
    fresh: list[str] = []
    for body, label in items:
        digest = _hash(_norm(body))
        if digest in known:
            continue
        known.add(digest)
        fresh.append(f"- {date.today().isoformat()}（来源 {label}）{body}")
    if not fresh:
        return {"path": str(path), "added": 0, "already": len(items)}

    lines = text.splitlines()
    span = _section(text)
    if span is None:
        while lines and not lines[-1].strip():
            lines.pop()
        head = ["", SECTION, ""] if lines else [SECTION, ""]
        lines = lines + head + fresh
    else:
        _, end = span
        lines = lines[:end] + fresh + lines[end:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": str(path), "added": len(fresh), "already": len(items) - len(fresh)}


def adopt(
    candidate_ids: list[str] | str,
    project_dir: str | os.PathLike[str] | None = None,
) -> dict:
    """Append reviewed candidates to the project's `AGENTS.md`.

    Idempotent: a fact already in the section is skipped, not duplicated. An
    unknown id is an error rather than a silent no-op, because the usual cause
    is a stale board whose source file has since changed.
    """
    root = _project(project_dir)
    ids = [candidate_ids] if isinstance(candidate_ids, str) else list(candidate_ids)
    ids = [str(item).strip() for item in ids if str(item).strip()]
    if not ids:
        raise ValueError("adopt needs at least one candidate id")
    known = {cand["id"]: cand for cand in harvest(root)}
    missing = [cid for cid in ids if cid not in known]
    if missing:
        raise ValueError("unknown candidate id: " + ", ".join(missing[:5]))
    chosen = [known[cid] for cid in ids if not known[cid]["adopted"]]
    result = _append(root, [(cand["text"], _source_label(cand, root)) for cand in chosen])
    return {
        "ok": True,
        "adopted": [cand["id"] for cand in chosen],
        "path": result["path"],
        "added": result["added"],
        "already": result["already"],
    }


def remember(
    text: str,
    project_dir: str | os.PathLike[str] | None = None,
    source: str = "agent",
) -> dict:
    """One fact, written straight into `AGENTS.md`.

    This is the write path an agent gets through MCP. It is deliberately the
    same file a human edits: nothing lands anywhere else, and `git diff` is the
    review.
    """
    body = " ".join((text or "").split())
    if not body:
        raise ValueError("memory text is empty")
    root = _project(project_dir)
    result = _append(root, [(body[:MAX_TEXT], source)])
    return {
        "ok": True,
        "text": body[:MAX_TEXT],
        "path": result["path"],
        "added": result["added"],
    }


def verified_facts(project_dir: str | os.PathLike[str] | None = None) -> str:
    """The `## 已验证事实` section, or '' when there is nothing to inject.

    Only a harness that cannot read the file itself needs this, which is why
    the runner calls it behind `reads_memory_files` instead of on every run.
    """
    root = _project(project_dir)
    text = _read(root / "AGENTS.md")
    span = _section(text)
    if span is None:
        return ""
    start, end = span
    lines = text.splitlines()[start:end]
    if len(lines) <= 1:
        return ""
    return "\n".join(lines) + "\n"
