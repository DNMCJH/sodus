"""Run lifecycle: build context, spawn a CLI worker, persist its event stream.

A run is a sequence of attempts. Each attempt is one (agent, provider) pair from
the route. The fallback rule is the important part and it is deliberately
narrow:

    fall through only when the attempt provably did nothing

"Did nothing" means no tool call was ever parsed out of the stream. A worker
that got sixty seconds into an edit and then lost its connection is not a
provider outage — rerunning it on another endpoint would apply the same edits
twice. That is the one failure mode a fallback chain must never introduce, so
it is the first condition checked.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

from . import adapters, agents, config, db, meter, profiles, providers, routes, verify

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".obsidian",
    ".pytest_cache", "dist", "build", ".next", ".cache",
}

#: A child that stops writing for this long is treated as hung, not busy.
IDLE_TIMEOUT = float(os.environ.get("NODUS_IDLE_TIMEOUT", "900"))

#: A run that changed files runs the project's tests before it closes. The
#: detector only knows standard commands (pytest / npm test / cargo / go), and
#: an unknown project is left alone: running arbitrary test suites in a tree
#: nobody vetted is how an acceptance layer destroys work.
AUTO_VERIFY = os.environ.get("NODUS_AUTO_VERIFY", "1") != "0"
VERIFY_TIMEOUT = float(os.environ.get("NODUS_VERIFY_TIMEOUT", "600"))

#: Text that marks a failure as belonging to the endpoint rather than the task.
#: Only consulted when the attempt produced no tool call, so a false positive
#: costs one retry of something that had not started, never duplicated work.
UPSTREAM_SIGNALS = re.compile(
    r"401|403|429|5\d\d|unauthoriz|forbidden|invalid api key|authenticat|"
    r"rate.?limit|quota|insufficient|overloaded|connection|reset by peer|"
    r"econnreset|econnrefused|etimedout|timed? out|socket|tls|dns|"
    r"no output for|api error|upstream",
    re.IGNORECASE,
)


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
    return f"""你是 Nodus 里负责执行任务的 agent（{agent}）。

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


def looks_upstream(attempt: dict) -> bool:
    """Is this failure the endpoint's fault rather than the task's?"""
    if attempt.get("tool_calls"):
        return False
    text = " ".join(list(attempt.get("errors") or []) + list(attempt.get("noise") or []))
    if text and UPSTREAM_SIGNALS.search(text):
        return True
    # A process that produced nothing at all and exited non-zero never got far
    # enough to act; treating it as an endpoint failure is safe.
    if attempt.get("exit_code") not in (0, None) and not attempt.get("output_lines"):
        return True
    return False


def should_fall_through(attempt: dict, nxt: str | None) -> bool:
    """The one rule that keeps a fallback chain from doing work twice.

    Every condition has to hold, and the tool-call check is first among the
    reasons: once a worker has acted, a second endpoint would replay those
    actions on top of the first one's side effects.
    """
    if not nxt:
        return False
    if attempt.get("ok"):
        return False
    return looks_upstream(attempt)


