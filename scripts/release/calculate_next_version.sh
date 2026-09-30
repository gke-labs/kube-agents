#!/usr/bin/env bash
# Calculates the next semantic version (X.Y.Z) based on Conventional Commits since the GA release
# the candidate's history descends from (get_base_ga_tag_for_commit), on main or on a release line.
# Correctly implements SemVer 2.0 clause 4: in 0.y.z initial development, Breaking Changes bump MINOR (0.1.0 -> 0.2.0).
# Releases strictly use pure numeric SemVer without 'v' prefix (e.g. 0.1.0, 0.2.0).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

EXPLICIT_RELEASE_VERSION="${EXPLICIT_RELEASE_VERSION:-${3:-}}"
BASE_TAG_PARAM="${1:-${BASE_TAG_PARAM:-${BASE_TAG:-}}}"
TARGET_REF_PARAM="${2:-${TARGET_REF_PARAM:-${TARGET_COMMIT:-${TARGET_REF:-}}}}"
SKIP_VALIDATION="${SKIP_STAGING_VALIDATION:-${4:-false}}"
# A release line (`X.Y`) cuts patches from its own branch: the candidate is the
# line's head, the base is the line's last release, and the bump is PATCH.
RELEASE_LINE="${RELEASE_LINE:-}"
if [ -n "${RELEASE_LINE}" ] && ! [[ "${RELEASE_LINE}" =~ ${RELEASE_LINE_SHAPE_REGEX} ]]; then
  echo "❌ ERROR: RELEASE_LINE '${RELEASE_LINE}' is not a release line; expected X.Y." >&2
  exit 1
fi

# 0. Protection against Shallow Checkout and Remote Tag Sync in CI. Pruned, so
# that the tag list the base is read from is the remote's: a tag only this
# checkout holds (a release that never landed, in a persistent clone) would
# otherwise be the base of the version and, downstream, the notes-start tag of
# a release the repository does not hold.
if is_ci_pipeline; then
  TARGET_REPO="$(get_target_repo)"
  echo "📥 Fetching tags from target repository (${TARGET_REPO})..." >&2
  git fetch "https://github.com/${TARGET_REPO}.git" --tags --force --prune --prune-tags 2>/dev/null ||
    git fetch --tags --force --prune --prune-tags 2>/dev/null || true
fi

if [ "$(git rev-parse --is-shallow-repository 2>/dev/null || echo "false")" = "true" ]; then
  echo "ℹ️ Shallow repository detected. Unshallowing git history for version calculation..." >&2
  git fetch --unshallow --tags >/dev/null 2>&1 || git fetch --depth=100 --tags >/dev/null 2>&1 || true
fi

# 1. Resolve Target Commit / Ref for version calculation
if [ -n "${RELEASE_LINE}" ]; then
  # The line names its own candidate. A TARGET_COMMIT that says otherwise is a
  # contradiction to refuse, not a preference to honour: the branch step can
  # only fast-forward from the head, and the line's protection vouches for the
  # head alone.
  LINE_CANDIDATE="$(release_line_resolve_candidate "${RELEASE_LINE}" "${TARGET_REF_PARAM}" "${EXPLICIT_RELEASE_VERSION}")" || exit 1
  TARGET_REF_PARAM="${LINE_CANDIDATE}"
  echo "ℹ️ Release line ${RELEASE_LINE}: candidate is the line's own ${LINE_CANDIDATE:0:7}" >&2
elif [ -z "${TARGET_REF_PARAM}" ] || [ "${TARGET_REF_PARAM}" = "null" ]; then
  if is_truthy "${SKIP_VALIDATION}"; then
    TARGET_REF_PARAM="HEAD"
    echo "ℹ️ Emergency override: calculating version from HEAD" >&2
  else
    # The same lookup the release gate uses. The two resolving differently would
    # compute a version for one commit and publish another.
    LATEST_GATE_TAG="$(get_latest_staging_tag)"
    if [ -n "${LATEST_GATE_TAG}" ]; then
      TARGET_REF_PARAM="${LATEST_GATE_TAG}"
      echo "ℹ️ Auto-resolved target commit from newest staging promotion tag '${LATEST_GATE_TAG}'" >&2
    else
      TARGET_REF_PARAM="HEAD"
    fi
  fi
