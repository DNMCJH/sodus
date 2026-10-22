from ranges import parse_ranges


def test_single_number():
    assert parse_ranges("5") == [5]


def test_range_includes_both_ends():
    assert parse_ranges("1-3") == [1, 2, 3]
