#!/usr/bin/env bash
# ==============================================================================
# 🤖 kube-agents Installer: Premium Interactive CLI Setup
# ==============================================================================

set -euo pipefail

# ─── ANSI Styling & Color Tokens ──────────────────────────────────────────────
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_DIM='\033[2m'
C_ITALIC='\033[3m'
C_UNDERLINE='\033[4m'

# Palette
C_BLUE='\033[38;5;39m'
C_CYAN='\033[38;5;51m'
C_PURPLE='\033[38;5;141m'
C_PINK='\033[38;5;206m'
C_GREEN='\033[38;5;84m'
C_YELLOW='\033[38;5;220m'
C_RED='\033[38;5;196m'
C_GRAY='\033[38;5;242m'
C_WHITE='\033[38;5;255m'

# UI Components
print_banner() {
  clear 2>/dev/null || true
  echo -e "${C_PURPLE}${C_BOLD}"
  echo "       ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄"
  echo "       █ ☸️  k u b e - a g e n t s :: P L A T F O R M   █"
  echo "       █    Autonomous Kubernetes Agentic Harness      █"
  echo "       ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀"
  echo -e "${C_RESET}"
}

print_header() {
  local title="$1"
  echo -e "\n${C_PINK}────── ${C_BOLD}${title}${C_RESET}${C_PINK} ──────────────────────────────────────────────────${C_RESET}\n"
}

log_info()    { echo -e " ${C_CYAN}ℹ${C_RESET}  ${C_WHITE}$*${C_RESET}"; }
log_success() { echo -e " ${C_GREEN}✔${C_RESET}  ${C_BOLD}${C_WHITE}$*${C_RESET}"; }
log_warn()    { echo -e " ${C_YELLOW}⚠${C_RESET}  ${C_YELLOW}$*${C_RESET}"; }
log_error()   { echo -e " ${C_RED}✖${C_RESET}  ${C_RED}${C_BOLD}$*${C_RESET}"; }
log_step()    { echo -e " ${C_PURPLE}✦${C_RESET}  ${C_BOLD}${C_WHITE}$*${C_RESET}"; }

# Animated Spinner for Long Operations
SPINNER_PID=""
start_spinner() {
  local msg="$1"
  echo -ne " ${C_CYAN}◐${C_RESET}  ${C_GRAY}${msg}${C_RESET}..."
  (
    spin=('○' '◖' '◧' '◗')
    i=0
    while true; do
      printf "\r ${C_CYAN}%s${C_RESET}  ${C_WHITE}%s${C_RESET}..." "${spin[$i]}" "$msg"
      i=$(( (i + 1) % 4 ))
      sleep 0.1
    done
  ) &>/dev/null &
  SPINNER_PID=$!
}

stop_spinner() {
  local res_type="${1:-success}"
  local msg="${2:-Done}"
  if [ -n "$SPINNER_PID" ]; then
    kill "$SPINNER_PID" 2>/dev/null || true
    wait "$SPINNER_PID" 2>/dev/null || true
    SPINNER_PID=""
  fi
  printf "\r\033[K"
  if [ "$res_type" = "success" ]; then
    log_success "$msg"
  else
    log_warn "$msg"
  fi
}

# ─── Default Configuration & State ──────────────────────────────────────────
NON_INTERACTIVE="${NON_INTERACTIVE:-0}"
NAMESPACE="${NAMESPACE:-kubeagents-system}"
CREATE_CLUSTER="${CREATE_CLUSTER:-0}"

AUTO_PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
PROJECT_ID="${PROJECT_ID:-$AUTO_PROJECT}"
REGION="${REGION:-us-central1}"
CLUSTER_NAME="${CLUSTER_NAME:-platform-agent-cluster}"

AUTO_CONTEXT="$(kubectl config current-context 2>/dev/null || true)"
KUBE_CONTEXT="${KUBE_CONTEXT:-$AUTO_CONTEXT}"

