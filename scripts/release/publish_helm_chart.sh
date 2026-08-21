#!/usr/bin/env bash
# Packages, publishes, and signs the official kube-agents Helm chart to GHCR as an OCI artifact.
# Releases strictly use pure numeric SemVer without 'v' prefix (e.g. 0.1.0, 0.2.0).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

RELEASE_VERSION="${1:-${RELEASE_VERSION:-}}"
RELEASE_COMMIT="${2:-${RELEASE_COMMIT:-}}"

if [ -z "${RELEASE_VERSION}" ]; then
  echo "❌ ERROR: RELEASE_VERSION is required as first argument or environment variable." >&2
  echo "Usage: $0 (with RELEASE_VERSION in env) or $0 <RELEASE_VERSION> [RELEASE_COMMIT]" >&2
  exit 1
fi

validate_pure_numeric_semver "${RELEASE_VERSION}" "Release version" || exit 1

if ! command -v helm >/dev/null 2>&1; then
  if is_ci_pipeline; then
    echo "❌ ERROR: 'helm' CLI is mandatory in CI for packaging charts but was not found in PATH." >&2
    exit 1
  else
    echo "⚠️ WARNING: 'helm' CLI not found in PATH. Skipping local chart publication." >&2
    exit 0
  fi
fi

if ! command -v cosign >/dev/null 2>&1; then
  if is_ci_pipeline; then
    echo "❌ ERROR: 'cosign' CLI is mandatory in CI for signing Helm charts but was not found in PATH." >&2
    exit 1
  fi
fi

CHART_DIR="${REPO_ROOT}/charts/kube-agents"
TMP_EXTRACT_DIR=""

if [ -n "${RELEASE_COMMIT}" ]; then
  if RESOLVED_COMMIT="$(git rev-parse --verify "${RELEASE_COMMIT}^{commit}" 2>/dev/null)"; then
    echo "🔍 Extracting Helm chart from release commit ${RESOLVED_COMMIT:0:7}..."
    TMP_EXTRACT_DIR="$(mktemp -d)"
    if ! git archive "${RESOLVED_COMMIT}" charts/kube-agents | tar -x -C "${TMP_EXTRACT_DIR}"; then
      echo "❌ ERROR: Failed to extract charts/kube-agents from commit ${RESOLVED_COMMIT:0:7}!" >&2
      exit 1
    fi
    CHART_DIR="${TMP_EXTRACT_DIR}/charts/kube-agents"
  else
    echo "❌ ERROR: Cannot resolve valid Git commit for Helm chart packaging: ${RELEASE_COMMIT}" >&2
    exit 1
  fi
fi

if [ ! -d "${CHART_DIR}" ]; then
  echo "❌ ERROR: Helm chart directory '${CHART_DIR}' not found!" >&2
  exit 1
fi

echo "🔍 Linting Helm chart at ${CHART_DIR}..."
helm lint "${CHART_DIR}" \
  --set platformAgent.harness.clusterName=ci-cluster \
  --set platformAgent.harness.location=us-central1 \
  --set platformAgent.harness.projectId=ci-project

TMP_CHART_DIR="$(mktemp -d)"
# shellcheck disable=SC2064
trap 'rm -rf "${TMP_CHART_DIR}" ${TMP_EXTRACT_DIR:+"${TMP_EXTRACT_DIR}"}' EXIT

echo "📦 Packaging Helm chart version ${RELEASE_VERSION}..."
if ! helm package "${CHART_DIR}" --version "${RELEASE_VERSION}" --app-version "${RELEASE_VERSION}" --destination "${TMP_CHART_DIR}"; then
  echo "❌ ERROR: Failed to package Helm chart ${CHART_DIR}!" >&2
  exit 1
fi

local_package="${TMP_CHART_DIR}/kube-agents-${RELEASE_VERSION}.tgz"
if [ ! -f "${local_package}" ]; then
  echo "❌ ERROR: Expected Helm package archive '${local_package}' was not created!" >&2
  exit 1
