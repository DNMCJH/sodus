# 080d execution report — 0.8.1-exec

## Scope and delivered behavior

- Schema v8 migrates v7 without losing plans/tasks/nodes: plan_node.run_id and
  plan_execution.feedback; existing node inserts now name columns explicitly.
- materialize accepts approved plans, creates tasks in stable DAG order with goal,
  node description, file scope and acceptance commands, persists task ids and is
  idempotent. Database materializing claim plus daemon lock excludes competing writes.
- execute claims executing before dispatch, checks budget before each node, starts
  verified work runs in plan cwd, records run ids and node states, and stops on any
  budget/start/run failure. Successful plans become done; failed plans retain completed
  node evidence. A conditional database claim rejects concurrent execution.
- Startup reconcile marks orphaned planning/executing/materializing plans failed,
  marks running nodes failed, retains done nodes and never resumes work automatically.
- replan creates version+1 with parent_id and persisted feedback, calls the configured
  opus planner with old plan JSON and feedback, and uses the existing completion
  validator. The old plan and approval remain unchanged; new approval is required.
- Added authenticated, recursion-protected POST /plans/{id}/execute and /replan,
  daemon HTTP CLI execute (--route, --wait/--no-wait) and replan (--feedback),
  OpenAPI contracts, CLI/architecture documentation and changelog entry.

## Verification evidence

- Targeted tests: tests/test_plan.py, tests/test_openapi.py, tests/test_070e.py;
  exit code 0, 109 tests; output: 080d-targeted-pytest.txt.
- Coverage includes reversed-source DAG materialization order/idempotence, rejection
  before approval, successful work execution, start/run/budget failures, downstream
  stop, reentrant and asynchronous concurrent request rejection, restart reconciliation,
  replan version/parent/feedback/planner and old-plan immutability, v7-to-v8 migration
  repeated twice with data preservation, HTTP 202/400, CLI dispatch and API schema.
- Initial full-test attempts exposed outdated schema/route assertions (updated) and
  Windows tasklist decoding failures caused by PYTHONUTF8=1 used for edit scripts.
  Acceptance rerun removes that override and uses the requested Python invocation.
- Full acceptance: `A:\Anaconda\python.exe -m pytest -q`, exit code **0**,
  all **1208** collected tests passed. Output: `080d-pytest-acceptance.txt`;
  recorded exit code: `080d-pytest-acceptance-exit.txt`.
- `git diff --check`: exit code 0.
- No real M4-M6 plan was executed or changed. Work runs/planners in new tests use
  fake implementations and temporary databases/repositories. No git commit/push/merge.

## Remaining boundaries

- Execution is serial and stops the entire remaining DAG on failure; no automatic retry.
- Node budget_cny is planning metadata; runtime checks reuse the existing machine and
  runner budget gates, without introducing a separate per-node currency ledger.
- A completed run marks a node done per this brief; verification evidence remains on
  the run and is not equivalent to an independently passing verification verdict.
- A crash between create_task and task_id persistence can leave an unattached task;
  restart fails the interrupted plan rather than automatically repeating materialization.
- Lead owns version bump and final review; M4-M6 still require human approval.

## SUMMARY

Implemented the approved-plan execution pipeline and new-version replanning with
schema v8, HTTP/CLI, docs and regression coverage. Full pytest: 1208 tests, exit
code 0. No real M4-M6 execution and no git commit. Next owner: Lead review and
version bump; retain the manual approval gate for M4-M6.
