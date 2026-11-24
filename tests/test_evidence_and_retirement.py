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


def test_evidence_git_diff_stat_and_proposal_basis(tmp_path, monkeypatch):
    import subprocess
    from sodus import config as _cfg

    monkeypatch.setattr(_cfg, "HOME", tmp_path / "home")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    target = repo / "app.py"
    target.write_text("a = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    target.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")

    task = db.create_task("edit app", "pytest -q")
    run = db.create_run(task["id"], "codex", str(repo))
    db.update_run(run["id"], status="failed", error="exit code 1", started_at=time.time() - 3, ended_at=time.time())
    db.add_artifact(run["id"], task["id"], "file", str(target), "sha", rel_path="app.py")

    evidence.fresh_cache()
    got = evidence.collect(db.get_run(run["id"]), "none")
    assert got["change"]["added"] == 2
    assert got["change"]["removed"] == 0
    assert "git diff --numstat" in got["change"]["basis"]

    # Proposal files take precedence over working-tree numstat.
    db.create_proposal(
        run_id=run["id"],
        task_id=task["id"],
        repo=str(repo),
        branch="sodus/r1",
        worktree=str(repo),
        base="abc123",
        patch_path=str(tmp_path / "p.patch"),
        files=[{"path": "app.py", "added": 5, "removed": 1}],
        stat="1 file changed, 5 insertions(+), 1 deletion(-)",
    )
    evidence.fresh_cache()
    got_prop = evidence.collect(db.get_run(run["id"]), "none")
    assert got_prop["change"]["added"] == 5
    assert got_prop["change"]["removed"] == 1
    assert "提案分支" in got_prop["change"]["basis"]


def test_evidence_no_evidence_fallback_and_retry_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    task = db.create_task("flaky", "pytest -q")
    r1 = db.create_run(task["id"], "codex", str(tmp_path))
    db.update_run(r1["id"], status="failed", error="", started_at=time.time() - 20, ended_at=time.time() - 18)

    evidence.fresh_cache()
    empty = evidence.collect(db.get_run(r1["id"]), "none")
    assert any("没有工具调用记录" in m for m in empty["missing"])
    assert any("没有原始报错" in m for m in empty["missing"])
    assert any("没有记录改动文件" in m for m in empty["missing"])

    # Retry child appears in attempts list on the chain.
    r2 = db.create_run(task["id"], "codex", str(tmp_path))
    db.update_run(
        r2["id"],
        status="failed",
        error="exit code 2",
        parent_run_id=r1["id"],
        note="auto-retry 1/2",
        started_at=time.time() - 10,
        ended_at=time.time() - 6,
    )
    evidence.fresh_cache()
    chain_ev = evidence.collect(db.get_run(r2["id"]), "none")
    assert len(chain_ev["attempts"]) == 1
    assert chain_ev["attempts"][0]["n"] == 1
    assert chain_ev["attempts"][0]["run"] == r2["id"]
    assert chain_ev["attempts"][0]["outcome"] == "exit 2"
    assert chain_ev["attempts"][0]["seconds"] == 4


def test_cli_reap_orphans_dry_run_and_apply(tmp_path, monkeypatch, capsys):
    from sodus.cli import build_parser

    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setenv("SODUS_WORKSPACE", "ws-main")
    task = db.create_task("orphan test", "pytest")
    r_local = db.create_run(task["id"], "codex", str(tmp_path))
    db.update_run(r_local["id"], status="queued", owner_pid=999999)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO run(id,workspace_id,agent,task_id,cwd,status,started_at,owner_pid) VALUES (?,?,?,?,?,?,?,?)",
            ("run-foreign-ws", "ws-other", "codex", "t-other", str(tmp_path), "queued", time.time(), 999999),
        )

    # Default dry-run touches nothing and stays inside current workspace.
    args = build_parser().parse_args(["debug", "reap-orphans", "--json"])
    assert args.needs_daemon is False
    assert args.func(args) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True
    assert dry["count"] == 1
    assert dry["reaped"] == 0
    assert db.get_run(r_local["id"])["status"] == "queued"

    # --all-workspaces --apply reaps both local and foreign workspace orphans.
    args = build_parser().parse_args(["debug", "reap-orphans", "--all-workspaces", "--apply", "--json"])
    assert args.func(args) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["dry_run"] is False
    assert applied["reaped"] == 2
    assert db.get_run(r_local["id"])["status"] == "failed"
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM run WHERE id='run-foreign-ws'").fetchone()
        assert row["status"] == "failed"

