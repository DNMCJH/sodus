"""Versioned plan contract, mechanical validation, and SQLite storage."""

from __future__ import annotations
import time
import shutil, tempfile
import hashlib, json, logging, math, os, re, shlex, subprocess
from pathlib import PureWindowsPath
from pathlib import Path
from . import db, config

PLAN_CONTRACT = """JSON 契约

顶层对象字段：schema_version（整数 1）、goal（非空字符串）、non_goals（字符串数组）、
base_sha（目标仓库可解析的 40 位十六进制 commit）、recon（对象）、milestones（非空数组）、
nodes（数组）、risks（非空字符串数组）、rollback（非空字符串）、review_points（非空字符串数组）。
recon 包含 files（相对路径数组）、tests（命令数组）、agents_md（sha256 或 null）。
milestone 包含 id（唯一非空字符串）、title（非空字符串）、exit_criteria（非空字符串数组）。
node 包含 id（唯一非空字符串）、milestone（已定义的 milestone id）、title、description（非空字符串）、
deps（node id 数组）、file_scope（相对路径数组）、acceptance（非空命令数组）、
difficulty（整数 1–5）、route_hint（非空字符串，例如 sol）、budget_cny（有限非负数字）。

路径必须已存在于 cwd 内，允许目录，禁止绝对路径、上级逃逸和指向外部的符号链接。
新文件应把已存在的父目录列入 file_scope；校验不会创建文件。
deps 必须构成 DAG，每个 milestone 必须有节点。
acceptance 只做命令语法检查，拒绝“完成即可”等自然语言；不会执行，也不代表验收已通过。
可接受 python/pytest/npm 等常见命令或显式相对/绝对可执行路径及 shell 命令。
不检查命令是否安装、不调用模型、不执行 acceptance。

"""


def _plan_contract():
    return PLAN_CONTRACT

HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
COMMANDS = {"python", "python3", "pytest", "pnpm", "npm", "node", "go", "cargo", "dotnet", "make", "bash", "pwsh", "powershell"}


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
    if not re.fullmatch(r"[A-Za-z0-9_./:\\-]+", executable) or executable.startswith("-"):
        return False
    return Path(executable).is_absolute() or "/" in executable or "\\" in executable or executable.lower() in COMMANDS


def _validate_plan(plan: dict, cwd: str | os.PathLike = ".", warnings=None) -> list[dict]:
    errors = []
    root = Path(cwd).resolve()
    if not isinstance(plan, dict):
        return [{"kind": "schema", "path": "$", "message": "must be an object"}]

    def err(kind, path, message):
        errors.append({"kind": kind, "path": path, "message": message})

    def warn(kind, path, message):
        if warnings is not None:
            warnings.append({"kind": kind, "path": path, "message": message})

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
                creationflags=config.NO_WINDOW,
            )
            if result.returncode:
                err("base_sha", "base_sha", "commit is not resolvable")
            else:
                head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                      text=True, timeout=10, check=False, creationflags=config.NO_WINDOW)
                if head.returncode == 0 and head.stdout.strip().lower() != sha.lower():
                    warn("base_sha", "base_sha", "differs from target repository HEAD")
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
    elif agent_hash is not None:
        agents_path = recon.get("agents_md_path", "AGENTS.md")
        if not isinstance(agents_path, str) or not agents_path.strip() or Path(agents_path).is_absolute() or ".." in Path(agents_path).parts:
            err("path", "recon.agents_md_path", "must be a relative repository path")
        else:
            target = root / agents_path
            if not target.resolve().is_relative_to(root):
                err("path", "recon.agents_md_path", "must remain inside repository")
            elif not target.is_file():
                warn("agents_md", "recon.agents_md", f"{agents_path} is missing")
            elif hashlib.sha256(target.read_bytes()).hexdigest().lower() != agent_hash.lower():
                warn("agents_md", "recon.agents_md", f"{agents_path} sha256 mismatch")
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
    warnings = []
    errors = validate_plan(plan, cwd, warnings)
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    raw = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    pid = db.new_id("plan")
    now = db.now()
    with _connect_meta() as c:
        c.execute(
            "INSERT INTO plan (id,workspace_id,task_id,version,parent_id,status,base_sha,plan_json,hash,errors_json,created_at,validated_at,approved_by,approved_at,warnings_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                json.dumps(warnings),
            ),
        )
        c.execute('INSERT INTO plan_execution(plan_id,cwd) VALUES(?,?)', (pid, str(Path(cwd).resolve())))
        for n in plan["nodes"]:
            c.execute(
                "INSERT INTO plan_node VALUES(?,?,?,?,?)",
                (pid, n["id"], n.get("title", ""), "pending", None),
            )
    return pid


