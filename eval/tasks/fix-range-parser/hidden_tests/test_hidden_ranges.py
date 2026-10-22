"""Hidden checks for fix-range-parser."""

import pytest

from ranges import parse_ranges


def test_mixed_ranges():
    assert parse_ranges("1-3,5,8-10") == [1, 2, 3, 5, 8, 9, 10]


def test_single_element_range():
    assert parse_ranges("4-4") == [4]


def test_whitespace_and_trailing_comma():
    assert parse_ranges(" 7 , 2-3 , ") == [2, 3, 7]


def test_duplicates_collapse():
    assert parse_ranges("3,3,3,2-3") == [2, 3]


def test_overlapping_ranges_merge():
    assert parse_ranges("2-3,1-2") == [1, 2, 3]


def test_empty_string():
    assert parse_ranges("") == []


def test_large_range():
    assert parse_ranges("10-13") == [10, 11, 12, 13]


@pytest.mark.parametrize("bad", ["a-b", "1-x", "x-3", "1-2-3"])
def test_invalid_input_raises_value_error(bad):
    with pytest.raises(ValueError):
        parse_ranges(bad)
