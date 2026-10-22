from inventory.orders import order_line


def test_order_line_normalizes_the_sku():
    assert order_line(" ab_12 ", 2, 3.5) == {"sku": "AB-12", "qty": 2, "total": 7.0}


def test_order_line_rounds_the_total_to_cents():
    assert order_line("x", 3, 2.5)["total"] == 7.5


def test_order_line_collapses_repeated_separators():
    assert order_line("a--b", 1, 1.0)["sku"] == "A-B"
