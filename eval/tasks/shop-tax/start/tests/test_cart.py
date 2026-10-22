from shop.cart import subtotal, total


def test_subtotal():
    assert subtotal([(10.0, 2), (2.5, 4)]) == 30.0


def test_total_without_tax():
    assert total([(10.0, 2)], "none") == 20.0


def test_total_defaults_to_no_tax():
    assert total([(10.0, 2)]) == 20.0
