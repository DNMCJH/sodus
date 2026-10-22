"""Hidden checks for shop-tax: new module, wiring, and the old behaviour."""

import pytest

import shop.tax
from shop.cart import subtotal, total
from shop.tax import tax_for


def test_rates_table_is_exact():
    assert shop.tax.RATES == {"cn": 0.13, "us": 0.08, "jp": 0.10}


def test_tax_for_uses_the_rate():
    assert tax_for(100.0, "cn") == pytest.approx(13.0)
    assert tax_for(100.0, "us") == pytest.approx(8.0)
    assert tax_for(100.0, "jp") == pytest.approx(10.0)


def test_tax_for_rejects_unknown_region():
    with pytest.raises(ValueError) as exc:
        tax_for(10.0, "mars")
    assert "mars" in str(exc.value)


def test_cart_adds_tax():
    assert total([(10.0, 2)], "cn") == 22.6


def test_cart_rounds_to_two_places():
    assert total([(19.99, 1)], "us") == 21.59


def test_cart_without_tax_is_unchanged():
    assert total([(10.0, 2)], "none") == 20.0
    assert total([(10.0, 2)]) == 20.0


def test_cart_rejects_unknown_region():
    with pytest.raises(ValueError):
        total([(10.0, 2)], "mars")


def test_subtotal_still_works():
    assert subtotal([(10.0, 2), (2.5, 4)]) == 30.0
