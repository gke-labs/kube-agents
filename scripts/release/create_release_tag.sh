#!/usr/bin/env bash
# Creates and pushes a Release Candidate Git tag for a target commit SHA safely and idempotently.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

COMMIT_SHA="${1:-${COMMIT_SHA:-}}"
RC_TAG="${2:-${RC_TAG:-}}"

if [ -z "${COMMIT_SHA}" ] || [ -z "${RC_TAG}" ]; then
  echo "❌ ERROR: COMMIT_SHA and RC_TAG are required." >&2
  exit 1
fi

exec "${SCRIPT_DIR}/tag_commit.sh" \
  --title "CREATING AND PUSHING RELEASE CANDIDATE GIT TAG" \
  "${RC_TAG}" "${COMMIT_SHA}" "Release Candidate ${RC_TAG} for commit ${COMMIT_SHA}"
