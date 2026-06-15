#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 5: Teardown Kubernetes Operator (CRDs & Controller Manager)
# ==============================================================================
# Idempotent script to clean up the deployed operator and CRDs.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$SCRIPT_DIR" == */scripts ]]; then
  OPERATOR_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  OPERATOR_DIR="${SCRIPT_DIR}"
fi
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
C_CYAN='\033[96m'
C_GREEN='\033[92m'
C_YELLOW='\033[93m'
C_RED='\033[91m'
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_WHITE='\033[97m'

# ─── Argument Parsing ─────────────────────────────────────────────────────────
NO_CONFIRM=0
while [[ "$#" -gt 0 ]]; do
  case $1 in
    --no-confirm|-y) NO_CONFIRM=1 ;;
  esac
  shift
done

# ─── Configuration State Restoration ──────────────────────────────────────────
if [ -f "$VARS_FILE" ]; then
  source "$VARS_FILE"
else
  echo -e "  ${C_YELLOW}⚠ State file ${VARS_FILE} not found. Prompting for target values...${C_RESET}"
  ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
  echo -ne "  ${C_CYAN}Enter Target GCP Project ID [${C_WHITE}${ACTIVE_PROJECT}${C_CYAN}]: ${C_RESET}"
  read -r INPUT_PROJECT_ID
  export PROJECT_ID="${INPUT_PROJECT_ID:-$ACTIVE_PROJECT}"
  if [ -z "$PROJECT_ID" ]; then
    echo -e "  ${C_RED}✗ Project ID is required.${C_RESET}"
    exit 1
  fi
  export REGION="us-east4"
  export CLUSTER_NAME="platform-agent-host"
fi

# ─── Confirmation Prompt ──────────────────────────────────────────────────────
if [ "$NO_CONFIRM" -ne 1 ]; then
  echo ""
  echo -e "${C_RED}${C_BOLD}🚨 WARNING: This will permanently undeploy the Kubernetes Operator and remove its CRDs from the GKE cluster.${C_RESET}"
  echo -e "${C_YELLOW}==============================================================================${C_RESET}"
  echo -e "  ${C_BOLD}GCP Project:${C_RESET}    ${C_BOLD}${PROJECT_ID}${C_RESET}"
  echo -e "  ${C_BOLD}GKE Cluster:${C_RESET}    ${C_BOLD}${CLUSTER_NAME}${C_RESET}"
  echo -e "${C_YELLOW}==============================================================================${C_RESET}"
  echo ""
  echo -ne "  ${C_CYAN}Are you sure you want to proceed? (y/N): ${C_RESET}"
  read -r -n 1 REPLY
  echo
  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
      echo -e "  ${C_YELLOW}ℹ Aborted.${C_RESET}"
      exit 0
  fi
fi

gcloud config set project "$PROJECT_ID" --quiet

# ─── Step 1: Connect to GKE Cluster ───────────────────────────────────────────
CLUSTER_EXISTS=$(gcloud container clusters list --filter="name=${CLUSTER_NAME} AND zone:${REGION}*" --format="value(name)" --project="${PROJECT_ID}" 2>/dev/null || echo "")
if [ -n "$CLUSTER_EXISTS" ]; then
  echo -e "  ${C_CYAN}ℹ Fetching cluster credentials...${C_RESET}"
  gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet || true
else
  echo -e "  ${C_GREEN}✓ GKE cluster '${CLUSTER_NAME}' does not exist. Skipping operator cleanup.${C_RESET}"
  exit 0
fi

# ─── Step 2: Undeploy Operator Manager ────────────────────────────────────────
OPERATOR_DEPLOYED=$(kubectl get deployment kubeagents-controller-manager -n kubeagents-system --ignore-not-found 2>/dev/null || echo "")
if [ -n "$OPERATOR_DEPLOYED" ]; then
  echo -e "  ${C_CYAN}ℹ Undeploying Operator Controller Manager from GKE cluster...${C_RESET}"
  make -C "$OPERATOR_DIR" undeploy
  echo -e "  ${C_GREEN}✓ Operator Controller Manager undeployed successfully.${C_RESET}"
else
  echo -e "  ${C_GREEN}✓ Operator Controller Manager is already undeployed.${C_RESET}"
fi

# ─── Step 3: Uninstall Custom Resource Definitions (CRDs) ─────────────────────
CRDS_INSTALLED=$(kubectl get crds -o jsonpath='{.items[*].metadata.name}' 2>/dev/null | grep -o 'platformagents.kubeagents.x-k8s.io' || echo "")
if [ -n "$CRDS_INSTALLED" ]; then
  echo -e "  ${C_CYAN}ℹ Uninstalling CRDs from GKE cluster...${C_RESET}"
  make -C "$OPERATOR_DIR" uninstall
  echo -e "  ${C_GREEN}✓ CRDs uninstalled successfully.${C_RESET}"
else
  echo -e "  ${C_GREEN}✓ CRDs are already uninstalled.${C_RESET}"
fi
