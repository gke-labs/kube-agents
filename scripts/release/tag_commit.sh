#!/usr/bin/env bash
# Creates and pushes one annotated Git tag on a commit, safely and idempotently.
#
# The shared body of every rung of the release ladder: print a banner naming what
# is being tagged, then call ensure_git_tag, which no-ops when the tag already
# points at the same commit and fails when it points elsewhere.
# create_release_tag.sh (rc_*), tag_validated_release.sh (_validated),
# tag_staging_promotion.sh (staging_*) and tag_ga_release.sh (GA SemVer) wrap it,
# each keeping only what is genuinely its own.
#
# Usage: tag_commit.sh [--title TITLE] [--detail "Label: value"]... <tag> <commit-sha> [message]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

TITLE="CREATING AND PUSHING GIT TAG"
DETAILS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --title | --detail)
      # Checked rather than left to `shift 2`, which under `set -e` aborts with
      # bash's own "shift count out of range" and no mention of the flag.
      if [ $# -lt 2 ]; then
        echo "❌ ERROR: '$1' needs a value." >&2
        exit 1
      fi
      if [ "$1" = "--title" ]; then
        TITLE="$2"
      else
        DETAILS+=("$2")
      fi
      shift 2
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "❌ ERROR: unknown option '$1' for tag_commit.sh." >&2
      exit 1
      ;;
    *)
      break
      ;;
  esac
done

TAG_NAME="${1:-${TAG_NAME:-}}"
COMMIT_SHA="${2:-${COMMIT_SHA:-}}"
TAG_MESSAGE="${3:-${TAG_MESSAGE:-}}"

if [ -z "${TAG_NAME}" ] || [ -z "${COMMIT_SHA}" ]; then
  echo "❌ ERROR: a tag name and a commit SHA are required." >&2
  echo "Usage: $0 [--title TITLE] [--detail 'Label: value']... <tag> <commit-sha> [message]" >&2
  exit 1
fi

TAG_MESSAGE="${TAG_MESSAGE:-${TAG_NAME} for commit ${COMMIT_SHA}}"

echo "======================================================================"
echo "🏷️ ${TITLE}"
echo "Tag:          ${TAG_NAME}"
echo "Commit SHA:   ${COMMIT_SHA}"
# bash 3.2 compatibility: guard empty array expansion under set -u.
for detail in ${DETAILS[@]+"${DETAILS[@]}"}; do
  echo "${detail}"
done
echo "======================================================================"

ensure_git_tag "${TAG_NAME}" "${COMMIT_SHA}" "${TAG_MESSAGE}"
