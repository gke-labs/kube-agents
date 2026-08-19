#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 13: Deploy Hindsight Memory Store
# ==============================================================================
# Idempotent script that connects to GKE and deploys Hindsight — the API server
# and the Postgres/pgvector database behind the Chat Agent's long-term memory.
# Requires step 9, since Hindsight sends its extraction and consolidation calls
# through the LiteLLM gateway. Skipped unless the install asked for it:
# MEMORY_PROVIDER must name a Hindsight-backed provider, so an install that chose
# `multiuser_memory` or `none` runs no database. Waits for the API to roll out
# and fails with diagnostics if it does not (override the wait budget with
# AGENT_READY_TIMEOUT, default 600s; a cold roll spends most of it pulling a
# 1.4 GB image and loading models).
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
check_prereqs "gcloud" "kubectl" "jq"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State for Hindsight Deployment"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "us-east4" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "platform-agent-host" "Enter GKE Cluster Name"
init_var_memory_provider
init_agent_ready_timeout

# ─── Memory Selection Gate ────────────────────────────────────────────────────
#
# Hindsight is one memory provider among several, so deploying an API server and
# a Postgres database is only right when this install chose that provider.
#
# MEMORY_PROVIDER alone decides it. MEMORY_ENABLED is deliberately not consulted:
# it gates Hermes' built-in MEMORY.md store, not the provider, and every install
# this repo has written set it false while running a provider quite happily.
# Reading it here would have skipped the deployment for every upgrade.
# `none` is how an install says it wants no memory at all.
#
# The gate lives here rather than in provision.sh because this step is also
# reachable on its own, through `make gcp-provision-13-deploy-hindsight`.
#
# Exits 0, not 1: "nothing to deploy" is the correct outcome of this step for
# these settings, not a failure of it. Switching the provider later is a matter
# of re-running the step — nothing about it is one-way.
if ! memory_provider_uses_hindsight "$MEMORY_PROVIDER"; then
  print_info "Memory provider is '${MEMORY_PROVIDER}', which does not use Hindsight."
  print_info "Skipping the Hindsight deployment. Re-run this step after switching"
  print_info "MEMORY_PROVIDER to 'kube_agents_memory' if you want it."
  exit 0
fi

# ─── Image Resolution ─────────────────────────────────────────────────────────
#
# Resolved from images.json, and redirected onto THIRD_PARTY_REGISTRY_PREFIX
# when one is set, so an approved-registry install reaches these two as well.
# After the gate rather than before it: an install on another memory provider
# deploys nothing here and has no reason to care whether the mirror holds them.
init_third_party_image "HINDSIGHT_API_IMAGE" "hindsight-api"
init_third_party_image "HINDSIGHT_POSTGRES_IMAGE" "hindsight-postgresql"

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

# Step 2: Deploy Hindsight
verify_hindsight() {
  # Always return false so the manifests are re-applied idempotently every run.
  return 1
}
execute_hindsight() {
  print_info "Deploying Hindsight memory store (${HINDSIGHT_API_IMAGE}) into GKE..."
  export NAMESPACE HINDSIGHT_API_IMAGE HINDSIGHT_POSTGRES_IMAGE
  make -C "${OPERATOR_DIR}" deploy-hindsight || return 1
}

# Step 3: Wait for the API to come up
#
# Worth waiting on rather than firing and forgetting: the API image loads its
# embedding and reranking models at startup, so a first roll takes visibly longer
# than the rest of the install and a failure here is silent until the first
# person tries to use memory.
#
# On the shared readiness budget (AGENT_READY_TIMEOUT, default 600s) rather than
# a hardcoded 5m, which was too short for what api.yaml's probe comment
# describes: a cold roll pulls the image and only then spends a 5-minute
# startupProbe budget loading models. run_step turns a false return into an
# `exit 1`, so a gate that expires fails the whole install rather than reporting
# a slow one — which is why it is worth having room, and why it fails loudly
# (#712).
verify_hindsight_ready() {
  # Always false, the way step 14's gate is. run_step runs verify and then
  # execute, so waiting in both would give a stuck rollout two full budgets
  # back to back; and `rollout status` on a finished rollout returns at once,
  # so there is nothing for a verify to save.
  return 1
}
execute_hindsight_ready() {
  print_info "Waiting for hindsight-api rollout to complete (timeout ${AGENT_READY_TIMEOUT})..."
  kubectl rollout status deploy/hindsight-api -n "$NAMESPACE" \
      --timeout="${AGENT_READY_TIMEOUT}" || {
    # Same shape as step 14's gate. Without this the operator's only signal is
    # kubectl's one-line "timed out waiting for the condition", after a wait
    # long enough that they have stopped watching.
    print_error "hindsight-api did not roll out successfully."
    kubectl get pods -n "$NAMESPACE" -l app.kubernetes.io/name=hindsight,app.kubernetes.io/component=api \
        -o jsonpath='{range .items[*]}{.metadata.name}: phase={.status.phase}{"\n"}{range .status.containerStatuses[*]}  {.name}: ready={.ready} restarts={.restartCount} state={.state}{"\n"}{end}{end}' 2>/dev/null || true
    print_info "Recent container logs:"
    kubectl logs -n "$NAMESPACE" -l app.kubernetes.io/name=hindsight,app.kubernetes.io/component=api \
        --all-containers --prefix --tail=20 2>/dev/null || true
    return 1
  }
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_step "2. Deploy Hindsight memory store" verify_hindsight execute_hindsight 0
run_step "3. Wait for the Hindsight API" verify_hindsight_ready execute_hindsight_ready 0

# ─── Conclusion Checklist ─────────────────────────────────────────────────────
echo -e "\n${C_GREEN}${C_BOLD}✓ Hindsight memory store deployed successfully to GKE!${C_RESET}"
echo -e "  ${C_CYAN}ℹ There are no memory banks to create. The Chat Agent's provider${C_RESET}"
echo -e "  ${C_CYAN}  creates its bank, mission and retain strategies on the first${C_RESET}"
echo -e "  ${C_CYAN}  session that stores anything.${C_RESET}"
