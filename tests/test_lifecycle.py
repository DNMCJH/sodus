"""The state machine, written down once, and pinned to what already runs.

Three modules already answer "what is this run doing": `recover.retry_plan`
names why a failed run must not be retried, `lineage._run_summary` decides
`ok`/`unverified`, and `server.board_state` splits the same rows into
needs_you / handling / stopped. None of them is changed here. These tests copy
their rules out -- including reading recover's gate names out of its own source
-- and assert `lifecycle` does not contradict them.

The last test replays 20 rows from the real `~/.sodus/sodus.sqlite3` (a
read-only snapshot, so a running daemon cannot be disturbed) and asserts the
same agreement on history rather than on fixtures. It skips on a machine that
has no history database; it is a regression on real data, not a CI dependency.
"""

from __future__ import annotations

import inspect
import json
import re
import sqlite3
import time
from pathlib import Path

import pytest

from sodus import config, db, lifecycle, lineage, recover, runner, server

@pytest.fixture(autouse=True)
def recovery_enabled(monkeypatch):
    monkeypatch.setenv("SODUS_AUTO_RECOVER", "1")


REAL_DB = Path.home() / ".sodus" / "sodus.sqlite3"

#: Every gate `recover.retry_plan` names today. The source-reading test below
#: fails if this list and the source disagree, which is the point: a new gate
#: must be given an attention bucket instead of silently becoming "done".
RECOVER_GATES = {
    "not-failed",
    "deterministic",
    "missing-context",
    "task-gone",
    "retry-limit",
    "already-retried",
    "task-running",
    "no-end-time",
    "too-old",
    "same-signature",
    "over-budget",
    "budget",
    "cost-cap",
    "backoff",
}


# ---- the table ---------------------------------------------------------


def test_run_states_are_the_flow():
    assert lifecycle.states(lifecycle.RUN) == (
        "queued",
        "running",
        "stalled",
        "completed",
        "failed",
    )


def test_proposal_states():
    assert lifecycle.states(lifecycle.PROPOSAL) == ("open", "merged", "rejected")


def test_terminal_states():
    # `stalled` is not terminal: it can go back to running or to failed.
    assert lifecycle.terminal(lifecycle.RUN) == {"completed", "failed"}
    assert lifecycle.terminal(lifecycle.PROPOSAL) == {"merged", "rejected"}


def test_terminal_returns_a_fresh_set():
    marks = lifecycle.terminal(lifecycle.RUN)
    marks.add("stalled")
    assert "stalled" not in lifecycle.terminal(lifecycle.RUN)


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("queued", "running"),
        ("running", "completed"),
        ("running", "failed"),
        ("running", "stalled"),
        ("stalled", "running"),
        ("stalled", "failed"),
        ("open", "merged"),
        ("open", "rejected"),
    ],
)
def test_declared_edges_are_legal(from_state, to_state):
    entity = lifecycle.PROPOSAL if from_state == "open" else lifecycle.RUN
    assert lifecycle.can_transition(entity, from_state, to_state)


@pytest.mark.parametrize(
    "entity,from_state,to_state",
    [
        (lifecycle.RUN, "queued", "completed"),
        (lifecycle.RUN, "queued", "failed"),
        (lifecycle.RUN, "running", "queued"),
        (lifecycle.RUN, "stalled", "completed"),
        (lifecycle.RUN, "completed", "running"),
        (lifecycle.RUN, "failed", "running"),
        (lifecycle.RUN, "completed", "failed"),
        (lifecycle.RUN, "failed", "failed"),
        (lifecycle.PROPOSAL, "merged", "open"),
        (lifecycle.PROPOSAL, "rejected", "merged"),
        (lifecycle.PROPOSAL, "open", "stalled"),
    ],
)
def test_everything_else_is_illegal(entity, from_state, to_state):
    assert not lifecycle.can_transition(entity, from_state, to_state)


def test_a_state_cannot_transition_to_itself():
    for state in lifecycle.states(lifecycle.RUN):
        assert not lifecycle.can_transition(lifecycle.RUN, state, state)


