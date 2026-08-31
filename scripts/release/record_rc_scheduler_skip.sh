#!/usr/bin/env bash
# Records a quiet three-hourly tick in the scheduler's job summary.
#
# The whole point of rc-scheduler.yml is that a tick with nothing to do leaves
# no pipeline run behind to be mistaken for a passing one. That makes this
# summary the only trace such a tick leaves, so it says explicitly that a green
# scheduler here reports nothing about the last pipeline run's result.
set -euo pipefail

COMMIT_SHA="${COMMIT_SHA:-}"

render_summary() {
  echo "### No new release candidate"
  echo ""
  echo "The newest built commit (\`${COMMIT_SHA}\`) has already been attempted or"
  echo "validated, so no pipeline run was started. This is the normal quiet-tick"
  echo "outcome and says nothing about the last pipeline run's result."
}

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  render_summary >>"${GITHUB_STEP_SUMMARY}"
else
  render_summary
fi