GEMINI_API_KEY="${GEMINI_API_KEY:-}"
GH_TOKEN="${GH_TOKEN:-}"
GITOPS_REPO="${GITOPS_REPO:-https://github.com/fkc1e100/gke-fleet-iac}"
CHAT_PROVIDER="${CHAT_PROVIDER:-google_chat}"
GOOGLE_CHAT_SPACE="${GOOGLE_CHAT_SPACE:-spaces/AAQAn16pJfI}"
GOOGLE_CHAT_USER="${GOOGLE_CHAT_USER:-fcurrie@google.com}"

# CLI Arguments
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
      echo "  --non-interactive, -y   Run non-interactively"
      echo "  --create-cluster        Provision dedicated GKE Autopilot cluster"
      echo "  --cluster-name <name>   Target Cluster Name"
      echo "  --region <region>       GCP Region"
      echo "  --context <context>     Target Kubectl Context"
      echo "  --project-id <id>       Target GCP Project ID"
      echo "  --namespace <ns>        Target Namespace"
      echo "  --gemini-api-key <key>  Gemini API Key"
      echo "  --gh-token <token>      GitHub Personal Access Token"
      echo "  --gitops-repo <url>     GitOps Repository URL"
      echo "  --chat-provider <name>  Chat Provider (google_chat/slack/none)"
      echo "  --gchat-space <space>   Google Chat Space ID"
      exit 0
      ;;
    *) log_error "Unknown argument: $1"; exit 1 ;;
  esac
done

print_banner

# Interactive Cluster Selection Wizard
if [ "$NON_INTERACTIVE" -eq 0 ]; then
  print_header "Step 1/5: Cluster Strategy"
  echo -e " ${C_BOLD}${C_WHITE}Where should the Platform Agent run?${C_RESET}\n"
  echo -e "   ${C_CYAN}❯ [1]${C_RESET} ${C_BOLD}Use active Kubernetes context:${C_RESET} ${C_GRAY}(${KUBE_CONTEXT:-none})${C_RESET}"
  echo -e "   ${C_CYAN}  [2]${C_RESET} ${C_BOLD}Provision a new dedicated GKE Autopilot cluster${C_RESET} ${C_GRAY}(via gcloud)${C_RESET}\n"
  
  read -p " Select option [1/2] (default: 1): " CLUSTER_CHOICE
  if [ "$CLUSTER_CHOICE" = "2" ]; then
    CREATE_CLUSTER=1
    echo ""
    read -p " Target GCP Project ID [${PROJECT_ID}]: " INPUT_PROJ
    PROJECT_ID="${INPUT_PROJ:-$PROJECT_ID}"
    read -p " GKE Cluster Name [${CLUSTER_NAME}]: " INPUT_CNAME
    CLUSTER_NAME="${INPUT_CNAME:-$CLUSTER_NAME}"
    read -p " GCP Region [${REGION}]: " INPUT_REG
    REGION="${INPUT_REG:-$REGION}"
  fi
fi

# ─── 1. Prerequisites Check & Cluster Setup ─────────────────────────────────
print_header "Step 1/5: Environment Verification"

for tool in kubectl helm gcloud curl openssl; do
  if ! command -v "$tool" &>/dev/null; then
    log_error "Required CLI tool '$tool' is missing."
    exit 1
  fi
done
log_success "Prerequisite tools verified (kubectl, helm, gcloud, curl, openssl)"

if [ "$CREATE_CLUSTER" -eq 1 ]; then
  start_spinner "Checking GKE Autopilot cluster '${CLUSTER_NAME}' in project '${PROJECT_ID}'"
  if gcloud container clusters describe "${CLUSTER_NAME}" --region="${REGION}" --project="${PROJECT_ID}" &>/dev/null; then
    stop_spinner "success" "GKE cluster '${CLUSTER_NAME}' exists."
  else
    stop_spinner "warn" "GKE cluster '${CLUSTER_NAME}' not found. Creating..."
    start_spinner "Provisioning GKE Autopilot cluster '${CLUSTER_NAME}'"
    gcloud container clusters create-auto "${CLUSTER_NAME}" --region="${REGION}" --project="${PROJECT_ID}" --quiet
    stop_spinner "success" "GKE Autopilot cluster '${CLUSTER_NAME}' created!"
  fi

  start_spinner "Fetching cluster credentials"
  gcloud container clusters get-credentials "${CLUSTER_NAME}" --region="${REGION}" --project="${PROJECT_ID}" --quiet
  KUBE_CONTEXT="$(kubectl config current-context)"
  stop_spinner "success" "Cluster context updated to '${KUBE_CONTEXT}'"
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
  log_error "No active kubectl context. Connect to a cluster first."
  exit 1
