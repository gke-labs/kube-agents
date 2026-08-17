#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 8: Deploy PlatformAgent Custom Resource Manifest
# ==============================================================================
# Idempotent script that connects to GKE, renders the platform-agent.yaml
# template, deploys it to the cluster, labels the host cluster for discovery,
# and fails unless the operator reconciles the change into the agent Deployment
# (override the wait budget with AGENT_READY_TIMEOUT, default 600s). Whether the
# Deployment then rolls out is verified by step 13, after the agent's
# dependencies exist.
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
check_prereqs "gcloud" "kubectl" "envsubst"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State for Agent Deployment"
# This step deploys the platform agent image from this repo, so it needs a tag.
REQUIRES_IMAGE_TAG=1
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "$DEFAULT_REGION" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "$DEFAULT_CLUSTER_NAME" "Enter GKE Cluster Name"
init_var "ENABLE_GVISOR" "false" "Enable GKE Sandbox (gVisor) runtime isolation? (true/false)"
init_var_model_provider

# Map global state variables to expected template variables
export GSA_NAME="${PLATFORM_AGENT_GSA_NAME}"
export KSA_NAME="${PLATFORM_AGENT_KSA_NAME}"

DEFAULT_AGENT_IMAGE="$(registry_prefix)/platform-agent"
init_var "AGENT_IMAGE" "$DEFAULT_AGENT_IMAGE" "Enter Platform Agent Image Path"
warn_on_registry_prefix_mismatch "AGENT_IMAGE"
init_var "MEMORY_ENABLED" "false" "Enable Hermes' built-in MEMORY.md store? (true/false)"
init_var_memory_provider
init_var "USER_PROFILE_ENABLED" "false" "Enable per-user memory profiling? (true/false)"
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


