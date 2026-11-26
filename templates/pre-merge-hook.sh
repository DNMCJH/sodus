#!/bin/bash
# Git pre-merge hook for sodus review
# Install: cp templates/pre-merge-hook.sh .git/hooks/pre-merge-commit && chmod +x .git/hooks/pre-merge-commit
#
# This hook runs before git merge completes, triggering a review of the last run.
# If the review fails with critical/high findings, the merge is blocked.

set -e

echo "🔍 Running pre-merge review..."

# Get the most recent run for this project
LATEST_RUN=$(python -m sodus runs | head -2 | tail -1 | awk '{print $1}')

if [ -z "$LATEST_RUN" ]; then
    echo "⚠️  No runs found to review, allowing merge"
    exit 0
fi

echo "Reviewing run: $LATEST_RUN"

# Run review (exit code 2 = review found issues)
python -m sodus review "$LATEST_RUN" --by auto

REVIEW_EXIT=$?

if [ $REVIEW_EXIT -eq 0 ]; then
    echo "✅ Review passed, merge allowed"
    exit 0
elif [ $REVIEW_EXIT -eq 2 ]; then
    echo "❌ Review found issues, merge blocked"
    echo "   Fix the issues and re-run, or use 'git merge --no-verify' to override"
    exit 1
else
    echo "⚠️  Review failed to run, allowing merge (exit code: $REVIEW_EXIT)"
    exit 0
fi
