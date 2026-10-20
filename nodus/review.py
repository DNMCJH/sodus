"""Cross-agent review.

A second agent reads the evidence (task, acceptance, what changed) and returns
structured findings. Two rules keep this honest:

1. The reviewer never decides completion. Its verdict can only downgrade a task
   to `needs_review`; a green test run is still what makes a task `done`.
2. The reviewed material is the evidence this repo already stores - artifacts,
   file contents, a git diff - not a summary the implementing agent wrote about
   itself.

The reviewer is always a different agent than the one that produced the work.
Reviewing your own patch is how "looks good to me" happens.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from . import agents, config, db
from .runner import RUNNER

SEVERITIES = ("critical", "high", "medium", "low")
SKIP_KINDS = {"handoff", "verify", "review", "review-context", "report"}
MAX_FILE_CHARS = 16_000
MAX_CONTEXT_CHARS = 80_000

NOTE = """你是独立审查者，不是实现者。只读审查，不要修改任何文件。

审查对象：任务「{title}」，实现者是 {agent}。{focus}

要求：
- 逐条对照验收标准，判断改动是否正确、是否有边界或安全问题。
- 每条结论必须给出文件和证据（引用具体代码或行为）。不要写"可能有问题"这类空话。
- 不要复述代码，不要打总体分，不要执行会改动文件的命令。

只输出一个 JSON 对象，前后不要有任何其他文字：
{{"verdict": "accept|revise|reject",
  "summary": "一句话结论",
  "findings": [{{"severity": "critical|high|medium|low",
                "file": "相对路径", "line": 0,
                "issue": "问题是什么", "evidence": "代码或行为证据",
                "suggestion": "怎么改"}}],
  "test_gaps": ["没有被测试覆盖的关键行为"]}}

## 证据

