"""Repair loop: review findings -> another agent fixes -> verify -> review again.

This is the only place where agents take turns on the same artifact with a
machine-checked brake between them:

    reviewer (agent X) -> fixer (agent Y) -> tests -> reviewer (agent X again)

The loop stops on acceptance, on a failed verification, or when rounds run out.
A fixer never gets to declare success: the test exit code and the next review do.
"""

from __future__ import annotations

from pathlib import Path

from . import db, verify
from . import review as review_mod
from .runner import RUNNER

FIX_NOTE = """上一轮审查发现了问题，请修复。

审查结论：{verdict}
摘要：{summary}

问题清单：
{findings}

要求：
- 只改生产代码，不要修改测试文件；改测试视为作弊。
- 每一条要么修掉，要么在最终回复里说明为什么不成立，不要沉默跳过。
- 修完自己跑一遍测试（{test_cmd}）确认仍然是绿的。
- 不要顺手改无关代码。
"""


def render_findings(findings: list[dict], include_low: bool = True) -> str:
    lines = []
    for f in findings:
        if f.get("severity") == "low" and not include_low:
            continue
        loc = f.get("file") or "(未指明)"
        if f.get("line"):
            loc += f":{f['line']}"
        line = f"- [{f['severity'].upper()}] {loc}  {f.get('issue') or ''}"
        if f.get("evidence"):
            line += f"\n  证据：{f['evidence'][:400]}"
        if f.get("suggestion"):
            line += f"\n  建议：{f['suggestion'][:300]}"
        lines.append(line)
    return "\n".join(lines) or "- （审查没有给出具体问题）"


def latest_review(task_id: str, target_run_id: str) -> dict | None:
    """Newest review whose target is this run. Reviews live on the reviewer's run."""
    best: dict | None = None
    for run in db.list_runs(task_id, 200):
        for ev in db.events_filtered(run["id"], ("review.done",)):
            payload = ev["payload"]
            if payload.get("target_run") != target_run_id:
                continue
            if best is None or ev["ts"] >= best.get("ts", 0):
                best = {
                    **payload,
                    "review_run": run["id"],
                    "ts": ev["ts"],
                }
    return best


def pick_fixer(target_agent: str, reviewer: str, by: str = "") -> str:
    from . import config, routes

    secrets = config.load_secrets()

    def ready(name: str) -> bool:
        return routes.ready(name, secrets)

    if by:
        if not ready(by):
            raise ValueError(f"fixer not ready: {by}")
        return by
    # Prefer a third agent: not the reviewer, not the one who wrote the bug.
    for name in ("dsh", "claude", "codex"):
        if name != reviewer and name != target_agent and ready(name):
            return name
    for name in ("claude", "codex", "dsh"):
        if name != reviewer and ready(name):
            return name
    raise ValueError("no ready fixer")


