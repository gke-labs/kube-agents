#!/usr/bin/env bash
# Resolves candidate commit SHA and release tag, setting GITHUB_OUTPUT.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

RC_TAG="${1:-${RC_TAG:-}}"
COMMIT_INPUT="${2:-${COMMIT_SHA:-}}"
IS_SCHEDULED="${IS_SCHEDULED:-false}"

SKIP_RC="false"

# Strict Target Commit SHA resolution
if [ -n "${COMMIT_INPUT}" ]; then
  if ! COMMIT_SHA=$(git rev-parse --verify "${COMMIT_INPUT}^{commit}" 2>/dev/null); then
    echo "❌ ERROR: Cannot resolve valid Git commit SHA from input '${COMMIT_INPUT}'!" >&2
    exit 1
  fi
elif [ -n "${RC_TAG}" ]; then
  target_repo="$(get_target_repo)"

  if is_ci_pipeline; then
    if [ -n "${target_repo}" ]; then
      git fetch "https://github.com/${target_repo}.git" --tags >/dev/null 2>&1 || true
    else
      git fetch --tags >/dev/null 2>&1 || true
    fi
  fi

  if ! COMMIT_SHA=$(git rev-parse --verify "${RC_TAG}^{commit}" 2>/dev/null); then
    echo "❌ ERROR: Cannot resolve valid Git commit SHA from release tag '${RC_TAG}'!" >&2
    exit 1
  fi
elif [ "${IS_SCHEDULED}" = "true" ]; then
  COMMIT_SHA=$(find_latest_built_commit)
  if [ -z "${COMMIT_SHA}" ]; then
    exit 1
  fi

  if is_commit_already_validated "${COMMIT_SHA}"; then
    echo "ℹ️ Latest built commit ${COMMIT_SHA:0:7} is already validated (*_validated). Skipping redundant RC run." >&2
    SKIP_RC="true"
  elif is_commit_already_attempted "${COMMIT_SHA}"; then
    echo "ℹ️ Latest built commit ${COMMIT_SHA:0:7} has already been evaluated in a previous RC pipeline run. Skipping redundant RC run." >&2
    SKIP_RC="true"
  else
    SKIP_RC="false"
  fi
else
  echo "❌ ERROR: Neither COMMIT_SHA nor RC_TAG input was provided (mandatory for manual runs)!" >&2
  exit 1
fi

# Resolve Release Tag Name (User input > existing candidate tag > deterministic rc_YYMMDDHHMM_<short_sha>)
if [ -z "${RC_TAG}" ]; then
  existing_rc_tag="$(get_existing_rc_tag "${COMMIT_SHA}")"
  if [ -n "${existing_rc_tag}" ]; then
    RC_TAG="${existing_rc_tag}"
  else
    SHORT_SHA="${COMMIT_SHA:0:7}"
    COMMIT_DATE="$(TZ=UTC0 git show -s --date=format-local:'%y%m%d%H%M' --format='%cd' "${COMMIT_SHA}")"
    RC_TAG="rc_${COMMIT_DATE}_${SHORT_SHA}"
  fi
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "commit_sha=${COMMIT_SHA}" >> "$GITHUB_OUTPUT"
  echo "rc_tag=${RC_TAG}" >> "$GITHUB_OUTPUT"
  echo "skip_rc=${SKIP_RC}" >> "$GITHUB_OUTPUT"
fi

echo "======================================================================"
echo "🏷️ RESOLVED RELEASE CANDIDATE TARGET"
echo "Target Commit SHA:            ${COMMIT_SHA}"
echo "Release Tag:                  ${RC_TAG}"
echo "Skip (RC Already Validated):  ${SKIP_RC}"
echo "======================================================================"
