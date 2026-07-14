#!/usr/bin/env bash
# ==============================================================================
# 🤖 Hybrid E2E Terraform & Local Helm Deployment and Verification Script
# ==============================================================================
set -euo pipefail

# Google Environment Terraform Wrapper Function with state isolation
# Clear HTTP/HTTPS proxy variables for local commands to ensure direct connections
# to GKE control plane endpoints bypass any corporate proxies.
export HTTP_PROXY=""
export HTTPS_PROXY=""
export http_proxy=""
export https_proxy=""
export NO_PROXY="*"

# ANSI Colors
C_CYAN='\033[96m'
C_GREEN='\033[92m'
C_YELLOW='\033[93m'
C_RED='\033[91m'
C_WHITE='\033[97m'
C_RESET='\033[0m'
C_BOLD='\033[1m'

print_step() { echo -e "\n${C_BOLD}${C_CYAN}>>>${C_RESET} ${C_BOLD}$1${C_RESET} ${C_BOLD}${C_CYAN}<<<${C_RESET}"; }
print_success() { echo -e "  ${C_GREEN}✓${C_RESET} $1"; }
print_info() { echo -e "  ${C_CYAN}ℹ${C_RESET} $1"; }
print_error() { echo -e "  ${C_RED}✗${C_RESET} $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TF_DIR="${ROOT_DIR}/deploy/terraform"

# Usage / Help Message
usage() {
  echo -e "${C_BOLD}Usage:${C_RESET} $0 [required-options] [optional-options]"
  echo ""
  echo -e "${C_BOLD}Required Options:${C_RESET}"
  echo "  -p, --project-id VALUE         Target GCP Project ID"
  echo "  -r, --region VALUE             GCP Region for the GKE cluster"
  echo "  -c, --cluster-name VALUE       GKE Cluster Name"
  echo "  -n, --namespace VALUE          Kubernetes namespace for deployment"
  echo ""
  echo -e "${C_BOLD}Optional Options:${C_RESET}"
  echo "  -m, --model-provider VALUE     Model Provider: gemini, anthropic, openai, chatgpt (default: gemini)"
  echo "  -d, --model-default-name VALUE Default Model Name (default: gemini-3.5-flash)"
  echo "  -u, --allowed-users VALUE      Comma-separated list of allowed chat users (default: empty/all)"
  echo "  -go, --github-org VALUE        GitHub Organization/Owner name (for Token Minter)"
  echo "  -gr, --github-repo VALUE       GitHub Repository name (for Token Minter)"
  echo "  -ga, --github-app-id VALUE     GitHub App ID (for Token Minter)"
  echo "  -gp, --github-pem-path VALUE   GitHub App Private Key PEM file path (for KMS import)"
  echo "  -h, --help                     Display this help message and exit"
  echo ""
  exit 1
}

# Initialize variables
PROJECT_ID=""
REGION=""
CLUSTER_NAME=""
NAMESPACE=""
MODEL_PROVIDER="gemini"
MODEL_DEFAULT_NAME="gemini-3.5-flash"
ALLOWED_USERS=""
GITHUB_ORG=""
GITHUB_REPO=""
GITHUB_APP_ID=""
GITHUB_PEM_PATH=""

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    -p|--project-id)
      PROJECT_ID="$2"
      shift 2
      ;;
    -r|--region)
      REGION="$2"
      shift 2
      ;;
    -c|--cluster-name)
      CLUSTER_NAME="$2"
      shift 2
      ;;
    -n|--namespace)
      NAMESPACE="$2"
      shift 2
      ;;
    -m|--model-provider)
      MODEL_PROVIDER="$2"
      shift 2
      ;;
    -d|--model-default-name)
      MODEL_DEFAULT_NAME="$2"
      shift 2
      ;;
    -u|--allowed-users)
      ALLOWED_USERS="$2"
      shift 2
      ;;
    -go|--github-org)
      GITHUB_ORG="$2"
      shift 2
      ;;
    -gr|--github-repo)
      GITHUB_REPO="$2"
      shift 2
      ;;
    -ga|--github-app-id)
      GITHUB_APP_ID="$2"
      shift 2
      ;;
    -gp|--github-pem-path)
      GITHUB_PEM_PATH="$2"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      print_error "Unknown option: $1"
      usage
      ;;
  esac
