from inventory.reports import sku_summary


def test_sku_summary_merges_differently_spelled_skus():
    entries = [
        {"sku": "a_b", "qty": 2},
        {"sku": " A-B ", "qty": 3},
        {"sku": "c", "qty": 1},
    ]
    assert sku_summary(entries) == {"A-B": 5, "C": 1}


def test_sku_summary_of_nothing_is_empty():
    assert sku_summary([]) == {}


def test_sku_summary_keeps_the_empty_key():
    assert sku_summary([{"sku": "", "qty": 2}]) == {"": 2}
