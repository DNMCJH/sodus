"""`/llm/chat`: the one door a pet has to a model.

What is worth pinning here is the *contract*, not the network: the caller names
an agent and never an endpoint, the two limits hold, a failed provider falls
through to the route's next one, and a successful call leaves a priced row that
`nodus stats` counts. The HTTP shapes are checked by `test_openapi.py`.
"""

from __future__ import annotations

import json

import pytest

from nodus import config, llm, providers, routes


@pytest.fixture(autouse=True)
def _clean_limits():
    llm._CALLS.clear()
    yield
    llm._CALLS.clear()


@pytest.fixture
def secrets(monkeypatch):
    """Every built-in provider has a key, so no attempt is skipped for lack of one."""
    keys = {name: "test-key" for name in ("RELAY_API_KEY", "DEEPSEEK_API_KEY", "MINIMAX_API_KEY")}
    monkeypatch.setattr(llm.config, "load_secrets", lambda *a, **k: keys)
    return keys


def _anthropic_body(text="哼。", model="claude-opus-5-5[1M]", output=12):
    return {
        "model": model,
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 30, "output_tokens": output},
        "stop_reason": "end_turn",
    }


def test_the_limits_are_ten_calls_a_minute_and_four_hundred_tokens():
    assert llm.MAX_OUTPUT_TOKENS == 400
    assert llm.CALLS_PER_MINUTE == 10


def test_the_eleventh_call_in_a_minute_is_refused(monkeypatch):
    now = 1_000.0
    for _ in range(10):
        assert llm.allow("claude", now) is True
    assert llm.allow("claude", now) is False
    # a different agent has its own budget: the pet host is one process, four pets
    assert llm.allow("codex", now) is True
    # and the window slides
    assert llm.allow("claude", now + llm.WINDOW_SECONDS + 0.1) is True


def test_model_for_drops_opencode_namespace():
    route = routes.resolve("opencode-gemini")
    provider = providers.load()["relay-openai"]
    assert llm.model_for(route, provider) == "gemini-3.8-flash"


def test_model_for_falls_back_to_the_protocol_default():
    route = routes.resolve("dsh")
    provider = providers.load()["deepseek"]
    assert llm.model_for(route, provider) == "deepseek-chat"


def test_model_for_reads_the_config_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    (tmp_path / "config.json").write_text(
        json.dumps({"llm": {"models": {"dsh": "deepseek-reasoner"}}}), encoding="utf-8"
    )
    route = routes.resolve("dsh")
    assert llm.model_for(route, providers.load()["deepseek"]) == "deepseek-reasoner"


def test_the_url_follows_the_protocol():
    table = providers.load()
    assert llm._url(table["relay"]) == "https://9527.codes/v1/messages"
    assert llm._url(table["relay-openai"]) == "https://9527.codes/v1/chat/completions"
    assert llm._url(table["deepseek"]) == "https://api.deepseek.com/chat/completions"


@pytest.mark.parametrize(
    ("payload", "kind", "status"),
    [
        ({"agent": "", "messages": [{"role": "user", "content": "x"}]}, "bad-request", 400),
        ({"agent": "claude", "messages": []}, "bad-request", 400),
        ({"agent": "claude", "messages": [{"role": "boss", "content": "x"}]}, "bad-request", 400),
        ({"agent": "claude", "messages": [{"role": "user", "content": " "}]}, "bad-request", 400),
        ({"agent": "nope", "messages": [{"role": "user", "content": "x"}]}, "unknown-agent", 404),
    ],
)
def test_bad_input_gets_a_structured_error(payload, kind, status):
    got_status, body = llm.chat(payload["agent"], payload["messages"])
    assert got_status == status
    assert body["error"]["kind"] == kind
    assert body["error"]["message"]


def test_a_call_returns_the_text_and_records_a_priced_row(monkeypatch, secrets):
    recorded = {}
    monkeypatch.setattr(llm, "_send", lambda *a, **k: (_anthropic_body(), None))
    monkeypatch.setattr(llm, "_record", lambda *a, **k: recorded.setdefault("run", "run-test"))

    status, body = llm.chat(
        "claude", [{"role": "user", "content": "说句话"}], note="桌宠"
    )

    assert status == 200
    assert body["text"] == "哼。"
    assert body["route"] == "claude"
    assert body["provider"] == "relay"
    assert body["model"] == "claude-opus-5-5[1M]"
    # claude-opus-5-5[1M] has a measured calibration: the cost is not a guess
    assert body["cost_basis"] == "calibrated"
    assert body["currency"] == "CNY"
    assert body["cost"] > 0
    assert body["run"] == "run-test"


def test_a_provider_without_its_key_is_skipped_not_attempted(monkeypatch):
    """No relay key means the relay is not called at all — minimax answers."""
    monkeypatch.setattr(
        llm.config, "load_secrets", lambda *a, **k: {"MINIMAX_API_KEY": "k"}
    )
    tried = []

    def fake_send(provider, secret, body):
        tried.append(provider.name)
        return (
            {
                "model": "MiniMax-M3.1-Flash",
                "content": [{"type": "text", "text": "我在。"}],
                "usage": {"input_tokens": 20, "output_tokens": 8},
            },
            None,
        )

    monkeypatch.setattr(llm, "_send", fake_send)
    monkeypatch.setattr(llm, "_record", lambda *a, **k: "run-skip")
    status, body = llm.chat("claude", [{"role": "user", "content": "在吗"}])
    assert status == 200
    assert tried == ["minimax"]
    assert body["provider"] == "minimax"


def test_a_failing_provider_falls_through_to_the_next_one(monkeypatch):
    keys = {"RELAY_API_KEY": "k", "MINIMAX_API_KEY": "k"}
    monkeypatch.setattr(llm.config, "load_secrets", lambda *a, **k: keys)
    tried = []

    def fake_send(provider, secret, body):
        tried.append(provider.name)
        if provider.name == "relay":
            return None, (502, {"error": {"kind": "provider-error", "message": "503"}})
        return (
            {
                "model": "MiniMax-M3.1-Flash",
                "content": [{"type": "text", "text": "我在。"}],
                "usage": {"input_tokens": 20, "output_tokens": 8},
            },
            None,
        )

    monkeypatch.setattr(llm, "_send", fake_send)
    monkeypatch.setattr(llm, "_record", lambda *a, **k: "run-fallback")

    status, body = llm.chat("claude", [{"role": "user", "content": "在吗"}])

    assert status == 200
    assert tried == ["relay", "minimax"]
    assert body["provider"] == "minimax"
    assert body["cost_basis"] == "list_price"


def test_an_empty_answer_is_an_error_not_an_empty_string(monkeypatch, secrets):
    monkeypatch.setattr(
        llm,
        "_send",
        lambda *a, **k: ({"model": "m", "content": [], "usage": {}}, None),
    )
    status, body = llm.chat("claude", [{"role": "user", "content": "在吗"}])
    assert status == 502
    assert body["error"]["kind"] == "empty"


def test_output_tokens_are_capped_at_four_hundred(monkeypatch, secrets):
    captured = {}

    def fake_send(provider, secret, body):
        captured.update(body)
        return _anthropic_body(), None

    monkeypatch.setattr(llm, "_send", fake_send)
    monkeypatch.setattr(llm, "_record", lambda *a, **k: "run-cap")
    llm.chat("claude", [{"role": "user", "content": "x"}], max_tokens=5000)
    assert captured["max_tokens"] == llm.MAX_OUTPUT_TOKENS
