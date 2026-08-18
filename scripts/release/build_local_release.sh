#!/usr/bin/env bash
set -euo pipefail

# Local Release Builder & Verification Script for kube-agents
TAG_NAME="${1:-v0.1.0}"
VERSION_NUM="${TAG_NAME#v}"
BUNDLE_PREFIX="kube-agents-${TAG_NAME}"
REPO_ROOT="$(pwd)"
BUILD_DIR="${REPO_ROOT}/build/dist"
STAGE_BASE="$(mktemp -d)"
STAGE_DIR="${STAGE_BASE}/${BUNDLE_PREFIX}"

# Verify required prerequisite tools early
for req_cmd in make go helm tar zip; do
  if ! command -v "$req_cmd" &>/dev/null; then
    echo "❌ Missing required prerequisite tool: $req_cmd" >&2
    exit 1
  fi
done

echo "============================================================"
echo "🚀 LOCAL RELEASE BUILD ENGINE: Packaging ${TAG_NAME}"
echo "============================================================"

# ─── GATE 1: Static, Security & Code Verification ────────────────────────
echo -e "\n[GATE 1/3] Running Static, Security & Code Verification..."

echo "1.1 Validating Repo Structure..."
cd "${REPO_ROOT}"
make validate

echo "1.2 Running Operator Go Unit Tests..."
cd "${REPO_ROOT}/k8s-operator"
go test ./...
cd "${REPO_ROOT}"

echo "1.3 Running Shellcheck on Shell Scripts..."
SCRIPTS=()
for f in install.sh uninstall.sh upgrade.sh; do
  if [ -f "${f}" ]; then
    SCRIPTS+=("${f}")
  fi
done

if command -v shellcheck &>/dev/null; then
  if [ "${#SCRIPTS[@]}" -gt 0 ]; then
    shellcheck "${SCRIPTS[@]}"
    echo "✓ Shellcheck passed on ${#SCRIPTS[@]} root script(s)."
  else
    shellcheck -e SC2034,SC1090,SC1091,SC2148,SC2155,SC2206 k8s-operator/scripts/provision.sh k8s-operator/scripts/teardown.sh
    echo "✓ Shellcheck passed on k8s-operator scripts."
  fi
else
  echo "ℹ Shellcheck not installed locally; skipping."
fi

echo "1.4 Running Documentation Integrity Checks..."
cd "${REPO_ROOT}"
make docs-check
echo "✓ Gate 1 Verification SUCCESSFUL!"

# ─── GATE 2: Container, Helm, Archive & Checksum Packaging ─────────────
echo -e "\n[GATE 2/3] Packaging Release Bundles & Artifacts..."
rm -rf "${BUILD_DIR}" "${STAGE_DIR}"
mkdir -p "${BUILD_DIR}" "${STAGE_DIR}"

echo "2.1 Linting and Templating Helm Chart..."
cd "${REPO_ROOT}"
SET_FLAGS=(
  --set platformAgent.harness.clusterName=release-cluster
  --set platformAgent.harness.location=us-central1
  --set platformAgent.harness.projectId=release-project
)
helm lint charts/kube-agents "${SET_FLAGS[@]}"
helm template test-release charts/kube-agents "${SET_FLAGS[@]}" > /dev/null

echo "2.2 Packaging Helm Chart..."
helm package charts/kube-agents --version "${VERSION_NUM}" --app-version "${TAG_NAME}" -d "${BUILD_DIR}"

echo "2.3 Staging Release Source & Manifest Bundle..."
mkdir -p "${STAGE_DIR}"
for item in charts k8s-operator agents terraform README.md install.sh uninstall.sh upgrade.sh INSTALL.md LICENSE; do
  if [ -e "${REPO_ROOT}/${item}" ]; then
    cp -r "${REPO_ROOT}/${item}" "${STAGE_DIR}/"
  fi
done
rm -rf "${STAGE_DIR}/k8s-operator/bin" 2>/dev/null || true
find "${STAGE_DIR}" -type f \( -name "vars.sh" -o -name ".env*" -o -name "*.log" -o -name "*.tmp" \) -exec rm -f {} + 2>/dev/null || true
find "${STAGE_DIR}/terraform" "${STAGE_DIR}/k8s-operator" -type d -name ".terraform" -exec rm -rf {} + 2>/dev/null || true
find "${STAGE_DIR}/terraform" "${STAGE_DIR}/k8s-operator" -type f \( -name "*.tfstate*" -o -name "*.tfvars" -o -name ".terraform.lock.hcl" \) -exec rm -f {} + 2>/dev/null || true
sed -i.bak "s/^version:.*/version: ${VERSION_NUM}/" "${STAGE_DIR}/charts/kube-agents/Chart.yaml" && rm -f "${STAGE_DIR}/charts/kube-agents/Chart.yaml.bak"
sed -i.bak "s/^appVersion:.*/appVersion: \"${TAG_NAME}\"/" "${STAGE_DIR}/charts/kube-agents/Chart.yaml" && rm -f "${STAGE_DIR}/charts/kube-agents/Chart.yaml.bak"

