"""HTTP API + SSE. One user, one machine: no auth beyond a loopback bind."""

from __future__ import annotations

import errno
import hmac
import json
import os
import socket
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import (
    budget as budget_mod,
    board,
    state,
    agents,
    config,
    db,
    diagnosis,
    harness_status,
    jobs,
    ledger,
    llm,
    memory,
    observe,
    pet,
    profiles,
    propose,
    providers,
    recover,
    routes,
    runner,
    ui,
    verify,
    watchdog,
)

MAX_BODY = 1_000_000
#: The HTTP contract lives in the repo root so editors and code generators can
#: find it without importing the package; the daemon also serves it verbatim.
SPEC_PATH = Path(__file__).resolve().parent.parent / "openapi.yaml"


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


def _is_disconnect(exc: BaseException | None) -> bool:
    """A client that hung up is not a server error.

    Browsers open the SSE stream and walk away; Windows reports the dead pipe as
    WinError 10053/10054, Python usually as one of the three ConnectionError
    subclasses. Logging a 20-line traceback for each one is what grew
    daemon.log past 6 MB. Anything that is not a disconnect keeps its traceback.
    """
    if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
        return True
    if getattr(exc, "winerror", None) in (10053, 10054):
        return True
    return getattr(exc, "errno", None) in (
        errno.ECONNABORTED,
        errno.ECONNRESET,
        errno.EPIPE,
    )


def authorized(header: str) -> bool:
    """Constant-time token check; a plain == leaks length/prefix timing."""
    return hmac.compare_digest(header or "", f"Bearer {config.token()}")


def body_allowed(length: int) -> bool:
    return 0 <= length <= MAX_BODY


# Compatibility for Python consumers of the former presentation helpers.
board_state = board.board_state
_day_start = board._day_start
_today = board._today


def __getattr__(name):
    if hasattr(board, name):
        return getattr(board, name)
    raise AttributeError(name)


