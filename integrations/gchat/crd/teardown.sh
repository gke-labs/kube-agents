#!/usr/bin/env bash
# ==============================================================================
# 🧹 GKE Standard & Google Chat E2E Teardown Script
# ==============================================================================
# An idempotent, comprehensive cleanup script to tear down all GCP and GKE
# resources provisioned by provision.sh.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
C_CYAN='\033[96m'
C_GREEN='\033[92m'
C_YELLOW='\033[93m'
C_RED='\033[91m'
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_WHITE='\033[97m'

# ─── Configuration State Restoration ──────────────────────────────────────────
echo -e "${C_BOLD}=== 0. Restoring Configuration State ===${C_RESET}"
if [ -f "$VARS_FILE" ]; then
  echo -e "  ${C_GREEN}✓ Loading state variables from ${VARS_FILE}...${C_RESET}"
  # shellcheck disable=SC1090
  source "$VARS_FILE"
else
  echo -e "  ${C_YELLOW}⚠ State file ${VARS_FILE} not found. Prompting for target values...${C_RESET}"
  
  # 1. Get active GCP Project ID
  ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
  echo -ne "  ${C_CYAN}Enter Target GCP Project ID [${C_WHITE}${ACTIVE_PROJECT}${C_CYAN}]: ${C_RESET}"
  read -r INPUT_PROJECT_ID
  export PROJECT_ID="${INPUT_PROJECT_ID:-$ACTIVE_PROJECT}"
  if [ -z "$PROJECT_ID" ]; then
    echo -e "  ${C_RED}✗ Project ID is required.${C_RESET}"
    exit 1
  fi

  # 2. Get Region
  DEFAULT_REGION="us-central1"
  echo -ne "  ${C_CYAN}Enter GKE GCP Region [${C_WHITE}${DEFAULT_REGION}${C_CYAN}]: ${C_RESET}"
  read -r INPUT_REGION
  export REGION="${INPUT_REGION:-$DEFAULT_REGION}"

  # 3. Get Cluster Name
  DEFAULT_CLUSTER="platform-agent-host"
  echo -ne "  ${C_CYAN}Enter GKE Cluster Name [${C_WHITE}${DEFAULT_CLUSTER}${C_CYAN}]: ${C_RESET}"
  read -r INPUT_CLUSTER
  export CLUSTER_NAME="${INPUT_CLUSTER:-$DEFAULT_CLUSTER}"

  # 4. Get Namespace
  DEFAULT_NAMESPACE="agent-system"
  echo -ne "  ${C_CYAN}Enter GKE Target Namespace [${C_WHITE}${DEFAULT_NAMESPACE}${C_CYAN}]: ${C_RESET}"
  read -r INPUT_NAMESPACE
  export NAMESPACE="${INPUT_NAMESPACE:-$DEFAULT_NAMESPACE}"

  export REPO_NAME="hermes-agent-repo"
fi

# ─── Confirmation Prompt ──────────────────────────────────────────────────────
echo ""
echo -e "${C_RED}${C_BOLD}🚨 WARNING: This will permanently delete all GChat integration GKE cluster, GCP resources, Secret Manager keys, and docker images.${C_RESET}"
echo -e "${C_YELLOW}==============================================================================${C_RESET}"
echo -e "  ${C_BOLD}GCP Project:${C_RESET}    ${C_WHITE}${PROJECT_ID}${C_RESET}"
echo -e "  ${C_BOLD}Region:${C_RESET}         ${C_WHITE}${REGION}${C_RESET}"
echo -e "  ${C_BOLD}GKE Cluster:${C_RESET}    ${C_WHITE}${CLUSTER_NAME}${C_RESET}"
echo -e "  ${C_BOLD}Namespace:${C_RESET}      ${C_WHITE}${NAMESPACE}${C_RESET}"
echo -e "  ${C_BOLD}Artifact Repo:${C_RESET}  ${C_WHITE}${REPO_NAME}${C_RESET}"
echo -e "${C_YELLOW}==============================================================================${C_RESET}"
echo ""
echo -ne "  ${C_CYAN}Are you sure you want to proceed with teardown? (y/N): ${C_RESET}"
read -r -n 1 REPLY
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo -e "  ${C_YELLOW}ℹ Teardown aborted.${C_RESET}"
    exit 0
fi

gcloud config set project "$PROJECT_ID" --quiet

