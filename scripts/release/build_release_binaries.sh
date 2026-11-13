#!/usr/bin/env bash
# Builds the standalone binaries a GA release attaches, from the release
# commit, one archive per platform: k8s-api-proxy_<X.Y.Z>_<os>_<arch>.tar.gz
# holding the binary, LICENSE and README.md. package_release_bundle.sh runs it
# into its staging directory, so checksums.txt (and its signature) and the
# GitHub Release cover the archives like every other asset.
#
# Usage: ./scripts/release/build_release_binaries.sh <TAG_NAME>
#   DIST_DIR        where the archives go (default: build/dist)
#   RELEASE_COMMIT  the commit to build (default: the commit the tag points to)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CALLER_REPO_ROOT="${REPO_ROOT:-}"

# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

# common.sh points REPO_ROOT at this script's checkout; a caller's REPO_ROOT,
# naming the repository to build from, wins over it.
REPO_ROOT="${CALLER_REPO_ROOT:-${REPO_ROOT}}"

# The Go module each binary is built from, and its main package within it.
readonly BINARY_NAME="k8s-api-proxy"
readonly BINARY_MODULE_DIR="k8s-api-proxy"
readonly BINARY_MAIN_PACKAGE="./cmd/k8s-api-proxy"
# GOOS/GOARCH pairs: the Linux pair a host agent or a container runs on, and
# the macOS pair a workstation runs on.
readonly BINARY_PLATFORMS=(
  "linux/amd64"
  "linux/arm64"
  "darwin/amd64"
  "darwin/arm64"
)
# The same flags deploy/kube-proxy/Dockerfile builds the image's binary with,
# plus -trimpath so the binary carries no runner path.
readonly BINARY_LDFLAGS="-s -w"
# Files copied beside the binary in each archive, relative to the repository root.
readonly BINARY_ARCHIVE_FILES=(
  "LICENSE"
  "k8s-api-proxy/README.md"
)

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

if ! git -C "${REPO_ROOT}" cat-file -e "${commit_sha}:${BINARY_MODULE_DIR}/go.mod" 2>/dev/null; then
  # A patch on a release line cut before the module existed ships no binary.
  echo "ℹ️ ${BINARY_MODULE_DIR}/go.mod does not exist at ${commit_sha:0:7}; no ${BINARY_NAME} binaries for ${TAG_NAME}."
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

source_dir="${WORK_DIR}/src"
extract_commit_tree "${commit_sha}" "${source_dir}" "${BINARY_MODULE_DIR}" "${BINARY_ARCHIVE_FILES[@]}"

mkdir -p "${DIST_DIR}"
echo "======================================================================"
echo "🔨 BUILDING ${BINARY_NAME} RELEASE BINARIES FOR ${TAG_NAME}"
echo "Commit SHA:   ${commit_sha}"
echo "Destination:  ${DIST_DIR}"
echo "======================================================================"

for platform in "${BINARY_PLATFORMS[@]}"; do
  goos="${platform%/*}"
  goarch="${platform#*/}"
  archive_root="${BINARY_NAME}_${TAG_NAME}_${goos}_${goarch}"
  stage="${WORK_DIR}/${archive_root}"
  mkdir -p "${stage}"

  echo "==> ${goos}/${goarch}"
  (
    cd "${source_dir}/${BINARY_MODULE_DIR}"
    CGO_ENABLED=0 GOOS="${goos}" GOARCH="${goarch}" GOFLAGS=-mod=readonly \
      go build -trimpath -ldflags="${BINARY_LDFLAGS}" -o "${stage}/${BINARY_NAME}" "${BINARY_MAIN_PACKAGE}"
  )
  if [ ! -f "${stage}/${BINARY_NAME}" ]; then
    echo "❌ ERROR: go build produced no ${BINARY_NAME} for ${goos}/${goarch}." >&2
    exit 1
  fi
  for file in "${BINARY_ARCHIVE_FILES[@]}"; do
    cp "${source_dir}/${file}" "${stage}/"
  done
  tar -czf "${DIST_DIR}/${archive_root}.tar.gz" -C "${WORK_DIR}" "${archive_root}"
done

echo "✅ Built ${#BINARY_PLATFORMS[@]} ${BINARY_NAME} archives into ${DIST_DIR}."
