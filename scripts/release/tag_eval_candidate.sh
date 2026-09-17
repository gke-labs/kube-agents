#!/usr/bin/env bash
# Nominates a validated Release Candidate commit for staging by tagging it
# evalcand_<ts>_<sha>.
#
# The tag is the eval trigger: post-kube-agents-eval-rc in
# GoogleCloudPlatform/oss-test-infra starts on a push of a tag matching
# ^evalcand_[0-9]{10}_[0-9a-f]{7}$ and runs the full eval catalog against the
# candidate's published images. It is not a deploy trigger — nothing in this
# repository reads the evalcand_ family, and the staging_ tag that IS a deploy
# trigger is pushed later, by tag_staging_promotion.sh, and only if the eval
# comes back green.
#
# The guards below are therefore the same ones tag_staging_promotion.sh applies,
# less the ones about deploying. A nomination is not free: the eval takes hours
# and holds one project out of a pool shared with the merge-blocking presubmit,
# so a tag composed by hand or pointed at an unvalidated commit costs real
# capacity for a measurement nobody can use.
#
# It must be pushed with the dedicated GitHub App token (kube-agents-release-bot)
# for the same reason the staging tag is: a tag pushed with the default
# GITHUB_TOKEN triggers no workflow. Prow reads the push through a webhook rather
# than through Actions, so that particular suppression does not apply to it — but
# the App token also bypasses the tag-protection rulesets, which does.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

COMMIT_SHA="${1:-${COMMIT_SHA:-}}"
RC_TAG="${2:-${RC_TAG:-}}"
EVALCAND_TAG="${3:-${EVALCAND_TAG:-}}"

if [ -z "${COMMIT_SHA}" ] || [ -z "${RC_TAG}" ]; then
  echo "❌ ERROR: COMMIT_SHA and RC_TAG are required." >&2
  echo "Usage: $0 <commit-sha> <rc-validated-tag> [evalcand-tag]" >&2
  exit 1
fi

# Derive rather than trust, as tag_staging_promotion.sh does. A caller may pass
# the tag explicitly, but it has to be the one this candidate maps to: the eval's
# verdict is recorded against the commit and read back by the poller, so a tag
# naming a different candidate would put the right verdict under the wrong name.
DERIVED_EVALCAND_TAG="$(evalcand_tag_for_rc "${RC_TAG}")"
if [ -z "${EVALCAND_TAG}" ]; then
  EVALCAND_TAG="${DERIVED_EVALCAND_TAG}"
elif [ "${EVALCAND_TAG}" != "${DERIVED_EVALCAND_TAG}" ]; then
  echo "❌ ERROR: eval-candidate tag '${EVALCAND_TAG}' does not match the tag derived from '${RC_TAG}' ('${DERIVED_EVALCAND_TAG}')." >&2
  exit 1
fi

# The same gate tag_staging_promotion.sh applies, on the commit rather than the
# tag. COMMIT_SHA and RC_TAG are independent arguments, so every check above can
# pass while COMMIT_SHA points somewhere else.
if ! is_rc_candidate_commit_already_validated "${COMMIT_SHA}"; then
  echo "❌ ERROR: commit ${COMMIT_SHA} carries no rc_*_validated tag; refusing to nominate it for the release-candidate eval." >&2
  echo "   Only candidates the RC pipeline validated can be nominated." >&2
  exit 1
fi

# Namespace guard, kept even though the value was derived a line ago: this is the
# last point before a tag that spends hours of a shared project pool is pushed,
# and a future caller passing EVALCAND_TAG in the environment reaches here too.
case "${EVALCAND_TAG}" in
  "${EVALCAND_TAG_PREFIX}"?*) ;;
  *)
    echo "❌ ERROR: refusing to push '${EVALCAND_TAG}': an eval-candidate tag must start with '${EVALCAND_TAG_PREFIX}'." >&2
    exit 1
    ;;
esac

# Shape and not just the prefix, because the shape is what the eval job's
# `branches` regex matches. A tag that clears the prefix guard but not the shape
# fires nothing, and the pipeline would then wait out its whole poll deadline on
# a job that was never going to start.
if ! grep -qE "${EVALCAND_TAG_SHAPE_REGEX}" <<<"${EVALCAND_TAG}"; then
  echo "❌ ERROR: refusing to push '${EVALCAND_TAG}': it does not match the shape the eval job triggers on (${EVALCAND_TAG_SHAPE_REGEX})." >&2
  exit 1
fi

# The staging trigger check, applied here rather than only at promotion time.
# tag_staging_promotion.sh refuses a commit whose tree declares a staging trigger
# the flat staging_<ts>_<sha> tag does not match, and that refusal has not moved.
# What has moved is when it is worth discovering: a candidate that will be
# refused at promotion is a candidate whose eval is hours of a leased project
# spent on a verdict that cannot be acted on.
if ! staging_trigger_matches_at_commit "${COMMIT_SHA}" "$(staging_tag_for_rc "${RC_TAG}")"; then
  echo "::error title=Candidate predates the staging_* trigger::Commit ${COMMIT_SHA} declares a staging-redeploy trigger that its staging_ tag would not match, so even a green eval could not be promoted. Refusing to nominate it and spend an eval on it. Promote a candidate validated after the trigger rename; the RC pipeline produces one every three hours." >&2
  echo "==> Refusing to nominate ${COMMIT_SHA}: its staging-redeploy trigger does not match the tag it would be promoted under." >&2
  exit 1
fi

exec "${SCRIPT_DIR}/tag_commit.sh" \
  --title "NOMINATING VALIDATED CANDIDATE FOR THE RELEASE-CANDIDATE EVAL" \
  --detail "Source RC Tag: ${RC_TAG}" \
  "${EVALCAND_TAG}" "${COMMIT_SHA}" "Eval nomination of ${RC_TAG} (commit ${COMMIT_SHA})"
