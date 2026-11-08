"""Console helper processes must never flash a terminal window on Windows."""

import inspect
import os

from nodus import board, config, daemon, observe, pet, plan, propose, review, runner, verify


def test_no_window_flag_is_windows_only_constant():
    assert config.NO_WINDOW == (0x08000000 if os.name == "nt" else 0)


def test_every_console_spawn_site_opts_out_of_a_window():
    # Source-level guard: these modules shell out to console programs (git,
    # taskkill, zstd, pytest). Popen on the worker path already uses
    # CREATE_NO_WINDOW; these subprocess.run sites used to pop a console
    # window when the daemon had no console of its own.
    for mod in (verify, propose, plan, review, observe, daemon, runner):
        src = inspect.getsource(mod)
        runs = src.count("subprocess.run(")
        flags = src.count("creationflags=config.NO_WINDOW")
        assert runs >= 1, mod.__name__
        assert flags == runs, f"{mod.__name__}: {runs} subprocess.run, {flags} no-window"


def test_verify_run_passes_creationflags(monkeypatch, tmp_path):
    captured = {}

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr(verify.subprocess, "run", fake_run)
    result = verify.run(["python", "-c", "pass"], tmp_path)
    assert result["ok"] is True
    assert captured["creationflags"] == config.NO_WINDOW
