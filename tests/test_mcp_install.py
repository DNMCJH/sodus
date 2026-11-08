"""Installer behavior with isolated host files, never real user configuration."""
import json
from pathlib import Path
import pytest
from nodus.cli import main
from nodus.cli.commands import mcp


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setenv('DSH_HOME', str(tmp_path / 'dsh'))
    return tmp_path


def run(host, *flags):
    return main(['mcp', 'install', '--host', host, *flags])


@pytest.mark.parametrize('host,relative', [('codex','.codex/config.toml'), ('claude','.claude.json'), ('dsh','dsh/cordis.patch.yml')])
def test_lifecycle(home, host, relative, capsys):
    path = home / relative
    assert run(host, '--check') == 1
    assert 'missing' in capsys.readouterr().out
    assert run(host, '--dry-run') == 0
    assert not path.exists()
    assert run(host) == 0
    first = path.read_bytes()
    assert run(host, '--check') == 0
    assert 'configured' in capsys.readouterr().out
    assert run(host) == 0
    assert path.read_bytes() == first
    assert not list(path.parent.glob(path.name + '.bak-*'))
    assert run(host, '--uninstall', '--dry-run') == 0
    assert path.read_bytes() == first
    assert run(host, '--uninstall') == 0
    assert len(list(path.parent.glob(path.name + '.bak-*'))) == 1
    assert run(host, '--check') == 1
    assert run(host, '--uninstall') == 0
    assert len(list(path.parent.glob(path.name + '.bak-*'))) == 1


@pytest.mark.parametrize('host,text', [
    ('codex', '# keep\n[mcp_servers.other]\ncommand = "other"\n'),
    ('claude', '{"theme":"dark","mcpServers":{"other":{"command":"other"}}}'),
    ('dsh', '- insert:\n  - name: other\n    config: {value: 42}\n  - name: "@deepseek-ai/dsh-mcp-client"\n    config: {serverName: other}\n')])
def test_preserve_other_entries_and_backup(home, host, text):
    relative = {'codex':'.codex/config.toml','claude':'.claude.json','dsh':'dsh/cordis.patch.yml'}[host]
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    assert run(host) == 0
    assert next(path.parent.glob(path.name+'.bak-*')).read_text(encoding='utf-8') == text
    assert 'other' in path.read_text(encoding='utf-8')
    assert run(host, '--uninstall') == 0
    assert 'other' in path.read_text(encoding='utf-8')
    if host == 'codex':
        assert path.read_text(encoding='utf-8').startswith('# keep\n')


@pytest.mark.parametrize('host,relative,bad', [('codex','.codex/config.toml','[bad'), ('claude','.claude.json','{"oops"'), ('dsh','dsh/cordis.patch.yml','insert: [')])
def test_bad_files_not_overwritten(home, host, relative, bad):
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bad, encoding='utf-8')
    assert run(host) == 1
    assert path.read_text(encoding='utf-8') == bad
    assert not list(path.parent.glob(path.name+'.bak-*'))


def test_drift_multiple_hosts_and_missing_dsh(home, monkeypatch, capsys):
    (home / '.claude.json').write_text('{"mcpServers":{"sodus":{"command":"old"}}}', encoding='utf-8')
    assert run('claude', '--check') == 1
    assert 'drift' in capsys.readouterr().out
    assert main(['mcp', 'install', '--host', 'codex,claude', '--host', 'dsh']) == 0
    assert main(['mcp', 'install', '--check']) == 0
    monkeypatch.delenv('DSH_HOME')
    assert run('dsh', '--check') == 0
    assert '跳过 DSH' in capsys.readouterr().out
    assert run('dsh', '--dsh-home', str(home / 'dsh'), '--check') == 0


def test_codex_preserves_array_tables_after_sodus(home):
    path = home / '.codex/config.toml'
    path.parent.mkdir()
    path.write_text('[mcp_servers.sodus]\ncommand="old"\n[[other]]\nname="kept"\n', encoding='utf-8')
    assert run('codex') == 0
    import tomllib
    assert tomllib.loads(path.read_text(encoding='utf-8'))['other'] == [{'name':'kept'}]
    assert run('codex', '--uninstall') == 0
    assert tomllib.loads(path.read_text(encoding='utf-8'))['other'] == [{'name':'kept'}]


def test_interpreter_and_legacy_drift(home, capsys):
    import sys
    assert run('claude') == 0
    path = home / '.claude.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['mcpServers']['sodus']['command'] == sys.executable
    data['mcpServers']['sodus']['command'] = 'Z:/legacy/python.exe'
    path.write_text(json.dumps(data), encoding='utf-8')
    assert run('claude', '--check') == 1
    assert 'drift' in capsys.readouterr().out


@pytest.mark.parametrize('host,relative,text', [
    ('claude', '.claude.json', '{"mcpServers":{"sodus":{"env":{"TOKEN":"keep"},"custom":7}}}'),
    ('dsh', 'dsh/cordis.patch.yml', '- insert:\n  - name: "@deepseek-ai/dsh-mcp-client"\n    config: {serverName: sodus, env: {TOKEN: keep}, custom: 7}\n'),
])
def test_merge_keeps_custom_sodus_fields(home, host, relative, text):
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    assert run(host) == 0
    if host == 'claude':
        entry = json.loads(path.read_text(encoding='utf-8'))['mcpServers']['sodus']
        assert entry['env'] == {'TOKEN': 'keep'} and entry['custom'] == 7
    else:
        entry = next(e for p in mcp._yaml().safe_load(path.read_text(encoding='utf-8')) for e in p.get('insert', []) if e.get('config', {}).get('serverName') == 'sodus')
        assert entry['config']['env'] == {'TOKEN': 'keep'} and entry['config']['custom'] == 7


def test_atomic_replace_failure_preserves_original(home, monkeypatch):
    path = home / '.claude.json'
    path.write_text('{"theme":"dark"}\n', encoding='utf-8')
    original = path.read_bytes()
    monkeypatch.setattr(mcp.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('replace failed')))
    assert run('claude') == 1
    assert path.read_bytes() == original
    assert list(path.parent.glob(path.name + '.*.tmp')) == []
