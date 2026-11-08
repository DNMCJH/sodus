"""State contract, live/cached boundaries and actual HTTP latency."""
import json
import math
import time
import urllib.request
import urllib.error

import pytest
from nodus import board, config, db, observe, server, state


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(observe, "observed_runs", lambda start: [])
    state._CACHE = None
    db.create_task("state", "pytest")
    return tmp_path


def seed(home, status="running", **fields):
    task = db.list_tasks()[0]
    row = db.create_run(task["id"], "codex", str(home), route="sol")
    db.update_run(row["id"], status=status, **fields)
    return row["id"]


def ask(status):
    with db.connect() as conn:
        conn.execute("INSERT INTO ask(id,request_id,workspace_id,payload_hash,text,status,created_at,expires_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (status, status, config.workspace_id(), "hash", "hello", status, time.time(), time.time()+60, time.time()))


def test_schema_counts_and_ledger(home, monkeypatch):
    now = time.time()
    seed(home, "completed", usage_json=json.dumps({"cost": 1.25, "currency": "CNY"}))
    seed(home, "completed", usage_json=json.dumps({"cost": 9, "currency": "USD"}))
    seed(home, "running")
    seed(home, "queued")
    seed(home, "stalled")
    for status in ("queued", "running", "answered", "failed"):
        ask(status)
    db.memory_add_candidate("pending")
    # A board render is deliberately forbidden in the lightweight endpoint.
    monkeypatch.setattr(board, "board_state", lambda *a: pytest.fail("full board called"))
    got = state.snapshot(now)
    assert set(got) == {"version", "workspace_id", "now", "running", "counts", "today", "asks", "memory"}
    assert got["version"] == "0.7.1"
    assert len(got["running"]) == 3
    assert set(got["running"][0]) == {"id", "task_id", "agent", "route", "started_at", "cwd"}
    assert got["counts"] == {"running": 1, "queued": 1, "needs_you": 0, "unverifiable": 1,
                             "proposals": 0, "asks_open": 1, "memory_candidates": 1}
    assert got["asks"] == {"open": 1, "running": 1, "done": 1, "failed": 1}
    assert got["today"] == {"CNY": 1.25, "runs": 5, "unpriced": 3}
    assert got["memory"] == {"candidates_pending": 1}


def test_running_cap_sort_and_uncapped_counts(home):
    for n in range(25):
        seed(home, started_at=time.time()+n)
    got = state.snapshot()
    assert len(got["running"]) == 20
    assert got["counts"]["running"] == 25
    starts = [r["started_at"] for r in got["running"]]
    assert starts == sorted(starts, reverse=True)


def test_cache_expiry_live_reads_and_scope(home, monkeypatch):
    tick = [10.0]
    monkeypatch.setattr(state.time, "monotonic", lambda: tick[0])
    first = state.snapshot()
    seed(home)
    ask("queued")
    db.memory_add_candidate("new pending")
    second = state.snapshot()
    assert second["counts"]["running"] == 1
    assert second["asks"]["open"] == 1
    assert second["today"] == first["today"]
    assert second["memory"]["candidates_pending"] == 0
    second["today"]["CNY"] = 900
    assert state.snapshot()["today"]["CNY"] == 0
    tick[0] += state.TTL
    assert state.snapshot()["memory"]["candidates_pending"] == 1
    assert state.snapshot()["today"]["runs"] == 1
    monkeypatch.setattr(config, "HOME", home / "other")
    assert state.snapshot()["today"]["runs"] == 0


def test_spend_and_unverifiable_match_board(home, monkeypatch):
    seed(home, "completed")
    seed(home, "failed", error="connection reset", ended_at=time.time())
    monkeypatch.setattr(state.recover, "enabled", lambda: True)
    monkeypatch.setattr(state.recover, "retry_plan", lambda *a, **kw: {"gate": "cost-cap"})
    assert state.snapshot()["counts"]["needs_you"] == 1
    assert state.snapshot()["counts"]["unverifiable"] == 1
    monkeypatch.setattr(state.recover, "enabled", lambda: False)
    assert state.snapshot()["counts"]["needs_you"] == 0


def request(httpd, path="/state", token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


def test_http_auth_errors_and_p95(home, mcp_http, capsys):
    for n in range(30):
        seed(home, "running" if n % 2 else "completed")
    with pytest.raises(urllib.error.HTTPError) as error:
        request(mcp_http)
    assert error.value.code == 401
    assert json.load(error.value) == {"error": "unauthorized"}
    with pytest.raises(urllib.error.HTTPError) as error:
        request(mcp_http, token="wrong")
    assert error.value.code == 401
    token = config.token()
    with pytest.raises(urllib.error.HTTPError) as error:
        request(mcp_http, "/state/unknown", token)
    assert error.value.code == 404
    request(mcp_http, token=token)  # excluded warm-up
    elapsed = []
    for _ in range(20):
        start = time.perf_counter()
        assert request(mcp_http, token=token)["version"] == "0.7.1"
        elapsed.append((time.perf_counter()-start)*1000)
    p95 = sorted(elapsed)[math.ceil(len(elapsed)*.95)-1]
    with capsys.disabled():
        print(f"/state HTTP p95={p95:.3f}ms (20 serial requests, warm-up excluded)")
    assert p95 < 200
