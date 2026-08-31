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
GA_TAG_DETAILS=(--detail "Release Version:     ${RELEASE_VERSION}")
GA_TAG_DETAILS+=(--detail "RC Candidate Commit: ${RC_CANDIDATE_COMMIT_SHA:0:7}")
if [ "${RELEASE_COMMIT}" != "${RC_CANDIDATE_COMMIT_SHA}" ]; then
  GA_TAG_DETAILS+=(--detail "Release Commit:      ${RELEASE_COMMIT:0:7}")
fi

exec "${SCRIPT_DIR}/tag_commit.sh" \
  --title "CREATING AND PUSHING GA RELEASE GIT TAG" \
  "${GA_TAG_DETAILS[@]}" \
  "${RELEASE_VERSION}" "${RELEASE_COMMIT}" "Release ${RELEASE_VERSION}"
