"""`nodus up` / `nodus down` must be honest about what is running.

No real processes are spawned here: `start`/`stop` are exercised through the
state file and the health probe, which is where the interesting decisions live.
"""

from __future__ import annotations

from nodus import daemon


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "state_path", lambda: tmp_path / "daemon.json")
    assert daemon.read_state() == {}
    daemon.write_state(4321, 4199)
    assert daemon.read_state()["pid"] == 4321
    daemon.clear_state()
    assert daemon.read_state() == {}


def test_status_reports_down_when_nothing_answers(monkeypatch):
    monkeypatch.setattr(daemon, "read_state", lambda: {"pid": 1, "port": 59999})
    monkeypatch.setattr(daemon, "health", lambda port, timeout=1.0: False)
    st = daemon.status()
    assert st["up"] is False
    assert st["pid"] is None       # a dead pid must not be reported as ours
    assert st["url"] == ""


def test_status_reports_up_with_recorded_pid(monkeypatch):
    monkeypatch.setattr(daemon, "read_state", lambda: {"pid": 77, "port": 4199, "started": 1.0})
    monkeypatch.setattr(daemon, "health", lambda port, timeout=1.0: True)
    st = daemon.status()
    assert st["up"] and st["pid"] == 77 and st["url"].endswith(":4199/")


def test_start_is_idempotent_when_already_up(monkeypatch):
    monkeypatch.setattr(daemon, "read_state", lambda: {"pid": 77, "port": 4199})
    monkeypatch.setattr(daemon, "health", lambda port, timeout=1.0: True)
    result = daemon.start()
    assert result["ok"] and result["already"] is True


def test_stop_without_a_record_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "state_path", lambda: tmp_path / "daemon.json")
    monkeypatch.setattr(daemon, "health", lambda port, timeout=1.0: True)
    result = daemon.stop()
    # Something still answers but we never recorded a pid: refusing to claim we
    # stopped it is the whole point.
    assert result["ok"] is False
    assert "no pid recorded" in result["note"]