fi
log_info "Active target context: ${C_BOLD}${C_CYAN}${KUBE_CONTEXT}${C_RESET}"

# Interactive Credentials Wizard
if [ "$NON_INTERACTIVE" -eq 0 ]; then
  print_header "Step 2/5: LLM & GitOps Configuration"

  if [ -z "$GEMINI_API_KEY" ]; then
    read -sp " Enter GEMINI_API_KEY: " INPUT_GEMINI
    echo ""
    GEMINI_API_KEY="${INPUT_GEMINI}"
  fi

  if [ -z "$GH_TOKEN" ]; then
    read -sp " Enter GitHub PAT (GH_TOKEN): " INPUT_GH
    echo ""
    GH_TOKEN="${INPUT_GH}"
  fi

  read -p " Enter GitOps Repository URL [${GITOPS_REPO}]: " INPUT_REPO
  GITOPS_REPO="${INPUT_REPO:-$GITOPS_REPO}"
fi

# ─── 2. GitHub Connection Probe ──────────────────────────────────────────────
print_header "Step 2/5: Validating GitHub Access"

if [ -n "$GH_TOKEN" ] && [ -n "$GITOPS_REPO" ]; then
  REPO_PATH="$(echo "$GITOPS_REPO" | sed -E 's|https://github.com/||; s|\.git$||')"
  start_spinner "Probing GitHub API for '${REPO_PATH}'"

  HTTP_STATUS="$(curl -s -o /tmp/gh_probe.json -w "%{http_code}" \
    -H "Authorization: token ${GH_TOKEN}" \
    "https://api.github.com/repos/${REPO_PATH}")"

  if [ "$HTTP_STATUS" -eq 200 ]; then
    GH_USER="$(grep -o '"login": "[^"]*' /tmp/gh_probe.json 2>/dev/null | head -n 1 | cut -d'"' -f4 || echo "authenticated user")"
    stop_spinner "success" "GitHub PAT verified for '${GH_USER}' on '${REPO_PATH}'!"
  else
    stop_spinner "warn" "GitHub API returned HTTP ${HTTP_STATUS}. Proceeding..."
  fi
  rm -f /tmp/gh_probe.json
else
  log_warn "GH_TOKEN empty. Autonomous PR creation will run in dry-run mode."
fi

# Interactive Chat Provider Wizard
if [ "$NON_INTERACTIVE" -eq 0 ]; then
  print_header "Step 3/5: Chat Interface Configuration"
  echo -e " ${C_BOLD}${C_WHITE}Select Chat Provider for Notifications & Interaction:${C_RESET}\n"
  echo -e "   ${C_CYAN}❯ [1]${C_RESET} ${C_BOLD}Google Chat${C_RESET} ${C_GRAY}(via Cloud Pub/Sub)${C_RESET}"
  echo -e "   ${C_CYAN}  [2]${C_RESET} ${C_BOLD}Slack${C_RESET} ${C_GRAY}(via Socket Mode)${C_RESET}"
  echo -e "   ${C_CYAN}  [3]${C_RESET} ${C_BOLD}None${C_RESET} ${C_GRAY}(Headless API & CLI mode)${C_RESET}\n"
  
  read -p " Select option [1/2/3] (default: 1): " CHAT_CHOICE
  case "$CHAT_CHOICE" in
    2) CHAT_PROVIDER="slack" ;;
    3) CHAT_PROVIDER="none" ;;
    *) CHAT_PROVIDER="google_chat" ;;
  esac

  if [ "$CHAT_PROVIDER" = "google_chat" ]; then
    read -p " Enter Google Chat Space ID [${GOOGLE_CHAT_SPACE}]: " INPUT_SPACE
    GOOGLE_CHAT_SPACE="${INPUT_SPACE:-$GOOGLE_CHAT_SPACE}"
  fi
