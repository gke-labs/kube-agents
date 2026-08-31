#!/usr/bin/env bash
# Attaches an '_validated' Git tag to a verified Release Candidate commit safely and idempotently.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

COMMIT_SHA="${1:-${COMMIT_SHA:-}}"
RC_TAG="${2:-${RC_TAG:-}}"

if [ -z "${COMMIT_SHA}" ] || [ -z "${RC_TAG}" ]; then
  echo "❌ ERROR: COMMIT_SHA and RC_TAG are required." >&2
  exit 1
fi

VALIDATED_TAG="${RC_TAG}_validated"

exec "${SCRIPT_DIR}/tag_commit.sh" \
  --title "MARKING RELEASE CANDIDATE COMMIT AS VALIDATED" \
  --detail "Original Tag: ${RC_TAG}" \
  "${VALIDATED_TAG}" "${COMMIT_SHA}" "Validated RC ${RC_TAG} for commit ${COMMIT_SHA}"