def test_unknown_state_is_not_a_transition():
    # A typo must not look like a legal edge, and must not raise either: a
    # caller checking a row from disk should get "no" and move on.
    assert not lifecycle.can_transition(lifecycle.RUN, "bogus", "running")
    assert not lifecycle.can_transition(lifecycle.RUN, "running", "bogus")


def test_unknown_entity_raises():
    for call in (
        lambda: lifecycle.states("widget"),
        lambda: lifecycle.terminal("widget"),
        lambda: lifecycle.can_transition("widget", "open", "merged"),
    ):
        with pytest.raises(ValueError):
            call()


# ---- needs_attention ---------------------------------------------------


def _run(status: str, error: str = "", **over) -> dict:
    return {"id": "run-lifecycle", "status": status, "error": error, **over}


def test_queued_needs_nothing():
    assert lifecycle.needs_attention(_run("queued"), {}) == ""


def test_running_with_fresh_progress_needs_nothing():
    now = 1_000_000.0
    facts = {"now": now, "last_progress_at": now - 30, "stall_after": 900}
    assert lifecycle.needs_attention(_run("running"), facts) == ""


def test_running_without_progress_is_stalled():
    now = 1_000_000.0
    facts = {"now": now, "last_progress_at": now - 901, "stall_after": 900}
    assert lifecycle.needs_attention(_run("running"), facts) == "stalled"


def test_exactly_the_window_is_not_yet_stalled():
    # The runner kills on "older than IDLE_TIMEOUT", not "at least"; a run at
    # exactly 900s is still given the benefit of the doubt.
    now = 1_000_000.0
    facts = {"now": now, "last_progress_at": now - 900, "stall_after": 900}
    assert lifecycle.needs_attention(_run("running"), facts) == ""


def test_running_without_progress_facts_makes_no_claim():
    assert lifecycle.needs_attention(_run("running"), {}) == ""
    assert lifecycle.needs_attention(_run("running"), {"now": 1000.0}) == ""
    assert lifecycle.needs_attention(_run("running"), {"last_progress_at": 10.0}) == ""


def test_running_with_unreadable_progress_facts_makes_no_claim():
    facts = {"now": "later", "last_progress_at": object()}
    assert lifecycle.needs_attention(_run("running"), facts) == ""


def test_recorded_stalled_state_is_stalled():
    assert lifecycle.needs_attention(_run("stalled"), {}) == "stalled"


def test_completed_is_done_whatever_the_verify_state():
    # `completed` is an exit code. lineage's ok and unverified both land here;
    # the verify question is not a lifecycle question.
    for verify in ("ok", "red", "none"):
        assert lifecycle.needs_attention(_run("completed"), {"verify": verify}) == "done"


def test_a_failed_run_without_a_gate_makes_no_claim():
    assert lifecycle.needs_attention(_run("failed", "exit code 1"), {}) == ""
    assert (
        lifecycle.needs_attention(
            _run("failed", "exit code 1"), {"classification": "transient"}
        )
        == ""
    )


@pytest.mark.parametrize("gate", lifecycle.RETRY_PENDING_GATES)
def test_retry_pending_gates(gate):
    facts = {"classification": "transient", "retry_gate": gate}
    assert lifecycle.needs_attention(_run("failed", "exit code 1"), facts) == "retry-pending"


@pytest.mark.parametrize("gate", lifecycle.AWAITING_HUMAN_GATES)
def test_spend_gates_await_a_person(gate):
    facts = {"classification": "transient", "retry_gate": gate}
    assert lifecycle.needs_attention(_run("failed", "exit code 1"), facts) == "awaiting-human"


@pytest.mark.parametrize("gate", lifecycle.SETTLED_GATES)
def test_settled_gates_are_done(gate):
    facts = {"classification": "transient", "retry_gate": gate}
    assert lifecycle.needs_attention(_run("failed", "exit code 1"), facts) == "done"


def test_deterministic_failure_is_done_not_a_human_question():
    run = _run("failed", "401 Unauthorized: invalid api key")
    assert recover.classify(run) == "deterministic"
    plan = recover.retry_plan(run)
    assert plan["gate"] == "deterministic"
    facts = {
        "classification": recover.classify(run),
        "retry_gate": plan["gate"],
        "retry_enabled": True,
    }
    # The board's rule, copied: `_needs_you` skips deterministic causes, so a
    # person is not asked. lifecycle agrees by calling it done.
    assert lifecycle.needs_attention(run, facts) == "done"


