"""Profiles: a named set of agent configs, generated from provider + overlay."""

from __future__ import annotations

import json
import tomllib

import pytest

from nodus import agents, config, profiles, providers


def _relay() -> providers.Provider:
    return providers.Provider(
        name="relay",
        protocol=providers.ANTHROPIC,
        base_url="https://relay.test",
        key_secret="RELAY_API_KEY",
        model="claude-opus-5-5[1M]",
        tiers={"opus": "claude-opus-5-5[1M]"},
    )


def _relay_openai() -> providers.Provider:
    return providers.Provider(
        name="relay-openai",
        protocol=providers.OPENAI,
        base_url="https://relay.test/v1",
        key_secret="RELAY_API_KEY",
        model="gpt-6.1-sol",
    )


def test_claude_settings_are_generated_without_an_env_block(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    home = profiles.materialize(agents.SPECS["claude"], _relay(), {"RELAY_API_KEY": "sk"})
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert settings["model"] == "claude-opus-5-5[1M]"
    assert "env" not in settings
    assert "sk" not in (home / "settings.json").read_text(encoding="utf-8")


def test_an_overlay_is_merged_but_cannot_smuggle_env_back_in(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    profiles.write_overlay(
        "claude", {"modelSettings": {"x": {"effortLevel": "high"}}, "env": {"A": "1"}}
    )
    home = profiles.materialize(agents.SPECS["claude"], _relay(), {"RELAY_API_KEY": "sk"})
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert settings["modelSettings"] == {"x": {"effortLevel": "high"}}
    assert "env" not in settings


def test_codex_config_is_generated_from_the_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    profiles.write_overlay("codex", {"windows": {"sandbox": "elevated"}})
    home = profiles.materialize(agents.SPECS["codex"], _relay_openai(), {"RELAY_API_KEY": "sk"})
    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert data["model"] == "gpt-6.1-sol"
    block = data["model_providers"][data["model_provider"]]
    assert block["base_url"] == "https://relay.test/v1"
    assert block["experimental_bearer_token"] == "sk"
    assert data["windows"]["sandbox"] == "elevated"


def test_codex_toml_quotes_keys_that_are_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    profiles.write_overlay("codex", {"projects": {r"a:\work\thing": {"trust_level": "trusted"}}})
    home = profiles.materialize(agents.SPECS["codex"], _relay_openai(), {"RELAY_API_KEY": "sk"})
    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert data["projects"][r"a:\work\thing"]["trust_level"] == "trusted"


def test_each_provider_gets_its_own_home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    a = profiles.materialize(agents.SPECS["claude"], _relay(), {"RELAY_API_KEY": "sk"})
    b = profiles.materialize(
        agents.SPECS["claude"],
        providers.Provider(name="minimax", protocol=providers.ANTHROPIC, model="mm"),
        {},
    )
    assert a != b
    assert a.parent == b.parent


def test_profiles_are_created_switched_and_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    profiles.write(profiles.Profile(name="default"))
    profiles.create("gpu")
    assert set(profiles.names()) == {"default", "gpu"}
    profiles.use("gpu")
    assert config.active_profile() == "gpu"


def test_switching_to_an_unknown_profile_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    with pytest.raises(ValueError, match="unknown profile"):
        profiles.use("ghost")


def test_the_active_profile_cannot_be_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    profiles.write(profiles.Profile(name="default"))
    profiles.create("scratch")
    with pytest.raises(ValueError, match="active profile"):
        profiles.remove("default")


def test_a_new_profile_can_copy_another(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    profiles.write(profiles.Profile(name="default", model="m1"))
    profiles.write_overlay("claude", {"modelSettings": {"k": 1}})
    profiles.create("copy", from_="default")
    assert profiles.read("copy").model == "m1"
    assert profiles.overlay("claude", "copy") == {"modelSettings": {"k": 1}}


def test_migration_moves_the_old_layout_and_keeps_its_settings(tmp_path, monkeypatch):
    home = tmp_path / "home"
    legacy = home / "agents" / "claude"
    legacy.mkdir(parents=True)
    (legacy / "settings.json").write_text(
        json.dumps(
            {
                "model": "sonnet",
                "modelSettings": {"claude-opus-5-5": {"effortLevel": "medium"}},
                "env": {
                    "ANTHROPIC_BASE_URL": "https://relay.test",
                    "ANTHROPIC_AUTH_TOKEN": "sk-legacy",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "LEGACY_AGENTS_DIR", home / "agents")

    profiles.ensure_ready()

    moved = home / "profiles" / "default" / "claude" / "relay"
    assert (moved / "settings.json").exists()
    assert not (home / "agents").exists()
    # the provider-independent part is kept as a hand-editable overlay
    assert profiles.overlay("claude") == {"modelSettings": {"claude-opus-5-5": {"effortLevel": "medium"}}}
    assert providers.load()["relay"].base_url == "https://relay.test"
    # and a backup of the file we are about to regenerate is kept
    assert list((home / "backup").rglob("claude-settings.json"))


def test_migration_runs_only_once(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "LEGACY_AGENTS_DIR", home / "agents")
    profiles.ensure_ready()
    marker = (home / "providers.json").stat().st_mtime_ns
    profiles.ensure_ready()
    assert (home / "providers.json").stat().st_mtime_ns == marker
