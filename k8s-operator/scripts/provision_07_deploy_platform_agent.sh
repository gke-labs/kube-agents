#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 6: Deploy PlatformAgent Custom Resource Manifest
# ==============================================================================
# Idempotent script that connects to GKE, renders the platform-agent.yaml 
# template, and deploys it to the cluster.
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

init_var "HARNESS_FRAMEWORK" "hermes" "Enter Agent Harness Framework [hermes/openclaw]"
HARNESS_FRAMEWORK=$(echo "${HARNESS_FRAMEWORK:-hermes}" | tr '[:upper:]' '[:lower:]')
if [[ ! "$HARNESS_FRAMEWORK" =~ ^(hermes|openclaw)$ ]]; then
  print_error "Invalid Harness Framework '$HARNESS_FRAMEWORK'. Must be 'hermes' or 'openclaw'."
  exit 1
fi
export HARNESS_FRAMEWORK

if [ "${GOOGLE_CHAT_ENABLED:-false}" = "true" ]; then
  init_var "GOOGLE_CHAT_MODE" "default" "Enter Google Chat Output Mode (default or debug)"
  init_var "ALLOWED_USERS" "" "Enter Allowed Google Chat Users Emails (comma separated). Leaving it empty will allow all users."
  if [ "$HARNESS_FRAMEWORK" = "openclaw" ]; then
    init_var "APP_URL" "https://your-ngrok-tunnel.ngrok-free.dev/googlechat" "Enter Google Chat Webhook URL (e.g. ngrok endpoint)"
    init_var "APP_PRINCIPAL" "*" "Enter Google Chat App ID (use '*' to allow any App)"
  else
    export APP_URL=""
    export APP_PRINCIPAL=""
  fi
else
  export GOOGLE_CHAT_MODE="default"
  export ALLOWED_USERS=""
  export APP_URL=""
  export APP_PRINCIPAL=""
fi

DEFAULT_AGENT_IMAGE="ghcr.io/gke-labs/kube-agents/platform-agent"
if [ "$HARNESS_FRAMEWORK" = "openclaw" ]; then
  DEFAULT_AGENT_IMAGE="ghcr.io/gke-labs/kube-agents/platform-agent-openclaw"
fi
init_var "AGENT_IMAGE" "$DEFAULT_AGENT_IMAGE" "Enter Platform Agent Image Path"
if [ "$HARNESS_FRAMEWORK" = "openclaw" ]; then
  if [[ ! "$AGENT_IMAGE" =~ openclaw$ ]]; then
    AGENT_IMAGE="${AGENT_IMAGE/%platform-agent/platform-agent-openclaw}"
  fi
else
  if [[ "$AGENT_IMAGE" == *"-openclaw" ]]; then
    AGENT_IMAGE="${AGENT_IMAGE%-openclaw}"
  fi