done

# Validate the first four (required) parameters
if [ -z "${PROJECT_ID}" ] || [ -z "${REGION}" ] || [ -z "${CLUSTER_NAME}" ] || [ -z "${NAMESPACE}" ]; then
  print_error "Missing required arguments: project-id, region, cluster-name, and namespace must all be specified."
  echo ""
  usage
fi

# Detect if GEMINI_API_KEY is available in the local environment
TF_VARS=""
if [ -n "${GEMINI_API_KEY:-}" ]; then
  print_info "GEMINI_API_KEY detected in environment. Will pass it to Helm."
else
  print_info "No GEMINI_API_KEY found in environment. Using default placeholder."
fi

# Generate unique GSA and Pub/Sub names for non-default clusters
if [ "${CLUSTER_NAME}" != "kube-agents-dedicated-cluster" ]; then
  # Remove "kube-agents-" prefix if present to make suffix shorter and readable
  SUFFIX="${CLUSTER_NAME#kube-agents-}"
  # Clean up suffix to only allow valid GSA characters
  SUFFIX=$(echo "${SUFFIX}" | tr -cd 'a-zA-Z0-9-')
  CONTROLLER_GSA=$(echo "ka-ctrl-${SUFFIX}" | cut -c1-30)
  PLATFORM_GSA=$(echo "ka-plat-${SUFFIX}" | cut -c1-30)
  OPERATOR_GSA=$(echo "ka-oper-${SUFFIX}" | cut -c1-30)
  DEVTEAM_GSA=$(echo "ka-dev-${SUFFIX}" | cut -c1-30)
  GITHUB_MINTER_GSA=$(echo "ka-git-${SUFFIX}" | cut -c1-30)
  TOPIC_NAME="platform-agent-chat-events-${SUFFIX}"
  SUB_NAME="platform-agent-chat-events-sub-${SUFFIX}"
  DELETION_PROTECTION="false"
else
  CONTROLLER_GSA="kubeagents-controller-gsa"
  PLATFORM_GSA="kubeagents-platform-gsa"
  OPERATOR_GSA="kubeagents-operator-gsa"
  DEVTEAM_GSA="kubeagents-devteam-gsa"
  GITHUB_MINTER_GSA="kubeagents-github-minter-gsa"
  TOPIC_NAME="platform-agent-chat-events"
  SUB_NAME="platform-agent-chat-events-sub"
  DELETION_PROTECTION="true"
fi

# Step 1: Initialize Terraform
print_step "Initializing Terraform"
cd "${TF_DIR}"
echo "=== DEBUG: Current Directory ==="
pwd
echo "=== DEBUG: Files in TF_DIR ==="
ls -la
echo "=== DEBUG: Contents of variables.tf ==="
cat variables.tf
terraform init

# Step 2: Apply Terraform Configuration (Only GCP Infrastructure)
print_step "Applying Terraform Configuration (GCP Resources & GKE Cluster)"
terraform apply -auto-approve \
  -state="terraform.tfstate.${CLUSTER_NAME}" \
  -var="project_id=${PROJECT_ID}" \
  -var="region=${REGION}" \
  -var="cluster_name=${CLUSTER_NAME}" \
  -var="namespace=${NAMESPACE}" \
  -var="controller_gsa_name=${CONTROLLER_GSA}" \
  -var="platform_gsa_name=${PLATFORM_GSA}" \
  -var="operator_gsa_name=${OPERATOR_GSA}" \
  -var="devteam_gsa_name=${DEVTEAM_GSA}" \
  -var="github_minter_gsa_name=${GITHUB_MINTER_GSA}" \
  -var="github_org=${GITHUB_ORG:-}" \
  -var="github_repo=${GITHUB_REPO:-}" \
  -var="github_app_id=${GITHUB_APP_ID:-}" \
  -var="gchat_topic_name=${TOPIC_NAME}" \
  -var="gchat_subscription_name=${SUB_NAME}" \
  -var="deletion_protection=${DELETION_PROTECTION}"

