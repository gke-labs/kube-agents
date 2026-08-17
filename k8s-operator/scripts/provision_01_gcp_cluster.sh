#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 1: GCP APIs & GKE Cluster Initialization
# ==============================================================================
# Idempotent setup script that enables GCP APIs (including Backup for GKE),
# provisions Cloud KMS database encryption keys, and bootstraps the bare GKE
# cluster with the BackupRestore addon enabled. The target namespace is
# created later, by the operator deploy in step 03.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$SCRIPT_DIR" == */scripts ]]; then
  OPERATOR_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  OPERATOR_DIR="${SCRIPT_DIR}"
fi
VARS_FILE="${SCRIPT_DIR}/vars.sh"

source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud" "kubectl"
# Before enabling APIs or cutting a KMS key: execute_cluster passes
# --managed-otel-scope on the GA surface, and an older gcloud rejects it during
# argument parsing, stranding those resources behind a failed run.
require_min_gcloud_version || exit 1

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "CLUSTER_NAME" "$DEFAULT_CLUSTER_NAME" "Enter GKE Cluster Name"
init_var "REGION" "$DEFAULT_REGION" "Enter GKE GCP Region"
init_var "GKE_DB_KMS_KEYRING" "platform-agent-keyring" "Enter Cloud KMS Keyring Name for GKE Database Encryption"
init_var "GKE_DB_KMS_KEY" "k8s-secret-encryption-key" "Enter Cloud KMS Key Name for GKE Database Encryption"

# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Enable APIs
verify_apis() {
  local out=$(gcloud services list --enabled --project="$PROJECT_ID" --format="value(config.name)" 2>/dev/null || echo "")
  echo "$out" | grep -q 'container.googleapis.com' && \
  echo "$out" | grep -q 'cloudkms.googleapis.com' && \
  echo "$out" | grep -q 'gkebackup.googleapis.com'
}
execute_apis() {
  gcloud services enable \
      container.googleapis.com \
      cloudkms.googleapis.com \
      gkebackup.googleapis.com \
      --project="$PROJECT_ID"
}

# Step 2: Cloud KMS Key Provisioning
verify_kms() {
  local kms_location
  kms_location="$(derive_kms_location "$REGION")"
  gcloud kms keys describe "$GKE_DB_KMS_KEY" \
      --keyring="$GKE_DB_KMS_KEYRING" \
      --location="$kms_location" \
      --project="$PROJECT_ID" >/dev/null 2>&1 || return 1

  local project_number
  project_number=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)" 2>/dev/null || echo "")
  [ -n "$project_number" ] || return 1
  local service_agent="service-${project_number}@container-engine-robot.iam.gserviceaccount.com"

  local policy
  policy=$(gcloud kms keys get-iam-policy "$GKE_DB_KMS_KEY" \
      --keyring="$GKE_DB_KMS_KEYRING" \
      --location="$kms_location" \
      --project="$PROJECT_ID" \
      --format="json" 2>/dev/null || echo "")

  echo "$policy" | grep -q "$service_agent" || return 1
  echo "$policy" | grep -q "roles/cloudkms.cryptoKeyEncrypterDecrypter" || return 1
  return 0
}
execute_kms() {
  local kms_location
  kms_location="$(derive_kms_location "$REGION")"
  print_info "Creating Cloud KMS Keyring '$GKE_DB_KMS_KEYRING' in location '$kms_location'..."
  gcloud kms keyrings create "$GKE_DB_KMS_KEYRING" \
      --location="$kms_location" \
      --project="$PROJECT_ID" 2>/dev/null || true

  print_info "Creating Cloud KMS Crypto Key '$GKE_DB_KMS_KEY' in keyring '$GKE_DB_KMS_KEYRING'..."
  gcloud kms keys create "$GKE_DB_KMS_KEY" \
      --keyring="$GKE_DB_KMS_KEYRING" \
      --location="$kms_location" \
      --purpose="encryption" \
      --project="$PROJECT_ID" 2>/dev/null || true

  local project_number
  project_number=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)" 2>/dev/null)
  local service_agent="service-${project_number}@container-engine-robot.iam.gserviceaccount.com"
  gcloud beta services identity create --service=container.googleapis.com --project="$PROJECT_ID" 2>/dev/null || true

  print_info "Granting GKE Service Agent ($service_agent) roles/cloudkms.cryptoKeyEncrypterDecrypter on KMS Key..."
  gcloud kms keys add-iam-policy-binding "$GKE_DB_KMS_KEY" \
      --keyring="$GKE_DB_KMS_KEYRING" \
      --location="$kms_location" \
      --member="serviceAccount:${service_agent}" \
      --role="roles/cloudkms.cryptoKeyEncrypterDecrypter" \
      --project="$PROJECT_ID" \
      --quiet
}

