"""The other terminals on this machine, read the same way DSH is.

Nodus dispatches work to several harness CLIs. Each of them keeps its own
session log, and that log is the only presence signal the board can read
without turning the board into a supervisor: this module never starts a
harness, never calls its CLI and never opens a socket. Three questions per
harness, all read-only:

- 装没装: `shutil.which` (plus a known install location for a CLI that keeps
  its binary out of PATH, which is how Kimi ships);
- 在不在跑: the newest mtime under that harness's own session log, or a run
  the run table still marks queued/running. A process table would answer a
  sharper question, but it needs a platform-specific subprocess per refresh;
  the person reading the board is asking "is it busy", not "is the process
  alive";
- 今天: runs / spend / conversations aggregated from the run table, so all
  five are counted by one rule. A harness `Nodus` never dispatched simply
  reads zero instead of disappearing.

DSH's observed sessions are priced by `observe.py` and land in 今天的钱. They
are deliberately not folded in here: a per-harness total only DSH can fill
would make the one thing this panel is for -- treating the five the same --
untrue.

A missing directory, a stale file, a harness never used on this machine: all
of those are answers, not exceptions.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import db, ledger, observe

#: A log written within this window means the harness is still working. The
#: board refreshes by hand, so the window only has to bridge a pause in a
#: conversation, not a polling interval.
ACTIVE_SECONDS = 600.0

#: Run statuses that mean a process Nodus started is still going. A live run
#: is presence a session log cannot show (the log may not exist yet).
LIVE_STATUS = ("queued", "running")


@dataclass(frozen=True)
class Harness:
    """One terminal: its CLI name and where its own log lives."""

    key: str
    name: str
    cli: str
    #: Globs, relative to the harness's log root. These are the actual file
    #: layouts on this machine, one pattern per version that matters.
    logs: tuple[str, ...]
    #: Fallback install locations, relative to the user's home. `which` is the
    #: real answer; this only covers a CLI that does not put itself on PATH.
    candidates: tuple[str, ...] = ()


HARNESSES: tuple[Harness, ...] = (
    Harness("dsh", "DSH", "dsh", (observe.FRAME_GLOB,)),
    Harness("claude", "Claude Code", "claude", ("projects/**/*.jsonl",)),
    Harness("codex", "Codex", "codex", ("sessions/**/rollout-*.jsonl",)),
    # OpenCode moved its sessions into one SQLite file; the -wal file gets the
    # fresh write. The -shm file is deliberately excluded: merely opening a WAL
    # database touches it, so a read would otherwise look like activity.
    Harness(
        "opencode",
        "OpenCode",
        "opencode",
        ("*.db", "*.db-wal", "storage/session/**/*.json"),
    ),
    # Kimi writes one wire.jsonl per session under
    # sessions/<workspace>/<session>/agents/main/.
    Harness(
        "kimi",
        "Kimi",
        "kimi",
        ("sessions/**/agents/*/wire.jsonl", "sessions/**/state.json", "session_index.jsonl"),
        (".kimi-code/bin/kimi.exe",),
    ),
)
HARNESS_BY_KEY = {harness.key: harness for harness in HARNESSES}


def roots(home: Path | None = None) -> dict[str, Path]:
    """Where each harness keeps its logs, on this machine."""
    base = Path(home).expanduser() if home is not None else Path.home()
    return {
        "dsh": observe.root(),
        "claude": base / ".claude",
        "codex": base / ".codex",
        "opencode": base / ".local" / "share" / "opencode",
        "kimi": base / ".kimi-code",
    }


def harness_key(agent: str) -> str:
    """Which terminal a `run.agent` belongs to, or "" when it is not one.

    `opencode-glm` and `opencode-gemini` are two routes through one terminal,
    so they share a row. A route such as `opus` or `minimax` stays with the
    CLI that ran it (the agent column), which is what the page is naming.
    """
    name = str(agent or "").strip().lower()
    if name.startswith("opencode"):
        return "opencode"
    return name if name in HARNESS_BY_KEY else ""


def newest_activity(root: Path, logs: Iterable[str]) -> float | None:
    """The freshest write time in a harness's own session log, or None."""
    newest: float | None = None
    for pattern in logs:
        for name in glob.glob(str(Path(root) / pattern), recursive=True):
            try:
                stamp = os.stat(name).st_mtime
            except OSError:  # deleted between glob and stat is not an error
                continue
            if newest is None or stamp > newest:
                newest = stamp
    return newest


def _binary(harness: Harness, home: Path, which: Callable[[str], str | None]) -> str:
    found = which(harness.cli)
    if found:
        return str(found)
    for relative in harness.candidates:
        candidate = Path(home) / relative
        if candidate.exists():
            return str(candidate)
    return ""


