"""Agents: argument dialect and process environment, nothing about endpoints."""

from __future__ import annotations

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


def test_claude_can_continue_a_session():
    """It could not, until the CLI grew `--resume`; the flag is the capability."""
    argv = agents.argv(agents.SPECS["claude"], _provider(), session_id="session-abc123")
    assert argv[-2:] == ["--resume", "session-abc123"]


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
    # opencode appears twice because the model is part of what a task asks for.
    assert set(agents.registry()) == {
        "claude",
        "codex",
        "dsh",
        "opencode-glm",
        "opencode-gemini",
    }


def test_each_cli_declares_the_protocols_it_speaks():
    assert agents.SPECS["claude"].accepts == (providers.ANTHROPIC,)
    assert agents.SPECS["codex"].accepts == (providers.OPENAI,)
    assert agents.SPECS["dsh"].accepts == (providers.DEEPSEEK,)
    assert agents.SPECS["opencode-glm"].accepts == (providers.OPENAI,)
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


# ---- opencode ----------------------------------------------------------


def _relay_openai(**over) -> providers.Provider:
    base = dict(
        name="relay-openai",
        protocol=providers.OPENAI,
        base_url="https://relay.test/v1",
        key_secret="RELAY_API_KEY",
        model="gpt-6.1-sol",
    )
    base.update(over)
    return providers.Provider(**base)


def test_opencode_specs_exist_one_per_model():
    for name in ("opencode-glm", "opencode-gemini"):
        spec = agents.SPECS[name]
        assert spec.vendor == "opencode"
        assert spec.command[0] == "opencode"
        assert spec.command[1] == "run"
        assert spec.resume_mode == "flag"
        assert spec.fork_mode == "flag"


def test_opencode_runs_headless_json():
    argv = agents.argv(agents.SPECS["opencode-glm"], _relay_openai())
    assert argv[argv.index("--format") + 1] == "json"


def test_opencode_model_is_namespace_qualified():
    argv = agents.argv(
        agents.SPECS["opencode-glm"], _relay_openai(), model="codes9527/glm-5.3-flash"
    )
    assert argv[argv.index("-m") + 1] == "codes9527/glm-5.3-flash"
    assert argv.index("-m") > argv.index("run")


def test_opencode_will_not_pass_a_bare_model_name():
    """relay-openai's model is `gpt-6.1-sol`, which is not a `provider/model`
    pair; passing it would be an invalid flag rather than a useful default."""
    argv = agents.argv(agents.SPECS["opencode-glm"], _relay_openai())
    assert "-m" not in argv


def test_opencode_takes_the_prompt_positionally_and_last():
    argv = agents.argv(
        agents.SPECS["opencode-glm"], _relay_openai(), model="p/m", prompt="回答两个字：收到"
    )
    assert argv[-1] == "回答两个字：收到"


def test_stdin_agents_are_given_no_positional_prompt():
    """The other three read the brief from stdin; an extra argument would land
    in the middle of their own flag parsing."""
    cases = (
        ("claude", providers.ANTHROPIC),
        ("codex", providers.OPENAI),
        ("dsh", providers.DEEPSEEK),
    )
    for name, protocol in cases:
        argv = agents.argv(agents.SPECS[name], _provider(protocol=protocol), prompt="do it")
        assert "do it" not in argv


def test_opencode_resume_puts_the_session_before_the_prompt():
    argv = agents.argv(
        agents.SPECS["opencode-glm"],
        _relay_openai(),
        session_id="ses_abc",
        model="p/m",
        prompt="next",
    )
    assert argv[argv.index("-s") + 1] == "ses_abc"
    assert argv[-1] == "next"
    assert "--fork" not in argv


def test_opencode_fork_adds_the_flag_after_the_session():
    argv = agents.argv(
        agents.SPECS["opencode-glm"],
        _relay_openai(),
        session_id="ses_abc",
        session_mode="fork",
        model="p/m",
        prompt="next",
    )
    assert argv[argv.index("-s") + 1] == "ses_abc"
    assert argv.index("--fork") > argv.index("-s")
    assert argv[-1] == "next"


def test_opencode_borrows_the_relay_for_metering_not_for_routing():
    """The relay is borrowed for metering, not for routing.

    opencode reads `~/.config/opencode/opencode.json`; the env below is what
    `meter.read` uses, and the CLI ignores it. Saying so here keeps the next
    person from "fixing" the route by editing a base URL that never applied.
    """
    spec = agents.SPECS["opencode-glm"]
    assert spec.secrets == ()  # no extra credential is injected
    env = agents.build_env(spec, _relay_openai(), {"RELAY_API_KEY": "sk"})
    assert env["OPENAI_API_KEY"] == "sk"
    assert env["NODUS_RUN"] == "1"
