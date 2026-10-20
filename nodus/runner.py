"""Run lifecycle: build context, spawn a CLI worker, persist its event stream."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from . import adapters, agents, config, db, meter

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".obsidian",
    ".pytest_cache", "dist", "build", ".next", ".cache",
}

# A child that stops writing for this long is treated as hung, not busy.
IDLE_TIMEOUT = float(os.environ.get("NODUS_IDLE_TIMEOUT", "900"))


class EventBus:
    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


BUS = EventBus()


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()[:16]


def build_prompt(
    task: dict,
    handoff: str,
    agent: str,
    cwd: str,
    note: str = "",
    include_handoff: bool = True,
) -> str:
    extra = f"\n## 本次指令\n{note}\n" if note else ""
    previous = (
        f"\n## 上一位留下的交接\n{handoff or '无，你是第一个执行者。'}\n"
        if include_handoff
        else ""
    )
    return f"""你是工作台里负责执行任务的 agent（{agent}）。

## 任务
{task.get('goal', '')}

## 验收标准
{task.get('acceptance') or '未提供，按任务目标自行判断'}
{extra}{previous}
## 工作目录
{cwd}

## 规则
- 只做完成任务必需的事，不要顺手重构成无关代码。
- 结束时用三句话说明：做了什么、产出了什么、下一位该接什么。
- 如果无法完成，直接说明卡在哪里，不要假装完成。
"""


def recent_handoff(task_id: str) -> str:
    h = db.latest_handoff(task_id)
    if not h:
        return ""
    try:
        return Path(h["path"]).read_text(encoding="utf-8")
    except OSError:
        return ""


def touched_files(cwd: Path, since: float, limit: int = 50) -> list[Path]:
    out: list[Path] = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".git")]
        for name in files:
            p = Path(root) / name
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime >= since:
                out.append(p)
                if len(out) >= limit:
                    return out
    return out


class Runner:
    def __init__(self) -> None:
        self.procs: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()

    def start(
        self,
        task_id: str,
        agent_name: str,
        cwd: str | None = None,
        parent_run_id: str | None = None,
        note: str = "",
        effort: str = "",
        include_handoff: bool = True,
        session_id: str = "",
    ) -> dict:
        specs = agents.registry()
        spec = specs.get(agent_name)
        if spec is None:
            raise ValueError(f"unknown agent: {agent_name}")
        task = db.get_task(task_id)
        if not task:
            raise ValueError(f"unknown task: {task_id}")
        secrets = config.load_secrets()
        missing = agents.missing_requirements(spec, secrets)
        if missing:
            raise RuntimeError(f"{agent_name} not ready: {', '.join(missing)}")

        workdir = Path(cwd or Path.cwd()).resolve()
        run = db.create_run(task_id, agent_name, str(workdir), parent_run_id, note, effort)
        rid = run["id"]
        thread = threading.Thread(
            target=self._run,
            args=(rid, spec, task, workdir, secrets, include_handoff, session_id),
            daemon=True,
        )
        thread.start()
        return db.get_run(rid)

    def _run(
        self,
        rid: str,
        spec,
        task: dict,
        workdir: Path,
        secrets: dict,
        include_handoff: bool = True,
        session_id: str = "",
    ) -> None:
        try:
            self._run_inner(rid, spec, task, workdir, secrets, include_handoff, session_id)
        except Exception as exc:  # never leave a run stuck in "running"
            import traceback

            message = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
            try:
                seq = db.next_seq(rid)
                db.append_event(rid, seq, "run.failed", {"error": message, "status": "failed"})
            except Exception:
                pass
            db.update_run(rid, status="failed", ended_at=time.time(), error=message)

    def _run_inner(
        self,
        rid: str,
        spec,
        task: dict,
        workdir: Path,
        secrets: dict,
        include_handoff: bool = True,
        session_id: str = "",
    ) -> None:
        run_dir = config.HOME / "runs" / rid
        run_dir.mkdir(parents=True, exist_ok=True)
        raw_path = run_dir / "raw.jsonl"
        note = db.get_run(rid).get("note") or ""
        if session_id:
            # Resuming: the session already carries its own history, so send the
            # instruction verbatim instead of wrapping it in a fresh task brief.
            prompt = note
        else:
            prompt = build_prompt(
                task,
                recent_handoff(task["id"]),
                spec.name,
                str(workdir),
                note,
                include_handoff=include_handoff,
            )
        (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

        seq = 0
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0}
        texts: list[str] = []
        session_ref: dict = {}
        error_text = ""

        def emit(etype: str, payload: dict) -> None:
            nonlocal seq
            seq = db.next_seq(rid)
            ev = db.append_event(rid, seq, etype, payload)
            BUS.publish({"run_id": rid, **{k: ev[k] for k in ("seq", "type", "payload", "ts")}})

        emit(
            "run.started",
            {
                "agent": spec.name,
                "cwd": str(workdir),
                "task": task["id"],
                **({"session_id": session_id} if session_id else {}),
            },
        )
        started_at = time.time()
        db.update_run(rid, status="running", started_at=started_at)

        env = agents.build_env(spec, secrets)
        effort = (db.get_run(rid).get("effort") or "").strip()
        # Take the meter reading before the process exists, so this run's spend
        # cannot already be inside the window.
        meter_before = meter.read(spec)
        creation = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                agents.argv(spec, effort or None, session_id),
                cwd=str(workdir),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation,
            )
        except OSError as exc:
            emit("run.failed", {"error": str(exc)})
            db.update_run(rid, status="failed", ended_at=time.time(), error=str(exc))
            return

        with self.lock:
            self.procs[rid] = proc

        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass

        with raw_path.open("w", encoding="utf-8") as raw:
            assert proc.stdout is not None
            incoming: "queue.Queue[str | None]" = queue.Queue()

            def reader() -> None:
                try:
                    for line in proc.stdout:  # type: ignore[union-attr]
                        incoming.put(line)
                finally:
                    incoming.put(None)

            threading.Thread(target=reader, daemon=True).start()
            exited_at: float | None = None
            last_activity = time.time()
            while True:
                try:
                    line = incoming.get(timeout=0.5)
                except queue.Empty:
                    # A child process can hold the stdout pipe open after the
                    # CLI itself exits; do not wait on EOF forever.
                    if proc.poll() is not None:
                        exited_at = exited_at or time.time()
                        if time.time() - exited_at > 2.0:
                            break
                    # A live child that stops writing is hung on something
                    # (API stall, dead MCP server). No output is not progress.
                    elif time.time() - last_activity > IDLE_TIMEOUT:
                        error_text = f"no output for {int(IDLE_TIMEOUT)}s, killed"
                        emit("run.failed", {"error": error_text})
                        self._kill_tree(proc)
                        break
                    continue
                if line is None:
                    break
                last_activity = time.time()
                raw.write(line if line.endswith("\n") else line + "\n")
                raw.flush()
                for ev in adapters.parse(spec.vendor, line):
                    kind = ev.pop("type")
                    if kind == "usage":
                        for k in usage:
                            usage[k] += int(ev.get(k) or 0)
                        if ev.get("cost"):
                            usage["cost"] = usage.get("cost", 0) + float(ev["cost"])
                        continue
                    if kind == "session":
                        session_ref = {
                            "provider": spec.vendor,
                            "session_id": ev.get("session_id"),
                            "cwd": str(workdir),
                        }
                    if kind == "message" and ev.get("text"):
                        if ev.get("final") and texts and ev["text"] == texts[-1]:
                            continue  # the CLI repeats its final text
                        texts.append(ev["text"])
                    if kind == "error":
                        error_text = ev.get("message", "")
                    emit(kind, ev)

        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._kill_tree(proc)
            code = proc.wait(timeout=5)
        with self.lock:
            self.procs.pop(rid, None)

        final_text = texts[-1] if texts else ""
        (run_dir / "final.md").write_text(final_text, encoding="utf-8")
        status = "completed" if code == 0 and not error_text else "failed"
        err = error_text or ("" if code == 0 else f"exit code {code}")

        # Cost, in order of trustworthiness:
        #   metered    what the provider actually deducted (starts after exit)
        #   list_price tokens x published price, exact when billing is at list
        #   reported   the CLI's local price book, which is often another
        #              vendor's rates anyway (Claude Code prices MiniMax with
        #              Anthropic's prices, and adds no relay multiplier)
        meter_after = meter.read(spec, settle_from=meter_before)
        metered = meter.delta(meter_before, meter_after)
        if metered is not None and meter_before is not None:
            usage["cost"] = round(metered * config.cost_rate(meter_before.unit), 6)
            usage["currency"] = "CNY"
            usage["cost_basis"] = "metered"
            usage["meter"] = {
                "unit": meter_before.unit,
                "delta": round(metered, 6),
                "window_s": round(time.time() - started_at, 1),
            }
        elif spec.pricing is not None:
            usage["cost"] = spec.pricing.cost(usage)
            usage["currency"] = spec.pricing.currency
            usage["cost_basis"] = "list_price"
        elif isinstance(usage.get("cost"), (int, float)):
            usage["currency"] = "USD"
            usage["cost_basis"] = "reported"

        files = touched_files(workdir, started_at)
        artifacts = [str(p) for p in files]
        for p in files:
            try:
                rel = str(p.relative_to(workdir))
            except ValueError:
                rel = ""
            db.add_artifact(rid, task["id"], "file", str(p), _digest(p), rel_path=rel)

        handoff_path = run_dir / "handoff.md"
        verify_mark = db.verify_states([rid]).get(rid, "none")
        handoff = build_handoff(
            task, rid, spec.name, status, final_text, files, err, verify_mark
        )
        handoff_path.write_text(handoff, encoding="utf-8")
        db.add_handoff(task["id"], rid, str(handoff_path), _digest(handoff_path))
        db.add_artifact(rid, task["id"], "handoff", str(handoff_path), _digest(handoff_path), "handoff")

        db.update_run(
            rid,
            status=status,
            ended_at=time.time(),
            usage_json=json.dumps(usage),
            session_ref_json=json.dumps(session_ref),
            error=err or None,
        )
        emit(
            "run.completed" if status == "completed" else "run.failed",
            {
                "status": status,
                "exit_code": code,
                "usage": usage,
                "artifacts": artifacts,
                "handoff": str(handoff_path),
                "error": err,
            },
        )

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                )
            else:
                proc.terminate()
        except OSError:
            pass

    def stop(self, rid: str) -> bool:
        with self.lock:
            proc = self.procs.get(rid)
        if not proc:
            return False
        self._kill_tree(proc)
        return True

    def wait(self, rid: str, timeout: int = 1800, poll: float = 1.0) -> dict:
        """Block until the run leaves queued/running, or the timeout fires."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            run = db.get_run(rid)
            if run.get("status") in ("completed", "failed"):
                return run
            time.sleep(poll)
        return db.get_run(rid)

    def reconcile(self, stale_seconds: int = 900) -> list[dict]:
        """Fail runs whose owner process died while they were still running."""
        out: list[dict] = []
        for run in db.stale_runs(stale_seconds):
            rid = run["id"]
            with self.lock:
                proc = self.procs.pop(rid, None)
            if proc is not None:
                self._kill_tree(proc)
            message = f"orphaned: no activity for {stale_seconds}s"
            try:
                db.append_event(
                    rid, db.next_seq(rid), "run.failed", {"error": message, "status": "failed"}
                )
            except Exception:
                pass
            db.update_run(rid, status="failed", ended_at=time.time(), error=message)
            out.append({**run, "error": message})
        return out


VERIFY_TEXT = {
    "ok": "测试通过（verify.green）",
    "red": "测试跑过但失败（verify.red）",
    "none": "没有跑测试 —— 上面的状态只代表进程退出码",
}


def build_handoff(
    task: dict,
    rid: str,
    agent: str,
    status: str,
    final_text: str,
    files: list[Path],
    error: str,
    verify_mark: str = "none",
) -> str:
    listed = "\n".join(f"- {p}" for p in files) or "- （无改动）"
    unverified = (
        "- 本轮没有验证记录：结论只来自 agent 自述，下一位拿到可执行的验收命令后先跑一遍。\n"
        if verify_mark == "none"
        else ""
    )
    return f"""# Handoff — {task.get('title', '')}

- 任务：{task['id']}
- 执行：{rid}（{agent}）
- 状态：{status}
- 验证：{VERIFY_TEXT.get(verify_mark, verify_mark)}
- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}

## 目标

{task.get('goal', '')}

## 验收标准

{task.get('acceptance') or '（未提供）'}

## 本次结论

{final_text or '（无输出）'}

## 改动文件

{listed}

## 错误

{error or '（无）'}

## 未决

{unverified}- （人工补充）
"""


RUNNER = Runner()
