"""Hidden checks for interface-first-rules: shape first, then behaviour.

Two groups, both required:

- the interface the review console binds to (abstract members, signatures,
  dataclass fields, error attributes);
- the dispatch semantics (priority, ties, trace, read-only context, error
  wrapping) exercised through custom rules plus the two shipped rules.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, fields, is_dataclass
from types import MappingProxyType

import pytest

import rulekit
import rulekit.base
import rulekit.engine
import rulekit.rules
from rulekit import (
    Decision,
    Engine,
    HighValueRule,
    LowRiskRule,
    NoRuleMatched,
    Rule,
    RuleError,
)

# ---- interface ---------------------------------------------------------


def test_rule_is_abstract_with_the_two_abstract_methods():
    assert inspect.isabstract(Rule)
    assert set(Rule.__abstractmethods__) == {"check", "apply"}
    assert Rule.code == ""
    assert Rule.priority == 0
    assert list(inspect.signature(Rule.check).parameters) == ["self", "ctx"]
    assert list(inspect.signature(Rule.apply).parameters) == ["self", "ctx"]


def test_decision_is_a_frozen_dataclass_with_exact_fields():
    assert is_dataclass(Decision)
    assert [field.name for field in fields(Decision)] == ["code", "priority", "payload", "trace"]
    decision = Decision("x", 1, {"a": 1}, ("x",))
    assert decision.payload == {"a": 1}
    with pytest.raises(FrozenInstanceError):
        decision.code = "y"  # type: ignore[misc]


def test_rule_error_carries_the_rule_and_keeps_rule_keyword_only():
    parameters = inspect.signature(RuleError.__init__).parameters
    assert list(parameters) == ["self", "message", "rule"]
    assert parameters["rule"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["rule"].default is None
    error = RuleError("bad", rule="x")
    assert str(error) == "bad"
    assert error.rule == "x"
    assert RuleError("plain").rule is None


def test_no_rule_matched_takes_ctx_and_is_a_rule_error():
    assert issubclass(NoRuleMatched, RuleError)
    parameters = inspect.signature(NoRuleMatched.__init__).parameters
    assert list(parameters) == ["self", "message", "ctx"]
    error = NoRuleMatched("nothing", {"a": 1})
    assert error.ctx == {"a": 1}
    assert error.rule is None


def test_engine_signatures_and_the_rules_property():
    init = inspect.signature(Engine.__init__).parameters
    assert list(init) == ["self", "rules"]
    assert init["rules"].default == ()
    assert [p for p in inspect.signature(Engine.register).parameters] == ["self", "rule"]
    assert [p for p in inspect.signature(Engine.evaluate).parameters] == ["self", "ctx"]
    assert isinstance(Engine.rules, property)
    assert Engine().rules == ()


def test_package_exports_every_name():
    for name in (
        "Rule",
        "RuleError",
        "NoRuleMatched",
        "Decision",
        "Engine",
        "HighValueRule",
        "LowRiskRule",
    ):
        assert getattr(rulekit, name, None) is not None, name
    assert rulekit.base.Rule is Rule
    assert rulekit.engine.Engine is Engine
    assert rulekit.rules.HighValueRule is HighValueRule


# ---- behaviour ---------------------------------------------------------


class NamedRule(Rule):
    """A rule whose check result is fixed, used to pin dispatch order."""

    def __init__(self, code: str, priority: int, answer: bool = True) -> None:
        self.code = code
        self.priority = priority
        self.answer = answer

    def check(self, ctx: Mapping) -> bool:
        return self.answer

    def apply(self, ctx: Mapping) -> dict:
        return {"ran": self.code}


def test_priority_wins_and_trace_is_ordered_by_priority():
    engine = Engine([NamedRule("low", 1), NamedRule("high", 5)])
    decision = engine.evaluate({"amount": 1})
    assert decision.code == "high"
    assert decision.priority == 5
    assert decision.trace == ("high", "low")
    assert decision.payload == {"ran": "high"}


def test_equal_priority_falls_back_to_registration_order():
    a = NamedRule("a", 3)
    b = NamedRule("b", 3)
    assert Engine([a, b]).evaluate({}).code == "a"
    assert Engine([b, a]).evaluate({}).code == "b"


def test_non_matching_rules_stay_out_of_the_trace():
    engine = Engine([NamedRule("no", 9, answer=False), NamedRule("yes", 1)])
    decision = engine.evaluate({})
    assert decision.code == "yes"
    assert decision.trace == ("yes",)


def test_context_is_one_read_only_shallow_copy_shared_with_apply():
    seen = {}

    class Capture(Rule):
        code = "capture"
        priority = 1

        def check(self, ctx):
            seen["check"] = ctx
            return True

        def apply(self, ctx):
            seen["apply"] = ctx
            return {}

    original = {"amount": 10}
    Engine([Capture()]).evaluate(original)
    assert isinstance(seen["check"], MappingProxyType)
    assert seen["check"] is seen["apply"]
    with pytest.raises(TypeError):
        seen["check"]["amount"] = 20  # type: ignore[index]
    assert original == {"amount": 10}


def test_no_match_raises_with_a_copied_context():
    with pytest.raises(NoRuleMatched) as raised:
        Engine([NamedRule("no", 1, answer=False)]).evaluate({"amount": 10})
    error = raised.value
    assert isinstance(error.ctx, dict)
    assert error.ctx == {"amount": 10}
    assert error.rule is None
    error.ctx["amount"] = 99
    assert error.ctx == {"amount": 99}


def test_evaluate_rejects_a_non_mapping_context():
    with pytest.raises(TypeError):
        Engine([NamedRule("r", 1)]).evaluate(["not", "a", "mapping"])
    with pytest.raises(TypeError):
        Engine([NamedRule("r", 1)]).evaluate(None)


def test_register_rejects_bad_rules():
    engine = Engine()
    with pytest.raises(TypeError):
        engine.register(object())
    with pytest.raises(ValueError):
        engine.register(NamedRule("", 1))
    with pytest.raises(TypeError):
        engine.register(NamedRule("float-priority", 1.5))
    engine.register(NamedRule("dup", 1))
    with pytest.raises(ValueError):
        engine.register(NamedRule("dup", 2))
    assert [rule.code for rule in engine.rules] == ["dup"]


def test_apply_must_return_a_dict():
    class BadResult(Rule):
        code = "bad-result"
        priority = 1

        def check(self, ctx):
            return True

        def apply(self, ctx):
            return ["not", "a", "dict"]

    with pytest.raises(RuleError) as raised:
        Engine([BadResult()]).evaluate({})
    assert raised.value.rule == "bad-result"
    assert not isinstance(raised.value, NoRuleMatched)


def test_other_exceptions_are_wrapped_with_the_rule_and_a_cause():
    class Exploding(Rule):
        code = "exploding"
        priority = 1

        def check(self, ctx):
            raise KeyError("missing")

        def apply(self, ctx):
            return {}

    with pytest.raises(RuleError) as raised:
        Engine([Exploding()]).evaluate({})
    assert raised.value.rule == "exploding"
    assert isinstance(raised.value.__cause__, KeyError)


def test_a_rule_error_from_check_passes_through_unchanged():
    error = RuleError("deliberate", rule="boom")

    class Booming(Rule):
        code = "boom"
        priority = 1

        def check(self, ctx):
            raise error

        def apply(self, ctx):
            return {}

    with pytest.raises(RuleError) as raised:
        Engine([Booming()]).evaluate({})
    assert raised.value is error


# ---- shipped rules -----------------------------------------------------


def test_high_value_rule_checks_and_payload():
    rule = HighValueRule()
    assert rule.code == "high-value"
    assert rule.priority == 20
    assert rule.check({"amount": 100.0}) is True
    assert rule.check({"amount": 100.01}) is True
    assert rule.check({"amount": 99.99}) is False
    assert rule.check({"amount": "150"}) is False
    assert rule.check({}) is False
    assert rule.apply({"amount": 150}) == {
        "decision": "review",
        "rule": "high-value",
        "amount": 150,
    }


def test_low_risk_rule_checks_and_payload():
    rule = LowRiskRule()
    assert rule.code == "low-risk"
    assert rule.priority == 10
    assert rule.check({"country": "CN", "amount": 50}) is True
    assert rule.check({"country": "SG", "amount": 99.99}) is True
    assert rule.check({"country": "CN", "amount": 100}) is False
    assert rule.check({"country": "US", "amount": 50}) is False
    assert rule.check({"country": "CN"}) is False
    assert rule.apply({"country": "CN", "amount": 50}) == {
        "decision": "approve",
        "rule": "low-risk",
        "country": "CN",
    }


def test_builtin_rules_through_the_engine():
    engine = Engine([LowRiskRule(), HighValueRule()])
    decision = engine.evaluate({"amount": 150, "country": "CN"})
    assert decision.code == "high-value"
    assert decision.priority == 20
    assert decision.trace == ("high-value",)
    assert decision.payload["decision"] == "review"

    low = engine.evaluate({"amount": 50, "country": "CN"})
    assert low.code == "low-risk"
    assert low.trace == ("low-risk",)

    with pytest.raises(NoRuleMatched):
        engine.evaluate({"amount": 50, "country": "US"})