def run_fix(
    target_run_id: str,
    by: str = "",
    rounds: int = 2,
    review_first: bool = False,
    timeout: int = 1800,
) -> dict:
    target = db.get_run(target_run_id)
    if not target:
        raise ValueError(f"unknown run: {target_run_id}")
    task = db.get_task(target.get("task_id", ""))
    if not task:
        raise ValueError(f"run has no task: {target_run_id}")
    cwd = Path(target.get("cwd") or ".").resolve()
    detected = verify.detect(cwd)
    if not detected:
        raise ValueError(f"no test runner detected in {cwd}")

    result: dict = {
        "ok": False,
        "target_run": target_run_id,
        "agent": target.get("agent"),
        "rounds": [],
    }

    review = latest_review(task["id"], target_run_id)
    if review_first or not review or review.get("verdict") == "accept":
        fresh = review_mod.run_review(target_run_id)
        result["forced_review"] = fresh.get("review_run")
        if not fresh.get("ok"):
            result["error"] = fresh.get("error") or "review failed"
            return result
        review = {
            "verdict": fresh.get("verdict"),
            "summary": fresh.get("summary"),
            "findings": fresh.get("findings") or [],
            "counts": fresh.get("counts") or {},
            "reviewer": fresh.get("reviewer"),
            "review_run": fresh.get("review_run"),
        }
    result["review_before"] = {
        "verdict": review.get("verdict"),
        "reviewer": review.get("reviewer"),
        "review_run": review.get("review_run"),
        "counts": review.get("counts"),
    }

    fixer = pick_fixer(target.get("agent", ""), review.get("reviewer", ""), by)
    result["fixer"] = fixer
    baseline = verify.test_file_hashes(cwd)

    for i in range(1, rounds + 1):
        if review.get("verdict") == "accept":
            break
        note = FIX_NOTE.format(
            verdict=review.get("verdict"),
            summary=review.get("summary") or "",
            findings=render_findings(review.get("findings") or []),
            test_cmd=" ".join(detected["cmd"]),
        )
        fix_run = RUNNER.start(
            task["id"],
            fixer,
            str(cwd),
            parent_run_id=target_run_id,
            note=note,
            include_handoff=False,
        )
        db.append_event(
            fix_run["id"],
            db.next_seq(fix_run["id"]),
            "fix.requested",
            {
                "target_run": target_run_id,
                "round": i,
                "fixer": fixer,
                "review_run": review.get("review_run"),
                "verdict": review.get("verdict"),
            },
        )
        RUNNER.wait(fix_run["id"], timeout=timeout)
        check = verify.run(detected["cmd"], cwd)
        verify.persist(fix_run["id"], task["id"], f"fix{i}", check, note=f"after fix round {i}")
        hashes_now = verify.test_file_hashes(cwd)
        changed = [
            name for name, digest in baseline.items() if hashes_now.get(name) != digest
        ]
        if changed:
            db.append_event(
                fix_run["id"],
                db.next_seq(fix_run["id"]),
                "verify.tests_changed",
                {"files": sorted(set(changed)), "round": i},
            )

        entry = {
            "round": i,
            "fix_run": fix_run["id"],
            "verify": {"ok": check["ok"], "exit": check["exit"]},
            "tests_changed": sorted(set(changed)),
        }
        result["rounds"].append(entry)
        if not check["ok"]:
            entry["error"] = "tests failed after fix"
            break

        review = review_mod.run_review(target_run_id, by=review.get("reviewer") or "")
        entry["review_verdict"] = review.get("verdict")
        entry["review_run"] = review.get("review_run")
        if not review.get("ok"):
            entry["error"] = review.get("error") or "re-review failed"
            break
        db.append_event(
            fix_run["id"],
            db.next_seq(fix_run["id"]),
            "fix.done",
            {
                "round": i,
                "target_run": target_run_id,
                "verify_ok": check["ok"],
                "review_verdict": review.get("verdict"),
                "review_run": review.get("review_run"),
            },
        )

    final = result["rounds"][-1] if result["rounds"] else None
    result["review_after"] = {
        "verdict": review.get("verdict"),
        "reviewer": review.get("reviewer"),
        "review_run": review.get("review_run"),
        "counts": review.get("counts"),
    }
    result["ok"] = bool(
        final
        and final.get("verify", {}).get("ok")
        and review.get("verdict") == "accept"
        and not final.get("tests_changed")
    )
    db.update_task(task["id"], status="done" if result["ok"] else "needs_review")
    return result


def last_fix_summary(task_id: str, target_run_id: str) -> dict | None:
    """Newest fix.done for a target, for report-style consumers."""
    best = None
    for run in db.list_runs(task_id, 200):
        for ev in db.events_filtered(run["id"], ("fix.done",)):
            payload = ev["payload"]
            if payload.get("target_run") != target_run_id:
                continue
            if best is None or ev["ts"] >= best.get("ts", 0):
                best = {**payload, "fix_run": run["id"], "ts": ev["ts"]}
    return best
