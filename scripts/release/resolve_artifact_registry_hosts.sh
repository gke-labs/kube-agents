#!/usr/bin/env bash
# Writes to GITHUB_OUTPUT the Artifact Registry host of each registry variable
# release-publish.yml signs in to: `candidate` for CANDIDATE_REGISTRY_PREFIX and
# `additional` for ADDITIONAL_RELEASE_REGISTRY_PREFIX. A variable that is unset
# or names another kind of registry writes nothing, and the workflow skips its
# login. A variable that looks like Artifact Registry and whose host is not one
# (is_artifact_registry_host in common.sh) fails the run: the login sends a
# Google access token to the host written here.
set -euo pipefail

# What marks a prefix as meant for Artifact Registry: its Docker host suffix,
# then the path.
readonly ARTIFACT_REGISTRY_PREFIX_MARK="-docker.pkg.dev/"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

OUTPUT_FILE="${GITHUB_OUTPUT:-/dev/stdout}"

resolve_artifact_registry_host() {
  local output_name="$1" variable_name="$2" prefix="$3"
  case "${prefix}" in
    *"${ARTIFACT_REGISTRY_PREFIX_MARK}"*) ;;
    *) return 0 ;;
  esac

  local host="${prefix%%/*}"
  if ! is_artifact_registry_host "${host}"; then
    echo "::error title=Not an Artifact Registry host::${variable_name} names the host '${host}', which is not an Artifact Registry Docker host; no login is made to it."
    return 1
  fi
  echo "${output_name}=${host}" >>"${OUTPUT_FILE}"
}

resolve_artifact_registry_host candidate CANDIDATE_REGISTRY_PREFIX "${CANDIDATE_REGISTRY_PREFIX:-}"
resolve_artifact_registry_host additional ADDITIONAL_RELEASE_REGISTRY_PREFIX "${ADDITIONAL_RELEASE_REGISTRY_PREFIX:-}"
