#!/usr/bin/env bash
# ==============================================================================
# 🧹 Extra Step 1: Teardown OperatorAgent & DevTeamAgent Custom Resources & IAM
# ==============================================================================
# Idempotent script to clean up applied OperatorAgent and DevTeamAgent custom
# resources, delete their local generated manifest files, and remove dedicated GSAs.
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
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Configuration State Restoration ──────────────────────────────────────────
ensure_teardown_state

init_var "TARGET_CLUSTER_NAME" "ac-3" "Enter Target GKE Cluster Name"
init_var "TARGET_CLUSTER_LOCATION" "us-central1" "Enter Target GKE Cluster Location"
init_var "TARGET_NAMESPACE" "devteam-app-ns" "Enter Target Workload Namespace"

# ─── Confirmation Prompt ──────────────────────────────────────────────────────
confirm_action "This will permanently delete OperatorAgent and DevTeamAgent Custom Resources and dedicated GSAs." \
  "GCP Project:$PROJECT_ID" \
  "Host GKE Cluster:$CLUSTER_NAME" \
  "Target Cluster:$TARGET_CLUSTER_NAME" \
  "Target Namespace:$TARGET_NAMESPACE"

gcloud config set project "$PROJECT_ID" --quiet

# ─── Step 1: Connect to Host GKE Cluster ──────────────────────────────────────
CLUSTER_EXISTS=$(cluster_exists)
if [ -n "$CLUSTER_EXISTS" ]; then
  connect_cluster || true
else
  echo -e "  ${C_GREEN}✓ GKE cluster '${CLUSTER_NAME}' does not exist. Skipping custom resource cleanup.${C_RESET}"
fi

# ─── Step 2: Delete Extra Agent Custom Resources ──────────────────────────────
if [ -n "$CLUSTER_EXISTS" ]; then
  OP_CR_NAME="operator-agent-${TARGET_CLUSTER_NAME}-${TARGET_CLUSTER_LOCATION}"
  DT_CR_NAME="devteam-agent-${TARGET_CLUSTER_NAME}-${TARGET_CLUSTER_LOCATION}-${TARGET_NAMESPACE}"

  OP_CRD_EXISTS=$(kubectl get crd operatoragents.kubeagents.x-k8s.io --ignore-not-found 2>/dev/null || echo "")
  if [ -n "$OP_CRD_EXISTS" ]; then
    OP_CR_EXISTS=$(kubectl get operatoragents.kubeagents.x-k8s.io "$OP_CR_NAME" -n "$NAMESPACE" --ignore-not-found 2>/dev/null || echo "")
    if [ -n "$OP_CR_EXISTS" ]; then
      echo -e "  ${C_CYAN}ℹ Deleting OperatorAgent '${OP_CR_NAME}'...${C_RESET}"
      if [ "${DRY_RUN:-0}" -eq 1 ]; then
        echo -e "  ${C_GREEN}[DRY-RUN] Would delete OperatorAgent '${OP_CR_NAME}' in namespace '${NAMESPACE}'.${C_RESET}"
      else
        kubectl delete operatoragents.kubeagents.x-k8s.io "$OP_CR_NAME" -n "$NAMESPACE" --timeout=60s || {
          echo -e "  ${C_YELLOW}⚠ Timeout waiting for OperatorAgent deletion. Force removing finalizers if present...${C_RESET}"
          kubectl delete validatingwebhookconfiguration kubeagents-validating-webhook-configuration --ignore-not-found 2>/dev/null || true
          kubectl patch operatoragents.kubeagents.x-k8s.io "$OP_CR_NAME" -n "$NAMESPACE" -p '{"metadata":{"finalizers":null}}' --type=merge || true
          kubectl delete operatoragents.kubeagents.x-k8s.io "$OP_CR_NAME" -n "$NAMESPACE" --ignore-not-found --timeout=30s || true
        }
        echo -e "  ${C_GREEN}✓ OperatorAgent '${OP_CR_NAME}' successfully deleted.${C_RESET}"
      fi
    else
      echo -e "  ${C_GREEN}✓ OperatorAgent '${OP_CR_NAME}' does not exist.${C_RESET}"
    fi
  fi

  DT_CRD_EXISTS=$(kubectl get crd devteamagents.kubeagents.x-k8s.io --ignore-not-found 2>/dev/null || echo "")
  if [ -n "$DT_CRD_EXISTS" ]; then
    DT_CR_EXISTS=$(kubectl get devteamagents.kubeagents.x-k8s.io "$DT_CR_NAME" -n "$NAMESPACE" --ignore-not-found 2>/dev/null || echo "")
    if [ -n "$DT_CR_EXISTS" ]; then
      echo -e "  ${C_CYAN}ℹ Deleting DevTeamAgent '${DT_CR_NAME}'...${C_RESET}"
      if [ "${DRY_RUN:-0}" -eq 1 ]; then
        echo -e "  ${C_GREEN}[DRY-RUN] Would delete DevTeamAgent '${DT_CR_NAME}' in namespace '${NAMESPACE}'.${C_RESET}"
      else
        kubectl delete devteamagents.kubeagents.x-k8s.io "$DT_CR_NAME" -n "$NAMESPACE" --timeout=60s || {
          echo -e "  ${C_YELLOW}⚠ Timeout waiting for DevTeamAgent deletion. Force removing finalizers if present...${C_RESET}"
          kubectl delete validatingwebhookconfiguration kubeagents-validating-webhook-configuration --ignore-not-found 2>/dev/null || true
          kubectl patch devteamagents.kubeagents.x-k8s.io "$DT_CR_NAME" -n "$NAMESPACE" -p '{"metadata":{"finalizers":null}}' --type=merge || true
          kubectl delete devteamagents.kubeagents.x-k8s.io "$DT_CR_NAME" -n "$NAMESPACE" --ignore-not-found --timeout=30s || true
        }
        echo -e "  ${C_GREEN}✓ DevTeamAgent '${DT_CR_NAME}' successfully deleted.${C_RESET}"
      fi
    else
      echo -e "  ${C_GREEN}✓ DevTeamAgent '${DT_CR_NAME}' does not exist.${C_RESET}"
    fi
  fi
