# Phase 3: Review Triggers (审查触发器)

Phase 3 adds automated review triggers to catch issues early:

## 1. Periodic Audit (定期巡检) ✅

**Command:** `sodus audit`

Batch review multiple runs based on criteria.

### Usage Examples

```bash
# Review specific runs
sodus audit run-abc123 run-def456

# Review all runs from last 7 days
sodus audit --since 7d

# Review all runs for a specific task
sodus audit --task task-xyz789

# Review only failed runs
sodus audit --failed-only

# Use a specific reviewer
sodus audit --since 7d --by dsh
```

### Features

- Query runs by time (--since), task (--task), or status (--failed-only)
- Batch review multiple runs in one command
- Aggregate summary with finding counts by severity
- Highlights critical/high severity findings
- Exit code reflects overall audit result (0 = all passed, 1 = issues found)

---

## 2. Pre-merge Hook (merge 前触发) ✅

**Template:** `templates/pre-merge-hook.sh`

Git hook that reviews the last run before allowing a merge.

### Installation

```bash
# Copy template to git hooks directory
cp templates/pre-merge-hook.sh .git/hooks/pre-merge-commit
chmod +x .git/hooks/pre-merge-commit
```

### How It Works

1. When `git merge` is executed, the hook runs before the merge completes
2. Gets the most recent run for the project
3. Triggers `sodus review` on that run
4. If review finds critical/high issues, blocks the merge
5. Developer must fix issues or use `--no-verify` to override

### Exit Codes

- 0: Review passed, merge allowed
- 1: Review found issues, merge blocked
- 2: Review error, merge allowed (fail-open)

---

## 3. CI Integration (CI 失败后触发) ✅

**Template:** `templates/github-actions-review.yml`

GitHub Actions workflow that triggers review when CI fails.

### Installation

```bash
# Copy template to GitHub workflows directory
cp templates/github-actions-review.yml .github/workflows/review-on-failure.yml
```

### How It Works

1. Monitors your CI/test workflows
2. When a workflow fails, automatically triggers
3. Finds the most recent failed run
4. Reviews it with focus "CI failure analysis"
5. Uploads review results as artifacts
6. (Optional) Posts review summary as PR comment

### Customization

- Edit `workflows:` list to match your CI workflow names
- Adjust Python version if needed
- Uncomment PR comment section to post results on PRs
- Add Slack/email notifications if desired

### Alternative: Manual Trigger

Uncomment the `workflow_dispatch` section to allow manual review triggering via GitHub UI.

---

## Implementation Summary

**Phase 3 delivers:**
- 1 new CLI command (`audit`) with 5 options
- 2 integration templates (git hook + CI)
- Batch review capability
- Automated quality gates

**Files changed:**
- `sodus/cli/commands/pipeline.py` - Added audit_cmd function (~100 lines)
- `sodus/cli/__init__.py` - Added "audit" to TOP_LEVEL
- `templates/pre-merge-hook.sh` - Git hook template
- `templates/github-actions-review.yml` - CI integration template

**Usage:**
```bash
# Daily audit
sodus audit --since 1d

# Install git hook
cp templates/pre-merge-hook.sh .git/hooks/pre-merge-commit && chmod +x .git/hooks/pre-merge-commit

# Setup CI integration
cp templates/github-actions-review.yml .github/workflows/
```

---

## Next Steps

1. Test audit command: `sodus audit --since 7d`
2. Install git hook for projects that need merge gates
3. Add CI integration to repositories with automated testing
4. Consider scheduling daily/weekly audits via system cron:
   ```bash
   # Daily audit at 9am
   0 9 * * * cd /path/to/project && python -m sodus audit --since 1d
   ```

For questions or issues, see main HANDOFF.md or consult coordinator evaluation reports.
