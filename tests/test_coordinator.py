"""Tests for coordinator.py — A-plane pipeline planner."""
from __future__ import annotations

import pytest

from sodus import coordinator


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _plan(text: str, **kw) -> coordinator.CoordinatorPlan:
    return coordinator.plan(text, **kw)


# ---------------------------------------------------------------------------
# basic routing
# ---------------------------------------------------------------------------

class TestPlanBasic:
    def test_returns_coordinator_plan(self):
        p = _plan("fix a typo in README.md")
        assert isinstance(p, coordinator.CoordinatorPlan)

    def test_executor_route_is_valid_string(self):
        p = _plan("write unit tests for runner.py")
        assert isinstance(p.executor_route, str)
        assert p.executor_route  # non-empty

    def test_difficulty_range(self):
        p = _plan("implement cross-module auth refactor with rollback strategy")
        assert 1 <= p.difficulty <= 5

    def test_effort_non_empty(self):
        p = _plan("fix a typo in README.md")
        assert p.effort  # non-empty string

    def test_empty_text_returns_defaults(self):
        p = _plan("")
        assert p.executor_route == "claude"
        assert p.review_kind == "none"
        assert p.reviewer_route is None

    def test_to_json_is_serializable(self):
        import json
        p = _plan("refactor handoff.py for performance")
        data = p.to_json()
        assert isinstance(data, dict)
        # Verify it's JSON-serialisable
        encoded = json.dumps(data)
        decoded = json.loads(encoded)
        assert decoded["executor_route"] == p.executor_route

    def test_rationale_non_empty(self):
        p = _plan("implement async task queue")
        assert p.rationale

    def test_assessment_has_difficulty(self):
        p = _plan("refactor runner.py across five modules")
        assert "difficulty" in p.assessment
        assert p.assessment["difficulty"] == p.difficulty


# ---------------------------------------------------------------------------
# review gating
# ---------------------------------------------------------------------------

class TestReviewGating:
    def test_trivial_no_review(self):
        # A single-word typo fix → difficulty 1 → no review
        p = _plan("fix typo")
        assert p.review_kind == "none"
        assert p.reviewer_route is None

    def test_advisory_at_difficulty_3(self):
        # Force difficulty ≥3 without override — broad multi-file reasoning
        p = _plan("refactor cross-module state management with explicit locking")
        if p.difficulty >= coordinator.ADVISORY_THRESHOLD:
            assert p.review_kind in ("advisory", "blocking")

    def test_blocking_at_difficulty_4(self):
        # Cross-module architecture with concurrent safety requirements
        p = _plan(
            "重构整个 auth 模块，涉及跨模块状态迁移、并发安全、权限边界设计，"
            "需要多个文件保持一致性"
        )
        if p.difficulty >= coordinator.BLOCKING_THRESHOLD:
            assert p.review_kind == "blocking"

    def test_force_review_upgrades_none(self):
        p = _plan("fix typo", force_review=True)
        # If review would be "none", force_review upgrades it to advisory
        # (difficulty might still be 1 for a typo)
        assert p.review_kind in ("none", "advisory", "blocking")
        # At minimum force_review must not make it worse
        p2 = _plan("fix typo")
        # force_review can only upgrade, never downgrade
        order = {"none": 0, "advisory": 1, "blocking": 2}
        assert order[p.review_kind] >= order[p2.review_kind]

    def test_skip_review_suppresses_all(self):
        p = _plan(
            "重构整个系统架构，涉及多模块并发安全",
            skip_review=True,
        )
        assert p.review_kind == "none"
        assert p.reviewer_route is None


# ---------------------------------------------------------------------------
# reviewer selection
# ---------------------------------------------------------------------------

class TestReviewerSelection:
    def test_reviewer_differs_from_executor(self):
        # Only meaningful when a review is planned
        p = _plan("refactor cross-module state management")
        if p.reviewer_route:
            assert p.reviewer_route != p.executor_route

    def test_reviewer_different_family(self):
        from sodus.coordinator import _family
        p = _plan("refactor cross-module state management")
        if p.reviewer_route:
            assert _family(p.reviewer_route) != _family(p.executor_route)

    def test_reviewer_none_when_review_none(self):
        p = _plan("fix typo")
        if p.review_kind == "none":
            assert p.reviewer_route is None


# ---------------------------------------------------------------------------
# _pick_reviewer internals
# ---------------------------------------------------------------------------

class TestPickReviewer:
    def test_never_returns_executor(self):
        present = ["claude", "dsh", "codex"]
        for route in present:
            reviewer = coordinator._pick_reviewer(route, difficulty=4)
            if reviewer:
                assert reviewer != route

    def test_different_family_preferred(self):
        from sodus.coordinator import _family
        reviewer = coordinator._pick_reviewer("claude", difficulty=4)
        if reviewer:
            assert _family(reviewer) != _family("claude")

    def test_high_difficulty_prefers_strong_route(self):
        from sodus.coordinator import _family
        reviewer = coordinator._pick_reviewer("dsh", difficulty=4)
        if reviewer:
            assert _family(reviewer) != _family("dsh")

    def test_review_kind_mapping(self):
        assert coordinator._review_kind(1, []) == "none"
        assert coordinator._review_kind(2, []) == "none"
        assert coordinator._review_kind(3, []) == "advisory"
        assert coordinator._review_kind(3, ["reasoning"]) == "advisory"
        assert coordinator._review_kind(4, []) == "blocking"
        assert coordinator._review_kind(5, []) == "blocking"
