"""HTTP API + SSE. One user, one machine: no auth beyond a loopback bind."""

from __future__ import annotations

import hmac
import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import agents, config, db, jobs, pet, profiles, propose, providers, routes, runner, ui

MAX_BODY = 1_000_000


def _log(message: str) -> None:
    """One line per event in `~/.nodus/daemon.log`.

    A detached daemon has nowhere to print, so an unexplained exit used to
    leave no evidence at all.
    """
    try:
        config.HOME.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with (config.HOME / "daemon.log").open("a", encoding="utf-8") as f:
            f.write(f"{stamp} {message}\n")
    except OSError:
        pass


def authorized(header: str) -> bool:
    """Constant-time token check; a plain == leaks length/prefix timing."""
    return hmac.compare_digest(header or "", f"Bearer {config.token()}")


def body_allowed(length: int) -> bool:
    return 0 <= length <= MAX_BODY


def _usage(raw: str | None) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _day_start(now: float | None = None) -> tuple[float, str]:
    """Midnight in the operator's timezone.

    "Today" has to be their day: a UTC boundary would move the total under
    them at 08:00 local time.
    """
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return midnight, time.strftime("%Y-%m-%d", lt)


def _live_rows(conn, now: float) -> list[dict]:
    """Runs that are still going, with what has been recorded so far.

    `run.usage_json` is only written when a run finalises, so a live row has no
    price yet. What it does have is its `usage` events, which is a real record
    of what the CLI reported mid-flight — a lower bound, never presented as the
    final `_price()` number.
    """
    rows = conn.execute(
        "SELECT * FROM run WHERE workspace_id=? AND status IN ('queued','running')"
        " ORDER BY started_at",
        (config.workspace_id(),),
    ).fetchall()
    out = []
    for row in rows:
        run = dict(row)
        tokens = {"input": 0, "output": 0}
        reported: float | None = None
        for ev in conn.execute(
            "SELECT payload_json FROM event WHERE run_id=? AND type='usage'", (run["id"],)
        ):
            payload = _usage(ev["payload_json"])
            tokens["input"] += int(payload.get("input_tokens") or 0)
            tokens["output"] += int(payload.get("output_tokens") or 0)
            if isinstance(payload.get("cost"), (int, float)):
                reported = (reported or 0.0) + float(payload["cost"])
        out.append(
            {
                "id": run["id"],
                "agent": run.get("agent") or "",
                "route": run.get("route") or run.get("agent") or "",
                "provider": run.get("provider") or "",
                "cwd": run.get("cwd") or "",
                "note": run.get("note") or "",
                "status": run.get("status") or "",
                "started_at": run.get("started_at"),
                "tokens": tokens,
                "partial_cost": round(reported, 6) if reported is not None else None,
                # The CLI's own `total_cost_usd`; different basis from _price().
                "partial_currency": "USD" if reported is not None else "",
            }
        )
    return out


def _decisions(runs: list[dict], states: dict[str, str]) -> tuple[list[dict], dict]:
    """Everything on this board that cannot move without a human."""
    items: list[dict] = []
    counts = {"proposal": 0, "red": 0, "failed": 0, "unverified": 0}

    proposals = db.list_proposals("open")
    counts["proposal"] = len(proposals)
    for p in proposals[:5]:
        files = [str(f.get("path") or "") for f in (p.get("files") or [])]
        items.append(
            {
                "kind": "proposal",
                "id": p.get("id") or "",
                "run": p.get("run_id") or "",
                "title": "提案在分支上，等你合入或丢弃",
                "detail": p.get("stat") or "",
                "note": ", ".join(files)[:160],
                "at": p.get("created_at"),
            }
        )

    red = [r for r in runs if states.get(r["id"]) == "red"]
    counts["red"] = len(red)
    for r in red[:3]:
        items.append(
            {
                "kind": "red",
                "id": r["id"],
                "run": r["id"],
                "title": "跑完了，测试是红的",
                "detail": (r.get("note") or "")[:140],
                "note": "",
                "at": r.get("ended_at") or r.get("started_at"),
            }
        )

    failed = [r for r in runs if r.get("status") == "failed"]
    counts["failed"] = len(failed)
    for r in failed[:3]:
        items.append(
            {
                "kind": "failed",
                "id": r["id"],
                "run": r["id"],
                "title": "执行失败，要不要重跑由你定",
                "detail": (r.get("error") or "")[:160],
                "note": "",
                "at": r.get("ended_at") or r.get("started_at"),
            }
        )

    unverified = [
        r for r in runs if r.get("status") == "completed" and states.get(r["id"], "none") == "none"
    ]
    counts["unverified"] = len(unverified)
    if unverified:
        items.append(
            {
                "kind": "unverified",
                "id": "",
                "run": "",
                "title": f"{len(unverified)} 个 run 跑完但没有测试证据（状态只代表进程退出码）",
                "detail": "",
                "note": "",
                "at": unverified[0].get("ended_at") or unverified[0].get("started_at"),
            }
        )
    return items, counts