fi

# Validate target ref exists in git repository and resolve 40-character SHA
if ! RC_CANDIDATE_COMMIT="$(git rev-parse --verify "${TARGET_REF_PARAM}^{commit}" 2>/dev/null)"; then
  echo "❌ ERROR: Target ref '${TARGET_REF_PARAM}' does not exist in git repository!" >&2
  exit 1
fi

# 2. Resolve baseline GA SemVer tag (pure numeric X.Y.Z, excluding rc_* tags)
if [ -n "${BASE_TAG_PARAM}" ]; then
  validate_pure_numeric_semver "${BASE_TAG_PARAM}" "Base tag" || exit 1
  if ! git rev-parse --verify "refs/tags/${BASE_TAG_PARAM}^{commit}" >/dev/null 2>&1; then
    echo "❌ ERROR: Base tag '${BASE_TAG_PARAM}' does not exist in git repository!" >&2
    exit 1
  fi
  LATEST_GA_TAG="${BASE_TAG_PARAM}"
else
  # By ancestry, not by number: once release/0.7 has cut 0.7.1, main's base is
  # still 0.7.0 and the line's is 0.7.1. On main the tags are qualified against
  # main's head, so a release cut by hand from a later main commit still counts.
  # See get_base_ga_tag_for_commit.
  BASE_TIP=""
  if [ -z "${RELEASE_LINE}" ]; then
    BASE_TIP="$(release_main_tip)" || exit 1
    # A release from main is cut from main: a release-line commit named here
    # would be stamped as a release main never sees as its base, and every later
    # main release would collide with it. Its own dispatch is release_line.
    if [ -n "${BASE_TIP}" ] && ! git merge-base --is-ancestor "${RC_CANDIDATE_COMMIT}" "${BASE_TIP}" 2>/dev/null; then
      if is_ci_pipeline; then
        echo "❌ ERROR: Target commit ${RC_CANDIDATE_COMMIT:0:7} is not on ${RELEASE_MAIN_BRANCH}; a commit on a release line is released with release_line, not target_commit." >&2
        exit 1
      fi
      # Off CI the main this checkout can see is a tracking or local ref, as fresh as
      # the last fetch (release_main_ref), and on a fork clone origin/main is the
      # fork's: a stale one is the likelier reading of "not on main" than a line
      # commit, so the check is advisory here and the base is qualified against
      # the candidate alone, as when main cannot be read at all.
      echo "⚠️ Warning: Target commit ${RC_CANDIDATE_COMMIT:0:7} is not on the ${RELEASE_MAIN_BRANCH} this checkout can see (${BASE_TIP:0:7}), which is as fresh as its last fetch; qualifying the GA base against the candidate alone. In CI, where ${RELEASE_MAIN_BRANCH} is fetched from the release repository, this is refused." >&2
      BASE_TIP=""
    fi
  fi
  LATEST_GA_TAG="$(get_base_ga_tag_for_commit "${RC_CANDIDATE_COMMIT}" "" "${BASE_TIP}")" || exit 1
fi

if [ -n "${RELEASE_LINE}" ]; then
  if [ -z "${LATEST_GA_TAG}" ]; then
    echo "❌ ERROR: Release line ${RELEASE_LINE}'s candidate ${RC_CANDIDATE_COMMIT:0:7} descends from no GA release; a line is cut from its minor's release." >&2
    exit 1
  fi
  if [ "$(release_line_for_version "${LATEST_GA_TAG}")" != "${RELEASE_LINE}" ]; then
    echo "❌ ERROR: Release line ${RELEASE_LINE}'s candidate ${RC_CANDIDATE_COMMIT:0:7} descends from ${LATEST_GA_TAG}, which is not on the line; refusing." >&2
    exit 1
  fi
