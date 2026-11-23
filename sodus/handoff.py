"""Project handoff consoles and project-level decision inbox.

Only explicit local CLI commands write the console file; HTTP and MCP remain
read-only projections over the same Markdown file and SQLite metadata.
"""
from __future__ import annotations

from contextlib import closing, contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

from . import config, ledger

START = '<!-- sodus:auto:start -->'
END = '<!-- sodus:auto:end -->'
SCHEMA_VERSION = 2
SECTIONS = (
    '项目一句话',
    '当前唯一优先',
    '硬约束/禁止事项',
    '实时状态',
    '关键路径',
    '决策记录',
    '进度节点',
    '未决/风险',
    '下一步',
    '最近变更',
    '更新规则',
)

# Section heading aliases supported across legacy consoles and hand-authored templates.
PENDING_ALIASES = ('未决/风险', '未决与风险', '未决 / 待你拍板', '未决')
DECISION_LOG_ALIASES = ('决策记录', '决策记录（已定，别重复问）')
STATUS_ALIASES = ('实时状态', '实时状态（机器段）')

_DECISION_HEADING_RE = re.compile(r'^###\s+(D-\d{3,})\s+(.+?)\s*$', re.M)
_DECISION_ID_RE = re.compile(r'\bD-(\d{3,})\b')
_LOCK_STALE_SECONDS = 30.0
_LOCK_WAIT_SECONDS = 5.0


def git(root, *args):
    try:
        p = subprocess.run(
            ['git', '-C', str(root), *args],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=config.NO_WINDOW,
        )
        return p.stdout.strip() if p.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def project_root(cwd=None):
    path = Path(cwd or Path.cwd()).expanduser().resolve()
    if not path.is_dir():
        raise ValueError('cwd must be an existing directory')
    return Path(git(path, 'rev-parse', '--show-toplevel') or path).resolve()


def _stored_root(raw_cwd, cache: dict[str, Path | None] | None = None) -> Path | None:
    """Resolve a persisted DB cwd strictly.

    Relative paths such as '.' are rejected so historical test rows or
    daemon-relative runs can never be misattributed to whichever project runs
    `sodus handoff sync`.
    """
    if not isinstance(raw_cwd, str) or not raw_cwd.strip():
        return None
    key = raw_cwd.strip()
    if cache is not None and key in cache:
        return cache[key]
    try:
        path = Path(key).expanduser()
        if not path.is_absolute() or not path.is_dir():
            result = None
        else:
            result = project_root(path)
    except (ValueError, OSError):
        result = None
    if cache is not None:
        cache[key] = result
    return result


def _is_daemon_home(root: Path) -> bool:
    try:
        return root.resolve() == config.HOME.resolve()
    except OSError:
        return False


def console_path(root):
    path = Path(config.load_config().get('handoff', {}).get('path', 'HANDOFF.md')).expanduser()
    return (path if path.is_absolute() else Path(root) / path).resolve()


