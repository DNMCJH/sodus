import json
import subprocess
from pathlib import Path
import urllib.request
import urllib.error
import pytest
from nodus import config, handoff, mcp, db
from nodus.cli import build_parser


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(config, 'HOME', tmp_path / 'home')
    monkeypatch.setattr(handoff, 'daemon_info', lambda: {'version': config.VERSION, 'pid': 123})
    subprocess.run(['git', 'init', str(root)], check=True, capture_output=True)
    subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@example.org',
                    'commit', '--allow-empty', '-m', 'initial'], check=True, capture_output=True)
    return root


def test_init_show_append_and_force(project):
    child = project / 'child'
    child.mkdir()
    data = handoff.init(child)
    assert Path(data['project_root']) == project
    assert len(data['content'].splitlines()) <= 200
    assert all('## ' + s in data['content'] for s in handoff.SECTIONS)
    with pytest.raises(ValueError):
        handoff.init()
    data = handoff.append('完成测试', '下一步')
    assert '完成测试' in data['content'].split('## 下一步')[1].split('## 最近变更')[0]
    assert data['snapshot_at']
    with pytest.raises(ValueError):
        handoff.append('x', 'unknown')
    assert '完成测试' not in handoff.init(force=True)['content']


def test_sync_preserves_human_and_check_failures(project, monkeypatch):
    handoff.init()
    handoff.append('人工内容')
    before = handoff.show()['content']
    after = handoff.sync()['content']
    assert before.split(handoff.START)[0] == after.split(handoff.START)[0]
    assert before.split(handoff.END)[1] == after.split(handoff.END)[1]
    assert handoff.check()['ok']
    assert handoff.sync()['content'] == after
    subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@example.org',
                    'commit', '--allow-empty', '-m', 'drift'], check=True, capture_output=True)
    result = handoff.check()
    assert result['ok'] and 'head_mismatch' in result['warnings']
    path = project / 'HANDOFF.md'
    text = path.read_text(encoding='utf-8').replace('- `.`', '- `missing.txt`')
    text = text.replace(handoff.show()['snapshot_at'], '2000-01-01T00:00:00+00:00')
    path.write_text(text, encoding='utf-8')
    errors = handoff.check()['errors']
    assert 'snapshot_expired' in errors and 'path_missing: missing.txt' in errors
    monkeypatch.setattr(handoff, 'daemon_info', lambda: {'version': 'different'})
    assert 'daemon_version_mismatch' in handoff.check()['errors']


def test_config_fallback_and_missing(project, monkeypatch):
    monkeypatch.setattr(config, 'load_config', lambda: {'handoff': {'path': 'docs/console.md'}})
    assert not handoff.show()['exists']
    assert not handoff.check()['ok']
    assert handoff.init()['path'] == str(project / 'docs' / 'console.md')
    plain = project.parent / 'plain'
    plain.mkdir()
    assert handoff.project_root(plain) == plain


def test_list_existing_db_and_unavailable(project, monkeypatch):
    handoff.init()
    assert len(handoff.list_projects()) == 1
    assert not config.HOME.exists()
    other = project.parent / 'other'
    other.mkdir()
    with db.connect() as conn:
        conn.execute('INSERT INTO run(id,workspace_id,agent,task_id,cwd,status,started_at) VALUES (?,?,?,?,?,?,?)',
                     ('t-handoff', config.workspace_id(), 'other', '', str(other), 'open', 1))
    result = handoff.list_projects()
    assert {r['project_root'] for r in result} == {str(project), str(other)}
    assert all('content' not in r for r in result)


@pytest.mark.parametrize('argv', [['init'], ['show', '--json'], ['append', 'hello'], ['sync'], ['check', '--json'], ['list', '--json']])
def test_cli(project, argv, capsys):
    if argv[0] != 'init':
        handoff.init()
        handoff.sync()
    args = build_parser().parse_args(['handoff', *argv])
    assert args.needs_daemon is False
    assert args.func(args) == 0
    assert capsys.readouterr().out


def test_http_mcp_readonly_auth_and_boundary(project, mcp_http):
    from nodus.cli import client
    handoff.init()
    handoff.sync()
    path = project / 'HANDOFF.md'
    before = path.read_bytes()
    base = client.BASE
    def request(route, token=True, method='GET'):
        req = urllib.request.Request(base + route, method=method)
        if token:
            req.add_header('Authorization', 'Bearer ' + config.token())
        return urllib.request.urlopen(req)
    for route in ('/handoff', '/handoffs', '/handoff/check'):
        with pytest.raises(urllib.error.HTTPError) as error:
            request(route, False)
        assert error.value.code == 401
        with request(route) as response:
            assert response.status == 200
    outside = project.parent / 'outside'
    outside.mkdir()
    from urllib.parse import urlencode
    with pytest.raises(urllib.error.HTTPError) as error:
        request('/handoff?' + urlencode({'cwd': str(outside)}))
    assert error.value.code == 403
    with pytest.raises(urllib.error.HTTPError):
        request('/handoff', method='POST')
    names = {t['name'] for t in mcp.TOOLS}
    for action in ('show', 'list', 'check'):
        name = 'nodus_handoff_' + action
        assert name in names
        result = mcp.call_tool(name, {})
        assert not result['isError']
        assert mcp.call_tool(name, {'write': True})['isError']
    assert mcp.call_tool('nodus_handoff_show', {'cwd': str(outside)})['isError']
    assert path.read_bytes() == before


