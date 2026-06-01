#!/usr/bin/env bash
# ==============================================================================
# 🤖 GKE Standard & Google Chat E2E Resumable Provisioner
# ==============================================================================
# An idempotent, interactive setup script to bootstrap GCP, GKE, Artifact
# Registry, Secrets, build the GChat container, deploy the operator,
# and launch the Hermes Agent.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
C_CYAN='\033[96m'
C_GREEN='\033[92m'
C_YELLOW='\033[93m'
C_MAGENTA='\033[95m'
C_BLUE='\033[94m'
C_RED='\033[91m'
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_WHITE='\033[97m'

VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── UI Helpers ───────────────────────────────────────────────────────────────
print_step() {
  echo -e "\n${C_MAGENTA}${C_BOLD}>>>  $1  <<<${C_RESET}"
}

print_success() {
  echo -e "  ${C_GREEN}✓ $1${C_RESET}"
}

print_info() {
  echo -e "  ${C_CYAN}ℹ $1${C_RESET}"
}

print_error() {
  echo -e "  ${C_RED}✗ $1${C_RESET}"
}

wait_for_a_bit() {
  local seconds=$1
  local msg=$2
  local spinner=( "⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏" )
  
  echo -ne "  ${C_YELLOW}${msg} (${seconds}s)...  "
  tput civis 2>/dev/null || true
  
  for (( i=0; i<seconds*10; i++ )); do
    local idx=$(( i % 10 ))
    echo -ne "\b${spinner[$idx]}"
    sleep 0.1
  done
  
  echo -ne "\b ${C_RESET}\n"
  tput cnorm 2>/dev/null || true
}

cleanup() {
  tput cnorm 2>/dev/null || true
}
trap cleanup EXIT

# ─── Argument Parsing ─────────────────────────────────────────────────────────
DRY_RUN=0
while [[ "$#" -gt 0 ]]; do
  case $1 in
    --dry-run) DRY_RUN=1 ;;
  esac
  shift
done

# ─── Configuration & State Restoration ────────────────────────────────────────
if [ ! -f "$VARS_FILE" ]; then
  print_step "Setting up Configuration State"
  
  # 1. Get active GCP Project ID
  ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
  if [ -z "$ACTIVE_PROJECT" ]; then
    DEFAULT_PROJECT_ID="$(whoami)-gkedemos"
  elif [[ "$ACTIVE_PROJECT" == *"-gkedemos" ]]; then
    DEFAULT_PROJECT_ID="$ACTIVE_PROJECT"
  else
    DEFAULT_PROJECT_ID="${ACTIVE_PROJECT}-gkedemos"
  fi
  echo -ne "  ${C_CYAN}Enter Target GCP Project ID [${C_WHITE}${DEFAULT_PROJECT_ID}${C_CYAN}]: ${C_RESET}"
  read -r INPUT_PROJECT_ID
  export PROJECT_ID="${INPUT_PROJECT_ID:-$DEFAULT_PROJECT_ID}"
  
  # 2. Dynamically resolve project number using gcloud to prevent HTTP metadata server queries later
  print_info "Resolving numeric Project Number for $PROJECT_ID..."
  PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)" 2>/dev/null || echo "")
  if [ -z "$PROJECT_NUMBER" ]; then
    echo -ne "  ${C_YELLOW}Failed to resolve project number automatically. Please enter it manually: ${C_RESET}"
    read -r PROJECT_NUMBER
  fi
  export PROJECT_NUMBER
  print_success "Project Number resolved: $PROJECT_NUMBER"

  # 3. Get Region
  DEFAULT_REGION="us-central1"
  echo -ne "  ${C_CYAN}Enter GKE GCP Region [${C_WHITE}${DEFAULT_REGION}${C_CYAN}]: ${C_RESET}"
  read -r INPUT_REGION
  export REGION="${INPUT_REGION:-$DEFAULT_REGION}"

  # 4. Get Cluster Name
  DEFAULT_CLUSTER="platform-agent-host"
  echo -ne "  ${C_CYAN}Enter GKE Cluster Name [${C_WHITE}${DEFAULT_CLUSTER}${C_CYAN}]: ${C_RESET}"
  read -r INPUT_CLUSTER
  export CLUSTER_NAME="${INPUT_CLUSTER:-$DEFAULT_CLUSTER}"

  # 5. Get Namespace
  DEFAULT_NAMESPACE="agent-system"
  echo -ne "  ${C_CYAN}Enter GKE Target Namespace [${C_WHITE}${DEFAULT_NAMESPACE}${C_CYAN}]: ${C_RESET}"
  read -r INPUT_NAMESPACE
  export NAMESPACE="${INPUT_NAMESPACE:-$DEFAULT_NAMESPACE}"

  # 6. Get Allowed User Email
  DEFAULT_USER="$(gcloud config get-value account 2>/dev/null || whoami@google.com)"
  echo -ne "  ${C_CYAN}Enter Allowed Google Chat User Email [${C_WHITE}${DEFAULT_USER}${C_CYAN}]: ${C_RESET}"
  read -r INPUT_USER
  export ALLOWED_USER="${INPUT_USER:-$DEFAULT_USER}"

  # 6.5. Generate secure random API Server auth key
  export API_SERVER_KEY=$(openssl rand -hex 16)

  # 7. Write state file
  cat <<EOF > "$VARS_FILE"
