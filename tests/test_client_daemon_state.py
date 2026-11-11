"""A daemon started from the CLI client must be stoppable by `sodus down`.

`ensure_daemon` starts the daemon as a side effect of any command that needs
it. That path used to skip the state file `daemon.start` writes, so
`daemon.json` kept pointing at a dead pid while another process was serving:
`sodus down` killed nothing, and `sodus up` reported a pid that no longer
existed. The rule is one state file and one fact in it -- whoever spawns the
daemon records the pid.
"""

from __future__ import annotations

from sodus.cli import client


class _FakeProc:
    pid = 4321


def test_ensure_daemon_records_the_pid_it_spawned(tmp_path, monkeypatch):
    recorded = {}

    calls = {"n": 0}

    def fake_alive(port=4199):
        # First call is the pre-spawn check (down); the loop then sees the
        # daemon answering.
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(client.config, "HOME", tmp_path)
    monkeypatch.setattr(client, "alive", fake_alive)
    monkeypatch.setattr(client.subprocess, "Popen", lambda *args, **kwargs: _FakeProc())
    monkeypatch.setattr(
        client, "write_state", lambda pid, port: recorded.update(pid=pid, port=port)
    )

    client.ensure_daemon()

    assert recorded == {"pid": 4321, "port": 4199}
