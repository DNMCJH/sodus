"""One name: sodus.

0.9.0 removed the nodus console script, Python package, state directory and MCP
tool prefix. The old ~/.nodus directory is a migration source only --
sodus migrate-home --apply copies it; nothing reads it implicitly.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

from sodus import config

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *argv],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
    )


def test_one_version_everywhere():
    declared = _pyproject()["project"]["version"]
    assert declared == "0.9.0"
    from sodus import __version__ as package_version

    assert package_version == declared
    assert config.VERSION == declared


def test_only_the_sodus_console_script_is_declared():
    scripts = _pyproject()["project"]["scripts"]
    assert scripts == {"sodus": "sodus.cli:main"}
    assert not (ROOT / "nodus").exists()
    assert not (ROOT / "nodus.py").exists()
    assert (ROOT / "sodus").is_dir()


def test_python_dash_m_sodus_prints_sodus():
    proc = _run("-m", "sodus", "--version")
    assert proc.returncode == 0
    text = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
    assert text == "sodus 0.9.0"


def test_home_resolves_to_sodus_and_never_the_legacy_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("SODUS_HOME", raising=False)
    monkeypatch.setattr(config, "HOME_DEFAULT", tmp_path / ".sodus")
    monkeypatch.setattr(config, "LEGACY_IMPL_HOME", tmp_path / ".nodus")
    (tmp_path / ".nodus").mkdir()
    assert config._resolve_home() == tmp_path / ".sodus"

    monkeypatch.setenv("SODUS_HOME", str(tmp_path / "explicit"))
    assert config._resolve_home() == tmp_path / "explicit"


def test_migrate_home_defaults_nodus_to_sodus(tmp_path, monkeypatch):
    from sodus.cli.commands import migrate

    monkeypatch.delenv("SODUS_HOME", raising=False)
    monkeypatch.delenv("SODUS_LEGACY_HOME", raising=False)
    monkeypatch.setattr(config, "HOME_DEFAULT", tmp_path / ".sodus")
    monkeypatch.setattr(config, "LEGACY_IMPL_HOME", tmp_path / ".nodus")
    assert migrate._default_source() == tmp_path / ".nodus"
    assert migrate._default_target() == tmp_path / ".sodus"