# SRE Sourced Variables for GKE & GCP Setup
export PROJECT_ID="${PROJECT_ID}"
export PROJECT_NUMBER="${PROJECT_NUMBER}"
export REGION="${REGION}"
export CLUSTER_NAME="${CLUSTER_NAME}"
export NAMESPACE="${NAMESPACE}"
export ALLOWED_USER="${ALLOWED_USER}"
export REPO_NAME="platform-agent-repo"
export CHAT_TOPIC_NAME="platform-agent-chat-events"
export CHAT_SUB_NAME="platform-agent-chat-events-sub"
export GSA_NAME="platform-agent-bot-platform-agent"
export KSA_NAME="platform-agent-platform-sa"
export API_SERVER_KEY="${API_SERVER_KEY}"
EOF
  print_success "Created configuration state file at $VARS_FILE"
fi

source "$VARS_FILE"

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
PREREQS=("gcloud" "kubectl" "make" "go")
for cmd in "${PREREQS[@]}"; do
  echo -ne "  ${C_CYAN}Checking for $cmd... ${C_RESET}"
  if command -v "$cmd" &> /dev/null; then
    echo -e "✅"
  else
    echo -e "❌"
    print_error "$cmd is required but not installed. Please install it and rerun."
    exit 1
  fi
done

# ─── Step Runner Framework ────────────────────────────────────────────────────
run_step() {
  local name=$1
  local verify_func=$2
  local execute_func=$3
  local wait_time=$4
  
  print_step "$name"
  echo -e "  ${C_CYAN}Verifying current GCP/GKE state...${C_RESET}"
  
  if $verify_func; then
    print_success "Already completed: $name"
    return 0
  fi
  
  if [ "$DRY_RUN" -eq 1 ]; then
    print_info "[DRY-RUN] Would execute: $name"
    return 0
  fi

  print_info "Executing action..."
  if $execute_func; then
    print_success "Successfully executed."
    if [ -n "$wait_time" ] && [ "$wait_time" -gt 0 ]; then
      wait_for_a_bit "$wait_time" "Waiting for changes to propagate"
    fi
  else
    print_error "Failed to execute step: $name"
    exit 1
  fi
}

# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Enable APIs
verify_apis() {
  local out=$(gcloud services list --enabled --project="$PROJECT_ID" --format="value(config.name)" 2>/dev/null || echo "")
  echo "$out" | grep -q 'container.googleapis.com' && \
  echo "$out" | grep -q 'artifactregistry.googleapis.com' && \
  echo "$out" | grep -q 'cloudbuild.googleapis.com' && \
  echo "$out" | grep -q 'secretmanager.googleapis.com' && \
  echo "$out" | grep -q 'pubsub.googleapis.com' && \
  echo "$out" | grep -q 'chat.googleapis.com' && \
  echo "$out" | grep -q 'gsuiteaddons.googleapis.com' && \
  echo "$out" | grep -q 'aiplatform.googleapis.com'
}
execute_apis() {
  gcloud services enable \
      container.googleapis.com \
      artifactregistry.googleapis.com \
      cloudbuild.googleapis.com \
      secretmanager.googleapis.com \
      pubsub.googleapis.com \
      chat.googleapis.com \
      gsuiteaddons.googleapis.com \
      aiplatform.googleapis.com \
      --project="$PROJECT_ID"
}

# Step 2: Create Artifact Registry Repository
verify_registry() {
  gcloud artifacts repositories describe "$REPO_NAME" --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1
}
execute_registry() {
  gcloud artifacts repositories create "$REPO_NAME" \
      --repository-format=docker \
      --location="$REGION" \
      --project="$PROJECT_ID"
}

# Step 3: GKE Cluster Provisioning
verify_cluster() {
  gcloud container clusters describe "$CLUSTER_NAME" --region="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1
}
execute_cluster() {
  print_info "Creating GKE Standard Cluster with Workload Identity. This takes approximately 5-8 minutes in Google Cloud..."
  gcloud container clusters create "$CLUSTER_NAME" \
      --region "$REGION" \
      --machine-type="e2-standard-4" \
      --num-nodes=1 \
      --workload-pool="${PROJECT_ID}.svc.id.goog" \
      --project "$PROJECT_ID"
}

# Step 3.5: Enable GKE Config Connector Add-on
verify_kcc_addon() {
  local val=$(gcloud container clusters describe "$CLUSTER_NAME" --region="$REGION" --project="$PROJECT_ID" --format="value(addonsConfig.configConnectorConfig.enabled)" 2>/dev/null || echo "")
  [ "$val" = "True" ]
}
execute_kcc_addon() {
  print_info "Enabling GKE Config Connector Add-on on GKE Cluster..."
  gcloud container clusters update "$CLUSTER_NAME" \
      --update-addons ConfigConnector=ENABLED \
      --region "$REGION" \
      --project "$PROJECT_ID"
}

# Step 3.6: Configure KCC GCP Identity (GSA & Workload Identity)
verify_kcc_identity() {
  # We check if the GSA exists, has Owner role bound in project, and has the correct namespaced WI member binding
  gcloud iam service-accounts describe "platform-agent-kcc-sa@${PROJECT_ID}.iam.gserviceaccount.com" --project="$PROJECT_ID" >/dev/null 2>&1 && \
  gcloud projects get-iam-policy "$PROJECT_ID" --format=json 2>/dev/null | grep -q "platform-agent-kcc-sa@${PROJECT_ID}.iam.gserviceaccount.com" && \
  gcloud iam service-accounts get-iam-policy "platform-agent-kcc-sa@${PROJECT_ID}.iam.gserviceaccount.com" --project="$PROJECT_ID" --format=json 2>/dev/null | grep -q "cnrm-controller-manager-${NAMESPACE}"
}
execute_kcc_identity() {
  # 1. Create GSA if not exists
  if ! gcloud iam service-accounts describe "platform-agent-kcc-sa@${PROJECT_ID}.iam.gserviceaccount.com" --project="$PROJECT_ID" >/dev/null 2>&1; then
    print_info "Creating GCP GSA platform-agent-kcc-sa..."
    gcloud iam service-accounts create "platform-agent-kcc-sa" --project="$PROJECT_ID"
  fi

  # 2. Grant Owner role to KCC GSA
  print_info "Binding Owner role to KCC GSA..."
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
      --member="serviceAccount:platform-agent-kcc-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
      --role="roles/owner"

  # 3. Bind Workload Identity (GKE KCC pod cnrm-controller-manager to GCP GSA)
  print_info "Binding GKE KCC system controller to KCC GSA via Workload Identity..."
  gcloud iam service-accounts add-iam-policy-binding \
      "platform-agent-kcc-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
      --member="serviceAccount:${PROJECT_ID}.svc.id.goog[cnrm-system/cnrm-controller-manager-${NAMESPACE}]" \
      --role="roles/iam.workloadIdentityUser" \
      --project="$PROJECT_ID"
}


