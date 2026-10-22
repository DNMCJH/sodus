"""Session lineage: the chain a conversation was continued from, and its forks.

Every test builds the same two rows a real run builds (a task carries a session
row, the run writes `session_ref_json` only when it ends) and then calls the
same `lineage.attach` the runner calls. No CLI worker is spawned.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from nodus import config, db, lineage
from nodus.cli import main


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-a")
    return tmp_path


def _ref(vendor: str) -> str:
    return json.dumps({"provider": "claude", "session_id": vendor, "cwd": ""})


def sitting(
    title: str,
    *,
    vendor: str = "",
    resume: str = "",
    mode: str = "fresh",
    route: str = "claude",
    status: str = "completed",
    verify: str = "",
    cost: float | None = None,
) -> dict:
    """One sitting: a session row + task + run, linked the way a real run links them.

    Order matters. `session_ref_json` is written when the run *ends*, so it must
    be set after `attach` — otherwise a resume would see its own brand-new ref
    and could not tell its parent from itself.
    """
    session = db.create_session(str(Path.cwd()), title)
    task = db.create_task(f"goal {title}", session_id=session["id"], title=title)
    run = db.create_run(task["id"], "claude", cwd=str(Path.cwd()), route=route)
    lineage.attach(run, task, session_id=resume, session_mode=mode)
    usage = {"cost": cost, "currency": "USD"} if cost is not None else {}
    db.update_run(
        run["id"],
        status=status,
        started_at=time.time() - 10,
        ended_at=time.time(),
        usage_json=json.dumps(usage),
        session_ref_json=_ref(vendor) if vendor else None,
    )
    if verify:
        db.append_event(run["id"], 1, f"verify.{verify}", {"ok": verify == "green"})
    return {
        "session": db.get_session(session["id"]),
        "task": db.get_task(task["id"]),
        "run": db.get_run(run["id"]),
    }


# ---- chain -------------------------------------------------------------


def test_resume_chain_reads_root_to_leaf(home):
    a = sitting("A", vendor="v-a")
    b = sitting("B", vendor="v-a", resume="v-a", mode="resume")
    c = sitting("C", vendor="v-a", resume="v-a", mode="resume")

    chain = lineage.chain(c["session"]["id"])

    assert [row["title"] for row in chain] == ["A", "B", "C"]
    assert [row["relation"] for row in chain] == ["fresh", "resume", "resume"]
    assert [row["origin_run_id"] for row in chain] == [
        a["run"]["id"],
        b["run"]["id"],
        c["run"]["id"],
    ]
    # Root first, self last, and the leaf knows the whole path.
    assert chain[-1]["id"] == c["session"]["id"]
    assert chain[0]["parent_session_id"] in (None, "")


def test_a_fresh_session_has_no_parent_and_owns_its_first_run(home):
    a = sitting("A", vendor="v-a")
    row = a["session"]
    assert row["relation"] == "fresh"
    assert not row["parent_session_id"]
    assert row["origin_run_id"] == a["run"]["id"]


def test_attaching_twice_does_not_duplicate_or_rewrite(home):
    a = sitting("A", vendor="v-a")
    b = sitting("B", vendor="v-a", resume="v-a", mode="resume")
    before = db.get_session(b["session"]["id"])
    lineage.attach(b["run"], b["task"], session_id="v-a", session_mode="resume")
    after = db.get_session(b["session"]["id"])
    assert before == after
    assert after["origin_run_id"] == b["run"]["id"]


# ---- forks -------------------------------------------------------------


def test_two_forks_from_one_parent_are_both_listed(home):
    a = sitting("A", vendor="v-a")
    b = sitting("B", vendor="v-b", resume="v-a", mode="fork")
    c = sitting("C", vendor="v-c", resume="v-a", mode="fork")

    kids = lineage.descendants(a["session"]["id"])

    assert {row["id"] for row in kids} == {b["session"]["id"], c["session"]["id"]}
    assert {row["relation"] for row in kids} == {"branch"}
    assert {row["depth"] for row in kids} == {1}
    assert [row["title"] for row in lineage.chain(b["session"]["id"])] == ["A", "B"]


def test_descendants_include_a_grandchild_with_its_depth(home):
    a = sitting("A", vendor="v-a")
    b = sitting("B", vendor="v-b", resume="v-a", mode="fork")
    c = sitting("C", vendor="v-c", resume="v-b", mode="fork")

    kids = {row["title"]: row for row in lineage.descendants(a["session"]["id"])}

    assert set(kids) == {"B", "C"}
    assert kids["C"]["depth"] == 2
    assert kids["C"]["parent_session_id"] == b["session"]["id"]


def test_branch_from_a_branch_walks_the_short_path(home):
    a = sitting("A", vendor="v-a")
    b = sitting("B", vendor="v-b", resume="v-a", mode="fork")
    c = sitting("C", vendor="v-c", resume="v-b", mode="fork")
    assert [row["title"] for row in lineage.chain(c["session"]["id"])] == ["A", "B", "C"]


# ---- cycles ------------------------------------------------------------


def test_self_reference_does_not_loop(home):
    a = sitting("A", vendor="v-a")
    sid = a["session"]["id"]
    db.set_session_lineage(sid, parent_session_id=sid)

    assert [row["id"] for row in lineage.chain(sid)] == [sid]
    assert lineage.descendants(sid) == []  # the self-link is not a child


def test_two_node_cycle_terminates(home):
    a = sitting("A", vendor="v-a")
    b = sitting("B", vendor="v-b", resume="v-a", mode="fork")
    db.set_session_lineage(a["session"]["id"], parent_session_id=b["session"]["id"])

    chain = lineage.chain(b["session"]["id"])
    assert [row["id"] for row in chain] == [a["session"]["id"], b["session"]["id"]]
    # The walk terminates and does not present b as its own descendant.
    assert [row["id"] for row in lineage.descendants(b["session"]["id"])] == [
        a["session"]["id"]
    ]


# ---- for_run -----------------------------------------------------------


def test_for_run_reports_sitting_chain_siblings_and_proposals(home):
    a = sitting("A", vendor="v-a")
    b = sitting("B", vendor="v-a", resume="v-a", mode="resume")
    prop = db.create_proposal(
        b["run"]["id"], b["task"]["id"], "repo", "nodus/x", "/tmp/wt", "main",
        "/tmp/x.patch", [{"path": "a.py"}], "1 file changed",
    )

    data = lineage.for_run(b["run"]["id"])

    assert data["run"]["id"] == b["run"]["id"]
    assert data["task"]["id"] == b["task"]["id"]
    assert data["session"]["id"] == b["session"]["id"]
    assert [row["id"] for row in data["chain"]] == [
        a["session"]["id"], b["session"]["id"]
    ]
    assert [row["id"] for row in data["runs"]] == [a["run"]["id"]]
    assert [row["id"] for row in data["proposals"]] == [prop["id"]]


def test_for_session_on_a_leaf_still_shows_the_whole_tree(home):
    a = sitting("A", vendor="v-a")
    b = sitting("B", vendor="v-a", resume="v-a", mode="resume")
    c = sitting("C", vendor="v-c", resume="v-a", mode="fork")

    data = lineage.for_session(b["session"]["id"])

    assert [row["id"] for row in data["chain"]] == [
        a["session"]["id"], b["session"]["id"]
    ]
    # B and C share a parent, so the view from B must still contain C.
    assert {row["id"] for row in data["descendants"]} == {
        b["session"]["id"], c["session"]["id"]
    }


def test_cli_lineage_on_a_leaf_marks_it_inside_the_tree(home, capsys):
    a = sitting("A", vendor="v-a")
    b = sitting("B", vendor="v-a", resume="v-a", mode="resume")
    c = sitting("C", vendor="v-c", resume="v-a", mode="fork")
    capsys.readouterr()

    assert main(["lineage", b["session"]["id"]]) == 0

    out = capsys.readouterr().out
    assert a["session"]["id"] in out
    assert b["session"]["id"] in out
    assert c["session"]["id"] in out


# ---- for_task: the evidence behind "who should get this" ---------------


def test_for_task_counts_routes_and_finds_the_first_success(home):
    task = db.create_task("ship the thing")
    ok = db.create_run(task["id"], "claude", cwd=str(Path.cwd()), route="claude")
    db.update_run(
        ok["id"], status="completed", started_at=time.time() - 30,
        ended_at=time.time(), session_ref_json=_ref("v-ok"),
        usage_json=json.dumps({"cost": 0.25, "currency": "USD"}),
    )
    db.append_event(ok["id"], 1, "verify.green", {})
    bad = db.create_run(task["id"], "codex", cwd=str(Path.cwd()), route="codex")
    db.update_run(
        bad["id"], status="failed", error="boom", started_at=time.time() - 5,
        ended_at=time.time(), session_ref_json=_ref("v-bad"),
        usage_json=json.dumps({"cost": 0.05, "currency": "USD"}),
    )

    data = lineage.for_task(task["id"])

    assert data["attempts"] == 2
    assert data["ok"] == 1
    assert data["failed"] == 1
    assert data["first_success"] == ok["id"]
    assert [row["id"] for row in data["runs"]] == [ok["id"], bad["id"]]  # oldest first
    routes = {row["route"]: row for row in data["routes"]}
    assert routes["claude"]["ok"] == 1 and routes["claude"]["failed"] == 0
    assert routes["codex"]["ok"] == 0 and routes["codex"]["failed"] == 1
    assert routes["claude"]["cost"] == pytest.approx(0.25)
    assert routes["codex"]["cost"] == pytest.approx(0.05)
    # The task's own sitting is the tail of the history it was launched from.
    assert data["sessions"][-1]["id"] == task["session_id"]
    assert data["runs"][1]["error"] == "boom"


def test_a_completed_run_with_red_verification_is_not_a_success(home):
    task = db.create_task("looks green, is not")
    run = db.create_run(task["id"], "claude", cwd=str(Path.cwd()), route="claude")
    db.update_run(run["id"], status="completed", started_at=time.time() - 2,
                  ended_at=time.time())
    db.append_event(run["id"], 1, "verify.red", {})

    data = lineage.for_task(task["id"])

    assert data["runs"][0]["verify"] == "red"
    assert data["runs"][0]["ok"] is False
    assert data["ok"] == 0
    assert data["failed"] == 1


# ---- old database ------------------------------------------------------


def test_an_old_session_table_is_migrated_not_rebuilt(home):
    path = config.HOME / "nodus.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session (
          id TEXT PRIMARY KEY,
          workspace_id TEXT,
          workspace TEXT,
          title TEXT,
          status TEXT DEFAULT 'open',
          created_at REAL
        );
        INSERT INTO session(id, title, created_at)
          VALUES('ses-old', 'written before lineage existed', 1.0);
        """
    )
    conn.commit()
    conn.close()

    row = db.get_session("ses-old")

    assert row["title"] == "written before lineage existed"
    assert row["parent_session_id"] is None
    assert row["origin_run_id"] is None
    assert row["relation"] == "fresh"  # no parent is what "root" means
    cols = {r[1] for r in db.connect().execute("PRAGMA table_info(session)")}
    assert {"parent_session_id", "origin_run_id", "relation"} <= cols
    assert [r["id"] for r in lineage.chain("ses-old")] == ["ses-old"]


