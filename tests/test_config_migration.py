"""Renaming the product must not abandon the state that already exists."""

from __future__ import annotations

from sodus import config


def test_legacy_home_is_moved_not_abandoned(tmp_path, monkeypatch):
    old, new = tmp_path / ".workbench", tmp_path / ".sodus"
    old.mkdir()
    (old / "token").write_text("secret-token", encoding="utf-8")
    (old / "wb.sqlite3").write_text("db", encoding="utf-8")
    (old / "wb.sqlite3-wal").write_text("wal", encoding="utf-8")

    monkeypatch.delenv("SODUS_HOME", raising=False)
    monkeypatch.setattr(config, "HOME", new)
    monkeypatch.setattr(config, "LEGACY_HOME", old)
    config._absorb_legacy_home()

    assert (new / "token").read_text(encoding="utf-8") == "secret-token"
    assert (new / "sodus.sqlite3").read_text(encoding="utf-8") == "db"
    assert (new / "sodus.sqlite3-wal").read_text(encoding="utf-8") == "wal"
    assert not old.exists()


def test_migration_never_overwrites_an_existing_home(tmp_path, monkeypatch):
    old, new = tmp_path / ".workbench", tmp_path / ".sodus"
    old.mkdir()
    (old / "marker").write_text("old", encoding="utf-8")
    new.mkdir()
    (new / "marker").write_text("new", encoding="utf-8")

    monkeypatch.delenv("SODUS_HOME", raising=False)
    monkeypatch.setattr(config, "HOME", new)
    monkeypatch.setattr(config, "LEGACY_HOME", old)
    config._absorb_legacy_home()

    assert (new / "marker").read_text(encoding="utf-8") == "new"
    assert old.exists()


def test_an_explicit_home_is_left_alone(tmp_path, monkeypatch):
    old, new = tmp_path / ".workbench", tmp_path / "custom"
    old.mkdir()
    (old / "marker").write_text("old", encoding="utf-8")

    monkeypatch.setenv("SODUS_HOME", str(new))
    monkeypatch.setattr(config, "HOME", new)
    monkeypatch.setattr(config, "LEGACY_HOME", old)
    config._absorb_legacy_home()

    assert not new.exists()
    assert old.exists()
