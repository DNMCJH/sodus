"""Session lineage: what a session came from, and what came out of it.

A row in `session` is one *sitting*: the conversation Nodus was handed for one
task. Resuming a conversation opens a new sitting whose `parent_session_id`
points at the last sitting of that conversation, so a history of A -> B -> C
stays readable even though all three have the same vendor session id underneath.

Two existing facts make this writable without another table:

  - `task.session_id` names the sitting a run executed in (created with the task)
  - `run.session_ref_json["session_id"]` is the vendor conversation id, written
    when the run finishes

The second one is the only bridge between a vendor id (what `nodus resume`
receives) and our own rows. A conversation Nodus never ran therefore has no
parent: the link is unknown, not invented.

Writers: `attach()` (called by `Runner.start`, before the worker spawns, so the
link is visible while the run is still going). Readers: `chain`, `descendants`,
`for_session`, `for_run`, `for_task`.
"""

from __future__ import annotations

import json
import time

from . import db

#: Hard stop for any walk. A hand-edited or corrupted parent link must not turn
#: `nodus lineage` into an infinite loop.
MAX_DEPTH = 50

RELATIONS = ("resume", "branch", "fresh")


# ---- small helpers -----------------------------------------------------


def _json(value: str) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _vendor(run: dict) -> str:
    """The vendor's own conversation id for a run, or "" when it never got one."""
    return str(_json(run.get("session_ref_json")).get("session_id") or "")


def _relation_for(session_id: str, session_mode: str) -> str:
    if not session_id:
        # A seeded first turn ("resume" with no vendor id to resume) carries no
        # history, whatever the caller called it.
        return "fresh"
    mode = str(session_mode or "").strip().lower()
    if mode in ("fork", "branch"):
        return "branch"
    if mode == "fresh":
        return "fresh"
    return "resume"


def _node(row: dict) -> dict:
    item = dict(row)
    item["relation"] = item.get("relation") or "fresh"
    return item


def session_for_vendor(vendor_id: str) -> dict:
    """The latest sitting of this vendor conversation, or {} if we never ran it.

    A full scan is deliberate: `session_ref_json` is a JSON blob and matching it
    with LIKE breaks the first time the encoder changes spacing. The local DB
    holds thousands of runs, not millions.
    """
    value = str(vendor_id or "").strip()
    if not value:
        return {}
    for run in db.list_runs(limit=100000):  # newest first
        if _vendor(run) != value:
            continue
        task = db.get_task(run.get("task_id") or "")
        row = db.get_session(task.get("session_id") or "")
        if row:
            return row
    return {}


# ---- writer ------------------------------------------------------------


def attach(
    run: dict, task: dict, session_id: str = "", session_mode: str = "resume"
) -> dict:
    """Record how this run's sitting came to be. Idempotent, never inserts.

    The sitting row is created together with the task, so "continuing an
    existing session does not add a row" is structural here rather than a
    check: a resume run reuses the row it was given and only fills in the link.
    Replaying the same run (recover, retry) leaves the first answer alone.
    """
    sid = str((task or {}).get("session_id") or "")
    rid = str((run or {}).get("id") or "")
    if not sid or not rid:
        return db.get_session(sid) if sid else {}
    current = db.get_session(sid)
    if not current:
        return {}

    relation = _relation_for(session_id, session_mode)
    parent = current.get("parent_session_id") or ""
    if not parent and relation != "fresh":
        source = session_for_vendor(session_id)
        if source and source.get("id") != sid:
            parent = source["id"]

    # The first run that touched this sitting owns it; a later run in the same
    # sitting must not rewrite history. Likewise a branch/resume label wins over
    # the 'fresh' default it was created with, but nothing downgrades it back.
    origin = current.get("origin_run_id") or rid
    existing = current.get("relation") or ""
    if not existing or (existing == "fresh" and relation != "fresh"):
        existing = relation

    if (
        parent != (current.get("parent_session_id") or "")
        or origin != (current.get("origin_run_id") or "")
        or existing != (current.get("relation") or "")
    ):
        db.set_session_lineage(
            sid,
            parent_session_id=parent or None,
            origin_run_id=origin,
            relation=existing,
        )
    return db.get_session(sid)


# ---- readers -----------------------------------------------------------


