#!/usr/bin/env bash
# Deletes an eval-candidate tag whose eval never returned a verdict, so the
# candidate can be nominated again tomorrow.
#
# WHY THIS EXISTS. The evalcand_ tag means "this candidate has been answered
# about", and resolve_promotion_candidate.sh reads it that way: a commit carrying
# one is skipped by every later nightly. That is right for a candidate the eval
# judged — a red verdict must not be re-measured nightly at hours of a leased
# project each time — and wrong for one whose lane broke before it reached a
# verdict. A Boskos project that never leased, a killed pod, a deploy that failed,
# a tag whose push fired nothing: none of those say anything about the candidate,
# but each leaves the tag behind, and the tag is what makes the commit
# ineligible forever after.
#
# Deleting it is the retry. There is no second attempt to make: the next nightly
# resolves the same candidate, finds no tag, and nominates it cleanly, which is
# also why this needs no retry counter or attempt suffix.
#
# The tag name is derived from the RC tag and is stable across attempts, so every
# attempt's build lands in one archive at one commit, all of them matching on the
# commit alone. Which of them answers is poll_rc_eval_verdict.py's problem and it
# does not solve it by reading the newest: Prow takes a minute or two to upload
# the new build's started.json, so during that window the newest build at the
# commit is the PREVIOUS attempt's, already finished. The poller is passed the
# second the nomination was pushed (`--not-before`) and skips anything older,
# which is what makes a re-nomination a retry rather than an echo of the attempt
# this script just withdrew. Renaming or restamping the tag here would break that
# pairing, not help it.
#
# WHAT IT MUST NOT DO is delete a tag for a settled verdict, which would put a
# rejected candidate back in the queue every night. That decision is the
# workflow's — staging-promotion-pipeline.yml runs this only when the poller reported
# `settled=false` — and this script does not second-guess it. What it does
# enforce is that the thing being deleted is an evalcand_ tag of the right shape,
# so a wrong or stale argument cannot reach the staging_ or rc_ families, and —
# when the tag is in this clone — that it points at the expected commit. The
# shape guard holds on every path; the commit check cannot, because the one path
# that reaches the remote without a local ref has no ref to resolve. The comment
# at that branch says so where it happens.
#
# A tag that is not there is success, not an error. The nomination step no-ops
# when the tag already exists, and step 4 of the pipeline can be skipped
# altogether, so the common case for this script on a broken run is that there is
# nothing to delete. "Not there" is checked against the remote and not only
# against the local clone, because release_fetch_tags ends in `|| true`: a fetch
# that failed leaves a clone that knows about no tags at all, and every tag then
# reads as already-withdrawn. That reads as success on the run whose whole job was
# to withdraw one.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

# The remote ensure_git_tag pushes to, so the one the deletion has to reach.
readonly DEFAULT_REMOTE="origin"

COMMIT_SHA="${1:-${COMMIT_SHA:-}}"
EVALCAND_TAG="${2:-${EVALCAND_TAG:-}}"

if [ -z "${COMMIT_SHA}" ] || [ -z "${EVALCAND_TAG}" ]; then
  echo "❌ ERROR: COMMIT_SHA and EVALCAND_TAG are required." >&2
  echo "Usage: $0 <commit-sha> <evalcand-tag>" >&2
  exit 1
fi

# Shape, not prefix, and for a stronger reason than at nomination. There the
# shape guard stops a tag that would fire nothing; here it is the only thing
# standing between a mis-set environment variable and `git push --delete` against
# a tag family that deploys. `staging_1234567890_abc1234` clears no part of this.
if ! grep -qE "${EVALCAND_TAG_SHAPE_REGEX}" <<<"${EVALCAND_TAG}"; then
  echo "❌ ERROR: refusing to delete '${EVALCAND_TAG}': it is not an eval-candidate tag (${EVALCAND_TAG_SHAPE_REGEX})." >&2
  exit 1
fi

release_fetch_tags

TARGET_FULL_SHA="$(git rev-parse --verify "${COMMIT_SHA}^{commit}" 2>/dev/null || echo "${COMMIT_SHA}")"