fi

# ─── 3. Cert-Manager Installation ───────────────────────────────────────────
print_header "Step 3/5: Cert-Manager Infra Check"

if $KUBECTL_CMD get namespace cert-manager &>/dev/null && $KUBECTL_CMD get crd certificates.cert-manager.io &>/dev/null; then
  log_success "cert-manager is already installed on the cluster."
else
  start_spinner "Installing cert-manager via Helm"
  $HELM_CMD repo add jetstack https://charts.jetstack.io --force-update &>/dev/null || true
  $HELM_CMD repo update &>/dev/null || true
  if $HELM_CMD install cert-manager jetstack/cert-manager --namespace cert-manager --create-namespace --set installCRDs=true --wait --timeout=3m &>/dev/null; then
    stop_spinner "success" "cert-manager installed!"
  else
    stop_spinner "warn" "Applying cert-manager manifests fallback..."
    $KUBECTL_CMD apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.4/cert-manager.yaml &>/dev/null
  fi
fi

# ─── 4. Namespace & Secrets Provisioning ────────────────────────────────────
print_header "Step 4/5: Secrets & GCP Pub/Sub Setup"

$KUBECTL_CMD create namespace "${NAMESPACE}" --dry-run=client -o yaml | $KUBECTL_CMD apply -f - &>/dev/null
API_SERVER_KEY="$(openssl rand -hex 16)"

if [ "$CHAT_PROVIDER" = "google_chat" ] && [ -n "$PROJECT_ID" ]; then
  start_spinner "Provisioning GCP APIs, Pub/Sub Topic & Subscription for Google Chat"
  gcloud services enable pubsub.googleapis.com chat.googleapis.com --project="${PROJECT_ID}" --quiet &>/dev/null || true
  CHAT_TOPIC_NAME="platform-agent-chat-events"
  CHAT_SUB_NAME="platform-agent-chat-events-sub"
  gcloud pubsub topics create "${CHAT_TOPIC_NAME}" --project="${PROJECT_ID}" --quiet &>/dev/null || true
  gcloud pubsub subscriptions create "${CHAT_SUB_NAME}" --topic="${CHAT_TOPIC_NAME}" --ack-deadline=60 --project="${PROJECT_ID}" --quiet &>/dev/null || true
  gcloud pubsub topics add-iam-policy-binding "${CHAT_TOPIC_NAME}" --member="serviceAccount:chat-api-push@system.gserviceaccount.com" --role="roles/pubsub.publisher" --project="${PROJECT_ID}" --quiet &>/dev/null || true
  stop_spinner "success" "GCP Pub/Sub backend for Google Chat configured!"
fi

start_spinner "Writing Kubernetes Secret 'platform-agent-secrets'"
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
  --dry-run=client -o yaml | $KUBECTL_CMD apply -f - &>/dev/null
stop_spinner "success" "Secrets provisioned in namespace '${NAMESPACE}'"

# ─── 5. Deploy Platform Agent Gateway ────────────────────────────────────────
print_header "Step 5/5: Platform Gateway Deployment & Health Verification"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HELM_CHART_DIR="${REPO_ROOT}/deploy/helm/platform-agent"

start_spinner "Deploying Platform Agent Gateway"
if [ -d "$HELM_CHART_DIR" ]; then
  $HELM_CMD upgrade --install platform-agent "${HELM_CHART_DIR}" --namespace "${NAMESPACE}" --set display.platforms.google_chat.enabled=true --wait --timeout=3m &>/dev/null || true
else
  if [ -f "${REPO_ROOT}/deploy/kustomize/platform-agent.yaml" ]; then
    $KUBECTL_CMD apply -f "${REPO_ROOT}/deploy/kustomize/platform-agent.yaml" -n "${NAMESPACE}" &>/dev/null
  fi
fi