def _today(conn, start: float) -> dict:
    """Cost recorded today, straight out of `run.usage_json`.

    No re-derivation and no FX: `_price()` already chose the number and its
    currency, and adding two currencies would invent an exchange rate.
    """
    rows = conn.execute(
        "SELECT agent, route, usage_json FROM run"
        " WHERE workspace_id=? AND started_at IS NOT NULL AND started_at>=?",
        (config.workspace_id(), start),
    ).fetchall()
    by_agent: dict[str, dict] = {}
    totals: dict[str, float] = {}
    basis: dict[str, int] = {}
    priced = 0
    for row in rows:
        usage = _usage(row["usage_json"])
        agent = row["agent"] or "-"
        bucket = by_agent.setdefault(
            agent, {"agent": agent, "costs": {}, "runs": 0, "priced": 0, "routes": []}
        )
        bucket["runs"] += 1
        route = row["route"] or ""
        if route and route != agent and route not in bucket["routes"]:
            bucket["routes"].append(route)
        cost = usage.get("cost")
        if not isinstance(cost, (int, float)):
            continue
        priced += 1
        bucket["priced"] += 1
        currency = str(usage.get("currency") or "USD")
        bucket["costs"][currency] = round(bucket["costs"].get(currency, 0.0) + float(cost), 6)
        totals[currency] = round(totals.get(currency, 0.0) + float(cost), 6)
        key = str(usage.get("cost_basis") or "none")
        basis[key] = basis.get(key, 0) + 1
    ordered = sorted(by_agent.values(), key=lambda b: (-b["priced"], -b["runs"], b["agent"]))
    return {
        "runs": len(rows),
        "priced": priced,
        "unpriced": len(rows) - priced,
        "totals": totals,
        "by_agent": ordered,
        "basis": basis,
    }


def board_state(now: float | None = None) -> dict:
    """The first screen: what is running, what waits on a human, today's money."""
    now = time.time() if now is None else now
    start, day = _day_start(now)
    runs = db.list_runs(limit=50)
    states = db.verify_states([r["id"] for r in runs])
    decisions, counts = _decisions(runs, states)
    conn = db.connect()
    try:
        live = _live_rows(conn, now)
        today = _today(conn, start)
    finally:
        conn.close()
    return {
        "now": now,
        "day": day,
        "profile": config.active_profile(),
        "running": live,
        "decisions": decisions,
        "decision_counts": counts,
        "today": {"window_start": start, **today},
    }


