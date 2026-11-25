"""Tests for aplane.py — A-plane ephemeral handoff context."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from sodus import aplane, coordinator, config


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_TASK = {
    "id": "task-aplane-test",
    "title": "Test task",
    "goal": "refactor handoff.py",
    "acceptance": "all tests pass",
}

_PLAN = coordinator.CoordinatorPlan(
    executor_route="codex",
    effort="medium",
    reviewer_route="dsh",
    review_kind="advisory",
    difficulty=3,
    kind="code",
    needs=["tool_use"],
    confidence=0.6,
    basis="rules-only",
    rationale="difficulty 3 code task",
    assessment={},
    scoring={},
)


# ---------------------------------------------------------------------------
# build()
# ---------------------------------------------------------------------------

class TestBuild:
    def test_returns_aplane_ctx(self):
        ctx = aplane.build("run-001", _TASK, _PLAN)
        assert isinstance(ctx, aplane.APlaneCtx)

    def test_fields_populated(self):
        ctx = aplane.build("run-001", _TASK, _PLAN, verify_state="ok", diff_stat="1 file changed")
        assert ctx.run_id == "run-001"
        assert ctx.task_id == _TASK["id"]
        assert ctx.executor_route == _PLAN.executor_route
        assert ctx.reviewer_route == _PLAN.reviewer_route
        assert ctx.review_kind == _PLAN.review_kind
        assert ctx.verify_state == "ok"
        assert ctx.diff_stat == "1 file changed"

    def test_changed_files_list(self):
        ctx = aplane.build("run-001", _TASK, _PLAN, changed_files=["a.py", "b.py"])
        assert ctx.changed_files == ["a.py", "b.py"]

    def test_schema_version(self):
        ctx = aplane.build("run-001", _TASK, _PLAN)
        assert ctx.schema_version == 1


# ---------------------------------------------------------------------------
# to_json() / to_prompt_fragment()
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_to_json_is_dict(self):
        ctx = aplane.build("run-001", _TASK, _PLAN)
        data = ctx.to_json()
        assert isinstance(data, dict)

    def test_to_json_round_trip(self):
        ctx = aplane.build("run-001", _TASK, _PLAN)
        data = ctx.to_json()
        encoded = json.dumps(data, ensure_ascii=False)
        decoded = json.loads(encoded)
        assert decoded["run_id"] == "run-001"
        assert decoded["executor_route"] == _PLAN.executor_route

    def test_to_prompt_fragment_has_key_fields(self):
        ctx = aplane.build("run-001", _TASK, _PLAN, verify_state="red", diff_stat="2 files changed")
        frag = ctx.to_prompt_fragment()
        assert "run-001" in frag
        assert "red" in frag
        assert "advisor" in frag.lower() or "advisory" in frag

    def test_to_prompt_fragment_includes_diff_stat(self):
        ctx = aplane.build("run-001", _TASK, _PLAN, diff_stat="handoff.py | 42 +++")
        frag = ctx.to_prompt_fragment()
        assert "handoff.py" in frag


# ---------------------------------------------------------------------------
# save() / load()
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_save_and_load_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "HOME", tmp_path)
        ctx = aplane.build("run-persist-001", _TASK, _PLAN, verify_state="ok")
        saved = aplane.save(ctx)
        assert saved.exists()

        loaded = aplane.load("run-persist-001")
        assert loaded is not None
        assert loaded.run_id == ctx.run_id
        assert loaded.executor_route == ctx.executor_route
        assert loaded.reviewer_route == ctx.reviewer_route
        assert loaded.verify_state == ctx.verify_state

    def test_load_nonexistent_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "HOME", tmp_path)
        result = aplane.load("run-does-not-exist")
        assert result is None

    def test_load_corrupt_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "HOME", tmp_path)
        path = aplane._ctx_path("run-corrupt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bad json", encoding="utf-8")
        result = aplane.load("run-corrupt")
        assert result is None

    def test_load_non_dict_returns_none(self, tmp_path, monkeypatch):
        """JSON that is not a dict (e.g. a list) must return None, not raise."""
        monkeypatch.setattr(config, "HOME", tmp_path)
        path = aplane._ctx_path("run-listjson")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]", encoding="utf-8")
        result = aplane.load("run-listjson")
        assert result is None

    def test_save_creates_parent_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "HOME", tmp_path)
        ctx = aplane.build("run-nested-001", _TASK, _PLAN)
        path = aplane.save(ctx)
        assert path.parent.is_dir()
        assert path.exists()
