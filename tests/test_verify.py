"""Verification stays deterministic: detection, exit codes, and persisted evidence."""

from __future__ import annotations

import sys

from sodus import config, db, verify


def test_detect_pytest_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    found = verify.detect(tmp_path)
    assert found["kind"] == "pytest"
    assert found["cmd"] == [sys.executable, "-m", "pytest", "-q"]


def test_detect_node_from_package_json(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest"}}', encoding="utf-8")
    found = verify.detect(tmp_path)
    assert found["kind"] == "node"
    assert found["cmd"][0] in ("npm", "pnpm")


def test_detect_none_for_empty_dir(tmp_path):
    assert verify.detect(tmp_path) is None


def test_run_records_exit_code_and_output(tmp_path):
    result = verify.run(
        [sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"], tmp_path
    )
    assert result["ok"] is False
    assert result["exit"] == 3
    assert "boom" in result["output"]


def test_run_missing_binary_is_not_ok(tmp_path):
    result = verify.run(["definitely-not-a-real-binary-xyz"], tmp_path)
    assert result["ok"] is False
    assert result["exit"] == -2


def test_persist_writes_artifact_and_event(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    result = verify.run([sys.executable, "-c", "import sys; print('red'); sys.exit(1)"], tmp_path)
    event = verify.persist("run-test", "task-test", "red", result, note="after writer")

    path = tmp_path / "runs" / "run-test" / "verify-red.txt"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "# verify red" in text
    assert "red" in text

    artifacts = db.artifacts_for("run-test")
    assert len(artifacts) == 1
    assert artifacts[0]["kind"] == "verify"
    assert artifacts[0]["summary"] == "red"

    events = db.events_after("run-test")
    assert events[-1]["type"] == "verify.red"
    assert events[-1]["payload"]["exit"] == 1
    assert events[-1]["payload"]["ok"] is False
    assert event["path"] == str(path)


def test_persist_without_run_id_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    assert verify.persist("", "task-test", "red", {"ok": False}) == {}


def test_test_file_hashes_tracks_relative_paths(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text("assert 1\n", encoding="utf-8")
    (tmp_path / "test_root.py").write_text("assert 2\n", encoding="utf-8")
    hashes = verify.test_file_hashes(tmp_path)
    assert set(hashes) == {"tests/test_a.py", "test_root.py"}
