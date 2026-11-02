"""The run and proposal state machines, and the one "needs attention" answer.

Every module that asks "what is this run doing" has so far answered it from the
fields it happened to read: `runner` writes `completed`/`failed`, `recover` owns
failed rows and names a reason it must not retry, `lineage` decides `ok` versus
`unverified` from completed + verify, and `server` sorts the same failures into
needs_you / handling / stopped. Four readers, four vocabularies. This module is
the vocabulary they can share: pure functions over data the caller already has,
no database, no network, no clock.

Run states::

    queued -> running -> completed
                      -> failed
                      -> stalled -> running
                                 -> failed

`completed` is an exit code, not a success: it says the process exited 0. The
question "was the result actually verified" belongs to `verify` and `lineage`,
and is deliberately not part of this table. A run that stops making progress is
`stalled`, not `running`: the runner already enforces that idea with a
no-output kill (`runner.IDLE_TIMEOUT`, default 900s) and `db.stale_runs()` finds
the same situation for a dead owner. Neither writes the state today -- the
runner records `failed` with a timeout cause and `reconcile` reaps the row --
so `stalled` exists here as the target of that migration rather than as a claim
that the column already holds it.

Proposal states::

    open -> merged
         -> rejected

`can_transition` is exact, not advisory: a state change that is not an edge in
the table above is `False`, including a state "change" to itself and any
unknown state name. An unknown *entity* raises, because that is a programming
error rather than a transition to refuse.

`needs_attention` returns "" when there is nothing to say and one of four
reasons otherwise. The mapping is copied from what the code already does, not
invented here: `tests/test_lifecycle.py` reads `recover`'s gates, `lineage`'s
ok/unverified rule and the board's grouping back out of the modules and asserts
this table does not contradict them.

New code that needs a run's state, or needs to know whether a person has to
act, must be built on this module instead of writing a fifth predicate. The
existing call sites are not migrated in this commit -- behaviour is unchanged
on purpose; `docs/overlap-audit.md` records who still reads what until they are.
"""

from __future__ import annotations

from typing import Any, Mapping

RUN = "run"
PROPOSAL = "proposal"

#: Entity -> its state table. An edge is "this transition is legal"; a state
#: with no outgoing edges is terminal. The order of `states()` follows this
#: dict and is the flow order, not a claim about how long a state lasts.
_TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    RUN: {
        "queued": frozenset({"running"}),
        "running": frozenset({"completed", "failed", "stalled"}),
        "stalled": frozenset({"running", "failed"}),
        "completed": frozenset(),
        "failed": frozenset(),
    },
    PROPOSAL: {
        "open": frozenset({"merged", "rejected"}),
        "merged": frozenset(),
        "rejected": frozenset(),
    },
}

#: How long a running run may record no progress before it is called stalled.
#: The same 900s the runner uses to kill a silent child (`IDLE_TIMEOUT`) and the
#: default `runner.RUNNER.reconcile(stale_seconds=900)` uses to reap a row. It
#: is a default for callers, not a measurement: `needs_attention` only applies
#: it when the caller passes `last_progress_at` *and* `now`.
STALL_AFTER = 900.0

#: Retry gates that mean "the machine will try again on its own". The empty
#: gate is `recover.retry_plan`'s "may start now"; `backoff` is the wait for its
#: slot. Both are the board's `handling` block.
RETRY_PENDING_GATES: tuple[str, ...] = ("", "backoff")

#: Retry gates that are a spend decision, the only failure-shaped rows the
#: board asks a person about (`server.SPEND_GATES`). Kept as a name here so
#: `needs_attention` can answer without importing the HTTP layer; a test
#: asserts the two tuples stay equal.
AWAITING_HUMAN_GATES: tuple[str, ...] = ("budget", "cost-cap", "over-budget")

#: Every other gate `recover.retry_plan` (or `server`, for `disabled`) can
#: return. These are situations the machine has finished with: a deterministic
#: error, an exhausted chain, a repeated signature, a run too old to recover, a
#: gone task, a live sibling, recovery switched off. `deterministic` and
#: `not-failed` are recover's own names; the rest are read from its source by
#: the tests, so a new gate cannot silently fall through to "done".
SETTLED_GATES: tuple[str, ...] = (
    "not-failed",
    "deterministic",
    "missing-context",
    "task-gone",
    "retry-limit",
    "already-retried",
    "task-running",
    "no-end-time",
    "too-old",
    "same-signature",
    "disabled",
)


def _table(entity: str) -> dict[str, frozenset[str]]:
    try:
        return _TRANSITIONS[str(entity)]
    except KeyError:
        raise ValueError(
            f"unknown entity {entity!r}; expected one of {', '.join(sorted(_TRANSITIONS))}"
        ) from None