def _json(handler: BaseHTTPRequestHandler, code: int, payload) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _sse(handler: BaseHTTPRequestHandler, run_id: str | None, after: int) -> None:
    q = runner.BUS.subscribe()
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.end_headers()
    try:
        backlog = db.events_after(run_id, after) if run_id else []
        for ev in backlog:
            handler.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            handler.wfile.flush()
        last_ping = time.time()
        while True:
            try:
                ev = q.get(timeout=15)
            except queue.Empty:
                if time.time() - last_ping > 15:
                    handler.wfile.write(b": ping\n\n")
                    handler.wfile.flush()
                    last_ping = time.time()
                continue
            if run_id and ev.get("run_id") != run_id:
                continue
            handler.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            handler.wfile.flush()
            last_ping = time.time()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        runner.BUS.unsubscribe(q)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # quiet by default
        return

    def _guard(self, handler) -> None:
        """Never let an exception take the connection (and the client) down."""
        try:
            handler()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:  # noqa: BLE001 - last line of defence
            import traceback

            _log("handler crashed:\n" + traceback.format_exc())
            try:
                _json(self, 500, {"error": "internal error"})
            except OSError:
                pass

    def do_GET(self) -> None:
        self._guard(self._get)

    def do_POST(self) -> None:
        self._guard(self._post)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _authorized(self) -> bool:
        # `/` is the board itself: it has to load before anything else can be
        # requested, and it carries the token for the requests it makes.
        #
        # Compare the *path* only. Comparing `self.path` directly fails for the
        # natural way to open the board, `/?token=...` — the query string makes
        # it unequal to "/", so a correct token still got 401.
        path = urlparse(self.path).path or "/"
        if (path == "/" or path.startswith("/health")
                or path == "/favicon.ico" or path.startswith("/pet")):
            return True
        if authorized(self.headers.get("Authorization", "")):
            return True
        # EventSource cannot set an Authorization header. The stream is
        # read-only, loopback-only and already public to anything on this
        # machine (which can read ~/.nodus/token anyway), but the token still
        # has to match — an unauthenticated stream would leak run payloads.
        u = urlparse(self.path)
        if u.path.rstrip("/") == "/events":
            token = (parse_qs(u.query).get("token") or [""])[0]
            return hmac.compare_digest(token, config.token())
        return False

    def _get(self) -> None:
        if not self._authorized():
            return _json(self, 401, {"error": "unauthorized"})
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"

        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/":
            body = ui.PAGE.replace("__TOKEN__", config.token()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/pet":
            # Public on purpose: a desktop widget has no way to hold the token,
            # and this is the same loopback-only machine.
            return _json(self, 200, pet.state())

        if path == "/health":
            return _json(
                self, 200, {"ok": True, "version": config.VERSION, "profile": config.active_profile()}
            )

        if path == "/agents":
            secrets = config.load_secrets()
            return _json(
                self,
                200,
                [
                    {
                        "name": s.name,
                        "vendor": s.vendor,
                        "protocols": list(s.accepts),
                        "ready": not agents.unavailable(s),
                        "missing": agents.unavailable(s),
                    }
                    for s in agents.registry().values()
                ],
            )

        if path == "/providers":
            secrets = config.load_secrets()
            return _json(
                self,
                200,
                [
                    {
                        "name": p.name,
                        "protocol": p.protocol,
                        "base_url": p.base_url,
                        "model": p.model,
                        "tiers": p.tiers,
                        "pricing": vars(p.pricing) if p.pricing else None,
                        "meter": p.meter,
                        "note": p.note,
                        "ready": not providers.missing(p, secrets),
                        "missing": providers.missing(p, secrets),
                    }
                    for p in providers.load().values()
                ],
            )

        if path == "/routes":
            secrets = config.load_secrets()
            table = providers.load()
            out = []
            for route in routes.load().values():
                spec = agents.get(route.agent)
                gaps = routes.problems(route)
                if spec is not None:
                    for name in route.providers:
                        provider = table.get(name)
                        if provider is not None:
                            gaps.extend(
                                f"{name}: {gap}" for gap in agents.missing(spec, provider, secrets)
                            )
                out.append(
                    {
                        "name": route.name,
                        "agent": route.agent,
                        "providers": list(route.providers),
                        "model": route.model,
                        "effort": route.effort,
                        "ready": not gaps,
                        "problems": gaps,
                    }
                )
            return _json(self, 200, out)

        if path == "/profiles":
            active = config.active_profile()
            return _json(
                self,
                200,
                [
                    {**vars(profiles.read(name)), "active": name == active}
                    for name in (profiles.names() or ["default"])
                ],
            )

        if path == "/board":
            return _json(self, 200, board_state())

        if path == "/tasks":
            return _json(self, 200, db.list_tasks())

        if path.startswith("/tasks/"):
            return _json(self, 200, db.get_task(path.split("/")[2]))

        if path == "/runs":
            task_id = (qs.get("task_id") or [None])[0]
            runs = db.list_runs(task_id)
            states = db.verify_states([r["id"] for r in runs])
            for run in runs:
                run["verify"] = states.get(run["id"], "none")
            return _json(self, 200, runs)

        if path.startswith("/runs/"):
            parts = path.split("/")
            rid = parts[2]
            if len(parts) == 3:
                return _json(self, 200, db.get_run(rid))
            if parts[3] == "events":
                after = int((qs.get("after") or [0])[0])
                if (qs.get("stream") or ["0"])[0] in ("1", "true"):
                    return _sse(self, rid, after)
                return _json(self, 200, db.events_after(rid, after))
            if parts[3] == "artifacts":
                return _json(self, 200, db.artifacts_for(rid))
            if parts[3] == "handoff":
                h = db.latest_handoff(db.get_run(rid).get("task_id", ""))
                text = ""
                if h:
                    try:
                        text = Path(h["path"]).read_text(encoding="utf-8")
                    except OSError:
                        text = ""
                return _json(self, 200, {"path": h.get("path"), "text": text})

        if path == "/events":
            return _sse(self, None, 0)

        if path == "/pipelines":
            return _json(self, 200, db.list_pipelines((qs.get("status") or [""])[0]))

        if path == "/proposals":
            return _json(self, 200, db.list_proposals("" if (qs.get("all") or [""])[0] else "open"))

        if path.startswith("/pipelines/"):
            parts = path.split("/")
            pid = parts[2]
            row = db.get_pipeline(pid)
            if not row:
                return _json(self, 404, {"error": "not found"})
            if len(parts) == 3:
                return _json(self, 200, row)
            if parts[3] == "events":
                after = int((qs.get("after") or [0])[0])
                return _json(self, 200, db.events_after(pid, after))

        return _json(self, 404, {"error": "not found"})

    def _post(self) -> None:
        if not self._authorized():
            return _json(self, 401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return _json(self, 400, {"error": "invalid content-length"})
        if not body_allowed(length):
            return _json(self, 413, {"error": "body too large"})
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        try:
            body = self._body()
        except json.JSONDecodeError:
            return _json(self, 400, {"error": "invalid json"})

        if path == "/tasks":
            task = db.create_task(
                goal=body.get("goal", ""),
                acceptance=body.get("acceptance", ""),
                session_id=body.get("session_id"),
                title=body.get("title", ""),
            )
            return _json(self, 201, task)

        if path == "/runs":
            try:
                run = runner.RUNNER.start(
                    task_id=body.get("task_id", ""),
                    route_name=body.get("route") or body.get("agent") or "claude",
                    cwd=body.get("cwd"),
                    parent_run_id=body.get("parent_run_id"),
                    note=body.get("note", ""),
                    effort=body.get("effort", ""),
                    model=body.get("model", ""),
                    profile=body.get("profile", ""),
                    wait=float(body.get("wait") or 0.0),
                    session_id=body.get("session_id", ""),
                    session_mode=body.get("session_mode", "resume"),
                    provider=body.get("provider", ""),
                    verify_run=body.get("verify"),
                    propose_run=bool(body.get("propose", False)),
                )
            except (ValueError, RuntimeError) as exc:
                return _json(self, 400, {"error": str(exc)})
            return _json(self, 201, run)

        if path.startswith("/runs/") and path.endswith("/stop"):
            rid = path.split("/")[2]
            return _json(self, 200, {"stopped": runner.RUNNER.stop(rid)})

        if path == "/pipelines":
            kind = body.get("kind", "")
            params = body.get("params") or {}
            if kind not in ("tdd", "review", "fix"):
                return _json(self, 400, {"error": f"unknown pipeline: {kind}"})
            try:
                row = jobs.JOBS.submit(kind, params, body.get("label", ""))
            except (ValueError, TypeError) as exc:
                return _json(self, 400, {"error": str(exc)})
            return _json(self, 201, row)

        if path.startswith("/proposals/"):
            parts = path.split("/")
            pid, action = parts[2], (parts[3] if len(parts) > 3 else "")
            row = db.get_proposal(pid)
            if not row:
                return _json(self, 404, {"error": "not found"})
            if action == "reject":
                propose.reject(row)
                _log(f"proposal {pid} rejected from the board")
                return _json(self, 200, {"ok": True, "proposal": pid, "status": "rejected"})
            if action == "merge":
                result = propose.merge(row)
                _log(f"proposal {pid} merge -> {result}")
                return _json(self, 200 if result["ok"] else 409, result)

        if path.startswith("/pipelines/") and path.endswith("/stop"):
            pid = path.split("/")[2]
            return _json(self, 200, {"stopped": jobs.JOBS.stop(pid)})

        return _json(self, 404, {"error": "not found"})


def serve(port: int = 4199, host: str = "127.0.0.1") -> None:
    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get(
        "NODUS_ALLOW_REMOTE"
    ):
        raise SystemExit("refusing non-loopback bind without NODUS_ALLOW_REMOTE=1")
    config.ensure_dirs()
    profiles.ensure_ready()
    db.connect().close()
    # A daemon that just started owns no runs: anything still marked running
    # belongs to a process that is gone. Reap before accepting work, so a
    # restart never leaves clients waiting on a corpse.
    reaped = runner.RUNNER.reap_orphans()
    stalled = jobs.reap(db.list_pipelines("running"))
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    # The pet doubles as a status light: when something new starts waiting on a
    # human, the desktop pet says so. Off with NODUS_STATUS_LIGHT=0 for anyone
    # who does not run a pet — it costs one sqlite read per interval and never
    # speaks unless a count went up.
    if os.environ.get("NODUS_STATUS_LIGHT", "1") != "0":
        try:
            from . import statuslight

            statuslight.start()
        except Exception:  # a broken pet must never keep the board from serving
            pass
    _log(f"nodusd {config.VERSION} listening on {host}:{port} (pid {os.getpid()})")
    if reaped:
        _log(f"reaped {len(reaped)} orphaned run(s): " + ", ".join(r["id"] for r in reaped))
    if stalled:
        _log(f"closed {len(stalled)} pipeline(s) the previous daemon left running")
    if sys.stderr.isatty():  # a detached daemon writes to the log, not the console
        print(f"nodusd listening on http://{host}:{port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("nodusd stopped (interrupt)")
        raise
    except Exception:
        import traceback

        _log("nodusd crashed:\n" + traceback.format_exc())
        raise


def serve_async(port: int = 4199) -> threading.Thread:
    t = threading.Thread(target=serve, args=(port,), daemon=True)
    t.start()
    return t
