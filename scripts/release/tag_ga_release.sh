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

RELEASE_LINE_BRANCH="$(release_branch_for_line "$(release_line_for_version "${RELEASE_VERSION}")")"

# The banner is printed here rather than by tag_commit.sh: the GA rung pushes
# two refs, the tag and its release line, and it pushes them atomically through
# ensure_ga_release_refs so that neither can exist on the remote without the
# other, and it reads where the line is before anything is pushed: a line at
# any commit but the release commit, its candidate, or beyond it stops the run
# with nothing pushed. The line's first release creates `release/X.Y` because
# the line is absent; a later patch fast-forwards it because it is at the
# candidate; a merge that lands on the line meanwhile rejects the whole push,
# and the re-run stamps from the new head. This script keeps what is genuinely its own — the pure-SemVer gate, the
# swapped-argument handling, the stamping — and the shared helpers keep the
# idempotency contract every rung of the ladder has.
echo "======================================================================"
echo "🏷️ CREATING AND PUSHING GA RELEASE GIT TAG"
echo "Tag:          ${RELEASE_VERSION}"
echo "Commit SHA:   ${RELEASE_COMMIT}"
echo "Release Version:     ${RELEASE_VERSION}"
echo "RC Candidate Commit: ${RC_CANDIDATE_COMMIT_SHA:0:7}"
if [ "${RELEASE_COMMIT}" != "${RC_CANDIDATE_COMMIT_SHA}" ]; then
  echo "Release Commit:      ${RELEASE_COMMIT:0:7}"
fi
echo "Release Line:        ${RELEASE_LINE_BRANCH}"
echo "======================================================================"

ensure_ga_release_refs "${RELEASE_VERSION}" "${RELEASE_COMMIT}" "${RC_CANDIDATE_COMMIT_SHA}"
