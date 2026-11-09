"""One program, two names: the `sodus` surface must not become a second implementation.

Phase A gave the CLI a second console script. Phase B/C1 adds a thin `sodus`
package that forwards to `nodus`, bumps both names to one version, and teaches
the state directory a product-name override. These tests pin those facts: same
version everywhere, both entry points run the same parser, and the state home
resolves in the documented order without creating anything.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import nodus
import sodus
from nodus import config

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def _command_names(parser: argparse.ArgumentParser) -> set[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("no subcommands registered")


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *argv],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
    )


def _text(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout + proc.stderr).decode("utf-8", "replace")


def test_one_version_everywhere():
    # The bug this replaces: the package said 0.2.0 while the dist was 0.4.0.
    declared = _pyproject()["project"]["version"]
    assert declared == "0.8.0"
    assert nodus.__version__ == declared
    assert sodus.__version__ == declared
    assert config.VERSION == declared


def test_the_two_console_scripts_point_at_the_two_packages():
    project = _pyproject()
    scripts = project["project"]["scripts"]
    assert scripts["nodus"] == "nodus.cli:main"
    assert scripts["sodus"] == "sodus.cli:main"
    include = project["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "nodus*" in include
    assert "sodus*" in include


def test_sodus_is_the_same_parser_under_another_name():
    from nodus.cli import build_parser

    assert build_parser("sodus").prog == "sodus"
    assert build_parser("nodus").prog == "nodus"
    # Same command surface, not a second one.
    assert _command_names(build_parser("sodus")) == _command_names(build_parser("nodus"))


def test_python_dash_m_sodus_prints_sodus():
    proc = _run("-m", "sodus", "--version")
    assert proc.returncode == 0, _text(proc)
    assert _text(proc).strip() == "sodus 0.8.0"


def test_python_dash_m_nodus_keeps_its_own_name():
    proc = _run("-m", "nodus", "--version")
    assert proc.returncode == 0, _text(proc)
    assert _text(proc).strip() == "nodus 0.8.0"


@pytest.fixture()
def homes(tmp_path, monkeypatch):
    """Point the resolver's two defaults inside tmp_path and clear the overrides."""
    monkeypatch.delenv("SODUS_HOME", raising=False)
    monkeypatch.delenv("NODUS_HOME", raising=False)
    monkeypatch.setattr(config, "DEFAULT_HOME", tmp_path / ".nodus")
    monkeypatch.setattr(config, "SODUS_HOME_DEFAULT", tmp_path / ".sodus")
    return tmp_path


def test_sodus_home_beats_nodus_home(homes, monkeypatch):
    monkeypatch.setenv("SODUS_HOME", str(homes / "explicit-sodus"))
    monkeypatch.setenv("NODUS_HOME", str(homes / "explicit-nodus"))
    assert config._resolve_home() == homes / "explicit-sodus"


def test_nodus_home_is_used_when_it_is_the_only_override(homes, monkeypatch):
    monkeypatch.setenv("NODUS_HOME", str(homes / "explicit-nodus"))
    assert config._resolve_home() == homes / "explicit-nodus"


def test_an_existing_sodus_home_wins_over_the_nodus_default(homes):
    (homes / ".sodus").mkdir()
    (homes / ".nodus").mkdir()
    assert config._resolve_home() == homes / ".sodus"


def test_without_sodus_home_the_nodus_default_stays(homes):
    assert config._resolve_home() == homes / ".nodus"


def test_resolving_the_home_creates_nothing(homes):
    # A dry resolve must not leave a ~/.sodus behind: only migrate-home writes one.
    config._resolve_home()
    assert not (homes / ".sodus").exists()
    assert not (homes / ".nodus").exists()