def test_recovery_off_turns_a_pending_retry_into_done():
    facts = {"classification": "transient", "retry_gate": "", "retry_enabled": False}
    assert lifecycle.needs_attention(_run("failed", "orphaned"), facts) == "done"


def test_unknown_gate_makes_no_claim():
    facts = {"classification": "transient", "retry_gate": "some-future-gate"}
    assert lifecycle.needs_attention(_run("failed", "exit code 1"), facts) == ""


def test_unknown_status_makes_no_claim():
    assert lifecycle.needs_attention(_run("mystery"), {}) == ""
    assert lifecycle.needs_attention({}, {}) == ""


# ---- rules copied out of the existing modules --------------------------


def test_every_recover_gate_has_an_attention_bucket():
    source = inspect.getsource(recover.retry_plan)
    found = set(re.findall(r'stop\(\s*"([a-z-]+)"', source))
    assert found == RECOVER_GATES, (
        "recover.retry_plan's gates changed; lifecycle's buckets must be updated"
        f": new={sorted(found - RECOVER_GATES)} gone={sorted(RECOVER_GATES - found)}"
    )
    covered = (
        set(lifecycle.RETRY_PENDING_GATES)
        | set(lifecycle.AWAITING_HUMAN_GATES)
        | set(lifecycle.SETTLED_GATES)
    )
    assert found <= covered, f"gates with no attention bucket: {sorted(found - covered)}"


def test_spend_gates_match_state_module():
    assert lifecycle.AWAITING_HUMAN_GATES == ("budget", "cost-cap", "over-budget")


def test_retry_pending_is_the_recover_handling_pair():
    assert '("", "backoff")' in inspect.getsource(recover.recovering)
    assert lifecycle.RETRY_PENDING_GATES == ("", "backoff")


def test_stall_window_is_the_runner_s_window():
    assert lifecycle.STALL_AFTER == 900.0
    reaper = inspect.signature(runner.RUNNER.reconcile).parameters["stale_seconds"]
    assert reaper.default == 900


def test_lineage_ok_and_unverified_both_map_to_done():
    # The rule from lineage._run_summary, copied verbatim: ok is completed AND
    # verify ok; unverified is completed AND verify none. Neither is a failure.
    for verify, expected_ok, expected_unverified in (
        ("ok", True, False),
        ("none", False, True),
        ("red", False, False),
    ):
        row = lineage._run_summary(
            {
                "id": "run-lineage",
                "status": "completed",
                "route": "claude",
                "agent": "claude",
                "provider": "relay",
                "model": "m",
                "usage_json": "{}",
                "started_at": 10.0,
                "ended_at": 20.0,
            },
            verify,
        )
        assert row["ok"] is expected_ok
        assert row["unverified"] is expected_unverified
        assert lifecycle.needs_attention(row, {"verify": verify}) == "done"


def test_lineage_never_calls_a_failed_run_ok():
    row = lineage._run_summary(
        {
            "id": "run-lineage",
            "status": "failed",
            "route": "claude",
            "agent": "claude",
            "provider": "relay",
            "model": "m",
            "usage_json": "{}",
            "started_at": 10.0,
            "ended_at": 20.0,
        },
        "ok",
    )
    assert not row["ok"] and not row["unverified"]
    facts = {"classification": "transient", "retry_gate": "backoff"}
    assert lifecycle.needs_attention(row, facts) == "retry-pending"


# ---- the board's own grouping, on seeded runs --------------------------


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("SODUS_WORKSPACE", "ws-lifecycle")
    monkeypatch.delenv("SODUS_DAILY_BUDGET", raising=False)
    monkeypatch.setenv("SODUS_AUTO_RECOVER", "1")
    (tmp_path / "home").mkdir()
    return tmp_path


def _seed(home, error: str, *, status: str = "failed", usage: dict | None = None) -> dict:
    cwd = home / "work"
    cwd.mkdir(parents=True, exist_ok=True)
    task = db.create_task(goal="修好登录", acceptance="pytest -q", title="修复登录")
    run = db.create_run(
        task["id"], "claude", str(cwd), route="claude", provider="relay"
    )
    now = time.time()
    db.update_run(
        run["id"],
        status=status,
        error=error,
        started_at=now - 40,
        ended_at=now - 5,
        usage_json=json.dumps(usage or {}),
    )
    return db.get_run(run["id"])


