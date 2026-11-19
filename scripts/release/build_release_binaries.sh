#!/usr/bin/env bash
# Builds the standalone binaries a GA release attaches, from the release
# commit, one archive per binary and platform: <name>_<X.Y.Z>_<os>_<arch>.tar.gz
# (.zip on Windows) holding a top-level <name>_<X.Y.Z>_<os>_<arch>/ directory
# with the binary, LICENSE and a README.md. package_release_bundle.sh runs it
# into its staging directory, so checksums.txt (and its signature) and the
# GitHub Release cover the archives like every other asset.
#
# A required binary (k8s-api-proxy) fails the script on any build or archive
# error. An optional one (kube-agents-mcp) emits a GitHub Actions warning
# instead, removes every archive of its own from DIST_DIR, so checksums.txt
# never lists a missing or partial file, and the release publishes without it.
#
# Usage: ./scripts/release/build_release_binaries.sh <TAG_NAME>
#   DIST_DIR           where the archives go (default: build/dist)
#   RELEASE_COMMIT     the commit to build (default: the commit the tag points to)
#   RELEASE_REPO_ROOT  the repository to build from (default: this checkout)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

# common.sh points REPO_ROOT at this script's checkout. A variable of its own
# rather than an inherited REPO_ROOT, which every other release script ignores.
REPO_ROOT="${RELEASE_REPO_ROOT:-${REPO_ROOT}}"

readonly FAILURE_MODE_REQUIRED="required"
readonly FAILURE_MODE_OPTIONAL="optional"
readonly SPEC_FIELD_SEPARATOR="|"
# One spec per binary, its fields separated by SPEC_FIELD_SEPARATOR:
#   name | Go module dir | main package within it | README copied beside the
#   binary (relative to the repository root) | failure mode | GOOS/GOARCH pairs
readonly RELEASE_BINARY_SPECS=(
  # The Linux pair a host agent or a container runs on, and the macOS pair a
  # workstation runs on.
  "k8s-api-proxy|k8s-api-proxy|./cmd/k8s-api-proxy|k8s-api-proxy/README.md|${FAILURE_MODE_REQUIRED}|linux/amd64 linux/arm64 darwin/amd64 darwin/arm64"
  # The workstation MCP bridge, for wherever an MCP host runs. Neither its
  # command nor the a2a module has a README, so it carries the repository's.
  "kube-agents-mcp|a2a|./cmd/kube-agents-mcp|README.md|${FAILURE_MODE_OPTIONAL}|darwin/amd64 darwin/arm64 linux/amd64 linux/arm64 windows/amd64"
)
# The same flags deploy/kube-proxy/Dockerfile builds the image's binary with,
# plus -trimpath so the binary carries no runner path.
readonly BINARY_LDFLAGS="-s -w"
# The variable the release version is stamped into, which --version prints.
readonly BINARY_VERSION_SYMBOL="main.version"
# The toolchain already installed, as the Dockerfile sets: a release commit
# needing a newer one fails rather than downloads one mid-release.
readonly BINARY_GOTOOLCHAIN="local"
# Copied beside the binary in every archive, relative to the repository root.
readonly LICENSE_FILE="LICENSE"
# Windows gets a .exe binary in a .zip; every other platform a .tar.gz.
readonly WINDOWS_GOOS="windows"
readonly WINDOWS_BINARY_SUFFIX=".exe"
readonly TAR_ARCHIVE_SUFFIX=".tar.gz"
readonly ZIP_ARCHIVE_SUFFIX=".zip"

TAG_NAME="${1:-${TAG_NAME:-${RELEASE_VERSION:-}}}"
if [ -z "${TAG_NAME}" ]; then
  echo "❌ ERROR: TAG_NAME must be specified as first argument or environment variable." >&2
  echo "Usage: $0 <TAG_NAME>" >&2
  exit 1
fi

validate_pure_numeric_semver "${TAG_NAME}" "Release tag" || exit 1

DIST_DIR="${DIST_DIR:-${REPO_ROOT}/build/dist}"

if [ -n "${RELEASE_COMMIT:-}" ]; then
  if ! commit_sha="$(git -C "${REPO_ROOT}" rev-parse --verify "${RELEASE_COMMIT}^{commit}" 2>/dev/null)"; then
    echo "❌ ERROR: RELEASE_COMMIT '${RELEASE_COMMIT}' is not a valid commit in repository!" >&2
    exit 1
  fi
else
  commit_sha="$(cd "${REPO_ROOT}" && resolve_release_commit "${TAG_NAME}")"
fi

# Splits a spec into the spec_* variables.
parse_spec() {
  IFS="${SPEC_FIELD_SEPARATOR}" read -r spec_name spec_module_dir spec_main_package spec_readme spec_failure_mode spec_platforms <<<"$1"
}

# A patch on a release line cut before a binary's main package existed (the
# k8s-api-proxy module landed in 0.21, cmd/k8s-api-proxy in 0.24) ships no
# such binary. Skipped rather than failed: the GA tag is already public when
# the bundle is built.
present_specs=()
for spec in "${RELEASE_BINARY_SPECS[@]}"; do
  parse_spec "${spec}"
  main_package_path="${spec_module_dir}/${spec_main_package#./}"
  if ! git -C "${REPO_ROOT}" cat-file -e "${commit_sha}:${main_package_path}" 2>/dev/null; then
    echo "ℹ️ ${main_package_path} does not exist at ${commit_sha:0:7}; no ${spec_name} binaries for ${TAG_NAME}."
    continue
  fi
  present_specs+=("${spec}")
