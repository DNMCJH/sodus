"""Harness identity per run: what the worker saw, which route served it, how it ended.

The thing that improves in an agent system is not only the model: it is the
harness around it — the prompt template, the route, the model, the acceptance
check. The 2026 papers on harness self-improvement (Harness-Zero, Harness²)
both start from the same prerequisite: a comparable record of what was actually
running and how it turned out. Without that, "this route is better for this
kind of failure" is a feeling, not evidence.

This module is read-only. It derives a fingerprint from a run row (the row is
the source for agent/route/model/provider), reads the one recorded
`run.harness` event for the brief version, and folds run history into
(component, cause) buckets. It schedules nothing, mutates nothing, spends
nothing; the command behind it is `nodus debug harness-stats`.

The cause keys are the machine causes from `diagnosis` (timeout, auth, …),
not the display sentences: a bucket that is keyed by changing copy cannot be
compared across versions.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping

from . import diagnosis

#: The components a run's harness is compared on. Order is the display order.
COMPONENTS: tuple[str, ...] = ("route", "agent", "model", "provider", "brief_version")

#: What a run says when it did not record a component. A plain fact, not a
#: guessed default: there is no "default harness" to substitute, and inventing
#: one would make two different setups look like the same series.
UNRECORDED = "未记录"

#: Runs that did not fail. Kept out of the cause buckets so a rate means "when
#: it breaks like this, does this harness get through", not "how often does the
#: model breathe".
NO_CAUSE = "未失败"

#: The outcome classes an ended run can land in. `unverified` is deliberately
#: not `passed`: a completed process is an exit code, not evidence the work was
#: accepted.
OUTCOMES: tuple[str, ...] = ("passed", "red", "failed", "unverified")


def version_of(template: str) -> str:
    """The brief's identity: a hash of the prompt template itself.

    A hand-bumped number drifts from the text the moment somebody edits the
    template and forgets the number. Hashing the template means the version
    changes exactly when the prompt does.
    """
    return hashlib.sha1(str(template or "").encode("utf-8")).hexdigest()[:8]


def fingerprint(run: Mapping[str, Any], brief_version: str = "") -> dict[str, str]:
    """The harness behind one run, one string per component.

    `route` falls back to the agent because a route named after its agent is
    how most rows were written before the column existed; it is the same fact,
    not a guess about a different one.
    """
    agent = str(run.get("agent") or "").strip()
    return {
        "agent": agent or UNRECORDED,
        "route": str(run.get("route") or agent or UNRECORDED).strip(),
        "model": str(run.get("model") or "").strip() or UNRECORDED,
        "provider": str(run.get("provider") or "").strip() or UNRECORDED,
        "brief_version": str(brief_version or "").strip() or UNRECORDED,
    }


def outcome(run: Mapping[str, Any], verify: str) -> str:
    """Which bucket one ended run belongs in.

    A run still in flight is `live` — it has no outcome yet and counting it as
    a failure would make a busy machine look like a broken one.
    """
    status = str(run.get("status") or "")
    if status in ("queued", "running", "stalled"):
        return "live"
    if status == "failed":
        return "failed"
    if verify == "red":
        return "red"
    if verify == "ok":
        return "passed"
    return "unverified"


def stats(
    rows: Iterable[Mapping[str, Any]],
    verify: Mapping[str, str],
    briefs: Mapping[str, str] | None = None,
    *,
    by: str = "route",
) -> dict:
    """Fold run history into (component, cause) buckets.

    `verify` maps run id -> `ok`/`red`/`none` (`db.verify_states`); `briefs` maps
    run id -> the `run.harness` event's version for runs that recorded one.
    Runs that did not fail keep `NO_CAUSE`, so the clean-run rate stays a
    separate question from "when this breaks, does this route get through".

    `pass_rate` divides by the runs that actually have an outcome
    (`passed + red + failed`). `unverified` is printed but never divided in: an
    exit code with no test is not a pass.
    """
    if by not in COMPONENTS:
        raise ValueError(f"unknown harness component: {by}")
    briefs = briefs or {}
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    live = 0
    for row in rows:
        rid = str(row.get("id") or "")
        mark = str(verify.get(rid) or "none")
        kind = outcome(row, mark)
        if kind == "live":
            live += 1
            continue
        fp = fingerprint(row, briefs.get(rid, ""))
        cause = diagnosis.cause(row, {"verify": mark}) or NO_CAUSE
        key = (fp[by], cause)
        bucket = groups.setdefault(
            key,
            {
                "key": fp[by],
                "cause": cause,
                "runs": 0,
                "passed": 0,
                "red": 0,
                "failed": 0,
                "unverified": 0,
            },
        )
        bucket[kind] += 1
        bucket["runs"] += 1

    out: list[dict[str, Any]] = []
    for bucket in groups.values():
        decided = bucket["passed"] + bucket["red"] + bucket["failed"]
        bucket["pass_rate"] = round(bucket["passed"] / decided, 3) if decided else None
        out.append(bucket)
    # Rows that carry evidence come first: the clean-run rows are context, the
    # (route, cause) rows with a decided outcome are the question.
    out.sort(
        key=lambda b: (
            -(b["passed"] + b["red"] + b["failed"]),
            -b["runs"],
            b["key"],
            b["cause"],
        )
    )

    totals = {name: 0 for name in ("runs", "passed", "red", "failed", "unverified")}
    for bucket in out:
        for name in totals:
            totals[name] += int(bucket[name])
    decided = totals["passed"] + totals["red"] + totals["failed"]
    totals["pass_rate"] = round(totals["passed"] / decided, 3) if decided else None
    return {"by": by, "groups": out, "totals": totals, "live": live}