{context}
"""


def _git(cwd: Path, *args: str, timeout: float = 20.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


def _read_text(path: Path, limit: int) -> str:
    try:
        return path.read_text(encoding="utf-8")[:limit]
    except (OSError, UnicodeDecodeError):
        return ""


def _rel(path: Path, cwd: Path) -> str:
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


def _git_context(cwd: Path, rels: list[str]) -> dict | None:
    code, out = _git(cwd, "rev-parse", "--is-inside-work-tree")
    if code != 0 or "true" not in out:
        return None
    paths = rels or ["."]
    code, diff = _git(cwd, "diff", "HEAD", "--", *paths)
    if code != 0:
        return None
    code, status = _git(cwd, "status", "--porcelain")
    wanted = set(rels)
    untracked = [
        line[3:].strip().strip('"')
        for line in status.splitlines()
        if line.startswith("?? ") and (not wanted or line[3:].strip().strip('"') in wanted)
    ]
    sections: list[str] = []
    files: list[str] = []
    if diff.strip():
        sections.append("```diff\n" + diff + "\n```")
        files.extend(
            line[len("+++ b/") :].strip()
            for line in diff.splitlines()
            if line.startswith("+++ b/")
        )
    for rel in untracked[:20]:
        text = _read_text(cwd / rel, MAX_FILE_CHARS)
        if not text:
            continue
        sections.append(f"### {rel}（新增文件）\n```\n{text}\n```")
        files.append(rel)
    if not sections:
        return None
    text = "\n\n".join(sections)
    truncated = len(text) > MAX_CONTEXT_CHARS
    return {
        "mode": "git",
        "text": text[:MAX_CONTEXT_CHARS],
        "files": files,
        "truncated": truncated,
    }


def _file_context(cwd: Path, rows: list[dict]) -> dict:
    sections: list[str] = []
    files: list[str] = []
    total = 0
    truncated = False
    for row in rows:
        path = Path(row["path"])
        body = _read_text(path, MAX_FILE_CHARS)
        if not body:
            continue
        rel = row.get("rel_path") or _rel(path, cwd)
        rel = rel.replace("\\", "/")
        marker = ""
        recorded = row.get("hash")
        if recorded and _digest(path) != recorded:
            marker = "（当前内容与 run 记录不一致）"
        room = MAX_CONTEXT_CHARS - total
        if room <= 0:
            truncated = True
            break
        if len(body) > room:
            body = body[:room]
            truncated = True
        sections.append(f"### {rel}{marker}\n```\n{body}\n```")
        files.append(rel)
        total += len(body)
    return {"mode": "files", "text": "\n\n".join(sections), "files": files, "truncated": truncated}


def build_context(task: dict, target_run: dict, scope: str = "task") -> dict:
    """What the reviewer gets to see. Bounded, and never a self-report alone."""
    cwd = Path(target_run.get("cwd") or ".").resolve()
    rows = db.artifacts_for_task(task.get("id", ""))
    if scope == "run":
        rows = [r for r in rows if r.get("run_id") == target_run.get("id")]
    latest: dict[str, dict] = {}
    for row in rows:  # ordered by created_at, last write wins
        if row.get("kind") in SKIP_KINDS:
            continue
        latest[row["path"]] = row

    header = [
        "### 任务",
        f"- 目标：{task.get('goal', '')}",
        f"- 验收：{task.get('acceptance') or '（未提供）'}",
        f"- 实现者：{target_run.get('agent')}（{target_run.get('id')}）",
        f"- 状态：{target_run.get('status')}",
        "",
        "### 改动",
    ]
    rows = list(latest.values())
    rels = []
    for row in rows:
        rel = row.get("rel_path") or _rel(Path(row["path"]), cwd)
        rel = rel.replace("\\", "/")
        if rel and not Path(rel).is_absolute():
            rels.append(rel)
    ctx = _git_context(cwd, rels)
    if ctx is None:
        ctx = _file_context(cwd, rows)
    text = "\n".join(header) + "\n\n" + (ctx["text"] or "（没有找到改动文件）")
    ctx["text"] = text[: MAX_CONTEXT_CHARS + 2000]
    ctx["chars"] = len(ctx["text"])
    ctx["cwd"] = str(cwd)
    return ctx


def pick_reviewer(target_agent: str, by: str = "") -> str:
    secrets = config.load_secrets()

    def ready(name: str) -> bool:
        return agents.ready(name, secrets)

    if by:
        if not ready(by):
            raise ValueError(f"reviewer not ready: {by}")
        if by == target_agent:
            raise ValueError(f"reviewer must differ from the implementing agent ({by})")
        return by
    order = {
        "claude": ["codex", "dsh"],
        "codex": ["claude", "dsh"],
        "dsh": ["claude", "codex"],
    }.get(target_agent, ["claude", "codex"])
    for name in order:
        if ready(name):
            return name
    raise ValueError(f"no ready reviewer besides {target_agent}")


def _balanced(text: str, start: int) -> str | None:
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_json_object(text: str) -> dict | None:
    """Find the first JSON object in a reply, fenced or prose-wrapped."""
    text = (text or "").strip()
    if not text:
        return None
    candidates: list[str] = []
    if "```" in text:
        parts = text.split("```")
        for i in range(1, len(parts), 2):
            body = parts[i]
            if body.lstrip().lower().startswith("json"):
                body = body.lstrip()[4:]
            candidates.append(body)
    candidates.append(text)
    for body in candidates:
        try:
            obj = json.loads(body.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    for i, ch in enumerate(text[:20_000]):
        if ch != "{":
            continue
        chunk = _balanced(text, i)
        if not chunk:
            continue
        try:
            obj = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def normalize(obj: dict | None) -> dict:
    if not isinstance(obj, dict):
        return {
            "verdict": "unparsed",
            "summary": "",
            "findings": [],
            "test_gaps": [],
            "counts": {s: 0 for s in SEVERITIES},
        }
    findings: list[dict] = []
    for raw in obj.get("findings") or []:
        if not isinstance(raw, dict):
            continue
        sev = str(raw.get("severity", "")).strip().lower()
        if sev not in SEVERITIES:
            sev = "medium"
        try:
            line = int(raw.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        findings.append(
            {
                "severity": sev,
                "file": str(raw.get("file") or ""),
                "line": line,
                "issue": str(raw.get("issue") or "").strip(),
                "evidence": str(raw.get("evidence") or "").strip(),
                "suggestion": str(raw.get("suggestion") or "").strip(),
            }
        )
    findings.sort(key=lambda f: (SEVERITIES.index(f["severity"]), f["file"]))
    verdict = str(obj.get("verdict") or "").strip().lower()
    if verdict not in ("accept", "revise", "reject"):
        verdict = "unknown"
    counts = {s: 0 for s in SEVERITIES}
    for f in findings:
        counts[f["severity"]] += 1
    gaps = [str(g).strip() for g in (obj.get("test_gaps") or []) if str(g).strip()]
    return {
        "verdict": verdict,
        "summary": str(obj.get("summary") or "").strip(),
        "findings": findings,
        "test_gaps": gaps,
        "counts": counts,
    }


def render_md(data: dict) -> str:
    lines = [
        f"# 审查 — {data.get('task_title') or data.get('target_run')}",
        "",
        f"- 审查者：{data.get('reviewer')}（{data.get('review_run')}）",
        f"- 被审：{data.get('target_run')}（{data.get('target_agent')}）",
        f"- 结论：**{data.get('verdict')}**",
    ]
    if data.get("summary"):
        lines += ["", data["summary"]]
    counts = data.get("counts") or {}
    lines += [
        "",
        f"- critical {counts.get('critical', 0)} / high {counts.get('high', 0)} / "
        f"medium {counts.get('medium', 0)} / low {counts.get('low', 0)}",
    ]
    findings = data.get("findings") or []
    if findings:
        lines += ["", "## Findings", ""]
        for f in findings:
            loc = f.get("file") or "(未指明)"
            if f.get("line"):
                loc += f":{f['line']}"
            lines += [
                f"### [{f['severity'].upper()}] {loc}",
                "",
                f.get("issue", ""),
                "",
                f"- 证据：{f.get('evidence') or '（无）'}",
                f"- 建议：{f.get('suggestion') or '（无）'}",
                "",
            ]
    gaps = data.get("test_gaps") or []
    if gaps:
        lines += ["## 测试缺口", ""]
        lines += [f"- {g}" for g in gaps]
        lines += [""]
    lines += ["## 审查输入", "", f"- 模式：{data.get('context_mode')}", f"- 文件：{', '.join(data.get('context_files') or []) or '（无）'}", ""]
    return "\n".join(lines)


def run_review(
    target_run_id: str,
    by: str = "",
    focus: str = "",
    scope: str = "task",
    timeout: int = 1800,
    dry_run: bool = False,
) -> dict:
    target = db.get_run(target_run_id)
    if not target:
        raise ValueError(f"unknown run: {target_run_id}")
    task = db.get_task(target.get("task_id", ""))
    if not task:
        raise ValueError(f"run has no task: {target_run_id}")
    reviewer = pick_reviewer(target.get("agent", ""), by)
    ctx = build_context(task, target, scope)
    note = NOTE.format(
        title=task.get("title") or task.get("goal", "")[:60],
        agent=target.get("agent"),
        focus=focus,
        context=ctx["text"],
    )
    base = {
        "ok": False,
        "target_run": target_run_id,
        "target_agent": target.get("agent"),
        "reviewer": reviewer,
        "context_mode": ctx["mode"],
        "context_files": ctx["files"],
        "context_chars": ctx["chars"],
        "context_truncated": ctx["truncated"],
    }
    if dry_run:
        return {**base, "ok": True, "dry_run": True, "note_chars": len(note)}

    review_run = RUNNER.start(
        task["id"],
        reviewer,
        target.get("cwd"),
        parent_run_id=target_run_id,
        note=note,
        include_handoff=False,  # a reviewer must not inherit the previous verdict
    )
    rid = review_run["id"]
    base["review_run"] = rid
    db.append_event(
        target_run_id,
        db.next_seq(target_run_id),
        "review.requested",
        {"review_run": rid, "by": reviewer, "scope": scope},
    )

    finished = RUNNER.wait(rid, timeout=timeout)
    run_dir = config.HOME / "runs" / rid
    final_path = run_dir / "final.md"
    final = _read_text(final_path, 200_000)
    parsed = normalize(extract_json_object(final))

    data = {
        **base,
        "task_id": task["id"],
        "task_title": task.get("title", ""),
        "status": finished.get("status"),
        "run_error": finished.get("error"),
        "created_at": time.time(),
        **parsed,
        "raw_reply": final,
    }
    (run_dir / "review.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "review.md").write_text(render_md(data), encoding="utf-8")
    (run_dir / "context.md").write_text(ctx["text"], encoding="utf-8")

    for name, kind, summary in (
        ("review.json", "review", "json"),
        ("review.md", "review", "md"),
        ("context.md", "review-context", ctx["mode"]),
    ):
        path = run_dir / name
        db.add_artifact(
            rid,
            task["id"],
            kind,
            str(path),
            _digest(path),
            summary=summary,
        )

    ok = parsed["verdict"] != "unparsed"
    data["error"] = (
        ""
        if ok
        else finished.get("error") or "reviewer did not return a parseable verdict"
    )
    db.append_event(
        rid,
        db.next_seq(rid),
        "review.done",
        {
            "verdict": parsed["verdict"],
            "counts": parsed["counts"],
            "summary": parsed["summary"],
            "target_run": target_run_id,
            "reviewer": reviewer,
            "path": str(run_dir / "review.json"),
        },
    )
    return {**data, "ok": ok}


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()[:16]