fi

# ─── Step 3: Clean up Local Manifest Files ────────────────────────────────────
op_yaml="${OPERATOR_DIR}/examples/operatoragent.yaml"
dt_yaml="${OPERATOR_DIR}/examples/devteamagent.yaml"
for local_yaml in "$op_yaml" "$dt_yaml"; do
  if [ -f "$local_yaml" ]; then
    if [ "${DRY_RUN:-0}" -eq 1 ]; then
      echo -e "  ${C_GREEN}[DRY-RUN] Would delete local manifest ${local_yaml}.${C_RESET}"
    else
      rm -f "$local_yaml"
      echo -e "  ${C_GREEN}✓ Deleted $(basename "$local_yaml")${C_RESET}"
    fi
  fi
done

# ─── Step 4: Clean up Dedicated GCP IAM & GSAs ────────────────────────────────
cleanup_agent_iam() {
  local ksa_name=$1
  local gsa_name=$2
  shift 2
  local roles=("$@")
  
  local gsa_email="${gsa_name}@${PROJECT_ID}.iam.gserviceaccount.com"
  
  local gsa_exists=0
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    gsa_exists=1
  elif gcloud iam service-accounts describe "${gsa_email}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gsa_exists=1
  fi

  if [ "$gsa_exists" -eq 1 ]; then
    echo -e "  ${C_CYAN}ℹ Removing project-level IAM policy bindings for ${gsa_name}...${C_RESET}"
    for role in "${roles[@]}"; do
      if [ "${DRY_RUN:-0}" -eq 1 ]; then
        echo -e "  ${C_GREEN}[DRY-RUN] Would remove project-level IAM policy binding '${role}' for ${gsa_name}.${C_RESET}"
      else
        gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
            --member="serviceAccount:${gsa_email}" \
            --role="${role}" \
            --quiet 2>/dev/null || true
      fi
    done

    echo -e "  ${C_CYAN}ℹ Removing Workload Identity Policy Binding for ${gsa_name}...${C_RESET}"
    local wi_member="serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${ksa_name}]"
    if [ "${DRY_RUN:-0}" -eq 1 ]; then
      echo -e "  ${C_GREEN}[DRY-RUN] Would remove Workload Identity binding for ${gsa_name} to ${ksa_name}.${C_RESET}"
    else
      gcloud iam service-accounts remove-iam-policy-binding "${gsa_email}" \
          --role="roles/iam.workloadIdentityUser" \
          --member="${wi_member}" \
          --project="${PROJECT_ID}" \
          --quiet 2>/dev/null || true
    fi

    echo -e "  ${C_CYAN}ℹ Deleting GSA ${gsa_name}...${C_RESET}"
    if [ "${DRY_RUN:-0}" -eq 1 ]; then
      echo -e "  ${C_GREEN}[DRY-RUN] Would delete GSA ${gsa_name}.${C_RESET}"
    else
      gcloud iam service-accounts delete "${gsa_email}" --project="${PROJECT_ID}" --quiet || true
      echo -e "  ${C_GREEN}✓ GSA '${gsa_name}' successfully removed.${C_RESET}"
    fi
  else
    echo -e "  ${C_GREEN}✓ GSA '${gsa_name}' does not exist. Skipping cleanup.${C_RESET}"
  fi
}

export TARGET_OP_KSA="operator-agent-${TARGET_CLUSTER_NAME}"
export TARGET_OP_GSA="op-gsa-${TARGET_CLUSTER_NAME}"
export TARGET_DT_KSA="devteam-agent-${TARGET_CLUSTER_NAME}-${TARGET_NAMESPACE}"
export TARGET_DT_GSA="dt-gsa-${TARGET_CLUSTER_NAME}"

cleanup_agent_iam "${TARGET_OP_KSA}" "${TARGET_OP_GSA}" \
    "roles/container.clusterViewer" \
    "roles/monitoring.viewer" \
    "roles/logging.viewer" \
    "roles/iam.serviceAccountUser"

cleanup_agent_iam "${TARGET_DT_KSA}" "${TARGET_DT_GSA}" \
    "roles/container.clusterViewer" \
    "roles/monitoring.viewer" \
    "roles/logging.viewer" \
    "roles/iam.serviceAccountUser"

save_var "EXTRA_AGENTS_DEPLOYED" "false"

echo -e "\n${C_GREEN}${C_BOLD}✅ Extra Agents Custom Resources & IAM successfully cleaned up!${C_RESET}"