# Step 3: Read Terraform Outputs
print_step "Reading Terraform Outputs for Workload Configuration"
tf_output() {
  terraform output -state="terraform.tfstate.${CLUSTER_NAME}" "$@"
}

CONTROLLER_GSA_EMAIL=$(tf_output -raw controller_gsa_email)
PLATFORM_GSA_EMAIL=$(tf_output -raw platform_agent_gsa_email)
OPERATOR_GSA_EMAIL=$(tf_output -raw operator_agent_gsa_email)
DEVTEAM_GSA_EMAIL=$(tf_output -raw devteam_agent_gsa_email)
GITHUB_MINTER_GSA_EMAIL=$(tf_output -raw github_minter_gsa_email || echo "")
KMS_KEYRING=$(tf_output -raw kms_keyring || echo "")
KMS_KEY=$(tf_output -raw kms_key || echo "")
TOPIC_ID=$(tf_output -raw gchat_pubsub_topic)
SUB_ID=$(tf_output -raw gchat_pubsub_subscription)

# Extract short resource names from the full GCP resource paths
TOPIC_NAME=$(basename "${TOPIC_ID}")
SUB_NAME=$(basename "${SUB_ID}")

print_info "Controller GSA: ${CONTROLLER_GSA_EMAIL}"
print_info "Platform GSA:   ${PLATFORM_GSA_EMAIL}"
if [ -n "${GITHUB_ORG}" ]; then
  print_info "GitHub Minter GSA: ${GITHUB_MINTER_GSA_EMAIL}"
  print_info "KMS Keyring:       ${KMS_KEYRING}"
  print_info "KMS Key:           ${KMS_KEY}"
fi
print_info "Pub/Sub Topic:  ${TOPIC_NAME}"

# Step 4: Generate Secure API Server Key locally
print_step "Generating Secure API Server Key"
API_SERVER_KEY=$(openssl rand -hex 16)
print_success "API Server Key generated successfully."

# Step 5: Fetch GKE Credentials
print_step "Connecting kubectl to the GKE cluster"
if [ -n "${GOOGLE_OAUTH_ACCESS_TOKEN:-}" ]; then
  print_info "Generating static kubeconfig using OAuth token..."
  GKE_ENDPOINT=$(terraform output -state="terraform.tfstate.${CLUSTER_NAME}" -raw gke_cluster_endpoint)
  GKE_CA=$(terraform output -state="terraform.tfstate.${CLUSTER_NAME}" -raw gke_cluster_ca_certificate)
  
  tmp_ca=$(mktemp)
  echo "${GKE_CA}" | base64 -d > "${tmp_ca}"
  
  kubectl config set-cluster "${CLUSTER_NAME}" \
    --server="https://${GKE_ENDPOINT}" \
    --certificate-authority="${tmp_ca}" \
    --embed-certs=true
  
  rm -f "${tmp_ca}"
  
  kubectl config set-credentials iac-user --token="${GOOGLE_OAUTH_ACCESS_TOKEN}"
  kubectl config set-context iac-context --cluster="${CLUSTER_NAME}" --user=iac-user
  kubectl config use-context iac-context
  KUBE_CONTEXT="iac-context"
else
  gcloud container clusters get-credentials "${CLUSTER_NAME}" --region "${REGION}" --project "${PROJECT_ID}" --quiet
  KUBE_CONTEXT="gke_${PROJECT_ID}_${REGION}_${CLUSTER_NAME}"
