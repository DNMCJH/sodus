"""Agents: argument dialect and process environment, nothing about endpoints."""

from __future__ import annotations

import pytest

from nodus import agents, providers


def _provider(**over) -> providers.Provider:
    base = dict(
        name="relay",
        protocol=providers.ANTHROPIC,
        base_url="https://relay.test",
        key_secret="RELAY_API_KEY",
        model="claude-opus-5-5[1M]",
        tiers={"opus": "claude-opus-5-5[1M]"},
    )
    base.update(over)
    return providers.Provider(**base)


def test_dsh_resume_inserts_session_flag_before_stdin_marker():
    spec = agents.SPECS["dsh"]
    argv = agents.argv(spec, _provider(protocol=providers.DEEPSEEK), session_id="session-abc123")
    assert "--session-id" in argv
    assert argv[argv.index("--session-id") + 1] == "session-abc123"
    assert argv[-1] == "-"


def test_dsh_without_resume_has_no_flag():
    argv = agents.argv(agents.SPECS["dsh"], _provider(protocol=providers.DEEPSEEK))
    assert "--session-id" not in argv


def test_claude_rejects_session_id():
    with pytest.raises(ValueError, match="cannot resume"):
        agents.argv(agents.SPECS["claude"], _provider(), session_id="session-abc123")


def test_claude_effort_flag():
    argv = agents.argv(agents.SPECS["claude"], _provider(), effort="high")
    assert "--effort" in argv
    assert argv[argv.index("--effort") + 1] == "high"


def test_codex_effort_uses_config_override():
    argv = agents.argv(agents.SPECS["codex"], _provider(protocol=providers.OPENAI), effort="low")
    assert "-c" in argv
    assert any("model_reasoning_effort" in part for part in argv)


def test_registry_holds_clis_not_endpoints():
    # `minimax` used to be an agent here. It was always "the Claude CLI pointed
    # at another URL", so it is a provider now and this list got shorter.
    assert set(agents.registry()) == {"claude", "codex", "dsh"}


def test_each_cli_declares_the_protocols_it_speaks():
    assert agents.SPECS["claude"].accepts == (providers.ANTHROPIC,)
    assert agents.SPECS["codex"].accepts == (providers.OPENAI,)
    assert agents.SPECS["dsh"].accepts == (providers.DEEPSEEK,)
    assert not agents.SPECS["claude"].accepts_protocol(providers.OPENAI)


def test_provider_token_is_injected_not_stored():
    env = agents.build_env(agents.SPECS["claude"], _provider(), {"RELAY_API_KEY": "sk-test"})
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-test"
    assert env["ANTHROPIC_BASE_URL"] == "https://relay.test"
    # an inherited Anthropic key must not outrank the provider token
    assert "ANTHROPIC_API_KEY" not in env


def test_config_home_is_per_agent_and_provider(tmp_path):
    home = tmp_path / "claude" / "relay"
    env = agents.build_env(agents.SPECS["claude"], _provider(), {}, home=home)
    assert env["CLAUDE_CONFIG_DIR"] == str(home)
    codex = agents.build_env(
        agents.SPECS["codex"], _provider(protocol=providers.OPENAI), {}, home=home
    )
    assert codex["CODEX_HOME"] == str(home)


def test_only_the_declared_secrets_reach_the_worker():
    secrets = {"RELAY_API_KEY": "sk", "UNRELATED_KEY": "do-not-leak"}
    env = agents.build_env(agents.SPECS["claude"], _provider(), secrets)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk"
    assert "UNRELATED_KEY" not in env


def test_missing_secret_is_reported_by_name():
    gap = agents.missing(agents.SPECS["claude"], _provider(), {})
    assert "RELAY_API_KEY" in gap


def test_protocol_mismatch_is_reported():
    gap = agents.missing(
        agents.SPECS["claude"], _provider(protocol=providers.OPENAI), {"RELAY_API_KEY": "sk"}
    )
    assert any("cannot speak" in item for item in gap)


def test_provider_model_is_pinned_on_the_command_line():
    argv = agents.argv(agents.SPECS["claude"], _provider())
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5[1M]"


def test_a_run_can_pin_its_own_model():
    argv = agents.argv(agents.SPECS["claude"], _provider(), model="claude-sonnet-5-5[1M]")
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5[1M]"


def test_a_provider_without_a_model_adds_no_flag():
    argv = agents.argv(agents.SPECS["claude"], _provider(model=None))
    assert "--model" not in argv


def test_codex_takes_its_model_as_a_config_override():
    argv = agents.argv(agents.SPECS["codex"], _provider(protocol=providers.OPENAI))
    assert any('model="claude-opus-5-5[1M]"' in part for part in argv)


def test_dsh_records_the_model_but_cannot_apply_it():
    argv = agents.argv(agents.SPECS["dsh"], _provider(protocol=providers.DEEPSEEK), model="x")
    assert "--model" not in argv
