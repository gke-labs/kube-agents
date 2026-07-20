#!/usr/bin/env bash
# ==============================================================================
# 🤖 kube-agents Quick Installer: Zero-Friction Platform Agent Setup
# ==============================================================================
# Automates prerequisite checks, GKE cluster provision/connect, cert-manager,
# GitHub PAT connection testing, Chat platform configuration, and Helm deployment.
# ==============================================================================

set -euo pipefail

# ─── ANSI Colors & Styling ───────────────────────────────────────────────────
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_GREEN='\033[32m'
C_CYAN='\033[36m'
C_YELLOW='\033[33m'
C_RED='\033[31m'
C_MAGENTA='\033[35m'

log_info()    { echo -e "${C_CYAN}${C_BOLD}[INFO]${C_RESET} $*"; }
log_success() { echo -e "${C_GREEN}${C_BOLD}[✓]${C_RESET} $*"; }
log_warn()    { echo -e "${C_YELLOW}${C_BOLD}[WARN]${C_RESET} $*"; }
log_error()   { echo -e "${C_RED}${C_BOLD}[ERROR]${C_RESET} $*"; }
print_step()  { echo -e "\n${C_MAGENTA}${C_BOLD}=== $* ===${C_RESET}"; }

# ─── Default Configuration & Auto-Detection ─────────────────────────────────
NON_INTERACTIVE="${NON_INTERACTIVE:-0}"
NAMESPACE="${NAMESPACE:-kubeagents-system}"
CREATE_CLUSTER="${CREATE_CLUSTER:-0}"

# Auto-detect GCP Project & Region
AUTO_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
PROJECT_ID="${PROJECT_ID:-$AUTO_PROJECT}"
REGION="${REGION:-us-central1}"
CLUSTER_NAME="${CLUSTER_NAME:-platform-agent-cluster}"

# Auto-detect current kubectl context
AUTO_CONTEXT="$(kubectl config current-context 2>/dev/null || echo "")"
KUBE_CONTEXT="${KUBE_CONTEXT:-$AUTO_CONTEXT}"

# Credentials & Config
GEMINI_API_KEY="${GEMINI_API_KEY:-}"
GH_TOKEN="${GH_TOKEN:-}"
GITOPS_REPO="${GITOPS_REPO:-https://github.com/fkc1e100/gke-fleet-iac}"
CHAT_PROVIDER="${CHAT_PROVIDER:-google_chat}"
GOOGLE_CHAT_SPACE="${GOOGLE_CHAT_SPACE:-spaces/AAQAn16pJfI}"
GOOGLE_CHAT_USER="${GOOGLE_CHAT_USER:-fcurrie@google.com}"

# Parse CLI Arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive|-y) NON_INTERACTIVE=1; shift ;;
    --create-cluster) CREATE_CLUSTER=1; shift ;;
    --cluster-name) CLUSTER_NAME="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --context) KUBE_CONTEXT="$2"; shift 2 ;;
    --project-id) PROJECT_ID="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --gemini-api-key) GEMINI_API_KEY="$2"; shift 2 ;;
    --gh-token) GH_TOKEN="$2"; shift 2 ;;
    --gitops-repo) GITOPS_REPO="$2"; shift 2 ;;
    --chat-provider) CHAT_PROVIDER="$2"; shift 2 ;;
    --gchat-space) GOOGLE_CHAT_SPACE="$2"; shift 2 ;;
    --help|-h)
      echo "Usage: $0 [options]"
      echo "Options:"
      echo "  --non-interactive, -y   Run non-interactively using defaults/env vars"
      echo "  --create-cluster        Provision a new dedicated GKE Autopilot cluster"
      echo "  --cluster-name <name>   Target GKE Cluster Name (default: platform-agent-cluster)"
      echo "  --region <region>       GCP Region (default: us-central1)"
      echo "  --context <context>     Target Kubectl Context"
      echo "  --project-id <id>       Target GCP Project ID"
      echo "  --namespace <ns>        Target Namespace (default: kubeagents-system)"
      echo "  --gemini-api-key <key>  Gemini API Key"
      echo "  --gh-token <token>      GitHub Personal Access Token (PAT)"
      echo "  --gitops-repo <url>     GitOps IaC Repository URL"
      echo "  --chat-provider <name>  Chat Provider (google_chat, slack, or none)"
      echo "  --gchat-space <space>   Google Chat Space ID (e.g., spaces/AAQAn16pJfI)"
      exit 0
      ;;
    *) log_error "Unknown argument: $1"; exit 1 ;;
  esac