def registered_projects() -> list[dict]:
    """Optional explicit project registry (`~/.sodus/projects.json`)."""
    reg = config.HOME / 'projects.json'
    if not reg.is_file():
        return []
    try:
        raw = json.loads(reg.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return []
    items = raw.get('projects') if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get('active', True) is False:
            continue
        raw_root = item.get('root') or item.get('path')
        root = _stored_root(raw_root) if raw_root else None
        if root is None:
            continue
        out.append(dict(item, resolved_root=root))
    return out


def records():
    """Read existing storage without creating or migrating it."""
    path = config.HOME / 'sodus.sqlite3'
    if not path.is_file():
        return []
    rows = []
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            for table in ('task', 'run'):
                cols = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
                if 'cwd' in cols:
                    query = f'SELECT * FROM {table}'
                    params = ()
                    if 'workspace_id' in cols:
                        query += ' WHERE workspace_id=?'
                        params = (config.workspace_id(),)
                    rows.extend(dict(r, _table=table) for r in conn.execute(query, params))
            # Tasks/plans inherit their project from recorded run cwd; neither
            # table has a cwd column in the current schema.
            task_cwds = {(r.get('task_id'), r.get('cwd')) for r in rows if r['_table'] == 'run'}
            for table in ('task', 'plan'):
                cols = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
                if not cols or 'cwd' in cols:
                    continue
                for row in conn.execute(f'SELECT * FROM {table} WHERE workspace_id=?', (config.workspace_id(),)):
                    data = dict(row)
                    for task_id, cwd in task_cwds:
                        if task_id == data.get('id' if table == 'task' else 'task_id'):
                            rows.append(dict(data, cwd=cwd, _table=table))
    except sqlite3.Error:
        return []
    return rows


def known_roots(rows: list[dict] | None = None) -> list[Path]:
    """Deduplicated set of known project roots, skipping relative cwds and daemon HOME."""
    reg = registered_projects()
    roots: set[Path] = set()
    try:
        cur = project_root()
        if not _is_daemon_home(cur):
            roots.add(cur)
    except (ValueError, OSError):
        pass
    for item in reg:
        roots.add(item['resolved_root'])
    cache: dict[str, Path | None] = {}
    source_rows = records() if rows is None else rows
    # Deduplicate raw cwd strings before spawning any git rev-parse subprocess.
    unique_cwds = {
        str(r.get('cwd')).strip()
        for r in source_rows
        if isinstance(r.get('cwd'), str) and r.get('cwd').strip()
    }
    for raw_cwd in unique_cwds:
        root = _stored_root(raw_cwd, cache=cache)
        if root is not None and not _is_daemon_home(root):
            roots.add(root)
    return sorted(roots, key=str)


def allowed_root(cwd=None, allowed: set[Path] | list[Path] | None = None) -> Path:
    root = project_root(cwd)
    allowed_set = set(known_roots()) if allowed is None else set(allowed)
    if root not in allowed_set:
        raise ValueError('cwd outside allowed project roots')
    return root


def field(content, name):
    match = re.search(r'^' + re.escape(name) + r':[^\S\r\n]*([^\r\n]*)', content, re.M)
    return match.group(1).strip().strip("\"'") if match else ''


def show(cwd=None, remote=False, allowed: set[Path] | list[Path] | None = None):
    root = allowed_root(cwd, allowed=allowed) if remote else project_root(cwd)
    path = console_path(root)
    configured = Path(config.load_config().get('handoff', {}).get('path', 'HANDOFF.md')).expanduser()
    if remote and not configured.is_absolute() and not path.is_relative_to(root):
        raise ValueError('handoff path escapes project root')
    with path.open(encoding='utf-8', newline='') if path.is_file() else nullcontext(None) as stream:
        content = stream.read() if stream else ''
    return dict(
        path=str(path),
        project_root=str(root),
        exists=path.is_file(),
        snapshot_at=field(content, 'snapshot_at'),
        head=field(content, 'head'),
        content=content,
    )


def _content_hash(path: Path) -> str:
    if not path.is_file():
        return ''
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def _file_lock(path: Path):
    """Cross-process exclusive lock file beside HANDOFF.md with stale recovery."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / (path.name + '.lock')
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    fd = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, f'{os.getpid()}:{time.time()}\n'.encode('utf-8'))
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > _LOCK_STALE_SECONDS:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f'handoff file locked: {lock_path}')
            time.sleep(0.02)
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write(path, text, *, expected_hash: str | None = None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if expected_hash is not None and _content_hash(path) != expected_hash:
        raise RuntimeError('handoff changed concurrently; retry')
    fd, temp = tempfile.mkstemp(prefix='.handoff-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def _detect_newline(text: str) -> str:
    return '\r\n' if '\r\n' in text else '\n'


def _find_section(content: str, names: tuple[str, ...]) -> tuple[re.Match | None, int, int]:
    """Locate a level-2 heading matching any alias (with optional numeric prefix like `## 4. `)."""
    for name in names:
        pattern = r'^##\s+(?:\d+\.\s*)?' + re.escape(name) + r'(?:（[^）\r\n]*）)?[^\S\r\n]*(?:\r?\n|$)'
        match = re.search(pattern, content, re.M)
        if match:
            next_heading = re.search(r'^##\s+', content[match.end():], re.M)
            end = match.end() + next_heading.start() if next_heading else len(content)
            return match, match.end(), end
    return None, -1, -1


def _touch_snapshot(content: str, now_iso: str, newline: str) -> str:
    if re.search(r'^snapshot_at:', content, re.M):
        content = re.sub(r'^snapshot_at:.*$', f'snapshot_at: "{now_iso}"', content, count=1, flags=re.M)
    if re.search(r'^updated:', content, re.M):
        content = re.sub(r'^updated:.*$', f'updated: {now_iso[:10]}', content, count=1, flags=re.M)
    return content


def init(cwd=None, force=False):
    root = project_root(cwd)
    path = console_path(root)
    with _file_lock(path):
        if path.exists() and not force:
            raise ValueError('handoff already exists; use --force')
        now = stamp()
        text = (
            f'---\ntitle: "{root.name} 项目接力控制台"\ndate: {now[:10]}\n'
            f'updated: {now[:10]}\nversion: 1\ntype: project-handoff\nstatus: active\n'
            f'snapshot_at: "{now}"\ntags: [workflow/handoff]\n---\n\n# 项目接力控制台\n\n'
        )
        for section in SECTIONS:
            text += f'## {section}\n\n'
            if section == '实时状态':
                text += START + '\n' + END + '\n\n'
            elif section == '关键路径':
                text += '- `.`\n\n'
            elif section == '决策记录':
                text += '| 日期 | 编号 | 决定 | 依据 | 状态 |\n|---|---|---|---|---|\n\n'
            elif section == '更新规则':
                text += '- 人工维护决策和下一步；sync 仅刷新机器段，不复制长文或密钥。\n\n'
        atomic_write(path, text)
    return show(root)


def append(text, section='最近变更', cwd=None):
    root = project_root(cwd)
    path = console_path(root)
    with _file_lock(path):
        data = show(root)
        if not data['exists']:
            raise ValueError('handoff missing; run init')
        if not text.strip() or '\n' in section or section == '实时状态':
            raise ValueError('invalid text or section')
        before_hash = _content_hash(path)
        content = data['content']
        match, _, end = _find_section(content, (section,))
        if not match:
            raise ValueError('section not found')
        bullet = f'- {stamp()[:10]} ' + text.replace('\n', '\n  ') + '\n\n'
        content = content[:end].rstrip() + '\n\n' + bullet + content[end:]
        content = _touch_snapshot(content, stamp(), _detect_newline(content))
        atomic_write(path, content, expected_hash=before_hash)
    return show(root)


# ---- project-level decision inbox --------------------------------------


def _parse_decision_block(did: str, title: str, body: str) -> dict:
    fields: dict[str, str] = {}
    for line in body.splitlines():
        m = re.match(r'^-\s*\*\*([^*]+?)\*\*[：:]\s*(.*)$', line.strip())
        if not m:
            m = re.match(r'^-\s*([^：:]+?)[：:]\s*(.*)$', line.strip())
        if m:
            fields[m.group(1).strip()] = m.group(2).strip()
    raw_status = (fields.get('状态') or 'open').strip().lower()
    if raw_status.startswith(('deferred', '降级', '暂缓')):
        status = 'deferred'
    elif raw_status.startswith(('decided', 'closed', '已定')):
        status = 'decided'
    else:
        status = 'open'
    return {
        'id': did,
        'title': title.strip(),
        'why': fields.get('为什么轮到你', ''),
        'background': fields.get('背景', ''),
        'options': fields.get('选项', ''),
        'recommend': fields.get('推荐', fields.get('推荐与理由', '')),
        'evidence': fields.get('证据', ''),
        'wait_cost': fields.get('等待代价', fields.get('等待的代价', '')),
        'status': status,
        'status_raw': fields.get('状态', 'open'),
        'created_at': fields.get('创建日期', ''),
    }


def parse_decisions(content: str) -> list[dict]:
    """Parse `### D-xxx <title>` entries inside the pending-decisions section."""
    _, start, end = _find_section(content, PENDING_ALIASES)
    if start < 0:
        return []
    section = content[start:end]
    matches = list(_DECISION_HEADING_RE.finditer(section))
    out: list[dict] = []
    for idx, match in enumerate(matches):
        block_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(section)
        body = section[match.end():block_end].strip()
        out.append(_parse_decision_block(match.group(1), match.group(2), body))
    return out


def _next_decision_id(content: str) -> str:
    nums = [int(m.group(1)) for m in _DECISION_ID_RE.finditer(content)]
    return f'D-{(max(nums) + 1) if nums else 1:03d}'


def _escape_cell(value: str) -> str:
    return ' '.join(str(value or '').replace('|', '｜').splitlines()).strip()


def decide_draft(
    title: str,
    *,
    why: str = '',
    background: str = '',
    options: list[str] | tuple[str, ...] = (),
    recommend: str = '',
    evidence: str = '',
    wait_cost: str = '',
    cwd=None,
) -> dict:
    """Append a structured `### D-xxx` decision item to `## 未决/风险` under lock."""
    title = str(title or '').strip()
    if not title or '\n' in title:
        raise ValueError('decision title must be a single non-empty line')
    root = project_root(cwd)
    path = console_path(root)
    with _file_lock(path):
        data = show(root)
        if not data['exists']:
            raise ValueError('handoff missing; run init')
        before_hash = _content_hash(path)
        content = data['content']
        nl = _detect_newline(content)
        match, _, end = _find_section(content, PENDING_ALIASES)
        if not match:
            content = content.rstrip() + nl + nl + '## 未决/风险' + nl + nl
            match, _, end = _find_section(content, PENDING_ALIASES)
        did = _next_decision_id(content)
        now = stamp()
        opt_text = '；'.join(str(o).strip() for o in options if str(o).strip()) or '（待补充）'
        lines = [
            f'### {did} {title}',
            f'- **为什么轮到你**：{why.strip() or "需要你拍板方向或不可逆取舍"}',
            f'- **背景**：{background.strip() or "（见项目现状）"}',
            f'- **选项**：{opt_text}',
            f'- **推荐**：{recommend.strip() or "（无默认推荐）"}',
        ]
        if evidence.strip():
            lines.append(f'- **证据**：{evidence.strip()}')
        if wait_cost.strip():
            lines.append(f'- **等待代价**：{wait_cost.strip()}')
        lines.append('- **状态**：open')
        lines.append(f'- **创建日期**：{now[:10]}')
        block = nl.join(lines) + nl + nl
        content = content[:end].rstrip() + nl + nl + block + content[end:].lstrip('\r\n')
        content = _touch_snapshot(content, now, nl)
        atomic_write(path, content, expected_hash=before_hash)
    return {
        'id': did,
        'title': title,
        'status': 'open',
        'project_root': str(root),
        'path': str(path),
        'created_at': now[:10],
    }


def _append_decision_log(content: str, did: str, title: str, decision: str, reason: str, status: str, day: str, nl: str) -> str:
    match, start, end = _find_section(content, DECISION_LOG_ALIASES)
    if not match:
        content = content.rstrip() + nl + nl + '## 决策记录' + nl + nl + '| 日期 | 编号 | 决定 | 依据 | 状态 |' + nl + '|---|---|---|---|---|' + nl
        match, start, end = _find_section(content, DECISION_LOG_ALIASES)
    section = content[start:end]
    cell_decision = _escape_cell(f'{title}：{decision}' if title else decision)
    cell_reason = _escape_cell(reason or '人工拍板')
    cell_status = _escape_cell(status)
    table_lines = [ln for ln in section.splitlines() if ln.strip().startswith('|')]
    if table_lines:
        cols = [c.strip() for c in table_lines[0].strip().strip('|').split('|')]
        if len(cols) >= 5:
            row = f'| {day} | {did} | {cell_decision} | {cell_reason} | {cell_status} |'
        elif len(cols) == 4:
            row = f'| {day} | {did} | {cell_decision} | {cell_reason} |'
        else:
            row = f'| {day} | **{did}** {cell_decision} | {cell_reason}（{cell_status}） |'
        updated_section = section.rstrip() + nl + row + nl + nl
    else:
        bullet = f'- **{day} {did}**（{cell_status}）：{cell_decision}。依据：{cell_reason}'
        updated_section = section.rstrip() + nl + bullet + nl + nl
    return content[:start] + updated_section + content[end:]


def decide_close(
    did: str,
    *,
    decision: str,
    reason: str = '',
    status: str = 'decided',
    unnecessary: bool = False,
    cwd=None,
) -> dict:
    """Close or defer a `D-xxx` entry: archive to `## 决策记录` (or mark deferred)."""
    did = str(did or '').strip().upper()
    if not re.fullmatch(r'D-\d{3,}', did):
        raise ValueError('decision id must look like D-001')
    if status not in ('decided', 'deferred'):
        raise ValueError('status must be decided or deferred')
    decision = str(decision or '').strip()
    if not decision:
        raise ValueError('decision text required')
    root = project_root(cwd)
    path = console_path(root)
    with _file_lock(path):
        data = show(root)
        if not data['exists']:
            raise ValueError('handoff missing; run init')
        before_hash = _content_hash(path)
        content = data['content']
        nl = _detect_newline(content)
        _, start, end = _find_section(content, PENDING_ALIASES)
        if start < 0:
            raise ValueError('pending decisions section not found')
        section = content[start:end]
        matches = list(_DECISION_HEADING_RE.finditer(section))
        target_idx = next((i for i, m in enumerate(matches) if m.group(1).upper() == did), None)
        if target_idx is None:
            raise ValueError(f'decision not found: {did}')
        m = matches[target_idx]
        title = m.group(2).strip()
        b_start = m.start()
        b_end = matches[target_idx + 1].start() if target_idx + 1 < len(matches) else len(section)
        now = stamp()
        tag = f'{reason}；[unnecessary]' if unnecessary and reason else ('[unnecessary]' if unnecessary else reason)
        if status == 'deferred':
            block = section[b_start:b_end]
            note = f'deferred（{now[:10]}：{decision}）'
            if re.search(r'^-\s*\*\*状态\*\*[：:].*$', block, re.M):
                block = re.sub(r'^-\s*\*\*状态\*\*[：:].*$', f'- **状态**：{note}', block, count=1, flags=re.M)
            else:
                block = block.rstrip() + nl + f'- **状态**：{note}' + nl + nl
            new_section = section[:b_start] + block + section[b_end:]
            content = content[:start] + new_section + content[end:]
        else:
            new_section = (section[:b_start].rstrip() + nl + nl + section[b_end:].lstrip('\r\n')).strip()
            new_section = (nl + new_section + nl + nl) if new_section else (nl + nl)
            content = content[:start] + new_section + content[end:]
            content = _append_decision_log(content, did, title, decision, tag, status, now[:10], nl)
        content = _touch_snapshot(content, now, nl)
        atomic_write(path, content, expected_hash=before_hash)
    return {
        'id': did,
        'title': title,
        'status': status,
        'decision': decision,
        'reason': tag,
        'unnecessary': bool(unnecessary),
        'closed_at': now[:10],
        'project_root': str(root),
        'path': str(path),
    }


def list_decisions(
    *,
    cwd=None,
    all_projects: bool = False,
    status: str = 'open',
    remote: bool = False,
) -> dict:
    """Read-only decision inbox for one project or aggregated across known projects."""
    if status not in ('open', 'deferred', 'all'):
        raise ValueError('status must be open, deferred, or all')
    roots: list[Path]
    errors: list[dict] = []
    if all_projects:
        roots = known_roots()
        allowed_set = set(roots)
    else:
        allowed_set = set(known_roots()) if remote else None
        roots = [allowed_root(cwd, allowed=allowed_set) if remote else project_root(cwd)]
    items: list[dict] = []
    scanned = 0
    for root in roots:
        try:
            data = show(root, remote=remote, allowed=allowed_set)
        except (ValueError, OSError) as exc:
            errors.append({'project_root': str(root), 'error': str(exc)})
            continue
        if not data['exists']:
            continue
        scanned += 1
        for entry in parse_decisions(data['content']):
            if status != 'all' and entry['status'] != status:
                continue
            items.append(
                {
                    **entry,
                    'project': root.name,
                    'project_root': str(root),
                    'path': data['path'],
                }
            )
    return {
        'scope': 'all' if all_projects else str(roots[0]),
        'status_filter': status,
        'projects_scanned': scanned,
        'count': len(items),
        'decisions': items,
        'errors': errors,
    }


def daemon_info():
    from .cli import client
    try:
        result = client.call('GET', '/health', retries=0, timeout=2)
        from .daemon import read_state
        result = {k: result.get(k) for k in ('version', 'pid')}
        if result['pid'] is None:
            result['pid'] = read_state().get('pid')
        return result
    except (OSError, RuntimeError, ValueError):
        return {'version': None, 'pid': None, 'status': 'unavailable'}


def _project_facts(root: Path, relevant: list[dict], content: str) -> dict:
    """Build project-level facts for the auto block without dumping raw run lists."""
    runs = [r for r in relevant if r['_table'] == 'run']
    tasks = [r for r in relevant if r['_table'] == 'task']
    now = time.time()
    day_start, _, _ = ledger.bounds('day', now)
    month_cutoff = now - 30 * 86400
    today_runs = [r for r in runs if (r.get('started_at') or 0) >= day_start]
    month_runs = [r for r in runs if (r.get('started_at') or 0) >= month_cutoff]
    today_sum = ledger.summarize(today_runs)['totals']['cost']
    month_sum = ledger.summarize(month_runs)['totals']['cost']

    # Query open proposals and latest verify verdict read-only when DB exists.
    open_proposals = 0
    latest_verify = None
    db_path = config.HOME / 'sodus.sqlite3'
    if db_path.is_file() and runs:
        run_ids = [r['id'] for r in runs]
        try:
            with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                marks = ','.join('?' * len(run_ids))
                open_proposals = conn.execute(
                    f"SELECT COUNT(*) FROM proposal WHERE status='open' AND run_id IN ({marks})",
                    run_ids,
                ).fetchone()[0]
                ev = conn.execute(
                    f"SELECT run_id, type, ts FROM event WHERE type IN ('verify.green','verify.red') "
                    f"AND run_id IN ({marks}) ORDER BY ts DESC LIMIT 1",
                    run_ids,
                ).fetchone()
                if ev:
                    latest_verify = {
                        'run_id': ev['run_id'],
                        'result': 'green' if ev['type'] == 'verify.green' else 'red',
                        'ts': ev['ts'],
                    }
        except sqlite3.Error:
            pass

    decisions = parse_decisions(content)
    open_decisions = sum(1 for d in decisions if d['status'] == 'open')
    deferred_decisions = sum(1 for d in decisions if d['status'] == 'deferred')
    task_counts: dict[str, int] = {}
    for t in tasks:
        st = str(t.get('status') or 'open')
        task_counts[st] = task_counts.get(st, 0) + 1

    return {
        'schema_version': SCHEMA_VERSION,
        'head': git(root, 'rev-parse', 'HEAD'),
        'branch': git(root, 'branch', '--show-current'),
        'tag': git(root, 'describe', '--tags', '--always'),
        'dirty': bool(git(root, 'status', '--porcelain')),
        'daemon': daemon_info(),
        'open_decisions_count': open_decisions,
        'deferred_decisions_count': deferred_decisions,
        'open_proposals': open_proposals,
        'latest_verify': latest_verify,
        'cost_today': {k: v['total'] for k, v in today_sum.items()},
        'cost_30d': {k: v['total'] for k, v in month_sum.items()},
        'tasks_summary': task_counts,
        'recent_runs_count': len(runs),
        'budget_soft_families': config.load_config().get('budget', {}).get('soft_families', {}),
    }


def sync(cwd=None):
    root = project_root(cwd)
    path = console_path(root)
    with _file_lock(path):
        data = show(root)
        content = data['content']
        if not data['exists']:
            raise ValueError('handoff missing; run init')
        before_hash = _content_hash(path)
        original = content
        newline = _detect_newline(content)
        migrating = START not in content and END not in content
        if migrating:
            heading, _, _ = _find_section(content, STATUS_ALIASES)
            pair = START + newline + END + newline
            if heading:
                pos = heading.end()
                content = content[:pos] + (newline if not content[:pos].endswith('\n') else '') + pair + content[pos:]
            else:
                content += (newline if content and not content.endswith('\n') else '') + pair
                print('handoff: 实时状态 section missing; appended auto block at EOF', file=sys.stderr)
        if content.count(START) != 1 or content.count(END) != 1 or content.index(START) > content.index(END):
            raise ValueError('exactly one ordered auto marker pair required')
        cache: dict[str, Path | None] = {}
        relevant = []
        for row in records():
            if _stored_root(row.get('cwd'), cache=cache) == root:
                relevant.append(row)
        relevant.sort(key=lambda r: r.get('started_at') or r.get('created_at') or 0)
        machine = _project_facts(root, relevant, content)
        block = (
            '\nhead: ' + machine['head'] + '\n```json\n'
            + json.dumps(machine, ensure_ascii=False, indent=2)
            + '\n```\n'
        ).replace('\n', newline)
        content = content[:content.index(START) + len(START)] + block + content[content.index(END):]
        if migrating:
            backup = Path(data['path'] + '.bak-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'))
            with backup.open('xb') as stream:
                stream.write(original.encode('utf-8'))
        atomic_write(path, content, expected_hash=before_hash)
    return show(root)


def _console_only_commit(root: Path, recorded: str, console: str) -> bool:
    if not recorded:
        return False
    changed = [line.strip() for line in git(root, 'diff', '--name-only', recorded, 'HEAD').splitlines() if line.strip()]
    try:
        relative = Path(console).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return False
    return changed == [relative]


def check(cwd=None, remote=False):
    data = show(cwd, remote)
    errors = []
    warnings = []
    if not data['exists']:
        errors.append('handoff_missing')
    try:
        date = datetime.fromisoformat(data['snapshot_at'])
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        days = config.load_config().get('handoff', {}).get('max_age_days', 7)
        if datetime.now(timezone.utc) - date > timedelta(days=days):
            errors.append('snapshot_expired')
    except (ValueError, TypeError):
        errors.append('snapshot_invalid')
    root = Path(data['project_root'])
    head = git(root, 'rev-parse', 'HEAD')
    if data['head'] != head and not _console_only_commit(root, data['head'], data['path']):
        warnings.append('head_mismatch')
    section = re.search(r'^## 关键路径\s*\n(.*?)(?=^## |\Z)', data['content'], re.M | re.S)
    for rel in re.findall(r'`([^`]+)`', section.group(1) if section else ''):
        if not (root / rel).exists():
            errors.append('path_missing: ' + rel)
    content = data['content']
    missing_auto = START not in content and END not in content
    if missing_auto:
        warnings.append('auto_block_missing: run sodus handoff sync to migrate')
    elif content.count(START) != 1 or content.count(END) != 1 or content.index(START) > content.index(END):
        errors.append('auto_markers_invalid')
    auto = content.partition(START)[2].partition(END)[0]
    block = re.search(r'```json\r?\n(.*?)\r?\n```', auto, re.S)
    try:
        recorded = json.loads(block.group(1))['daemon']['version'] if block else None
    except (ValueError, KeyError, TypeError):
        recorded = None
    actual = daemon_info().get('version')
    if missing_auto:
        pass
    elif recorded is None or actual is None:
        errors.append('daemon_unavailable')
    elif recorded != actual:
        errors.append('daemon_version_mismatch')
    return {k: v for k, v in data.items() if k != 'content'} | dict(ok=not errors, errors=errors, warnings=warnings)


def list_projects(remote=False):
    roots = known_roots()
    allowed_set = set(roots)
    result = []
    for root in roots:
        try:
            data = show(root, remote=remote, allowed=allowed_set)
        except (ValueError, OSError):
            result.append(dict(
                name=root.name,
                project_root=str(root),
                path=str(console_path(root)),
                exists=console_path(root).is_file(),
                snapshot_at='',
                head='',
                open_decisions=0,
                status='unreadable',
                next_action='fix-path',
            ))
            continue
        open_decisions = sum(1 for d in parse_decisions(data['content']) if d['status'] == 'open') if data['exists'] else 0
        result.append(
            {k: data[k] for k in ('path', 'project_root', 'exists', 'snapshot_at', 'head')}
            | dict(
                name=root.name,
                open_decisions=open_decisions,
                status='present' if data['exists'] else 'missing',
                next_action='check' if data['exists'] else 'init',
            )
        )
    return result