fi

REGISTRY_PREFIX="$(get_registry_prefix)"
REGISTRY_HOST="${REGISTRY_PREFIX%%/*}"
CHART_OCI_DEST="oci://${REGISTRY_PREFIX}/charts"

# Safety Guard: Remote chart push and cosign signing executes exclusively inside CI
if ! is_ci_pipeline; then
  echo "⚠️ [Local Execution] Dry-run: Helm chart packaged at ${local_package}. Remote push to ${CHART_OCI_DEST} and signing skipped (runs only in CI)."
  exit 0
fi

CHART_TAG_REF="${REGISTRY_PREFIX}/charts/kube-agents:${RELEASE_VERSION}"
CHART_DIGEST=""

# Safety Guard: Check if target chart OCI package already exists in registry to prevent duplicate push
if CHART_DIGEST="$(get_image_manifest_digest "${CHART_TAG_REF}" 2>/dev/null)" && [ -n "${CHART_DIGEST}" ]; then
  echo "    ℹ️ Helm chart '${CHART_TAG_REF}' already exists in registry (digest: ${CHART_DIGEST}). Skipping duplicate push."
elif command -v docker >/dev/null 2>&1 && docker manifest inspect "${CHART_TAG_REF}" >/dev/null 2>&1; then
  echo "    ℹ️ Helm chart '${CHART_TAG_REF}' already exists in registry. Skipping duplicate push."
else
  # Securely authenticate with registry if credentials are provided in the environment
  if [ -n "${GH_TOKEN:-}" ]; then
    AUTH_ACTOR="${GH_USER:-${GITHUB_ACTOR:-oauth2}}"
    echo "🔑 Logging in to ${REGISTRY_HOST} via Helm..."
    if ! printf '%s' "${GH_TOKEN}" | helm registry login "${REGISTRY_HOST}" -u "${AUTH_ACTOR}" --password-stdin >/dev/null 2>&1; then
      echo "❌ ERROR: Failed to authenticate to ${REGISTRY_HOST} with Helm!" >&2
      exit 1
    fi
    echo "✅ Successfully logged in to ${REGISTRY_HOST} via Helm."
  fi

  echo "======================================================================"
  echo "📦 PUBLISHING AND SIGNING HELM CHART (OCI)"
  echo "Release Version: ${RELEASE_VERSION}"
  echo "OCI Destination: ${CHART_OCI_DEST}"
  echo "======================================================================"

  if ! PUSH_OUTPUT=$(helm push "${local_package}" "${CHART_OCI_DEST}" 2>&1); then
    echo "${PUSH_OUTPUT}" >&2
    echo "❌ ERROR: Failed to push Helm chart to ${CHART_OCI_DEST}!" >&2
    exit 1
  fi
  echo "${PUSH_OUTPUT}"

  CHART_DIGEST=$(echo "${PUSH_OUTPUT}" | awk '/^Digest:/ {print $2}')
  if [ -z "${CHART_DIGEST}" ]; then
    echo "❌ ERROR: Could not extract chart digest from helm push output!" >&2
    exit 1
  fi
fi

if command -v cosign >/dev/null 2>&1; then
  SIGN_TARGET="${REGISTRY_PREFIX}/charts/kube-agents${CHART_DIGEST:+@${CHART_DIGEST}}"
  if [ -z "${CHART_DIGEST}" ]; then
    SIGN_TARGET="${CHART_TAG_REF}"
  fi
  echo "🛡️ Signing Helm chart with Cosign (${SIGN_TARGET})..."
  if ! cosign sign --yes "${SIGN_TARGET}"; then
    echo "❌ ERROR: Failed to sign Helm chart ${SIGN_TARGET} with cosign!" >&2
    exit 1
  fi
  echo "✅ Successfully signed Helm chart digest ${CHART_DIGEST:-${SIGN_TARGET}}."
fi

echo "✅ Successfully published Helm chart ${RELEASE_VERSION} to ${CHART_OCI_DEST}."
