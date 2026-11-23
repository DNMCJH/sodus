"""Branding: the product is Sodus; identifiers are sodus."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv), cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, timeout=120
    )


def _text(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout + proc.stderr).decode("utf-8", "replace")


def test_python_dash_m_sodus_keeps_its_name():
    proc = _run(sys.executable, "-m", "sodus", "--help")
    assert proc.returncode == 0, _text(proc)
    text = _text(proc)
    assert "usage: sodus" in text
    # The only legitimate nodus mention is the migrate-home source directory.


def test_the_sodus_console_script_shows_sodus():
    exe = shutil.which("sodus")
    if exe is None:
        pytest.skip("sodus is not on PATH; pip install -e . adds it")
    proc = _run(exe, "--help")
    assert proc.returncode == 0, _text(proc)
    text = _text(proc)
    assert "usage: sodus" in text