fi

# Step 5.5: Apply Custom Resource Definitions (CRDs)
print_step "Applying Custom Resource Definitions (CRDs) from config/crd/bases"
kubectl --context="${KUBE_CONTEXT}" apply -f "${ROOT_DIR}/config/crd/bases/"

# Step 5.6: Securely Import GitHub Private Key into KMS (if provided)
KMS_KEY_VERSION="1"
if [ -n "${GITHUB_ORG}" ] && [ -n "${GITHUB_PEM_PATH}" ] && [ -f "${GITHUB_PEM_PATH}" ]; then
  print_step "Importing GitHub Private Key PEM into KMS via Minty CLI"
  if ! command -v go &>/dev/null; then
    print_warning "Go is required to run the Minty CLI tool. Skipping automatic key import."
  else
    tmp_dir=$(mktemp -d)
    print_info "Cloning github-token-minter CLI tool (v2.7.1)..."
    if git clone --depth 1 --branch v2.7.1 https://github.com/abcxyz/github-token-minter.git "$tmp_dir" >/dev/null 2>&1; then
      abs_pem=$(realpath "${GITHUB_PEM_PATH}")
      import_success=0
      (
        cd "$tmp_dir"
        for i in {1..6}; do
          if go run ./cmd/minty tools import-pk \
              -project-id="${PROJECT_ID}" \
              -location="${REGION}" \
              -key-ring="${KMS_KEYRING}" \
              -key="${KMS_KEY}" \
              -private-key="@${abs_pem}"; then
            exit 0
          fi
          echo "  [Retry $i/6] Waiting 5 seconds for KMS Import Job to become ACTIVE..."
          sleep 5
        done
        exit 1
      ) && import_success=1
      rm -rf "$tmp_dir"
      
      if [ "$import_success" -eq 1 ]; then
        print_success "Successfully imported GitHub Private Key to KMS."
        # Resolve the active key version
        active_ver=$(gcloud kms keys versions list --key="${KMS_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" --project="${PROJECT_ID}" --filter="state=ENABLED" --format="value(name)" 2>/dev/null | awk -F'/' '{print $NF}' | sort -n | tail -n 1)
        if [ -n "$active_ver" ]; then
          KMS_KEY_VERSION="${active_ver}"
          print_success "Resolved active KMS key version: ${KMS_KEY_VERSION}"
        fi
      else
        print_error "Failed to import key. Defaulting KMS_KEY_VERSION to 1."
      fi
    else
      rm -rf "$tmp_dir"
      print_error "Failed to clone minty tool. Defaulting KMS_KEY_VERSION to 1."
    fi
  fi
fi

# Step 6: Deploy Workloads via local Helm CLI
print_step "Deploying Workloads via Local Helm CLI"
HELM_CHART_PATH="${ROOT_DIR}/deploy/helm/kube-agents"

HELM_ARGS=(
  "--namespace" "${NAMESPACE}"
  "--create-namespace"
  "--set" "global.namespace=${NAMESPACE}"
  "--set" "projectId=${PROJECT_ID}"
  "--set" "clusterName=${CLUSTER_NAME}"
  "--set" "clusterLocation=${REGION}"
  "--set" "operator.controllerGsaEmail=${CONTROLLER_GSA_EMAIL}"
  "--set" "agents.platform.gsaName=${PLATFORM_GSA}"
  "--set" "agents.platform.gsaEmail=${PLATFORM_GSA_EMAIL}"
  "--set" "agents.operator.gsaEmail=${OPERATOR_GSA_EMAIL}"
  "--set" "agents.devteam.gsaEmail=${DEVTEAM_GSA_EMAIL}"
  "--set" "model.provider=${MODEL_PROVIDER}"
  "--set" "model.defaultName=${MODEL_DEFAULT_NAME}"
  "--set" "keys.geminiApiKey=${GEMINI_API_KEY:-placeholder}"
  "--set" "keys.apiServerKey=${API_SERVER_KEY}"
  "--set" "gchat.topicName=${TOPIC_NAME}"
  "--set" "gchat.subscriptionName=${SUB_NAME}"
  "--set" "gchat.allowedUsers=${ALLOWED_USERS}"
)

