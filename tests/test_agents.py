"""Agent registry: argv construction is the only place vendor flags live."""

from __future__ import annotations

import pytest

from nodus import agents


def test_dsh_resume_inserts_session_flag_before_stdin_marker():
    spec = agents.SPECS["dsh"]
    argv = agents.argv(spec, None, "session-abc123")
    assert "--session-id" in argv
    assert argv[argv.index("--session-id") + 1] == "session-abc123"
    assert argv[-1] == "-"


def test_dsh_without_resume_has_no_flag():
    argv = agents.argv(agents.SPECS["dsh"])
    assert "--session-id" not in argv


def test_claude_rejects_session_id():
    with pytest.raises(ValueError, match="cannot resume"):
        agents.argv(agents.SPECS["claude"], None, "session-abc123")


def test_claude_effort_flag():
    argv = agents.argv(agents.SPECS["claude"], "high")
    assert "--effort" in argv
    assert argv[argv.index("--effort") + 1] == "high"


def test_codex_effort_uses_config_override():
    argv = agents.argv(agents.SPECS["codex"], "low")
    assert "-c" in argv
    assert any("model_reasoning_effort" in part for part in argv)


def test_registry_contains_the_known_vendors():
    assert set(agents.registry()) == {"claude", "codex", "dsh", "minimax"}


def test_minimax_reuses_the_claude_stream_contract():
    spec = agents.SPECS["minimax"]
    assert spec.vendor == "claude"
    assert spec.command == agents.SPECS["claude"].command
    assert spec.config_home != agents.SPECS["claude"].config_home


def test_minimax_token_is_injected_not_stored():
    spec = agents.SPECS["minimax"]
    env = agents.build_env(spec, {"MINIMAX_API_KEY": "sk-test"})
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-test"
    assert env["CLAUDE_CONFIG_DIR"] == str(spec.config_home)
    # an inherited Anthropic key must not outrank the provider token
    assert "ANTHROPIC_API_KEY" not in env


def test_minimax_without_secret_is_not_ready_and_gets_no_token(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    spec = agents.SPECS["minimax"]
    assert agents.missing_requirements(spec, {}) == ["MINIMAX_API_KEY"]
    assert "ANTHROPIC_AUTH_TOKEN" not in agents.build_env(spec, {})


def test_minimax_pins_its_model_on_the_command_line():
    argv = agents.argv(agents.SPECS["minimax"])
    assert argv[argv.index("--model") + 1] == agents.MINIMAX_MODEL
    assert "--model" not in agents.argv(agents.SPECS["claude"])


def test_pricing_turns_tokens_into_money():
    p = agents.Pricing(input=1.0, output=4.0, cache_read=0.1, currency="CNY")
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read": 1_000_000}
    assert p.cost(usage) == pytest.approx(1.0 + 4.0 + 0.1)
    assert agents.SPECS["claude"].pricing is None


def test_unknown_agent_is_not_ready():
    assert agents.ready("nope") is False