fi

# Whether the base's line has its own branch. Once it does, PATCH numbers belong
# to the line: main bumps at least MINOR (step 6), and an explicit version on
# main may not name the line's next patch (step 3). Read from the remote in CI;
# an unreadable remote is an error rather than a guess.
BASE_LINE_HAS_BRANCH="false"
if [ -z "${RELEASE_LINE}" ] && [ -n "${LATEST_GA_TAG}" ]; then
  BASE_LINE="$(release_line_for_version "${LATEST_GA_TAG}")"
  if release_line_branch_exists "${BASE_LINE}"; then
    BASE_LINE_HAS_BRANCH="true"
  elif [ $? -eq 2 ]; then
    echo "❌ ERROR: Could not read whether release line ${BASE_LINE} exists; refusing to pick a version without knowing." >&2
    exit 1
  fi
fi

# 3. Handle explicit version override (EXPLICIT_RELEASE_VERSION) with downgrade and collision protection
if [ -n "${EXPLICIT_RELEASE_VERSION}" ]; then
  # 3.1 Validate SemVer 2.0 format
  validate_pure_numeric_semver "${EXPLICIT_RELEASE_VERSION}" "Explicit release version" || exit 1

  # 3.2 Protect against version downgrade, against the candidate's own base:
  # 0.7.1 on release/0.7 is fine while 0.8.0 exists on main.
  if [ -n "${LATEST_GA_TAG}" ]; then
    CMP_RES="$(compare_semver "${EXPLICIT_RELEASE_VERSION}" "${LATEST_GA_TAG}")"
    if [ "${CMP_RES}" = "-1" ]; then
      echo "❌ ERROR: Explicit release version '${EXPLICIT_RELEASE_VERSION}' is lower than the candidate's base release '${LATEST_GA_TAG}'. Version downgrade is prohibited." >&2
      exit 1
    fi
  fi

  # 3.2b A line takes its own patch numbers and nothing else; main keeps off
  # them once the line has a branch.
  if [ -n "${RELEASE_LINE}" ]; then
    if [ "$(release_line_for_version "${EXPLICIT_RELEASE_VERSION}")" != "${RELEASE_LINE}" ]; then
      echo "❌ ERROR: Explicit release version '${EXPLICIT_RELEASE_VERSION}' is not on release line ${RELEASE_LINE}." >&2
      exit 1
    fi
  # An explicit version equal to the base is a re-run of the release that
  # created the line (3.3 below admits it only when the tag sits on this
  # candidate's stamp), not a new patch on it.
  elif [ "${BASE_LINE_HAS_BRANCH}" = "true" ] && [ "${EXPLICIT_RELEASE_VERSION}" != "${LATEST_GA_TAG}" ] && [ "$(release_line_for_version "${EXPLICIT_RELEASE_VERSION}")" = "${BASE_LINE}" ]; then
    echo "❌ ERROR: Explicit release version '${EXPLICIT_RELEASE_VERSION}' is a patch on line ${BASE_LINE}, which has its own branch; release it from release/${BASE_LINE}, or bump MINOR on main." >&2
    exit 1
  fi

  # 3.3 Protect against tag collisions on different commits
  if TAG_COMMIT="$(git rev-parse --verify "refs/tags/${EXPLICIT_RELEASE_VERSION}^{commit}" 2>/dev/null)"; then
    REQ_COMMIT="$(git rev-parse --verify "${RC_CANDIDATE_COMMIT}^{commit}" 2>/dev/null || echo "")"
    if [ -n "${REQ_COMMIT}" ] && ! is_valid_stamped_or_direct_release_commit "${REQ_COMMIT}" "${TAG_COMMIT}" "${EXPLICIT_RELEASE_VERSION}"; then
      echo "❌ ERROR: Tag '${EXPLICIT_RELEASE_VERSION}' already exists in git repository on a different commit (${TAG_COMMIT:0:7}). Cannot re-assign existing release tag." >&2
      exit 1
    fi
  fi

  echo "ℹ️ Using verified explicit release version: ${EXPLICIT_RELEASE_VERSION}" >&2
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "release_version=${EXPLICIT_RELEASE_VERSION}" >> "${GITHUB_OUTPUT}"
    echo "version=${EXPLICIT_RELEASE_VERSION}" >> "${GITHUB_OUTPUT}"
    echo "rc_candidate_commit=${RC_CANDIDATE_COMMIT}" >> "${GITHUB_OUTPUT}"
    echo "release_commit=${RC_CANDIDATE_COMMIT}" >> "${GITHUB_OUTPUT}"
    echo "previous_version=${LATEST_GA_TAG}" >> "${GITHUB_OUTPUT}"
    echo "has_changes=true" >> "${GITHUB_OUTPUT}"
    echo "bump_type=manual" >> "${GITHUB_OUTPUT}"
  fi
  echo "${EXPLICIT_RELEASE_VERSION}"
  exit 0
