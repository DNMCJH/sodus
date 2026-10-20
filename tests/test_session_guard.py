"""Session guard rails: one writer, optional read whitelist."""

from __future__ import annotations

import json
import time

import pytest
import zstandard

from nodus import config, dsh_sessions as ds


def make_session(root, project: str, sid: str, cwd: str) -> None:
    rows = [
        {"type": "session", "version": 4, "id": sid, "createdAt": 1_791_000_000_000, "cwd": cwd},
        {"type": "session/title", "seq": 1, "time": 1_791_000_000_000, "data": {"title": "t"}},
        {
            "type": "user/message",
            "seq": 2,
            "time": 1_791_000_001_000,
            "data": {"content": [{"type": "text", "text": "hi"}], "source": {"kind": "user"}},
        },
    ]
    folder = root / project / sid
    folder.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(r) for r in rows) + "\n"
    with (folder / ds.FILE_NAME).open("wb") as f:
        with zstandard.ZstdCompressor().stream_writer(f) as writer:
            writer.write(payload.encode("utf-8"))


@pytest.fixture()
def root(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(ds, "ROOT", sessions)
    monkeypatch.setattr(config, "HOME", tmp_path / "wbhome")
    (tmp_path / "wbhome").mkdir()
    return sessions


def test_lock_is_exclusive(root):
    with ds.session_lock("session-abc"):
        with pytest.raises(RuntimeError, match="session busy"):
            with ds.session_lock("session-abc"):
                pass
    with ds.session_lock("session-abc"):
        pass  # released, can take again


def test_stale_lock_is_reclaimed(root):
    lock_dir = config.HOME / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / "session-old.lock"
    path.write_text("stale", encoding="utf-8")
    old = time.time() - 7200
    import os

    os.utime(path, (old, old))
    with ds.session_lock("session-old", stale_seconds=60):
        pass
    assert not path.exists()


def test_whitelist_blocks_outside_session(root, tmp_path, monkeypatch):
    make_session(root, "--p--", "session-in", str(tmp_path / "allowed" / "proj"))
    make_session(root, "--p--", "session-out", str(tmp_path / "elsewhere"))
    (tmp_path / "allowed").mkdir()
    (config.HOME / "config.json").write_text(
        json.dumps({"session_roots": [str(tmp_path / "allowed")]}), encoding="utf-8"
    )
    assert ds.load("session-in")[0]["id"] == "session-in"
    with pytest.raises(ValueError, match="session_roots"):
        ds.load("session-out")
    listed = {s["id"] for s in ds.list_sessions()}
    assert listed == {"session-in"}


def test_empty_whitelist_allows_everything(root, tmp_path):
    make_session(root, "--p--", "session-any", str(tmp_path / "anywhere"))
    assert ds.load("session-any")[0]["id"] == "session-any"