def chain(session_id: str) -> list[dict]:
    """Ancestors of this session, root first, ending with the session itself.

    Stops at the first repeat: a self-reference returns just that row, and a
    longer cycle returns the part that is not a repeat.
    """
    path: list[dict] = []
    seen: set[str] = set()
    current = str(session_id or "").strip()
    while current and current not in seen and len(path) < MAX_DEPTH:
        row = db.get_session(current)
        if not row:
            break
        seen.add(current)
        path.append(_node(row))
        current = str(row.get("parent_session_id") or "").strip()
    path.reverse()
    return path


def descendants(session_id: str) -> list[dict]:
    """Everything derived from this session, breadth first, cycle-safe.

    `depth` is the distance from the root, so a fork of a fork reads as 2.
    """
    root = db.get_session(str(session_id or "").strip())
    if not root:
        return []
    out: list[dict] = []
    seen = {root["id"]}
    frontier = [root["id"]]
    depth = 0
    while frontier and depth < MAX_DEPTH:
        following: list[str] = []
        for parent in frontier:
            for child in db.child_sessions(parent):
                if child["id"] in seen:
                    continue
                seen.add(child["id"])
                item = _node(child)
                item["depth"] = depth + 1
                out.append(item)
                following.append(child["id"])
        frontier = following
        depth += 1
    return out


def for_session(session_id: str) -> dict:
    row = db.get_session(str(session_id or "").strip())
    if not row:
        return {}
    ancestry = chain(row["id"])
    # The tree is rendered from the root, not from the node that was asked
    # about: asking about a leaf still has to show its siblings and cousins,
    # otherwise the tree would omit the very row the user named.
    root = ancestry[0]["id"] if ancestry else row["id"]
    return {
        "session": _node(row),
        "chain": ancestry,
        "descendants": descendants(root),
    }