if ! EXISTING_SHA="$(git rev-parse --verify "refs/tags/${EVALCAND_TAG}^{commit}" 2>/dev/null)"; then
  # Absent locally. Before calling that success, ask the remote — the clone may
  # simply not know, since release_fetch_tags swallows its own failure. A
  # ls-remote that itself fails is not evidence of absence either, so it is an
  # error rather than a third reading of "probably gone".
  if ! REMOTE_REFS="$(git ls-remote --tags "${DEFAULT_REMOTE}" "refs/tags/${EVALCAND_TAG}" 2>&1)"; then
    echo "❌ ERROR: '${EVALCAND_TAG}' is absent from this clone and the remote could not be asked: ${REMOTE_REFS}" >&2
    echo "Refusing to report a withdrawal that may not have happened." >&2
    exit 1
  fi
  if [ -z "${REMOTE_REFS}" ]; then
    echo "✅ No tag '${EVALCAND_TAG}' to delete; the candidate is already eligible for renomination."
    exit 0
  fi
  # It is on the remote and not here, so the fetch is what failed. Delete it by
  # name: the commit-match guard below cannot run without a local ref, and the
  # shape guard above has already bounded what the name can be.
  echo "⚠️ '${EVALCAND_TAG}' is on the remote but not in this clone; the tag fetch failed. Deleting it by name." >&2
  EXISTING_SHA=""
fi

# The tag exists but names a different commit, which this script cannot have
# created: evalcand_ tags carry the commit's own short SHA, so the same name at
# two commits means someone composed one by hand. Deleting it would destroy a
# nomination nobody here made, and the candidate this run cares about was never
# tagged anyway.
if [ -n "${EXISTING_SHA}" ] && [ "${EXISTING_SHA}" != "${TARGET_FULL_SHA}" ]; then
  echo "❌ ERROR: tag '${EVALCAND_TAG}' points at ${EXISTING_SHA}, not at ${TARGET_FULL_SHA}; refusing to delete it." >&2
  exit 1
fi

echo "======================================================================"
echo "🗑️ DROPPING AN EVAL NOMINATION THAT RETURNED NO VERDICT"
echo "Tag:          ${EVALCAND_TAG}"
echo "Commit SHA:   ${TARGET_FULL_SHA}"
echo "Effect:       the next nightly may nominate this candidate again."
echo "======================================================================"

if [ -n "${EXISTING_SHA}" ]; then
  git tag --delete "${EVALCAND_TAG}"
fi

# Same guard ensure_git_tag applies to the push: the local delete above is all a
# developer running this by hand should get.
if ! is_ci_pipeline; then
  echo "⚠️ [Local Execution] Dry-run: tag '${EVALCAND_TAG}' deleted locally. Remote delete skipped (runs only in CI)."
  exit 0
fi

TARGET_REPO="$(get_target_repo)"

if PUSH_ERR=$(git push --delete "${DEFAULT_REMOTE}" "${EVALCAND_TAG}" 2>&1); then
  echo "✅ Tag '${EVALCAND_TAG}' deleted from the remote repository (${TARGET_REPO})."
elif PUSH_ERR=$(git push --delete "https://github.com/${TARGET_REPO}.git" "${EVALCAND_TAG}" 2>&1); then
  echo "✅ Tag '${EVALCAND_TAG}' deleted from the remote repository (${TARGET_REPO})."
else
  # Fatal, and the difference it makes is whether anyone finds out. The retry
  # this script exists to enable did not happen: the tag is still on the remote,
  # so every later nightly skips the candidate and the commit is stranded until
  # a human deletes the tag. An annotation on a green job is not how that gets
  # noticed — the run it is attached to is already red from step 5, so the
  # warning sits among the ones explaining that, and the job that was supposed
  # to clean up reports success.
  #
  # Reding the job costs nothing it was protecting. Step 5b gates nothing: step 6
  # does not depend on it, and step 5 has already failed, so the pipeline's
  # verdict is red either way. What changes is that the failure names the thing
  # that needs a hand.
  echo "::error title=Eval nomination not withdrawn::Could not delete '${EVALCAND_TAG}' from ${TARGET_REPO}: ${PUSH_ERR}. The candidate stays nominated and no later nightly will retry it until the tag is deleted by hand." >&2
  echo "❌ ERROR: could not delete '${EVALCAND_TAG}' from the remote repository (${TARGET_REPO}): ${PUSH_ERR}" >&2
  exit 1
fi