def _facts(run: dict, now: float | None = None) -> dict:
    plan = recover.retry_plan(run, now=now)
    return {
        "classification": recover.classify(run),
        "retry_gate": plan["gate"],
        "retry_enabled": recover.enabled(),
        "last_progress_at": run.get("started_at") or 0,
        "now": time.time() if now is None else now,
    }


def test_a_retryable_failure_is_handling(home):
    run = _seed(home, "exit code 1")
    facts = _facts(run)
    assert facts["retry_gate"] in lifecycle.RETRY_PENDING_GATES
    assert lifecycle.needs_attention(run, facts) == "retry-pending"


def test_a_deterministic_failure_is_stopped_not_asked(home):
    from sodus import state

    run = _seed(home, "401 Unauthorized: invalid api key")
    facts = _facts(run)
    assert facts["retry_gate"] == "deterministic"
    assert lifecycle.needs_attention(run, facts) == "done"
    assert state.snapshot()["counts"]["needs_you"] == 0


def test_the_money_gate_is_the_only_human_question(home, monkeypatch):
    from sodus import state

    _seed(home, "", status="completed", usage={"cost": 0.5, "currency": "CNY"})
    run = _seed(home, "exit code 1", usage={"cost": 0.4, "currency": "CNY"})
    monkeypatch.setenv("SODUS_DAILY_BUDGET", "1.0")
    facts = _facts(run)
    assert facts["retry_gate"] == "budget"
    assert lifecycle.needs_attention(run, facts) == "awaiting-human"
    assert state.snapshot()["counts"]["needs_you"] == 1


# ---- 20 real historical runs -------------------------------------------


