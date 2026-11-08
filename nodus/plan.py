"""Versioned plan contract, mechanical validation, and SQLite storage."""

from __future__ import annotations
import hashlib, json, math, os, re, shlex, subprocess
from pathlib import PureWindowsPath
from pathlib import Path
from . import db, config

HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")


def is_command(value):
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        return False
    try:
        tokens = shlex.split(value, posix=False)
    except ValueError:
        return False
    if not tokens:
        return False
    executable = tokens[0].strip('"')
    return bool(
        re.fullmatch(r"[A-Za-z0-9_./:\\-]+", executable)
    ) and not executable.startswith("-")


def _validate_plan(plan: dict, cwd: str | os.PathLike = ".") -> list[dict]:
    errors = []
    root = Path(cwd).resolve()
    if not isinstance(plan, dict):
        return [{"kind": "schema", "path": "$", "message": "must be an object"}]

    def err(kind, path, message):
        errors.append({"kind": kind, "path": path, "message": message})

    if type(plan.get("schema_version")) is not int or plan.get("schema_version") != 1:
        err("schema", "schema_version", "must be 1")
    if not isinstance(plan.get("goal"), str) or not plan["goal"].strip():
        err("goal", "goal", "must be non-empty")
    sha = plan.get("base_sha", "")
    if not isinstance(sha, str) or not HEX40.fullmatch(sha):
        err("base_sha", "base_sha", "must be 40 hex characters")
    else:
        try:
            result = subprocess.run(
                ["git", "cat-file", "-e", sha + "^{commit}"],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if result.returncode:
                err("base_sha", "base_sha", "commit is not resolvable")
        except (OSError, subprocess.TimeoutExpired):
            err("base_sha", "base_sha", "repository unavailable")

    def text(value):
        return isinstance(value, str) and bool(value.strip())

    def strings(value, nonempty=False):
        return (
            isinstance(value, list)
            and (bool(value) or not nonempty)
            and all(text(x) for x in value)
        )

    for key in ("non_goals", "risks", "review_points"):
        if not strings(plan.get(key), key != "non_goals"):
            err("schema", key, "must be a string array")
    if not text(plan.get("rollback")):
        err("schema", "rollback", "must be non-empty text")
    for key in ("nodes", "milestones"):
        if not isinstance(plan.get(key), list):
            err("schema", key, "must be an array")
    recon = plan.get("recon") if isinstance(plan.get("recon"), dict) else {}
    if not isinstance(plan.get("recon"), dict):
        err("schema", "recon", "must be an object")
    for key in ("files", "tests"):
        if not strings(recon.get(key)):
            err("schema", "recon." + key, "must be a string array")
    agent_hash = recon.get("agents_md")
    if "agents_md" not in recon or (
        agent_hash is not None
        and (
            not isinstance(agent_hash, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", agent_hash)
        )
    ):
        err("schema", "recon.agents_md", "must be sha256 or null")
    scopes = list(recon.get("files", []) or [])
    nodes = plan.get("nodes", []) or []
    for i, n in enumerate(nodes):
        scopes += list(n.get("file_scope", []) or [])
    for p in scopes:
        if (
            not isinstance(p, str)
            or not p
            or PureWindowsPath(p).drive
            or Path(p).is_absolute()
            or ".." in PureWindowsPath(p).parts
            or not (root / p).resolve().is_relative_to(root)
            or not (root / p).exists()
        ):
            err("path", str(p), "relative path does not exist")
    milestones = plan.get("milestones", []) or []
    mids = {m.get("id") for m in milestones}
    seen = set()
    for m in milestones:
        if not text(m.get("id")) or m["id"] in seen:
            err("milestone", "milestones", "ids must be unique non-empty strings")
        seen.add(m.get("id"))
        if not text(m.get("title")) or not strings(m.get("exit_criteria"), True):
            err("milestone", str(m.get("id")), "title and exit criteria required")
    ids = [n.get("id") for n in nodes]
    for x in ids:
        if ids.count(x) > 1:
            err("duplicate_id", str(x), "node id is duplicated")
    graph = {x: [] for x in ids}
    for n in nodes:
        for key in ("id", "title", "description", "route_hint"):
            if not text(n.get(key)):
                err("schema", key, "must be non-empty text")
        for key in ("deps", "file_scope"):
            if not strings(n.get(key)):
                err("schema", key, "must be a string array")
        if n.get("milestone") not in mids:
            err("milestone", str(n.get("id")), "milestone reference is missing")
        for d in n.get("deps", []) or []:
            if d not in graph:
                err("dependency", str(n.get("id")), "dependency reference is missing")
            else:
                graph[n["id"]].append(d)
        acc = n.get("acceptance", []) or []
        if (
            not isinstance(acc, list)
            or not acc
            or not all(isinstance(a, str) and a.strip() and is_command(a) for a in acc)
        ):
            err(
                "acceptance",
                str(n.get("id")),
                "acceptance must contain executable commands",
            )
        if type(n.get("difficulty")) is not int or not 1 <= n["difficulty"] <= 5:
            err("difficulty", str(n.get("id")), "difficulty must be 1..5")
        if (
            type(n.get("budget_cny")) not in (int, float)
            or not math.isfinite(n["budget_cny"])
            or n["budget_cny"] < 0
        ):
            err("budget", str(n.get("id")), "budget_cny must be non-negative")
    state = {}

    def visit(x):
        if state.get(x) == 1:
            return True
        if state.get(x) == 2:
            return False
        state[x] = 1
        cyc = any(visit(y) for y in graph.get(x, []))
        state[x] = 2
        return cyc

    if any(visit(x) for x in graph):
        err("cycle", "nodes", "dependencies contain a cycle")
    if not milestones:
        err("milestone", "milestones", "at least one milestone required")
    for m in milestones:
        if not any(n.get("milestone") == m.get("id") for n in nodes):
            err("milestone", str(m.get("id")), "milestone has no nodes")
    for key in ("risks", "review_points", "rollback"):
        if not plan.get(key):
            err("metadata", key, "must be non-empty")
    return errors


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def import_plan(value, cwd="."):
    plan = load(value) if isinstance(value, (str, Path)) else value
    errors = validate_plan(plan, cwd)
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    raw = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    pid = db.new_id("plan")
    now = db.now()
    with db.connect() as c:
        c.execute(
            "INSERT INTO plan VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pid,
                config.workspace_id(),
                None,
                1,
                None,
                "validated",
                plan["base_sha"],
                raw,
                hashlib.sha256(raw.encode()).hexdigest(),
                "[]",
                now,
                now,
                None,
                None,
            ),
        )
        for n in plan["nodes"]:
            c.execute(
                "INSERT INTO plan_node VALUES(?,?,?,?,?)",
                (pid, n["id"], n.get("title", ""), "pending", None),
            )
    return pid


def get_plan(pid):
    with db.connect() as c:
        p = c.execute(
            "SELECT * FROM plan WHERE id=? AND workspace_id=?",
            (pid, config.workspace_id()),
        ).fetchone()
        if not p:
            return None
        out = dict(p)
        out["plan_json"] = json.loads(out["plan_json"])
        out["nodes"] = [
            dict(x)
            for x in c.execute("SELECT * FROM plan_node WHERE plan_id=?", (pid,))
        ]
        return out


def list_plans():
    with db.connect() as c:
        return [
            dict(x)
            for x in c.execute(
                "SELECT id,status,version,created_at FROM plan WHERE workspace_id=? ORDER BY created_at DESC",
                (config.workspace_id(),),
            )
        ]


def validate_plan(plan, cwd="."):
    """Return structured errors; never execute acceptance or mutate repository."""
    try:
        return _validate_plan(plan, cwd)
    except (
        TypeError,
        ValueError,
        AttributeError,
        KeyError,
        OSError,
        RecursionError,
    ) as exc:
        return [
            {"kind": "schema", "path": "$", "message": f"invalid plan structure: {exc}"}
        ]
