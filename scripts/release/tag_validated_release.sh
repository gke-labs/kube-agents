#!/usr/bin/env bash
# Attaches an '_validated' Git tag to a verified Release Candidate commit safely and idempotently.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

COMMIT_SHA="${1:-${COMMIT_SHA:-}}"
RC_TAG="${2:-${RC_TAG:-}}"

if [ -z "${COMMIT_SHA}" ] || [ -z "${RC_TAG}" ]; then
  echo "❌ ERROR: COMMIT_SHA and RC_TAG are required." >&2
  exit 1
fi

VALIDATED_TAG="${RC_TAG}_validated"

echo "======================================================================"
echo "🏷️ MARKING RELEASE CANDIDATE COMMIT AS VALIDATED"
echo "Commit SHA:     ${COMMIT_SHA}"
echo "Original Tag:   ${RC_TAG}"
echo "Validated Tag:  ${VALIDATED_TAG}"
echo "======================================================================"

ensure_git_tag "${VALIDATED_TAG}" "${COMMIT_SHA}" "Validated RC ${RC_TAG} for commit ${COMMIT_SHA}"