# Step 4: Secret Manager Placeholders
verify_secrets() {
  gcloud secrets describe "GEMINI_API_KEY" --project="$PROJECT_ID" >/dev/null 2>&1
}
execute_secrets() {
  for SECRET in "GEMINI_API_KEY"; do
    if ! gcloud secrets describe "$SECRET" --project="$PROJECT_ID" >/dev/null 2>&1; then
      echo -ne "  ${C_CYAN}Secret '$SECRET' not found in cloud. Enter actual key value now (or press ENTER to create empty placeholder): ${C_RESET}"
      read -s -r INPUT_KEY
      echo ""
      local VAL="${INPUT_KEY:-placeholder}"
      echo -n "$VAL" | gcloud secrets create "$SECRET" --data-file=- --replication-policy="automatic" --project="$PROJECT_ID"
      print_success "Secret '$SECRET' created in GCP Secret Manager."
    fi
  done
}

# Step 5: Kubeconfig Setup & Namespace Creation
verify_kubeconfig() {
  kubectl get namespace "$NAMESPACE" >/dev/null 2>&1
}
execute_kubeconfig() {
  print_info "Fetching cluster credentials..."
  gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID"
  print_info "Creating namespace '$NAMESPACE'..."
  kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
}

# Step 5.5: Configure KCC Namespaced Mode & Target Project Annotations
verify_kcc_namespaced() {
  kubectl get configconnectorcontext configconnectorcontext.core.cnrm.cloud.google.com -n "$NAMESPACE" >/dev/null 2>&1 && \
  kubectl get namespace "$NAMESPACE" -o jsonpath='{.metadata.annotations.cnrm\.cloud\.google\.com/project-id}' 2>/dev/null | grep -q "$PROJECT_ID"
}
execute_kcc_namespaced() {
  print_info "1/2. Applying ConfigConnectorContext in namespace '$NAMESPACE'..."
  local KCC_CR=$(mktemp)
  cat <<EOF > "$KCC_CR"
apiVersion: core.cnrm.cloud.google.com/v1beta1
kind: ConfigConnectorContext
metadata:
  name: configconnectorcontext.core.cnrm.cloud.google.com
  namespace: ${NAMESPACE}
spec:
  googleServiceAccount: platform-agent-kcc-sa@${PROJECT_ID}.iam.gserviceaccount.com
EOF
  kubectl apply -f "$KCC_CR"
  rm -f "$KCC_CR"

  print_info "2/2. Annotating target namespace '$NAMESPACE' with GCP project ID..."
  kubectl annotate namespace "$NAMESPACE" cnrm.cloud.google.com/project-id="$PROJECT_ID" --overwrite
}


