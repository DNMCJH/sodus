from inventory.stock import add_sku


def test_add_sku_creates_a_normalized_key():
    state = add_sku({}, "ab_12", 2)
    assert state == {"AB-12": 2}


def test_add_sku_merges_variants_of_the_same_sku():
    state = add_sku({}, "ab_12", 2)
    assert add_sku(state, " AB-12 ", 3) == {"AB-12": 5}


def test_add_sku_keeps_the_empty_key_for_an_empty_sku():
    assert add_sku({}, "", 4) == {"": 4}
