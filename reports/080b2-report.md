# 0.8.0-b2 approval gate report

## Implementation
- HTTP POST /plans/{id}/approve is disabled: authenticated requests return 403 {"error":"approve_requires_cli"} before body parsing; no approval dispatch. Existing authentication still returns 401 for unauthenticated requests.
- CLI keeps --by human, rejects NODUS_ROLE=worker / NODUS_RUN=1 and requires both stdin/stdout TTY. It displays id, status, review verdict and the first 12 hash characters on stderr, requires the exact plan id, and cancels on mismatch/EOF.
- Confirmation calls local plan.approve(id, 'human'), without starting/contacting the daemon. JSON result remains on stdout.
- Existing plan hash, repository HEAD, AGENTS hash and validator checks are retained without weakening. Existing drift tests cover hash/HEAD/AGENTS/warnings.
- OpenAPI marks HTTP approve deprecated with 403, and CLI/architecture docs explain the boundary. No handoff/B3 implementation changes; no git commit/push/merge.

## Verification
- Targeted tests: tests/test_plan.py and tests/test_openapi.py passed (78 cases before final EOF-message cleanup; final changes included in full suite).
- Real noninteractive child process: python -m sodus plan approve noninteractive-probe --by human, with worker/run markers removed in child only, exited 1 with "approval requires an interactive terminal". In inherited worker context it also exited 1 with worker/run rejection.
- Real ephemeral loopback HTTP server: authenticated POST /plans/manual-probe/approve with malformed JSON returned HTTP 403 {"error":"approve_requires_cli"}.
- HTTP integration tests assert reviewed plan remains unchanged. CLI tests simulate TTY streams and cover both non-TTY streams, both worker markers, wrong confirmation, EOF and successful local approval; daemon/HTTP calls fail the test if attempted.
- git diff --check passed.

## Residual risk
The same local user can spoof a TTY, remove environment markers, read tokens or directly call Python/SQLite. This is not cryptographic identity isolation; it increases friction for unattended/worker automatic approval. TTY confirmation is tested with simulated streams, not a physical human terminal session.

## SUMMARY
PASS: Full-suite rerun `A:\Anaconda\python.exe -m pytest -q -x --tb=short` completed all 1179 tests (100%) with exit code 0; `-x` did not truncate the run because no tests failed. Evidence: `080b2-full-tests-final.txt`.

Initial exact `A:\Anaconda\python.exe -m pytest -q` run exited 1: unrelated `tests/test_ask.py::test_http_auth_and_contract` aborted a loopback unauthenticated POST /ask connection with Windows WinError 10053. All remaining cases passed. The full rerun passed without source changes; initial evidence is preserved in `080b2-full-tests.txt`. No unrelated network/test implementation changes were made.

HTTP 403, noninteractive CLI rejection, simulated TTY confirmation/cancellation and successful local approval are verified. No handoff/B3 changes or git commit. Next: review B4 diff/report and continue the separately scoped B3 integration only when assigned.
