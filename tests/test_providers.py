"""Providers: the endpoint as data, overridable without touching Python."""

from __future__ import annotations

import json

import pytest

from sodus import config, providers


def test_builtins_cover_the_accounts_endpoints():
    table = providers.load()
    assert {"relay", "relay-openai", "minimax", "deepseek"} <= set(table)
    assert table["relay"].protocol == providers.ANTHROPIC
    assert table["relay-openai"].protocol == providers.OPENAI
    # the two relay faces are one credential, and that should be visible
    assert table["relay"].key_secret == table["relay-openai"].key_secret


def test_anthropic_env_carries_url_token_model_and_tiers():
    p = providers.Provider(
        name="x",
        protocol=providers.ANTHROPIC,
        base_url="https://x.test",
        key_secret="K",
        model="m-main",
        tiers={"opus": "m-big", "haiku": "m-small"},
    )
    env = providers.env(p, {"K": "sk"})
    assert env == {
        "ANTHROPIC_BASE_URL": "https://x.test",
        "ANTHROPIC_AUTH_TOKEN": "sk",
        "ANTHROPIC_MODEL": "m-main",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "m-big",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "m-small",
    }


def test_openai_and_deepseek_use_their_own_variable_names():
    o = providers.Provider(name="o", protocol=providers.OPENAI, base_url="https://o", key_secret="K")
    d = providers.Provider(name="d", protocol=providers.DEEPSEEK, key_secret="K")
    assert providers.env(o, {"K": "sk"}) == {"OPENAI_BASE_URL": "https://o", "OPENAI_API_KEY": "sk"}
    assert providers.env(d, {"K": "sk"}) == {"DEEPSEEK_API_KEY": "sk"}


def test_a_missing_key_is_named_not_guessed():
    p = providers.Provider(name="x", protocol=providers.ANTHROPIC, key_secret="NEEDED")
    assert providers.missing(p, {}) == ["NEEDED"]
    assert providers.missing(p, {"NEEDED": "sk"}) == []


def test_cli_settings_never_carry_env():
    """Claude Code's settings.json env outranks the process env.

    Leaving one there would silently outvote whatever provider the run chose —
    the exact bug that cost 190s of retries when MiniMax was first wired up.
    """
    p = providers.Provider(name="x", protocol=providers.ANTHROPIC, model="m")
    assert providers.cli_settings(p) == {"model": "m"}
    assert "env" not in providers.cli_settings(p)


def test_pricing_turns_tokens_into_money():
    p = providers.Pricing(input=1.0, output=4.0, cache_read=0.1, currency="CNY")
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read": 1_000_000}
    assert p.cost(usage) == pytest.approx(1.0 + 4.0 + 0.1)


def test_a_custom_provider_survives_a_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    providers.save(
        {
            **providers.load(),
            "mine": providers.Provider(
                name="mine",
                protocol=providers.ANTHROPIC,
                base_url="https://mine.test",
                key_secret="MINE_KEY",
                model="m",
                tiers={"opus": "m"},
            ),
        }
    )
    reloaded = providers.load()["mine"]
    assert reloaded.base_url == "https://mine.test"
    assert reloaded.tiers == {"opus": "m"}


def test_only_the_difference_from_the_builtins_is_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    table = providers.load()
    table["relay"] = providers.Provider(
        name="relay", protocol=providers.ANTHROPIC, base_url="https://other.test"
    )
    providers.save(table)
    data = json.loads((tmp_path / "providers.json").read_text(encoding="utf-8"))
    assert set(data) == {"relay"}
    # a full dump would freeze today's defaults into the user's file
    assert "minimax" not in data


def test_removing_a_builtin_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    table = providers.load()
    table.pop("minimax")
    providers.save(table)
    assert "minimax" not in providers.load()
    assert {"_removed": ["minimax"]} == json.loads(
        (tmp_path / "providers.json").read_text(encoding="utf-8")
    )


def test_bootstrap_recovers_the_endpoint_from_the_old_settings_file(tmp_path, monkeypatch):
    """Upgrading must not leave every agent without a token.

    The pre-0.4 layout kept the relay URL and key inside a per-agent
    settings.json `env` block; nothing else recorded them.
    """
    home = tmp_path / "home"
    legacy = home / "agents" / "claude"
    legacy.mkdir(parents=True)
    (legacy / "settings.json").write_text(
        json.dumps(
            {
                "model": "sonnet",
                "env": {
                    "ANTHROPIC_BASE_URL": "https://relay.test",
                    "ANTHROPIC_AUTH_TOKEN": "sk-legacy",
                    "ANTHROPIC_MODEL": "m-main",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": "m-big",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "LEGACY_AGENTS_DIR", home / "agents")

    providers.bootstrap()

    relay = providers.load()["relay"]
    assert relay.base_url == "https://relay.test"
    assert relay.model == "m-main"
    assert relay.tiers["opus"] == "m-big"
    secrets = json.loads((home / "secrets.json").read_text(encoding="utf-8"))
    assert secrets["RELAY_API_KEY"] == "sk-legacy"


def test_bootstrap_never_overwrites_a_hand_edited_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "providers.json").write_text(
        json.dumps({"relay": {"protocol": "anthropic", "base_url": "https://mine.test"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "HOME", home)
    providers.bootstrap()
    assert providers.load()["relay"].base_url == "https://mine.test"
