"""Render the current workspace as one markdown page.

The Axel Desk plugin turns open checkboxes in the vault into dashboard rows, so
the page is written as plain markdown: a table for state, `- [ ]` lines for
whatever still needs a human. No HTML, no frontmatter keys the plugin does not
read.
"""

from __future__ import annotations

import time

from . import db, ledger

VERIFY_LABEL = {"ok": "通过", "red": "失败", "none": "未验证"}
STATUS_LABEL = {
    "open": "进行中",
    "done": "完成",
    "needs_review": "待复审",
    "blocked": "阻塞",
}


def _money(run: dict) -> str:
    """One run's price for the table. Never borrows a symbol it did not record."""
    priced = ledger.cost_of(run.get("usage_json"))
    if priced is None:
        return "-"
    amount, currency, _basis = priced
    if not amount:
        # A meter with 0.01 resolution reads zero for a cheap run; "-" says
        # "no number", not "free".
        return "-"
    if currency == ledger.UNKNOWN_CURRENCY:
        return f"{ledger.UNKNOWN_LABEL} {amount:.2f}"
    if currency == "CNY":
        return f"¥{amount:.2f}"
    if currency == "USD":
        return f"${amount:.2f}"
    return f"{currency} {amount:.2f}"


def collect(limit: int = 15) -> list[dict]:
    """One row per task, newest first, with its latest run and verification."""
    rows: list[dict] = []
    for task in db.list_tasks(limit=limit):
        runs = db.list_runs(task["id"], limit=1)
        last = runs[0] if runs else None
        mark = db.verify_states([last["id"]]).get(last["id"], "none") if last else "none"
        rows.append(
            {
                "id": task["id"],
                "title": task.get("title") or task.get("goal") or task["id"],
                "status": task.get("status") or "open",
                "agent": _endpoint(last),
                "run_status": (last or {}).get("status") or "-",
                "verify": mark,
                "cost": _money(last) if last else "-",
                "updated": (last or {}).get("ended_at") or task.get("created_at") or 0,
            }
        )
    return rows


def _endpoint(run: dict | None) -> str:
    """Which pairing actually ran, not just which CLI was asked for."""
    if not run:
        return "-"
    route = run.get("route") or run.get("agent") or "-"
    provider = run.get("provider") or ""
    return route if not provider or provider == route else f"{route}→{provider}"


def todos(rows: list[dict]) -> list[str]:
    """Only things a human must look at. Empty list is a real answer."""
    out: list[str] = []
    unverified: list[str] = []
    for row in rows:
        if row["status"] == "needs_review":
            out.append(f"{row['title']}（{row['id']}）：审查有异议，需要人定夺")
        elif row["run_status"] == "failed":
            out.append(f"{row['title']}（{row['id']}）：最近一次执行失败，看 run 日志")
        elif row["verify"] == "red":
            out.append(f"{row['title']}（{row['id']}）：测试红着")
        elif row["verify"] == "none" and row["run_status"] == "completed":
            unverified.append(row["id"])
    if unverified:
        # One line, not one per task: the same missing evidence repeated ten
        # times is noise, not ten separate decisions.
        out.append(
            f"{len(unverified)} 个任务跑完但没有验证记录（结果只来自自述）："
            + "、".join(unverified)
        )
    return out


def render(rows: list[dict], generated_at: float | None = None) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(generated_at or time.time()))
    pending = todos(rows)
    lines = [
        "# Nodus 项目进度",
        "",
        f"> 由 `nodus desk` 生成于 {stamp}。手改会被下一次覆盖；要留意见就写在这份文件之外。",
        "",
        "## 待处理",
        "",
    ]
    lines += [f"- [ ] {item}" for item in pending] or ["- 没有需要人处理的项。"]
    lines += [
        "",
        "## 任务",
        "",
        "| 任务 | 状态 | 最近执行 | 验证 | 成本 | 更新 |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(row["updated"])) if row["updated"] else "-"
        lines.append(
            f"| {row['title']} | {STATUS_LABEL.get(row['status'], row['status'])} "
            f"| {row['agent']}/{row['run_status']} | {VERIFY_LABEL.get(row['verify'], row['verify'])} "
            f"| {row['cost']} | {when} |"
        )
    if not rows:
        lines.append("| （没有任务） | | | | | |")
    lines.append("")
    return "\n".join(lines)
