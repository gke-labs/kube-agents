#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 8: Deploy PlatformAgent Custom Resource Manifest
# ==============================================================================
# Idempotent script that connects to GKE, renders the platform-agent.yaml
# template, deploys it to the cluster, and waits for the agent Deployment to
# finish rolling out (override the timeout with AGENT_READY_TIMEOUT, default
# 600s).
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
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "us-east4" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "platform-agent-host" "Enter GKE Cluster Name"
init_var "ENABLE_GVISOR" "false" "Enable GKE Sandbox (gVisor) runtime isolation? (true/false)"
init_var_model_provider

# Map global state variables to expected template variables
export GSA_NAME="${PLATFORM_AGENT_GSA_NAME}"
export KSA_NAME="${PLATFORM_AGENT_KSA_NAME}"

DEFAULT_AGENT_IMAGE="$(registry_prefix)/platform-agent"
init_var "AGENT_IMAGE" "$DEFAULT_AGENT_IMAGE" "Enter Platform Agent Image Path"
warn_on_registry_prefix_mismatch "AGENT_IMAGE"
init_var "MEMORY_ENABLED" "false" "Enable agent memory persistence? (true/false)"
init_var "MEMORY_PROVIDER" "multiuser_memory" "Enter agent memory provider"
init_var "USER_PROFILE_ENABLED" "false" "Enable per-user memory profiling? (true/false)"

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

  # Normalize memory variables to strict boolean values
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

  # Ensure variables are explicitly exported so envsubst can access them
  export PROJECT_ID REGION CLUSTER_NAME MODEL_DEFAULT_NAME MODEL_PROVIDER GSA_NAME CHAT_SUB_NAME CHAT_TOPIC_NAME GOOGLE_CHAT_MODE ALLOWED_USERS AGENT_IMAGE NAMESPACE KSA_NAME GOOGLE_CHAT_ENABLED SLACK_ENABLED SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_ALLOWED_USERS SLACK_HOME_CHANNEL SLACK_HOME_CHANNEL_NAME IMAGE_TAG GITHUB_FULL_REPO MEMORY_ENABLED MEMORY_PROVIDER USER_PROFILE_ENABLED

  envsubst < "$CR_TEMPLATE" > "$CR_MANIFEST"
  
  if is_truthy "$ENABLE_GVISOR"; then
    print_info "Enabling gVisor runtimeClassName in '$CR_MANIFEST'..."
    sed -i.bak 's/# runtimeClassName: gvisor/runtimeClassName: gvisor/g' "$CR_MANIFEST" && rm -f "${CR_MANIFEST}.bak"
  fi

  local deploy_name="platform-agent-gateway"

  # Remember the workload generation before applying, so we can tell whether the
  # operator has translated this apply into a Deployment change yet.
  local prev_generation=""
  if [ "${DRY_RUN:-0}" -ne 1 ]; then
    prev_generation=$(kubectl get "deployment/${deploy_name}" -n "${NAMESPACE}" \
        -o jsonpath='{.metadata.generation}' 2>/dev/null || echo "")
  fi

  print_info "Applying 'platform-agent' Custom Resource to the GKE cluster..."
  kubectl apply -f "$CR_MANIFEST" || return 1

  # Applying the CR only tells us the operator accepted it. Without a wait, a Pod
  # that never comes up — crashlooping sidecar, unschedulable, unpullable image —
  # still reports a clean install.
  #
  # Gate on the Deployment rollout rather than the CR's Ready condition. That
  # condition is derived from the live replica count and PlatformAgentStatus
  # carries no observedGeneration, so on a re-apply (verify_custom_resource
  # always returns 1, so this is the normal path) it can still describe the
  # previous generation and pass instantly. kubectl rollout status compares the
  # Deployment's own observedGeneration against metadata.generation, so it will
  # not accept a stale success.
  ensure_k8s_resource_exists "deployment/${deploy_name}" "${NAMESPACE}" 30 || return 1

  # A CR change that alters the workload bumps the Deployment generation. Wait
  # briefly for that, so the rollout we gate on is the new one rather than the
  # one already running. A re-apply that changes nothing never bumps it, and
  # falls through to confirm the running Deployment is still healthy.
  if [ -n "$prev_generation" ] && [ "${DRY_RUN:-0}" -ne 1 ]; then
    local waited=0 current_generation=""
    while [ "$waited" -lt 30 ]; do
      current_generation=$(kubectl get "deployment/${deploy_name}" -n "${NAMESPACE}" \
          -o jsonpath='{.metadata.generation}' 2>/dev/null || echo "")
      [ -n "$current_generation" ] && [ "$current_generation" != "$prev_generation" ] && break
      sleep 3
      waited=$((waited + 3))
    done
  fi

  if [ "${DRY_RUN:-0}" -ne 1 ]; then
    print_info "Waiting for ${deploy_name} rollout to complete..."
    kubectl rollout status "deployment/${deploy_name}" -n "${NAMESPACE}" \
        --timeout="${AGENT_READY_TIMEOUT:-600s}" || {
      print_error "PlatformAgent workload '${deploy_name}' did not roll out successfully."
      kubectl get platformagent platform-agent -n "${NAMESPACE}" \
          -o jsonpath='{.status.phase}{"\n"}{range .status.conditions[*]}{.type}={.status} {.reason}: {.message}{"\n"}{end}' 2>/dev/null || true
      print_info "Container states:"
      kubectl get pods -n "${NAMESPACE}" -l "app=${deploy_name}" \
          -o jsonpath='{range .items[*].status.containerStatuses[*]}  {.name}: ready={.ready} restarts={.restartCount}{"\n"}{end}' 2>/dev/null || true
      return 1
    }
  fi
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_step "2. Apply PlatformAgent Custom Resource" verify_custom_resource execute_custom_resource 0

# ─── Conclusion Checklist ─────────────────────────────────────────────────────
echo -e "\n${C_GREEN}${C_BOLD}✓ PlatformAgent Custom Resource applied successfully to GKE!${C_RESET}"
