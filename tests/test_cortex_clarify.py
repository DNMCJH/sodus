# -*- coding: utf-8 -*-
"""Tests for cortex (meta-LLM client) and clarify (L1 intake).

Covers the failure modes the dsh review flagged as CRITICAL:
- cortex must never raise, whatever the endpoint returns
- cortex must not send keys to api.anthropic.com when base_url is missing
- clarify must reject invalid JSON / bad schema with ok=False, not crash
"""

import json
from pathlib import Path

import pytest

from sodus import clarify, cortex


# ---------------------------------------------------------------------------
# cortex._resolve — base_url guard
# ---------------------------------------------------------------------------

class TestResolve:
    def test_missing_base_url_rejected(self, tmp_path, monkeypatch):
        """A provider without base_url must be skipped, never fall through to
        api.anthropic.com (that would leak a relay key to Anthropic)."""
        monkeypatch.setattr(cortex.config, "HOME", tmp_path)
        # Provide a route whose provider has no base_url
        prov_table = {
            "relay-supplement": cortex.providers.Provider(
                name="relay-supplement",
                protocol="anthropic",
                base_url=None,          # the hazard
                key_secret="RELAY_KEY",
                model="claude-sonnet-5-5",
            ),
        }
        monkeypatch.setattr(cortex.providers, "load", lambda: prov_table)
        monkeypatch.setattr(cortex.config, "load_secrets", lambda: {"RELAY_KEY": "sk-test"})
        route = type("R", (), {
            "providers": ("relay-supplement",),
            "model": "claude-sonnet-5-5",
        })
        monkeypatch.setattr(cortex.routes, "resolve", lambda n: route if n == "coordinator" else None)
        assert cortex._resolve("coordinator") is None


