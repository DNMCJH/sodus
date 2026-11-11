"""Project handoff files. Only explicit local commands write the console."""
from pathlib import Path
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import nullcontext, closing
from datetime import datetime, timezone, timedelta
from . import config

START = '<!-- sodus:auto:start -->'
END = '<!-- sodus:auto:end -->'
SECTIONS = ('项目一句话', '当前唯一优先', '硬约束/禁止事项', '实时状态', '关键路径', '决策记录', '进度节点', '未决/风险', '下一步', '最近变更', '更新规则')


def git(root, *args):
    try:
        p = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True,
                           timeout=5, creationflags=config.NO_WINDOW)
        return p.stdout.strip() if p.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def project_root(cwd=None):
    path = Path(cwd or Path.cwd()).expanduser().resolve()
    if not path.is_dir():
        raise ValueError('cwd must be an existing directory')
    return Path(git(path, 'rev-parse', '--show-toplevel') or path).resolve()


def console_path(root):
    path = Path(config.load_config().get('handoff', {}).get('path', 'HANDOFF.md')).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


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


def known_roots():
    roots = {project_root()}
    for row in records():
        try:
            if row.get('cwd'):
                roots.add(project_root(row['cwd']))
        except (ValueError, OSError):
            pass
    return sorted(roots, key=str)


def allowed_root(cwd=None):
    root = project_root(cwd)
    if root not in known_roots():
        raise ValueError('cwd outside allowed project roots')
    return root


def field(content, name):
    match = re.search(r'^' + re.escape(name) + r':[^\S\r\n]*([^\r\n]*)', content, re.M)
    return match.group(1).strip().strip("\"'") if match else ''


def show(cwd=None, remote=False):
    root = allowed_root(cwd) if remote else project_root(cwd)
    path = console_path(root)
    configured = Path(config.load_config().get('handoff', {}).get('path', 'HANDOFF.md')).expanduser()
    if remote and not configured.is_absolute() and not path.is_relative_to(root):
        raise ValueError('handoff path escapes project root')
    with path.open(encoding='utf-8', newline='') if path.is_file() else nullcontext(None) as stream:
        content = stream.read() if stream else ''
    return dict(path=str(path), project_root=str(root), exists=path.is_file(),
                snapshot_at=field(content, 'snapshot_at'), head=field(content, 'head'), content=content)


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
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


def init(cwd=None, force=False):
    root = project_root(cwd)
    path = console_path(root)
    if path.exists() and not force:
        raise ValueError('handoff already exists; use --force')
    now = stamp()
    text = f'---\ntitle: "{root.name} 项目接力控制台"\ndate: {now[:10]}\nupdated: {now[:10]}\nversion: 1\ntype: project-handoff\nstatus: active\nsnapshot_at: "{now}"\ntags: [workflow/handoff]\n---\n\n# 项目接力控制台\n\n'
    for section in SECTIONS:
        text += f'## {section}\n\n'
        if section == '实时状态':
            text += START + '\n' + END + '\n\n'
        elif section == '关键路径':
            text += '- `.`\n\n'
        elif section == '更新规则':
            text += '- 人工维护决策和下一步；sync 仅刷新机器段，不复制长文或密钥。\n\n'
    atomic_write(path, text)
    return show(root)


def append(text, section='最近变更', cwd=None):
    data = show(cwd)
    if not data['exists']:
        raise ValueError('handoff missing; run init')
    if not text.strip() or '\n' in section or section == '实时状态':
        raise ValueError('invalid text or section')
    content = data['content']
    match = re.search(r'^## ' + re.escape(section) + r'\s*$', content, re.M)
    if not match:
        raise ValueError('section not found')
    next_heading = re.search(r'^## ', content[match.end():], re.M)
    end = match.end() + next_heading.start() if next_heading else len(content)
    bullet = f'- {stamp()[:10]} ' + text.replace('\n', '\n  ') + '\n\n'
    content = content[:end].rstrip() + '\n\n' + bullet + content[end:]
    content = re.sub(r'^snapshot_at:.*$', f'snapshot_at: "{stamp()}"', content, count=1, flags=re.M)
    atomic_write(Path(data['path']), content)
    return show(cwd)


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


def sync(cwd=None):
    data = show(cwd)
    content = data['content']
    if not data['exists']:
        raise ValueError('handoff missing; run init')
    original = content
    newline = '\r\n' if '\r\n' in content else '\n'
    migrating = START not in content and END not in content
    if migrating:
        heading = re.search(r'^## 实时状态[^\S\r\n]*(?:\r?\n|$)', content, re.M)
        pair = START + newline + END + newline
        if heading:
            pos = heading.end()
            content = content[:pos] + (newline if not content[:pos].endswith('\n') else '') + pair + content[pos:]
        else:
            content += (newline if content and not content.endswith('\n') else '') + pair
            print('handoff: 实时状态 section missing; appended auto block at EOF', file=sys.stderr)
    if content.count(START) != 1 or content.count(END) != 1 or content.index(START) > content.index(END):
        raise ValueError('exactly one ordered auto marker pair required')
    root = Path(data['project_root'])
    relevant = []
    for row in records():
        try:
            if row.get('cwd') and project_root(row['cwd']) == root:
                relevant.append(row)
        except (ValueError, OSError):
            pass
    def summary(row):
        return {k: row.get(k) for k in ('id', 'title', 'status', 'kind', 'task_id') if k in row}
    relevant.sort(key=lambda r: r.get('started_at') or r.get('created_at') or 0)
    machine = dict(head=git(root, 'rev-parse', 'HEAD'), branch=git(root, 'branch', '--show-current'),
                   tag=git(root, 'describe', '--tags', '--always'), daemon=daemon_info(),
                   tasks=[summary(r) for r in relevant if r['_table'] == 'task'][-5:],
                   runs=[summary(r) for r in relevant if r['_table'] == 'run'][-5:],
                   plans=[summary(r) for r in relevant if r['_table'] == 'plan'][-5:],
                   budget_soft_families=config.load_config().get('budget', {}).get('soft_families', {}))
    block = '\nhead: ' + machine['head'] + '\n```json\n' + json.dumps(machine, ensure_ascii=False, indent=2) + '\n```\n'
    block = block.replace('\n', newline)
    content = content[:content.index(START) + len(START)] + block + content[content.index(END):]
    if migrating:
        backup = Path(data['path'] + '.bak-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'))
        with backup.open('xb') as stream:
            stream.write(original.encode('utf-8'))
    atomic_write(Path(data['path']), content)
    return show(cwd)


def _console_only_commit(root: Path, recorded: str, console: str) -> bool:
    """True when the only tree change since the recorded snapshot is this console.

    `sync` records the HEAD it saw, then the operator commits the console that
    contains that snapshot: by construction the next HEAD differs from the
    recorded one, and that is not drift. Any other change since the snapshot
    still warns, because the recorded state is then genuinely stale.
    """
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
    for path in re.findall(r'`([^`]+)`', section.group(1) if section else ''):
        if not (root / path).exists():
            errors.append('path_missing: ' + path)
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
    result = []
    for root in known_roots():
        try:
            data = show(root, remote=remote)
        except (ValueError, OSError):
            result.append(dict(name=root.name, project_root=str(root), path=str(console_path(root)),
                               exists=console_path(root).is_file(), snapshot_at='', head='',
                               status='unreadable', next_action='fix-path'))
            continue
        result.append({k: data[k] for k in ('path', 'project_root', 'exists', 'snapshot_at', 'head')} |
                      dict(name=root.name, status='present' if data['exists'] else 'missing',
                           next_action='check' if data['exists'] else 'init'))
    return result