# ---- id resolution and the CLI ----------------------------------------


def test_vendor_session_id_resolves_to_its_latest_sitting(home):
    a = sitting("A", vendor="v-a")
    b = sitting("B", vendor="v-a", resume="v-a", mode="resume")

    kind, ident = lineage.resolve("v-a")

    assert kind == "session"
    assert ident == b["session"]["id"] != a["session"]["id"]


def test_cli_lineage_on_a_session_prints_the_tree(home, capsys):
    a = sitting("A", vendor="v-a")
    sitting("B", vendor="v-b", resume="v-a", mode="fork")
    capsys.readouterr()

    assert main(["lineage", a["session"]["id"]]) == 0

    out = capsys.readouterr().out
    assert a["session"]["id"] in out
    # The tree connector is not asserted on: pytest's capture stream encoding is
    # the OS default, and this test is about the rows, not the glyphs.
    assert "branch" in out
    assert '"A"' in out


def test_cli_lineage_json_is_structured(home, capsys):
    a = sitting("A", vendor="v-a")
    capsys.readouterr()

    assert main(["lineage", a["session"]["id"], "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["kind"] == "session"
    assert data["session"]["id"] == a["session"]["id"]
    assert data["chain"][0]["relation"] == "fresh"


def test_cli_lineage_on_a_run_and_a_task(home, capsys):
    a = sitting("A", vendor="v-a")
    capsys.readouterr()
    assert main(["lineage", a["run"]["id"]]) == 0
    run_out = capsys.readouterr().out
    assert f"run     {a['run']['id']}" in run_out
    assert "same conversation" in run_out

    assert main(["lineage", a["task"]["id"]]) == 0
    task_out = capsys.readouterr().out
    assert f"task     {a['task']['id']}" in task_out
    assert "routes:" in task_out


def test_unknown_id_is_a_clear_error(home):
    with pytest.raises(ValueError, match="unknown id"):
        lineage.resolve("sess-does-not-exist")
