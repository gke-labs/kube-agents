#!/usr/bin/env bash
# Records a quiet weekly tick in the GA release scheduler's job summary.
#
# The whole point of release-scheduler.yml is that a tick with nothing to release
# leaves no pipeline run behind to be mistaken for a passing one. That makes this
# summary the only trace such a tick leaves, so it says explicitly that a green
# scheduler here reports nothing about the last pipeline run's result.
set -euo pipefail

RELEASE_COMMIT="${RELEASE_COMMIT:-}"
GATE_TAG="${GATE_TAG:-}"
SKIP_REASON="${SKIP_REASON:-}"

render_summary() {
  echo "### No GA release required"
  echo ""
  if [ -n "${SKIP_REASON}" ]; then
    echo "${SKIP_REASON}"
  elif [ -n "${GATE_TAG}" ]; then
    if [ -n "${RELEASE_COMMIT}" ]; then
      echo "The newest candidate (\`${GATE_TAG}\` / \`${RELEASE_COMMIT:0:7}\`) does not require a GA release."
    else
      echo "The newest candidate (\`${GATE_TAG}\`) does not require a GA release."
    fi
  else
    echo "No eligible staging promotion candidate exists to release."
  fi
  echo ""
  echo "No pipeline run was started. This is the normal quiet-tick outcome and says nothing about the last pipeline run's result."
}

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  render_summary >>"${GITHUB_STEP_SUMMARY}"
else
  render_summary
fi