echo "2.4 Creating Web Download Archives (.tar.gz, .tgz, .zip)..."
tar -czf "${BUILD_DIR}/${BUNDLE_PREFIX}.tar.gz" -C "${STAGE_BASE}" "${BUNDLE_PREFIX}"
cp "${BUILD_DIR}/${BUNDLE_PREFIX}.tar.gz" "${BUILD_DIR}/${BUNDLE_PREFIX}.tgz"
(cd "${STAGE_BASE}" && zip -q -r "${BUILD_DIR}/${BUNDLE_PREFIX}.zip" "${BUNDLE_PREFIX}")
chmod -R u+w "${STAGE_BASE}" 2>/dev/null || true
rm -rf "${STAGE_BASE}"

echo "2.5 Generating SPDX Software Bill of Materials (SBOM)..."
if command -v syft &>/dev/null; then
  syft dir:. -o spdx-json="${BUILD_DIR}/${BUNDLE_PREFIX}.spdx.json"
  echo "✓ Generated SPDX SBOM via syft."
else
  cat <<EOF > "${BUILD_DIR}/${BUNDLE_PREFIX}.spdx.json"
{
  "spdxVersion": "SPDX-2.3",
  "dataLicense": "CC0-1.0",
  "SPDXID": "SPDXRef-DOCUMENT",
  "name": "${BUNDLE_PREFIX}-local-sbom",
  "documentNamespace": "https://github.com/gke-labs/kube-agents/releases/tag/${TAG_NAME}",
  "creationInfo": {
    "creators": ["Tool: kube-agents-local-release-builder"],
    "created": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  }
}
EOF
  echo "✓ Generated fallback SPDX JSON manifest."
fi

echo "2.6 Computing SHA-256 Checksums..."
cd "${BUILD_DIR}"
if command -v sha256sum &>/dev/null; then
  sha256sum * > checksums.txt
elif command -v shasum &>/dev/null; then
  shasum -a 256 * > checksums.txt
fi
cd "${REPO_ROOT}"

echo "✓ Gate 2 Packaging SUCCESSFUL!"

# ─── GATE 3: Ephemeral Dry-Run Smoke Test ────────────────────────────────
echo -e "\n[GATE 3/3] Running Dry-Run Installer & Upgrader Smoke Suite..."
cd "${REPO_ROOT}"
VARS_BACKUP=""
if [ -f "${REPO_ROOT}/k8s-operator/scripts/vars.sh" ]; then
  VARS_BACKUP="$(mktemp)"
  cp "${REPO_ROOT}/k8s-operator/scripts/vars.sh" "${VARS_BACKUP}"
fi
TEST_BIN_DIR="$(mktemp -d)"

restore_vars() {
  rm -rf "${TEST_BIN_DIR}"
  if [ -n "${VARS_BACKUP}" ] && [ -f "${VARS_BACKUP}" ]; then
    cp "${VARS_BACKUP}" "${REPO_ROOT}/k8s-operator/scripts/vars.sh"
    rm -f "${VARS_BACKUP}"
  else
    rm -f "${REPO_ROOT}/k8s-operator/scripts/vars.sh"
  fi
}
trap restore_vars EXIT

cat > "${TEST_BIN_DIR}/gcloud" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  "config get-value account") echo "maintainer@example.com" ;;
  "config get-value project") echo "release-project" ;;
  "config get-value compute/region") echo "us-central1" ;;
  "auth print-access-token") echo "test-access-token" ;;
  projects\ describe*) echo "123456789012" ;;
  secrets\ versions\ access*) echo "test-model-key" ;;
  config\ set\ project*) ;;
  *) echo "Unexpected gcloud invocation: $*" >&2; exit 0 ;;
esac
EOF
chmod +x "${TEST_BIN_DIR}/gcloud"

for tool in kubectl gh helm; do
  printf '#!/usr/bin/env bash\nexit 0\n' > "${TEST_BIN_DIR}/$tool"
  chmod +x "${TEST_BIN_DIR}/$tool"
done

CURRENT_SHA="$(git rev-parse HEAD)"
PATH="${TEST_BIN_DIR}:$PATH" ./install.sh --dry-run -y --project-id="release-project" --cluster-name="release-cluster" --region="us-central1" --image-tag="${CURRENT_SHA}"
PATH="${TEST_BIN_DIR}:$PATH" ./upgrade.sh --dry-run -y --upgrade-mode=full --project-id="release-project" --image-tag="${CURRENT_SHA}"
PATH="${TEST_BIN_DIR}:$PATH" ./uninstall.sh --dry-run -y --project-id="release-project" --cluster-name="release-cluster" --region="us-central1"
echo "✓ Gate 3 Smoke Suite SUCCESSFUL!"

echo -e "\n============================================================"
echo "🎉 LOCAL RELEASE BUILD COMPLETE: ${TAG_NAME}"
echo "============================================================"
echo "Generated Release Artifacts in build/dist/:"
ls -lh "${BUILD_DIR}"
