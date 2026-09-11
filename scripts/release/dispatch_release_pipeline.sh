#!/usr/bin/env bash
# Starts release-publish.yml for an eligible candidate, from release-scheduler.yml.
#
# It is the only scheduled mechanism that starts that pipeline, so a failure here
# means "no GA release is being published" rather than "one run went wrong". It says
# so in an annotation instead of leaving a bare non-zero exit code to interpret.
#
# GITHUB_TOKEN is sufficient here, and the scheduler passes it. GitHub suppresses
# workflow runs triggered by the default token to stop recursion, but exempts
# `workflow_dispatch` and `repository_dispatch` — they always create a run.
set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GITHUB_REF_NAME:?GITHUB_REF_NAME is required}"

RELEASE_COMMIT="${RELEASE_COMMIT:-}"
GATE_TAG="${GATE_TAG:-}"
WORKFLOW_FILE="${WORKFLOW_FILE:-release-publish.yml}"

# Verify required CLI dependencies defensively
command -v gh >/dev/null 2>&1 || {
  echo "::error title=Missing dependency::gh CLI is required to dispatch ${WORKFLOW_FILE}." >&2
  exit 1
}

# Dispatches release-publish.yml with schedule_gate=evaluate.
# Note: target_commit is deliberately omitted because under evaluate mode,
# decide_release_gate.sh explicitly refuses target_commit to ensure the resolver's
# verdict and commit range scan are honoured.
if ! gh workflow run "${WORKFLOW_FILE}" \
  --repo "${GITHUB_REPOSITORY}" \
  --ref "${GITHUB_REF_NAME}" \
  -f "schedule_gate=evaluate"; then
  echo "::error title=GA release pipeline dispatch failed::Could not dispatch ${WORKFLOW_FILE}. No GA release is being published until this succeeds. A 403 here means the job lost its \`actions: write\` permission; a 404 usually means ${WORKFLOW_FILE} is missing from ${GITHUB_REF_NAME} or has no workflow_dispatch trigger." >&2
  exit 1
fi

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### GA release pipeline dispatched"
    echo ""
    echo "| Field | Value |"
    echo "| --- | --- |"
    if [ -n "${RELEASE_COMMIT}" ]; then
      echo "| Commit | \`${RELEASE_COMMIT:0:7}\` |"
    fi
    if [ -n "${GATE_TAG}" ]; then
      echo "| Gate tag | \`${GATE_TAG}\` |"
    fi
    echo "| Mode | \`evaluate\` |"
  } >>"${GITHUB_STEP_SUMMARY}"
fi
