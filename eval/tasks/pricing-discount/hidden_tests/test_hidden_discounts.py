"""Hidden checks for the discount interface and its integration into quote."""

from __future__ import annotations

import inspect

import pytest

from pricing import LineItem, quote
from pricing.discounts import discount_for


def test_discount_for_signature_is_unchanged():
    parameters = inspect.signature(discount_for).parameters
    assert list(parameters) == ["subtotal", "vip", "coupon"]
    assert parameters["subtotal"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters["vip"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["vip"].default is False
    assert parameters["coupon"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["coupon"].default is None


def test_quote_takes_a_keyword_only_coupon():
    parameters = inspect.signature(quote).parameters
    assert {"items", "region", "customer_tier", "coupon"} == set(parameters)
    assert parameters["coupon"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["coupon"].default is None


@pytest.mark.parametrize(
    ("subtotal", "vip", "coupon", "expected"),
    [
        (100.0, False, None, 0.0),
        (100.0, True, None, 10.0),
        (250.0, True, None, 25.0),
        (250.0, True, "SAVE10", 25.0),
        (99.99, False, "SAVE10", 0.0),
        (100.0, False, "SAVE10", 10.0),
        (100.0, True, "SAVE10", 10.0),
        (33.33, True, None, 3.33),
        (5.0, True, None, 0.5),
        (0.0, True, None, 0.0),
        (-20.0, True, None, 0.0),
        (50.0, False, "NOT-A-CODE", 0.0),
        (50.0, False, "", 0.0),
    ],
)
def test_discount_rules(subtotal, vip, coupon, expected):
    assert discount_for(subtotal, vip=vip, coupon=coupon) == expected


def test_quote_returns_the_four_documented_keys():
    result = quote([LineItem("a", 1, 10.0)])
    assert set(result) == {"subtotal", "discount", "tax", "total"}
    assert result["discount"] == 0.0


def test_vip_discount_is_taxed_after_the_discount():
    result = quote([LineItem("a", 1, 100.0)], "cn", customer_tier="vip")
    assert result == {"subtotal": 100.0, "discount": 10.0, "tax": 11.7, "total": 101.7}


def test_coupon_below_the_threshold_does_not_discount():
    result = quote([LineItem("a", 1, 50.0)], "us", coupon="SAVE10")
    assert result == {"subtotal": 50.0, "discount": 0.0, "tax": 4.0, "total": 54.0}


def test_larger_discount_wins_on_a_big_vip_cart():
    result = quote([LineItem("a", 2, 100.0)], "jp", customer_tier="vip", coupon="SAVE10")
    assert result["discount"] == 20.0
    assert result["tax"] == 18.0
    assert result["total"] == 198.0


def test_standard_customer_with_coupon():
    result = quote([LineItem("a", 1, 200.0)], "none", coupon="SAVE10")
    assert result == {"subtotal": 200.0, "discount": 10.0, "tax": 0.0, "total": 190.0}
