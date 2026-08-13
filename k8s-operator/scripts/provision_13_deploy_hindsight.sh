#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 13: Deploy Hindsight Memory Store
# ==============================================================================
# Idempotent script that connects to GKE and deploys Hindsight — the API server
# and the Postgres/pgvector database behind the Chat Agent's long-term memory.
# Requires step 9, since Hindsight sends its extraction and consolidation calls
# through the LiteLLM gateway. Skipped unless the install asked for it:
# MEMORY_PROVIDER must name a Hindsight-backed provider, so an install that chose
# `multiuser_memory` or `none` runs no database.
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
check_prereqs "gcloud" "kubectl"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State for Hindsight Deployment"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "us-east4" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "platform-agent-host" "Enter GKE Cluster Name"
init_var_memory_provider

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
  print_info "Deploying Hindsight memory store into GKE..."
  export NAMESPACE
  make -C "${OPERATOR_DIR}" deploy-hindsight || return 1
}

# Step 3: Wait for the API to come up
#
# Worth waiting on rather than firing and forgetting: the API image loads its
# embedding and reranking models at startup, so a first roll takes visibly longer
# than the rest of the install and a failure here is silent until the first
# person tries to use memory.
verify_hindsight_ready() {
  kubectl rollout status deploy/hindsight-api -n "$NAMESPACE" --timeout=5m
}
execute_hindsight_ready() {
  kubectl rollout status deploy/hindsight-api -n "$NAMESPACE" --timeout=5m
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