def test_atomic_failure_leaves_original(project, monkeypatch):
    handoff.init()
    path = project / 'HANDOFF.md'
    before = path.read_bytes()
    def fail(*args):
        raise OSError('replace failed')
    monkeypatch.setattr(handoff.os, 'replace', fail)
    with pytest.raises(OSError):
        handoff.append('lost')
    assert path.read_bytes() == before
    assert not list(project.glob('.handoff-*'))


def test_sync_crlf_and_bad_markers(project):
    data = handoff.init()
    path = Path(data['path'])
    path.write_bytes(data['content'].replace('\n', '\r\n').encode('utf-8'))
    before = path.read_bytes()
    handoff.sync()
    after = path.read_bytes()
    assert before.split(handoff.START.encode())[0] == after.split(handoff.START.encode())[0]
    assert before.split(handoff.END.encode())[1] == after.split(handoff.END.encode())[1]
    assert handoff.check()['ok']
    path.write_text(handoff.START + handoff.START + handoff.END, encoding='utf-8')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        handoff.sync()
    assert path.read_bytes() == before


def test_remote_relative_escape_rejected(project, monkeypatch):
    outside = project.parent / 'secret.md'
    outside.write_text('secret', encoding='utf-8')
    monkeypatch.setattr(config, 'load_config', lambda: {'handoff': {'path': '../secret.md'}})
    with pytest.raises(ValueError, match='escapes'):
        handoff.show(remote=True)
    listed = handoff.list_projects(remote=True)
    assert listed[0]['status'] == 'unreadable'
    assert 'content' not in listed[0]
    monkeypatch.setattr(config, 'load_config', lambda: {'handoff': {'path': str(outside)}})
    assert handoff.show(remote=True)['content'] == 'secret'


def test_mcp_auth_failure_uses_only_get(project, monkeypatch):
    calls = []
    def denied(method, path, body=None, **kwargs):
        calls.append((method, path))
        raise RuntimeError('HTTP 401: unauthorized')
    monkeypatch.setattr(mcp.client, 'call', denied)
    for action in ('show', 'list', 'check'):
        result = mcp.call_tool('nodus_handoff_' + action, {})
        assert result['isError']
        assert '401' in result['content'][0]['text']
    assert all(method == 'GET' for method, _ in calls)


@pytest.mark.parametrize('heading', [True, False])
def test_sync_migrates_manual_console(project, heading, capsys, monkeypatch):
    data = handoff.init()
    text = data['content'].replace(handoff.START + '\n' + handoff.END + '\n', '')
    if not heading:
        text = text.replace('## 实时状态', '## 人工状态')
    path = Path(data['path'])
    original = text.replace('\n', '\r\n').encode('utf-8')
    path.write_bytes(original)
    with monkeypatch.context() as unavailable:
        unavailable.setattr(handoff, 'daemon_info', lambda: {'version': None})
        result = handoff.check()
    assert result['ok'] and any(w.startswith('auto_block_missing') for w in result['warnings'])
    assert 'daemon_unavailable' not in result['errors']
    handoff.sync()
    after = path.read_bytes()
    assert b'\n' not in after.replace(b'\r\n', b'')
    start, end = after.index(handoff.START.encode()), after.index(handoff.END.encode()) + len(handoff.END) + 2
    assert after[:start] + after[end:] == original
    assert next(project.glob('HANDOFF.md.bak-*')).read_bytes() == original
    assert handoff.check()['ok']
    assert ('appended' in capsys.readouterr().err) == (not heading)
    handoff.sync()
    assert len(list(project.glob('HANDOFF.md.bak-*'))) == 1


@pytest.mark.parametrize('markers', [handoff.START, handoff.END, handoff.END + handoff.START, handoff.START * 2 + handoff.END])
def test_sync_rejects_partial_or_invalid_markers(project, markers):
    path = project / 'HANDOFF.md'
    path.write_text(markers, encoding='utf-8')
    before = path.read_bytes()
    with pytest.raises(ValueError, match='ordered auto marker'):
        handoff.sync()
    assert path.read_bytes() == before
    assert 'auto_markers_invalid' in handoff.check()['errors']


def test_migration_replace_failure_preserves_original(project, monkeypatch):
    path = project / 'HANDOFF.md'
    path.write_bytes(b'## manual\r\nkeep me\r\n')
    original = path.read_bytes()
    def fail(*args):
        raise OSError('replace failed')
    monkeypatch.setattr(handoff.os, 'replace', fail)
    with pytest.raises(OSError):
        handoff.sync()
    assert path.read_bytes() == original
    assert next(project.glob('HANDOFF.md.bak-*')).read_bytes() == original
    assert not list(project.glob('.handoff-*'))


def test_check_cli_warning_is_success(project, capsys):
    handoff.init()
    handoff.sync()
    subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@example.org',
                    'commit', '--allow-empty', '-m', 'drift'], check=True, capture_output=True)
    args = build_parser().parse_args(['handoff', 'check', '--json'])
    assert args.func(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['errors'] == [] and result['warnings'] == ['head_mismatch']