# Step 6: Synchronize Secrets to GKE Namespace
verify_k8s_secrets() {
  kubectl get secret platform-agent-secrets -n "$NAMESPACE" >/dev/null 2>&1
}
execute_k8s_secrets() {
  print_info "Resolving keys from GCP Secret Manager..."
  local GEMINI_KEY=$(gcloud secrets versions access latest --secret="GEMINI_API_KEY" --project="$PROJECT_ID" 2>/dev/null || echo "placeholder")
  
  if [ "$GEMINI_KEY" = "placeholder" ]; then
    print_error "Your GEMINI_API_KEY is currently a placeholder in Secret Manager!"
    echo -ne "  ${C_CYAN}Please enter your actual Gemini API Key value now to synchronize: ${C_RESET}"
    read -s -r USER_GEMINI_KEY
    echo ""
    if [ -n "$USER_GEMINI_KEY" ]; then
      # Save to cloud
      echo -n "$USER_GEMINI_KEY" | gcloud secrets versions add "GEMINI_API_KEY" --data-file=- --project="$PROJECT_ID"
      GEMINI_KEY="$USER_GEMINI_KEY"
      print_success "Saved updated Gemini API Key to Secret Manager."
    fi
  fi

  # Self-healing check: Generate API_SERVER_KEY if missing from stale vars.sh cache
  if [ -z "${API_SERVER_KEY:-}" ]; then
    print_info "API_SERVER_KEY not found in vars.sh state. Generating a secure random key..."
    export API_SERVER_KEY=$(openssl rand -hex 16)
    echo "export API_SERVER_KEY=\"${API_SERVER_KEY}\"" >> "$VARS_FILE"
  fi

  print_info "Writing Kubernetes Secret 'platform-agent-secrets' into '$NAMESPACE'..."
  kubectl create secret generic platform-agent-secrets \
      --namespace="$NAMESPACE" \
      --from-literal=GEMINI_API_KEY="$GEMINI_KEY" \
      --from-literal=API_SERVER_KEY="$API_SERVER_KEY" \
      --dry-run=client -o yaml | kubectl apply -f -
}

# Step 7: Build and Push Custom GChat Platform Agent Image
verify_agent_image() {
  # We check if the image 'platform-agent' exists in registry
  gcloud artifacts docker images list "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/platform-agent" --project="$PROJECT_ID" --format="value(image)" 2>/dev/null | grep -q "platform-agent"
}
execute_agent_image() {
  print_info "Building custom, unpatched GChat Platform Agent container via Google Cloud Build..."
  (
    cd "$SCRIPT_DIR/../../.."
    gcloud builds submit \
        --config="integrations/gchat/app/cloudbuild.yaml" \
        --substitutions="_IMAGE_URI=$REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/platform-agent:latest" \
        --project "$PROJECT_ID" \
        .
  )
}

# Step 8: Build, Push, and Deploy Go Operator
verify_operator() {
  kubectl get deployment platform-agent-operator-controller-manager -n platform-agent-operator-system >/dev/null 2>&1 && \
  gcloud artifacts docker images list "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/platform-agent-operator" --project="$PROJECT_ID" --format="value(image)" 2>/dev/null | grep -q "platform-agent-operator"
}
execute_operator() {
  local OPERATOR_IMG="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/platform-agent-operator:latest"
  print_info "1/2. Building and pushing Go Operator image via Google Cloud Build..."
  (
    cd "$SCRIPT_DIR/platform-agent-operator"
    gcloud builds submit --tag "$OPERATOR_IMG" --project "$PROJECT_ID" .
  )
  
  print_info "2/2. Registering CRD & deploying Operator Controller in namespace platform-agent-operator-system..."
  (
    cd "$SCRIPT_DIR/platform-agent-operator"
    # deploy automatically runs 'make install' (CRD registration) first!
    make deploy IMG="$OPERATOR_IMG"
  )
}

