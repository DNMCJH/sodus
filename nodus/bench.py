"""Task-level evaluation: does a route actually finish the job?

SWE-bench's shape at a size that fits in a coffee break. A task is a directory
with a `start/` tree and a `hidden_tests/` tree the agent never sees:

    eval/tasks/<id>/task.json   id, title, prompt, timeout
    eval/tasks/<id>/start/      copied into a scratch directory before the run
    eval/tasks/<id>/hidden_tests/   copied in only after the agent stopped

The prompt goes to the agent through the normal run path; when it stops, the
hidden tests land in `_bench_checks/` under the work tree, pytest runs on that
directory alone, and its exit code is the score. Nothing here asks the agent
how it did, and an agent's own tests cannot turn a red result green.

Why this exists next to `verify.py`: verification answers "did this run leave
the project green?" using the project's own tests, which the agent can edit.
A benchmark answers "can this route do this class of work at all?" — same
tasks for every route, same start state, tests the agent cannot weaken.

Cost isolation is the caller's job: `cli/commands/bench.py` points
`config.HOME` at a scratch directory before anything runs, so no row ever
reaches the real database.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import db, runner, verify

REQUIRED = ("id", "title", "prompt", "timeout")
#: Directory the hidden tests land in, inside the work tree. Prefixed so it
#: cannot collide with an ordinary project directory, and removed before every
#: copy so a previous attempt's leftovers cannot be scored.
CHECK_DIR = "_bench_checks"
#: Directories that are noise for "did the working tree change?".
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", ".venv", "venv", "node_modules", CHECK_DIR}
TEST_TIMEOUT = 120

SOLVE = "solve"
TESTS_RED = "tests_red"
NO_CHANGE = "no_change"
TIMEOUT = "timeout"
AGENT_ERROR = "agent_error"
VERDICTS = (SOLVE, TESTS_RED, NO_CHANGE, TIMEOUT, AGENT_ERROR)


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    prompt: str
    timeout: float
    root: Path
    #: Declared difficulty 1-5. The router must read this, not re-derive it
    #: from the title: a title says what a task is about, not how hard it is.
    difficulty: int = 0

    @property
    def start(self) -> Path:
        return self.root / "start"

    @property
    def hidden(self) -> Path:
        return self.root / "hidden_tests"

    @property
    def test_files(self) -> list[Path]:
        if not self.hidden.is_dir():
            return []
        return sorted(p for p in self.hidden.rglob("*.py") if p.is_file())


def load_task(root: Path) -> Task:
    """Read one task directory. Raises ValueError with the directory named."""
    meta_path = root / "task.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"{root.name}: cannot read task.json: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"{root.name}: task.json is not valid JSON: {exc}") from None
    if not isinstance(meta, dict):
        raise ValueError(f"{root.name}: task.json must be an object")
    missing = [key for key in REQUIRED if not meta.get(key)]
    if missing:
        raise ValueError(f"{root.name}: task.json is missing {', '.join(missing)}")
    try:
        timeout = float(meta["timeout"])
    except (TypeError, ValueError):
        raise ValueError(f"{root.name}: timeout must be a number") from None
    if timeout <= 0:
        raise ValueError(f"{root.name}: timeout must be positive")
    raw_difficulty = meta.get("difficulty", 0)
    try:
        difficulty = int(raw_difficulty or 0)
    except (TypeError, ValueError):
        difficulty = 0
    if difficulty < 0 or difficulty > 5:
        raise ValueError(f"{root.name}: difficulty must be 0-5")
    task = Task(
        id=str(meta["id"]),
        title=str(meta["title"]),
        prompt=str(meta["prompt"]),
        timeout=timeout,
        root=root,
        difficulty=difficulty,
    )
    if not task.start.is_dir():
        raise ValueError(f"{root.name}: missing start/")
    if not task.test_files:
        raise ValueError(f"{root.name}: hidden_tests/ has no .py files")
    return task


def discover(tasks_dir: Path) -> list[Task]:
    """Every `task.json` one level below `tasks_dir`, sorted by id."""
    root = Path(tasks_dir)
    if not root.is_dir():
        raise ValueError(f"no such tasks directory: {root}")
    tasks = [load_task(p) for p in sorted(root.iterdir()) if (p / "task.json").is_file()]
    if not tasks:
        raise ValueError(f"no task.json found under {root}")
    seen: set[str] = set()
    for task in tasks:
        if task.id in seen:
            raise ValueError(f"duplicate task id: {task.id}")
        seen.add(task.id)
    return sorted(tasks, key=lambda t: t.id)


def score(*, tests_ok: bool, timed_out: bool, status: str, changed: bool) -> str:
    """One verdict per attempt.

    The hidden tests are the only positive signal, and they outrank the
    process: an agent that exits non-zero (or gets killed a second too early)
    but leaves a tree that passes did the work. A red tree is then explained
    by the most useful available cause, in that order: timeout, a failed run,
    no change at all, or changed-but-wrong.
    """
    if tests_ok:
        return SOLVE
    if timed_out:
        return TIMEOUT
    if status != "completed":
        return AGENT_ERROR
    if not changed:
        return NO_CHANGE
    return TESTS_RED


# ---- the working tree --------------------------------------------------


def fingerprint(root: Path) -> dict[str, str]:
    """Hash every file that counts as output, keyed by relative path."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in filenames:
            path = Path(dirpath) / name
            try:
                rel = str(path.relative_to(root)).replace("\\", "/")
                out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            except OSError:
                continue
    return out


