#!/usr/bin/env bash
# Picks the candidate the nightly pipeline tests, and decides whether passing it
# should promote anything.
#
# Two decisions, deliberately separate:
#
#   skip_pipeline   there is no validated candidate to test at all, so the run has
#                   nothing to deploy. Rare: it means the RC pipeline has never
#                   produced an rc_*_validated tag.
#   skip_promotion  the candidate is already promoted — a staging_* tag points at
#                   its commit. The night still deploys and tests it; only the tag
#                   push is skipped. That is what makes re-running the pipeline on
#                   the same candidate a no-op rather than a second tag, and it is
#                   why the nightly matrix keeps running on quiet nights.
#
# Every skip is exit 0. The only exit 1 is a tag that does not resolve to a
# commit, or a hand-passed tag that the RC pipeline never validated.
#
# Selection and the validation check both come from common.sh rather than being
# re-implemented here: a second answer to "is this commit validated" is how the RC
# gate and the promotion gate drift apart.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

RC_TAG="${1:-${RC_TAG:-}}"

COMMIT_SHA=""
STAGING_TAG=""
SKIP_PIPELINE="false"
SKIP_PROMOTION="false"
SKIP_REASON=""

# Tags are the whole input, and a shallow or tagless checkout would silently
# resolve "no candidate" rather than fail.
release_fetch_tags

if [ -z "${RC_TAG}" ]; then
  RC_TAG="$(get_latest_validated_rc_tag)"
fi

if [ -z "${RC_TAG}" ]; then
  SKIP_PIPELINE="true"
  SKIP_PROMOTION="true"
  SKIP_REASON="No rc_*_validated tag exists, so there is no candidate to deploy."
  echo "ℹ️ ${SKIP_REASON}" >&2
else
  if ! COMMIT_SHA="$(git rev-parse --verify "${RC_TAG}^{commit}" 2>/dev/null)"; then
    echo "❌ ERROR: Cannot resolve a commit for candidate tag '${RC_TAG}'." >&2
    exit 1
  fi

  # A hand-passed tag gets the same gate as a resolved one. Without this, a
  # dispatch could name any rc_* tag — including one whose E2E run failed — and
  # the pipeline would promote it on a passing nightly.
  if ! is_rc_candidate_commit_already_validated "${COMMIT_SHA}"; then
    echo "❌ ERROR: Commit ${COMMIT_SHA:0:7} (from '${RC_TAG}') carries no rc_*_validated tag." >&2
    echo "   Only candidates the RC pipeline validated can be promoted to staging." >&2
    exit 1
  fi

  STAGING_TAG="$(staging_tag_for_rc "${RC_TAG}")"

  existing_staging_tag="$(get_existing_staging_tag "${COMMIT_SHA}")"
  if [ -n "${existing_staging_tag}" ]; then
    SKIP_PROMOTION="true"
    SKIP_REASON="Commit ${COMMIT_SHA:0:7} is already promoted as '${existing_staging_tag}'; the matrix still runs, nothing is tagged."
    echo "ℹ️ ${SKIP_REASON}" >&2
  fi
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "commit_sha=${COMMIT_SHA}"
    echo "rc_tag=${RC_TAG}"
    echo "staging_tag=${STAGING_TAG}"
    echo "skip_pipeline=${SKIP_PIPELINE}"
    echo "skip_promotion=${SKIP_PROMOTION}"
    echo "skip_reason=${SKIP_REASON}"
  } >> "${GITHUB_OUTPUT}"
fi

echo "======================================================================"
echo "🌙 RESOLVED NIGHTLY PROMOTION CANDIDATE"
echo "Candidate RC Tag:   ${RC_TAG:-<none>}"
echo "Commit SHA:         ${COMMIT_SHA:-<none>}"
echo "Staging Tag:        ${STAGING_TAG:-<none>}"
echo "Skip Pipeline:      ${SKIP_PIPELINE}"
echo "Skip Promotion:     ${SKIP_PROMOTION}"
if [ -n "${SKIP_REASON}" ]; then
  echo "Reason:             ${SKIP_REASON}"
fi
echo "======================================================================"