class Runner:
    def __init__(self) -> None:
        self.procs: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()

    # ---- entry point ---------------------------------------------------

    def start(
        self,
        task_id: str,
        route_name: str = "claude",
        cwd: str | None = None,
        parent_run_id: str | None = None,
        note: str = "",
        effort: str = "",
        model: str = "",
        include_handoff: bool = True,
        session_id: str = "",
        session_mode: str = "resume",
        profile: str = "",
        provider: str = "",
        wait: float = 0.0,
        verify_run: bool | None = None,
    ) -> dict:
        profiles.ensure_ready()
        if verify_run is None:
            verify_run = AUTO_VERIFY and config.auto_verify()
        route = routes.resolve_for_run(route_name)
        if provider:
            # Continuing a session means going back to the endpoint that holds
            # its history: `--resume` looks the id up inside that endpoint's own
            # config home, so here the chain is not a preference, it is a fact.
            route = routes.Route(
                name=route.name,
                agent=route.agent,
                providers=(provider,),
                model=route.model,
                effort=route.effort,
            )
        gap = routes.problems(route)
        if gap:
            raise RuntimeError(f"route {route.name} is not usable: {', '.join(gap)}")
        spec = agents.get(route.agent)
        assert spec is not None  # routes.problems already checked
        task = db.get_task(task_id)
        if not task:
            raise ValueError(f"unknown task: {task_id}")

        secrets = config.load_secrets()
        table = providers.load()
        ready = [
            name
            for name in route.providers
            if not agents.missing(spec, table[name], secrets)
        ]
        if not ready:
            first = table[route.primary]
            raise RuntimeError(
                f"{route.name} not ready: {', '.join(agents.missing(spec, first, secrets))}"
            )

        profile_name = profile or config.active_profile()
        workdir = Path(cwd or Path.cwd()).resolve()
        prof = profiles.read(profile_name)
        run = self._claim(
            task, spec, workdir, parent_run_id, note,
            effort or route.effort or prof.effort or "",
            model or route.model or prof.model or "",
            route, profile_name, wait,
        )
        thread = threading.Thread(
            target=self._execute,
            args=(run["id"], route, spec, task, workdir, secrets, profile_name,
                  include_handoff, session_id, session_mode, verify_run),
            daemon=True,
        )
        thread.start()
        return db.get_run(run["id"])

    def _claim(
        self,
        task: dict,
        spec: agents.AgentSpec,
        workdir: Path,
        parent_run_id: str | None,
        note: str,
        effort: str,
        model: str,
        route: routes.Route,
        profile_name: str,
        wait: float,
    ) -> dict:
        """Create the run and take the write lock on its directory.

        Two agents editing one tree is the failure this exists to prevent: the
        second one's edits land on top of a half-finished first one, and both
        handoffs describe a state that never existed. Refusing is cheap;
        untangling is not.
        """
        deadline = time.time() + max(0.0, wait)
        while True:
            try:
                return db.create_run(
                    task["id"],
                    spec.name,
                    str(workdir),
                    parent_run_id,
                    note,
                    effort,
                    model,
                    route=route.name,
                    provider=route.primary,
                    profile=profile_name,
                    lock=True,
                )
            except db.Busy as busy:
                # create_run already cleared a lock whose run had finished, so
                # anything left here has a live owner.
                if time.time() >= deadline:
                    raise RuntimeError(str(busy)) from None
                time.sleep(1.0)

    # ---- attempt loop --------------------------------------------------

    def _execute(
        self,
        rid: str,
        route: routes.Route,
        spec: agents.AgentSpec,
        task: dict,
        workdir: Path,
        secrets: dict[str, str],
        profile_name: str,
        include_handoff: bool,
        session_id: str,
        session_mode: str,
        verify_run: bool,
    ) -> None:
        try:
            self._execute_inner(
                rid, route, spec, task, workdir, secrets, profile_name,
                include_handoff, session_id, session_mode, verify_run,
            )
        except Exception as exc:  # never leave a run stuck in "running"
            import traceback

            traceback.print_exc()
            self._fail(rid, f"{type(exc).__name__}: {exc}")

    def _execute_inner(
        self,
        rid: str,
        route: routes.Route,
        spec: agents.AgentSpec,
        task: dict,
        workdir: Path,
        secrets: dict[str, str],
        profile_name: str,
        include_handoff: bool,
        session_id: str,
        session_mode: str,
        verify_run: bool,
    ) -> None:
        run_dir = config.HOME / "runs" / rid
        run_dir.mkdir(parents=True, exist_ok=True)
        record = db.get_run(rid)
        note = record.get("note") or ""
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

        pinned = (record.get("model") or "").strip()
        effort = (record.get("effort") or "").strip()
        self._emit(
            rid,
            "run.started",
            {
                "agent": spec.name,
                "route": route.name,
                "providers": list(route.providers),
                "profile": profile_name,
                "cwd": str(workdir),
                "task": task["id"],
                **({"session_id": session_id} if session_id else {}),
            },
        )
        db.update_run(rid, status="running", started_at=time.time())

        table = providers.load()
        attempts: list[dict] = []
        chosen: dict = {}
        first_started = None
        for index, provider_name in enumerate(route.providers):
            provider = table.get(provider_name)
            if provider is None:
                continue
            if agents.missing(spec, provider, secrets):
                continue  # a broken fallback must not stop the chain
            model = (pinned if index == 0 else "") or provider.model
            attempt = self._attempt(
                rid, index, spec, provider, workdir, secrets, profile_name,
                prompt, effort, model, session_id, session_mode,
            )
            if first_started is None:
                first_started = attempt["started_at"]
            attempts.append(attempt)
            chosen = attempt
            db.update_run(rid, provider=provider.name, model=model or "")
            if not attempt["ok"]:
                nxt = self._next(route, index + 1, table, spec, secrets)
                if should_fall_through(attempt, nxt):
                    self._emit(
                        rid,
                        "run.fallback",
                        {
                            "from": provider.name,
                            "to": nxt,
                            "reason": attempt["error"] or "exit code",
                            "tool_calls": attempt["tool_calls"],
                        },
                    )
                    continue
            break

        if not attempts:
            self._fail(rid, "no provider on this route could be reached")
            return

        self._finalize(
            rid, route, spec, task, workdir, run_dir, attempts, chosen,
            first_started or time.time(), session_id, verify_run,
        )

    @staticmethod
    def _next(
        route: routes.Route,
        start: int,
        table: dict[str, providers.Provider],
        spec: agents.AgentSpec,
        secrets: dict[str, str],
    ) -> str | None:
        for name in route.providers[start:]:
            provider = table.get(name)
            if provider is not None and not agents.missing(spec, provider, secrets):
                return name
        return None

    # ---- one attempt ---------------------------------------------------

    def _attempt(
        self,
        rid: str,
        index: int,
        spec: agents.AgentSpec,
        provider: providers.Provider,
        workdir: Path,
        secrets: dict[str, str],
        profile_name: str,
        prompt: str,
        effort: str,
        model: str,
        session_id: str,
        session_mode: str,
    ) -> dict:
        attempt: dict = {
            "index": index,
            "provider": provider.name,
            "model": model or "",
            "started_at": time.time(),
            "ended_at": 0.0,
            "exit_code": None,
            "error": "",
            "errors": [],
            "ok": False,
            "tool_calls": 0,
            "output_lines": 0,
            "noise": [],
            "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0},
            "texts": [],
            "session_ref": {},
        }
        self._emit(
            rid,
            "run.attempt",
            {"provider": provider.name, "model": model, "attempt": index + 1},
        )
        try:
            home = profiles.materialize(spec, provider, secrets, profile_name)
            env = agents.build_env(spec, provider, secrets, home=home)
            argv = agents.argv(
                spec, provider, effort or None, session_id, model or None, session_mode
            )
        except (OSError, ValueError) as exc:
            attempt["error"] = f"{type(exc).__name__}: {exc}"
            attempt["errors"].append(attempt["error"])
            attempt["ended_at"] = time.time()
            return attempt

        before = meter.read(provider, secrets)
        raw_path = config.HOME / "runs" / rid / f"raw-{index + 1}.jsonl"
        creation = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                argv,
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
            attempt["error"] = f"spawn failed: {exc}"
            attempt["errors"].append(str(exc))
            attempt["ended_at"] = time.time()
            return attempt

        with self.lock:
            self.procs[rid] = proc
        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass

        self._pump(rid, proc, raw_path, spec, attempt)
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._kill_tree(proc)
            code = proc.wait(timeout=5)
        with self.lock:
            self.procs.pop(rid, None)

        attempt["session_ref"].setdefault("cwd", str(workdir))
        attempt["exit_code"] = code
        attempt["ended_at"] = time.time()
        attempt["ok"] = code == 0 and not attempt["error"]
        if not attempt["ok"] and not attempt["error"]:
            attempt["error"] = f"exit code {code}"
            attempt["errors"].append(attempt["error"])
        self._price(attempt, spec, provider, secrets, before)
        return attempt

    def _pump(
        self,
        rid: str,
        proc: subprocess.Popen,
        raw_path: Path,
        spec: agents.AgentSpec,
        attempt: dict,
    ) -> None:
        usage = attempt["usage"]
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
                    # A child can hold the stdout pipe open after the CLI exits;
                    # do not wait on EOF forever.
                    if proc.poll() is not None:
                        exited_at = exited_at or time.time()
                        if time.time() - exited_at > 2.0:
                            break
                    # A live child that stops writing is hung on something
                    # (API stall, dead MCP server). No output is not progress.
                    elif time.time() - last_activity > IDLE_TIMEOUT:
                        attempt["error"] = f"no output for {int(IDLE_TIMEOUT)}s, killed"
                        attempt["errors"].append(attempt["error"])
                        self._emit(rid, "run.failed", {"error": attempt["error"]})
                        self._kill_tree(proc)
                        break
                    continue
                if line is None:
                    break
                last_activity = time.time()
                attempt["output_lines"] += 1
                raw.write(line if line.endswith("\n") else line + "\n")
                raw.flush()
                events = adapters.parse(spec.vendor, line)
                if not events and line.strip():
                    # CLIs put connection and auth failures on stderr as plain
                    # text. Keeping the tail is what lets a fallback recognise
                    # an endpoint problem that never reached the JSON stream.
                    attempt["noise"].append(line.strip()[:300])
                    del attempt["noise"][:-20]
                for ev in events:
                    kind = ev.pop("type")
                    if kind == "usage":
                        for k in ("input_tokens", "output_tokens", "cache_read", "cache_write"):
                            usage[k] += int(ev.get(k) or 0)
                        if ev.get("cost"):
                            usage["cost"] = usage.get("cost", 0) + float(ev["cost"])
                        continue
                    if kind == "session":
                        attempt["session_ref"] = {
                            "provider": spec.vendor,
                            "session_id": ev.get("session_id"),
                            "cwd": "",
                        }
                    if kind == "message" and ev.get("text"):
                        if ev.get("final") and attempt["texts"] and ev["text"] == attempt["texts"][-1]:
                            continue  # the CLI repeats its final text
                        attempt["texts"].append(ev["text"])
                    if kind == "tool_call":
                        attempt["tool_calls"] += 1
                    if kind == "error":
                        message = ev.get("message", "")
                        attempt["error"] = message
                        if message and len(attempt["errors"]) < 5:
                            attempt["errors"].append(message)
                    self._emit(rid, kind, ev)

    def _price(
        self,
        attempt: dict,
        spec: agents.AgentSpec,
        provider: providers.Provider,
        secrets: dict[str, str],
        before,
    ) -> None:
        """Cost in order of trustworthiness, recorded per attempt.

        metered_request  a gateway read this session's log: exact, per call
        metered          what the provider deducted over the attempt window
        list_price       tokens x published price, exact at list billing
        reported         the CLI's own price book, often another vendor's rates
        """
        usage = attempt["usage"]
        gateway = None
        if spec.gateway_sessions:
            gateway = meter.request_cost(
                attempt["session_ref"].get("session_id") or "", settle=6.0
            )
        if gateway is not None:
            usage["cost"], requests = gateway
            usage["currency"] = config.gateway_currency()
            usage["cost_basis"] = "metered_request"
            usage["meter"] = {
                "unit": "gateway_request",
                "requests": requests,
                "window_s": round(attempt["ended_at"] - attempt["started_at"], 1),
            }
            return
        after = meter.read(provider, secrets, settle_from=before)
        metered = meter.delta(before, after)
        if metered is not None and before is not None:
            usage["cost"] = round(metered * config.cost_rate(before.unit), 6)
            usage["currency"] = "CNY"
            usage["cost_basis"] = "metered"
            usage["meter"] = {
                "unit": before.unit,
                "delta": round(metered, 6),
                "window_s": round(attempt["ended_at"] - attempt["started_at"], 1),
            }
        elif provider.pricing is not None:
            usage["cost"] = provider.pricing.cost(usage)
            usage["currency"] = provider.pricing.currency
            usage["cost_basis"] = "list_price"
        elif isinstance(usage.get("cost"), (int, float)):
            usage["currency"] = "USD"
            usage["cost_basis"] = "reported"

    # ---- finalisation --------------------------------------------------

    def _finalize(
        self,
        rid: str,
        route: routes.Route,
        spec: agents.AgentSpec,
        task: dict,
        workdir: Path,
        run_dir: Path,
        attempts: list[dict],
        chosen: dict,
        first_started: float,
        session_id: str,
        verify_after: bool = True,
    ) -> None:
        final_text = chosen["texts"][-1] if chosen["texts"] else ""
        (run_dir / "final.md").write_text(final_text, encoding="utf-8")
        status = "completed" if chosen["ok"] else "failed"
        err = "" if chosen["ok"] else (chosen["error"] or f"exit code {chosen['exit_code']}")

        usage = dict(chosen["usage"])
        usage["attempts"] = [
            {
                "provider": a["provider"],
                "model": a["model"],
                "status": "ok" if a["ok"] else "failed",
                "tool_calls": a["tool_calls"],
                "cost": a["usage"].get("cost"),
                "currency": a["usage"].get("currency"),
                "cost_basis": a["usage"].get("cost_basis"),
            }
            for a in attempts
        ]
        fallbacks = [
            a["provider"] for a in attempts[:-1] if a["provider"] != chosen["provider"]
        ]

        files = touched_files(workdir, first_started)
        artifacts = [str(p) for p in files]
        for p in files:
            try:
                rel = str(p.relative_to(workdir))
            except ValueError:
                rel = ""
            db.add_artifact(rid, task["id"], "file", str(p), _digest(p), rel_path=rel)

        handoff_path = run_dir / "handoff.md"
        verify_mark = db.verify_states([rid]).get(rid, "none")
        if verify_mark == "none" and verify_after and files:
            verify_mark = self._auto_verify(rid, task["id"], workdir)
        handoff = build_handoff(
            task, rid, spec.name, status, final_text, files, err, verify_mark,
            provider=chosen["provider"], route=route.name, fallbacks=fallbacks,
        )
        handoff_path.write_text(handoff, encoding="utf-8")
        db.add_handoff(task["id"], rid, str(handoff_path), _digest(handoff_path))
        db.add_artifact(rid, task["id"], "handoff", str(handoff_path), _digest(handoff_path), "handoff")

        # Free the directory before the status write: `wait()` returning
        # "finished" must mean the caller can start the next run here, and a
        # pipeline (tdd) does exactly that on the next line.
        db.release_write(rid)
        db.update_run(
            rid,
            status=status,
            ended_at=time.time(),
            usage_json=json.dumps(usage),
            session_ref_json=json.dumps(chosen["session_ref"]),
            error=err or None,
            provider=chosen["provider"],
        )
        self._emit(
            rid,
            "run.completed" if status == "completed" else "run.failed",
            {
                "status": status,
                "exit_code": chosen["exit_code"],
                "provider": chosen["provider"],
                "usage": usage,
                "artifacts": artifacts,
                "handoff": str(handoff_path),
                "error": err,
            },
        )

    def _auto_verify(self, rid: str, task_id: str, workdir: Path) -> str:
        """Run the project's own tests and bind the result to this run.

        `completed` only ever meant "the process exited 0". If the worker
        changed something and the project has a test command we recognise, the
        evidence is one command away and there is no reason to ship without it.
        """
        found = verify.detect(workdir)
        if not found:
            return "none"
        self._emit(
            rid,
            "verify.started",
            {"cmd": found["cmd"], "reason": found["reason"], "timeout_s": VERIFY_TIMEOUT},
        )
        result = verify.run(found["cmd"], workdir, timeout=VERIFY_TIMEOUT)
        phase = "green" if result.get("ok") else "red"
        verify.persist(rid, task_id, phase, result, note=f"auto after run ({found['reason']})")
        return "ok" if phase == "green" else "red"

    # ---- helpers -------------------------------------------------------

    def _emit(self, rid: str, etype: str, payload: dict) -> None:
        try:
            seq = db.next_seq(rid)
            ev = db.append_event(rid, seq, etype, payload)
            BUS.publish({"run_id": rid, **{k: ev[k] for k in ("seq", "type", "payload", "ts")}})
        except Exception:
            pass

    def _fail(self, rid: str, message: str) -> None:
        self._emit(rid, "run.failed", {"error": message, "status": "failed"})
        db.release_write(rid)
        db.update_run(rid, status="failed", ended_at=time.time(), error=message)

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

    def salvage(self, run: dict) -> list[str]:
        """Record files the worker wrote before its owner died.

        A dead daemon does not mean the agent did nothing: the worker is a
        separate process and may have finished its files already. Failing the
        run without looking loses real output.
        """
        cwd = run.get("cwd") or ""
        started = run.get("started_at") or run.get("created_at") or 0
        root = Path(cwd)
        if not cwd or not root.is_dir():
            return []
        found: list[str] = []
        for path in touched_files(root, started):
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                rel = ""
            try:
                db.add_artifact(
                    run["id"], run.get("task_id"), "file", str(path), _digest(path), rel_path=rel
                )
            except Exception:
                continue
            found.append(str(path))
        return found

    def reap_orphans(self) -> list[dict]:
        """Fail every run that claims to be running when this process owns none."""
        return self._reap(db.live_runs(), "the process that owned this run is gone")

    def reconcile(self, stale_seconds: int = 900) -> list[dict]:
        """Fail runs whose owner process died while they were still running."""
        return self._reap(db.stale_runs(stale_seconds), f"no activity for {stale_seconds}s")

    def _reap(self, runs: list[dict], reason: str) -> list[dict]:
        out: list[dict] = []
        for run in runs:
            rid = run["id"]
            with self.lock:
                proc = self.procs.pop(rid, None)
            if proc is not None:
                self._kill_tree(proc)
            salvaged = self.salvage(run)
            message = f"orphaned: {reason}"
            if salvaged:
                message += f"; {len(salvaged)} file(s) written by the worker were kept"
            try:
                db.append_event(
                    rid,
                    db.next_seq(rid),
                    "run.failed",
                    {"error": message, "status": "failed", "salvaged": salvaged},
                )
            except Exception:
                pass
            db.release_write(rid)
            db.update_run(rid, status="failed", ended_at=time.time(), error=message)
            out.append({**run, "error": message, "salvaged": salvaged})
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
    provider: str = "",
    route: str = "",
    fallbacks: list[str] | None = None,
) -> str:
    listed = "\n".join(f"- {p}" for p in files) or "- （无改动）"
    unverified = (
        "- 本轮没有验证记录：结论只来自 agent 自述，下一位拿到可执行的验收命令后先跑一遍。\n"
        if verify_mark == "none"
        else ""
    )
    endpoint = f"- 端点：{provider}" + (f"（路由 {route}）" if route and route != provider else "")
    fallback_line = ""
    if fallbacks:
        fallback_line = (
            f"- 回退：{' → '.join(fallbacks)} → {provider}"
            "（前序端点未产生任何工具调用，属于上游故障而非任务失败）\n"
        )
    return f"""# Handoff — {task.get('title', '')}

- 任务：{task['id']}
- 执行：{rid}（{agent}）
{endpoint}
{fallback_line}- 状态：{status}
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
