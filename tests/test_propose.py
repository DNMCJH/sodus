"""Proposals: parallel work on a branch, merged under the main tree's lock."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from nodus import agents, config, db, propose, providers, routes, runner

WRITER = textwrap.dedent(
    """
    import json, pathlib, sys

    def emit(obj):
        print(json.dumps(obj), flush=True)

    emit({"type": "system", "subtype": "init", "session_id": "s-prop", "model": "fake"})
    pathlib.Path("out.txt").write_text("proposal work", encoding="utf-8")
    emit({"type": "result", "result": "done", "session_id": "s-prop",
          "usage": {"input_tokens": 3, "output_tokens": 1}})
    sys.exit(0)
    """
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, (args, proc.stderr)
    return proc.stdout


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    (tmp_path / "home").mkdir()
    work = tmp_path / "repo"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "test")
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    return work


def test_prepare_creates_a_worktree_on_its_own_branch(repo):
    ctx = propose.prepare(repo, "run-abc123")
    assert Path(ctx["worktree"]).is_dir()
    assert (Path(ctx["worktree"]) / "README.md").exists()
    branches = _git(repo, "branch", "--list", "nodus/*")
    assert "nodus/run-abc123" in branches
    assert ctx["base"] == _git(repo, "rev-parse", "HEAD").strip()


def test_collect_is_none_when_nothing_changed(repo):
    ctx = propose.prepare(repo, "run-empty")
    assert propose.collect(ctx, "t") is None


def test_collect_commits_and_produces_a_patch(repo):
    ctx = propose.prepare(repo, "run-work")
    tree = Path(ctx["worktree"])
    (tree / "new.txt").write_text("added\n", encoding="utf-8")
    (tree / "README.md").write_text("hello\nworld\n", encoding="utf-8")

    collected = propose.collect(ctx, "a task")

    assert "2 file(s)" in collected["stat"]
    patch = Path(collected["patch_path"]).read_text(encoding="utf-8")
    assert "new.txt" in patch and "world" in patch
    paths = {f["path"] for f in collected["files"]}
    assert paths == {"new.txt", "README.md"}


def test_merge_brings_the_branch_into_the_main_tree(repo):
    ctx = propose.prepare(repo, "run-merge")
    tree = Path(ctx["worktree"])
    (tree / "feature.txt").write_text("from the proposal\n", encoding="utf-8")
    collected = propose.collect(ctx, "add feature")
    record = propose.record("run-merge", "task-1", ctx, collected)

    assert not (repo / "feature.txt").exists()  # nothing leaked before merge
    result = propose.merge(record)

    assert result["ok"] is True
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "from the proposal\n"
    assert db.get_proposal(record["id"])["status"] == "merged"
    assert not Path(ctx["worktree"]).exists()
    assert "nodus/run-merge" not in _git(repo, "branch", "--list", "nodus/*")


def test_merge_refuses_while_someone_owns_the_main_tree(repo):
    ctx = propose.prepare(repo, "run-busy")
    (Path(ctx["worktree"]) / "x.txt").write_text("x\n", encoding="utf-8")
    record = propose.record("run-busy", "t", ctx, propose.collect(ctx, "x"))
    db.lock_path(repo, "run-someone-else")

    result = propose.merge(record)

    assert result["ok"] is False and "already being written" in result["reason"]
    assert db.get_proposal(record["id"])["status"] == "open"
    assert not (repo / "x.txt").exists()


def test_a_conflict_leaves_the_proposal_open_and_the_tree_untouched(repo):
    ctx = propose.prepare(repo, "run-conflict")
    tree = Path(ctx["worktree"])
    (tree / "README.md").write_text("proposal side\n", encoding="utf-8")
    record = propose.record("run-conflict", "t", ctx, propose.collect(ctx, "edit readme"))
    # main moves under it
    (repo / "README.md").write_text("main side\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main edit")

    result = propose.merge(record)

    assert result["ok"] is False and result["conflict"] is True
    assert db.get_proposal(record["id"])["status"] == "open"
    assert (repo / "README.md").read_text(encoding="utf-8") == "main side\n"


def test_reject_removes_the_branch_and_the_worktree(repo):
    ctx = propose.prepare(repo, "run-nope")
    (Path(ctx["worktree"]) / "junk.txt").write_text("junk\n", encoding="utf-8")
    record = propose.record("run-nope", "t", ctx, propose.collect(ctx, "junk"))

    propose.reject(record)

    assert db.get_proposal(record["id"])["status"] == "rejected"
    assert not Path(ctx["worktree"]).exists()
    assert not (repo / "junk.txt").exists()


def test_a_proposal_needs_a_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    (tmp_path / "plain").mkdir()
    with pytest.raises(ValueError, match="not a git repository"):
        propose.prepare(tmp_path / "plain", "run-x")


# ---- the end-to-end path through the runner ---------------------------


def test_a_propose_run_leaves_the_main_tree_alone(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "auto_verify", lambda: True)
    script = tmp_path / "fake.py"
    script.write_text(WRITER, encoding="utf-8")
    monkeypatch.setitem(
        agents.SPECS,
        "fake",
        agents.AgentSpec(
            name="fake", vendor="claude", command=(sys.executable, str(script)),
            accepts=(providers.ANTHROPIC,),
        ),
    )
    providers.upsert(
        providers.Provider(name="p", protocol=providers.ANTHROPIC,
                           base_url="https://p.test", key_secret="K")
    )
    routes.upsert(routes.Route(name="fake", agent="fake", providers=("p",)))
    monkeypatch.setattr(config, "load_secrets", lambda ws=None: {"K": "sk"})
    task = db.create_task("add a file")

    run = runner.RUNNER.start(task["id"], "fake", cwd=str(repo), propose_run=True)
    done = runner.RUNNER.wait(run["id"], timeout=120)

    assert done["status"] == "completed"
    assert str(config.HOME) in done["cwd"]  # it ran in the worktree
    assert not (repo / "out.txt").exists()

    rows = db.list_proposals("open")
    assert len(rows) == 1
    record = rows[0]
    assert record["run_id"] == done["id"]
    assert "1 file(s)" in record["stat"]
    handoff = (config.HOME / "runs" / done["id"] / "handoff.md").read_text(encoding="utf-8")
    assert "主工作区没有被改动" in handoff
    assert f"nodus merge {record['id']}" in handoff

    assert propose.merge(record)["ok"] is True
    assert (repo / "out.txt").read_text(encoding="utf-8") == "proposal work"
