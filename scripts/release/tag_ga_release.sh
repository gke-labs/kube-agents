#!/usr/bin/env bash
# Creates and pushes an official GA SemVer Git tag for a target commit SHA safely and idempotently.
# Releases strictly use pure numeric SemVer without 'v' prefix (e.g. 0.1.0, 0.2.0).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

RELEASE_VERSION="${1:-${RELEASE_VERSION:-${TARGET_VERSION:-${TARGET_TAG:-}}}}"
RC_CANDIDATE_COMMIT="${2:-${RC_CANDIDATE_COMMIT:-${TARGET_COMMIT:-}}}"

if [ -z "${RELEASE_VERSION}" ] || [ -z "${RC_CANDIDATE_COMMIT}" ]; then
  echo "❌ ERROR: RELEASE_VERSION and RC candidate commit are required as arguments or environment variables." >&2
  echo "Usage: $0 (with RELEASE_VERSION and RC candidate commit in env) or $0 <RELEASE_VERSION> <RC_CANDIDATE_COMMIT>" >&2
  exit 1
fi

validate_pure_numeric_semver "${RELEASE_VERSION}" "Release version" || exit 1

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || (cd "${SCRIPT_DIR}/../.." && pwd))"

# Canonicalize RC candidate commit SHA
RC_CANDIDATE_COMMIT_SHA="$(git -C "${REPO_ROOT}" rev-parse --verify "${RC_CANDIDATE_COMMIT}^{commit}" 2>/dev/null || echo "${RC_CANDIDATE_COMMIT}")"

RELEASE_COMMIT="$(create_stamped_release_commit "${RELEASE_VERSION}" "${RC_CANDIDATE_COMMIT_SHA}" "${REPO_ROOT}")"

# The banner comes from tag_commit.sh, below, rather than being printed here:
# the stamped release commit is resolved first, so the banner can name the commit
# the tag actually lands on. This script keeps what is genuinely its own — the
# pure-SemVer gate, the swapped-argument handling, and the stamping — and hands
# the tag itself to the shared tagger. A mistaken GA tag is the one rung of the
# ladder that cannot be fixed by deleting a tag, so it does not get a private
# copy of the tagging logic either.
# Where the release branch is, read before anything is pushed. ensure_release_branch
# below refuses a branch already at another commit; finding that only after the
# GA tag went out would leave the one artefact a failed run cannot take back.
release_branch_placement "${RELEASE_VERSION}" "${RELEASE_COMMIT}" >/dev/null

GA_TAG_DETAILS=(--detail "Release Version:     ${RELEASE_VERSION}")
GA_TAG_DETAILS+=(--detail "RC Candidate Commit: ${RC_CANDIDATE_COMMIT_SHA:0:7}")
if [ "${RELEASE_COMMIT}" != "${RC_CANDIDATE_COMMIT_SHA}" ]; then
  GA_TAG_DETAILS+=(--detail "Release Commit:      ${RELEASE_COMMIT:0:7}")
fi
GA_TAG_DETAILS+=(--detail "Release Branch:      $(release_branch_for_version "${RELEASE_VERSION}")")

"${SCRIPT_DIR}/tag_commit.sh" \
  --title "CREATING AND PUSHING GA RELEASE GIT TAG" \
  "${GA_TAG_DETAILS[@]}" \
  "${RELEASE_VERSION}" "${RELEASE_COMMIT}" "Release ${RELEASE_VERSION}"

# The branch is pushed after the tag, and the order is load-bearing. The tag is
# what a re-run keys on: create_stamped_release_commit reuses the tagged commit
# and verify_release_eligibility.sh reads the tag, so a run that fails here
# re-runs the way one that fails at image promotion does, and pushes the branch
# it did not get to. The other way round is not re-runnable: with the branch
# pushed and no tag, the re-run stamps a fresh commit and refuses the branch it
# pushed itself. Without the branch the stamped commit is reachable from the tag
# alone, which GitHub shows as belonging to no branch on the repository.
ensure_release_branch "${RELEASE_VERSION}" "${RELEASE_COMMIT}"