def probe(
    *,
    now: float | None = None,
    home: Path | None = None,
    roots_override: Mapping[str, Path] | None = None,
    which: Callable[[str], str | None] | None = None,
    active_seconds: float = ACTIVE_SECONDS,
) -> list[dict]:
    """Filesystem-only status for the five terminals, in a fixed order.

    No run table here: this is the half that can be checked with temp
    directories in tests. `snapshot` adds today's work.
    """
    moment = time.time() if now is None else float(now)
    base = Path(home).expanduser() if home is not None else Path.home()
    dirs = roots(base)
    if roots_override:
        dirs.update({key: Path(value) for key, value in roots_override.items()})
    resolver = which or shutil.which
    rows: list[dict] = []
    for harness in HARNESSES:
        root = dirs[harness.key]
        binary = _binary(harness, base, resolver)
        last_active = newest_activity(root, harness.logs)
        active = bool(last_active is not None and moment - last_active <= active_seconds)
        rows.append(
            {
                "key": harness.key,
                "name": harness.name,
                "installed": bool(binary),
                "binary": binary,
                "log_root": str(root),
                "last_active": last_active,
                "running": active,
                "state": "running" if active else ("idle" if binary else "missing"),
            }
        )
    return rows


def live_harnesses(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    """Terminals with a run still marked queued/running."""
    keys: set[str] = set()
    for row in rows:
        if str(row.get("status") or "") not in LIVE_STATUS:
            continue
        key = harness_key(str(row.get("agent") or ""))
        if key:
            keys.add(key)
    return keys


def _session_id(row: Mapping[str, Any]) -> str:
    """The vendor conversation a run recorded, else the run is its own."""
    try:
        ref = json.loads(row.get("session_ref_json") or "{}")
    except (TypeError, ValueError):
        ref = {}
    if isinstance(ref, dict) and ref.get("session_id"):
        return str(ref["session_id"])
    return str(row.get("id") or "")


def by_harness(
    rows: Iterable[Mapping[str, Any]], *, start: float | None = None
) -> dict[str, dict]:
    """Today's runs / spend / conversations per terminal.

    Runs are work only (a pet utterance is a token bill, not an attempt -- the
    same rule 今天的钱 and the run window use). Spend includes chat rows: the
    money was paid today, and hiding it would make the panel disagree with the
    bill. `sessions` counts the vendor conversation ids reached today; a run
    that recorded none counts as its own (hence the id column), so the number
    never under-reports.
    """
    buckets: dict[str, dict] = {}
    for row in rows:
        if start is not None and not ledger.in_window(row, start, None):
            continue
        key = harness_key(str(row.get("agent") or ""))
        if not key:
            continue
        bucket = buckets.setdefault(
            key, {"runs": 0, "sessions": set(), "cost": {}}
        )
        if str(row.get("kind") or "work") != "chat":
            bucket["runs"] += 1
            session = _session_id(row)
            if session:
                bucket["sessions"].add(session)
        priced = ledger.cost_of(row.get("usage_json"))
        if priced is not None:
            amount, currency, _basis = priced
            bucket["cost"][currency] = round(
                bucket["cost"].get(currency, 0.0) + float(amount), 6
            )
    for bucket in buckets.values():
        bucket["sessions"] = len(bucket["sessions"])
    return buckets


def snapshot(
    conn,
    *,
    now: float | None = None,
    start: float | None = None,
    home: Path | None = None,
    roots_override: Mapping[str, Path] | None = None,
    which: Callable[[str], str | None] | None = None,
    active_seconds: float = ACTIVE_SECONDS,
) -> list[dict]:
    """The `terminals` array for /board: presence plus today's numbers.

    `conn` is the board's own database connection, so the page and this panel
    read the same transaction and the same local day boundary.
    """
    moment = time.time() if now is None else float(now)
    if start is None:
        start = ledger.bounds("day", moment)[0]
    rows = db.runs_in_window(start, live=True, conn=conn)
    today = by_harness(rows, start=start)
    live = live_harnesses(rows)
    terminals = probe(
        now=moment,
        home=home,
        roots_override=roots_override,
        which=which,
        active_seconds=active_seconds,
    )
    for terminal in terminals:
        stats = today.get(terminal["key"]) or {"runs": 0, "sessions": 0, "cost": {}}
        terminal["today"] = stats
        # A run dated today proves the CLI exists even when this process's PATH
        # disagrees with the daemon's; an empty PATH lookup is not evidence a
        # harness that just did work is gone.
        installed = bool(terminal["installed"] or stats["runs"])
        # A queued/running run is presence no log mtime can show.
        running = bool(terminal["running"] or terminal["key"] in live)
        terminal["installed"] = installed
        terminal["running"] = running
        terminal["state"] = "running" if running else ("idle" if installed else "missing")
    return terminals
