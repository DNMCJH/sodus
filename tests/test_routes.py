"""Routes: one agent, an ordered fallback chain, validated before it runs."""

from __future__ import annotations

import json

import pytest

from nodus import config, routes


def test_builtin_routes_pair_an_agent_with_an_ordered_chain():
    table = routes.load()
    assert table["claude"].agent == "claude"
    assert table["claude"].providers == ("relay", "minimax")
    assert table["dsh"].providers == ("deepseek",)


def test_a_route_name_wins_over_a_bare_agent_name():
    assert routes.resolve("minimax").agent == "claude"


def test_a_bare_agent_name_still_resolves():
    """`--agent claude` predates routes and has to keep working."""
    route = routes.resolve("nonexistent-route")
    assert route is None
    assert routes.resolve_for_run("claude").agent == "claude"


def test_unknown_route_lists_what_exists():
    with pytest.raises(ValueError, match="known:"):
        routes.resolve_for_run("nope")


def test_problems_are_reported_before_anything_spawns(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    route = routes.Route(name="bad", agent="claude", providers=("nope",))
    assert "unknown provider: nope" in routes.problems(route)


def test_a_chain_cannot_mix_protocols(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    # claude speaks the Anthropic protocol; the relay's OpenAI face does not
    route = routes.Route(name="mixed", agent="claude", providers=("relay", "relay-openai"))
    problems = routes.problems(route)
    assert any("cannot speak" in item for item in problems)


def test_a_route_without_providers_is_not_runnable():
    assert "no providers" in routes.problems(routes.Route(name="x", agent="claude", providers=()))


def test_a_custom_route_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    routes.upsert(
        routes.Route(name="cheap", agent="claude", providers=("minimax",), model="MiniMax-M3")
    )
    reloaded = routes.load()["cheap"]
    assert reloaded.providers == ("minimax",)
    assert reloaded.model == "MiniMax-M3"


def test_only_the_difference_from_the_builtins_is_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    routes.upsert(routes.Route(name="flat", agent="claude", providers=("relay",)))
    data = json.loads((tmp_path / "routes.json").read_text(encoding="utf-8"))
    assert list(data) == ["flat"]


def test_readiness_means_the_route_can_actually_run(tmp_path, monkeypatch):
    """This is the question the review/fix pickers ask, and it used to be
    `agents.ready` — which stopped meaning anything once one agent could be
    reached through several endpoints."""
    monkeypatch.setattr(config, "HOME", tmp_path)
    assert routes.ready("claude", {}) is False  # no relay key on disk
    assert routes.ready("claude", {"RELAY_API_KEY": "sk"}) is True
    assert routes.ready("nope", {"RELAY_API_KEY": "sk"}) is False


def test_a_chain_is_ready_on_its_fallback_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    routes.upsert(routes.Route(name="backup", agent="claude", providers=("relay", "minimax")))
    assert routes.ready("backup", {"MINIMAX_API_KEY": "sk"}) is True


def test_the_reviewer_picker_still_works_after_the_split(tmp_path, monkeypatch):
    from nodus import review

    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(
        config, "load_secrets", lambda ws=None: {"RELAY_API_KEY": "sk", "MINIMAX_API_KEY": "sk"}
    )
    assert review.pick_reviewer("claude") in ("codex", "dsh")
    with pytest.raises(ValueError, match="must differ"):
        review.pick_reviewer("claude", "claude")
