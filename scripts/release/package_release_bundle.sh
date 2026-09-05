#!/usr/bin/env bash
# ==============================================================================
# 📦 Kubernetes Agentic Harness (kube-agents) Release Bundle Packaging Engine
# ==============================================================================
# Packages Helm charts, web-distribution bundles (.tar.gz, .zip), generates SBOMs,
# and computes sha256 checksums atomically into DIST_DIR (default: build/dist).
#
# Usage:
#   ./scripts/release/package_release_bundle.sh <TAG_NAME>
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"

# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

TAG_NAME="${1:-${TAG_NAME:-${RELEASE_VERSION:-}}}"
if [ -z "${TAG_NAME}" ]; then
  echo "❌ ERROR: TAG_NAME must be specified as first argument or environment variable." >&2
  echo "Usage: $0 <TAG_NAME>" >&2
  exit 1
fi

validate_pure_numeric_semver "${TAG_NAME}" "Release tag" || exit 1

DIST_DIR="${DIST_DIR:-${REPO_ROOT}/build/dist}"
BUNDLE_PREFIX="kube-agents-${TAG_NAME}"

# Resolves git commit SHA strictly without ambiguous HEAD fallbacks
resolve_bundle_commit() {
  local tag="${1:-}"
  local resolved=""

  if [ -n "${TARGET_COMMIT:-}" ]; then
    if resolved="$(git -C "${REPO_ROOT}" rev-parse --verify "${TARGET_COMMIT}^{commit}" 2>/dev/null)"; then
      echo "${resolved}"
      return 0
    fi
    echo "❌ ERROR: Explicit TARGET_COMMIT '${TARGET_COMMIT}' is not a valid commit in repository!" >&2
    return 1
  fi

  if [ -n "${RELEASE_COMMIT:-}" ]; then
    if resolved="$(git -C "${REPO_ROOT}" rev-parse --verify "${RELEASE_COMMIT}^{commit}" 2>/dev/null)"; then
      echo "${resolved}"
      return 0
    fi
    echo "❌ ERROR: Explicit RELEASE_COMMIT '${RELEASE_COMMIT}' is not a valid commit in repository!" >&2
    return 1
  fi

  # Resolve release commit directly from Git tag via shared common helper
  resolve_release_commit "${tag}"
}

check_prerequisites() {
  if ! command -v tar >/dev/null 2>&1; then
    echo "❌ ERROR: 'tar' CLI is required to create distribution tarball." >&2
    exit 1
  fi

  if ! command -v zip >/dev/null 2>&1; then
    echo "❌ ERROR: 'zip' CLI is required to create distribution zip archive." >&2
    exit 1
  fi

  if ! command -v helm >/dev/null 2>&1; then
    if is_ci_pipeline; then
      echo "❌ ERROR: 'helm' CLI is mandatory in CI for packaging charts but was not found in PATH." >&2
      exit 1
    else
      echo "⚠️ WARNING: 'helm' CLI not found. Skipping Helm chart packaging." >&2
    fi
  fi

  if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
    echo "❌ ERROR: Neither 'sha256sum' nor 'shasum' CLI found in PATH!" >&2
    exit 1
  fi

  # Validate commit resolution strictly before performing any file staging
  if ! resolve_bundle_commit "${TAG_NAME}" >/dev/null; then
    exit 1
  fi
}

TMP_STAGE_DIR="$(mktemp -d)"
TMP_DIST_DIR="$(mktemp -d)"
# shellcheck disable=SC2064
trap 'rm -rf "${TMP_STAGE_DIR}" "${TMP_DIST_DIR}"' EXIT

