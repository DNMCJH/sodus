"""Daemon-side jobs: pipelines that outlive the terminal that started them.

`nodus tdd/review/fix` used to run inside the CLI process, which meant closing
the window (or Ctrl-C) killed a half-finished pipeline, and `nodus stop` could
not reach a run owned by somebody else's terminal. A pipeline is a sequence of
runs plus verification steps; the daemon already owns runs, so it may as well
own the sequence.

The job row is the durable part; the thread is not. A daemon restart therefore
marks any `running` job failed on startup, exactly like orphaned runs — the
honest outcome, not a resurrection.
"""

from __future__ import annotations

import json
import threading
import traceback

from . import db, fix, review, tdd

#: The pipelines the daemon knows how to run. A closed list on purpose: this
#: endpoint starts agents, so "anything importable" is not an acceptable answer.
KINDS = ("tdd", "review", "fix")


def _emit(pid: str, etype: str, payload: dict) -> None:
    try:
        db.append_event(pid, db.next_seq(pid), etype, payload)
    except Exception:  # noqa: BLE001 - progress must never kill the job
        pass


class Jobs:
    """One thread per pipeline, keyed by its row id."""

    def __init__(self) -> None:
        self._threads: dict[str, threading.Thread] = {}
        self._current_run: dict[str, str] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    # ---- entry points --------------------------------------------------

    def submit(self, kind: str, params: dict, label: str = "") -> dict:
        if kind not in KINDS:
            raise ValueError(f"unknown pipeline: {kind} (known: {', '.join(KINDS)})")
        row = db.create_pipeline(kind, params, label)
        pid = row["id"]
        thread = threading.Thread(target=self._run, args=(pid, kind, params), daemon=True)
        with self._lock:
            self._threads[pid] = thread
        thread.start()
        return db.get_pipeline(pid)

    def stop(self, pid: str) -> bool:
        with self._lock:
            self._cancelled.add(pid)
            current = self._current_run.get(pid)
        if current:
            from . import runner

            runner.RUNNER.stop(current)
        return True

    def active(self) -> list[str]:
        with self._lock:
            return [pid for pid, t in self._threads.items() if t.is_alive()]

    # ---- execution -----------------------------------------------------

    def _on_run(self, pid: str) -> None:
        def announce(run_id: str) -> None:
            with self._lock:
                self._current_run[pid] = run_id
            _emit(pid, "pipeline.child", {"run_id": run_id})
            if pid in self._cancelled:
                from . import runner

                runner.RUNNER.stop(run_id)

        return announce

    def _run(self, pid: str, kind: str, params: dict) -> None:
        db.update_pipeline(pid, status="running", started_at=db.now())
        _emit(pid, "pipeline.started", {"kind": kind})
        on_run = self._on_run(pid)
        try:
            if kind == "tdd":
                result = tdd.run_tdd(**{**params, "on_run": on_run})
            elif kind == "review":
                result = review.run_review(**{**params, "on_run": on_run})
            elif kind == "fix":
                result = fix.run_fix(**{**params, "on_run": on_run})
            else:
                raise ValueError(f"unknown pipeline: {kind}")
            with self._lock:
                cancelled = pid in self._cancelled
            status = "stopped" if cancelled else ("failed" if result.get("ok") is False else "completed")
            if kind == "review" and result.get("ok") is False:
                status = "failed"
            db.update_pipeline(
                pid,
                status=status,
                ended_at=db.now(),
                result_json=json.dumps(result, ensure_ascii=False, default=str),
                error=(result.get("error") or None) if status == "failed" else None,
            )
            _emit(pid, "pipeline.finished", {"status": status, "ok": bool(result.get("ok"))})
        except Exception as exc:  # noqa: BLE001 - a job must always close
            traceback.print_exc()
            db.update_pipeline(
                pid,
                status="failed",
                ended_at=db.now(),
                error=f"{type(exc).__name__}: {exc}",
            )
            _emit(pid, "pipeline.failed", {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            with self._lock:
                self._threads.pop(pid, None)
                self._current_run.pop(pid, None)
                self._cancelled.discard(pid)


JOBS = Jobs()


def reap(rows: list[dict]) -> list[dict]:
    """Close pipelines a dead daemon left running."""
    out = []
    for row in rows:
        db.update_pipeline(
            row["id"],
            status="failed",
            ended_at=db.now(),
            error="orphaned: the daemon restarted while this pipeline was running",
        )
        _emit(
            row["id"],
            "pipeline.failed",
            {"error": "daemon restarted; the pipeline did not survive"},
        )
        out.append(row)
    return out
