#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 8: Deploy GitHub Token Minter
# ==============================================================================
# Idempotent script that deploys the GitHub Token Minter.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$SCRIPT_DIR" == */scripts ]]; then
  OPERATOR_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  OPERATOR_DIR="${SCRIPT_DIR}"
fi
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud" "kubectl" "envsubst"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State for Agent Deployment"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "us-east4" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "platform-agent-host" "Enter GKE Cluster Name"
init_var "KMS_KEYRING" "github-token-minter-keyring" "Enter Cloud KMS Keyring Name"
init_var "KMS_KEY" "github-token-minter-key" "Enter Cloud KMS Key Name"

export GOOGLE_CLOUD_QUOTA_PROJECT="${PROJECT_ID}"

# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Connect kubectl
verify_kubeconfig() {
  local current_ctx
  current_ctx=$(kubectl config current-context 2>/dev/null || echo "")
  [[ "$current_ctx" == *"${PROJECT_ID}"* && "$current_ctx" == *"${CLUSTER_NAME}"* ]] && \
  kubectl get namespace "$NAMESPACE" >/dev/null 2>&1
}

execute_kubeconfig() {
  connect_cluster
}

# Step 2: Enable KMS API
verify_kms_api() {
  local out=$(gcloud services list --enabled --project="$PROJECT_ID" --format="value(config.name)" 2>/dev/null || echo "")
  echo "$out" | grep -q 'cloudkms.googleapis.com'
}

execute_kms_api() {
  print_info "Enabling Cloud KMS API..."
  gcloud services enable \
      cloudkms.googleapis.com \
      --project="$PROJECT_ID"
}

# Step 3: Deploy GitHub Token Minter
verify_github_minter() {
  if [ -z "${GITHUB_ORG:-}" ] || [ -z "${GITHUB_REPO:-}" ] || [ -z "${GITHUB_APP_ID:-}" ]; then
    print_info "GitHub integration not configured. Skipping Minter deployment."
    return 0
  fi

  # Always return false to ensure configuration updates (like KMS key changes)
  # are applied to the Deployment workloads.
  return 1
}

