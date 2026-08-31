#!/usr/bin/env bash
# Promotes a validated Release Candidate commit to staging by tagging it staging_<ts>_<sha>.
#
# The tag is the deploy trigger: staging-redeploy-{agent,controller,integrations}.yml
# start on `push: tags: staging_*` and deploy github.sha, so a tag pushed here is a
# staging deploy. Two consequences the guards below exist for — the tag must be
# derived from a real validated candidate rather than composed by hand, and it must
# carry the staging_ prefix exactly.
#
# It must also be pushed with a PAT (RELEASE_BOT_TOKEN). A tag pushed with the
# default GITHUB_TOKEN triggers no workflow, so the promotion would go green and
# deploy nothing.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

COMMIT_SHA="${1:-${COMMIT_SHA:-}}"
RC_TAG="${2:-${RC_TAG:-}}"
STAGING_TAG="${3:-${STAGING_TAG:-}}"

if [ -z "${COMMIT_SHA}" ] || [ -z "${RC_TAG}" ]; then
  echo "❌ ERROR: COMMIT_SHA and RC_TAG are required." >&2
  echo "Usage: $0 <commit-sha> <rc-validated-tag> [staging-tag]" >&2
  exit 1
fi

# Derive rather than trust. A caller may pass the staging tag explicitly, but it
# has to be the one this candidate maps to — anything else would promote a commit
# under a name that reads back to a different candidate.
DERIVED_STAGING_TAG="$(staging_tag_for_rc "${RC_TAG}")"
if [ -z "${STAGING_TAG}" ]; then
  STAGING_TAG="${DERIVED_STAGING_TAG}"
elif [ "${STAGING_TAG}" != "${DERIVED_STAGING_TAG}" ]; then
  echo "❌ ERROR: staging tag '${STAGING_TAG}' does not match the tag derived from '${RC_TAG}' ('${DERIVED_STAGING_TAG}')." >&2
  exit 1
fi

# resolve_promotion_candidate.sh's gate, applied again here on the commit rather
# than on the tag. COMMIT_SHA and RC_TAG are independent arguments, so every check
# above can pass while COMMIT_SHA points somewhere else — and the tag pushed at it
# would read back to a candidate that was validated. Deliberately duplicated
# because this script is reachable by hand; the shared helper keeps the two
# answers from diverging.
if ! is_rc_candidate_commit_already_validated "${COMMIT_SHA}"; then
  echo "❌ ERROR: commit ${COMMIT_SHA} carries no rc_*_validated tag; refusing to promote it to staging." >&2
  echo "   Only candidates the RC pipeline validated can be promoted." >&2
  exit 1
fi

# Namespace guard, kept even though the value was derived a line ago: this is the
# last point before a live deploy trigger is pushed, and a future caller passing
# STAGING_TAG in the environment reaches here too.
case "${STAGING_TAG}" in
  "${STAGING_TAG_PREFIX}"?*) ;;
  *)
    echo "❌ ERROR: refusing to push '${STAGING_TAG}': a staging promotion tag must start with '${STAGING_TAG_PREFIX}'." >&2
    exit 1
    ;;
esac

# The candidate has to be able to answer the tag. A push event runs the workflows
# in the pushed ref's tree, and the ref here points at a commit that may predate
# the trigger this tag is shaped for — every rc_*_validated candidate up to
# rc_2608310656_cf038a2_validated still declares `staging/**`, which a flat
# staging_<ts>_<sha> does not match.
#
# Refusing is the point. The alternative is a promotion that pushes its tag,
# deploys nothing, and reports green — and then never retries, because
# get_existing_staging_tag finds the tag it just pushed and every later run sets
# skip_promotion. A red step 4 leaves the candidate unpromoted, so the next
# validated candidate is picked up normally.
#
# Delete this guard once no rc_*_validated tag predates the trigger rename; it is
# the same window the script-name fallbacks in deploy-environment.yml and
# teardown-environment.yml close.
if ! staging_trigger_matches_at_commit "${COMMIT_SHA}" "${STAGING_TAG}"; then
  echo "::error title=Candidate predates the staging_* trigger::Commit ${COMMIT_SHA} declares a staging-redeploy trigger that '${STAGING_TAG}' does not match, so pushing this tag would deploy nothing and still report success. Refusing. Promote a candidate validated after the trigger rename; the RC pipeline produces one every three hours." >&2
  echo "==> Refusing to promote ${COMMIT_SHA}: its staging-redeploy trigger does not match '${STAGING_TAG}'." >&2
  exit 1
fi

exec "${SCRIPT_DIR}/tag_commit.sh" \
  --title "PROMOTING VALIDATED CANDIDATE TO STAGING" \
  --detail "Source RC Tag: ${RC_TAG}" \
  "${STAGING_TAG}" "${COMMIT_SHA}" "Staging promotion of ${RC_TAG} (commit ${COMMIT_SHA})"