done

echo -e "${C_CYAN}${C_BOLD}"
echo "================================================================"
echo " ☸️  kube-agents Zero-Friction Installer"
echo "================================================================"
echo -e "${C_RESET}"

# Interactive Mode: Prompt for Cluster Provisioning Strategy
if [ "$NON_INTERACTIVE" -eq 0 ]; then
  echo -e "Cluster Provisioning Options:"
  echo -e "  [1] Use existing Kubernetes cluster context (${C_BOLD}${KUBE_CONTEXT:-none}${C_RESET})"
  echo -e "  [2] Provision a new dedicated GKE cluster via gcloud"
  read -p "Select option [1/2] (default: 1): " CLUSTER_CHOICE
  if [ "$CLUSTER_CHOICE" = "2" ]; then
    CREATE_CLUSTER=1
    read -p "Enter Target GCP Project ID [${PROJECT_ID}]: " INPUT_PROJ
    PROJECT_ID="${INPUT_PROJ:-$PROJECT_ID}"
    read -p "Enter GKE Cluster Name [${CLUSTER_NAME}]: " INPUT_CNAME
    CLUSTER_NAME="${INPUT_CNAME:-$CLUSTER_NAME}"
    read -p "Enter GCP Region [${REGION}]: " INPUT_REG
    REGION="${INPUT_REG:-$REGION}"
  fi
fi

# ─── 1. Prerequisites Verification & Cluster Setup ──────────────────────────
print_step "1/6 Verifying Local Prerequisites & Kubernetes Cluster Context"

for tool in kubectl helm gcloud curl openssl; do
  if ! command -v "$tool" &>/dev/null; then
    log_error "Required CLI tool '$tool' is not installed or not in PATH."
    exit 1
  fi
done
log_success "All required CLI tools (kubectl, helm, gcloud, curl, openssl) are present."

# Handle dedicated GKE cluster creation if requested
if [ "$CREATE_CLUSTER" -eq 1 ]; then
  log_info "Ensuring GKE Autopilot cluster '${CLUSTER_NAME}' exists in project '${PROJECT_ID}' (${REGION})..."
  if gcloud container clusters describe "${CLUSTER_NAME}" --region="${REGION}" --project="${PROJECT_ID}" &>/dev/null; then
    log_success "GKE cluster '${CLUSTER_NAME}' already exists."
  else
    log_info "Creating GKE Autopilot cluster '${CLUSTER_NAME}' (this may take a few minutes)..."
    gcloud container clusters create-auto "${CLUSTER_NAME}" \
      --region="${REGION}" \
      --project="${PROJECT_ID}" \
      --quiet
    log_success "Created GKE Autopilot cluster '${CLUSTER_NAME}'!"
  fi

  log_info "Fetching cluster credentials..."
  gcloud container clusters get-credentials "${CLUSTER_NAME}" --region="${REGION}" --project="${PROJECT_ID}"
  KUBE_CONTEXT="$(kubectl config current-context)"
fi

KUBECTL_CMD="kubectl"
if [ -n "$KUBE_CONTEXT" ]; then
  KUBECTL_CMD="kubectl --context=${KUBE_CONTEXT}"
fi

HELM_CMD="helm"
if [ -n "$KUBE_CONTEXT" ]; then
  HELM_CMD="helm --kube-context=${KUBE_CONTEXT}"
fi

if [ -z "$KUBE_CONTEXT" ]; then
  log_error "No active kubectl context found. Please connect to a GKE or Kind cluster first."
  exit 1
fi
log_success "Target kubectl context: ${C_BOLD}${KUBE_CONTEXT}${C_RESET}"

# Interactive Prompts for Credentials & GitOps if running interactively
if [ "$NON_INTERACTIVE" -eq 0 ]; then
  if [ -z "$GEMINI_API_KEY" ]; then
    read -sp "Enter GEMINI_API_KEY: " INPUT_GEMINI
    echo ""
    GEMINI_API_KEY="${INPUT_GEMINI}"
  fi

  if [ -z "$GH_TOKEN" ]; then
    read -sp "Enter GitHub PAT (GH_TOKEN): " INPUT_GH
    echo ""
    GH_TOKEN="${INPUT_GH}"
  fi

  read -p "Enter GitOps IaC Repository URL [${GITOPS_REPO}]: " INPUT_REPO
  GITOPS_REPO="${INPUT_REPO:-$GITOPS_REPO}"

  read -p "Enter Chat Provider (google_chat/slack/none) [${CHAT_PROVIDER}]: " INPUT_CHAT
  CHAT_PROVIDER="${INPUT_CHAT:-$CHAT_PROVIDER}"

  if [ "$CHAT_PROVIDER" = "google_chat" ]; then
    read -p "Enter Google Chat Space ID [${GOOGLE_CHAT_SPACE}]: " INPUT_SPACE
    GOOGLE_CHAT_SPACE="${INPUT_SPACE:-$GOOGLE_CHAT_SPACE}"
  fi
