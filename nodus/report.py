"""Task report: one page answering "what actually happened here?".

Everything in the report comes from the database and the decisions log, not from
an agent's summary. If a number is missing (Codex does not report cost, for
example), the report says so instead of interpolating.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import config, db

#: Vendors bill in their own currency; never add two currencies together.
CURRENCY_SYMBOL = {"USD": "$", "CNY": "¥"}
COST_BASIS = {"metered": "实测扣费", "list_price": "价目表", "reported": "CLI 自报"}


def _usage(run: dict) -> dict:
    try:
        data = json.loads(run.get("usage_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _seconds(run: dict) -> float | None:
    started, ended = run.get("started_at"), run.get("ended_at")
    if not started or not ended:
        return None
    return max(0.0, ended - started)


def _fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _decisions(task_id: str) -> list[dict]:
    path = config.HOME / "decisions.jsonl"
    if not path.exists():
        return []
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("task_id") == task_id:
            out.append(row)
    return out


def _answer_summary(row: dict) -> str:
    parts = []
    for key, ans in (row.get("answers") or {}).items():
        value = ans.get("choice")
        if value is None:
            value = ans.get("score")
        if value is None:
            value = ans.get("noul")
        conf = ans.get("confidence")
        parts.append(f"{key}={value}" + (f" ({conf:.2f})" if isinstance(conf, float) else ""))
    return " ".join(parts)


def collect(task_id: str) -> dict:
    task = db.get_task(task_id)
    runs = sorted(
        db.list_runs(task_id, 200), key=lambda r: r.get("started_at") or 0
    )
    verifies: list[dict] = []
    reviews: list[dict] = []
    changed: list[str] = []
    for run in runs:
        for ev in db.events_filtered(run["id"], ("verify.", "review.")):
            etype = ev["type"]
            if etype.startswith("verify.") and etype != "verify.tests_changed":
                verifies.append({"run_id": run["id"], "agent": run["agent"], **ev["payload"]})
            elif etype == "review.done":
                reviews.append({"run_id": run["id"], **ev["payload"]})
            elif etype == "verify.tests_changed":
                changed.extend(ev["payload"].get("files") or [])

    totals = {
        "in": 0,
        "out": 0,
        "cache_read": 0,
        "cache_write": 0,
        "costs": {},
        "seconds": 0.0,
    }
    cost_agents: set[str] = set()
    cost_basis: set[str] = set()
    cost_below = 0
    for run in runs:
        usage = _usage(run)
        totals["in"] += int(usage.get("input_tokens") or 0)
        totals["out"] += int(usage.get("output_tokens") or 0)
        totals["cache_read"] += int(usage.get("cache_read") or 0)
        totals["cache_write"] += int(usage.get("cache_write") or 0)
        if isinstance(usage.get("cost"), (int, float)):
            currency = usage.get("currency") or "USD"
            totals["costs"][currency] = totals["costs"].get(currency, 0.0) + float(usage["cost"])
            cost_agents.add(run["agent"])
            cost_basis.add(usage.get("cost_basis") or "reported")
            # A meter with 0.01 resolution reads zero for cheap runs. Printing
            # that as "¥0.0000" would claim the work was free.
            if not usage["cost"] and (usage.get("input_tokens") or usage.get("output_tokens")):
                cost_below += 1
        seconds = _seconds(run)
        if seconds:
            totals["seconds"] += seconds

    started = [r["started_at"] for r in runs if r.get("started_at")]
    ended = [r["ended_at"] for r in runs if r.get("ended_at")]
    totals["wall"] = (max(ended) - min(started)) if started and ended else None
    totals["cost_agents"] = sorted(cost_agents)
    totals["cost_basis"] = sorted(cost_basis)
    totals["cost_below_resolution"] = cost_below

    flags: list[str] = []
    latest_review: dict[str, dict] = {}
    for review in reviews:
        latest_review[review.get("target_run") or review.get("run_id") or ""] = review
    failed = [r for r in runs if r.get("status") == "failed"]
    if failed:
        flags.append(f"{len(failed)} 个 run 失败：" + ", ".join(r["id"] for r in failed))
    if changed:
        flags.append("测试文件在实现阶段被改动：" + ", ".join(sorted(set(changed))))
    reds = [v for v in verifies if v.get("phase") == "red"]
    greens = [v for v in verifies if v.get("phase") == "green"]
    if not verifies:
        flags.append("没有任何验证记录：结果只靠 agent 自述")
    else:
        if not reds:
            flags.append("没有 red 记录：无法证明测试是先失败再通过的")
        elif reds[0].get("ok"):
            flags.append("red 阶段测试直接通过：测试可能没有覆盖新功能")
        if not greens:
            flags.append("没有 green 记录：任务没有通过测试")
    if not reviews:
        flags.append("未做交叉审查")
    else:
        bad = [v for v in latest_review.values() if v.get("verdict") != "accept"]
        if bad:
            flags.append(
                "最新审查未通过："
                + ", ".join(f"{v.get('verdict')}({v.get('reviewer')})" for v in bad)
            )
    if task.get("status") == "needs_review":
        flags.append("任务状态 needs_review：测试通过但审查有异议")

    return {
        "task": task,
        "runs": runs,
        "verifies": verifies,
        "reviews": reviews,
        "latest_review_runs": [r.get("run_id") for r in latest_review.values()],
        "decisions": _decisions(task_id),
        "totals": totals,
        "flags": flags,
        "generated_at": time.time(),
    }


def render_md(data: dict) -> str:
    task = data["task"]
    totals = data["totals"]
    lines = [
        f"# 任务报告 — {task.get('title') or task.get('id')}",
        "",
        f"- 任务：{task.get('id')}",
        f"- 状态：{task.get('status')}",
        f"- 目标：{task.get('goal', '')}",
        f"- 验收：{task.get('acceptance') or '（未提供）'}",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data['generated_at']))}",
        "",
        "## 总览",
        "",
        f"- 墙钟：{_fmt_dur(totals.get('wall'))}（agent 累计 {_fmt_dur(totals.get('seconds'))}）",
        f"- token：in {totals['in']:,} / out {totals['out']:,} / "
        f"cache_read {totals['cache_read']:,} / cache_write {totals['cache_write']:,}",
    ]
    if totals.get("costs"):
        parts = " + ".join(
            f"{CURRENCY_SYMBOL.get(cur, cur + ' ')}{amount:.4f}"
            for cur, amount in sorted(totals["costs"].items())
        )
        basis = "/".join(COST_BASIS.get(b, b) for b in totals.get("cost_basis") or [])
        below = totals.get("cost_below_resolution") or 0
        extra = f"；{below} 个 run 低于计量精度，记 0" if below else ""
        lines.append(
            f"- 成本：{parts}（{basis or '来源未知'}；覆盖 {'/'.join(totals['cost_agents'])}{extra}）"
        )
    else:
        lines.append("- 成本：未上报（agent CLI 没有返回 cost 字段，也没有可读的计费表）")
    lines += ["", "## 执行", "", "| run | agent | 状态 | 用时 | in | out | cache_read |", "|---|---|---|---|---|---|---|"]
    for run in data["runs"]:
        usage = _usage(run)
        lines.append(
            f"| {run['id']} | {run['agent']} | {run.get('status')} | "
            f"{_fmt_dur(_seconds(run))} | {int(usage.get('input_tokens') or 0):,} | "
            f"{int(usage.get('output_tokens') or 0):,} | {int(usage.get('cache_read') or 0):,} |"
        )
    if not data["runs"]:
        lines.append("| （无） | | | | | | |")

    lines += ["", "## 验证", "", "| 阶段 | 结果 | exit | 用时 | 证据 |", "|---|---|---|---|---|"]
    for v in data["verifies"]:
        phase = v.get("phase")
        ok = v.get("ok")
        verdict = "PASS" if ok else "FAIL"
        if phase == "red":
            verdict += "（预期）"
        lines.append(
            f"| {phase} | {verdict} | {v.get('exit')} | {v.get('duration_ms')}ms | {v.get('path')} |"
        )
    if not data["verifies"]:
        lines.append("| （无） | | | | |")

    lines += ["", "## 审查", ""]
    if not data["reviews"]:
        lines.append("（未做交叉审查）")
    for r in data["reviews"]:
        counts = r.get("counts") or {}
        latest = r.get("run_id") in set(data.get("latest_review_runs") or [])
        lines.append(
            f"### {r.get('verdict')} — {r.get('reviewer')}（{r.get('run_id')}）"
            + ("（最新）" if latest else "（已被后一次审查取代）")
        )
        lines.append("")
        if r.get("summary"):
            lines.append(r["summary"])
            lines.append("")
        lines.append(
            f"- critical {counts.get('critical', 0)} / high {counts.get('high', 0)} / "
            f"medium {counts.get('medium', 0)} / low {counts.get('low', 0)}"
        )
        src = r.get("path")
        if src:
            md = Path(src).with_name("review.md")
            lines.append(f"- 详情：{md}")
        lines.append("")

    decisions = data["decisions"]
    if decisions:
        lines += ["## 决策（Jev，仅建议）", "", "| 时间 | 类型 | 结果 | 延迟 |", "|---|---|---|---|"]
        for d in decisions:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(d.get("ts", 0)))
            lines.append(
                f"| {stamp} | {d.get('kind')} | {_answer_summary(d)} | {d.get('latency_ms', '-')}ms |"
            )

    lines += ["", "## 标记", ""]
    if data["flags"]:
        lines += [f"- {f}" for f in data["flags"]]
    else:
        lines.append("- 无")
    lines.append("")
    return "\n".join(lines)


def write_report(task_id: str, out: str = "") -> tuple[Path, dict]:
    data = collect(task_id)
    text = render_md(data)
    path = Path(out) if out else config.HOME / "tasks" / task_id / "report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    last = data["runs"][-1]["id"] if data["runs"] else ""
    if last:
        digest = ""
        try:
            import hashlib

            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        except OSError:
            pass
        db.add_artifact(last, task_id, "report", str(path), digest, summary="md")
    return path, data