# ─── Step 1: Connect to GKE Cluster ───────────────────────────────────────────
echo -e "\n${C_BOLD}=== 1. Connecting to GKE Cluster ===${C_RESET}"
CLUSTER_EXISTS=$(gcloud container clusters list --filter="name=${CLUSTER_NAME} AND zone:${REGION}*" --format="value(name)" --project="${PROJECT_ID}" 2>/dev/null || echo "")
if [ -n "$CLUSTER_EXISTS" ]; then
  echo -e "  ${C_CYAN}ℹ Fetching cluster credentials...${C_RESET}"
  gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet || true
else
  echo -e "  ${C_GREEN}✓ GKE cluster '${CLUSTER_NAME}' does not exist. Skipping kubernetes resource cleanup.${C_RESET}"
fi

# ─── Step 2: Delete Custom Resource ───────────────────────────────────────────
if [ -n "$CLUSTER_EXISTS" ]; then
  echo -e "\n${C_BOLD}=== 2. Tearing Down HermesAgent Custom Resource ===${C_RESET}"
  
  # Check if CRD is registered
  CRD_EXISTS=$(kubectl get crd hermesagents.agent.hermes.io --ignore-not-found 2>/dev/null || echo "")
  if [ -n "$CRD_EXISTS" ]; then
    # Check if resource exists
    CR_EXISTS=$(kubectl get hermesagent platform-agent -n "$NAMESPACE" --ignore-not-found 2>/dev/null || echo "")
    if [ -n "$CR_EXISTS" ]; then
      echo -e "  ${C_CYAN}ℹ Deleting HermesAgent 'platform-agent' (this will trigger Config Connector to delete GCP GSAs, Pub/Sub topics/subscriptions, and IAM bindings)...${C_RESET}"
      kubectl delete hermesagent platform-agent -n "$NAMESPACE" --timeout=180s || {
        echo -e "  ${C_YELLOW}⚠ Timeout waiting for HermesAgent deletion. Force removing finalizers if present...${C_RESET}"
        kubectl patch hermesagent platform-agent -n "$NAMESPACE" -p '{"metadata":{"finalizers":null}}' --type=merge || true
        kubectl delete hermesagent platform-agent -n "$NAMESPACE" --ignore-not-found || true
      }
      echo -e "  ${C_GREEN}✓ HermesAgent 'platform-agent' successfully deleted.${C_RESET}"
    else
      echo -e "  ${C_GREEN}✓ HermesAgent 'platform-agent' does not exist.${C_RESET}"
    fi
  else
    echo -e "  ${C_GREEN}✓ CRD 'hermesagents.agent.hermes.io' is not registered.${C_RESET}"
  fi
fi

# ─── Step 3: Undeploy Go Operator ─────────────────────────────────────────────
if [ -n "$CLUSTER_EXISTS" ]; then
  echo -e "\n${C_BOLD}=== 3. Tearing Down Go Operator ===${C_RESET}"
  if [ -d "${SCRIPT_DIR}/hermes-operator" ]; then
    echo -e "  ${C_CYAN}ℹ Running make undeploy & make uninstall...${C_RESET}"
    (
      cd "${SCRIPT_DIR}/hermes-operator"
      make undeploy ignore-not-found=true || true
      make uninstall ignore-not-found=true || true
    )
    echo -e "  ${C_GREEN}✓ Go Operator successfully undeployed.${C_RESET}"
  else
    echo -e "  ${C_YELLOW}⚠ hermes-operator directory not found. Skipping Operator cleanup.${C_RESET}"
  fi
fi

# ─── Step 4: Clean up KCC GSA and Bindings ────────────────────────────────────
echo -e "\n${C_BOLD}=== 4. Tearing Down KCC GCP Identity ===${C_RESET}"
KCC_GSA="hermes-kcc-sa@${PROJECT_ID}.iam.gserviceaccount.com"
GSA_EXISTS=$(gcloud iam service-accounts list --filter="email=${KCC_GSA}" --format="value(email)" --project="${PROJECT_ID}" 2>/dev/null || echo "")
if [ -n "$GSA_EXISTS" ]; then
  echo -e "  ${C_CYAN}ℹ Removing Owner role from KCC GSA...${C_RESET}"
  gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${KCC_GSA}" \
      --role="roles/owner" \
      --quiet || true

  echo -e "  ${C_CYAN}ℹ Deleting KCC GSA '${KCC_GSA}'...${C_RESET}"
  gcloud iam service-accounts delete "${KCC_GSA}" --project="${PROJECT_ID}" --quiet || true
  echo -e "  ${C_GREEN}✓ KCC GSA and bindings successfully removed.${C_RESET}"
else
  echo -e "  ${C_GREEN}✓ KCC GSA '${KCC_GSA}' already deleted or does not exist.${C_RESET}"