stage_bundle_files() {
  local target_bundle_dir="${TMP_STAGE_DIR}/${BUNDLE_PREFIX}"
  mkdir -p "${target_bundle_dir}"

  local commit_sha
  commit_sha="$(resolve_bundle_commit "${TAG_NAME}")"

  echo "📁 Staging release bundle from commit ${commit_sha:0:7} into ${target_bundle_dir}..."
  local archive_paths=()
  for dir_name in "${RELEASE_BUNDLE_DIRECTORIES[@]}"; do
    if git -C "${REPO_ROOT}" cat-file -e "${commit_sha}:${dir_name}" 2>/dev/null; then
      archive_paths+=("${dir_name}")
    fi
  done
  for file_name in "${RELEASE_BUNDLE_ROOT_FILES[@]}"; do
    if git -C "${REPO_ROOT}" cat-file -e "${commit_sha}:${file_name}" 2>/dev/null; then
      archive_paths+=("${file_name}")
    fi
  done

  extract_commit_tree "${commit_sha}" "${target_bundle_dir}" ${archive_paths[@]+"${archive_paths[@]}"}

  # Ensure staged files are writable for sanitization and version stamping
  chmod -R u+w "${target_bundle_dir}"

  # Defense-in-depth: Sanitize sensitive configs, tokens, temporary files, and local caches
  find "${target_bundle_dir}" -type f \( \
    \( -name ".env*" ! -name "tags.env" \) -o \
    \( -name "install.env*" ! -name "install.env.example" \) -o \
    -name "vars.sh" -o \
    -name "*credentials*.json" -o \
    -name "service-account*.json" -o \
    -name "*.key" -o \
    -name "*.pem" -o \
    -name "*.p12" -o \
    -name "*.pfx" -o \
    -name "*.id_rsa" -o \
    \( -name "*.tfvars*" ! -name "*.tfvars.example" \) -o \
    -name "*.tfstate*" -o \
    -name "*.secret*" -o \
    -name "secrets.yaml" -o \
    -name "kubeconfig*" -o \
    -name "*.tmp" -o \
    -name "*.log" -o \
    -name ".coverage" -o \
    -name ".DS_Store" \
  \) -delete

  # Remove developer, build, and version control directories safely
  find "${target_bundle_dir}" -depth -type d \( \
    -name ".git" -o \
    -name ".github" -o \
    -name ".terraform" -o \
    -name ".kube" -o \
    -path "*/k8s-operator/bin" -o \
    -name "__pycache__" -o \
    -name ".pytest_cache" -o \
    -name ".mypy_cache" -o \
    -name ".astro" -o \
    -name "node_modules" \
  \) -exec rm -rf {} +
}

sync_bundle_versions() {
  local target_bundle_dir="${TMP_STAGE_DIR}/${BUNDLE_PREFIX}"
  echo "🏷️ Stamping baked release version and generating .release-bundle marker..."

  # Stamp release versions across installer scripts, Helm Chart.yaml, and Terraform defaults
  stamp_release_versions "${TAG_NAME}" "${target_bundle_dir}"

  local commit_sha
  commit_sha="$(resolve_bundle_commit "${TAG_NAME}")"
  local build_timestamp="${BUILD_DATE:-$(date -u +"%Y-%m-%dT%H:%M:%SZ")}"

  cat <<BUNDLE_META > "${target_bundle_dir}/.release-bundle"
name=kube-agents
version=${TAG_NAME}
tag=${TAG_NAME}
commit=${commit_sha}
build_date=${build_timestamp}
BUNDLE_META
}

package_helm_charts() {
  local target_bundle_dir="${TMP_STAGE_DIR}/${BUNDLE_PREFIX}"

  if ! command -v helm >/dev/null 2>&1; then
    return 0
  fi

  for chart_rel_path in "${RELEASE_HELM_CHARTS[@]}"; do
    local staged_chart_dir="${target_bundle_dir}/${chart_rel_path}"
    if [ -d "${staged_chart_dir}" ]; then
      echo "📦 Packaging Helm chart from staged bundle: ${chart_rel_path}..."
      helm package "${staged_chart_dir}" \
        --version "${TAG_NAME}" \
        --app-version "${TAG_NAME}" \
        --destination "${TMP_DIST_DIR}"

      local expected_pkg="${TMP_DIST_DIR}/kube-agents-${TAG_NAME}.tgz"
      if [ ! -f "${expected_pkg}" ]; then
        echo "❌ ERROR: Expected Helm chart package '${expected_pkg}' was not created!" >&2
        exit 1
      fi
    fi
  done
}

