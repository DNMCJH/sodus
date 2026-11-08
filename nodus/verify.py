"""Deterministic verification.

The point of this module is to be the one acceptance signal that does not depend
on what an agent says about its own work. It detects a test runner, runs it, and
returns the exit code.

`persist()` turns a verification result into evidence: the full output becomes an
artifact on disk, and a `verify.<phase>` event lands in the run's stream. Without
that, "red then green" is a claim in a terminal scrollback; with it, the next
session can re-read what actually failed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from . import config, db

MAX_TAIL = 4000
MAX_OUTPUT = 200_000


def detect(cwd: Path) -> dict | None:
    """Find a test command for this project, or None."""
    for marker in ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg"):
        if (cwd / marker).exists():
            return {
                "kind": "pytest",
                "cmd": [sys.executable, "-m", "pytest", "-q"],
                "reason": f"{marker} present",
            }
    pkg = cwd / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        if (data.get("scripts") or {}).get("test"):
            runner = "pnpm" if (cwd / "pnpm-lock.yaml").exists() else "npm"
            return {
                "kind": "node",
                "cmd": [runner, "test", "--silent"],
                "reason": "package.json scripts.test",
            }
    if (cwd / "Cargo.toml").exists():
        return {"kind": "cargo", "cmd": ["cargo", "test", "-q"], "reason": "Cargo.toml"}
    if (cwd / "go.mod").exists():
        return {"kind": "go", "cmd": ["go", "test", "./..."], "reason": "go.mod"}
    return None


def run(cmd: list[str], cwd: Path, timeout: int = 600) -> dict:
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=config.NO_WINDOW,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return {
            "ok": proc.returncode == 0,
            "exit": proc.returncode,
            "output": output[-MAX_OUTPUT:],
            "tail": output[-MAX_TAIL:],
            "duration_ms": int((time.time() - started) * 1000),
            "cmd": cmd,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "exit": -1,
            "output": f"timeout after {timeout}s",
            "tail": f"timeout after {timeout}s",
            "duration_ms": int((time.time() - started) * 1000),
            "cmd": cmd,
        }
    except OSError as exc:
        return {
            "ok": False,
            "exit": -2,
            "output": f"cannot execute: {exc}",
            "tail": f"cannot execute: {exc}",
            "duration_ms": int((time.time() - started) * 1000),
            "cmd": cmd,
        }


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()[:16]


def persist(run_id: str, task_id: str, phase: str, result: dict, note: str = "") -> dict:
    """Attach a verification result to a run: file on disk, event in the stream."""
    if not run_id:
        return {}
    out_dir = config.HOME / "runs" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"verify-{phase}.txt"
    cmd = result.get("cmd") or []
    body = result.get("output") or result.get("tail") or ""
    header = (
        f"# verify {phase}\n"
        f"# ok={result.get('ok')} exit={result.get('exit')} "
        f"duration_ms={result.get('duration_ms')}\n"
        f"# cmd: {' '.join(str(c) for c in cmd)}\n"
        + (f"# note: {note}\n" if note else "")
        + "\n"
    )
    path.write_text(header + body, encoding="utf-8")

    db.add_artifact(
        run_id, task_id, "verify", str(path), _digest(path), summary=phase
    )
    payload = {
        "phase": phase,
        "ok": bool(result.get("ok")),
        "exit": result.get("exit"),
        "duration_ms": result.get("duration_ms"),
        "cmd": cmd,
        "path": str(path),
        "tail": (result.get("tail") or "")[-1500:],
        "note": note,
    }
    db.append_event(run_id, db.next_seq(run_id), f"verify.{phase}", payload)
    return payload


def test_file_hashes(cwd: Path, limit: int = 50) -> dict[str, str]:
    """Hash test files so we can detect an agent weakening its own tests."""
    out: dict[str, str] = {}
    candidates: list[Path] = []
    tests_dir = cwd / "tests"
    if tests_dir.is_dir():
        candidates.extend(p for p in tests_dir.rglob("*.py") if p.is_file())
    candidates.extend(cwd.glob("test_*.py"))
    candidates.extend(cwd.glob("*_test.py"))
    for path in candidates[:limit]:
        try:
            rel = str(path.relative_to(cwd)).replace("\\", "/")
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        except OSError:
            continue
    return out


def infra_failure(result):
    code = result.get('exit')
    return code in (-1, -2) or (isinstance(code, int) and (code & 0xffffffff) in (0xC000013A, 0xC0000142, 0xC0000005))
