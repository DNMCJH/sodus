"""Tests for extracted run evidence and P0 retired-module regression guards."""
from __future__ import annotations

import importlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from sodus import config, db, evidence

RETIRED_MODULES = ("pet", "mood", "statuslight", "desk", "llm", "ui", "board")

PYTEST_RED = """..F
=================================== FAILURES ===================================
_______________________ test_token_refresh _______________________

    def test_token_refresh():
        token = login()
>       assert refresh(token) == 200
E       assert 401 == 200

tests/test_auth.py:14: AssertionError
=========================== short test summary info ===========================
FAILED tests/test_auth.py::test_token_refresh - assert 401 == 200
1 failed, 2 passed in 0.31s
"""


def test_retired_modules_do_not_exist():
    pkg = Path(__file__).resolve().parent.parent / "sodus"
    for name in RETIRED_MODULES:
        assert not (pkg / f"{name}.py").exists(), f"retired module still present: {name}.py"
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"sodus.{name}")


def test_evidence_collects_red_verify_and_error_details(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    task = db.create_task("fix auth", "pytest -q")
    run = db.create_run(task["id"], "claude", str(tmp_path))
    db.update_run(run["id"], status="completed", started_at=time.time() - 5, ended_at=time.time())
    log = tmp_path / "verify-red.txt"
    log.write_text(PYTEST_RED, encoding="utf-8")
    db.append_event(run["id"], 1, "tool_call", {"name": "Edit", "input": {"file_path": str(tmp_path / "auth.py")}})
    db.append_event(run["id"], 2, "verify.red", {"cmd": ["pytest", "-q"], "exit": 1, "path": str(log)})
    evidence.fresh_cache()
    got = evidence.collect(db.get_run(run["id"]), "red")
    assert "Edit auth.py" in got["action"]
    assert got["test_exit"] == 1
    assert got["tests"][0]["test"] == "test_token_refresh"
    assert "401 == 200" in got["tests"][0]["assertion"]


def test_http_root_json_and_retired_routes_return_404(tmp_path, monkeypatch, mcp_http):
    monkeypatch.setattr(config, "HOME", tmp_path)
    base = f"http://127.0.0.1:{mcp_http.server_port}"
    token = config.token()

    # Public root now returns lightweight JSON metadata instead of HTML board.
    with urllib.request.urlopen(f"{base}/", timeout=10) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("application/json")
        body = json.load(resp)
        assert body["service"] == "sodus"
        assert body["state"] == "/state"

    # /pet is no longer public (returns 401 without token) and 404 with token.
    with pytest.raises(urllib.error.HTTPError) as unauth:
        urllib.request.urlopen(f"{base}/pet", timeout=10)
    assert unauth.value.code == 401

    headers = {"Authorization": f"Bearer {token}"}
    for path in ("/pet", "/board"):
        req = urllib.request.Request(f"{base}{path}", headers=headers)
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=10)
        assert err.value.code == 404, path

    req = urllib.request.Request(
        f"{base}/llm/chat",
        data=b"{}",
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=10)
    assert err.value.code == 404

    # GET /runs/{id} still returns structured evidence for failed runs.
    task = db.create_task("broken", "pytest")
    run = db.create_run(task["id"], "codex", str(tmp_path))
    db.update_run(run["id"], status="failed", error="exit code 1", started_at=time.time() - 2, ended_at=time.time())
    req = urllib.request.Request(f"{base}/runs/{run['id']}", headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.load(resp)
        assert payload["evidence"]["error_tail"] == "exit code 1"
        assert payload["problem"]
