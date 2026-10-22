"""Hidden checks for word-metrics.

Copied into the work tree only after the agent has finished, so these cases are
never visible to it.
"""

from metrics import word_stats

EMPTY = {"words": 0, "chars": 0, "longest": "", "unique": 0}


def test_empty_string():
    assert word_stats("") == EMPTY


def test_whitespace_only():
    assert word_stats(" \t\n\u3000 ") == EMPTY


def test_single_word():
    assert word_stats("hello") == {
        "words": 1,
        "chars": 5,
        "longest": "hello",
        "unique": 1,
    }


def test_punctuation_stays_inside_a_token():
    assert word_stats("Hi, there!") == {
        "words": 2,
        "chars": 9,
        "longest": "there!",
        "unique": 2,
    }


def test_chars_ignores_whitespace():
    assert word_stats("a b\tc\nd") == {"words": 4, "chars": 4, "longest": "a", "unique": 4}


def test_tie_goes_to_the_first_longest():
    assert word_stats("ab cd")["longest"] == "ab"


def test_unique_is_case_sensitive():
    assert word_stats("A a A") == {"words": 3, "chars": 3, "longest": "A", "unique": 2}


def test_ideographic_space_counts_as_whitespace():
    assert word_stats("a\u3000b") == {"words": 2, "chars": 2, "longest": "a", "unique": 2}


def test_tabs_and_newlines():
    assert word_stats("one\ttwo\nthree  four") == {
        "words": 4,
        "chars": 15,
        "longest": "three",
        "unique": 4,
    }


def test_repeated_tokens_collapse_in_unique_only():
    assert word_stats("go go go stop") == {
        "words": 4,
        "chars": 10,
        "longest": "stop",
        "unique": 2,
    }


def test_unicode_text_is_counted_char_by_char():
    assert word_stats("你好 世界") == {
        "words": 2,
        "chars": 4,
        "longest": "你好",
        "unique": 2,
    }
