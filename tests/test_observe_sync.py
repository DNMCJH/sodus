"""The daemon's observation sweep: cheap, quiet, and never fatal."""

from __future__ import annotations

from sodus import observe, observe_sync


def test_sync_once_uses_the_import_path(monkeypatch):
    calls = []
    monkeypatch.setattr(
        observe, "scan", lambda dry_run=True: calls.append(dry_run) or {"inserted": 7}
    )
    assert observe_sync.sync_once()["inserted"] == 7
    assert calls == [False]


def test_disabled_means_no_thread(monkeypatch):
    monkeypatch.setenv("SODUS_OBSERVE_SYNC", "0")
    assert observe_sync.enabled() is False
    assert observe_sync.start() is None


def test_interval_reads_the_environment(monkeypatch):
    monkeypatch.delenv("SODUS_OBSERVE_SYNC_SECONDS", raising=False)
    assert observe_sync.interval() == 300.0
    monkeypatch.setenv("SODUS_OBSERVE_SYNC_SECONDS", "45")
    assert observe_sync.interval() == 45.0
    monkeypatch.setenv("SODUS_OBSERVE_SYNC_SECONDS", "later")
    assert observe_sync.interval() == 300.0
    monkeypatch.setenv("SODUS_OBSERVE_SYNC_SECONDS", "0")
    assert observe_sync.interval() == 300.0


def test_a_failed_sweep_is_logged_and_survived(monkeypatch):
    def boom(dry_run=True):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(observe, "scan", boom)
    logged = []
    seen = {"sleeps": 0}

    def stop_after_one_sleep(_seconds):
        seen["sleeps"] += 1
        raise StopIteration

    monkeypatch.setattr(observe_sync.time, "sleep", stop_after_one_sleep)
    try:
        observe_sync._loop(logged.append)
    except StopIteration:
        pass
    assert seen["sleeps"] == 1
    assert any("database is locked" in line for line in logged)
