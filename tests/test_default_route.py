"""A bare `sodus run` uses the configured default route."""

from __future__ import annotations

import json

from sodus import config


def test_default_route_falls_back_to_claude(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    assert config.default_route() == "claude"


def test_default_route_reads_routing_default(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    (tmp_path / "config.json").write_text(
        json.dumps({"routing": {"default": "sol"}}), encoding="utf-8"
    )
    assert config.default_route() == "sol"


def test_default_route_ignores_junk_values(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    (tmp_path / "config.json").write_text(
        json.dumps({"routing": {"default": 123}}), encoding="utf-8"
    )
    assert config.default_route() == "claude"
