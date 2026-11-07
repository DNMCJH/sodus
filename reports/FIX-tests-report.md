# FIX tests report

- H1: Added fenced JSON/schema validation tests and invalid extraction failure/replan regression in `tests/test_dream_engine.py`. During testing, a real defect prevented failed batches from being retried because of the unique dream range; `nodus/dream.py` now resets a failed batch for the same cursor before retry.
- H2: Added recovery journal tests covering hash==before, hash==after, and third-party modification branches, each repeated for idempotency, in `tests/test_recovery.py`.
- M1: Added failed-run to failed-ask terminal-state regression in `tests/test_ask.py`.
- M2: Added staged + harvest merge and text deduplication regression in `tests/test_w3c.py`.
- M3: Added default workspace candidate endpoint and `candidate_evidence` evidence-count regression in `tests/test_ask.py`.

Target command:
`A:\\Anaconda\\python.exe -m pytest -q tests/test_dream_engine.py tests/test_recovery.py tests/test_ask.py tests/test_w3c.py`
Result: PASS (all tests passed).

Full command:
`A:\\Anaconda\\python.exe -m pytest -q`
Result: PASS (all tests passed, exit code 0).

## SUMMARY
H1/H2/M1/M2/M3 each have regression coverage; target and full pytest both pass with exit code 0. No git commit was created.
