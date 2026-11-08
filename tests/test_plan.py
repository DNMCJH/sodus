import copy
import json
import sqlite3
import subprocess
import pytest
from nodus import config, db, plan
from sodus.cli import main


@pytest.fixture
def valid(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "state")
    monkeypatch.setenv("NODUS_WORKSPACE", "plan-test")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    value = dict(
        schema_version=1,
        goal="test",
        non_goals=[],
        base_sha=sha,
        recon=dict(files=["."], tests=["python -m pytest"], agents_md=None),
        milestones=[dict(id="m1", title="first", exit_criteria=["tests pass"])],
        nodes=[
            dict(
                id="n1",
                milestone="m1",
                title="test",
                description="test plan",
                deps=[],
                file_scope=["."],
                acceptance=["python -m pytest"],
                difficulty=1,
                route_hint="sol",
                budget_cny=0,
            )
        ],
        risks=["risk"],
        rollback="restore",
        review_points=["review"],
    )
    return value, repo


@pytest.mark.parametrize(
    "kind", ["base_sha", "path", "cycle", "acceptance", "duplicate_id", "milestone"]
)
def test_six_errors(valid, kind):
    value, repo = valid
    if kind == "base_sha":
        value["base_sha"] = "0" * 40
    if kind == "path":
        value["nodes"][0]["file_scope"] = ["missing.py"]
    if kind == "cycle":
        value["nodes"][0]["deps"] = ["n1"]
    if kind == "acceptance":
        value["nodes"][0]["acceptance"] = []
    if kind == "duplicate_id":
        value["nodes"].append(copy.deepcopy(value["nodes"][0]))
    if kind == "milestone":
        value["milestones"].append(
            dict(id="empty", title="empty", exit_criteria=["pass"])
        )
    assert kind in {e["kind"] for e in plan.validate_plan(value, repo)}
    with pytest.raises(ValueError):
        plan.import_plan(value, repo)
    assert plan.list_plans() == []


def test_roundtrip_cli(valid, tmp_path, capsys):
    value, repo = valid
    assert plan.validate_plan(value, repo) == []
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    assert main(["plan", "validate", str(path), "--cwd", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"]
    assert main(["plan", "import", str(path), "--cwd", str(repo)]) == 0
    pid = capsys.readouterr().out.strip()
    stored = plan.get_plan(pid)
    assert stored["plan_json"] == value
    assert stored["status"] == "validated" and stored["version"] == 1
    assert stored["nodes"][0]["node_id"] == "n1"
    assert stored["approved_at"] is None
    assert main(["plan", "show", pid, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == pid
    assert main(["plan", "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == pid
    assert main(["plan", "show", "missing"]) == 1
    assert main(["plan", "validate", str(path) + "missing", "--json"]) == 1
    path.write_text("{", encoding="utf-8")
    assert main(["plan", "validate", str(path), "--json"]) == 1


def test_v5_migration(valid):
    config.ensure_dirs()
    with sqlite3.connect(config.HOME / "nodus.sqlite3") as c:
        c.executescript(db.SCHEMA)
        c.execute("INSERT INTO meta VALUES('schema_version','5')")
        c.execute("INSERT INTO task(id,title) VALUES('old','preserved')")
    for _ in range(3):
        db._SCHEMA_READY.clear()
        with db.connect() as c:
            assert (
                c.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]
                == "6"
            )
            assert (
                c.execute("SELECT title FROM task WHERE id='old'").fetchone()[0]
                == "preserved"
            )
            assert c.execute("SELECT count(*) FROM plan").fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM plan_node").fetchone()[0] == 0


@pytest.mark.parametrize(
    "value", [None, [], {"nodes": [None]}, {"nodes": [{"id": []}]}]
)
def test_malformed(value):
    assert plan.validate_plan(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("budget_cny", -1),
        ("budget_cny", float("nan")),
        ("difficulty", True),
        ("difficulty", 6),
        ("acceptance", ["完成即可"]),
        ("acceptance", "pytest"),
        ("file_scope", ["../"]),
        ("deps", ["missing"]),
    ],
)
def test_node_constraints(valid, field, value):
    document, repo = valid
    document["nodes"][0][field] = value
    assert plan.validate_plan(document, repo)


def test_workspace_isolation(valid, monkeypatch):
    value, repo = valid
    pid = plan.import_plan(value, repo)
    monkeypatch.setenv("NODUS_WORKSPACE", "other-plan-workspace")
    assert plan.get_plan(pid) is None
    assert plan.list_plans() == []


def test_cli_invalid_plan(valid, tmp_path, capsys):
    value, repo = valid
    value["nodes"][0]["acceptance"] = ["完成即可"]
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    for action in ("validate", "import"):
        assert main(["plan", action, str(path), "--cwd", str(repo), "--json"]) == 1
        assert json.loads(capsys.readouterr().out)["errors"]
    assert plan.list_plans() == []
