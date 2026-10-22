"""Hidden checks: the shared normalizer exists, is the only one, and behavior held."""

from __future__ import annotations

import pytest

import inventory.common
import inventory.orders
import inventory.reports
import inventory.stock


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ab_12", "AB-12"),
        ("  Cafe  Latte ", "CAFE-LATTE"),
        ("a--b", "A-B"),
        ("a_-_b", "A-B"),
        ("Sku-9", "SKU-9"),
        ("", ""),
        ("   ", ""),
        ("---", ""),
        ("_", ""),
    ],
)
def test_common_normalizer_rules(raw, expected):
    assert inventory.common.normalize_sku(raw) == expected


def _explode(_sku):
    raise RuntimeError("the shared normalizer was called")


def test_stock_delegates_to_the_common_normalizer(monkeypatch):
    monkeypatch.setattr(inventory.common, "normalize_sku", _explode)
    with pytest.raises(RuntimeError, match="shared normalizer"):
        inventory.stock.add_sku({}, "ab_12", 1)


def test_orders_delegates_to_the_common_normalizer(monkeypatch):
    monkeypatch.setattr(inventory.common, "normalize_sku", _explode)
    with pytest.raises(RuntimeError, match="shared normalizer"):
        inventory.orders.order_line("ab_12", 1, 2.0)


def test_reports_delegates_to_the_common_normalizer(monkeypatch):
    monkeypatch.setattr(inventory.common, "normalize_sku", _explode)
    with pytest.raises(RuntimeError, match="shared normalizer"):
        inventory.reports.sku_summary([{"sku": "ab_12", "qty": 1}])


def test_the_three_public_functions_still_agree_on_edge_cases():
    weird = ["  a__b ", "a--b", "A _ B", "", "---"]
    for sku in weird:
        expected = inventory.common.normalize_sku(sku)
        assert inventory.stock.add_sku({}, sku, 1) == {expected: 1}
        assert inventory.orders.order_line(sku, 1, 1.0)["sku"] == expected
        assert inventory.reports.sku_summary([{"sku": sku, "qty": 1}]) == {expected: 1}


def test_public_shapes_did_not_change():
    assert inventory.stock.add_sku({}, "x", 2) == {"X": 2}
    assert inventory.orders.order_line("x", 3, 2.5) == {
        "sku": "X",
        "qty": 3,
        "total": 7.5,
    }
    assert inventory.reports.sku_summary(
        [{"sku": "a_b", "qty": 2}, {"sku": "A-B", "qty": 3}]
    ) == {"A-B": 5}
