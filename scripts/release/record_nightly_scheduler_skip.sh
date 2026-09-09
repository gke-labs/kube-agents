#!/usr/bin/env bash
# Records a quiet daily tick in the nightly scheduler's job summary.
#
# The whole point of nightly-scheduler.yml is that a tick with nothing to promote
# leaves no pipeline run behind to be mistaken for a passing one. That makes this
# summary the only trace such a tick leaves, so it says explicitly that a green
# scheduler here reports nothing about the last pipeline run's result.
set -euo pipefail

COMMIT_SHA="${COMMIT_SHA:-}"
RC_TAG="${RC_TAG:-}"
SKIP_REASON="${SKIP_REASON:-}"

render_summary() {
  echo "### No nightly promotion required"
  echo ""
  if [ -n "${SKIP_REASON}" ]; then
    echo "${SKIP_REASON}"
  elif [ -n "${RC_TAG}" ]; then
    echo "The newest validated candidate (\`${RC_TAG}\` / \`${COMMIT_SHA}\`) does not require staging promotion."
  else
    echo "No eligible validated candidate exists to promote."
  fi
  echo ""
  echo "No pipeline run was started. This is the normal quiet-tick outcome and says nothing about the last pipeline run's result."
}

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  render_summary >>"${GITHUB_STEP_SUMMARY}"
else
  render_summary
fi