# Step 2: Apply PlatformAgent Custom Resource
verify_custom_resource() {
  # Always return false to ensure configuration updates are applied to the Custom Resource
  return 1
}
execute_custom_resource() {
  print_info "Generating custom resource manifest 'platform-agent.yaml' from template..."
  local CR_TEMPLATE="${SCRIPT_DIR}/platform-agent.yaml.template"
  local CR_MANIFEST="${SCRIPT_DIR}/platform-agent.yaml"

  if [ ! -f "$CR_TEMPLATE" ]; then
    print_error "Custom resource template '$CR_TEMPLATE' not found!"
    exit 1
  fi

  # Determine if Google Chat should be enabled
  if [ "${GOOGLE_CHAT_ENABLED:-false}" = "true" ]; then
    export GOOGLE_CHAT_ENABLED="true"
    if [ -z "${CHAT_TOPIC_NAME}" ] || [ -z "${CHAT_SUB_NAME}" ]; then
      print_warning "Google Chat integration is enabled but CHAT_TOPIC_NAME or CHAT_SUB_NAME is missing. It may not work properly."
    fi
  else
    export GOOGLE_CHAT_ENABLED="false"
    export GOOGLE_CHAT_MODE="${GOOGLE_CHAT_MODE:-default}"
    export CHAT_TOPIC_NAME=""
    export CHAT_SUB_NAME=""
    export ALLOWED_USERS=""
  fi

  # Determine if Slack should be enabled
  if is_truthy "${SLACK_ENABLED:-false}"; then
    export SLACK_ENABLED="true"
    if [ -z "${SLACK_BOT_TOKEN}" ] || [ -z "${SLACK_APP_TOKEN}" ]; then
      print_warning "Slack integration is enabled but SLACK_BOT_TOKEN or SLACK_APP_TOKEN is missing. It may not work properly."
    fi
  else
    export SLACK_ENABLED="false"
    export SLACK_BOT_TOKEN=""
    export SLACK_APP_TOKEN=""
    export SLACK_ALLOWED_USERS=""
    export SLACK_HOME_CHANNEL=""
    export SLACK_HOME_CHANNEL_NAME=""
  fi

  # Handle optional GitHub integration variables
  if [ -n "${GITHUB_ORG:-}" ] && [ -n "${GITHUB_REPO:-}" ]; then
    export GITHUB_FULL_REPO="${GITHUB_ORG}/${GITHUB_REPO}"
  else
    export GITHUB_FULL_REPO=""
  fi

  # Normalize memory variables to strict boolean values.
  #
  # MEMORY_ENABLED and MEMORY_PROVIDER are independent, and deliberately so.
  # MEMORY_ENABLED gates Hermes' *built-in* MEMORY.md/USER.md store only; the
  # provider loads off MEMORY_PROVIDER alone. Every install this repo has ever
  # written set MEMORY_ENABLED=false and still got a working provider, so
  # deriving one from the other on upgrade would silently switch that store off.
  # Whether the agent remembers anything is MEMORY_PROVIDER's question, and
  # `none` is how it answers no.
  if is_truthy "${MEMORY_ENABLED:-false}"; then
    export MEMORY_ENABLED="true"
  else
    export MEMORY_ENABLED="false"
  fi

  if is_truthy "${USER_PROFILE_ENABLED:-false}"; then
    export USER_PROFILE_ENABLED="true"
  else
    export USER_PROFILE_ENABLED="false"
  fi

  # Normalize Hermes Dashboard variable
  if is_truthy "${HERMES_DASHBOARD_ENABLED:-false}"; then
    export HERMES_DASHBOARD_ENABLED="true"
  else
    export HERMES_DASHBOARD_ENABLED="false"
  fi

  # Ensure variables are explicitly exported so envsubst can access them
  export PROJECT_ID REGION CLUSTER_NAME MODEL_DEFAULT_NAME MODEL_PROVIDER GSA_NAME CHAT_SUB_NAME CHAT_TOPIC_NAME GOOGLE_CHAT_MODE ALLOWED_USERS AGENT_IMAGE NAMESPACE KSA_NAME GOOGLE_CHAT_ENABLED SLACK_ENABLED SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_ALLOWED_USERS SLACK_HOME_CHANNEL SLACK_HOME_CHANNEL_NAME IMAGE_TAG GITHUB_FULL_REPO MEMORY_ENABLED MEMORY_PROVIDER USER_PROFILE_ENABLED HERMES_DASHBOARD_ENABLED

  envsubst < "$CR_TEMPLATE" > "$CR_MANIFEST"
  
  if is_truthy "$ENABLE_GVISOR"; then
    print_info "Enabling gVisor runtimeClassName in '$CR_MANIFEST'..."
    sed -i.bak 's/# runtimeClassName: gvisor/runtimeClassName: gvisor/g' "$CR_MANIFEST" && rm -f "${CR_MANIFEST}.bak"
  fi

  local deploy_name="platform-agent-gateway"

  # Remember both generations before applying. The PlatformAgent CRD has the
  # status subresource, so kubectl apply bumps the CR's metadata.generation iff
  # the spec actually changed — which tells us whether the operator has a new
  # spec to reconcile, or this apply was a genuine no-op.
  local prev_deploy_generation prev_cr_generation
  prev_deploy_generation=$(kubectl get "deployment/${deploy_name}" -n "${NAMESPACE}" \
      -o jsonpath='{.metadata.generation}' 2>/dev/null || echo "")
  prev_cr_generation=$(kubectl get platformagent platform-agent -n "${NAMESPACE}" \
      -o jsonpath='{.metadata.generation}' 2>/dev/null || echo "")

  print_info "Applying 'platform-agent' Custom Resource to the GKE cluster..."
  kubectl apply -f "$CR_MANIFEST" || return 1

  # Applying the CR only tells us the operator accepted it; gate on the operator
  # having *reconciled* it. The CR's own Ready condition cannot carry that
  # information: it is derived from the live replica count and AgentStatus has
  # no observedGeneration (#534), so on a re-apply (verify_custom_resource
  # always returns 1, so this is the normal path) it can still describe the
  # previous spec. Whether the reconciled Deployment then rolls out healthy is
  # deliberately NOT checked here — the agent's model backend (the litellm
  # Service) is deployed by stage 09, after this one, so a fresh install cannot
  # become Ready yet. Step 13 verifies the rollout once the pipeline has
  # deployed everything the agent needs.
  ensure_k8s_resource_exists "deployment/${deploy_name}" "${NAMESPACE}" \
      "$(( AGENT_READY_TIMEOUT_SECONDS / 2 ))" || return 1

  local new_cr_generation
  new_cr_generation=$(kubectl get platformagent platform-agent -n "${NAMESPACE}" \
      -o jsonpath='{.metadata.generation}' 2>/dev/null || echo "")

  # If this apply changed the CR spec of an already-running install, the
  # operator must translate it into a Deployment update; a Deployment whose
  # generation never moves means the change was silently not delivered.
  # ConfigMap-only changes still count: the operator stamps config hashes into
  # the pod template annotations, so they too bump the Deployment generation.
  if [ -n "$prev_deploy_generation" ] && [ -n "$new_cr_generation" ] && \
     [ "$new_cr_generation" != "$prev_cr_generation" ]; then
    print_info "CR spec changed (generation ${prev_cr_generation:-none} -> ${new_cr_generation}); waiting for the operator to update the Deployment..."
    local waited=0 current_generation=""
    while [ "$waited" -lt "$AGENT_READY_TIMEOUT_SECONDS" ]; do
      current_generation=$(kubectl get "deployment/${deploy_name}" -n "${NAMESPACE}" \
          -o jsonpath='{.metadata.generation}' 2>/dev/null || echo "")
      [ -n "$current_generation" ] && [ "$current_generation" != "$prev_deploy_generation" ] && break
      sleep 3
      waited=$((waited + 3))
    done
    if [ "$current_generation" = "$prev_deploy_generation" ] || [ -z "$current_generation" ]; then
      print_error "Operator did not reconcile the changed PlatformAgent spec into deployment/${deploy_name} within ${AGENT_READY_TIMEOUT}."
      kubectl get platformagent platform-agent -n "${NAMESPACE}" \
          -o jsonpath='{.status.phase}{"\n"}{range .status.conditions[*]}{.type}={.status} {.reason}: {.message}{"\n"}{end}' 2>/dev/null || true
      return 1
    fi
  fi
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_step "2. Apply PlatformAgent Custom Resource" verify_custom_resource execute_custom_resource 0
register_host_label

# ─── Conclusion Checklist ─────────────────────────────────────────────────────
echo -e "\n${C_GREEN}${C_BOLD}✓ PlatformAgent Custom Resource applied and reconciled by the operator!${C_RESET}"
echo -e "  ${C_CYAN}The workload rollout is verified by step 13, after the agent's dependencies are deployed.${C_RESET}"
