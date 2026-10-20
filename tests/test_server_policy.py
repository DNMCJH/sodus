"""HTTP boundary rules: constant-time token check, body cap, loopback default."""

from __future__ import annotations

import pytest

from nodus import config, server


def test_authorized_accepts_only_the_real_token(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    token = config.token()
    assert server.authorized(f"Bearer {token}") is True
    assert server.authorized("Bearer nope") is False
    assert server.authorized("") is False
    assert server.authorized(f"Bearer {token}extra") is False


def test_body_cap():
    assert server.body_allowed(0) is True
    assert server.body_allowed(server.MAX_BODY) is True
    assert server.body_allowed(server.MAX_BODY + 1) is False
    assert server.body_allowed(-1) is False


def test_non_loopback_bind_is_refused(monkeypatch):
    monkeypatch.delenv("NODUS_ALLOW_REMOTE", raising=False)
    with pytest.raises(SystemExit):
        server.serve(port=1, host="0.0.0.0")
