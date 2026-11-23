import json
import subprocess
from pathlib import Path
import urllib.request
import urllib.error
import pytest
from sodus import config, handoff, mcp, db
from sodus.cli import build_parser


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


def test_committing_the_synced_console_is_not_drift(project):
    handoff.init()
    handoff.append('人工内容')
    handoff.sync()
    subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@example.org',
                    'add', 'HANDOFF.md'], check=True, capture_output=True)
    subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@example.org',
                    'commit', '-m', 'handoff'], check=True, capture_output=True)
    result = handoff.check()
    assert result['ok']
    assert 'head_mismatch' not in result['warnings']
    # A real code change after the snapshot is still drift.
    (project / 'code.txt').write_text('x', encoding='utf-8')
    subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@example.org',
                    'add', 'code.txt'], check=True, capture_output=True)
    subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@example.org',
                    'commit', '-m', 'code'], check=True, capture_output=True)
    assert 'head_mismatch' in handoff.check()['warnings']


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
    from sodus.cli import client
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
        name = 'sodus_handoff_' + action
        assert name in names
        result = mcp.call_tool(name, {})
        assert not result['isError']
        assert mcp.call_tool(name, {'write': True})['isError']
    assert mcp.call_tool('sodus_handoff_show', {'cwd': str(outside)})['isError']
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
        result = mcp.call_tool('sodus_handoff_' + action, {})
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


def test_relative_cwd_rows_never_leak_into_sync_or_known_roots(project):
    handoff.init()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO run(id,workspace_id,agent,task_id,cwd,status,started_at) VALUES (?,?,?,?,?,?,?)",
            ('run-rel-dot', config.workspace_id(), 'codex', 'task-leak', '.', 'completed', 100),
        )
        conn.execute(
            "INSERT INTO run(id,workspace_id,agent,task_id,cwd,status,started_at) VALUES (?,?,?,?,?,?,?)",
            ('run-rel-sub', config.workspace_id(), 'codex', 'task-leak2', 'sub/dir', 'completed', 101),
        )
    assert handoff.known_roots() == [project]
    synced = handoff.sync()['content']
    auto = synced.partition(handoff.START)[2].partition(handoff.END)[0]
    block = json.loads(auto.split('```json', 1)[1].split('```', 1)[0])
    assert block['recent_runs_count'] == 0
    assert block['schema_version'] == handoff.SCHEMA_VERSION
    assert 'task-leak' not in synced


def test_decision_inbox_draft_close_defer_and_cross_project_mcp(project, mcp_http, capsys):
    handoff.init()
    other = project.parent / 'other_proj'
    other.mkdir()
    handoff.init(other)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO run(id,workspace_id,agent,task_id,cwd,status,started_at) VALUES (?,?,?,?,?,?,?)",
            ('run-other', config.workspace_id(), 'codex', 't-other', str(other), 'completed', 100),
        )

    d1 = handoff.decide_draft(
        '采用路线 C 投稿 ICME？',
        why='方向取舍需要你拍板',
        background='论文一已有 17 项实验',
        options=['A. 全力写论文一', 'C. 论文一 + 1周 pilot'],
        recommend='C',
        wait_cost='推迟一周错过截稿',
    )
    assert d1['id'] == 'D-001'
    d2 = handoff.decide_draft('是否加 GPU 预算？', cwd=other)
    assert d2['id'] == 'D-001'

    all_open = handoff.list_decisions(all_projects=True, status='open')
    assert all_open['count'] == 2
    assert {item['project'] for item in all_open['decisions']} == {project.name, other.name}

    mcp_res = mcp.call_tool('sodus_handoff_decisions', {'all': True})
    assert not mcp_res['isError']
    mcp_payload = json.loads(mcp_res['content'][0]['text'])
    assert mcp_payload['count'] == 2

    deferred = handoff.decide_close('D-001', decision='等下周再看', status='deferred', cwd=other)
    assert deferred['status'] == 'deferred'
    closed = handoff.decide_close('D-001', decision='定为 C', reason='时间充裕', unnecessary=True)
    assert closed['status'] == 'decided' and closed['unnecessary'] is True

    assert handoff.list_decisions(all_projects=True, status='open')['count'] == 0
    assert handoff.list_decisions(all_projects=True, status='deferred')['count'] == 1
    content = handoff.show()['content']
    assert '### D-001' not in content
    assert '| D-001 |' in content and '[unnecessary]' in content
    d3 = handoff.decide_draft('下一步要不要发 0.9.1？')
    assert d3['id'] == 'D-002'

    args = build_parser().parse_args(['handoff', 'decisions', '--all', '--json'])
    assert args.needs_daemon is False
    assert args.func(args) == 0
    cli_out = json.loads(capsys.readouterr().out)
    assert cli_out['count'] == 1 and cli_out['decisions'][0]['id'] == 'D-002'


def test_decision_parser_handles_sample_and_numbered_headings(project):
    path = project / 'HANDOFF.md'
    path.write_text(
        '---\nsnapshot_at: "2026-11-23T00:00:00+00:00"\n---\n\n'
        '## 2. 决策记录（已定，别重复问）\n\n'
        '| 日期 | 决定 | 依据 |\n|---|---|---|\n| 2026-11-22 | 旧决定 | 样板 |\n\n'
        '## 4. 未决 / 待你拍板\n\n'
        '### D-003 局部片段会不会碰师兄工作？\n'
        '- **为什么轮到你**：边界问题\n'
        '- **状态**：降级，暂缓\n',
        encoding='utf-8',
    )
    items = handoff.parse_decisions(path.read_text(encoding='utf-8'))
    assert len(items) == 1 and items[0]['id'] == 'D-003' and items[0]['status'] == 'deferred'
    drafted = handoff.decide_draft('是否先跑 pilot？')
    assert drafted['id'] == 'D-004'
    handoff.decide_close('D-004', decision='先跑 1 周', reason='控风险')
    text = path.read_text(encoding='utf-8')
    assert '**D-004**' in text and '### D-004' not in text


def test_concurrent_decide_draft_allocates_unique_ids(project):
    from concurrent.futures import ThreadPoolExecutor

    handoff.init()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: handoff.decide_draft(f'并发决策 {i}'), range(6)))
    ids = [r['id'] for r in results]
    assert len(set(ids)) == 6
    assert sorted(ids) == [f'D-{i:03d}' for i in range(1, 7)]
