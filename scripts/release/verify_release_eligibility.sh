#!/usr/bin/env bash
# Verifies that a target commit is eligible for official GA release (has been promoted to staging by
# the nightly pipeline, meaning the full E2E matrix passed on it) and performs an idempotent skip if
# the commit has already been released under the EXACT SAME tag.
# Releases strictly use pure numeric SemVer without 'v' prefix (e.g. 0.1.0, 0.2.0).
#
# The gate is the staging_<ts>_<sha> tag and nothing beside it. An rc_*_validated tag is not checked
# as well, because it is implied: a staging tag is only ever created by the nightly pipeline, which
# only ever promotes a candidate that already carries one. Two gates to keep in step is how one of
# them ends up answering for the other.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

TARGET_VERSION="${1:-${TARGET_VERSION:-${RELEASE_VERSION:-${TARGET_TAG:-}}}}"
TARGET_COMMIT_INPUT="${2:-${RC_CANDIDATE_COMMIT:-${TARGET_COMMIT:-}}}"
TARGET_REPO="$(get_target_repo)"
SKIP_VALIDATION="${SKIP_STAGING_VALIDATION:-${3:-false}}"
EMERGENCY_REASON="${EMERGENCY_OVERRIDE_REASON:-${4:-}}"

if [ -z "${TARGET_VERSION}" ]; then
  echo "❌ ERROR: TARGET_VERSION is required as first argument or environment variable." >&2
  echo "Usage: $0 <TARGET_VERSION> [RC_CANDIDATE_COMMIT] or TARGET_VERSION=... $0" >&2
  exit 1
fi

validate_pure_numeric_semver "${TARGET_VERSION}" "Target release version" || exit 1

echo "======================================================================"
echo "🔍 VERIFYING RELEASE ELIGIBILITY FOR VERSION: ${TARGET_VERSION}"
echo "Target Commit:          ${TARGET_COMMIT_INPUT:-<auto-resolve>}"
echo "Target Repository:      ${TARGET_REPO}"
echo "Emergency Override:     ${SKIP_VALIDATION}"
if [ -n "${EMERGENCY_REASON}" ]; then
  echo "Emergency Reason:       ${EMERGENCY_REASON}"
fi
echo "======================================================================"

# Safe initialization of outputs to prevent false bypass
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "eligible=false" >> "${GITHUB_OUTPUT}"
  echo "already_released=false" >> "${GITHUB_OUTPUT}"
  echo "skip_release=false" >> "${GITHUB_OUTPUT}"
fi

# 1. Synchronize tags from remote if running in CI
if is_ci_pipeline; then
  echo "📥 Fetching tags from target repository (${TARGET_REPO})..."
  git fetch "https://github.com/${TARGET_REPO}.git" --tags --force 2>/dev/null || git fetch --tags --force 2>/dev/null || true
fi

# 2. Resolve Candidate Commit SHA
RC_CANDIDATE_COMMIT=""
if [ -n "${TARGET_COMMIT_INPUT}" ] && [ "${TARGET_COMMIT_INPUT}" != "null" ]; then
  if ! RC_CANDIDATE_COMMIT="$(git rev-parse --verify "${TARGET_COMMIT_INPUT}^{commit}" 2>/dev/null)"; then
    echo "❌ ERROR: Cannot resolve valid Git commit from '${TARGET_COMMIT_INPUT}'!" >&2
    exit 1
  fi
else
  # Auto-resolve commit:
  # Check if target version tag already exists in Git
  if TARGET_COMMIT="$(git rev-parse --verify "refs/tags/${TARGET_VERSION}^{commit}" 2>/dev/null)"; then
    RC_CANDIDATE_COMMIT="$(resolve_source_image_commit "${TARGET_VERSION}")"
    echo "ℹ️ Resolved target commit from existing release tag '${TARGET_VERSION}': ${RC_CANDIDATE_COMMIT:0:7}"
  elif is_truthy "${SKIP_VALIDATION}"; then
    # In emergency mode without an explicit commit parameter, default to current HEAD
    RC_CANDIDATE_COMMIT="$(git rev-parse --verify HEAD)"
    echo "ℹ️ Emergency override: defaulted target commit to HEAD (${RC_CANDIDATE_COMMIT:0:7})"
  else
    # In standard release mode, auto-resolve the newest staging-promoted commit
    LATEST_GATE_TAG="$(get_latest_staging_tag)"
    if [ -z "${LATEST_GATE_TAG}" ]; then
      echo "❌ ERROR: No staging-promoted commit found in history! Cannot publish release without a commit carrying a 'staging_<ts>_<sha>' tag." >&2
      exit 1
    fi
    RC_CANDIDATE_COMMIT="$(git rev-parse --verify "${LATEST_GATE_TAG}^{commit}")"
    echo "ℹ️ Auto-resolved newest staging-promoted commit from tag '${LATEST_GATE_TAG}': ${RC_CANDIDATE_COMMIT:0:7}"
  fi