def states(entity: str) -> tuple[str, ...]:
    """Every legal state for `entity`, in flow order."""
    return tuple(_table(entity))


def terminal(entity: str) -> set[str]:
    """The states `entity` cannot leave. A fresh set, safe for the caller to keep."""
    return {state for state, edges in _table(entity).items() if not edges}


def can_transition(entity: str, from_state: str, to_state: str) -> bool:
    """Is `from_state -> to_state` an edge in `entity`'s table?

    False for an unknown state name, for a state changing to itself, and for
    every edge the table does not declare. Raises only for an unknown entity.
    """
    table = _table(entity)
    if from_state not in table or to_state not in table:
        return False
    return to_state in table[from_state]


def _stalled(facts: Mapping[str, Any]) -> bool:
    """Did this running run record no progress for longer than the window?

    Requires both timestamps from the caller. A missing fact is not evidence:
    without `last_progress_at` and `now`, no stall is claimed.
    """
    last = facts.get("last_progress_at")
    now = facts.get("now")
    if last is None or now is None:
        return False
    try:
        elapsed = float(now) - float(last)
        window = float(facts.get("stall_after", STALL_AFTER))
    except (TypeError, ValueError):
        return False
    return elapsed > window


def needs_attention(run: Mapping[str, Any], facts: Mapping[str, Any]) -> str:
    """What, if anything, this run's state calls for. Empty string means nothing.

    The four non-empty answers:

    ``retry-pending``
        failed, retryable, and the automatic retry is either due now or waiting
        out its backoff (``recover.retry_plan`` gate `""` or `"backoff"`). The
        machine has it; this is the board's `handling` block.
    ``stalled``
        a running run that has recorded no progress for longer than
        `STALL_AFTER`, or a run whose recorded state is `stalled`. A running
        run with no progress facts makes no claim and returns `""`.
    ``awaiting-human``
        failed and blocked on a spend decision (`budget`, `cost-cap`,
        `over-budget`) -- the only failure-shaped rows the board puts under
        needs_you. A deterministic failure is NOT this: the board has already
        stopped it (`server._needs_you` skips deterministic causes).
    ``done``
        terminal, and nothing more will happen on its own: `completed`, a
        deterministic failure, an exhausted chain, a repeated signature, a row
        too old for the recovery window, a gone task, a live sibling task, or
        recovery switched off. `completed` is an exit code; whether it was
        verified is `lineage`'s question, and both its `ok` and `unverified`
        buckets land here without contradiction.

    ``facts`` is whatever the caller already knows; nothing here is looked up:

    ==================  =====================================================
    ``classification``  ``recover.classify(run)``: ``"deterministic"`` or
                        ``"retryable"``.
    ``retry_gate``      ``recover.retry_plan(run)["gate"]``: ``""``,
                        ``"backoff"``, or a named gate. **Required to answer
                        for a failed run**; `None`/absent means "not known",
                        which returns ``""`` rather than guessing.
    ``retry_enabled``   ``recover.enabled()``. Defaults to true, matching the
                        module default; pass it when recovery may be off.
    ``last_progress_at`` newest recorded event timestamp for a running run.
    ``now``             the clock the caller wants the stall window measured
                        against. Both timestamps are needed for a stall claim.
    ``stall_after``     seconds; defaults to `STALL_AFTER`.
    ==================  =====================================================

    The rules are ordered: `completed` and `stalled` are answered from the
    status alone; a failed run is judged first by ``classification``, then by
    whether recovery is on, then by ``retry_gate``. An unknown gate returns
    ``""`` -- the test that reads recover's gate names out of its source is
    what keeps that from becoming a silent "done".
    """
    status = str((run or {}).get("status") or "")
    facts = facts or {}

    if status == "queued":
        # Not started yet. A queued run is waiting for a slot, not for a person.
        return ""
    if status == "running":
        return "stalled" if _stalled(facts) else ""
    if status == "stalled":
        return "stalled"
    if status == "completed":
        return "done"
    if status != "failed":
        return ""

    if str(facts.get("classification") or "") == "deterministic":
        return "done"
    if not bool(facts.get("retry_enabled", True)):
        return "done"

    gate = facts.get("retry_gate")
    if gate is None:
        return ""
    gate = str(gate)
    if gate in RETRY_PENDING_GATES:
        return "retry-pending"
    if gate in AWAITING_HUMAN_GATES:
        return "awaiting-human"
    if gate in SETTLED_GATES:
        return "done"
    return ""