fi

# 4. Handle baseline repository initialization when no prior tags exist
if [ -z "${LATEST_GA_TAG}" ]; then
  echo "ℹ️ No previous GA SemVer tag found. Initializing repository at baseline ${DEFAULT_INITIAL_VERSION}." >&2
  NEXT_VERSION="${DEFAULT_INITIAL_VERSION}"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "release_version=${NEXT_VERSION}" >> "${GITHUB_OUTPUT}"
    echo "version=${NEXT_VERSION}" >> "${GITHUB_OUTPUT}"
    echo "rc_candidate_commit=${RC_CANDIDATE_COMMIT}" >> "${GITHUB_OUTPUT}"
    echo "release_commit=${RC_CANDIDATE_COMMIT}" >> "${GITHUB_OUTPUT}"
    echo "previous_version=" >> "${GITHUB_OUTPUT}"
    echo "has_changes=true" >> "${GITHUB_OUTPUT}"
    echo "bump_type=initial" >> "${GITHUB_OUTPUT}"
  fi
  echo "${NEXT_VERSION}"
  exit 0
fi

echo "📌 Latest GA Tag: ${LATEST_GA_TAG}" >&2
IFS='.' read -r MAJOR MINOR PATCH <<< "${LATEST_GA_TAG}"

# 5. Inspect commit range for subjects (%s) and bodies (%b). The read lives in
# common.sh so the bump and resolve_scheduled_release.sh's release decision are
# always taken over the same set of commits.
if ! release_read_commit_range "${LATEST_GA_TAG}" "${RC_CANDIDATE_COMMIT}"; then
  exit 1
fi
COMMITS_SUBJECTS="${RELEASE_RANGE_SUBJECTS}"
COMMITS_BODIES="${RELEASE_RANGE_BODIES}"

if [ -z "${COMMITS_SUBJECTS}" ]; then
  echo "ℹ️ No new commits since ${LATEST_GA_TAG}. Keeping current version." >&2
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "release_version=${LATEST_GA_TAG}" >> "${GITHUB_OUTPUT}"
    echo "version=${LATEST_GA_TAG}" >> "${GITHUB_OUTPUT}"
    echo "rc_candidate_commit=${RC_CANDIDATE_COMMIT}" >> "${GITHUB_OUTPUT}"
    echo "release_commit=${RC_CANDIDATE_COMMIT}" >> "${GITHUB_OUTPUT}"
    echo "previous_version=${LATEST_GA_TAG}" >> "${GITHUB_OUTPUT}"
    echo "has_changes=false" >> "${GITHUB_OUTPUT}"
    echo "bump_type=none" >> "${GITHUB_OUTPUT}"
  fi
  echo "${LATEST_GA_TAG}"
  exit 0
fi

# 6. Analyze commits according to SemVer 2.0 and Conventional Commits rules
BUMP_TYPE="patch"
HAS_BREAKING="false"

