#!/usr/bin/env bash
# Picks the candidate the nightly pipeline tests, and decides whether passing it
# should promote anything.
#
# Two decisions, deliberately separate:
#
#   skip_pipeline   there is nothing worth deploying: either no validated
#                   candidate exists at all, or the newest one predates the
#                   shared-pipeline restructure and the workflows would drive it
#                   with scripts and a suite selector its tree does not have.
#   skip_promotion  the candidate is already promoted — a staging_* tag points at
#                   its commit. The night still deploys and tests it; only the tag
#                   push is skipped. That is what makes re-running the pipeline on
#                   the same candidate a no-op rather than a second tag, and it is
#                   why the nightly matrix keeps running on quiet nights.
#
# Every skip is exit 0. The exits that are not: a tag that does not resolve to a
# commit, and a hand-passed tag the RC pipeline never validated or whose tree
# predates the shared-pipeline restructure. Both hand-passed cases fail rather
# than skip, because a caller who named a candidate is owed an answer about that
# candidate rather than a green run that quietly tested nothing.
#
# Selection and the validation check both come from common.sh rather than being
# re-implemented here: a second answer to "is this commit validated" is how the RC
# gate and the promotion gate drift apart.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

RC_TAG="${1:-${RC_TAG:-}}"
# Whether the caller named the candidate or the resolver picked it. The two get
# different treatment when a gate refuses: a resolver that picked a candidate
# nobody asked for skips, while a candidate somebody asked for by name fails.
RC_TAG_WAS_EXPLICIT="false"
if [ -n "${RC_TAG}" ]; then
  RC_TAG_WAS_EXPLICIT="true"
fi

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

  # A candidate that predates the shared-pipeline restructure is skipped whole
  # rather than run against. The workflows would drive it with a suite selector
  # and scripts its tree does not have, and both mismatches are silent: the gate
  # would test the wrong suite and the optional step would contribute nothing,
  # while the run reported a green matrix. Better to build no cluster at all than
  # to publish a result that means something other than it says.
  #
  # A skip when the resolver chose the candidate, an error when a human named it.
  # Nothing is wrong on the resolver's path — there is only nothing yet to do,
  # cleared by the next candidate the RC pipeline validates — and a failed run
  # every three hours for a condition that resolves itself is noise. But someone
  # who passes `rc_tag` asked for that candidate specifically, and answering with
  # a green run in which every later job was skipped tells them it was tested.
  # That is the distinction the header's "only exit 1" rule already draws for an
  # unvalidated hand-passed tag, and tag_staging_promotion.sh draws for the
  # matching trigger check.
  if ! candidate_supports_shared_pipeline "${COMMIT_SHA}"; then
    if [ "${RC_TAG_WAS_EXPLICIT}" = "true" ]; then
      echo "❌ ERROR: Candidate '${RC_TAG}' (${COMMIT_SHA:0:7}) predates a restructure the workflows depend on." >&2
      echo "   Its tree is missing at least one of: the E2E_SUITE selector, run_optional_e2e_suites.sh," >&2
      echo "   reconcile_environment.sh. The matrix would run a suite nobody asked for and report green," >&2
      echo "   or the staging reconcile would abort on a missing file while the promotion tag went out" >&2
      echo "   anyway. Refusing to test it." >&2
      echo "   Omit rc_tag to take the newest eligible candidate instead." >&2
      exit 1
    fi
    SKIP_PIPELINE="true"
    SKIP_PROMOTION="true"
    SKIP_REASON="Candidate '${RC_TAG}' (${COMMIT_SHA:0:7}) predates the shared-pipeline restructure, so its tree does not carry the suite selector and scripts these workflows drive it with. Waiting for the RC pipeline to validate a newer candidate."
    echo "ℹ️ ${SKIP_REASON}" >&2
  else
    existing_staging_tag="$(get_existing_staging_tag "${COMMIT_SHA}")"
    if [ -n "${existing_staging_tag}" ]; then
      SKIP_PROMOTION="true"
      SKIP_REASON="Commit ${COMMIT_SHA:0:7} is already promoted as '${existing_staging_tag}'; the matrix still runs, nothing is tagged."
      echo "ℹ️ ${SKIP_REASON}" >&2
    fi
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