create_archives() {
  echo "🗜️ Creating canonical distribution archives (.tar.gz, .zip)..."

  tar -czf "${TMP_DIST_DIR}/${BUNDLE_PREFIX}.tar.gz" -C "${TMP_STAGE_DIR}" "${BUNDLE_PREFIX}"
  (cd "${TMP_STAGE_DIR}" && zip -rq "${TMP_DIST_DIR}/${BUNDLE_PREFIX}.zip" "${BUNDLE_PREFIX}")

  if [ ! -f "${TMP_DIST_DIR}/${BUNDLE_PREFIX}.tar.gz" ]; then
    echo "❌ ERROR: Expected distribution tarball '${TMP_DIST_DIR}/${BUNDLE_PREFIX}.tar.gz' was not created!" >&2
    exit 1
  fi
  if [ ! -f "${TMP_DIST_DIR}/${BUNDLE_PREFIX}.zip" ]; then
    echo "❌ ERROR: Expected distribution zip archive '${TMP_DIST_DIR}/${BUNDLE_PREFIX}.zip' was not created!" >&2
    exit 1
  fi
}

generate_sboms() {
  local target_bundle_dir="${TMP_STAGE_DIR}/${BUNDLE_PREFIX}"
  echo "🛡️ Invoking SBOM generation for bundle and release container images..."
  local sbom_script="${SCRIPT_DIR}/generate_release_sbom.sh"
  if [ -f "${sbom_script}" ]; then
    DIST_DIR="${TMP_DIST_DIR}" bash "${sbom_script}" "${TAG_NAME}" "${target_bundle_dir}"
  else
    echo "❌ ERROR: SBOM generation script not found at ${sbom_script}!" >&2
    exit 1
  fi
}

compute_checksums() {
  echo "🔒 Computing SHA256 checksums across all release artifacts..."
  local chk_cmd=""
  if command -v sha256sum >/dev/null 2>&1; then
    chk_cmd="sha256sum"
  elif command -v shasum >/dev/null 2>&1; then
    chk_cmd="shasum -a 256"
  else
    echo "❌ ERROR: Neither 'sha256sum' nor 'shasum' CLI found in PATH!" >&2
    exit 1
  fi

  (
    cd "${TMP_DIST_DIR}"
    rm -f checksums.txt checksums.tmp
    find . -maxdepth 1 -type f ! -name "checksums.*" -exec basename {} \; | sort | while read -r fname; do
      $chk_cmd "$fname"
    done > checksums.tmp
    mv -f checksums.tmp checksums.txt
  )

  if [ ! -s "${TMP_DIST_DIR}/checksums.txt" ]; then
    echo "❌ ERROR: Generated checksums.txt is empty or missing!" >&2
    exit 1
  fi
}

promote_artifacts() {
  echo "🚀 Promoting verified release artifacts to ${DIST_DIR}..."
  mkdir -p "${DIST_DIR}"
  for artifact in "${TMP_DIST_DIR}"/*; do
    [ -e "${artifact}" ] || continue
    mv -f "${artifact}" "${DIST_DIR}/"
  done
}

main() {
  echo "======================================================================"
  echo "📦 PACKAGING RELEASE BUNDLE ASSETS FOR ${TAG_NAME}"
  echo "Bundle Name:   ${BUNDLE_PREFIX}"
  echo "Destination:   ${DIST_DIR}"
  echo "======================================================================"

  check_prerequisites

  stage_bundle_files
  sync_bundle_versions
  package_helm_charts
  create_archives
  generate_sboms
  compute_checksums
  promote_artifacts

  echo "======================================================================"
  echo "✅ Release bundle packaging completed successfully:"
  ls -lh "${DIST_DIR}"
  echo "======================================================================"
}

main "$@"
