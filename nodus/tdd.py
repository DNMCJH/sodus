"""TDD pipeline: a failing test first, then an implementation that turns it green.

The acceptance signal is the test runner's exit code, not an agent's claim.
The writer and the implementer are different agents so the test is not tailor-made
for the implementation.

Every verification result is persisted (artifact + event) so the red/green story
survives the terminal. `review=True` adds a third agent that reads the evidence
and reports findings; its verdict can downgrade the task, never upgrade it.
"""

from __future__ import annotations

from pathlib import Path

from . import db, verify
from . import review as review_mod
from .runner import RUNNER

WRITER_NOTE = """只写测试，不实现功能。

要求：
- 新增或修改测试文件，覆盖任务的验收标准。
- 测试当前必须是失败的（因为功能还没实现）。
- 不要修改任何生产代码。
- 结束时说明：加了哪个测试、断言什么、为什么现在会失败。
"""

IMPL_NOTE = """让现有测试通过。

要求：
- 只改生产代码，不要修改测试文件；改测试视为作弊。
- 下面是最近一次测试失败输出，先读懂再动手：

{failure}
"""


def plan(cwd: Path, test_cmd: str = "") -> dict:
    found = verify.detect(cwd)
    if test_cmd:
        return {"cmd": test_cmd.split(), "kind": "custom", "reason": "explicit --test"}
    if not found:
        return {}
    return found


def run_tdd(
    task_id: str,
    cwd: Path,
    writer: str = "claude",
    impl: str = "codex",
    test_cmd: str = "",
    retries: int = 2,
    dry_run: bool = False,
    review: bool = False,
    reviewer: str = "",
    on_run=None,
) -> dict:
    cwd = Path(cwd).resolve()
    detected = plan(cwd, test_cmd)
    if not detected:
        return {"ok": False, "stage": "detect", "error": f"no test runner detected in {cwd}"}

    cmd = detected["cmd"]
    result = {
        "ok": False,
        "stage": "detect",
        "cmd": cmd,
        "kind": detected.get("kind"),
        "reason": detected.get("reason"),
        "runs": [],
        "review": None,
    }
    if dry_run:
        result["stage"] = "dry_run"
        steps = [
            {"agent": writer, "role": "write failing test"},
            {"step": "verify", "expect": "fail"},
        ]
        steps += [{"agent": impl, "role": "implement", "retries": retries}]
        steps += [{"step": "verify", "expect": "pass"}]
        if review:
            steps += [{"agent": reviewer or f"not {impl}", "role": "review findings"}]
        result["planned"] = steps
        return result

    hashes_initial = verify.test_file_hashes(cwd)

    # 1. writer adds a failing test
    run = RUNNER.start(task_id, writer, str(cwd), note=WRITER_NOTE)
    if on_run:
        on_run(run["id"])
    result["runs"].append(run["id"])
    result["stage"] = "writer"
    writer_run = RUNNER.wait(run["id"])
    if writer_run.get("status") == "failed" and verify.test_file_hashes(cwd) == hashes_initial:
        result["stage"] = "writer_failed"
        result["error"] = writer_run.get("error") or "writer run failed and no test file changed"
        db.update_task(task_id, status="failed")
        return result

    red = verify.run(cmd, cwd)
    red_ev = verify.persist(run["id"], task_id, "red", red, note=f"after writer {writer}")
    result["red"] = red
    result["red_artifact"] = red_ev.get("path")
    if red["ok"]:
        result["stage"] = "red_unexpected_pass"
        result["error"] = "test passed before implementation: feature exists or the test is vacuous"
        db.update_task(task_id, status="failed")
        return result

    hashes_before = verify.test_file_hashes(cwd)

    # 2. implementer turns it green
    last = red
    parent = run["id"]
    impl_run_id = ""
    for attempt in range(retries + 1):
        note = IMPL_NOTE.format(failure=last["tail"][-1500:])
        impl_run = RUNNER.start(task_id, impl, str(cwd), note=note, parent_run_id=parent)
        if on_run:
            on_run(impl_run["id"])
        impl_run_id = impl_run["id"]
        result["runs"].append(impl_run_id)
        parent = impl_run_id
        result["stage"] = f"impl_{attempt + 1}"
        RUNNER.wait(impl_run_id)
        last = verify.run(cmd, cwd)
        phase = "green" if last["ok"] else f"impl{attempt + 1}"
        green_ev = verify.persist(impl_run_id, task_id, phase, last, note=f"attempt {attempt + 1}")
        if last["ok"]:
            result["green_artifact"] = green_ev.get("path")
        if last["ok"]:
            break

    hashes_after = verify.test_file_hashes(cwd)
    changed = [name for name, digest in hashes_before.items() if hashes_after.get(name) != digest]
    result["tests_changed"] = changed
    if changed and impl_run_id:
        db.append_event(
            impl_run_id,
            db.next_seq(impl_run_id),
            "verify.tests_changed",
            {"files": changed},
        )

    result["green"] = last
    result["ok"] = last["ok"]
    result["stage"] = "green" if last["ok"] else "retry_exhausted"
    status = "done" if last["ok"] else "failed"
    db.update_task(task_id, status=status)

    # 3. optional review by a different agent
    if review and result["ok"]:
        try:
            rev = review_mod.run_review(impl_run_id, by=reviewer, scope="task")
        except (ValueError, RuntimeError) as exc:
            rev = {"ok": False, "error": str(exc)}
        result["review"] = rev
        if not rev.get("ok"):
            db.update_task(task_id, status="needs_review")
        elif rev.get("verdict") != "accept":
            db.update_task(task_id, status="needs_review")

    return result
