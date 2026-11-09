import http.client
import sqlite3
import pytest
from nodus import config, db, daemon, recover, server, runner, verify


def test_recover_opt_in(monkeypatch):
    monkeypatch.delenv('NODUS_AUTO_RECOVER', raising=False)
    monkeypatch.setattr(config, 'load_config', lambda: {})
    assert not recover.enabled()
    monkeypatch.setattr(config, 'load_config', lambda: {'daemon': {'auto_recover': True}})
    assert recover.enabled()
    monkeypatch.setenv('NODUS_AUTO_RECOVER', '0')
    assert not recover.enabled()
    monkeypatch.setenv('NODUS_AUTO_RECOVER', '1')
    assert recover.enabled()


def test_lock_and_bind_before_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'HOME', tmp_path)
    with daemon.instance_lock():
        with pytest.raises(OSError):
            with daemon.instance_lock():
                pytest.fail('second lock accepted')
    touched = []
    monkeypatch.setattr(runner.RUNNER, 'reap_orphans', lambda: touched.append(True))
    with server._QuietServer(('127.0.0.1', 0), server.Handler) as first:
        with pytest.raises(OSError):
            server.serve(first.server_port)
    assert not touched


@pytest.mark.parametrize('host,code', [('evil.example',403), ('localhost',200), ('127.0.0.1',200), ('[::1]:4199',200)])
def test_host(mcp_http, host, code):
    conn = http.client.HTTPConnection('127.0.0.1', mcp_http.server_port)
    conn.request('GET', '/health', headers={'Host':host})
    response = conn.getresponse()
    assert response.status == code
    response.read()
    conn.close()


def test_verify_infra_retries(monkeypatch, tmp_path):
    monkeypatch.setattr(verify, 'detect', lambda cwd: {'cmd':['pytest'], 'reason':'test'})
    results = iter([{'ok':False, 'exit':3221225786}, {'ok':True, 'exit':0}])
    monkeypatch.setattr(verify, 'run', lambda *a, **k: next(results))
    phases = []
    monkeypatch.setattr(verify, 'persist', lambda rid, tid, phase, result, **kw: phases.append(phase))
    monkeypatch.setattr(runner.RUNNER, '_emit', lambda *a: None)
    assert runner.RUNNER._auto_verify('r', 't', tmp_path) == 'ok'
    assert phases == ['infra','green']


def test_cli_reconcile_uses_http(monkeypatch):
    from nodus.cli.commands import system
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(system, 'call', lambda *args: calls.append(args) or [])
    assert system.reconcile(SimpleNamespace(stale=100)) == 0
    assert calls == [('POST','/reconcile',{'stale':100})]


def test_v7_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'HOME', tmp_path)
    for _ in range(2):
        db._SCHEMA_READY.clear()
        with db.connect() as c:
            assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
            assert c.execute('SELECT count(*) FROM plan_execution').fetchone()[0] == 0


def test_reconcile_endpoint(mcp_http, monkeypatch):
    import json
    monkeypatch.setattr(runner.RUNNER, 'reconcile', lambda stale: [{'id':'owned', 'stale':stale}])
    conn = http.client.HTTPConnection('127.0.0.1', mcp_http.server_port)
    conn.request('POST', '/reconcile', body='{"stale":42}', headers={'Authorization':'Bearer '+config.token(), 'Content-Type':'application/json'})
    response = conn.getresponse()
    assert response.status == 200
    assert json.loads(response.read()) == [{'id':'owned','stale':42}]
    conn.close()


def test_verify_infra_twice_then_red(monkeypatch, tmp_path):
    monkeypatch.setattr(verify, 'detect', lambda cwd: {'cmd':['pytest'], 'reason':'test'})
    calls = []
    monkeypatch.setattr(verify, 'run', lambda *a, **kw: calls.append(1) or {'ok':False,'exit':-1073741510})
    phases = []
    monkeypatch.setattr(verify, 'persist', lambda rid, tid, phase, result, **kw: phases.append(phase))
    monkeypatch.setattr(runner.RUNNER, '_emit', lambda *a: None)
    assert runner.RUNNER._auto_verify('r','t',tmp_path) == 'red'
    assert calls == [1,1] and phases == ['infra','red']