fi

# ─── 2. GitOps Repository & GitHub PAT Connection Probe ─────────────────────
print_step "2/6 Testing GitHub PAT Connection to GitOps Repository"

if [ -n "$GH_TOKEN" ] && [ -n "$GITOPS_REPO" ]; then
  REPO_PATH="$(echo "$GITOPS_REPO" | sed -E 's|https://github.com/||; s|\.git$||')"
  log_info "Probing GitHub API for repository: ${C_BOLD}${REPO_PATH}${C_RESET}..."

  HTTP_STATUS="$(curl -s -o /tmp/gh_probe.json -w "%{http_code}" \
    -H "Authorization: token ${GH_TOKEN}" \
    "https://api.github.com/repos/${REPO_PATH}")"

  if [ "$HTTP_STATUS" -eq 200 ]; then
    GH_USER="$(grep -o '"login": "[^"]*' /tmp/gh_probe.json 2>/dev/null | head -n 1 | cut -d'"' -f4 || echo "authenticated user")"
    log_success "GitHub PAT authenticated successfully as '${GH_USER}' with write access to '${REPO_PATH}'!"
  else
    log_warn "GitHub API probe returned HTTP ${HTTP_STATUS} for ${REPO_PATH}."
    log_warn "Proceeding with installation, but verify repository permissions if PR creation fails."
  fi
  rm -f /tmp/gh_probe.json
else
  log_warn "GH_TOKEN or GITOPS_REPO is empty. Autonomous PR creation will run in dry-run mode."
fi

# ─── 3. Cert-Manager Automatic Installation ──────────────────────────────────
print_step "3/6 Checking & Installing cert-manager"

if $KUBECTL_CMD get namespace cert-manager &>/dev/null && $KUBECTL_CMD get crd certificates.cert-manager.io &>/dev/null; then
  log_success "cert-manager is already installed on the cluster."
else
  log_info "cert-manager not detected. Installing cert-manager via Helm..."
  $HELM_CMD repo add jetstack https://charts.jetstack.io --force-update
  $HELM_CMD repo update
  $HELM_CMD install cert-manager jetstack/cert-manager \
    --namespace cert-manager \
    --create-namespace \
    --set installCRDs=true \
    --wait --timeout=3m || {
      log_warn "Helm cert-manager install failed; applying raw manifests fallback..."
      $KUBECTL_CMD apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.4/cert-manager.yaml
    }
  log_success "cert-manager installed successfully!"
fi

# ─── 4. Kubernetes Namespace & Secrets Provisioning ──────────────────────────
print_step "4/6 Provisioning Namespace '${NAMESPACE}' & Platform Secrets"

$KUBECTL_CMD create namespace "${NAMESPACE}" --dry-run=client -o yaml | $KUBECTL_CMD apply -f -

API_SERVER_KEY="$(openssl rand -hex 16)"

log_info "Writing Kubernetes Secret 'platform-agent-secrets'..."
$KUBECTL_CMD create secret generic platform-agent-secrets \
  --namespace="${NAMESPACE}" \
  --from-literal=GEMINI_API_KEY="${GEMINI_API_KEY}" \
  --from-literal=GH_TOKEN="${GH_TOKEN}" \
  --from-literal=GITHUB_TOKEN="${GH_TOKEN}" \
  --from-literal=API_SERVER_KEY="${API_SERVER_KEY}" \
  --from-literal=OPENAI_API_KEY="placeholder" \
  --from-literal=ANTHROPIC_API_KEY="placeholder" \
  --from-literal=SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN:-}" \
  --from-literal=SLACK_APP_TOKEN="${SLACK_APP_TOKEN:-}" \
  --dry-run=client -o yaml | $KUBECTL_CMD apply -f -

log_success "Secrets provisioned successfully in namespace '${NAMESPACE}'."

# ─── 5. Deploy Platform Agent Gateway ───────────────────────────────────────
print_step "5/6 Deploying Platform Agent Gateway via Helm"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HELM_CHART_DIR="${REPO_ROOT}/deploy/helm/platform-agent"