fi

# ─── Step 5: Delete Secret Manager Placeholders ───────────────────────────────
echo -e "\n${C_BOLD}=== 5. Tearing Down Secret Manager Placeholders ===${C_RESET}"
SECRETS_TO_DELETE=("GCP_API_KEY" "GEMINI_API_KEY")
for SECRET in "${SECRETS_TO_DELETE[@]}"; do
  SECRET_EXISTS=$(gcloud secrets list --filter="name:${SECRET}" --format="value(name)" --project="${PROJECT_ID}" 2>/dev/null || echo "")
  if [ -n "$SECRET_EXISTS" ]; then
    echo -e "  ${C_CYAN}ℹ Deleting Secret '$SECRET'...${C_RESET}"
    gcloud secrets delete "$SECRET" --project="${PROJECT_ID}" --quiet || true
    echo -e "  ${C_GREEN}✓ Secret '$SECRET' successfully deleted.${C_RESET}"
  else
    echo -e "  ${C_GREEN}✓ Secret '$SECRET' already deleted or does not exist.${C_RESET}"
  fi
done

# ─── Step 6: Delete Artifact Registry Repository ──────────────────────────────
echo -e "\n${C_BOLD}=== 6. Tearing Down Artifact Registry Repo ===${C_RESET}"
REPO_EXISTS=$(gcloud artifacts repositories list --filter="name:${REPO_NAME} AND location:${REGION}" --format="value(name)" --project="${PROJECT_ID}" 2>/dev/null || echo "")
if [ -n "$REPO_EXISTS" ]; then
  echo -e "  ${C_CYAN}ℹ Deleting Artifact Registry repository '$REPO_NAME'...${C_RESET}"
  gcloud artifacts repositories delete "$REPO_NAME" --location="$REGION" --project="${PROJECT_ID}" --quiet || true
  echo -e "  ${C_GREEN}✓ Artifact Registry repository '$REPO_NAME' successfully deleted.${C_RESET}"
else
  echo -e "  ${C_GREEN}✓ Repository '$REPO_NAME' already deleted or does not exist.${C_RESET}"
fi

# ─── Step 7: Delete GKE Cluster ───────────────────────────────────────────────
echo -e "\n${C_BOLD}=== 7. Tearing Down GKE Cluster ===${C_RESET}"
if [ -n "$CLUSTER_EXISTS" ]; then
  echo -e "  ${C_CYAN}ℹ Deleting GKE Standard Cluster '$CLUSTER_NAME' in region '$REGION'...${C_RESET}"
  echo -e "    ${C_YELLOW}Note: This takes approximately 5-8 minutes in Google Cloud...${C_RESET}"
  gcloud container clusters delete "$CLUSTER_NAME" --region="$REGION" --project="${PROJECT_ID}" --quiet
  echo -e "  ${C_GREEN}✓ GKE Cluster '$CLUSTER_NAME' successfully deleted.${C_RESET}"
else
  echo -e "  ${C_GREEN}✓ Cluster '$CLUSTER_NAME' already deleted or does not exist.${C_RESET}"
fi

# ─── Step 8: Clean up Local State Files ───────────────────────────────────────
echo -e "\n${C_BOLD}=== 8. Cleaning up Local Generated Files ===${C_RESET}"
if [ -f "$VARS_FILE" ]; then
  echo -ne "  ${C_CYAN}Do you want to delete the local state file vars.sh? (keeps settings for next provision if kept) (y/N): ${C_RESET}"
  read -r -n 1 REMOVE_VARS || true
  echo
  if [[ ${REMOVE_VARS:-n} =~ ^[Yy]$ ]]; then
    rm -f "$VARS_FILE"
    echo -e "  ${C_GREEN}✓ Deleted ${VARS_FILE}${C_RESET}"
  else
    echo -e "  ${C_GREEN}✓ Kept ${VARS_FILE} for subsequent provisioning.${C_RESET}"
  fi
fi
local_yaml="${SCRIPT_DIR}/platform-agent.yaml"
if [ -f "$local_yaml" ]; then
  rm -f "$local_yaml"
  echo -e "  ${C_GREEN}✓ Deleted platform-agent.yaml${C_RESET}"
fi

echo -e "\n${C_GREEN}${C_BOLD}====================================================${C_RESET}"
echo -e "${C_GREEN}${C_BOLD}✅ Teardown Complete! All GChat GKE & GCP resources clean.${C_RESET}"
echo -e "${C_GREEN}${C_BOLD}====================================================${C_RESET}"