# A release line takes fixes only. A feat: or a breaking change on it is refused
# by name; explicit_release_version is the way to release one on purpose.
if [ -n "${RELEASE_LINE}" ]; then
  if commit_messages_have_breaking_change "${COMMITS_SUBJECTS}" "${COMMITS_BODIES}"; then
    echo "❌ ERROR: Release line ${RELEASE_LINE} carries a breaking change since ${LATEST_GA_TAG}; a line takes fixes only. Release it with explicit_release_version if you mean it." >&2
    exit 1
  fi
  if FEAT_SUBJECTS="$(grep -E "${FEAT_SUBJECT_REGEX}" <<<"${COMMITS_SUBJECTS}")"; then
    echo "❌ ERROR: Release line ${RELEASE_LINE} carries a feature since ${LATEST_GA_TAG}; a line takes fixes only:" >&2
    sed 's/^/   /' <<<"${FEAT_SUBJECTS}" >&2
    echo "   Release it with explicit_release_version if you mean it." >&2
    exit 1
  fi
fi

# Check for Breaking Changes in subject (feat!:, fix!:) or footer (BREAKING CHANGE: / BREAKING-CHANGE:).
# The definition lives in common.sh because resolve_scheduled_release.sh gates an
# unattended release on stable GA (>= 1.0.0) on the same question, while in 0.y.z
# initial development breaking changes bump MINOR under SemVer Clause 4.
if commit_messages_have_breaking_change "${COMMITS_SUBJECTS}" "${COMMITS_BODIES}"; then
  HAS_BREAKING="true"
fi

if [ "${HAS_BREAKING}" = "true" ]; then
  # SemVer 2.0 Clause 4: in 0.y.z initial development, breaking changes bump MINOR (0.1.0 -> 0.2.0)
  if ga_tag_is_initial_development "${LATEST_GA_TAG}"; then
    BUMP_TYPE="minor-breaking"
    MINOR=$((MINOR + 1))
    PATCH=0
  else
    BUMP_TYPE="major"
    MAJOR=$((MAJOR + 1))
    MINOR=0
    PATCH=0
  fi
# New features bump MINOR and reset PATCH. Herestring rather than `echo |`, for
# the reason commit_messages_have_breaking_change gives: under pipefail grep exits
# on its first match, the producer dies on SIGPIPE, and a large enough range reads
# as "no feat:" — a patch bump where a minor was owed.
elif grep -qE "${FEAT_SUBJECT_REGEX}" <<<"${COMMITS_SUBJECTS}"; then
  BUMP_TYPE="minor"
  MINOR=$((MINOR + 1))
  PATCH=0
else
  BUMP_TYPE="patch"
  PATCH=$((PATCH + 1))
fi

# Once the base's line has its own branch, its patch numbers are the line's:
# a fix-only range on main bumps MINOR instead.
if [ "${BUMP_TYPE}" = "patch" ] && [ "${BASE_LINE_HAS_BRANCH}" = "true" ]; then
  echo "ℹ️ release/${BASE_LINE} exists, so patch numbers are the line's; main bumps MINOR." >&2
  BUMP_TYPE="minor-line"
  MINOR=$((MINOR + 1))
  PATCH=0
fi

NEXT_VERSION="${MAJOR}.${MINOR}.${PATCH}"

echo "📈 Calculated next version: ${NEXT_VERSION} (bump: ${BUMP_TYPE})" >&2

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "release_version=${NEXT_VERSION}" >> "${GITHUB_OUTPUT}"
  echo "version=${NEXT_VERSION}" >> "${GITHUB_OUTPUT}"
  echo "rc_candidate_commit=${RC_CANDIDATE_COMMIT}" >> "${GITHUB_OUTPUT}"
  echo "release_commit=${RC_CANDIDATE_COMMIT}" >> "${GITHUB_OUTPUT}"
  echo "previous_version=${LATEST_GA_TAG}" >> "${GITHUB_OUTPUT}"
  echo "has_changes=true" >> "${GITHUB_OUTPUT}"
  echo "bump_type=${BUMP_TYPE}" >> "${GITHUB_OUTPUT}"
fi

echo "${NEXT_VERSION}"
