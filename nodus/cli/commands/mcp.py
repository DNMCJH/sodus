"""Idempotent host configuration for the stdio HTTP bridge."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import tomllib
from datetime import datetime

COMMAND = sys.executable
ARGS = ["-m", "sodus", "mcp"]


def _toml_string(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def add_install(parser):
    sub = parser.add_subparsers(dest="mcp_command")
    p = sub.add_parser("install", help="configure Codex / Claude / DSH / opencode MCP")
    p.add_argument("--host", action="append", help="codex|claude|dsh|opencode; repeat or comma-separate (default: all)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--uninstall", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--dsh-home")
    p.add_argument("--opencode-config", help="override opencode config file path")
    p.set_defaults(func=install, needs_daemon=False)


def _yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        raise ValueError("DSH 配置需要 PyYAML；请安装 PyYAML") from None


def _merge(host, text, uninstall):
    expected = {"command": COMMAND, "args": ARGS}
    if host == "codex":
        data = tomllib.loads(text)
        servers = data.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcp_servers must be a table")
        current = servers.get("sodus")
        if current is not None:
            expected = dict(current)
            expected.update({"command": COMMAND, "args": ARGS})
        # Preserve every byte outside our table, including comments and order.
        headers = list(re.finditer(r'(?m)^[ \t]*(\[\[?[^\n]+\]\]?)[ \t]*(?:#.*)?$', text))
        spans = []
        for i, match in enumerate(headers):
            name = match.group(1).strip()
            # tomllib handles quoted table keys without guessing their spelling.
            parsed = tomllib.loads(name)
            if isinstance(parsed.get('mcp_servers'), dict) and 'sodus' in parsed['mcp_servers']:
                spans.append((match.start(), headers[i+1].start() if i+1 < len(headers) else len(text)))
        if current is not None and not spans:
            raise ValueError("sodus uses an inline/dotted TOML table; refusing unsafe edit")
        updated = text
        for start, end in reversed(spans):
            block = updated[start:end]
            if not uninstall:
                block = re.sub(r'(?m)^command\s*=.*$', lambda _: 'command = ' + _toml_string(COMMAND), block)
                block = re.sub(r'(?m)^args\s*=.*$', 'args = ["-m", "sodus", "mcp"]', block)
                if not re.search(r'(?m)^command\s*=', block): block += 'command = ' + _toml_string(COMMAND) + '\n'
                if not re.search(r'(?m)^args\s*=', block): block += 'args = ["-m", "sodus", "mcp"]\n'
            updated = updated[:start] + ('' if uninstall else block) + updated[end:]
        if not uninstall and current is None:
            updated = updated.rstrip() + '\n\n[mcp_servers.sodus]\ncommand = ' + _toml_string(COMMAND) + '\nargs = ["-m", "sodus", "mcp"]\n'
        after = tomllib.loads(updated)
        # Never let a textual table edit silently consume another host setting.
        before_other = dict(data)
        after_other = dict(after)
        for obj in (before_other, after_other):
            tables = dict(obj.get("mcp_servers", {}))
            tables.pop("sodus", None)
            obj["mcp_servers"] = tables
        if before_other != after_other:
            raise ValueError("cannot safely preserve unrelated TOML settings")
    elif host in ("claude", "opencode"):
        data = json.loads(text) if text.strip() else {}
        key = 'mcp' if host == 'opencode' else 'mcpServers'
        if not isinstance(data, dict) or not isinstance(data.get(key, {}), dict):
            raise ValueError(f"{key} must be an object")
        current = data.get(key, {}).get('sodus')
        if current is not None and not isinstance(current, dict):
            raise ValueError("sodus must be an object")
        expected = ({"type": "local", "command": [COMMAND, *ARGS], "enabled": True}
                    if host == 'opencode' else dict(expected, type="stdio"))
        if uninstall:
            data.get(key, {}).pop('sodus', None)
        else:
            merged = dict(current or {})
            merged.update(expected)
            data.setdefault(key, {})['sodus'] = merged
            expected = merged
        updated = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    else:
        yaml = _yaml()
        try:
            data = yaml.safe_load(text) if text.strip() else []
        except yaml.YAMLError as exc:
            raise ValueError(str(exc)) from None
        if not isinstance(data, list):
            raise ValueError("cordis.patch.yml must contain a patch array")
        expected = {"name": "@deepseek-ai/dsh-mcp-client", "config": dict(expected, serverName="sodus", transport="stdio", toolCallTimeoutMs=600000)}
        matches = []
        for patch in data:
            if not isinstance(patch, dict):
                raise ValueError("patch entries must be objects")
            entries = patch.get('insert', [])
            if not isinstance(entries, list):
                raise ValueError("insert must be an array")
            for entry in entries:
                if isinstance(entry, dict) and entry.get('name') == expected['name'] and isinstance(entry.get('config'), dict) and entry['config'].get('serverName') == 'sodus':
                    matches.append(entry)
        current = matches[0] if len(matches) == 1 else (None if not matches else {})
        for patch in data:
            if 'insert' in patch:
                patch['insert'] = [e for e in patch['insert'] if e not in matches]
        if not uninstall:
            if matches:
                merged = dict(matches[0])
                cfg = dict(merged.get('config') or {})
                cfg.update(expected['config'])
                merged.update(expected)
                merged['config'] = cfg
                expected = merged
            data.append({'insert': [expected]})
        data = [p for p in data if p != {'insert': []}]
        updated = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    state = 'missing' if current is None else ('configured' if current == expected else 'drift')
    if (uninstall and current is None) or (not uninstall and state == 'configured'):
        updated = text
    return state, updated


def install(args):
    hosts = list(dict.fromkeys(h.strip() for item in (args.host or ['codex,claude,dsh,opencode']) for h in item.split(',')))
    if any(h not in ('codex', 'claude', 'dsh', 'opencode') for h in hosts):
        raise ValueError("--host must be codex|claude|dsh|opencode")
    failed = False
    for host in hosts:
        dsh_home = args.dsh_home or os.environ.get('DSH_HOME')
        if host == 'dsh' and not dsh_home:
            print('dsh: missing — 跳过 DSH：未设置 DSH_HOME / --dsh-home')
            continue
        path = {'codex': Path.home() / '.codex/config.toml', 'claude': Path.home() / '.claude.json',
                'opencode': Path(args.opencode_config or os.environ.get('OPENCODE_CONFIG') or
                    str(Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'opencode/opencode.json')),
                'dsh': Path(dsh_home or '.') / 'cordis.patch.yml'}[host]
        try:
            text = path.read_text(encoding='utf-8') if path.exists() else ''
            state, updated = _merge(host, text, args.uninstall)
            print(f'{host}: {state} ({path})')
            if args.check:
                failed |= state != 'configured'
            elif updated != text:
                if args.dry_run:
                    print(f'  dry-run: would {"uninstall" if args.uninstall else "install"} sodus')
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if path.exists():
                        backup = path.with_name(path.name + '.bak-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
                        shutil.copy2(path, backup)
                    fd, temp_name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
                    try:
                        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
                            stream.write(updated)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temp_name, path)
                    finally:
                        if os.path.exists(temp_name):
                            os.unlink(temp_name)
        except (ValueError, OSError, TypeError) as exc:
            print(f'{host}: drift — 配置无效，未写入：{exc}')
            failed = True
    return int(failed)
