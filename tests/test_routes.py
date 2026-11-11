"""Routes: one agent, an ordered fallback chain, validated before it runs."""

from __future__ import annotations

import json

import pytest

from sodus import agents, config, routes


def test_builtin_routes_pair_an_agent_with_an_ordered_chain():
    table = routes.load()
    assert table["claude"].agent == "claude"
    assert table["claude"].providers == ("relay", "minimax")
    assert table["dsh"].providers == ("deepseek",)


def test_opencode_routes_name_the_model_in_the_clis_own_namespace():
    table = routes.load()
    assert table["opencode-glm"].agent == "opencode-glm"
    assert table["opencode-glm"].providers == ("relay-openai",)
    assert table["opencode-glm"].model == "codes9527/glm-5.3-flash"
    assert table["opencode-gemini"].agent == "opencode-gemini"
    assert table["opencode-gemini"].model == "codes9527/gemini-3.8-flash"


def test_an_opencode_route_validates_even_though_it_dials_its_own_config(tmp_path, monkeypatch):
    """The provider is still real: same relay, same key, what the meter reads.

    What it does *not* do is reach the CLI — opencode dials its own
    `opencode.json` — so the pairing must validate on protocol and readiness
    alone, and nothing may claim to control the URL.
    """
    monkeypatch.setattr(config, "HOME", tmp_path)
    assert routes.problems(routes.load()["opencode-glm"]) == []


def test_opencode_cannot_be_pointed_at_the_anthropic_face(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    route = routes.Route(name="wrong", agent="opencode-glm", providers=("relay",))
    assert any("cannot speak" in item for item in routes.problems(route))


def test_opencode_readiness_is_the_relay_key(tmp_path, monkeypatch, installed):
    monkeypatch.setattr(config, "HOME", tmp_path)
    assert routes.ready("opencode-glm", {}) is False
    assert routes.ready("opencode-glm", {"RELAY_API_KEY": "sk"}) is True


def test_an_opencode_route_still_builds_a_runnable_argv():
    from sodus import providers

    route = routes.load()["opencode-glm"]
    spec = agents.SPECS[route.agent]
    argv = agents.argv(
        spec, providers.load()["relay-openai"], model=route.model, prompt="回答两个字：收到"
    )
    assert argv[argv.index("-m") + 1] == "codes9527/glm-5.3-flash"
    assert argv[argv.index("--format") + 1] == "json"
    assert argv[-1] == "回答两个字：收到"


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


@pytest.fixture()
def installed(monkeypatch):
    """Pretend every CLI is installed.

    These tests are about route readiness, not about what happens to be on the
    build machine — CI has no `claude` or `codex` binary, and the first version
    of these tests failed there for exactly that reason.
    """
    monkeypatch.setattr(agents, "unavailable", lambda spec: [])


def test_readiness_means_the_route_can_actually_run(tmp_path, monkeypatch, installed):
    """This is the question the review/fix pickers ask, and it used to be
    `agents.ready` — which stopped meaning anything once one agent could be
    reached through several endpoints."""
    monkeypatch.setattr(config, "HOME", tmp_path)
    assert routes.ready("claude", {}) is False  # no relay key on disk
    assert routes.ready("claude", {"RELAY_API_KEY": "sk"}) is True
    assert routes.ready("nope", {"RELAY_API_KEY": "sk"}) is False


def test_a_chain_is_ready_on_its_fallback_alone(tmp_path, monkeypatch, installed):
    monkeypatch.setattr(config, "HOME", tmp_path)
    routes.upsert(routes.Route(name="backup", agent="claude", providers=("relay", "minimax")))
    assert routes.ready("backup", {"MINIMAX_API_KEY": "sk"}) is True


def test_the_reviewer_picker_still_works_after_the_split(tmp_path, monkeypatch, installed):
    from sodus import review

    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(
        config, "load_secrets", lambda ws=None: {"RELAY_API_KEY": "sk", "MINIMAX_API_KEY": "sk"}
    )
    assert review.pick_reviewer("claude") in ("codex", "dsh")
    with pytest.raises(ValueError, match="must differ"):
        review.pick_reviewer("claude", "claude")


def test_a_legacy_run_name_resolves_to_the_cli_that_wrote_it(tmp_path, monkeypatch):
    """Rows written before 0.4.0 stored a route-shaped name in `agent`.

    Comparing that against CLI names would let Claude review its own work on
    every old MiniMax run, which is the one thing a reviewer must not do.
    """
    from sodus import review

    monkeypatch.setattr(config, "HOME", tmp_path)
    assert review.implementer_of({"agent": "minimax", "route": None}) == "claude"
    assert review.implementer_of({"agent": "claude", "route": "minimax"}) == "claude"
    assert review.implementer_of({"agent": "codex", "route": "codex"}) == "codex"
