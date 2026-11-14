#!/usr/bin/env bash
# Tags each published Go module at the commit a GA release tag points to, so
# `go get <module>@v<version>` resolves to the code that release shipped.
# A module in a subdirectory is tagged `<dir>/v<version>`: Go requires the
# directory prefix and the `v`, which the GA tag (pure numeric SemVer) has neither of.
# Runs after tag_ga_release.sh, which creates the GA tag this script reads.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

# Directories holding a go.mod whose module is released with the GA version.
# A module listed here must carry no `replace` directive
# (tests/test_tag_go_modules.py checks it): `go get` ignores a dependency's
# replaces, so a consumer would build against something the module never tested.
readonly PUBLISHED_GO_MODULE_DIRS=(
  "k8s-api-proxy"
)
readonly GO_MODULE_TAG_VERSION_PREFIX="v"
# What each module's path must be: Go resolves `<dir>/vX.Y.Z` only for this.
readonly GO_MODULE_PATH_PREFIX="github.com/gke-labs/kube-agents/"
# The highest major version tagged. The module is v0 whatever the product's
# version: at product 1.0 a v1 tag would promise compatibility nobody has
# decided on, and at 2.0 Go rejects a v2 tag on a path without /v2.
readonly MAX_TAGGED_GO_MODULE_MAJOR=0
readonly DEFAULT_GO_MODULE_PROXY_URL="https://proxy.golang.org"
# How long to wait for the module proxy to fetch a new tag. It fetches on first
# request anyway; asking now spares the first consumer the wait.
readonly GO_MODULE_PROXY_TIMEOUT_SECONDS=60

RELEASE_VERSION="${1:-${RELEASE_VERSION:-}}"
# Set empty to skip the proxy request (the tests do, to stay off the network).
GO_MODULE_PROXY_URL="${GO_MODULE_PROXY_URL-${DEFAULT_GO_MODULE_PROXY_URL}}"

if [ -z "${RELEASE_VERSION}" ]; then
  echo "❌ ERROR: RELEASE_VERSION is required as an argument or environment variable." >&2
  echo "Usage: $0 <RELEASE_VERSION> (the GA tag must already exist)" >&2
  exit 1
fi

validate_pure_numeric_semver "${RELEASE_VERSION}" "Release version" || exit 1

release_fetch_tags

if ! RELEASE_COMMIT="$(git rev-parse --verify --quiet "refs/tags/${RELEASE_VERSION}^{commit}")"; then
  echo "❌ ERROR: GA tag '${RELEASE_VERSION}' does not exist; run tag_ga_release.sh first." >&2
  exit 1
fi

release_major="${RELEASE_VERSION%%.*}"
if [ "${release_major}" -gt "${MAX_TAGGED_GO_MODULE_MAJOR}" ]; then
  echo "::warning::Release ${RELEASE_VERSION} is past major ${MAX_TAGGED_GO_MODULE_MAJOR}; no Go module is tagged until the modules' own versioning is decided."
  exit 0
fi

# Asks the module proxy for the new version. A failure is reported, not fatal:
# the tag is what consumers resolve, and the proxy fetches it on first request.
warm_go_module_proxy() {
  local module_path="$1" version="$2"
  local url="${GO_MODULE_PROXY_URL}/${module_path}/@v/${version}.info"
  if curl -fsS --max-time "${GO_MODULE_PROXY_TIMEOUT_SECONDS}" "${url}" >/dev/null; then
    echo "✅ ${GO_MODULE_PROXY_URL} serves ${module_path}@${version}."
  else
    echo "⚠️ ${GO_MODULE_PROXY_URL} did not serve ${module_path}@${version} yet; it fetches the tag on first request." >&2
  fi
}

for module_dir in "${PUBLISHED_GO_MODULE_DIRS[@]}"; do
  # A patch on a release line cut before the module existed has nothing to
  # tag. Skipped rather than failed: the GA tag is already pushed, and a
  # failure here would hold that release short of its GitHub Release on every re-run.
  if ! go_mod="$(git show "${RELEASE_COMMIT}:${module_dir}/go.mod" 2>/dev/null)"; then
    echo "ℹ️ ${module_dir}/go.mod does not exist at release commit ${RELEASE_COMMIT:0:7}; not tagging it."
    continue
  fi
  module_path="$(awk '$1 == "module" { print $2; exit }' <<<"${go_mod}")"
  if [ "${module_path}" != "${GO_MODULE_PATH_PREFIX}${module_dir}" ]; then
    echo "::warning::${module_dir}/go.mod at ${RELEASE_COMMIT:0:7} declares module '${module_path}', not '${GO_MODULE_PATH_PREFIX}${module_dir}'; not tagging it."
    continue
  fi
  module_version="${GO_MODULE_TAG_VERSION_PREFIX}${RELEASE_VERSION}"
  module_tag="${module_dir}/${module_version}"

  echo "======================================================================"
  echo "🏷️ TAGGING GO MODULE"
  echo "Module:       ${module_path}"
  echo "Tag:          ${module_tag}"
  echo "Commit SHA:   ${RELEASE_COMMIT}"
  echo "======================================================================"
  ensure_git_tag "${module_tag}" "${RELEASE_COMMIT}" "Go module ${module_path} ${module_version}"

  if is_ci_pipeline && [ -n "${GO_MODULE_PROXY_URL}" ]; then
    warm_go_module_proxy "${module_path}" "${module_version}"
  fi
done
