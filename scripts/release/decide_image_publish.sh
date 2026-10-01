#!/usr/bin/env bash
# Decides whether a push should build and publish the SHA-tagged images, for
# docker-publish-ghcr.yml. Writes `build` (true|false) and `reason` to
# GITHUB_OUTPUT and prints them.
#
# `main` always builds, as it always has. A release branch builds only when
# both of these hold, each one closing a way the publish could do harm:
#
#   - the push came from the merger (Tide), so a mistaken or hand-made push
#     under a release-branch name publishes nothing. This is a guard against
#     accident, not a security boundary: the workflow and this script run from
#     the pushed commit, so a collaborator who edits them in the same push is
#     not stopped here. What bounds that is who may push at all, a repository
#     rule, and it holds for any branch name, not only these. It is also what
#     keeps the GA tagger's stamped release commit, whose images nothing
#     deploys, from building: the tagger pushes it as the release App, not as
#     Tide, and a stamp is never a pull request, so no subject test is needed
#     (Tide's squash subjects end in "(#<number>)" in any case);
#   - the commit has no published images yet. Image tags are mutable, and a
#     line opened at, or fast-forwarded to, a commit `main` already built would
#     otherwise rebuild it under the same `:<sha>` tags, replacing the manifests
#     its validation tags were earned against with a build no gate has seen.
#     The registry is asked per image with ghcr_image_status; if it cannot be
#     asked, this script fails rather than guess, so nothing builds.
#
# A push that builds nothing is a green run: the reason is printed, written to
# the step summary and raised as a workflow notice, so it is not only in the log.
#
# Inputs (environment): GITHUB_REF, GITHUB_SHA, GITHUB_ACTOR.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

# The identity that pushes merges: Tide, through the google-oss-prow GitHub App.
readonly RELEASE_MERGE_ACTOR="google-oss-prow[bot]"
readonly MAIN_REF="${GIT_BRANCH_REF_PREFIX}${RELEASE_MAIN_BRANCH}"

REF="${GITHUB_REF:-}"
SHA="${GITHUB_SHA:-}"
ACTOR="${GITHUB_ACTOR:-}"

if [ -z "${REF}" ] || [ -z "${SHA}" ]; then
  echo "❌ ERROR: GITHUB_REF and GITHUB_SHA are required." >&2
  exit 1
fi

decide() {
  if [ "${REF}" = "${MAIN_REF}" ]; then
    echo "true" "a push to ${RELEASE_MAIN_BRANCH} always builds"
    return
  fi
  if [ "${ACTOR}" != "${RELEASE_MERGE_ACTOR}" ]; then
    echo "false" "only merges pushed by ${RELEASE_MERGE_ACTOR} build on a release branch; this push is by '${ACTOR}'"
    return
  fi
  local registry_prefix img status present=0
  registry_prefix="$(get_registry_prefix)"
  for img in "${REQUIRED_RELEASE_IMAGES[@]}"; do
    status="$(ghcr_image_status "${registry_prefix}/${img}:${SHA}")"
    case "${status}" in
      present) present=$((present + 1)) ;;
      absent) ;;
      *)
        echo "❌ ERROR: could not ask ${registry_prefix} whether ${img}:${SHA:0:7} exists; refusing to build rather than risk replacing published images." >&2
        exit 1
        ;;
    esac
  done
  if [ "${present}" -eq "${#REQUIRED_RELEASE_IMAGES[@]}" ]; then
    echo "false" "images for ${SHA:0:7} already exist; a rebuild would replace manifests the release ladder validated"
    return
  fi
  echo "true" "a merge onto a release branch whose commit has no images yet"
}

# Captured through an assignment, not read straight from the substitution: an
# `exit` inside `$(decide)` ends only that subshell, and a plain here-string
# would carry on with an empty decision.
if ! DECISION="$(decide)"; then
  exit 1
fi
read -r BUILD REASON <<<"${DECISION}"

echo "==> build=${BUILD}: ${REASON}"
if [ "${BUILD}" != "true" ]; then
  echo "::notice::Nothing published for ${REF#"${GIT_BRANCH_REF_PREFIX}"}@${SHA:0:7}: ${REASON}"
fi
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  echo "**Image publish for \`${REF#"${GIT_BRANCH_REF_PREFIX}"}\` @ \`${SHA:0:7}\`:** build=${BUILD} — ${REASON}" >>"${GITHUB_STEP_SUMMARY}"
fi
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "build=${BUILD}"
    echo "reason=${REASON}"
  } >>"${GITHUB_OUTPUT}"
fi