fi

# 3. Idempotent check and collision detection (always evaluated before validation checks)
IS_RESUMING_RELEASE="false"

# Scenario A: Target version tag already exists in Git
if TARGET_COMMIT="$(git rev-parse --verify "refs/tags/${TARGET_VERSION}^{commit}" 2>/dev/null)"; then
  # Verify that target tag commit is directly or strictly derived (single-parent stamp) from RC_CANDIDATE_COMMIT
  if ! is_valid_stamped_or_direct_release_commit "${RC_CANDIDATE_COMMIT}" "${TARGET_COMMIT}" "${TARGET_VERSION}"; then
    echo "❌ ERROR: Tag '${TARGET_VERSION}' already exists in git repository on a different commit (${TARGET_COMMIT:0:7})!" >&2
    echo "   Cannot re-assign existing release tag to candidate commit ${RC_CANDIDATE_COMMIT:0:7}." >&2
    exit 1
  fi

  if is_ci_pipeline || [ -n "${GH_TOKEN:-}" ]; then
    if command -v gh >/dev/null 2>&1 && gh release view "${TARGET_VERSION}" --repo "${TARGET_REPO}" >/dev/null 2>&1; then
      echo "ℹ️ IDEMPOTENT SKIP: Release version ${TARGET_VERSION} and GitHub Release for commit ${RC_CANDIDATE_COMMIT:0:7} are already published."
      echo "ℹ️ Skipping duplicate build and publish steps."
      if [ -n "${GITHUB_OUTPUT:-}" ]; then
        echo "eligible=false" >> "${GITHUB_OUTPUT}"
        echo "already_released=true" >> "${GITHUB_OUTPUT}"
        echo "skip_release=true" >> "${GITHUB_OUTPUT}"
        echo "existing_tag=${TARGET_VERSION}" >> "${GITHUB_OUTPUT}"
        echo "rc_candidate_commit=${RC_CANDIDATE_COMMIT}" >> "${GITHUB_OUTPUT}"
        echo "release_commit=${TARGET_COMMIT}" >> "${GITHUB_OUTPUT}"
      fi
      exit 0
    else
      echo "⚠️ Git tag '${TARGET_VERSION}' already points to commit ${TARGET_COMMIT:0:7}, but GitHub Release does not exist yet. Resuming release workflow..."
      IS_RESUMING_RELEASE="true"
    fi
  else
    echo "ℹ️ IDEMPOTENT SKIP: Release version ${TARGET_VERSION} for commit ${RC_CANDIDATE_COMMIT:0:7} is already published (local dry-run)."
    echo "ℹ️ Skipping duplicate build and publish steps."
    if [ -n "${GITHUB_OUTPUT:-}" ]; then
      echo "eligible=false" >> "${GITHUB_OUTPUT}"
      echo "already_released=true" >> "${GITHUB_OUTPUT}"
      echo "skip_release=true" >> "${GITHUB_OUTPUT}"
      echo "existing_tag=${TARGET_VERSION}" >> "${GITHUB_OUTPUT}"
      echo "rc_candidate_commit=${RC_CANDIDATE_COMMIT}" >> "${GITHUB_OUTPUT}"
      echo "release_commit=${TARGET_COMMIT}" >> "${GITHUB_OUTPUT}"
    fi
    exit 0
  fi
fi

