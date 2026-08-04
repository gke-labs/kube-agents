#!/usr/bin/env bash
# Resolves candidate commit SHA and release tag, setting GITHUB_OUTPUT.
set -euo pipefail

RC_TAG="${1:-${RC_TAG:-}}"
COMMIT_INPUT="${2:-${COMMIT_SHA:-}}"

# Strict Target Commit SHA resolution
if [ -n "${COMMIT_INPUT}" ]; then
  if ! COMMIT_SHA=$(git rev-parse --verify "${COMMIT_INPUT}^{commit}" 2>/dev/null); then
    echo "❌ ERROR: Cannot resolve valid Git commit SHA from input '${COMMIT_INPUT}'!" >&2
    exit 1
  fi
elif [ -n "${RC_TAG}" ]; then
  target_repo=""
  if [ -n "${GH_ORG:-}" ] && [ -n "${GH_REPO:-}" ]; then
    target_repo="${GH_ORG}/${GH_REPO}"
  elif [ -n "${GITHUB_REPOSITORY:-}" ]; then
    target_repo="${GITHUB_REPOSITORY}"
  fi

  if [ -n "${target_repo}" ]; then
    git fetch "https://github.com/${target_repo}.git" +refs/tags/*:refs/tags/* >/dev/null 2>&1 || true
  else
    git fetch --tags >/dev/null 2>&1 || true
  fi

  if ! COMMIT_SHA=$(git rev-parse --verify "${RC_TAG}^{commit}" 2>/dev/null); then
    echo "❌ ERROR: Cannot resolve valid Git commit SHA from release tag '${RC_TAG}'!" >&2
    exit 1
  fi
elif [ -n "${GITHUB_SHA:-}" ]; then
  COMMIT_SHA="${GITHUB_SHA}"
else
  echo "❌ ERROR: Neither COMMIT_SHA nor RC_TAG input was provided!" >&2
  exit 1
fi

# Resolve Release Tag Name (User input > auto-generated fallback with short SHA)
if [ -z "${RC_TAG}" ]; then
  SHORT_SHA="${COMMIT_SHA:0:7}"
  RC_TAG="rc_$(date -u +'%y%m%d%H%M')_${SHORT_SHA}"
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "commit_sha=${COMMIT_SHA}" >> "$GITHUB_OUTPUT"
  echo "rc_tag=${RC_TAG}" >> "$GITHUB_OUTPUT"
fi

echo "======================================================================"
echo "🏷️ RESOLVED RELEASE CANDIDATE TARGET"
echo "Target Commit SHA: ${COMMIT_SHA}"
echo "Release Tag:      ${RC_TAG}"
echo "======================================================================"
