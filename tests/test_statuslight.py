"""The status light only speaks when the news is bad and new.

These tests are pure: no database, no pet, no clock.
"""

from nodus import statuslight


def snap(waiting: int = 0, running: int = 0, failed: int = 0) -> dict:
    return {"waiting": waiting, "running": running, "failed": failed}


def test_quiet_when_nothing_changed():
    assert statuslight.line(snap(1, 2, 3), snap(1, 2, 3)) == ("", "")


def test_speaks_when_something_new_is_waiting():
    text, mood = statuslight.line(snap(1, 0, 0), snap(2, 0, 0))
    assert "2" in text and "等你" in text
    assert mood == "waiting"


def test_speaks_when_a_failure_is_new():
    text, mood = statuslight.line(snap(0, 0, 1), snap(0, 0, 4))
    assert "4" in text and mood == "error"


def test_waiting_outranks_failures():
    # Both went up: the human-owned item is the one worth a bubble, because
    # only that one can be unblocked by them.
    text, mood = statuslight.line(snap(0, 0, 0), snap(1, 0, 2))
    assert mood == "waiting"


def test_good_news_is_silent():
    # A count going down is not news; a pet that cheers every completion is
    # the reason people turn pets off.
    assert statuslight.line(snap(3, 0, 5), snap(0, 0, 0)) == ("", "")
