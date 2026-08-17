#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 14: Verify PlatformAgent Rollout
# ==============================================================================
# Final gate of the pipeline: waits for the agent Deployment to finish rolling
# out and fails with diagnostics if it does not (override the timeout with
# AGENT_READY_TIMEOUT, default 600s). Runs last because the agent's model
# backend — the litellm Service — only exists after step 9, so step 8 cannot
# verify readiness itself.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud" "kubectl"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State for Rollout Verification"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "us-east4" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "platform-agent-host" "Enter GKE Cluster Name"
init_agent_ready_timeout

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

# Step 2: Verify the agent Deployment rollout
verify_agent_rollout() {
  # Always re-verify: a completed rollout returns from execute immediately, and
  # an in-progress or stuck one is exactly what this step exists to catch.
  return 1
}
execute_agent_rollout() {
  local deploy_name="platform-agent-gateway"

  ensure_k8s_resource_exists "deployment/${deploy_name}" "${NAMESPACE}" \
      "$(( AGENT_READY_TIMEOUT_SECONDS / 2 ))" || return 1

  print_info "Waiting for ${deploy_name} rollout to complete (timeout ${AGENT_READY_TIMEOUT})..."
  # kubectl rollout status compares the Deployment's own observedGeneration
  # against metadata.generation, so it will not accept a stale success from a
  # rollout that finished before this pipeline ran.
  kubectl rollout status "deployment/${deploy_name}" -n "${NAMESPACE}" \
      --timeout="${AGENT_READY_TIMEOUT}" || {
    print_error "PlatformAgent workload '${deploy_name}' did not roll out successfully."
    kubectl get platformagent platform-agent -n "${NAMESPACE}" \
        -o jsonpath='{.status.phase}{"\n"}{range .status.conditions[*]}{.type}={.status} {.reason}: {.message}{"\n"}{end}' 2>/dev/null || true
    print_info "Container states:"
    kubectl get pods -n "${NAMESPACE}" -l "app=${deploy_name}" \
        -o jsonpath='{range .items[*].status.containerStatuses[*]}  {.name}: ready={.ready} restarts={.restartCount}{"\n"}{end}' 2>/dev/null || true
    print_info "Recent container logs:"
    kubectl logs -n "${NAMESPACE}" -l "app=${deploy_name}" \
        --all-containers --prefix --tail=20 2>/dev/null || true
    return 1
  }
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_step "2. Verify PlatformAgent Deployment rollout" verify_agent_rollout execute_agent_rollout 0

# ─── Conclusion Checklist ─────────────────────────────────────────────────────
echo -e "\n${C_GREEN}${C_BOLD}✓ PlatformAgent workload rolled out successfully!${C_RESET}"
