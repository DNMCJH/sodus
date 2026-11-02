"""Phase A of the rename: the product is Sodus, the implementation is nodus.

The line these tests hold: user-visible text says Sodus, identifiers stay
nodus. The package, the state directory (`~/.nodus`) and the localStorage key
`nodus-theme` are identifiers -- renaming any of them would move data or reset
a user's theme, so they are deliberately not part of phase A.

The CLI has two console scripts over one module. `nodus --help` keeps its name,
`sodus --help` follows argv[0]; both must run the same code, same daemon and
same database.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nodus import ui

ROOT = Path(__file__).resolve().parents[1]


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv),
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
    )


def _text(proc: subprocess.CompletedProcess) -> str:
    # The CLI reconfigures stdout to UTF-8, but a failure path may not; decoding
    # with replacement keeps the ASCII we assert on readable either way.
    return (proc.stdout + proc.stderr).decode("utf-8", "replace")


def test_python_dash_m_nodus_keeps_its_name():
    proc = _run(sys.executable, "-m", "nodus", "--help")
    assert proc.returncode == 0, _text(proc)
    text = _text(proc)
    assert "usage: nodus" in text
    assert "nodus" in text


def test_the_sodus_console_script_shows_sodus():
    exe = shutil.which("sodus")
    if exe is None:
        pytest.skip(
            "sodus is not on PATH (the console script is not installed); "
            "`pip install -e .` adds it. The `python -m nodus` check above "
            "still covers the entry point."
        )
    proc = _run(exe, "--help")
    assert proc.returncode == 0, _text(proc)
    text = _text(proc)
    assert "usage: sodus" in text
    assert "sodus" in text


def test_the_board_page_says_sodus():
    assert "<title>Sodus</title>" in ui.PAGE
    assert "<h1>SODUS</h1>" in ui.PAGE
    assert "Sodus" in ui.PAGE
    assert "SODUS" in ui.PAGE
    assert "<h1>NODUS</h1>" not in ui.PAGE


def test_the_theme_key_stayed_nodus():
    # An identifier, not a display string. Renaming it would silently drop the
    # theme every existing user picked.
    assert 'localStorage.getItem("nodus-theme")' in ui.PAGE
