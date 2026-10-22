import pytest

from pricing import LineItem, quote


def test_empty_cart_has_no_tax():
    result = quote([])
    assert result["subtotal"] == 0.0
    assert result["tax"] == 0.0
    assert result["total"] == 0.0


def test_region_none_means_no_tax():
    result = quote([LineItem("a", 2, 10.0)])
    assert result["subtotal"] == 20.0
    assert result["tax"] == 0.0
    assert result["total"] == 20.0


def test_cn_tax_is_thirteen_percent():
    result = quote([LineItem("a", 1, 100.0)], "cn")
    assert result["tax"] == 13.0
    assert result["total"] == 113.0


def test_unknown_region_raises():
    with pytest.raises(ValueError, match="mars"):
        quote([], "mars")


def test_totals_round_to_cents():
    result = quote([LineItem("a", 3, 0.33)], "none")
    assert result["subtotal"] == 0.99
    assert result["total"] == 0.99