def attention(now: float | None = None) -> dict:
    """The status light's three numbers, read off `board_state` — not recounted.

    One source, one denominator. `waiting` and `failed` come from the board's
    `decision_counts`; `running` is the same live-run list the page shows.

    - `waiting`: decisions a person owns (open proposals + a spend gate past
      the budget), over the board's 50-newest *work* runs and every open
      proposal. A failure never lands here.
    - `failed`: terminal runs in that same 50-work-run window whose outcome is
      not a success — process failed, or completed with red tests. `red` is a
      subset; live retries are not failures yet. Chat utterances are never in
      this window (`board_state` asks for `kind="work"`).
    - `running`: live work runs.

    `statuslight.snapshot` used to count `status == "failed"` itself from a
    50-row *all kinds* window: chat failures spoke, and a spend decision never
    did. That second counter is gone.
    """
    board = board_state(now)
    counts = board.get("decision_counts") or {}
    return {
        "waiting": int(counts.get("needs_you") or 0),
        "running": len(board.get("running") or []),
        "failed": int(counts.get("failed") or 0),
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
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass
    finally:
        runner.BUS.unsubscribe(q)


def mcp_status_payload(run_id: str) -> dict:
    run = db.get_run(run_id)
    if not run:
        raise ValueError(f"没有这个 run：{run_id}")

    usage = ledger.parse(run.get("usage_json"))
    verify = db.verify_states([run_id]).get(run_id, "none")
    handoff = config.HOME / "runs" / run_id / "handoff.md"
    started = run.get("started_at")
    ended = run.get("ended_at")
    return {
        "run_id": run["id"],
        "task_id": run.get("task_id") or "",
        "status": run.get("status") or "",
        "agent": run.get("agent") or "",
        "route": run.get("route") or run.get("agent") or "",
        "provider": run.get("provider") or "",
        "cwd": run.get("cwd") or "",
        "started_at": started,
        "ended_at": ended,
        "duration_s": round(ended - started, 3) if started and ended else None,
        "error": run.get("error") or "",
        # Same shape as always: counts are zero when unrecorded (a caller
        # adding them up wants a number), money is None when unpriced, and an
        # unrecorded currency stays "" rather than becoming the ledger's "-".
        "usage": {
            "input_tokens": int(usage.tokens.input or 0),
            "output_tokens": int(usage.tokens.output or 0),
            "cache_read": int(usage.tokens.cache_read or 0),
            "cache_write": int(usage.tokens.cache_write or 0),
            "cost": usage.cost.amount,
            "currency": usage.cost.currency or "",
            "cost_basis": usage.cost.basis or "",
        },
        "verify": {
            "state": verify,
            "meaning": runner.VERIFY_TEXT.get(verify, verify),
        },
        "handoff": str(handoff) if handoff.exists() else "",
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # quiet by default
        return

    def _guard(self, handler) -> None:
        """Never let an exception take the connection (and the client) down."""
        try:
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1 or not re.fullmatch(r"(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]+)?", hosts[0], re.IGNORECASE):
                self.close_connection = True
                return _json(self, 403, {"error": "forbidden Host"})
            handler()
        except Exception as exc:  # noqa: BLE001 - last line of defence
            # A client that hung up is not a server fault: no traceback for it.
            # Everything else still gets the full stack.
            if _is_disconnect(exc):
                return
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
                or path == "/favicon.ico" or path.startswith("/pet")
                or path.rstrip("/") == "/openapi.yaml"):
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

        if path in ('/handoff', '/handoffs', '/handoff/check'):
            from . import handoff
            try:
                if path == '/handoffs':
                    result = handoff.list_projects(remote=True)
                else:
                    cwd = (qs.get('cwd') or [None])[0]
                    result = (handoff.check if path.endswith('/check') else handoff.show)(cwd, remote=True)
                return _json(self, 200, result)
            except (ValueError, OSError) as exc:
                return _json(self, 403, {'error': str(exc)})

        if path.startswith('/plans/') and len(path.split('/')) == 3:
            from . import plan
            row = plan.get_plan(path.split('/')[2])
            return _json(self, 200 if row else 404, row or {'error': 'not_found'})

        if path == "/asks":
            from . import ask
            workspace = (qs.get("workspace") or [config.workspace_id()])[0]
            with db.connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM ask WHERE workspace_id=? AND expires_at>=? ORDER BY created_at DESC",
                    (workspace, time.time()),
                ).fetchall()
            return _json(self, 200, [
                {key: value for key, value in ask.view(dict(row)).items()
                 if key in ("id", "status", "created_at", "expires_at")}
                for row in rows
            ])

        if path.startswith("/ask/") and len(path.split("/")) == 3:
            from . import ask
            code, payload = ask.get(path.split("/")[2])
            return _json(self, code, payload)

        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/openapi.yaml":
            # Public on purpose: a client needs the contract before it can be
            # trusted with a token, and schema disclosure on loopback is cheap.
            try:
                body = SPEC_PATH.read_bytes()
            except OSError:
                return _json(self, 404, {"error": "openapi.yaml not found"})
            self.send_response(200)
            self.send_header("Content-Type", "application/yaml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/":
            body = board.page()
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
                self,
                200,
                {
                    "ok": True,
                    "version": config.VERSION,
                    "pid": os.getpid(),
                    "profile": config.active_profile(),
                    "home": config.home_id(),
                },
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

        if path == "/state":
            return _json(self, 200, state.snapshot())

        if path == "/board":
            return _json(self, 200, board_state())

        if path == "/cost/today":
            start, day = _day_start(time.time())
            with db.connect() as conn:
                today = _today(conn, start)
            return _json(self, 200, {"day": day, **today})

        if path == "/memory/harvest":
            project = (qs.get("project") or [None])[0]
            try:
                limit = int((qs.get("limit") or ["50"])[0])
                if not 1 <= limit <= 500:
                    raise ValueError("limit must be between 1 and 500")
                rows = memory.harvest(project)
                if (qs.get("conflicts_only") or ["false"])[0] == "true":
                    rows = [r for r in rows if r["conflict"]]
                rows = rows[:limit]
                return _json(self, 200, {"count": len(rows), "project": str(memory.project_root(project)), "candidates": rows,
                    "adopt_hint": "采纳由人做：看看板或 sodus debug memory adopt <id>"})
            except (ValueError, OSError) as exc:
                return _json(self, 400, {"error": str(exc)})

        if path == "/memory/candidates":
            workspace = (qs.get("workspace") or [config.workspace_id()])[0]
            status = (qs.get("status") or [""])[0]
            with db.connect() as c:
                sql = "SELECT * FROM memory_candidate WHERE workspace=?"
                args = [workspace]
                if status: sql += " AND status=?"; args.append(status)
                rows = [dict(r) for r in c.execute(sql + " ORDER BY updated_at DESC", args)]
            for row in rows:
                row["provenance"] = json.loads(row.get("sources_json") or "[]")
                with db.connect() as c:
                    row["evidence_count"] = c.execute("SELECT COUNT(DISTINCT lineage_root) FROM candidate_evidence WHERE candidate_id=? AND exposed=0", (row["id"],)).fetchone()[0]
            return _json(self, 200, rows)

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
                board._fresh_cache()
                run = db.get_run(rid)
                if not run:
                    return _json(self, 404, {"error": f"run not found: {rid}"})
                run["mcp"] = mcp_status_payload(rid)
                if run.get("status") == "failed":
                    # The log view and the board must agree on why something
                    # failed; the wording lives in recover.py, not in the page.
                    run["problem"] = recover.problem_text(run)
                    run["evidence"] = board._evidence(run, db.verify_states([rid]).get(rid, "none"))
                elif db.verify_states([rid]).get(rid) == "red":
                    run["evidence"] = board._evidence(run, "red")
                return _json(self, 200, run)
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
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return _json(self, 400, {"error": "invalid content-length"})
        if not body_allowed(length):
            self.close_connection = True
            return _json(self, 413, {"error": "body too large"})
        # Consume the bounded body before early rejection. Otherwise HTTP/1.1
        # sees leftover JSON as the next request, and Windows can abort the
        # client's still-in-progress body send when the connection closes.
        raw_body = self.rfile.read(length) if length else b''
        if not self._authorized():
            return _json(self, 401, {"error": "unauthorized"})
        path = urlparse(self.path).path.rstrip("/")
        if path.startswith('/plans/') and len(path.split('/')) == 4 and path.endswith('/approve'):
            return _json(self, 403, {"error": "approve_requires_cli"})
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except json.JSONDecodeError:
            return _json(self, 400, {"error": "invalid json"})

        if path == '/plan' or (path.startswith('/plans/') and len(path.split('/')) == 4):
            from . import plan
            try:
                depth = int(self.headers.get('X-Sodus-Depth', '0'))
            except (ValueError, TypeError):
                return _json(self, 400, {'error': 'invalid_depth'})
            if depth >= 1 or os.environ.get('NODUS_ROLE') == 'worker':
                return _json(self, 403, {'error': 'recursion_denied'})
            if depth < 0:
                return _json(self, 400, {'error': 'invalid_depth'})
            if not isinstance(body, dict):
                return _json(self, 400, {'error': 'invalid_body'})
            try:
                if path == '/plan':
                    row = plan.create(body.get('goal'), body.get('cwd', '.'),
                                      body.get('acceptance', ''), body.get('route'), wait=False)
                    return _json(self, 202, {**row, 'plan_id': row['id']})
                pid, action = path.split('/')[2:]
                if action == 'review':
                    row = plan.review(pid)
                    return _json(self, 202, row)
                else:
                    return _json(self, 404, {'error': 'not_found'})
            except KeyError:
                return _json(self, 404, {'error': 'not_found'})
            except (ValueError, TypeError, OSError) as exc:
                return _json(self, 400, {'error': str(exc)})

        if path == "/reconcile":
            return _json(self, 200, runner.RUNNER.reconcile(float(body.get("stale", 300))))

        if path == "/ask":
            from . import ask
            code, payload = ask.submit(body, self.headers.get("X-Sodus-Depth", "0"))
            return _json(self, code, payload)
        if path.startswith("/ask/") and path.endswith("/promote") and len(path.split("/")) == 4:
            from . import ask
            code, payload = ask.promote(path.split("/")[2], body)
            return _json(self, code, payload)

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
                    route_name=body.get("route") or body.get("agent") or config.default_route(),
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
                    allow_expensive=bool(body.get("allow_expensive", False)),
                    include_memory=not bool(body.get("no_memory", False)),
                )
            except (ValueError, RuntimeError) as exc:
                return _json(self, 400, {"error": str(exc)})
            return _json(self, 201, run)

        if path == "/llm/chat":
            # The desktop pets' only way to a model. One route, one provider,
            # one credential — the caller names an agent and never an endpoint,
            # so a pet host needs no key of its own. Failures carry a `kind`
            # because the caller has to tell "Nodus is down" from "the agent
            # name is wrong" without parsing prose.
            status, payload = llm.chat(
                agent=body.get("agent") or "",
                messages=body.get("messages"),
                max_tokens=body.get("max_tokens"),
                note=str(body.get("note") or ""),
                cwd=str(body.get("cwd") or ""),
            )
            return _json(self, status, payload)

        if path == "/memory/remember":
            try:
                text, source = body.get("text"), body.get("source", "agent")
                if not isinstance(text, str) or not isinstance(source, str):
                    raise ValueError("text and source must be strings")
                return _json(self, 200, memory.remember(text, source=source))
            except (ValueError, OSError) as exc:
                return _json(self, 400, {"error": str(exc)})

        if path == "/memory/adopt":
            ids = body.get("ids")
            if isinstance(ids, str):
                ids = [ids]
            if not isinstance(ids, list) or not ids:
                return _json(self, 400, {"error": "ids must be a non-empty list"})
            ids = [str(item).strip() for item in ids if str(item).strip()]
            if not all(re.fullmatch(r"mem-[0-9a-f]{6,40}", item) for item in ids):
                return _json(self, 400, {"error": "invalid candidate id"})
            try:
                result = memory.adopt(ids, memory.saved_project())
            except ValueError as exc:
                return _json(self, 400, {"error": str(exc)})
            except OSError as exc:
                _log(f"memory: write failed: {exc}")
                return _json(self, 409, {"error": f"could not write AGENTS.md: {exc}"})
            except Exception as exc:  # noqa: BLE001 - the reply carries the cause
                # "采纳失败：internal error" is unanswerable. Whatever broke, the
                # person clicking the button gets the actual message.
                _log(f"memory: adopt crashed: {type(exc).__name__}: {exc}")
                return _json(self, 500, {"error": f"{type(exc).__name__}: {exc}"})
            if result["added"]:
                _log(f"memory: adopted {result['added']} fact(s) into {result['path']}")
            elif result["already"]:
                _log(
                    f"memory: {result['already']} candidate(s) already adopted in "
                    f"{result['path']}"
                )
            else:
                _log("memory: nothing to adopt")
            return _json(self, 200, result)

        if path.startswith("/memory/candidates/"):
            cid = path.rsplit("/", 1)[-1]; action = body.get("action", "")
            row = db.memory_get_candidate(cid)
            if not row: return _json(self, 404, {"error":"not found"})
            try: version = int(body.get("version"))
            except (TypeError, ValueError): return _json(self, 400, {"error":"version required"})
            if version != int(row.get("version", 0)): return _json(self, 409, {"error":"candidate version conflict", "candidate":row})
            if action == "edit":
                text = str(body.get("text") or "").strip()
                if not text: return _json(self, 400, {"error":"text required"})
                try:
                    new_id = db.memory_add_candidate(text, row.get("workspace"), kind=row.get("kind","lesson"), sources_json=json.dumps({"edited_from":cid,"sources":json.loads(row.get("sources_json") or "[]")}, ensure_ascii=False))
                    db.memory_update_candidate(cid, version, status="rejected")
                    if new_id != cid:
                        with db.connect() as c:
                            c.execute('INSERT OR IGNORE INTO candidate_evidence SELECT ?,episode_id,lineage_root,exposed,created_at FROM candidate_evidence WHERE candidate_id=?', (new_id,cid))
                except ValueError as exc: return _json(self, 409, {"error":str(exc)})
                return _json(self, 200, {"ok":True,"id":new_id,"edited_from":cid})
            if action not in ("adopt", "reject", "retire"): return _json(self, 400, {"error":"unknown action"})
            status = {"adopt":"adopted", "reject":"rejected", "retire":"retire_proposed"}[action]
            try: updated = db.memory_update_candidate(cid, version, status=status)
            except ValueError as exc: return _json(self, 409, {"error":str(exc)})
            return _json(self, 200, updated)

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