# Scenario B: Collision check (has RC_CANDIDATE_COMMIT already been published under a DIFFERENT GA release tag?)
for other_tag in $(git tag -l '[0-9]*' 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' || true); do
  if [ "${other_tag}" != "${TARGET_VERSION}" ]; then
    OTHER_COMMIT="$(git rev-parse --verify "refs/tags/${other_tag}^{commit}" 2>/dev/null || echo "")"
    if [ -n "${OTHER_COMMIT}" ]; then
      if [ "${OTHER_COMMIT}" = "${RC_CANDIDATE_COMMIT}" ] || \
         [ "$(git rev-parse --verify "${OTHER_COMMIT}^" 2>/dev/null || echo "")" = "${RC_CANDIDATE_COMMIT}" ]; then
        echo "❌ ERROR: Collision detected! Commit ${RC_CANDIDATE_COMMIT} is already published under release ${other_tag}." >&2
        echo "   Cannot re-tag and re-release the same commit as ${TARGET_VERSION}." >&2
        exit 1
      fi
    fi
  fi
done

# 4. Check Emergency Override with mandatory non-empty audit reason & container image verification
if is_truthy "${SKIP_VALIDATION}"; then
  CLEAN_REASON="${EMERGENCY_REASON//[[:space:]]/}"
  if [ -z "${CLEAN_REASON}" ]; then
    echo "❌ ERROR: Emergency override (SKIP_STAGING_VALIDATION=true) requires an explicit non-whitespace EMERGENCY_OVERRIDE_REASON for audit compliance." >&2
    exit 1
  fi

  echo "🔎 [Emergency Override] Verifying required container images exist in registry for commit ${RC_CANDIDATE_COMMIT:0:7}..."
  if ! check_commit_images_exist "${RC_CANDIDATE_COMMIT}"; then
    echo "❌ ERROR: Cannot perform emergency release! Required container images for commit ${RC_CANDIDATE_COMMIT:0:7} do not exist in registry." >&2
    exit 1
  fi

  echo "⚠️ WARNING: RC E2E validation check is explicitly bypassed via emergency override!" >&2
  echo "⚠️ Reason: ${EMERGENCY_REASON}" >&2
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "eligible=true" >> "${GITHUB_OUTPUT}"
    echo "emergency_override=true" >> "${GITHUB_OUTPUT}"
    echo "rc_candidate_commit=${RC_CANDIDATE_COMMIT}" >> "${GITHUB_OUTPUT}"
    echo "release_commit=${RC_CANDIDATE_COMMIT}" >> "${GITHUB_OUTPUT}"
  fi
  exit 0
fi

# 5. Check for a staging promotion tag pointing at target commit.
#
# Shape-matched via common.sh rather than by the staging_ prefix: the prefix is a live deploy
# trigger anyone can push, so a hand-made 'staging_hotfix' would otherwise satisfy the release gate.
echo "🔎 Checking for staging_<ts>_<sha> tags pointing at commit ${RC_CANDIDATE_COMMIT}..."
VALIDATED_TAGS="$(staging_promotion_tags_at_commit "${RC_CANDIDATE_COMMIT}")"

if [ -z "${VALIDATED_TAGS}" ]; then
  echo "❌ BLOCKED: Commit ${RC_CANDIDATE_COMMIT} has NOT been promoted to staging!" >&2
  echo "   No tag matching 'staging_<ts>_<sha>' points to this commit." >&2
  echo "   To release this version:" >&2
  echo "     1. Wait for the nightly pipeline to run the full E2E matrix and promote this commit." >&2
  echo "     2. Or run the '.github/workflows/nightly-pipeline.yml' workflow manually on its candidate." >&2
  echo "     3. For emergency CVE hotfixes, run with skip_staging_validation=true and an explicit reason." >&2
  exit 1
fi

FIRST_VAL_TAG="$(head -n 1 <<<"${VALIDATED_TAGS}")"
echo "✅ ELIGIBLE: Found staging promotion tag(s) on commit ${RC_CANDIDATE_COMMIT}:"
for tag in ${VALIDATED_TAGS}; do
  echo "   • ${tag}"
done

# 6. Verify container images exist in registry
echo "🔎 Verifying required container images exist in registry for commit ${RC_CANDIDATE_COMMIT:0:7}..."
if ! check_commit_images_exist "${RC_CANDIDATE_COMMIT}"; then
  echo "❌ ERROR: Required container images for commit ${RC_CANDIDATE_COMMIT} do not exist in registry!" >&2
  exit 1
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "eligible=true" >> "${GITHUB_OUTPUT}"
  echo "gate_tag=${FIRST_VAL_TAG}" >> "${GITHUB_OUTPUT}"
  echo "rc_candidate_commit=${RC_CANDIDATE_COMMIT}" >> "${GITHUB_OUTPUT}"
  echo "release_commit=${RC_CANDIDATE_COMMIT}" >> "${GITHUB_OUTPUT}"
  if [ "${IS_RESUMING_RELEASE}" = "true" ]; then
    echo "resuming=true" >> "${GITHUB_OUTPUT}"
  fi
fi

exit 0
