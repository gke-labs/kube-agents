#!/usr/bin/env bash
# Starts nightly-pipeline.yml for a resolved candidate, from nightly-scheduler.yml.
#
# It is the only scheduled mechanism that starts that pipeline, so a failure here means
# "no nightly candidate is being tested for staging promotion" rather than "one run went wrong".
# It says so in an annotation instead of leaving a bare non-zero exit to interpret.
#
# GITHUB_TOKEN is enough here, and the scheduler passes it. GitHub suppresses
# workflow runs triggered by the default token to stop recursion, but names
# `workflow_dispatch` and `repository_dispatch` as the two exempt events — they
# always create a run.
set -euo pipefail

: "${COMMIT_SHA:?COMMIT_SHA is required}"
: "${RC_TAG:?RC_TAG is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GITHUB_REF_NAME:?GITHUB_REF_NAME is required}"

if ! command -v gh >/dev/null 2>&1; then
  echo "::error title=Nightly pipeline dispatch failed::gh CLI is required but not installed or not in PATH." >&2
  exit 1
fi

WORKFLOW_FILE="${WORKFLOW_FILE:-nightly-pipeline.yml}"

if ! gh workflow run "${WORKFLOW_FILE}" \
  --repo "${GITHUB_REPOSITORY}" \
  --ref "${GITHUB_REF_NAME}" \
  -f "rc_tag=${RC_TAG}"; then
  echo "::error title=Nightly pipeline dispatch failed::Could not dispatch ${WORKFLOW_FILE} for ${RC_TAG} (${COMMIT_SHA}). No nightly candidate is being tested until this succeeds. A 403 here means the job lost its \`actions: write\` permission; a 404 usually means ${WORKFLOW_FILE} is missing from ${GITHUB_REF_NAME} or has no workflow_dispatch trigger." >&2
  exit 1
fi

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### Nightly pipeline dispatched"
    echo ""
    echo "| Field | Value |"
    echo "| --- | --- |"
    echo "| Commit | \`${COMMIT_SHA}\` |"
    echo "| Candidate tag | \`${RC_TAG}\` |"
  } >>"${GITHUB_STEP_SUMMARY}"
fi