# ---------------------------------------------------------------------------
# cortex.ask — never raises
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestAskNeverRaises:
    def _patch_resolve(self, monkeypatch):
        prov = cortex.providers.Provider(
            name="relay-supplement",
            protocol="anthropic",
            base_url="https://relay.example",
            key_secret="RELAY_KEY",
            model="claude-sonnet-5-5",
        )
        monkeypatch.setattr(cortex, "_resolve", lambda r: (prov, "claude-sonnet-5-5", "sk-test"))

    def test_html_response(self, monkeypatch):
        self._patch_resolve(monkeypatch)
        monkeypatch.setattr(
            cortex.urllib.request, "urlopen",
            lambda req, timeout: _FakeResp(b"<html>502 Bad Gateway</html>"),
        )
        result = cortex.ask("sys", [{"role": "user", "content": "hi"}])
        assert result["ok"] is False
        assert "JSON" in result["unavailable"] or "decode" in result["unavailable"].lower()

    def test_empty_body(self, monkeypatch):
        self._patch_resolve(monkeypatch)
        monkeypatch.setattr(
            cortex.urllib.request, "urlopen",
            lambda req, timeout: _FakeResp(b""),
        )
        result = cortex.ask("sys", [{"role": "user", "content": "hi"}])
        assert result["ok"] is False

    def test_json_array_body(self, monkeypatch):
        self._patch_resolve(monkeypatch)
        monkeypatch.setattr(
            cortex.urllib.request, "urlopen",
            lambda req, timeout: _FakeResp(b'[{"a":1}]'),
        )
        result = cortex.ask("sys", [{"role": "user", "content": "hi"}])
        # Array is not a dict: must degrade to ok=False, not crash
        assert result["ok"] is False

    def test_valid_response(self, monkeypatch):
        self._patch_resolve(monkeypatch)
        body = json.dumps({
            "content": [{"type": "text", "text": '{"questions": []}'}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }).encode()
        monkeypatch.setattr(
            cortex.urllib.request, "urlopen",
            lambda req, timeout: _FakeResp(body),
        )
        result = cortex.ask("sys", [{"role": "user", "content": "hi"}])
        assert result["ok"] is True
        assert result["text"] == '{"questions": []}'


# ---------------------------------------------------------------------------
# clarify._parse / _validate
# ---------------------------------------------------------------------------

class TestParse:
    def test_plain_json(self):
        parsed = clarify._parse('{"questions": [{"q": "a"}]}')
        assert parsed is not None

    def test_fenced_json(self):
        parsed = clarify._parse('```json\n{"questions": []}\n```')
        assert parsed is not None

    def test_prose_wrapped(self):
        parsed = clarify._parse('Here you go: {"q": 1} hope it helps')
        assert parsed is not None


class TestValidate:
    def _ok(self, parsed):
        return clarify._validate(parsed) is None

    def test_good_schema(self):
        parsed = {
            "questions": [{"q": "路径?", "why": "x", "options": ["a", "b"]}],
            "draft_spec": "spec",
            "acceptance_draft": ["file exists"],
            "scope_risk": "high",
            "estimated_effort": "4h",
        }
        assert self._ok(parsed)

    def test_questions_as_string(self):
        err = clarify._validate({"questions": "not a list"})
        assert err is not None

    def test_question_item_not_dict(self):
        err = clarify._validate({"questions": ["plain string"]})
        assert err is not None

    def test_question_q_not_string(self):
        err = clarify._validate({"questions": [{"q": 42}]})
        assert err is not None

    def test_acceptance_as_string_coerced(self):
        # string acceptance_draft is rejected (not char-split)
        err = clarify._validate({"questions": [], "acceptance_draft": "abc"})
        assert err is not None

    def test_scope_risk_coerced(self):
        parsed = {"questions": [], "scope_risk": "high risk"}
        clarify._validate(parsed)
        assert parsed["scope_risk"] == "medium"


# ---------------------------------------------------------------------------
# clarify.run — failure paths return ok=False, never raise
# ---------------------------------------------------------------------------

class TestRunFailOpen:
    def test_cortex_failure(self, monkeypatch):
        monkeypatch.setattr(
            clarify.cortex, "ask",
            lambda **kw: {"ok": False, "unavailable": "network: down"},
        )
        result = clarify.run("some task", {"difficulty": 4, "kind": "analysis"})
        assert result.ok is False
        assert result.unavailable

    def test_unparseable_json(self, monkeypatch):
        monkeypatch.setattr(
            clarify.cortex, "ask",
            lambda **kw: {"ok": True, "text": "not json at all", "model": "m",
                          "latency_ms": 5, "stop_reason": "end_turn"},
        )
        result = clarify.run("some task", {"difficulty": 4})
        assert result.ok is False
        assert "parse" in result.unavailable

    def test_bad_schema(self, monkeypatch):
        monkeypatch.setattr(
            clarify.cortex, "ask",
            lambda **kw: {
                "ok": True,
                "text": json.dumps({"questions": "oops"}),
                "model": "m", "latency_ms": 5, "stop_reason": "end_turn",
            },
        )
        result = clarify.run("some task", {"difficulty": 4})
        assert result.ok is False
        assert "schema" in result.unavailable

    def test_good_payload(self, monkeypatch):
        payload = {
            "questions": [{"q": "路径?", "why": "需要确定", "options": ["B:/infra"]}],
            "draft_spec": "扫描 [ASSUMED]",
            "acceptance_draft": ["报告存在"],
            "scope_risk": "high",
            "estimated_effort": "4h",
        }
        monkeypatch.setattr(
            clarify.cortex, "ask",
            lambda **kw: {
                "ok": True,
                "text": json.dumps(payload),
                "model": "claude-sonnet-5-5", "latency_ms": 800,
                "stop_reason": "end_turn",
            },
        )
        result = clarify.run("审阅B盘infra资料", {"difficulty": 4})
        assert result.ok is True
        assert len(result.questions) == 1
        assert result.scope_risk == "high"


# ---------------------------------------------------------------------------
# ClarifyResult round-trip
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_from_dict(self):
        data = {
            "ok": True,
            "questions": [{"q": "a", "why": "b", "options": ["c"]}],
            "draft_spec": "s",
            "acceptance_draft": ["x"],
            "scope_risk": "low",
            "estimated_effort": "1h",
            "model": "m",
            "latency_ms": 1,
            "unavailable": "",
            "raw": "",
        }
        r = clarify.ClarifyResult.from_dict(data)
        assert r.ok and len(r.questions) == 1 and r.questions[0].q == "a"

    def test_markdown_renders_questions(self):
        r = clarify.ClarifyResult(
            ok=True,
            questions=[clarify.ClarifyQuestion(q="Q1?", why="W", options=["a"])],
            draft_spec="spec",
            acceptance_draft=["c1"],
        )
        md = r.to_markdown("task text")
        assert "Q1?" in md and "c1" in md