fi
export AGENT_IMAGE
save_var "AGENT_IMAGE" "$AGENT_IMAGE"
init_var "AGENT_TAG" "latest" "Enter Platform Agent Image Tag"
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
  print_info "Generating custom resource manifest 'platform-agent.yaml' from template ($HARNESS_FRAMEWORK)..."
  local CR_TEMPLATE="${SCRIPT_DIR}/platform-agent.yaml.template"
  if [ "${HARNESS_FRAMEWORK:-hermes}" = "openclaw" ]; then
    if [ -f "${SCRIPT_DIR}/platform-agent.openclaw.yaml.template" ]; then
      CR_TEMPLATE="${SCRIPT_DIR}/platform-agent.openclaw.yaml.template"
    fi
  fi
  local CR_MANIFEST="${SCRIPT_DIR}/platform-agent.yaml"

  if [ ! -f "$CR_TEMPLATE" ]; then
    print_error "Custom resource template '$CR_TEMPLATE' not found!"
    exit 1
  fi

  # Determine if Google Chat should be enabled
  if [ "${GOOGLE_CHAT_ENABLED:-false}" = "true" ]; then
    export GOOGLE_CHAT_ENABLED="true"
    if [ "${HARNESS_FRAMEWORK:-hermes}" = "openclaw" ]; then
      if [ -z "${APP_URL:-}" ]; then
        print_warning "Google Chat integration is enabled for OpenClaw but APP_URL is missing."
      fi
    else
      if [ -z "${CHAT_TOPIC_NAME:-}" ] || [ -z "${CHAT_SUB_NAME:-}" ]; then
        print_warning "Google Chat integration is enabled but CHAT_TOPIC_NAME or CHAT_SUB_NAME is missing. It may not work properly."
      fi
    fi
  else
    export GOOGLE_CHAT_ENABLED="false"
    if [ "${HARNESS_FRAMEWORK:-hermes}" != "openclaw" ]; then
      export CHAT_TOPIC_NAME=""
      export CHAT_SUB_NAME=""
    fi
    export ALLOWED_USERS=""
    export APP_URL=""
    export APP_PRINCIPAL=""
  fi

  # Determine if Slack should be enabled
  if [ "${SLACK_ENABLED:-false}" = "true" ]; then
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


  # Check/reserve global static IP and automate Cloud Endpoints DNS if applicable
  if [ "${GOOGLE_CHAT_ENABLED:-false}" = "true" ] && { [ -n "${GOOGLE_CHAT_DOMAIN:-}" ] || [ "${HARNESS_FRAMEWORK:-hermes}" = "openclaw" ]; }; then
    local ip_name="${AGENT_NAME:-platform-agent}-ip"
    print_info "Checking/reserving GCP Global Static IP ($ip_name) for Google Chat ingress..."
    gcloud compute addresses create "$ip_name" --global --project="$PROJECT_ID" 2>/dev/null || true
    
    # Wait up to 30 seconds for the IP address to be allocated
    local attempt=1
    while [ $attempt -le 6 ]; do
      STATIC_IP=$(gcloud compute addresses describe "$ip_name" --global --project="$PROJECT_ID" --format="get(address)" 2>/dev/null || echo "")
      if [ -n "$STATIC_IP" ] && [ "$STATIC_IP" != "PENDING" ]; then
        break
      fi
      print_info "Waiting for Static IP allocation (attempt $attempt/6)..."
      sleep 5
      attempt=$((attempt + 1))
    done

    if [ -n "$STATIC_IP" ] && [ "$STATIC_IP" != "PENDING" ]; then
      if [ "$GOOGLE_CHAT_DOMAIN" = "auto" ] || [ -z "$GOOGLE_CHAT_DOMAIN" ] || [[ "$GOOGLE_CHAT_DOMAIN" == *.endpoints*.cloud.goog ]]; then
        if [ "$GOOGLE_CHAT_DOMAIN" = "auto" ] || [ -z "$GOOGLE_CHAT_DOMAIN" ]; then
          GOOGLE_CHAT_DOMAIN="${AGENT_NAME:-platform-agent}.endpoints.${PROJECT_ID}.cloud.goog"
          print_info "No domain specified (or 'auto' selected). Using Cloud Endpoints (${GOOGLE_CHAT_DOMAIN}) as default..."
        fi
        APP_URL="https://${GOOGLE_CHAT_DOMAIN}/googlechat"
        export GOOGLE_CHAT_DOMAIN APP_URL
        save_var "GOOGLE_CHAT_DOMAIN" "$GOOGLE_CHAT_DOMAIN"
        save_var "APP_URL" "$APP_URL"
        print_info "Automating Cloud Endpoints DNS registration for ${GOOGLE_CHAT_DOMAIN} -> ${STATIC_IP}..."
        cat <<EOF > "/tmp/openapi-${AGENT_NAME:-platform-agent}.yaml"
swagger: "2.0"
info:
  title: "${AGENT_NAME:-platform-agent} Gateway Ingress"
  description: "Automated DNS mapping for OpenClaw Gateway on GKE"
  version: "1.0.0"
host: "${GOOGLE_CHAT_DOMAIN}"
x-google-endpoints:
  - name: "${GOOGLE_CHAT_DOMAIN}"
    target: "${STATIC_IP}"