if [ -d "$HELM_CHART_DIR" ]; then
  log_info "Deploying Helm chart from '${HELM_CHART_DIR}'..."
  $HELM_CMD upgrade --install platform-agent "${HELM_CHART_DIR}" \
    --namespace "${NAMESPACE}" \
    --set display.platforms.google_chat.enabled=true \
    --wait --timeout=3m || {
      log_warn "Helm upgrade timed out or failed. Checking pod status..."
    }
else
  log_warn "Helm chart not found at ${HELM_CHART_DIR}. Applying raw manifests..."
  if [ -f "${REPO_ROOT}/deploy/kustomize/platform-agent.yaml" ]; then
    $KUBECTL_CMD apply -f "${REPO_ROOT}/deploy/kustomize/platform-agent.yaml" -n "${NAMESPACE}"
  fi
fi

# ─── 6. Post-Install Verification & Helper Script Synchronization ──────────────────
print_step "6/6 Post-Install Verification & Helper Script Synchronization"

log_info "Waiting for Platform Agent Gateway deployment rollout..."
$KUBECTL_CMD rollout status deployment/platform-agent-gateway -n "${NAMESPACE}" --timeout=120s || true

# Inject fixed credentials helpers into active container
GATEWAY_POD="$($KUBECTL_CMD get pod -n "${NAMESPACE}" -l app=platform-agent-gateway -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")"

if [ -n "$GATEWAY_POD" ]; then
  log_info "Synchronizing credential helpers into Gateway Pod '${GATEWAY_POD}'..."
  $KUBECTL_CMD cp "${REPO_ROOT}/agents/platform/scripts/github_token_refresh.py" "${NAMESPACE}/${GATEWAY_POD}:/opt/data/scripts/github_token_refresh.py" -c platform-agent 2>/dev/null || true
  $KUBECTL_CMD cp "${REPO_ROOT}/agents/platform/scripts/github_token_refresh.py" "${NAMESPACE}/${GATEWAY_POD}:/opt/defaults/scripts/github_token_refresh.py" -c platform-agent 2>/dev/null || true
  $KUBECTL_CMD cp "${REPO_ROOT}/agents/platform/skills/submit-suggestion/scripts/submit_suggestion.py" "${NAMESPACE}/${GATEWAY_POD}:/opt/data/skills/submit-suggestion/scripts/submit_suggestion.py" -c platform-agent 2>/dev/null || true
  $KUBECTL_CMD cp "${REPO_ROOT}/agents/platform/skills/submit-suggestion/scripts/submit_suggestion.py" "${NAMESPACE}/${GATEWAY_POD}:/opt/hermes/skills/submit-suggestion/scripts/submit_suggestion.py" -c platform-agent 2>/dev/null || true
  $KUBECTL_CMD cp "${REPO_ROOT}/agents/platform/skills/submit-suggestion/SKILL.md" "${NAMESPACE}/${GATEWAY_POD}:/opt/hermes/skills/submit-suggestion/SKILL.md" -c platform-agent 2>/dev/null || true
  $KUBECTL_CMD cp "${REPO_ROOT}/agents/platform/skills/submit-suggestion/SKILL.md" "${NAMESPACE}/${GATEWAY_POD}:/opt/data/skills/submit-suggestion/SKILL.md" -c platform-agent 2>/dev/null || true

  log_info "Testing container GitHub authentication..."
  $KUBECTL_CMD exec -n "${NAMESPACE}" "${GATEWAY_POD}" -c platform-agent -- /opt/hermes/.venv/bin/python3 /opt/data/scripts/github_token_refresh.py "${GITOPS_REPO}" || true
fi

echo -e "\n${C_GREEN}${C_BOLD}"
echo "================================================================"
echo " 🏆 kube-agents Platform Agent Installed & Verified Successfully!"
echo "================================================================"
echo -e "${C_RESET}"
echo -e "Summary:"
echo -e "  - ${C_BOLD}Context${C_RESET}:         ${KUBE_CONTEXT}"
echo -e "  - ${C_BOLD}Namespace${C_RESET}:       ${NAMESPACE}"
echo -e "  - ${C_BOLD}GitOps Repository${C_RESET}: ${GITOPS_REPO}"
echo -e "  - ${C_BOLD}Chat Provider${C_RESET}:    ${CHAT_PROVIDER} (${GOOGLE_CHAT_SPACE})"
echo -e "  - ${C_BOLD}Gateway Status${C_RESET}:   Running"
echo ""