done
if [ "${#present_specs[@]}" -eq 0 ]; then
  exit 0
fi

if ! command -v go >/dev/null 2>&1; then
  if is_ci_pipeline; then
    echo "❌ ERROR: 'go' is mandatory in CI for building release binaries but was not found in PATH." >&2
    exit 1
  fi
  echo "⚠️ WARNING: 'go' not found. Skipping release binaries." >&2
  exit 0
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

mkdir -p "${DIST_DIR}"
# Absolute, since the zip step writes from inside WORK_DIR.
DIST_DIR="$(cd "${DIST_DIR}" && pwd)"

# What build_binary_archives was doing when it failed, for the warning.
failed_step=""

# Prints the archive file name (without directory) for a binary on a platform.
archive_file_name() {
  local name="$1" goos="$2" goarch="$3"
  local suffix="${TAR_ARCHIVE_SUFFIX}"
  if [ "${goos}" = "${WINDOWS_GOOS}" ]; then
    suffix="${ZIP_ARCHIVE_SUFFIX}"
  fi
  echo "${name}_${TAG_NAME}_${goos}_${goarch}${suffix}"
}

# Builds and archives one binary for each of its platforms. Every step checks
# its own status, since a caller testing the return value turns off set -e.
build_binary_archives() {
  local name="$1" module_dir="$2" main_package="$3" readme="$4"
  local platforms=()
  read -r -a platforms <<<"$5"
  local source_dir="${WORK_DIR}/src/${name}"

  failed_step="extracting ${module_dir} at ${commit_sha:0:7}"
  extract_commit_tree "${commit_sha}" "${source_dir}" "${module_dir}" "${LICENSE_FILE}" "${readme}" || return 1

  echo "======================================================================"
  echo "🔨 BUILDING ${name} RELEASE BINARIES FOR ${TAG_NAME}"
  echo "Commit SHA:   ${commit_sha}"
  echo "Destination:  ${DIST_DIR}"
  echo "======================================================================"

  local platform goos goarch archive_root stage binary_file archive_file
  for platform in "${platforms[@]}"; do
    goos="${platform%/*}"
    goarch="${platform#*/}"
    failed_step="${goos}/${goarch}"
    archive_root="${name}_${TAG_NAME}_${goos}_${goarch}"
    stage="${WORK_DIR}/${archive_root}"
    binary_file="${name}"
    if [ "${goos}" = "${WINDOWS_GOOS}" ]; then
      binary_file="${name}${WINDOWS_BINARY_SUFFIX}"
    fi
    archive_file="$(archive_file_name "${name}" "${goos}" "${goarch}")"
    mkdir -p "${stage}" || return 1

    echo "==> ${goos}/${goarch}"
    if ! (
      cd "${source_dir}/${module_dir}" &&
        CGO_ENABLED=0 GOOS="${goos}" GOARCH="${goarch}" GOFLAGS=-mod=readonly GOTOOLCHAIN="${BINARY_GOTOOLCHAIN}" \
          go build -trimpath -ldflags="${BINARY_LDFLAGS} -X ${BINARY_VERSION_SYMBOL}=${TAG_NAME}" -o "${stage}/${binary_file}" "${main_package}"
    ); then
      echo "❌ ERROR: go build failed for ${name} on ${goos}/${goarch}." >&2
      return 1
    fi
    if [ ! -f "${stage}/${binary_file}" ]; then
      echo "❌ ERROR: go build produced no ${name} for ${goos}/${goarch}." >&2
      return 1
    fi
    cp "${source_dir}/${LICENSE_FILE}" "${source_dir}/${readme}" "${stage}/" || return 1
    if [ "${goos}" = "${WINDOWS_GOOS}" ]; then
      # -X leaves out the owner and extra attributes of the build host.
      (cd "${WORK_DIR}" && zip -q -r -X "${DIST_DIR}/${archive_file}" "${archive_root}") || return 1
    else
      # COPYFILE_DISABLE keeps macOS tar from adding AppleDouble (._*) entries.
      COPYFILE_DISABLE=1 tar -czf "${DIST_DIR}/${archive_file}" -C "${WORK_DIR}" "${archive_root}" || return 1
    fi
  done

  echo "✅ Built ${#platforms[@]} ${name} archives into ${DIST_DIR}."
}

# Removes every archive an optional binary could have written for this tag, so
# none of a half-built set reaches checksums.txt or the release.
remove_binary_archives() {
  local name="$1"
  local platforms=()
  read -r -a platforms <<<"$2"
  local platform
  for platform in "${platforms[@]}"; do
    rm -f "${DIST_DIR}/$(archive_file_name "${name}" "${platform%/*}" "${platform#*/}")"
  done
}

for spec in "${present_specs[@]}"; do
  parse_spec "${spec}"
  if build_binary_archives "${spec_name}" "${spec_module_dir}" "${spec_main_package}" "${spec_readme}" "${spec_platforms}"; then
    continue
  fi
  if [ "${spec_failure_mode}" != "${FAILURE_MODE_OPTIONAL}" ]; then
    exit 1
  fi
  remove_binary_archives "${spec_name}" "${spec_platforms}"
  echo "::warning::${spec_name} failed to build or package (${failed_step}); the release publishes without ${spec_name} archives."
done
