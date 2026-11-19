#!/usr/bin/env bash
# Starts release-scheduler.yml from staging-promotion-pipeline.yml, right after
# the run has pushed its staging_<ts>_<sha> tag.
#
# The scheduler's daily tick reads the tag graph at 06:17 UTC. A promotion that
# tags after that moment used to wait for the next tick, a day: on 2026-11-18
# the tag landed three minutes after the tick and the candidate shipped a day
# late. The run that pushed the tag is the one place that knows the gate moved,
# so it asks the scheduler to evaluate now. Nothing here decides whether to
# release: resolve_scheduled_release.sh does, from the tag graph, as on every
# tick. When the tick and this dispatch overlap a publish still in flight, both
# can dispatch release-publish.yml; what keeps the second from publishing is
# release-publish.yml's own concurrency lock (kube-agents-release-publish, held
# to the end) followed by its re-evaluation under schedule_gate=evaluate, which
# finds no commits left once the first run's GA tag is on the graph. The
# scheduler evaluates on main only, which is why step 6b runs only there. The
# scheduler takes no inputs (release-scheduler.yml), so none are passed.
#
# A failure here is not a missed release: the next tick evaluates the same tag.
# It still exits non-zero with an error annotation, which reds the night's
# promotion run and has release-stage-broken-notify.yml file a stage issue
# naming this step, because the day's release then ships a day late.
#
# GITHUB_TOKEN is sufficient, with `actions: write` on the job: GitHub exempts
# `workflow_dispatch` from the recursion suppression on runs the default token
# starts, which is also what lets the scheduler start release-publish.yml.
set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GITHUB_REF_NAME:?GITHUB_REF_NAME is required}"
STAGING_TAG="${STAGING_TAG:-}"
WORKFLOW_FILE="${WORKFLOW_FILE:-release-scheduler.yml}"

command -v gh >/dev/null 2>&1 || {
  echo "::error title=Missing dependency::gh CLI is required to dispatch ${WORKFLOW_FILE}." >&2
  exit 1
}

# No inputs: the scheduler's workflow_dispatch declares none, and gh refuses
# an input the workflow does not declare.
if ! gh workflow run "${WORKFLOW_FILE}" \
  --repo "${GITHUB_REPOSITORY}" \
  --ref "${GITHUB_REF_NAME}"; then
  echo "::error title=GA release scheduler dispatch failed::Could not dispatch ${WORKFLOW_FILE} after promoting ${STAGING_TAG:-the candidate}. The scheduler's next daily tick evaluates the same tag, so today's release ships a day late unless someone dispatches ${WORKFLOW_FILE} by hand. A 403 here means the job lost its \`actions: write\` permission; a 404 usually means ${WORKFLOW_FILE} is missing from ${GITHUB_REF_NAME} or has no workflow_dispatch trigger." >&2
  exit 1
fi

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### GA release scheduler dispatched"
    echo ""
    echo "| Field | Value |"
    echo "| --- | --- |"
    if [ -n "${STAGING_TAG}" ]; then
      echo "| Gate tag | \`${STAGING_TAG}\` |"
    fi
    echo "| Ref | \`${GITHUB_REF_NAME}\` |"
    echo ""
    echo "The scheduler decides whether this promotion publishes a GA release; its own run summary says what it did."
  } >>"${GITHUB_STEP_SUMMARY}"
fi