class _QuietServer(ThreadingHTTPServer):
    """The HTTP server, minus the traceback for a client that hung up.

    socketserver writes a full traceback for every exception that escapes a
    request thread, including the ConnectionAbortedError(10053) a browser
    produces by closing an SSE stream. daemon.log had ~5,800 of those and they
    drowned the real failures. A disconnect is dropped silently; anything else
    still gets its full stack in the log.
    """

    allow_reuse_address = False

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address) -> None:
        if _is_disconnect(sys.exc_info()[1]):
            return
        _log("request crashed:\n" + traceback.format_exc())


def serve(port: int = 4199, host: str = "127.0.0.1") -> None:
    from .daemon import instance_lock
    config.ensure_dirs()
    with instance_lock():
        with _QuietServer((host, port), Handler) as server:
            _serve_bound(server, port, host)


def _serve_bound(server, port, host):
    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get(
        "NODUS_ALLOW_REMOTE"
    ):
        raise SystemExit("refusing non-loopback bind without NODUS_ALLOW_REMOTE=1")
    config.ensure_dirs()
    profiles.ensure_ready()
    db.connect().close()
    try:
        memory.recover_journals()
    except Exception as exc:
        _log(f"memory recovery: {exc}")
    # A daemon that just started owns no runs: anything still marked running
    # belongs to a process that is gone. Reap before accepting work, so a
    # restart never leaves clients waiting on a corpse.
    reaped = runner.RUNNER.reap_orphans()
    from . import plan
    plan.reconcile()
    stalled = jobs.reap(db.list_pipelines("running"))
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
    # Reaping has just turned the previous daemon's live runs into failures.
    # Those are precisely the interruptions the operator should not have to
    # decide on, so recovery gets one pass before the port starts answering,
    # then keeps sweeping for failures that happen while the daemon is up.
    if recover.enabled():
        recover.start(log=_log)
    else:
        _log("auto-recover disabled (NODUS_AUTO_RECOVER=0)")
    # The other half of "a run is not moving": recovery owns failures, the
    # watchdog owns rows that are still `running` but silent. `stalled` had no
    # writer before this; `NODUS_WATCHDOG=0` turns the loop off. One sweep is
    # cheap (live rows only) and never kills a run recovery is already handling.
    if watchdog.enabled():
        try:
            watchdog.start(log=_log)
        except Exception:  # noqa: BLE001 - the board outranks the watchdog
            _log("watchdog failed to start:\n" + traceback.format_exc())
    else:
        _log("watchdog disabled (NODUS_WATCHDOG=0)")
    # The board's money is only as fresh as the last import. This used to be a
    # manual step, so a weekend of conversations sat unread in DSH's session
    # files while /board showed the previous number. A broken sweep is logged
    # and retried, never allowed to take the daemon down.
    try:
        from . import observe_sync

        observe_sync.start(log=_log)
        from . import dream
        dream.start_daily(log=_log)
    except Exception:  # noqa: BLE001 - observation is a supplement, not the server
        pass
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
