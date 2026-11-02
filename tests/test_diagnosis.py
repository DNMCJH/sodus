"""One failure vocabulary: the four entry points must agree on the same run.

The four readers before `diagnosis` each had their own table, so the same error
could be `upstream` to `recover`, `无法归类` on the board and `进程自己退出`
to the retry history. These tests pin the one table and then ask all four
entry points about the same run; a disagreement fails here instead of on a
card.
"""

from __future__ import annotations

import pytest

from nodus import diagnosis, recover, server

#: One real-looking error line per cause. The first two and `[WinError 2]` are
#: verbatim from `~/.nodus/nodus.sqlite3` (37 failed runs, 2026-11-03); the
#: rest are the shapes that history and the old tests recorded. `tests-red`
#: and `no-change` are inferred from facts, not from an error line, and get
#: their own cases below.
CASES = [
    ("orphaned", "orphaned: the process that owned this run is gone"),
    ("exit-code", "exit code 1"),
    ("timeout", "Command timed out after 60s"),
    ("network", "ECONNRESET"),
    ("5xx", "502 Bad Gateway"),
    ("rate-limit", "429 Too Many Requests"),
    ("auth", "401 Unauthorized"),
    ("bad-request", "400 Bad Request: messages must not be empty"),
    ("model-missing", "model_not_found: gpt-9"),
    ("content-policy", "content policy violation"),
    ("unknown", "stale run from hung test"),
]


def _run(error: str, status: str = "failed") -> dict:
    return {"id": "run-1", "status": status, "error": error}


@pytest.mark.parametrize(("expected", "error"), CASES)
def test_the_four_entry_points_name_one_cause(expected, error):
    run = _run(error)
    answer = diagnosis.diagnose(run)

    assert answer.cause == expected
    # 1. the retry decision's cause
    assert recover.cause(run) == answer.cause
    # 2. the retry class
    assert recover.classify(run) == answer.failure_class
    # 3. the board's failure line
    assert server._failure_kind(run, {}, "none") == answer.kind
    # 4. the cause line the UI renders
    assert server._likely_cause(run, {}, "none").startswith(
        f"大概率是{answer.label}"
    )
    # kind and label are two names for one table, not two tables.
    assert answer.kind == answer.label
    assert answer.label in server.CAUSE_LABELS


def test_a_red_test_is_the_fourth_entry_point_on_a_completed_run():
    run = _run("", status="completed")
    facts = {"verify": "red"}
    answer = diagnosis.diagnose(run, facts)
    evidence = {"tests": [{"test": "test_token_refresh", "assertion": "401 == 200"}]}

    assert answer.cause == "tests-red"
    assert answer.failure_class == "human"
    assert recover.cause(run, facts) == "tests-red"
    assert recover.classify(run, facts) == "human"
    assert server._failure_kind(run, evidence, "red") == "测试红"
    assert server._likely_cause(run, evidence, "red").startswith("大概率是测试红")


def test_a_failed_run_with_no_record_is_no_change_not_a_guess():
    run = _run("")
    facts = {"operations": False}
    answer = diagnosis.diagnose(run, facts)

    assert answer.cause == "no-change"
    assert answer.failure_class == "human"
    assert recover.cause(run, facts) == "no-change"
    assert recover.classify(run, facts) == "human"
    assert server._failure_kind(run, {}, "none") == "证据缺失"
    assert server._likely_cause(run, {}, "none").startswith("大概率是证据缺失")


def test_a_completed_run_is_not_a_failure():
    answer = diagnosis.diagnose(_run("", status="completed"))
    assert answer.cause == ""
    assert answer.failure_class == "none"

    running = diagnosis.diagnose(_run("starting", status="running"))
    assert running.failure_class == "none"


def test_every_cause_has_exactly_one_class_one_kind_and_one_sentence():
    assert set(diagnosis.CAUSES) == set(diagnosis.KINDS)
    assert len(diagnosis.CAUSES) == len(set(diagnosis.CAUSES)) == 13
    for cause in diagnosis.CAUSES:
        assert diagnosis.class_of(cause) in diagnosis.CLASSES
        assert diagnosis.kind(cause)
        assert diagnosis.label(cause) == diagnosis.kind(cause)
        assert diagnosis.problem(cause)
        assert diagnosis.suggestion(cause)
    # The labels the advice tables answer for are the labels this module emits.
    assert tuple(diagnosis.label(cause) for cause in diagnosis.CAUSES) == server.CAUSE_LABELS


def test_the_retry_classes_are_the_old_deterministic_line():
    # The four old deterministic causes (auth/param/policy/model) are the same
    # four after the rename; everything else still goes to the other gates.
    assert diagnosis.DETERMINISTIC == {
        "auth", "bad-request", "model-missing", "content-policy"
    }
    for cause in diagnosis.DETERMINISTIC:
        assert diagnosis.class_of(cause) == "deterministic"
    for cause in ("orphaned", "timeout", "network", "5xx", "rate-limit", "exit-code", "unknown"):
        assert diagnosis.class_of(cause) == "transient"