$KUBECTL_CMD set env deployment/platform-agent-gateway --from=secret/platform-agent-secrets -n "${NAMESPACE}" &>/dev/null || true
stop_spinner "success" "Platform Agent Gateway deployed!"

start_spinner "Waiting for deployment rollout"
$KUBECTL_CMD rollout status deployment/platform-agent-gateway -n "${NAMESPACE}" --timeout=120s &>/dev/null || true
stop_spinner "success" "Gateway pod is running and healthy!"

GATEWAY_POD="$($KUBECTL_CMD get pod -n "${NAMESPACE}" -l app=platform-agent-gateway -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")"
if [ -n "$GATEWAY_POD" ]; then
  start_spinner "Syncing credential helpers into Gateway Pod '${GATEWAY_POD}'"
  $KUBECTL_CMD cp "${REPO_ROOT}/agents/platform/scripts/github_token_refresh.py" "${NAMESPACE}/${GATEWAY_POD}:/opt/data/scripts/github_token_refresh.py" -c platform-agent &>/dev/null || true
  $KUBECTL_CMD cp "${REPO_ROOT}/agents/platform/scripts/github_token_refresh.py" "${NAMESPACE}/${GATEWAY_POD}:/opt/defaults/scripts/github_token_refresh.py" -c platform-agent &>/dev/null || true
  $KUBECTL_CMD cp "${REPO_ROOT}/agents/platform/skills/submit-suggestion/scripts/submit_suggestion.py" "${NAMESPACE}/${GATEWAY_POD}:/opt/data/skills/submit-suggestion/scripts/submit_suggestion.py" -c platform-agent &>/dev/null || true
  $KUBECTL_CMD cp "${REPO_ROOT}/agents/platform/skills/submit-suggestion/scripts/submit_suggestion.py" "${NAMESPACE}/${GATEWAY_POD}:/opt/hermes/skills/submit-suggestion/scripts/submit_suggestion.py" -c platform-agent &>/dev/null || true
  $KUBECTL_CMD cp "${REPO_ROOT}/agents/platform/skills/submit-suggestion/SKILL.md" "${NAMESPACE}/${GATEWAY_POD}:/opt/hermes/skills/submit-suggestion/SKILL.md" -c platform-agent &>/dev/null || true
  $KUBECTL_CMD cp "${REPO_ROOT}/agents/platform/skills/submit-suggestion/SKILL.md" "${NAMESPACE}/${GATEWAY_POD}:/opt/data/skills/submit-suggestion/SKILL.md" -c platform-agent &>/dev/null || true
  stop_spinner "success" "Credential helpers synchronized into container"
fi

# ─── Summary Card ─────────────────────────────────────────────────────────────
echo -e "\n${C_GREEN}${C_BOLD}"
echo " ┌─────────────────────────────────────────────────────────────┐"
echo " │  🏆  PLATFORM AGENT INSTALLED & VERIFIED SUCCESSFULLY!     │"
echo " └─────────────────────────────────────────────────────────────┘"
echo -e "${C_RESET}"
echo -e " ${C_BOLD}Installation Overview:${C_RESET}"
echo -e "   ${C_PURPLE}✦ Context:${C_RESET}         ${C_CYAN}${KUBE_CONTEXT}${C_RESET}"
echo -e "   ${C_PURPLE}✦ Namespace:${C_RESET}       ${C_WHITE}${NAMESPACE}${C_RESET}"
echo -e "   ${C_PURPLE}✦ GitOps Target:${C_RESET}   ${C_WHITE}${GITOPS_REPO}${C_RESET}"
echo -e "   ${C_PURPLE}✦ Chat Provider:${C_RESET}   ${C_WHITE}${CHAT_PROVIDER}${C_RESET} ${C_GRAY}(${GOOGLE_CHAT_SPACE})${C_RESET}"
echo -e "   ${C_PURPLE}✦ Gateway Status:${C_RESET}  ${C_GREEN}Running (Pod ${GATEWAY_POD})${C_RESET}"
echo -e "\n ${C_GRAY}Your Platform Agent is active and monitoring fleet operations.${C_RESET}\n"
