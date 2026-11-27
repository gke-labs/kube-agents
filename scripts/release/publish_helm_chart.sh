#!/usr/bin/env bash
# Packages, publishes, and signs the official kube-agents Helm chart as an OCI artifact, in each
# registry the release is published to (release_registry_prefixes in common.sh): one package,
# pushed to each.
# Releases strictly use pure numeric SemVer without 'v' prefix (e.g. 0.1.0, 0.2.0).
set -euo pipefail

# The user name Artifact Registry takes with an OAuth access token as the password.
readonly ACCESS_TOKEN_REGISTRY_USER="oauth2accesstoken"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

RELEASE_VERSION="${1:-${RELEASE_VERSION:-${TARGET_VERSION:-${TARGET_TAG:-}}}}"

if [ -z "${RELEASE_VERSION}" ]; then
  echo "❌ ERROR: RELEASE_VERSION is required as first argument or environment variable." >&2
  echo "Usage: $0 <RELEASE_VERSION>" >&2
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

# Resolve release commit directly from Git tag (using shared common helper)
RELEASE_COMMIT="$(resolve_release_commit "${RELEASE_VERSION}")"

echo "🔍 Extracting Helm chart from release tag '${RELEASE_VERSION}' (commit ${RELEASE_COMMIT:0:7})..."
TMP_EXTRACT_DIR="$(mktemp -d)"
extract_commit_tree "${RELEASE_COMMIT}" "${TMP_EXTRACT_DIR}" charts/kube-agents
CHART_DIR="${TMP_EXTRACT_DIR}/charts/kube-agents"

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
CHART_OCI_DEST="oci://${REGISTRY_PREFIX}/charts"

# Safety Guard: Remote chart push and cosign signing executes exclusively inside CI
if ! is_ci_pipeline; then
  echo "⚠️ [Local Execution] Dry-run: Helm chart packaged at ${local_package}. Remote push to ${CHART_OCI_DEST} and signing skipped (runs only in CI)."
  exit 0
fi

# Pushes the packaged chart to one registry and signs it there. A chart already
# under the version is not pushed again and is still signed. The login is
# skipped when no credential is given, for a registry the caller is already
# logged in to.
publish_chart_to_registry() {
  local registry_prefix="$1" login_user="$2" login_token="$3"
  local registry_host="${registry_prefix%%/*}"
  local chart_oci_dest="oci://${registry_prefix}/charts"
  local chart_tag_ref="${registry_prefix}/charts/kube-agents:${RELEASE_VERSION}"
  local chart_digest="" push_output="" sign_target=""

  # Safety Guard: Check if target chart OCI package already exists in registry to prevent duplicate push
  if chart_digest="$(get_image_manifest_digest "${chart_tag_ref}" 2>/dev/null)" && [ -n "${chart_digest}" ]; then
    echo "    ℹ️ Helm chart '${chart_tag_ref}' already exists in registry (digest: ${chart_digest}). Skipping duplicate push."
  elif command -v docker >/dev/null 2>&1 && docker manifest inspect "${chart_tag_ref}" >/dev/null 2>&1; then
    echo "    ℹ️ Helm chart '${chart_tag_ref}' already exists in registry. Skipping duplicate push."
  else
    # Securely authenticate with registry if credentials are provided in the environment
    if [ -n "${login_token}" ]; then
      echo "🔑 Logging in to ${registry_host} via Helm..."
      if ! printf '%s' "${login_token}" | helm registry login "${registry_host}" -u "${login_user}" --password-stdin >/dev/null 2>&1; then
        echo "❌ ERROR: Failed to authenticate to ${registry_host} with Helm!" >&2
        return 1
      fi
      echo "✅ Successfully logged in to ${registry_host} via Helm."
    fi

    echo "======================================================================"
    echo "📦 PUBLISHING AND SIGNING HELM CHART (OCI)"
    echo "Release Version: ${RELEASE_VERSION}"
    echo "OCI Destination: ${chart_oci_dest}"
    echo "======================================================================"

    if ! push_output=$(helm push "${local_package}" "${chart_oci_dest}" 2>&1); then
      echo "${push_output}" >&2
      echo "❌ ERROR: Failed to push Helm chart to ${chart_oci_dest}!" >&2
      return 1
    fi
    echo "${push_output}"

    chart_digest=$(echo "${push_output}" | awk '/^Digest:/ {print $2}')
    if [ -z "${chart_digest}" ]; then
      echo "❌ ERROR: Could not extract chart digest from helm push output!" >&2
      return 1
    fi
  fi

  if command -v cosign >/dev/null 2>&1; then
    sign_target="${registry_prefix}/charts/kube-agents${chart_digest:+@${chart_digest}}"
    if [ -z "${chart_digest}" ]; then
      sign_target="${chart_tag_ref}"
    fi
    echo "🛡️ Signing Helm chart with Cosign (${sign_target})..."
    if ! cosign sign --yes "${sign_target}"; then
      echo "❌ ERROR: Failed to sign Helm chart ${sign_target} with cosign!" >&2
      return 1
    fi
    echo "✅ Successfully signed Helm chart digest ${chart_digest:-${sign_target}}."
  fi

  echo "✅ Successfully published Helm chart ${RELEASE_VERSION} to ${chart_oci_dest}."
}

# The registry prefix takes GH_TOKEN. An additional release registry takes
# ADDITIONAL_RELEASE_REGISTRY_TOKEN, a Google access token, and only when it
# is an Artifact Registry host: the token goes to no other. Each call is a
# plain statement, so errexit holds inside the function.
release_registries=()
release_registry_lines="$(release_registry_prefixes)"
while IFS= read -r release_registry; do release_registries+=("${release_registry}"); done <<<"${release_registry_lines}"
for release_registry in "${release_registries[@]}"; do
  if [ "${release_registry}" = "${REGISTRY_PREFIX}" ]; then
    publish_chart_to_registry "${release_registry}" "${GH_USER:-${GITHUB_ACTOR:-oauth2}}" "${GH_TOKEN:-}"
  elif is_artifact_registry_host "${release_registry%%/*}"; then
    publish_chart_to_registry "${release_registry}" "${ACCESS_TOKEN_REGISTRY_USER}" "${ADDITIONAL_RELEASE_REGISTRY_TOKEN:-}"
  else
    publish_chart_to_registry "${release_registry}" "" ""
  fi
done