# Step 3: GKE Cluster Provisioning
verify_cluster() {
  local enc_state
  enc_state=$(gcloud container clusters describe "$CLUSTER_NAME" \
    --location="$REGION" --project="$PROJECT_ID" \
    --format="value(databaseEncryption.state)" 2>/dev/null || echo "")
  if [ -z "$enc_state" ]; then
    return 1
  fi
  if is_valid_cmek_encryption_state "$enc_state" || is_truthy "${ALLOW_UNENCRYPTED_SECRETS:-false}"; then
    return 0
  fi
  return 1
}
execute_cluster() {
  local kms_location
  kms_location="$(derive_kms_location "$REGION")"
  local kms_key_resource="projects/${PROJECT_ID}/locations/${kms_location}/keyRings/${GKE_DB_KMS_KEYRING}/cryptoKeys/${GKE_DB_KMS_KEY}"
  local enc_state
  enc_state=$(gcloud container clusters describe "$CLUSTER_NAME" \
    --location="$REGION" --project="$PROJECT_ID" \
    --format="value(databaseEncryption.state)" 2>/dev/null || echo "")

  if [ -n "$enc_state" ]; then
    if is_valid_cmek_encryption_state "$enc_state"; then
      print_info "GKE Standard Cluster '$CLUSTER_NAME' already exists with active KMS Database Encryption ('$enc_state'). Skipping update."
    elif is_truthy "${ALLOW_UNENCRYPTED_SECRETS:-false}"; then
      print_warning "GKE Standard Cluster '$CLUSTER_NAME' exists without CMEK encryption ('${enc_state:-UNKNOWN}'), but ALLOW_UNENCRYPTED_SECRETS=true is set. Skipping CMEK update."
    else
      confirm_action "Existing GKE Cluster '$CLUSTER_NAME' database encryption is not enabled. Updating it will modify the live cluster control plane." \
        "Cluster:$CLUSTER_NAME" \
        "Key:$kms_key_resource"
      print_info "Upgrading existing GKE Standard Cluster '$CLUSTER_NAME' with KMS Database Encryption..."
      gcloud container clusters update "$CLUSTER_NAME" \
          --location "$REGION" \
          --database-encryption-key="$kms_key_resource" \
          --project "$PROJECT_ID" \
          --quiet
    fi
  else
    print_info "Creating GKE Standard Cluster with Workload Identity and KMS Database Encryption. This takes approximately 5-8 minutes in Google Cloud..."
    gcloud container clusters create "$CLUSTER_NAME" \
        --location "$REGION" \
        --machine-type="e2-standard-4" \
        --num-nodes=1 \
        --workload-pool="${PROJECT_ID}.svc.id.goog" \
        --database-encryption-key="$kms_key_resource" \
        --addons=GcpFilestoreCsiDriver,BackupRestore \
        --enable-dataplane-v2 \
        --enable-fqdn-network-policy \
        --managed-otel-scope=COLLECTION_AND_INSTRUMENTATION_COMPONENTS \
        --project "$PROJECT_ID" \
        --quiet
  fi
}

# Step 4: Connect kubectl
verify_kubeconfig() {
  local current_ctx
  current_ctx=$(kubectl config current-context 2>/dev/null || echo "")
  [[ "$current_ctx" == *"${PROJECT_ID}"* && "$current_ctx" == *"${CLUSTER_NAME}"* ]]
}
execute_kubeconfig() {
  connect_cluster
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Enable GCP Cluster APIs" verify_apis execute_apis 30
run_step "2. Provision Cloud KMS Key" verify_kms execute_kms 10
run_step "3. Provision GKE Cluster" verify_cluster execute_cluster 10
run_step "4. Connect kubectl" verify_kubeconfig execute_kubeconfig 5

echo -e "\n${C_MAGENTA}${C_BOLD}>>>  GKE Infrastructure Provisioned Successfully!  <<<${C_RESET}"