def get_plan(pid):
    with _connect_meta() as c:
        p = c.execute(
            "SELECT * FROM plan WHERE id=? AND workspace_id=?",
            (pid, config.workspace_id()),
        ).fetchone()
        if not p:
            return None
        out = dict(p)
        out["plan_json"] = json.loads(out["plan_json"])
        meta = c.execute('SELECT * FROM plan_execution WHERE plan_id=?', (pid,)).fetchone()
        out.update(dict(meta) if meta else dict(run_id=None, review_run_id=None, review_json=None, cwd=None))
        if out.get('review_json'):
            out['review_json'] = json.loads(out['review_json'])
        out["nodes"] = [
            dict(x)
            for x in c.execute("SELECT * FROM plan_node WHERE plan_id=?", (pid,))
        ]
        return out


def list_plans():
    with _connect_meta() as c:
        return [
            dict(x)
            for x in c.execute(
                "SELECT p.id,p.status,p.version,p.created_at,e.run_id,e.review_json FROM plan p LEFT JOIN plan_execution e ON e.plan_id=p.id WHERE workspace_id=? ORDER BY created_at DESC",
                (config.workspace_id(),),
            )
        ]


def validate_plan(plan, cwd=".", warnings=None):
    """Return structured errors; never execute acceptance or mutate repository."""
    try:
        return _validate_plan(plan, cwd, warnings)
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

# Additive execution metadata keeps the c1 plan contract and schema version intact.
def _connect_meta():
    return db.connect()


def _update(pid, **fields):
    with db.connect() as c:
        c.execute('UPDATE plan SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?',
                  (*fields.values(), pid))


_planner_workspaces = {}


def _isolated_cwd(cwd):
    root = Path(tempfile.mkdtemp(prefix='sodus-plan-'))
    target = root / 'workspace'
    git = False

    def cleanup():
        try:
            if git:
                subprocess.run(['git', 'worktree', 'remove', '--force', str(target)],
                    cwd=cwd, capture_output=True, timeout=30, creationflags=config.NO_WINDOW)
        finally:
            shutil.rmtree(root)

    try:
        probe = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=cwd,
            capture_output=True, text=True, timeout=10, creationflags=config.NO_WINDOW)
        if probe.returncode == 0:
            git = True
            subprocess.run(['git', 'worktree', 'add', '--detach', str(target), 'HEAD'],
                cwd=cwd, capture_output=True, check=True, timeout=60, creationflags=config.NO_WINDOW)
            relative = Path(cwd).resolve().relative_to(Path(probe.stdout.strip()).resolve())
            workdir = target / relative
        else:
            shutil.copytree(cwd, target, ignore=lambda directory, names: [
                name for name in names if name in {
                    '.git', '.venv', 'venv', 'node_modules', '__pycache__',
                    '.pytest_cache', '.mypy_cache', '.ruff_cache', 'cache',
                    '.cache', 'checkpoints'} or (Path(directory) / name).is_symlink()])
            workdir = target
        return str(workdir), cleanup
    except Exception:
        cleanup()
        raise


def _cleanup_planner(rid):
    cleanup = _planner_workspaces.pop(rid, None)
    if cleanup:
        try:
            cleanup()
        except Exception:
            logging.getLogger(__name__).exception('planner workspace cleanup failed: %s', rid)


def _start_planner_run(prompt, cwd, route, kind='plan'):
    from . import runner
    cleanup = None
    try:
        isolated, cleanup = _isolated_cwd(cwd)
    except Exception:
        logging.getLogger(__name__).warning(
            'planner isolation failed; falling back to original cwd, readonly is not guaranteed: %s', cwd,
            exc_info=True)
        isolated = cwd
    try:
        task = db.create_task(prompt)
        run = runner.RUNNER.start(task['id'], route_name=route, cwd=isolated, kind=kind,
            note='plan-only, do not write files', verify_run=False, readonly=True,
            include_handoff=False, include_memory=False)
        if cleanup:
            _planner_workspaces[run['id']] = cleanup
        return run
    except Exception:
        if cleanup:
            cleanup()
        raise