if [ -n "${GITHUB_ORG}" ]; then
  HELM_ARGS+=(
    "--set" "githubMinter.enabled=true"
    "--set" "githubMinter.gsaEmail=${GITHUB_MINTER_GSA_EMAIL}"
    "--set" "githubMinter.kmsKeyring=${KMS_KEYRING}"
    "--set" "githubMinter.kmsKey=${KMS_KEY}"
    "--set" "githubMinter.kmsKeyVersion=${KMS_KEY_VERSION}"
    "--set" "githubMinter.githubOrg=${GITHUB_ORG}"
    "--set" "githubMinter.githubRepo=${GITHUB_REPO}"
    "--set" "githubMinter.githubAppId=${GITHUB_APP_ID}"
  )
fi

helm --kube-context="${KUBE_CONTEXT}" upgrade --install kube-agents "${HELM_CHART_PATH}" "${HELM_ARGS[@]}"

# Step 6.5: Force rollout restart of deployments to refresh secrets in running pods
print_step "Forcing rollout restart of deployments to refresh secrets"
kubectl --context="${KUBE_CONTEXT}" rollout restart deployment/litellm -n "${NAMESPACE}"
if kubectl --context="${KUBE_CONTEXT}" get deployment/platform-agent-gateway -n "${NAMESPACE}" >/dev/null 2>&1; then
  kubectl --context="${KUBE_CONTEXT}" rollout restart deployment/platform-agent-gateway -n "${NAMESPACE}"
fi

# Step 7: Verify namespace
print_step "Verifying Namespace ${NAMESPACE}"
until kubectl --context="${KUBE_CONTEXT}" get namespace "${NAMESPACE}" >/dev/null 2>&1; do
  print_info "Waiting for namespace ${NAMESPACE} to be active..."
  sleep 5
done
print_success "Namespace ${NAMESPACE} is active."

# Step 8: Wait for Operator rollout
print_step "Waiting for Operator Deployment to roll out"
kubectl --context="${KUBE_CONTEXT}" rollout status deployment/kubeagents-controller-manager -n "${NAMESPACE}" --timeout=120s
print_success "Operator is running."

# Step 9: Wait for LiteLLM rollout
print_step "Waiting for LiteLLM Gateway Deployment to roll out"
kubectl --context="${KUBE_CONTEXT}" rollout status deployment/litellm -n "${NAMESPACE}" --timeout=120s
print_success "LiteLLM Gateway is running."

# Step 10: Wait for PlatformAgent Gateway rollout (created by the operator)
print_step "Waiting for Platform Agent Gateway to roll out (managed by the operator)"
until kubectl --context="${KUBE_CONTEXT}" get deployment/platform-agent-gateway -n "${NAMESPACE}" >/dev/null 2>&1; do
  print_info "Waiting for platform-agent-gateway deployment to be created by the operator..."
  sleep 5
done

kubectl --context="${KUBE_CONTEXT}" rollout status deployment/platform-agent-gateway -n "${NAMESPACE}" --timeout=180s
print_success "Platform Agent Gateway is running."

# Step 11: Final Verification of Pods
print_step "Final Status of Pods in Namespace ${NAMESPACE}"
kubectl --context="${KUBE_CONTEXT}" get pods -n "${NAMESPACE}"

print_success "End-to-End Hybrid Deployment completed and verified successfully!"
echo -e "\n${C_BOLD}Your kube-agents operator, LiteLLM gateway, and Platform Agent bot are fully deployed and running in a dedicated new GKE cluster!${C_RESET}"

