#!/usr/bin/env bash
# Renders step 1 of the nightly pipeline into the job summary: which candidate
# the run picked, and whether a green matrix will move staging.
#
# The two skips it reports are different things, and conflating them is the
# mistake this exists to make visible. SKIP_PIPELINE means there is no candidate
# to test and the run does nothing at all. SKIP_PROMOTION means the candidate is
# real and the matrix runs, but the commit already carries a staging tag, so a
# pass pushes nothing — the normal outcome once the RC pipeline has a quiet day.
#
# Writes nothing when GITHUB_STEP_SUMMARY is unset, so it is safe to run outside
# Actions.
set -euo pipefail

COMMIT_SHA="${COMMIT_SHA:-}"
RC_TAG="${RC_TAG:-}"
STAGING_TAG="${STAGING_TAG:-}"
SKIP_PIPELINE="${SKIP_PIPELINE:-}"
SKIP_PROMOTION="${SKIP_PROMOTION:-}"
SKIP_REASON="${SKIP_REASON:-}"

render_summary() {
  echo "### Nightly candidate"
  echo ""
  if [ "${SKIP_PIPELINE}" = "true" ]; then
    echo "Nothing to test: ${SKIP_REASON}"
    return
  fi

  echo "| Field | Value |"
  echo "| --- | --- |"
  echo "| Candidate | \`${RC_TAG}\` |"
  echo "| Commit | \`${COMMIT_SHA}\` |"
  echo "| Staging tag | \`${STAGING_TAG}\` |"
  if [ "${SKIP_PROMOTION}" = "true" ]; then
    echo "| Promotes | no — already promoted |"
  else
    echo "| Promotes | yes, if the matrix passes |"
  fi
  if [ -n "${SKIP_REASON}" ]; then
    echo ""
    echo "${SKIP_REASON}"
  fi
}

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  render_summary >>"${GITHUB_STEP_SUMMARY}"
else
  render_summary
fi
