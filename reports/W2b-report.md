# W2b implementation report

Date: 2026-11-08
Workspace: `A:\VScode\Code\Projects\nodus-0.6`

## Implementation and contracts

- Added MCP `sodus_ask(request_id, text, cwd?, route?, follow_up_of?)` and `sodus_ask_status(id)` with `nodus_ask` / `nodus_ask_status` aliases sharing the same handlers. All four names appear in tools/list; initialize instructions explain the ask flow and aliases.
- Ask submission forwards POST /ask with origin=mcp and the supplied parameters, using the CLI HTTP client, local Bearer token and configured localhost BASE. NODUS_DEPTH (default 0) is forwarded as X-Sodus-Depth. Successful response JSON is returned unchanged (202 for creation, 200 for an idempotent retry). Polling forwards GET /ask/{id}; HTTP failure text, including HTTP 410 and its original error JSON, becomes an isError tool result without a traceback wrapper.
- Before argument parsing or dispatch, NODUS_ROLE=worker OR NODUS_DEPTH>=1 rejects sodus_ask, nodus_ask, nodus_run and nodus_task_create with recursion_denied. Malformed depth is invalid_depth unless worker role already requires recursion_denied. Polling tools remain available in workers. The rejection performs neither HTTP nor task/run creation.
- Added authenticated GET /asks with optional workspace query parameter (defaults to the server workspace). Returns unexpired records newest first, projected to id/status/created_at/expires_at; status uses the existing ask polling derivation. It does not return question/answer text or mutate records.
- CLI sodus status explicitly passes its current workspace ID to /asks and adds `asks active    open=… running=… done=… failed=…`. The window is all unexpired asks (24 hour TTL), rather than local calendar today: open=queued, running=running+stalled, done=answered+promoted, failed=failed. No read/unread marker is invented.
- Updated docs/mcp.md (tool contracts, aliases, daemon ownership, recursion limitations, status scope) and openapi.yaml (/asks response and filter), with route coverage expectations. README already links to MCP docs without enumerating tools, so needs no factual change.

## Validation

- Target: `A:\Anaconda\python.exe -m pytest -q tests/test_mcp.py tests/test_ask.py tests/test_openapi.py tests/test_cli_surface.py tests/test_cli_rename.py tests/test_cli_watch.py tests/test_client_daemon_state.py -o addopts= --junitxml=reports/W2b-target-tests.xml`
  Result: 81 passed in 6.29s; exit code 0.
- Full: `A:\Anaconda\python.exe -m pytest -q`
  Result: all tests passed; progress reached 100%; exit code 0.
- git diff --check: exit code 0 (Git also reports local CRLF-to-LF normalization notices).

The first full run returned exit code 1 because the old rename test required every tool to start with nodus_. Updated that compatibility assertion to allow exactly sodus_ask and sodus_ask_status while retaining nodus aliases; the expanded target suite passes. Final full rerun reached 100% with exit code 0.

Tests cover tool discovery, alias identity, every blocked tool under role/depth combinations (including worker plus malformed depth), no side effects on rejection, full submission payload and default/explicit depth header, status success and unchanged 410 errors, list authentication/field projection/workspace filtering/expiry, status grouping and OpenAPI route coverage. HTTP tests also exercise MCP submission and 410 polling through the real client against a local ephemeral server; no real provider call is required.

## Boundaries and next handoff

No git commit/push/merge was performed; no dream/W3 implementation or unrelated refactoring was added. Existing untracked reports and operational files were preserved. Ask tools require the HTTP daemon; legacy MCP run ownership remains process-local, as documented. The inherited design limitation remains: a worker deliberately making direct authenticated HTTP calls while omitting the depth header cannot be identified by the HTTP guard. This change does not add session push/writeback or a read acknowledgement model.

Next executor should proceed with the next separately assigned brief; W2b does not authorize dream work.

## SUMMARY

W2b implements MCP ask/poll and aliases, worker recursion rejection, depth forwarding, authenticated ask listing and workspace status counts, OpenAPI/MCP documentation and tests. Target tests: 81 passed, exit code 0; full pytest: 100%, exit code 0. Deliverables include this report and reports/W2b-target-tests.xml. No W2b acceptance blockers remain.
