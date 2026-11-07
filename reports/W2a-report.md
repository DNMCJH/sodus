# W2a implementation report

Date: 2026-11-08
Workspace: `A:\VScode\Code\Projects\nodus-0.6`

## Implementation

- Added additive schema v4 migration after v3: all 22 specified ask columns, unique `(workspace_id, request_id)` index, and a minimal `ask_expired` id/workspace tombstone table so swept ids still return HTTP 410.
- Added `ask_create`, `ask_get`, `ask_get_by_request`, `ask_update`, transactional/idempotent `ask_promote`, and `ask_sweep` DB APIs. Concurrent insertion arbitration uses the unique index and catches `IntegrityError`; expired request ids can be reused. Serialized connection initialization fixes concurrent first-use WAL/schema initialization races exposed by the new concurrency test.
- Added authenticated `POST /ask`, `GET /ask/{id}`, and `POST /ask/{id}/promote`. Canonical UTF-8 JSON hashing covers text/cwd/follow_up_of/route; identical retries return 200, conflicts return 409 with original id/hash, new requests return 202, expiry returns 410, and promotion requires acceptance and returns the same task id on retries.
- Added `nodus/ask.py`: background execution uses normal runner routes, budget and ledger with `kind='ask'`; includes prior question/answer as read-only context, persists final assistant text, cost and errors, and derives polling state through `lifecycle.ask_state`.
- Git execution uses `propose.prepare` and `propose.discard`, preserves a requested subdirectory inside the temporary tree, detects staged/untracked/worker-committed changes against the base, records `wrote_files`, and discards changes in `finally`. To honor the explicit no-commit instruction, it uses the add/diff collection portion directly rather than calling `propose.collect`, whose existing implementation unconditionally commits dirty changes. No proposal/patch is retained and the main tree is never the worker cwd. Non-git execution uses the runner's existing ADR-012 write lock and detects touched files.
- `agents.build_env` now sets `NODUS_ROLE=worker` and `NODUS_DEPTH=parent+1`, only in process environments. HTTP rejects `X-Sodus-Depth >= 1` with 403 `recursion_denied` and rejects malformed depth values with 400.
- Updated OpenAPI operations, request/response schemas, error responses and route coverage expectations. Updated prior migration tests to expect the current schema version 4.

## Validation

1. Target command:
   `A:\Anaconda\python.exe -m pytest -q tests/test_ask.py tests/test_openapi.py tests/test_agents.py tests/test_memory_staging.py tests/test_lifecycle.py -o addopts= --junitxml=reports/W2a-target-tests.xml`
   Result: **153 passed in 11.63s; exit code 0**.
2. Full command:
   `A:\Anaconda\python.exe -m pytest -q`
   Result: **all tests passed, progress reached 100%; exit code 0**.
3. `git diff --check`: exit code 0.

New tests cover concurrent idempotency and promotion, conflicting payloads, TTL before/after sweep and request-id reuse, workspace isolation, schema v3-to-v4/reconnect, HTTP authentication and status codes, recursive calls, worker environment, background answer/cost persistence, follow-up context, git change detection/cleanup and budget failure cleanup. Real agent execution and git operations in background unit tests are monkeypatched; existing runner/proposal tests also pass in the full suite. No live provider/model call was made.

## Boundaries and follow-up

- No git commit/push/merge was performed. No MCP, CLI ask/status or dream implementation was added; unrelated pre-existing untracked files were preserved.
- Depth enforcement retains the documented design limitation: a worker directly invoking HTTP with a token and deliberately omitting the depth header cannot be identified by this HTTP guard. W2b should add the MCP-side role/depth rejection and forwarding.
- Expired-id tombstones intentionally retain only ids and workspace ids to preserve 410 after sweeping; ask answer text is removed by sweep and remains in run artifacts. `ask_sweep` is available as a DB cleanup API; no new timer/scheduler is introduced in W2a.
- Next: W2b MCP thin wrappers, worker-side rejection, and CLI/status integration under its own brief.

## SUMMARY

W2a implemented schema v4, ask DB APIs, authenticated HTTP entry/poll/promote, isolated asynchronous answer runs, recursion protection, OpenAPI and tests. Target tests: 153 passed, exit 0; full pytest: exit 0. Deliverables include this report and `reports/W2a-target-tests.xml`; MCP/dream remain out of scope and there are no W2a acceptance blockers.