# Step 9: Apply Custom Resource Manifest
verify_custom_resource() {
  kubectl get platformagent platform-agent -n "$NAMESPACE" >/dev/null 2>&1
}
execute_custom_resource() {
  print_info "Generating custom resource manifest 'platform-agent.yaml'..."
  local CR_MANIFEST="$SCRIPT_DIR/platform-agent.yaml"
  
  cat <<EOF > "$CR_MANIFEST"
apiVersion: agent.platform.io/v1alpha1
kind: PlatformAgent
metadata:
  name: platform-agent
  namespace: ${NAMESPACE}
spec:
  projectId: "${PROJECT_ID}"
  numericProjectId: "${PROJECT_NUMBER}"
  clusterName: "${CLUSTER_NAME}"
  location: "${REGION}"
  imageUri: "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/platform-agent:latest"
  chatTopicName: "${CHAT_TOPIC_NAME}"
  chatSubName: "${CHAT_SUB_NAME}"
  gsaName: "${GSA_NAME}"
  ksaName: "${KSA_NAME}"
  googleChatAllowedUsers: "${ALLOWED_USER}"
  googleChatHomeChannel: ""
EOF
  
  print_info "Applying 'platform-agent' Custom Resource to the GKE cluster..."
  kubectl apply -f "$CR_MANIFEST"
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Enable GCP APIs" verify_apis execute_apis 30
run_step "2. Create Artifact Registry Repo" verify_registry execute_registry 0
run_step "3. Provision GKE Cluster" verify_cluster execute_cluster 10
run_step "4. Enable GKE Config Connector Add-on" verify_kcc_addon execute_kcc_addon 15
run_step "5. Configure KCC GCP Identity (GSA & Workload Identity)" verify_kcc_identity execute_kcc_identity 15
run_step "6. Connect kubectl & Create Namespace" verify_kubeconfig execute_kubeconfig 5
run_step "7. Configure KCC Namespaced Mode & Target Project Annotations" verify_kcc_namespaced execute_kcc_namespaced 10
run_step "8. Setup Secret Manager Placeholders" verify_secrets execute_secrets 0
run_step "9. Sync API Keys to GKE Namespace Secrets" verify_k8s_secrets execute_k8s_secrets 0
run_step "10. Package & Build GChat Agent via Cloud Build" verify_agent_image execute_agent_image 0
run_step "11. Build & Deploy Go Operator Controller" verify_operator execute_operator 10
run_step "12. Declaratively Apply PlatformAgent Custom Resource" verify_custom_resource execute_custom_resource 0

# ─── Conclusion Copy-Paste Checklist ──────────────────────────────────────────
print_step "Infrastructure & Operator Provisioned Successfully!"

echo -e "${C_YELLOW}${C_BOLD}======================= START COPY&PASTE =======================${C_RESET}"
echo -e "${C_YELLOW}Your declarative GKE Platform Agent is rolling out in the background!${C_RESET}"
echo -e "Recommend you copy-paste this final step checklist to complete setup:\n"

echo -e "[ ] 1. Configure GChat bot connection in GCP Console:"
echo -e "       ${C_WHITE}https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project=${PROJECT_ID}${C_RESET}"
echo -e "       - Name: ${C_GREEN}GKE Platform Agent Bot${C_RESET}"
echo -e "       - Avatar: ${C_GREEN}https://platform-agent.nousresearch.com/docs/img/logo.png${C_RESET}"
echo -e "       - Connection Settings: Select ${C_BOLD}Cloud Pub/Sub${C_RESET}"
echo -e "       - Pub/Sub Topic Name: ${C_GREEN}projects/${PROJECT_ID}/topics/${CHAT_TOPIC_NAME}${C_RESET}"
echo -e "       - Under Visibility, check: ${C_GREEN}Only specific people (add your email ${ALLOWED_USER})${C_RESET}"

echo -e ""
echo -e "[ ] 2. Monitor Operator and Gateway pods rollout progress:"
echo -e "       ${C_WHITE}kubectl get pods -n platform-agent-operator-system${C_RESET}"
echo -e "       ${C_WHITE}kubectl get pods -n ${NAMESPACE}${C_RESET}"

echo -e ""
echo -e "[ ] 3. Send a DM to the Bot on Google Chat:"
echo -e "       Type: ${C_WHITE}\"Hi Hermes\"${C_RESET}"

echo -e ""
echo -e "[ ] 4. ${C_YELLOW}[Optional]${C_RESET} Approve pairing code in GKE container:"
echo -e "       ${C_CYAN}(Only required for first-time bot deployments in new GCP projects/spaces. If the bot responds instantly, skip this step!)${C_RESET}"
echo -e "       ${C_WHITE}kubectl exec -it deploy/platform-agent-gateway -n ${NAMESPACE} -c hermes -- hermes pairing approve google_chat <PAIRING_CODE>${C_RESET}"

echo -e ""
echo -e "======================== END COPY&PASTE ========================\n"