echo -e "\n${C_BOLD}======================= START COPY&PASTE =======================${C_RESET}"
echo -e "${C_BOLD}Your Kubernetes Operator and Custom Resources are ready!${C_RESET}"
echo -e "Next steps to configure and interact with your bot:\n"

echo -e "[ ] 1. Configure GChat bot connection in GCP Console:"
echo -e "       Link: https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project=${PROJECT_ID}"
echo -e "       - Application Info -> App name: ${C_BOLD}GKE Platform Agent Bot${C_RESET}"
echo -e "       - Application Info -> Avatar URL: ${C_BOLD}https://platform-agent.nousresearch.com/docs/img/logo.png${C_RESET}"
echo -e "       - Connection settings -> Interactive features: ${C_BOLD}Enabled${C_RESET}"
echo -e "       - Connection settings -> Connection type: Select ${C_BOLD}Cloud Pub/Sub${C_RESET}"
echo -e "       - Connection settings -> Pub/Sub Topic Name: ${C_BOLD}projects/${PROJECT_ID}/topics/${TOPIC_NAME}${C_RESET}"
echo -e "       - Visibility -> Check: ${C_BOLD}Only specific people and groups in your organization${C_RESET}"
echo -e "         (Add your email: ${C_BOLD}${ALLOWED_USERS:-your-email}${C_RESET})"

echo -e ""
echo -e "[ ] 2. Test & Connect to the Chat App (from an allowed user's account):"
echo -e "       As per https://developers.google.com/workspace/chat/quickstart/gcf-app#test-your-chat-app :"
echo -e "       a. Open Google Chat: ${C_BOLD}https://chat.google.com${C_RESET} (or open Chat inside Gmail)"
echo -e "       b. Click the ${C_BOLD}New chat${C_RESET} (+) button."
echo -e "       c. In the ${C_BOLD}Add 1 or more people${C_RESET} field, search for: ${C_BOLD}GKE Platform Agent Bot${C_RESET}"
echo -e "       d. Select the app from the search results to start a direct message."
echo -e "       e. Send a message to the bot (e.g. type: ${C_BOLD}\"Hi Hermes\"${C_RESET})."
echo -e "       ${C_CYAN}Note: It may take up to 2-3 minutes after GCP Console configuration for the bot to become active and respond.${C_RESET}"

echo -e ""
echo -e "[ ] 3. Approve the PAIRING_CODE in GKE (if prompted):"
echo -e "       - If the bot's first response in Google Chat asks you to approve a pairing code, copy the code."
echo -e "       - Run the following command in your terminal to approve it:"
echo -e "         kubectl exec -it -n ${NAMESPACE} --context=${KUBE_CONTEXT} deploy/platform-agent-gateway -c platform-agent -- hermes pairing approve google_chat <PAIRING_CODE>"
echo -e "       - ${C_BOLD}Note:${C_RESET} If the bot responds instantly with a normal message and does not ask for a pairing code, you can skip this step."

if [ "$MODEL_PROVIDER" = "chatgpt" ]; then
  echo -e ""
  echo -e "[ ] 4. ${C_BOLD}Complete ChatGPT OAuth Device Flow Authentication:${C_RESET}"
  echo -e "       Because you selected 'chatgpt' as the model provider, LiteLLM must be authenticated"
  echo -e "       via OpenAI's OAuth Device Flow. Please follow these steps to authenticate:"
  echo -e "       - View the LiteLLM gateway logs to retrieve the 8-digit user code:"
  echo -e "         kubectl logs -n ${NAMESPACE} --context=${KUBE_CONTEXT} deployment/litellm -f"
  echo -e "       - Open your browser and navigate to: https://auth.openai.com/codex/device"
  echo -e "       - Enter the code displayed in the LiteLLM logs and log in to authorize the device."
  echo -e "       - Once authorized, the LiteLLM gateway will automatically pair with your ChatGPT subscription."
fi

echo -e "======================== END COPY&PASTE ========================\n"
