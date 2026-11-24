"""State contract, live/cached boundaries and actual HTTP latency."""
import json
import math
import time
import urllib.request
import urllib.error

import pytest
from sodus import config, db, observe, server, state


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
    got = state.snapshot(now)
    assert set(got) == {"version", "workspace_id", "now", "running", "counts", "today", "asks", "memory", "decisions"}
    assert got["version"] == "0.9.0"
    assert len(got["running"]) == 3
    assert set(got["running"][0]) == {"id", "task_id", "agent", "route", "started_at", "cwd"}
    assert got["counts"] == {"running": 1, "queued": 1, "needs_you": 0, "unverifiable": 1,
                             "proposals": 0, "asks_open": 1, "memory_candidates": 1, "open_decisions": 0}
    assert got["asks"] == {"open": 1, "running": 1, "done": 1, "failed": 1}
    assert got["today"] == {"CNY": 1.25, "runs": 5, "unpriced": 3}
    assert got["memory"] == {"candidates_pending": 1}
    assert got["decisions"] == {"open": 0, "items": []}


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
        assert request(mcp_http, token=token)["version"] == "0.9.0"
        elapsed.append((time.perf_counter()-start)*1000)
    p95 = sorted(elapsed)[math.ceil(len(elapsed)*.95)-1]
    with capsys.disabled():
        print(f"/state HTTP p95={p95:.3f}ms (20 serial requests, warm-up excluded)")
    assert p95 < 200



def test_four_hosts_concurrent_state_and_task_creation(home, mcp_http):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    token = config.token()
    barrier = Barrier(4)

    def client(index):
        barrier.wait(timeout=10)
        snapshot = request(mcp_http, token=token)
        req = urllib.request.Request(
            f'http://127.0.0.1:{mcp_http.server_port}/tasks',
            data=json.dumps({'goal': f'host-{index}', 'acceptance': 'created'}).encode(),
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            method='POST')
        with urllib.request.urlopen(req, timeout=15) as response:
            assert response.status == 201
            task = json.load(response)
        assert request(mcp_http, token=token)['workspace_id'] == snapshot['workspace_id']
        return task['id']

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(client, range(4)))
    assert len(set(ids)) == 4
    assert all(db.get_task(task_id) for task_id in ids)


def test_state_and_decisions_zero_git_fast_with_250_roots(home, tmp_path, monkeypatch):
    from sodus import handoff

    roots_dir = tmp_path / "many_projects"
    roots_dir.mkdir()
    with db.connect() as conn:
        for i in range(250):
            d = roots_dir / f"p{i:03d}"
            d.mkdir()
            if i == 0:
                (d / ".git").mkdir()
                handoff.init(d)
                handoff.decide_draft("是否继续推进 P2？", recommend="先收口 P1.5", cwd=d)
            conn.execute(
                "INSERT INTO run(id,workspace_id,agent,task_id,cwd,status,started_at) VALUES (?,?,?,?,?,?,?)",
                (f"run-many-{i}", config.workspace_id(), "codex", f"t-{i}", str(d), "completed", 100 + i),
            )

    def forbid_git(*args, **kwargs):
        pytest.fail("state/decisions must never spawn git subprocesses")

    monkeypatch.setattr(handoff, "git", forbid_git)
    state._CACHE = None
    t0 = time.perf_counter()
    snap = state.snapshot()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert snap["counts"]["open_decisions"] == 1
    assert snap["decisions"]["open"] == 1
    assert snap["decisions"]["items"][0]["id"] == "D-001"
    assert snap["decisions"]["items"][0]["project"] == "p000"
    assert elapsed_ms < 500, f"state.snapshot took {elapsed_ms:.1f}ms across 250 roots"

