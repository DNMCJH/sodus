# 0.7.0-b execution report

## Implementation

- Added authenticated, read-only `GET /state` in `nodus/state.py`. Fixed fields:
  version/workspace_id/now/running/counts/today/asks/memory.
- Live runs are queued/running/stalled, descending started_at, limited to 20;
  running/queued counters remain uncapped. Asks are queried per request.
- Only local-day ledger totals, open proposal count and pending database memory
  count use a locked 1.5-second TTL; cache scope includes home/workspace/day/project.
- Today uses the same run + observation rows and ledger parsing as `/cost/today`
  (`nodus_cost_today`), sums CNY without FX and exposes `unpriced` run count.
  No per-agent expansion, git probes or full board rendering.
- `needs_you` preserves the board's 50-work-run spend decisions and maximum 50
  proposal entries; `unverifiable` is the existing section count (0 or 1).
- Moved board projections, wording, evidence and HTML entry into `nodus/board.py`.
  The template `nodus/ui.py` is unchanged. Python compatibility entry points,
  GET /board, SSE, pet/statuslight and cost endpoint readers are retained.
- Updated OpenAPI and architecture documentation. Added state schema/auth/live
  counts/ledger/TTL/isolation/latency tests and exact board top-level key assertion.

## Validation

- Targeted state/board/OpenAPI suite: exit 0; OpenAPI after final schema edit: 9 passed, exit 0.
- Actual local HTTP measurement: **p95 = 31.663 ms** (full-suite run; targeted run: 36.573 ms), 20 serial authenticated
  GET /state requests after one excluded warm-up, 30 seeded mixed work runs.
  Threshold: 200 ms. Measured endpoint includes HTTP serialization and authentication.
- Board exact keys: now, day, profile, running, decisions, decision_counts,
  unverified_runs, needs_you, handling, stopped, unverifiable, auto_recovering,
  deterministic, history_failures, memory_candidates, terminals, today.
- Full `A:\Anaconda\python.exe -m pytest -q`: **exit code 0**, reached 100%; see `reports/070b-pytest.log`.
- `git diff --check`: exit code 0.
- Equivalent `wc -l` via Python splitlines: server.py **2767 -> 813**,
  reduction **1954 lines** (required >=300).

## Diff

`git diff --stat` (tracked files; new files remain untracked because no git add/commit):

```text
 docs/architecture.md  |   22 +
 nodus/server.py       | 1988 +------------------------------------------------
 openapi.yaml          |   78 ++
 tests/test_board.py   |    6 +-
 tests/test_openapi.py |   12 +
 5 files changed, 134 insertions(+), 1972 deletions(-)
```

New implementation/test files:
- `nodus/board.py`: 1990 lines
- `nodus/state.py`: 60 lines
- `tests/test_state.py`: 130 lines

The new report and full pytest log are also untracked task outputs.
Pre-existing untracked files were left intact. No git commit/push/merge.

## Scope notes

Memory counts cover database proposed/retire_proposed rows in the current workspace
or saved project; adopted rows and filesystem harvest are excluded. Asks use
queued -> open and answered -> done, counting retained records without expiring or
mutating them. Slow counters may lag writes by at most the TTL; running and asks
remain fresh. The latency number is a local test measurement, not a production SLA.

## SUMMARY

Implemented /state and extracted board presentation; preserved board keys and HTML.
All acceptance checks passed, including full pytest exit 0 and HTTP p95 <200ms. Next executor can review
the uncommitted diff and continue the next 0.7 milestone; no publication was performed.