def _materialize(task: Task, workdir: Path) -> None:
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    shutil.copytree(task.start, workdir, dirs_exist_ok=True)


def run_pytest(workdir: Path, checks: Path, timeout: int = TEST_TIMEOUT) -> dict:
    """Run only the hidden tests, from a directory the agent never saw.

    Secret: run by path, not by discovery, so a test file the agent wrote can
    never be counted, and its `conftest.py`/`pytest.ini` cannot rewrite the
    hidden tests' collection. `--confcutdir` keeps `conftest.py` above the
    checks directory from loading.
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        f"--confcutdir={checks}",
        str(checks),
    ]
    return verify.run(cmd, workdir, timeout=timeout)


# ---- one attempt -------------------------------------------------------


class RunnerClient:
    """The real seam over `runner.RUNNER`, kept small so tests can replace it."""

    def __init__(self, route: str) -> None:
        self.route = route

    def start(self, task: Task, workdir: Path) -> dict:
        row = db.create_task(
            goal=task.prompt,
            acceptance="完成后由你看不到的隐藏测试判定，按任务说明实现即可",
            title=task.title,
        )
        return runner.RUNNER.start(
            row["id"],
            route_name=self.route,
            cwd=str(workdir),
            verify_run=False,
        )

    def wait(self, run_id: str, timeout: float) -> dict:
        return runner.RUNNER.wait(run_id, timeout=int(timeout))

    def stop(self, run_id: str) -> bool:
        return runner.RUNNER.stop(run_id)


def _usage(run: dict) -> tuple[float | None, str]:
    try:
        usage = json.loads(run.get("usage_json") or "{}")
    except ValueError:
        return None, "CNY"
    currency = str(usage.get("currency") or "CNY").upper()
    cost = usage.get("cost")
    try:
        return (float(cost) if cost is not None else None), currency
    except (TypeError, ValueError):
        return None, currency


def execute(
    task: Task,
    route: str,
    workdir: Path,
    *,
    client=None,
    test_runner: Callable[..., dict] = run_pytest,
    timeout: float | None = None,
) -> dict:
    """Start one run, wait for it, then let the hidden tests decide."""
    workdir = Path(workdir)
    _materialize(task, workdir)
    before = fingerprint(workdir)
    client = client or RunnerClient(route)
    limit = float(timeout or task.timeout)

    started = time.time()
    run = client.start(task, workdir)
    run_id = str(run.get("id") or "")
    finished = client.wait(run_id, limit)
    timed_out = finished.get("status") not in ("completed", "failed")
    if timed_out:
        client.stop(run_id)
        finished = client.wait(run_id, 10.0)

    wall = time.time() - started
    try:
        measured = float(finished.get("ended_at") or 0) - float(finished.get("started_at") or 0)
    except (TypeError, ValueError):
        measured = 0.0
    duration = measured if measured > 0 else wall

    changed = fingerprint(workdir) != before
    checks = workdir / CHECK_DIR
    if checks.exists():
        shutil.rmtree(checks)
    shutil.copytree(task.hidden, checks)
    tests = test_runner(workdir, checks, timeout=TEST_TIMEOUT)
    tests_ok = bool(tests.get("ok"))
    verdict = score(
        tests_ok=tests_ok,
        timed_out=timed_out,
        status=str(finished.get("status") or ""),
        changed=changed,
    )
    cost, currency = _usage(finished)
    return {
        "task": task.id,
        "title": task.title,
        "verdict": verdict,
        "run_id": run_id,
        "run_status": str(finished.get("status") or ""),
        "error": str(finished.get("error") or ""),
        "duration_s": round(duration, 1),
        "cost": cost,
        "currency": currency,
        "changed": changed,
        "tests_ok": tests_ok,
        "pytest_exit": tests.get("exit"),
        "pytest_tail": (tests.get("output") or "")[-1200:],
        "workdir": str(workdir),
    }


# ---- reporting ---------------------------------------------------------


def _short(text: str, width: int = 64) -> str:
    flat = re.sub(r"\s+", " ", (text or "").strip())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _money(row: dict) -> str:
    cost = row.get("cost")
    if not isinstance(cost, (int, float)):
        return "—"
    currency = str(row.get("currency") or "CNY").upper()
    return f"¥{cost:.3f}" if currency == "CNY" else f"{currency} {cost:.3f}"


def _detail(row: dict) -> str:
    verdict = row.get("verdict")
    if verdict == SOLVE:
        return ""
    if verdict == TIMEOUT:
        return "run exceeded the task timeout"
    if verdict == NO_CHANGE:
        return "no file changed"
    if verdict == AGENT_ERROR:
        return _short(row.get("error") or "the run did not finish")
    for line in reversed((row.get("pytest_tail") or "").splitlines()):
        if " failed" in line or " error" in line:
            return _short(line.strip())
    return f"pytest exit {row.get('pytest_exit')}"


def format_table(rows: list[dict]) -> str:
    headers = ("task", "verdict", "time", "cost", "detail")
    cells = [
        (
            row.get("task", "?"),
            row.get("verdict", "?"),
            f"{float(row.get('duration_s') or 0):.1f}s",
            _money(row),
            _detail(row),
        )
        for row in rows
    ]
    widths = [len(h) for h in headers]
    for line in cells:
        widths = [max(w, len(cell)) for w, cell in zip(widths, line)]
    out = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    out.append("  ".join("-" * w for w in widths))
    out.extend("  ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip() for line in cells)
    return "\n".join(out)


def summarize(rows: list[dict]) -> dict:
    attempts = len(rows)
    solved = sum(1 for row in rows if row.get("verdict") == SOLVE)
    seconds = sum(float(row.get("duration_s") or 0) for row in rows)
    cost = sum(
        float(row.get("cost") or 0.0)
        for row in rows
        if isinstance(row.get("cost"), (int, float))
        and str(row.get("currency") or "CNY").upper() == "CNY"
    )
    return {
        "attempts": attempts,
        "solved": solved,
        "rate": round(solved / attempts, 4) if attempts else 0.0,
        "seconds": round(seconds, 1),
        "mean_s": round(seconds / attempts, 1) if attempts else 0.0,
        "cost_cny": round(cost, 4),
    }


def format_summary(totals: dict) -> str:
    attempts = int(totals.get("attempts") or 0)
    solved = int(totals.get("solved") or 0)
    percent = round(100.0 * solved / attempts) if attempts else 0
    return (
        f"solve {solved}/{attempts} ({percent}%) · "
        f"平均 {float(totals.get('mean_s') or 0):.0f}s · "
        f"¥{float(totals.get('cost_cny') or 0):.2f}"
    )


def report(rows: list[dict]) -> str:
    return format_table(rows) + "\n\n" + format_summary(summarize(rows))


# ---- orchestration -----------------------------------------------------


def scratch_dir() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = Path(tempfile.gettempdir()) / "nodus-bench" / stamp
    suffix = 1
    while base.exists():
        suffix += 1
        base = base.parent / f"{stamp}-{suffix}"
    base.mkdir(parents=True)
    return base


def run(
    tasks: list[Task],
    route: str,
    *,
    repeat: int = 1,
    base_dir: Path | None = None,
    keep: bool = False,
    out: Path | None = None,
    progress: Callable[[str], None] | None = None,
    client_factory: Callable[[str], object] | None = None,
    test_runner: Callable[..., dict] = run_pytest,
) -> dict:
    """Run every task `repeat` times and write the evidence.

    `base_dir` is the scratch root; each attempt owns `<base>/work/<task>-<n>`.
    Without `keep` the work trees are deleted afterwards, but `results.json`
    stays: the numbers are the artifact, the trees are only for debugging.
    """
    say = progress or (lambda _message: None)
    base = Path(base_dir) if base_dir else scratch_dir()
    base.mkdir(parents=True, exist_ok=True)
    repeat = max(1, int(repeat))
    rows: list[dict] = []
    total = len(tasks) * repeat
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    for task in tasks:
        for attempt in range(1, repeat + 1):
            index = len(rows) + 1
            say(f"[{index}/{total}] {task.id} #{attempt}")
            workdir = base / "work" / f"{task.id}-{attempt}"
            try:
                row = execute(
                    task,
                    route,
                    workdir,
                    client=client_factory(route) if client_factory else None,
                    test_runner=test_runner,
                )
            except Exception as exc:  # noqa: BLE001 - one broken task must not hide the rest
                row = {
                    "task": task.id,
                    "title": task.title,
            "difficulty": task.difficulty,
                    "verdict": AGENT_ERROR,
                    "run_id": "",
                    "run_status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "duration_s": 0.0,
                    "cost": None,
                    "currency": "CNY",
                    "changed": False,
                    "tests_ok": False,
                    "pytest_exit": None,
                    "pytest_tail": "",
                    "workdir": str(workdir),
                }
            row["attempt"] = attempt
            rows.append(row)
            say(f"    {row['verdict']}  {row['duration_s']}s  {_money(row)}")

    totals = summarize(rows)
    payload = {
        "version": 1,
        "route": route,
        "repeat": repeat,
        "started_at": started_at,
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "base_dir": str(base),
        "totals": totals,
        "attempts": rows,
        "report": report(rows),
    }
    target = Path(out) if out else base / "results.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    payload["out"] = str(target)

    if not keep:
        shutil.rmtree(base / "work", ignore_errors=True)
    return payload