execute_github_minter() {
  if [ -z "${GITHUB_ORG:-}" ] || [ -z "${GITHUB_REPO:-}" ] || [ -z "${GITHUB_APP_ID:-}" ]; then
    return 0
  fi

  # Ensure KMS Keyring and Key exist.
  print_info "Ensuring KMS Keyring '${KMS_KEYRING}' exists..."
  if ! gcloud kms keyrings describe "${KMS_KEYRING}" --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud kms keyrings create "${KMS_KEYRING}" --location="${REGION}" --project="${PROJECT_ID}" || return 1
  fi

  print_info "Ensuring KMS Key '${KMS_KEY}' exists..."
  if ! gcloud kms keys describe "${KMS_KEY}" --location="${REGION}" --keyring="${KMS_KEYRING}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud kms keys create "${KMS_KEY}" \
        --location="${REGION}" \
        --keyring="${KMS_KEYRING}" \
        --purpose=asymmetric-signing \
        --default-algorithm=rsa-sign-pkcs1-2048-sha256 \
        --import-only \
        --skip-initial-version-creation \
        --project="${PROJECT_ID}" || return 1
  fi

  # Ensure the Minter GSA has signer permissions on the KMS key.
  local gsa_email="${GITHUB_MINTER_GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
  print_info "Ensuring GSA has signer permissions on KMS key..."
  gcloud kms keys add-iam-policy-binding "${KMS_KEY}" \
      --location="${REGION}" \
      --keyring="${KMS_KEYRING}" \
      --member="serviceAccount:${gsa_email}" \
      --role="roles/cloudkms.signerVerifier" \
      --project="${PROJECT_ID}" \
      --quiet >/dev/null || return 1

  # Import PEM if provided and no version exists
  local versions=$(gcloud kms keys versions list --key="${KMS_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" --project="${PROJECT_ID}" --filter="state=ENABLED" --format="value(name)" 2>/dev/null)
  if [ -z "$versions" ]; then
    if [ -n "${GITHUB_PEM_PATH}" ] && [ -f "${GITHUB_PEM_PATH}" ]; then
      if ! command -v go &>/dev/null; then
        print_warning "Go is required to run the Minty CLI tool for importing the private key."
        print_warning "Skipping automatic import. You must import the key manually later."
      else
        print_info "Importing GitHub Private Key PEM into KMS..."
        
        # We clone the Minty CLI here because it abstracts away all the complexity
        # of uploading asymmetric private keys to KMS, making the process much
        # more straightforward than using native gcloud commands.
        local tmp_dir=$(mktemp -d)
        print_info "Cloning github-token-minter CLI tool (v2.7.1) for secure cryptographic wrapping..."
        if git clone --depth 1 --branch v2.7.1 https://github.com/abcxyz/github-token-minter.git "$tmp_dir" >/dev/null 2>&1; then
          local abs_pem=$(realpath "${GITHUB_PEM_PATH}")
          local import_success=0
          (
            cd "$tmp_dir"
            retry 6 5 go run ./cmd/minty tools import-pk \
                -project-id="${PROJECT_ID}" \
                -location="${REGION}" \
                -key-ring="${KMS_KEYRING}" \
                -key="${KMS_KEY}" \
                -private-key="@${abs_pem}"
          ) && import_success=1
          rm -rf "$tmp_dir"
          
          if [ "$import_success" -eq 1 ]; then
            print_success "Successfully imported GitHub Private Key to KMS via Minty CLI."
          else
            print_error "Failed to import GitHub Private Key to KMS. You must import it manually."
          fi
        else
          rm -rf "$tmp_dir"
          print_error "Failed to clone github-token-minter repo for CLI tools. You must import it manually."
        fi
      fi
    else
      print_warning "No GitHub Private Key PEM path provided or file not found."
      print_warning "KMS Key '${KMS_KEY}' has no active version. Minter will fail to start until you import the key."
      print_warning "You can import it later manually using Minty CLI:"
      print_warning "  git clone --depth 1 --branch v2.7.1 https://github.com/abcxyz/github-token-minter.git /tmp/minty && cd /tmp/minty && go run ./cmd/minty tools import-pk -project-id=${PROJECT_ID} -location=${REGION} -key-ring=${KMS_KEYRING} -key=${KMS_KEY} -private-key=@/path/to/pem"
    fi
  fi

  # Resolve the latest active (ENABLED) version number dynamically
  print_info "Resolving active KMS key version number..."
  local active_version
  active_version=$(gcloud kms keys versions list --key="${KMS_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" --project="${PROJECT_ID}" --filter="state=ENABLED" --format="value(name)" 2>/dev/null | awk -F'/' '{print $NF}' | sort -n | tail -n 1)
  
  if [ -n "$active_version" ]; then
    export KMS_KEY_VERSION="${active_version}"
    print_success "Resolved active KMS key version: ${KMS_KEY_VERSION}"
  else
    print_warning "No active (ENABLED) version found for KMS Key '${KMS_KEY}'."
    print_warning "Defaulting KMS_KEY_VERSION to '1'. The Token Minter deployment will fail its readiness probes until a key is imported."
    export KMS_KEY_VERSION="1"
  fi

  print_info "Deploying GitHub Token Minter workloads..."
  local GITHUB_INTEGRATION_DIR="${OPERATOR_DIR}/config/integrations/github"
  
  if [ -d "$GITHUB_INTEGRATION_DIR" ]; then
    # Ensure all variables are exported for envsubst
    export PROJECT_ID REGION CLUSTER_NAME NAMESPACE GITHUB_MINTER_KSA_NAME GITHUB_MINTER_GSA_NAME KMS_KEYRING KMS_KEY KMS_KEY_VERSION GITHUB_ORG GITHUB_REPO KSA_NAME PLATFORM_AGENT_GSA_NAME
    make -C "${OPERATOR_DIR}" deploy-github || return 1
  else
    print_error "GitHub integration directory not found at ${GITHUB_INTEGRATION_DIR}"
    return 1
  fi
}


# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_step "2. Enable Cloud KMS API" verify_kms_api execute_kms_api 0
run_step "3. Deploy GitHub Token Minter" verify_github_minter execute_github_minter 10

# ─── Conclusion Checklist ─────────────────────────────────────────────────────
echo -e "\n${C_GREEN}${C_BOLD}✓ GitHub Token Minter deployed successfully to GKE!${C_RESET}"
