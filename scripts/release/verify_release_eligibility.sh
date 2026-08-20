#!/usr/bin/env bash
# Verifies that a target commit is eligible for official GA release (has passed live RC E2E validation)
# and performs an idempotent skip if the commit has already been released under the EXACT SAME tag.
# Releases strictly use pure numeric SemVer without 'v' prefix (e.g. 0.1.0, 0.2.0).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

TARGET_TAG="${1:-${TARGET_TAG:-}}"
TARGET_COMMIT="${2:-${TARGET_COMMIT:-}}"
TARGET_REPO="$(get_target_repo)"
SKIP_VALIDATION="${SKIP_RC_VALIDATION:-false}"
EMERGENCY_REASON="${EMERGENCY_OVERRIDE_REASON:-}"

if [ -z "${TARGET_TAG}" ]; then
  echo "❌ ERROR: Target release tag must be specified as first argument or TARGET_TAG environment variable." >&2
  exit 1
fi

if [[ ! "${TARGET_TAG}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "❌ ERROR: Target release tag '${TARGET_TAG}' is not a valid pure numeric SemVer (e.g. 0.1.0, 0.2.0). 'v' prefix is not supported." >&2
  exit 1
fi

echo "======================================================================"
echo "🔍 VERIFYING RELEASE ELIGIBILITY FOR: ${TARGET_TAG}"
echo "Target Commit:          ${TARGET_COMMIT:-<auto-resolve>}"
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

# 2. Resolve Target Commit SHA
RELEASE_COMMIT=""
if [ -n "${TARGET_COMMIT}" ] && [ "${TARGET_COMMIT}" != "null" ]; then
  if ! RELEASE_COMMIT="$(git rev-parse --verify "${TARGET_COMMIT}^{commit}" 2>/dev/null)"; then
    echo "❌ ERROR: Cannot resolve valid Git commit from '${TARGET_COMMIT}'!" >&2
    exit 1
  fi
else
  # Auto-resolve commit:
  # Check if target tag already exists in Git
  if RELEASE_COMMIT="$(git rev-parse --verify "${TARGET_TAG}^{commit}" 2>/dev/null)"; then
    echo "ℹ️ Resolved target commit from existing tag '${TARGET_TAG}': ${RELEASE_COMMIT:0:7}"
  elif is_truthy "${SKIP_VALIDATION}"; then
    # In emergency mode without an explicit commit parameter, default to current HEAD
    RELEASE_COMMIT="$(git rev-parse --verify HEAD)"
    echo "ℹ️ Emergency override: defaulted target commit to HEAD (${RELEASE_COMMIT:0:7})"
  else
    # In standard release mode, auto-resolve the latest validated commit with rc_*_validated tag
    LATEST_VALIDATED_TAG="$(git tag -l --sort=-creatordate 'rc_*_validated' 2>/dev/null | head -n 1 || echo "")"
    if [ -z "${LATEST_VALIDATED_TAG}" ]; then
      echo "❌ ERROR: No validated RC commit found in history! Cannot publish release without a commit carrying 'rc_*_validated' tag." >&2
      exit 1
    fi
    RELEASE_COMMIT="$(git rev-parse --verify "${LATEST_VALIDATED_TAG}^{commit}")"
    echo "ℹ️ Auto-resolved latest validated commit from tag '${LATEST_VALIDATED_TAG}': ${RELEASE_COMMIT:0:7}"
  fi
fi

# 3. Idempotent check and collision detection (always evaluated before validation checks)
EXISTING_RELEASE_TAGS="$(git tag --points-at "${RELEASE_COMMIT}" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' || true)"

for ex_tag in ${EXISTING_RELEASE_TAGS}; do
  # Scenario A: Re-running the exact same release tag -> Safe Idempotent Skip
  if [ "${ex_tag}" = "${TARGET_TAG}" ]; then
    echo "ℹ️ IDEMPOTENT SKIP: Release ${TARGET_TAG} for commit ${RELEASE_COMMIT} is already published."
    echo "ℹ️ Skipping duplicate build and publish steps."
    if [ -n "${GITHUB_OUTPUT:-}" ]; then
      echo "eligible=false" >> "${GITHUB_OUTPUT}"
      echo "already_released=true" >> "${GITHUB_OUTPUT}"
      echo "skip_release=true" >> "${GITHUB_OUTPUT}"
      echo "existing_tag=${ex_tag}" >> "${GITHUB_OUTPUT}"
      echo "release_commit=${RELEASE_COMMIT}" >> "${GITHUB_OUTPUT}"
    fi
    exit 0
  else
    # Scenario B: Collision (attempting to release the same commit under a DIFFERENT tag) -> Hard block
    echo "❌ ERROR: Collision detected! Commit ${RELEASE_COMMIT} is already published under release ${ex_tag}." >&2
    echo "   Cannot re-tag and re-release the same commit as ${TARGET_TAG}." >&2
    exit 1
  fi
done

# 4. Check Emergency Override with mandatory non-empty audit reason & container image verification
if is_truthy "${SKIP_VALIDATION}"; then
  CLEAN_REASON="${EMERGENCY_REASON//[[:space:]]/}"
  if [ -z "${CLEAN_REASON}" ]; then
    echo "❌ ERROR: Emergency override (SKIP_RC_VALIDATION=true) requires an explicit non-whitespace EMERGENCY_OVERRIDE_REASON for audit compliance." >&2
    exit 1
  fi

  echo "🔎 [Emergency Override] Verifying required container images exist in registry for commit ${RELEASE_COMMIT:0:7}..."
  if ! check_commit_images_exist "${RELEASE_COMMIT}"; then
    echo "❌ ERROR: Cannot perform emergency release! Required container images for commit ${RELEASE_COMMIT:0:7} do not exist in registry." >&2
    exit 1
  fi

  echo "⚠️ WARNING: RC E2E validation check is explicitly bypassed via emergency override!" >&2
  echo "⚠️ Reason: ${EMERGENCY_REASON}" >&2
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "eligible=true" >> "${GITHUB_OUTPUT}"
    echo "emergency_override=true" >> "${GITHUB_OUTPUT}"
    echo "release_commit=${RELEASE_COMMIT}" >> "${GITHUB_OUTPUT}"
  fi
  exit 0
fi

# 5. Check for validated RC tag pointing at target commit
echo "🔎 Checking for rc_*_validated tags pointing at commit ${RELEASE_COMMIT}..."
VALIDATED_TAGS="$(git tag --points-at "${RELEASE_COMMIT}" | grep -E '^rc_.*_validated$' || true)"

if [ -z "${VALIDATED_TAGS}" ]; then
  echo "❌ BLOCKED: Commit ${RELEASE_COMMIT} has NOT passed live RC E2E validation!" >&2
  echo "   No tag matching 'rc_*_validated' points to this commit." >&2
  echo "   To release this version:" >&2
  echo "     1. Wait for the scheduled RC pipeline to validate this commit." >&2
  echo "     2. Or run the '.github/workflows/release-rc.yml' workflow manually on this commit." >&2
  echo "     3. For emergency CVE hotfixes, run with skip_rc_validation=true and an explicit reason." >&2
  exit 1
fi

FIRST_VAL_TAG="$(echo "${VALIDATED_TAGS}" | head -n 1)"
echo "✅ ELIGIBLE: Found validated RC tag(s) on commit ${RELEASE_COMMIT}:"
for tag in ${VALIDATED_TAGS}; do
  echo "   • ${tag}"
done

# 6. Verify container images exist in registry
echo "🔎 Verifying required container images exist in registry for commit ${RELEASE_COMMIT:0:7}..."
if ! check_commit_images_exist "${RELEASE_COMMIT}"; then
  echo "❌ ERROR: Required container images for commit ${RELEASE_COMMIT} do not exist in registry!" >&2
  exit 1
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "eligible=true" >> "${GITHUB_OUTPUT}"
  echo "validated_rc_tag=${FIRST_VAL_TAG}" >> "${GITHUB_OUTPUT}"
  echo "release_commit=${RELEASE_COMMIT}" >> "${GITHUB_OUTPUT}"
fi

exit 0