def _extract_json(rid):
    messages = [e['payload']['text'] for e in db.events_filtered(rid, ('message',))
                if e['type'] == 'message' and isinstance(e['payload'].get('text'), str)
                and e['payload'].get('role', 'assistant') == 'assistant']
    if not messages:
        raise ValueError('no assistant message')
    text = messages[-1].strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s*```$', '', text)
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError('last assistant message must contain one JSON object')
    return result


def _wait_run(rid, timeout=None):
    from . import runner
    deadline = time.monotonic() + (timeout if timeout is not None else config.plan_timeout())
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            runner.RUNNER.stop(rid)
            db.update_run(rid, status='failed', ended_at=db.now(), error='planning deadline exceeded')
            raise TimeoutError('planning deadline exceeded')
        run = runner.RUNNER.wait(rid, timeout=min(remaining, 1), poll=min(remaining, 0.1))
        if time.monotonic() >= deadline:
            runner.RUNNER.stop(rid)
            db.update_run(rid, status='failed', ended_at=db.now(), error='planning deadline exceeded')
            raise TimeoutError('planning deadline exceeded')
        if run['status'] == 'completed':
            return
        if run['status'] not in ('queued', 'running'):
            raise ValueError('run failed: ' + str(run.get('error') or run['status']))


def _finish_create(pid):
    row = None
    try:
        row = get_plan(pid)
        _wait_run(row['run_id'])
        value = _extract_json(row['run_id'])
        warnings = []
        errors = validate_plan(value, row['cwd'], warnings)
        if errors:
            _update(pid, status='failed', errors_json=json.dumps(errors))
            return
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        with db.connect() as c:
            c.execute('UPDATE plan SET status=?,base_sha=?,plan_json=?,hash=?,errors_json=?,validated_at=? WHERE id=?',
                      ('validated', value['base_sha'], raw, hashlib.sha256(raw.encode()).hexdigest(), json.dumps(warnings), db.now(), pid))
            for n in value['nodes']:
                c.execute('INSERT INTO plan_node VALUES(?,?,?,?,?)', (pid, n['id'], n['title'], 'pending', None))
    except Exception as exc:
        logging.getLogger(__name__).exception('plan background completion failed: %s', pid)
        _update(pid, status='failed', errors_json=json.dumps([dict(kind='planner', message=str(exc))]))

    finally:
        if row:
            _cleanup_planner(row['run_id'])


def create(goal, cwd='.', acceptance='', route=None, wait=True):
    """Start a fresh, retryable plan attempt; return its persisted view."""
    import threading
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError('goal required')
    if not isinstance(acceptance, str) or (route is not None and not isinstance(route, str)):
        raise ValueError('invalid acceptance or route')
    target = str(Path(cwd).resolve())
    if not Path(target).is_dir():
        raise ValueError('cwd must be an existing directory')
    pid = db.new_id('plan')
    with _connect_meta() as c:
        c.execute('INSERT INTO plan (id,workspace_id,task_id,version,parent_id,status,base_sha,plan_json,hash,errors_json,created_at,validated_at,approved_by,approved_at,warnings_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (pid, config.workspace_id(), None, 1, None, 'draft', None, '{}', None, '[]', db.now(), None, None, None, '[]'))
        c.execute('INSERT INTO plan_execution(plan_id,cwd) VALUES(?,?)', (pid, target))
    try:
        schema = _plan_contract()
        prompt = ('Read-only planning only. Do not write files, commit, or execute acceptance commands.\n'
                  'Reconnaissance required: git rev-parse HEAD; git ls-files; read AGENTS.md and test commands. '
                  'Reference real repository-relative paths.\nGoal: ' + goal + '\nConstraints / acceptance: ' + acceptance +
                  '\nThe last assistant message must contain only one JSON object, no fences or explanation. Schema:\n' + schema)
        run = _start_planner_run(prompt, target, route or config.planner_route())
        with _connect_meta() as c:
            c.execute('UPDATE plan_execution SET run_id=? WHERE plan_id=?', (run['id'], pid))
        _update(pid, status='planning')
        if wait:
            _finish_create(pid)
        else:
            row = get_plan(pid)
            threading.Thread(target=_finish_create, args=(pid,), daemon=True).start()
            return row
    except Exception as exc:
        _update(pid, status='failed', errors_json=json.dumps([dict(kind='planner', message=str(exc))]))
    return get_plan(pid)


def review(plan_id):
    """Launch review asynchronously and return the reviewing plan immediately."""
    import threading
    row = get_plan(plan_id)
    if row is None:
        raise KeyError(plan_id)
    if row['status'] not in ('validated', 'reviewed'):
        raise ValueError('review requires a validated plan')
    if not row.get('cwd'):
        raise ValueError('plan has no repository context; create a planner attempt first')
    prompt = ('Read-only independent review. Do not write files or run acceptance commands. '
              'Inspect repository context, AGENTS.md, git rev-parse HEAD and git ls-files. '
              'Last assistant message must be JSON only: {"verdict":"approve|revise|reject","findings":[]}.\n'
              + json.dumps(row['plan_json'], ensure_ascii=False))
    _update(plan_id, status='reviewing', errors_json='[]')
    with _connect_meta() as c:
        c.execute('UPDATE plan_execution SET review_json=NULL WHERE plan_id=?', (plan_id,))
    run = _start_planner_run(prompt, row['cwd'], 'sol', kind='plan_review')
    with _connect_meta() as c:
        c.execute('UPDATE plan_execution SET review_run_id=?,review_json=NULL WHERE plan_id=?', (run['id'], plan_id))
    threading.Thread(target=_finish_review, args=(plan_id, run['id']), daemon=True).start()
    return get_plan(plan_id)


def _finish_review(plan_id, run_id):
    try:
        _wait_run(run_id, timeout=config.review_timeout())
        result = _extract_json(run_id)
        if result.get('verdict') not in ('approve', 'revise', 'reject') or not isinstance(result.get('findings'), list):
            raise ValueError('invalid review verdict/findings')
        with _connect_meta() as c:
            c.execute('UPDATE plan_execution SET review_json=? WHERE plan_id=?', (json.dumps(result), plan_id))
            c.execute("UPDATE plan SET status='reviewed',errors_json='[]' WHERE id=?", (plan_id,))
    except Exception as exc:
        logging.getLogger(__name__).exception('plan review failed: %s', plan_id)
        _update(plan_id, status='review_failed', errors_json=json.dumps([dict(kind='review', message=str(exc))]))

    finally:
        _cleanup_planner(run_id)


def approve(plan_id, by):
    row = get_plan(plan_id)
    if row is None:
        raise KeyError(plan_id)
    if by != 'human':
        raise ValueError('approval requires explicit by=human')
    if row['status'] != 'reviewed' or (row.get('review_json') or {}).get('verdict') != 'approve':
        raise ValueError('approval requires review verdict=approve (revise/reject cannot be approved)')
    raw = json.dumps(row['plan_json'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if hashlib.sha256(raw.encode()).hexdigest() != row['hash']:
        raise ValueError('plan hash mismatch')
    warnings = []
    errors = validate_plan(row['plan_json'], row['cwd'], warnings)
    agents_path = Path(row['cwd']) / row['plan_json']['recon'].get('agents_md_path', 'AGENTS.md')
    if agents_path.exists() and row['plan_json']['recon'].get('agents_md') is None:
        raise ValueError('AGENTS.md hash required')
    if errors or warnings:
        raise ValueError('approval validation failed: ' + json.dumps(errors + warnings))
    _update(plan_id, status='approved', approved_by=by, approved_at=db.now())
    return get_plan(plan_id)


def reconcile():
    """Startup owns no previous planning collector, even if its run survived."""
    failed = []
    with _connect_meta() as c:
        rows = c.execute("SELECT id FROM plan WHERE status='planning'").fetchall()
        for row in rows:
            errors = json.dumps([dict(kind='planner', message='daemon restarted; orphaned planning collector')])
            c.execute("UPDATE plan SET status='failed',errors_json=? WHERE id=? AND status='planning'", (errors, row['id']))
            failed.append(row['id'])
    return failed