def _conversation_runs(session_id: str, vendor: str, exclude: str = "") -> list[dict]:
    """Other runs of the same conversation: same sitting, or same vendor id.

    The vendor half is what makes a resumed sitting continue the record of the
    conversation instead of starting a second, unrelated one.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for run in db.runs_for_session(session_id):
        if run["id"] == exclude:
            continue
        seen.add(run["id"])
        rows.append(run)
    if vendor:
        for run in db.list_runs(limit=100000):
            if run["id"] in seen or run["id"] == exclude:
                continue
            if _vendor(run) == vendor:
                seen.add(run["id"])
                rows.append(run)
    rows.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
    return rows


def for_run(run_id: str) -> dict:
    run = db.get_run(str(run_id or "").strip())
    if not run:
        return {}
    task = db.get_task(run.get("task_id") or "")
    session = db.get_session(task.get("session_id") or "") if task else {}
    vendor = _vendor(run)
    return {
        "run": run,
        "task": task,
        "session": _node(session) if session else {},
        "chain": chain(session["id"]) if session else [],
        "runs": _conversation_runs(session.get("id", ""), vendor, exclude=run["id"]),
        "proposals": db.proposals_for_run(run["id"]),
    }


def _run_summary(run: dict, verify: str) -> dict:
    started = run.get("started_at") or 0
    ended = run.get("ended_at") or 0
    usage = _json(run.get("usage_json"))
    status = run.get("status") or ""
    return {
        "id": run["id"],
        "route": run.get("route") or "",
        "agent": run.get("agent") or "",
        "provider": run.get("provider") or "",
        "model": run.get("model") or "",
        "status": status,
        "verify": verify,
        # "completed" alone is an exit code. A run whose tests went red did not
        # succeed, and this is the field the route table counts.
        "ok": status == "completed" and verify != "red",
        "seconds": round(ended - started, 1) if ended and started else None,
        "cost": usage.get("cost"),
        "currency": usage.get("currency") or "",
        "vendor_session": _vendor(run),
        "error": run.get("error") or "",
        "started_at": started,
    }


def _route_summary(runs: list[dict]) -> list[dict]:
    buckets: dict[str, dict] = {}
    for run in runs:
        name = run["route"] or "?"
        bucket = buckets.setdefault(
            name,
            {"route": name, "runs": 0, "ok": 0, "failed": 0,
             "seconds": [], "cost": 0.0, "currency": "", "sessions": []},
        )
        bucket["runs"] += 1
        bucket["ok" if run["ok"] else "failed"] += 1
        if run["seconds"] is not None:
            bucket["seconds"].append(run["seconds"])
        if isinstance(run["cost"], (int, float)):
            bucket["cost"] += float(run["cost"])
            bucket["currency"] = bucket["currency"] or run["currency"]
        if run["vendor_session"] and run["vendor_session"] not in bucket["sessions"]:
            bucket["sessions"].append(run["vendor_session"])
    out = []
    for bucket in buckets.values():
        seconds = bucket.pop("seconds")
        bucket["avg_seconds"] = round(sum(seconds) / len(seconds), 1) if seconds else None
        bucket["cost"] = round(bucket["cost"], 6) if bucket["cost"] else None
        out.append(bucket)
    out.sort(key=lambda b: (-b["runs"], b["route"]))
    return out


def for_task(task_id: str) -> dict:
    """Everything this task tried: which sessions, which runs, which routes, what won.

    This is the honest answer to "who should get this next": the routes are
    counted only from runs that actually happened, and a route with no runs is
    absent rather than assumed.
    """
    task = db.get_task(str(task_id or "").strip())
    if not task:
        return {}
    runs = db.list_runs(task["id"], limit=100000)
    marks = db.verify_states([r["id"] for r in runs])
    rows = [_run_summary(r, marks.get(r["id"], "none")) for r in runs]
    rows.sort(key=lambda r: r["started_at"])  # oldest first: this is a history
    session = db.get_session(task.get("session_id") or "")
    first_success = next((r["id"] for r in rows if r["ok"]), "")
    return {
        "task": task,
        "session": _node(session) if session else {},
        "sessions": chain(session["id"]) if session else [],
        "runs": rows,
        "routes": _route_summary(rows),
        "attempts": len(rows),
        "ok": sum(1 for r in rows if r["ok"]),
        "failed": sum(1 for r in rows if not r["ok"]),
        "first_success": first_success,
    }


# ---- one entry point for the CLI ---------------------------------------


def resolve(ref: str) -> tuple[str, str]:
    """Guess what an id names from its prefix, then check that it exists."""
    value = str(ref or "").strip()
    if not value:
        raise ValueError("empty id")
    prefix = value.split("-", 1)[0]
    table = {
        "ses": ("session", db.get_session),
        "run": ("run", db.get_run),
        "task": ("task", db.get_task),
    }
    if prefix in table:
        kind, lookup = table[prefix]
        if not lookup(value):
            raise ValueError(f"unknown {kind}: {value}")
        return kind, value
    # Not one of ours by prefix. It may still be a vendor conversation id, which
    # is what `nodus sessions` prints — resolve that to its latest sitting.
    row = session_for_vendor(value)
    if row:
        return "session", row["id"]
    for kind, lookup in (("session", db.get_session), ("run", db.get_run), ("task", db.get_task)):
        if lookup(value):
            return kind, value
    raise ValueError(
        f"unknown id: {value} (expected a ses-, run- or task- id, or a vendor session id)"
    )


def report(ref: str) -> dict:
    kind, ident = resolve(ref)
    if kind == "session":
        return {"kind": "session", **for_session(ident)}
    if kind == "run":
        return {"kind": "run", **for_run(ident)}
    return {"kind": "task", **for_task(ident)}


# ---- rendering ---------------------------------------------------------


def _when(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "?"


def _title(row: dict, width: int = 38) -> str:
    text = " ".join(str(row.get("title") or "").split())
    return f'"{text[:width]}"' if text else ""


def _session_line(row: dict, mark: bool = False) -> str:
    line = (
        f"{row.get('id', '?'):<16}  {(row.get('relation') or 'fresh'):<6}  "
        f"{row.get('origin_run_id') or '-':<16}  {_title(row)}"
    ).rstrip()
    return line + ("   ← here" if mark else "")


def _subtree(
    parent: str, children: dict[str, list[dict]], target: str, prefix: str, seen: set[str]
) -> list[str]:
    kids = [k for k in children.get(parent, []) if k["id"] not in seen]
    lines: list[str] = []
    for index, kid in enumerate(kids):
        last = index == len(kids) - 1
        lines.append(
            prefix + ("└─ " if last else "├─ ") + _session_line(kid, kid["id"] == target)
        )
        if len(seen) >= MAX_DEPTH * 4:
            continue
        seen.add(kid["id"])
        lines += _subtree(kid["id"], children, target, prefix + ("   " if last else "│  "), seen)
    return lines


def _render_session(data: dict) -> list[str]:
    session = data.get("session") or {}
    if not session:
        return ["(no session)"]
    root = (data.get("chain") or [session])[0]
    children: dict[str, list[dict]] = {}
    for row in data.get("descendants") or []:
        children.setdefault(row.get("parent_session_id") or "", []).append(row)
    lines = [_session_line(root, root["id"] == session["id"])]
    lines += _subtree(root["id"], children, session["id"], "", {root["id"]})
    return lines


def _cost(run: dict) -> str:
    cost = run.get("cost")
    if not isinstance(cost, (int, float)):
        return "-"
    return f"{cost:.4g} {run.get('currency') or ''}".strip()


def _render_run(data: dict) -> list[str]:
    run = data.get("run") or {}
    if not run:
        return ["(no run)"]
    task = data.get("task") or {}
    session = data.get("session") or {}
    usage = _json(run.get("usage_json"))
    verify = db.verify_states([run["id"]]).get(run["id"], "none")
    seconds = ""
    if run.get("ended_at") and run.get("started_at"):
        seconds = f"{run['ended_at'] - run['started_at']:.0f}s"
    lines = [
        f"run     {run['id']}  {run.get('status') or '?'}  route={run.get('route') or '?'}"
        f"  agent={run.get('agent') or '?'}  provider={run.get('provider') or '?'}"
        f"  verify={verify}  {seconds}  {_cost({'cost': usage.get('cost'), 'currency': usage.get('currency')})}",
        f"task    {task.get('id') or '?'}  {_title(task)}",
        f"session {session.get('id') or '-'}  {(session.get('relation') or '-'):<6}  {_title(session)}",
    ]
    chain_ids = " → ".join(s["id"] for s in (data.get("chain") or []))
    lines.append(f"chain   {chain_ids or '-'}")
    others = data.get("runs") or []
    lines.append(f"same conversation: {len(others)} other run(s), newest first")
    for row in others:
        lines.append(
            f"  {row['id']}  {(row.get('status') or '?'):<10}  {(row.get('route') or '?'):<8}"
            f"  {_when(row.get('started_at'))}  {_vendor(row) or '-'}"
        )
    proposals = data.get("proposals") or []
    lines.append(f"proposals: {len(proposals)}")
    for prop in proposals:
        branch = prop.get("branch") or ""
        files = len(prop.get("files") or [])
        lines.append(f"  {prop.get('id')}  {prop.get('status') or '?':<8}  {branch}  {files} file(s)")
    return lines


def _render_task(data: dict) -> list[str]:
    task = data.get("task") or {}
    if not task:
        return ["(no task)"]
    lines = [
        f"task     {task.get('id')}  {task.get('status') or '?'}  {_title(task, 60)}",
        f"attempts {data.get('attempts', 0)}  ok {data.get('ok', 0)}  failed {data.get('failed', 0)}",
    ]
    chain_rows = data.get("sessions") or []
    if chain_rows:
        lines.append("sessions " + " → ".join(s["id"] for s in chain_rows))
    lines.append("runs (oldest first):")
    for run in data.get("runs") or []:
        seconds = f"{run['seconds']:.0f}s" if run.get("seconds") is not None else "-"
        lines.append(
            f"  {run['id']}  {run['status']:<10} {'ok' if run['ok'] else 'no'}  "
            f"route={run['route'] or '?':<8} verify={run['verify']:<5} {seconds:>5}  "
            f"{_cost(run)}  {run['vendor_session'] or '-'}"
        )
    lines.append("routes:")
    for route in data.get("routes") or []:
        avg = f"{route['avg_seconds']:.0f}s" if route.get("avg_seconds") is not None else "-"
        lines.append(
            f"  {route['route']:<10} {route['runs']} run(s)  {route['ok']} ok  "
            f"{route['failed']} failed  avg {avg}  {_cost(route)}"
        )
    return lines


def render(data: dict) -> str:
    kind = data.get("kind")
    if kind == "session":
        lines = _render_session(data)
    elif kind == "run":
        lines = _render_run(data)
    elif kind == "task":
        lines = _render_task(data)
    else:
        lines = [f"(unknown report: {kind})"]
    return "\n".join(lines)