def _snapshot_db(source: Path, dest: Path) -> None:
    """Consistent read-only copy; the live daemon keeps writing to `source`."""
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _dominant_workspace(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT workspace_id, COUNT(*) AS n FROM run"
            " WHERE workspace_id IS NOT NULL AND workspace_id != ''"
            " GROUP BY workspace_id ORDER BY n DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return str(row[0]) if row else "ws-default"


@pytest.fixture()
def history(tmp_path, monkeypatch):
    """A read-only snapshot of the real database, configured as this app's HOME."""
    if not REAL_DB.is_file():
        pytest.skip(f"no history database at {REAL_DB}")
    home_dir = tmp_path / "history-home"
    home_dir.mkdir()
    snapshot = home_dir / "sodus.sqlite3"
    try:
        _snapshot_db(REAL_DB, snapshot)
    except sqlite3.DatabaseError as exc:  # locked / mid-write: not this test's job
        pytest.skip(f"could not snapshot {REAL_DB}: {exc}")
    monkeypatch.setattr(config, "HOME", home_dir)
    monkeypatch.setenv("SODUS_WORKSPACE", _dominant_workspace(snapshot))
    monkeypatch.setenv("SODUS_AUTO_RECOVER", "1")
    monkeypatch.delenv("SODUS_DAILY_BUDGET", raising=False)
    return home_dir


def _gate_at_failure(run: dict) -> str:
    """recover's gate for this row at the moment it entered `failed`."""
    ended = float(run.get("ended_at") or run.get("started_at") or 0)
    return recover.retry_plan(run, now=ended + 1)["gate"]


def _sample(window: list[dict], every: list[dict]) -> list[dict]:
    """20 rows: the live one, recent work, and a retry-pending case from history.

    `window` is the newest slice the board itself sees; `every` is the whole
    history. The failure rows come from both ends of the window because a run
    that was retry-pending *at its own failure time* is exactly the case whose
    gate changes with age (`too-old`). If the window no longer holds one, the
    oldest matching row from `every` takes a slot, so the retry-pending branch
    is exercised on real data instead of only on a fixture.
    """
    picked: list[dict] = []
    seen: set[str] = set()

    def take(rows):
        for run in rows:
            if run["id"] in seen or len(picked) >= 20:
                continue
            seen.add(run["id"])
            picked.append(run)

    pending_at_failure = [
        r
        for r in every
        if r["status"] == "failed" and _gate_at_failure(r) in lifecycle.RETRY_PENDING_GATES
    ][:1]
    take([r for r in window if r["status"] in ("queued", "running")])
    take(pending_at_failure)
    take([r for r in window if r["status"] == "completed"][:6])
    take([r for r in window if r["status"] == "failed"][:4])
    take([r for r in window if r["status"] == "failed"][-6:])
    take([r for r in window if r["status"] == "failed"])
    take([r for r in window if r["status"] == "completed"])
    take(window)
    take(every)
    return picked


def test_twenty_real_runs_agree_with_recover_lineage_and_the_board(history):
    runs = db.list_runs(limit=100, kind="work")
    every = db.list_runs(limit=100000, kind="work")
    sample = _sample(runs, every)
    if len(sample) < 20:
        pytest.skip(f"history has only {len(sample)} work runs")
    assert len(sample) == 20

    ids = [run["id"] for run in sample]
    marks = db.verify_states(ids)
    now = time.time()

    mismatches: list[str] = []
    tokens: dict[str, int] = {}
    tokens_at_failure: dict[str, int] = {}

    for run in sample:
        run_id = run["id"]
        status = str(run.get("status") or "")
        assert status in lifecycle.states(lifecycle.RUN)

        plan = recover.retry_plan(run, now=now)
        facts = {
            "classification": recover.classify(run),
            "retry_gate": plan["gate"],
            "retry_enabled": recover.enabled(),
            "last_progress_at": max(
                [ev["ts"] for ev in db.events_after(run_id, limit=100000)]
                or [run.get("started_at") or 0]
            ),
            "now": now,
        }
        token = lifecycle.needs_attention(run, facts)
        tokens[token] = tokens.get(token, 0) + 1
        assert token in {"", "retry-pending", "stalled", "awaiting-human", "done"}

        # recover's conclusion -> lifecycle's token
        if status == "completed":
            if token != "done":
                mismatches.append(f"{run_id}: completed, lifecycle says {token!r}")
        elif status == "failed":
            gate = plan["gate"]
            if recover.classify(run) == "deterministic" and token != "done":
                mismatches.append(f"{run_id}: recover=deterministic, lifecycle={token!r}")
            elif gate in lifecycle.RETRY_PENDING_GATES and token != "retry-pending":
                mismatches.append(f"{run_id}: recover gate={gate!r}, lifecycle={token!r}")
            elif gate in lifecycle.AWAITING_HUMAN_GATES and token != "awaiting-human":
                mismatches.append(f"{run_id}: recover gate={gate!r}, lifecycle={token!r}")
            elif gate in lifecycle.SETTLED_GATES and token != "done":
                mismatches.append(f"{run_id}: recover gate={gate!r}, lifecycle={token!r}")
        else:
            if token not in ("", "stalled"):
                mismatches.append(f"{run_id}: live run, lifecycle says {token!r}")

        if status == "failed":
            # The same real row, judged at the moment it entered `failed`: a
            # gate is a fact about that moment (`too-old` is only true later).
            ended = float(run.get("ended_at") or run.get("started_at") or now)
            plan_then = recover.retry_plan(run, now=ended + 1)
            facts_then = dict(facts, retry_gate=plan_then["gate"], now=ended + 1)
            token_then = lifecycle.needs_attention(run, facts_then)
            tokens_at_failure[token_then] = tokens_at_failure.get(token_then, 0) + 1
            gate_then = plan_then["gate"]
            if gate_then in lifecycle.RETRY_PENDING_GATES and token_then != "retry-pending":
                mismatches.append(
                    f"{run_id}: recover at failure gate={gate_then!r},"
                    f" lifecycle={token_then!r}"
                )
            elif gate_then in lifecycle.AWAITING_HUMAN_GATES and token_then != "awaiting-human":
                mismatches.append(
                    f"{run_id}: recover at failure gate={gate_then!r},"
                    f" lifecycle={token_then!r}"
                )
            elif gate_then in lifecycle.SETTLED_GATES and token_then != "done":
                mismatches.append(
                    f"{run_id}: recover at failure gate={gate_then!r},"
                    f" lifecycle={token_then!r}"
                )

        # lineage's ok/unverified -> lifecycle's token; never a failure
        if run.get("task_id"):
            data = lineage.for_task(run["task_id"])
            row = next((r for r in data.get("runs", []) if r["id"] == run_id), None)
            if row is not None:
                verify = marks.get(run_id, "none")
                if row["ok"]:
                    assert status == "completed" and verify == "ok"
                    if token != "done":
                        mismatches.append(f"{run_id}: lineage ok, lifecycle={token!r}")
                if row["unverified"]:
                    assert status == "completed" and verify == "none"
                    if token != "done":
                        mismatches.append(
                            f"{run_id}: lineage unverified, lifecycle={token!r}"
                        )
                assert not (row["ok"] and row["unverified"])
                if status != "completed":
                    assert not row["ok"] and not row["unverified"]

    assert not mismatches, "lifecycle disagrees with the code it was copied from:\n" + "\n".join(
        mismatches
    )
    # The sample must actually exercise the terminal paths; an all-live database
    # would make this test vacuous. `stalled` needs no writer today, so it is
    # allowed (but not required) to be absent.
    assert tokens.get("done", 0) >= 1
    if any(
        r["status"] == "failed"
        and _gate_at_failure(r) in lifecycle.RETRY_PENDING_GATES
        for r in every
    ):
        assert tokens_at_failure.get("retry-pending", 0) >= 1, (
            "history held a run that was retry-pending at failure time, but the"
            f" sample judged none that way: {tokens_at_failure}"
        )

# ---- Sodus 0.6 derived task/ask contract --------------------------------

@pytest.mark.parametrize('status,verify,review,accepted,expected', [
    (None, None, None, None, 'open'),
    ('queued', None, None, None, 'running'),
    ('running', None, None, None, 'running'),
    ('stalled', None, None, None, 'stalled'),
    ('failed', 'failed', None, None, 'failed'),
    ('completed', 'red', 'accept', 'human', 'failed'),
    *[('completed', 'pass', r, None, 'needs_review') for r in ('revise','reject','unparsed','unknown')],
    ('completed', 'pass', None, None, 'done'),
    ('completed', 'pass', 'accept', None, 'done'),
    ('completed', 'unverified', None, None, 'unverified'),
    ('completed', 'unverified', 'accept', 'human', 'unverified'),
])
def test_task_mapping(status, verify, review, accepted, expected):
    task = {'accepted_by': accepted}
    runs = [{'id': 'work', 'status': status}] if status else []
    reviews = [{'verdict': review}] if review else []
    assert lifecycle.task_state(task, runs, reviews, verify) == expected
    assert task == {'accepted_by': accepted}


def test_task_latest_work_and_targeted_review():
    runs = [dict(id='old', status='failed', started_at=1),
            dict(id='new', status='completed', started_at=2),
            dict(id='review', kind='review', status='failed', started_at=3)]
    reviews = [dict(target_run='new', verdict='revise', ts=1),
               dict(target_run='new', verdict='accept', ts=2),
               dict(target_run='old', verdict='reject', ts=3)]
    assert lifecycle.task_state({}, runs, reviews, {'new': 'ok'}) == 'done'


@pytest.mark.parametrize('status,expected', [('queued','queued'), ('running','running'),
    ('stalled','stalled'), ('completed','answered'), ('failed','failed')])
def test_ask_mapping(status, expected):
    assert lifecycle.ask_state({'status':status}, expires_at=10, now=10) == expected
    assert lifecycle.ask_state({'status':status}, 'task', 10, 10) == 'promoted'
    assert lifecycle.ask_state({'status':status}, 'task', 10, 11) == 'expired'


@pytest.mark.parametrize('version', [None, '1', '2'])
def test_accepted_by_upgrade_and_reconnect(home, version):
    config.ensure_dirs()
    path = config.HOME / 'sodus.sqlite3'
    if version:
        with sqlite3.connect(path) as c:
            c.executescript("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
                            "CREATE TABLE task(id TEXT PRIMARY KEY, workspace_id TEXT, session_id TEXT, title TEXT, goal TEXT, acceptance TEXT, status TEXT DEFAULT 'open', budget_json TEXT, created_at REAL);"
                            "INSERT INTO task(id,title,acceptance) VALUES('legacy','保留','pytest');")
            c.execute("INSERT INTO meta VALUES('schema_version',?)", (version,))
    for _ in range(3):
        db._SCHEMA_READY.clear()
        with db.connect() as c:
            assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
            assert [r[1] for r in c.execute('PRAGMA table_info(task)')].count('accepted_by') == 1
            if version:
                assert tuple(c.execute("SELECT title,acceptance,accepted_by FROM task WHERE id='legacy'").fetchone()) == ('保留','pytest',None)
    task = db.create_task('confirmation', acceptance='pytest')
    db.update_task(task['id'], accepted_by='human')
    assert db.get_task(task['id'])['accepted_by'] == 'human'
    assert db.get_task(task['id'])['acceptance'] == 'pytest'


@pytest.mark.parametrize('verify', ['ok', 'none', 'red'])
def test_lineage_matches_task_derivation(verify):
    run = dict(id='work', status='completed')
    row = lineage._run_summary(run, verify)
    state = lifecycle.task_state({}, [run], verify=verify)
    assert row['ok'] == (state == 'done')
    assert row['unverified'] == (state == 'unverified')

@pytest.mark.parametrize('green,review,expected', [(True,'accept','done'), (True,'revise','needs_review'), (False,'revise','failed')])
def test_fix_cache_uses_task_mapping(home, monkeypatch, green, review, expected):
    from sodus import fix
    task = db.create_task('repair', 'pytest')
    target = db.create_run(task['id'], 'codex', str(home))
    db.update_run(target['id'], status='completed')
    monkeypatch.setattr(fix, 'latest_review', lambda *a: dict(verdict='revise', target_run=target['id'], reviewer='claude'))
    monkeypatch.setattr(fix, 'pick_fixer', lambda *a: 'codex')
    monkeypatch.setattr(fix.verify, 'detect', lambda *a: {'cmd':['pytest']})
    monkeypatch.setattr(fix.verify, 'test_file_hashes', lambda *a: {})
    monkeypatch.setattr(fix.verify, 'run', lambda *a: {'ok':green, 'exit':0 if green else 1})
    monkeypatch.setattr(fix.verify, 'persist', lambda *a, **k: {})
    def start(tid, agent, cwd, **kwargs):
        run = db.create_run(tid, agent, cwd)
        db.update_run(run['id'], status='completed')
        return db.get_run(run['id'])
    monkeypatch.setattr(fix.RUNNER, 'start', start)
    monkeypatch.setattr(fix.RUNNER, 'wait', lambda rid, **k: db.get_run(rid))
    monkeypatch.setattr(fix.review_mod, 'run_review', lambda *a, **k: dict(ok=True, verdict=review, target_run=target['id']))
    fix.run_fix(target['id'], rounds=1)
    assert db.get_task(task['id'])['status'] == expected


@pytest.mark.parametrize('review,expected', [('accept','done'), ('revise','needs_review')])
def test_tdd_cache_uses_task_mapping(home, monkeypatch, review, expected):
    from sodus import tdd
    task = db.create_task('implement', 'pytest')
    monkeypatch.setattr(tdd, 'plan', lambda *a: {'cmd':['pytest']})
    monkeypatch.setattr(tdd.verify, 'test_file_hashes', lambda *a: {})
    checks = iter([{'ok':False,'tail':'red'}, {'ok':True,'tail':'green'}])
    monkeypatch.setattr(tdd.verify, 'run', lambda *a: next(checks))
    monkeypatch.setattr(tdd.verify, 'persist', lambda *a, **k: {})
    def start(tid, agent, cwd, **kwargs):
        run = db.create_run(tid, agent, cwd)
        db.update_run(run['id'], status='completed')
        return db.get_run(run['id'])
    monkeypatch.setattr(tdd.RUNNER, 'start', start)
    monkeypatch.setattr(tdd.RUNNER, 'wait', lambda rid: db.get_run(rid))
    monkeypatch.setattr(tdd.review_mod, 'run_review', lambda *a, **k: dict(ok=True, verdict=review))
    tdd.run_tdd(task['id'], home, review=True)
    assert db.get_task(task['id'])['status'] == expected