paths:
  /googlechat:
    post:
      description: "Google Chat webhook endpoint"
      operationId: "postGoogleChat"
      responses:
        200:
          description: "Success"
EOF
        if gcloud endpoints services deploy "/tmp/openapi-${AGENT_NAME:-platform-agent}.yaml" --project="$PROJECT_ID" --impersonate-service-account="${GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" --quiet 2>/dev/null || gcloud endpoints services deploy "/tmp/openapi-${AGENT_NAME:-platform-agent}.yaml" --project="$PROJECT_ID" --quiet 2>/dev/null; then
          print_success "Cloud Endpoints DNS registered! ($GOOGLE_CHAT_DOMAIN -> $STATIC_IP)"
          echo -e "${C_CYAN}╔═════════════════════════════════════════════════════════════════════════════╗${C_RESET}"
          echo -e "${C_CYAN}║  >>> CLOUD ENDPOINTS DNS & SSL CERTIFICATE PROVISIONING <<<                 ║${C_RESET}"
          echo -e "${C_CYAN}║  Domain:    ${C_GREEN}${GOOGLE_CHAT_DOMAIN}${C_CYAN}                                     ║${C_RESET}"
          echo -e "${C_CYAN}║  Static IP: ${C_GREEN}${STATIC_IP}${C_CYAN}                                           ║${C_RESET}"
          echo -e "${C_CYAN}║  Note:      GKE is provisioning a Google-managed SSL certificate.           ║${C_RESET}"
          echo -e "${C_CYAN}║             It takes ~15-30 mins to transition from PROVISIONING to ACTIVE. ║${C_RESET}"
          echo -e "${C_CYAN}║  Verify:    kubectl get managedcertificate ${AGENT_NAME:-platform-agent}-cert -n ${NAMESPACE:-agents}       ║${C_RESET}"
          echo -e "${C_CYAN}╚═════════════════════════════════════════════════════════════════════════════╝${C_RESET}"
        else
          print_warning "Automatic Cloud Endpoints DNS deployment could not be completed."
          echo -e "${C_YELLOW}╔═════════════════════════════════════════════════════════════════════════════╗${C_RESET}"
          echo -e "${C_YELLOW}║  >>> DNS ACTION REQUIRED FOR GOOGLE CHAT HTTPS <<<                          ║${C_RESET}"
          echo -e "${C_YELLOW}║  Domain:    ${C_GREEN}${GOOGLE_CHAT_DOMAIN}${C_YELLOW}                                     ║${C_RESET}"
          echo -e "${C_YELLOW}║  Static IP: ${C_GREEN}${STATIC_IP}${C_YELLOW}                                           ║${C_RESET}"
          echo -e "${C_YELLOW}║  Action:    Please verify domain ownership or create a DNS 'A Record'       ║${C_RESET}"
          echo -e "${C_YELLOW}║             pointing your domain to the Static IP above.                    ║${C_RESET}"
          echo -e "${C_YELLOW}╚═════════════════════════════════════════════════════════════════════════════╝${C_RESET}"
        fi
        rm -f "/tmp/openapi-${AGENT_NAME:-platform-agent}.yaml"
      elif [[ "$GOOGLE_CHAT_DOMAIN" == *.nip.io ]]; then
        APP_URL="https://${GOOGLE_CHAT_DOMAIN}/googlechat"
        export GOOGLE_CHAT_DOMAIN APP_URL
        save_var "GOOGLE_CHAT_DOMAIN" "$GOOGLE_CHAT_DOMAIN"
        save_var "APP_URL" "$APP_URL"
        print_success "Assigned zero-interaction wildcard DNS: ${GOOGLE_CHAT_DOMAIN} -> ${STATIC_IP}"
      else
        APP_URL="https://${GOOGLE_CHAT_DOMAIN}/googlechat"
        export APP_URL
        save_var "GOOGLE_CHAT_DOMAIN" "$GOOGLE_CHAT_DOMAIN"
        save_var "APP_URL" "$APP_URL"
        echo -e "${C_YELLOW}╔════════════════════════════════════════════════════════════════════════╗${C_RESET}"
        echo -e "${C_YELLOW}║  >>> DNS MAPPING REQUIRED FOR GOOGLE CHAT HTTPS <<<                    ║${C_RESET}"
        echo -e "${C_YELLOW}║  Domain:    ${C_GREEN}${GOOGLE_CHAT_DOMAIN}${C_YELLOW}                                     ║${C_RESET}"
        echo -e "${C_YELLOW}║  Static IP: ${C_GREEN}${STATIC_IP}${C_YELLOW}                                           ║${C_RESET}"
        echo -e "${C_YELLOW}║  Action:    Create a DNS 'A Record' pointing your domain to this IP.   ║${C_RESET}"
        echo -e "${C_YELLOW}╚════════════════════════════════════════════════════════════════════════╝${C_RESET}"
      fi
    fi
  fi

  # Ensure variables are explicitly exported so envsubst can access them
  export PROJECT_ID PROJECT_NUMBER REGION CLUSTER_NAME MODEL_DEFAULT_NAME MODEL_PROVIDER GSA_NAME GOOGLE_CHAT_MODE ALLOWED_USERS AGENT_IMAGE NAMESPACE KSA_NAME APP_URL APP_PRINCIPAL GOOGLE_CHAT_DOMAIN

  # Handle optional GitHub integration variables
  if [ -n "${GITHUB_ORG:-}" ] && [ -n "${GITHUB_REPO:-}" ]; then
    export GITHUB_FULL_REPO="${GITHUB_ORG}/${GITHUB_REPO}"
  else
    export GITHUB_FULL_REPO=""
  fi

  # Normalize memory variables to strict boolean values
  if [[ "${MEMORY_ENABLED:-false}" =~ ^(true|yes|1|y|Y)$ ]]; then
    export MEMORY_ENABLED="true"
  else
    export MEMORY_ENABLED="false"
  fi

  if [[ "${USER_PROFILE_ENABLED:-false}" =~ ^(true|yes|1|y|Y)$ ]]; then
    export USER_PROFILE_ENABLED="true"
  else
    export USER_PROFILE_ENABLED="false"
  fi

  # Ensure variables are explicitly exported so envsubst can access them
  export PROJECT_ID PROJECT_NUMBER REGION CLUSTER_NAME MODEL_DEFAULT_NAME MODEL_PROVIDER GSA_NAME GOOGLE_CHAT_MODE ALLOWED_USERS AGENT_IMAGE NAMESPACE KSA_NAME APP_URL APP_PRINCIPAL GOOGLE_CHAT_DOMAIN GOOGLE_CHAT_ENABLED SLACK_ENABLED SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_ALLOWED_USERS SLACK_HOME_CHANNEL SLACK_HOME_CHANNEL_NAME AGENT_TAG GITHUB_FULL_REPO CHAT_SUB_NAME CHAT_TOPIC_NAME MEMORY_ENABLED MEMORY_PROVIDER USER_PROFILE_ENABLED HARNESS_FRAMEWORK

  envsubst < "$CR_TEMPLATE" > "$CR_MANIFEST"
  
  if [[ "$ENABLE_GVISOR" =~ ^(true|yes|1)$ ]]; then
    print_info "Enabling gVisor runtimeClassName in '$CR_MANIFEST'..."
    sed -i.bak 's/# runtimeClassName: gvisor/runtimeClassName: gvisor/g' "$CR_MANIFEST" && rm -f "${CR_MANIFEST}.bak"
  fi

  print_info "Applying 'platform-agent' Custom Resource to the GKE cluster..."
  kubectl apply -f "$CR_MANIFEST"
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_step "2. Apply PlatformAgent Custom Resource" verify_custom_resource execute_custom_resource 0

# ─── Conclusion Checklist ─────────────────────────────────────────────────────
echo -e "\n${C_GREEN}${C_BOLD}✓ PlatformAgent Custom Resource applied successfully to GKE!${C_RESET}"
