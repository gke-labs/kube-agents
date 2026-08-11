#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 7: GKE Kubernetes Secrets Setup
# ==============================================================================
# Idempotent setup script to validate and configure Kubernetes secrets directly.
# Requires CMEK database encryption unless ALLOW_UNENCRYPTED_SECRETS=true is set.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$SCRIPT_DIR" == */scripts ]]; then
  OPERATOR_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  OPERATOR_DIR="${SCRIPT_DIR}"
fi
VARS_FILE="${SCRIPT_DIR}/vars.sh"

source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud" "kubectl" "openssl"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "$DEFAULT_REGION" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "$DEFAULT_CLUSTER_NAME" "Enter GKE Cluster Name"

# Prompt for Model Provider and Default Name early to determine which API key is required
init_var_model_provider

# Automatically sanitize any preexisting literal "placeholder" values from memory and vars.sh
for var in GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY API_SERVER_KEY SLACK_BOT_TOKEN SLACK_APP_TOKEN; do
  if [ "${!var:-}" = "placeholder" ]; then
    save_secret_var "$var" ""
  fi
done

# Securely prompt for Gemini API Key if Gemini is the active provider
if [ "$MODEL_PROVIDER" = "gemini" ]; then
  if [ -z "${GEMINI_API_KEY:-}" ]; then
    if is_non_interactive; then
      save_secret_var "GEMINI_API_KEY" "${GEMINI_API_KEY:-}"
    else
      echo -ne "  ${C_CYAN}Enter your GEMINI_API_KEY (press ENTER to leave empty): ${C_RESET}"
      read -s -r INPUT_KEY
      echo ""
      save_secret_var "GEMINI_API_KEY" "${INPUT_KEY:-}"
    fi
  else
    save_secret_var "GEMINI_API_KEY" "${GEMINI_API_KEY}"
  fi
else
  save_secret_var "GEMINI_API_KEY" "${GEMINI_API_KEY:-}"
fi

# Securely prompt for OpenAI API Key if OpenAI is the active provider
if [ "$MODEL_PROVIDER" = "openai" ]; then
  if [ -z "${OPENAI_API_KEY:-}" ]; then
    if is_non_interactive; then
      save_secret_var "OPENAI_API_KEY" "${OPENAI_API_KEY:-}"
    else
      echo -ne "  ${C_CYAN}Enter your OPENAI_API_KEY (press ENTER to leave empty): ${C_RESET}"
      read -s -r INPUT_KEY
      echo ""
      save_secret_var "OPENAI_API_KEY" "${INPUT_KEY:-}"
    fi
  else
    save_secret_var "OPENAI_API_KEY" "${OPENAI_API_KEY}"
  fi
else
  save_secret_var "OPENAI_API_KEY" "${OPENAI_API_KEY:-}"
fi

# Securely prompt for Anthropic API Key if Anthropic is the active provider
if [ "$MODEL_PROVIDER" = "anthropic" ]; then
  if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    if is_non_interactive; then
      save_secret_var "ANTHROPIC_API_KEY" "${ANTHROPIC_API_KEY:-}"
    else
      echo -ne "  ${C_CYAN}Enter your ANTHROPIC_API_KEY (press ENTER to leave empty): ${C_RESET}"
      read -s -r INPUT_KEY
      echo ""
      save_secret_var "ANTHROPIC_API_KEY" "${INPUT_KEY:-}"
    fi
  else
    save_secret_var "ANTHROPIC_API_KEY" "${ANTHROPIC_API_KEY}"
  fi
else
  save_secret_var "ANTHROPIC_API_KEY" "${ANTHROPIC_API_KEY:-}"
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

# Step 2: Sync API Keys to GKE Namespace Secrets
verify_k8s_secrets() {
  # Always return false to ensure secret updates are synchronized to GKE
  return 1
}

# Stamps the Kubernetes recommended labels onto a Secret manifest read from
# stdin and applies it, so provisioned Secrets show up alongside everything else
# under -l app.kubernetes.io/part-of=kube-agents. See
# docs/site/src/content/docs/reference/resource-labels.md
#
# 'kubectl create secret' has no --label flag, hence the second pass. The
# manifest stays on the pipe and is never written to disk or echoed, so the
# credentials it carries are not exposed.
apply_labelled_secret() {
  kubectl label --local -f - -o yaml \
      "app.kubernetes.io/name=platform-agent" \
      "app.kubernetes.io/instance=${NAMESPACE}-platform-agent" \
      "app.kubernetes.io/part-of=kube-agents" \
      "app.kubernetes.io/managed-by=provisioner" \
    | kubectl apply -f -
}
execute_k8s_secrets() {
  local enc_state
  enc_state=$(gcloud container clusters describe "$CLUSTER_NAME" \
    --location="$REGION" --project="$PROJECT_ID" \
    --format="value(databaseEncryption.state)" 2>/dev/null || echo "")

  if ! is_valid_cmek_encryption_state "$enc_state"; then
    if ! is_truthy "${ALLOW_UNENCRYPTED_SECRETS:-false}"; then
      print_error "Cluster database encryption state is '${enc_state:-UNKNOWN}'. Writing secrets requires CMEK database encryption or ALLOW_UNENCRYPTED_SECRETS=true override."
      exit 1
    else
      print_warning "Cluster database encryption is not ENCRYPTED ('${enc_state:-UNKNOWN}'), but ALLOW_UNENCRYPTED_SECRETS=true is set. Proceeding..."
    fi
  fi

  # Recover or generate API_SERVER_KEY on the connected target cluster
  if [ -z "${API_SERVER_KEY:-}" ]; then
    local existing_key=""
    if [ "${DRY_RUN:-0}" -ne 1 ]; then
      existing_key=$(kubectl get secret platform-agent-secrets -n "$NAMESPACE" -o jsonpath='{.data.API_SERVER_KEY}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
    fi
    if [ -n "$existing_key" ]; then
      print_info "Preserving existing API_SERVER_KEY from Kubernetes Secret..."
      save_secret_var "API_SERVER_KEY" "$existing_key"
    else
      print_info "Generating a secure random API_SERVER_KEY..."
      save_secret_var "API_SERVER_KEY" "$(openssl rand -hex 16)"
    fi
  else
    save_secret_var "API_SERVER_KEY" "${API_SERVER_KEY}"
  fi

  # Recover or prompt for Slack tokens on the connected target cluster
  if is_truthy "${SLACK_ENABLED:-false}"; then
    if [ -z "${SLACK_BOT_TOKEN:-}" ]; then
      local existing_bot_token=""
      if [ "${DRY_RUN:-0}" -ne 1 ]; then
        existing_bot_token=$(kubectl get secret platform-agent-secrets -n "$NAMESPACE" -o jsonpath='{.data.SLACK_BOT_TOKEN}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
      fi
      if [ -n "$existing_bot_token" ]; then
        print_info "Preserving existing SLACK_BOT_TOKEN from Kubernetes Secret..."
        save_secret_var "SLACK_BOT_TOKEN" "$existing_bot_token"
      elif is_non_interactive; then
        save_secret_var "SLACK_BOT_TOKEN" "${SLACK_BOT_TOKEN:-}"
      else
        echo -ne "  ${C_CYAN}Enter your SLACK_BOT_TOKEN (xoxb-...): ${C_RESET}"
        read -s -r input_bot_token
        echo ""
        save_secret_var "SLACK_BOT_TOKEN" "${input_bot_token:-}"
      fi
    else
      save_secret_var "SLACK_BOT_TOKEN" "${SLACK_BOT_TOKEN}"
    fi

    if [ -z "${SLACK_APP_TOKEN:-}" ]; then
      local existing_app_token=""
      if [ "${DRY_RUN:-0}" -ne 1 ]; then
        existing_app_token=$(kubectl get secret platform-agent-secrets -n "$NAMESPACE" -o jsonpath='{.data.SLACK_APP_TOKEN}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
      fi
      if [ -n "$existing_app_token" ]; then
        print_info "Preserving existing SLACK_APP_TOKEN from Kubernetes Secret..."
        save_secret_var "SLACK_APP_TOKEN" "$existing_app_token"
      elif is_non_interactive; then
        save_secret_var "SLACK_APP_TOKEN" "${SLACK_APP_TOKEN:-}"
      else
        echo -ne "  ${C_CYAN}Enter your SLACK_APP_TOKEN (xapp-...): ${C_RESET}"
        read -s -r input_app_token
        echo ""
        save_secret_var "SLACK_APP_TOKEN" "${input_app_token:-}"
      fi
    else
      save_secret_var "SLACK_APP_TOKEN" "${SLACK_APP_TOKEN}"
    fi
  else
    save_secret_var "SLACK_BOT_TOKEN" "${SLACK_BOT_TOKEN:-}"
    save_secret_var "SLACK_APP_TOKEN" "${SLACK_APP_TOKEN:-}"
  fi

  for key in GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY API_SERVER_KEY SLACK_BOT_TOKEN SLACK_APP_TOKEN; do
    if [ "${!key:-}" = "placeholder" ]; then
      print_error "Refusing to write literal 'placeholder' for $key to Kubernetes secret."
      exit 1
    fi
  done

  if [ "$MODEL_PROVIDER" = "gemini" ] && [ -z "${GEMINI_API_KEY:-}" ]; then
    print_warning "GEMINI_API_KEY is empty. The platform agent will run but cannot authenticate with Gemini until updated."
  elif [ "$MODEL_PROVIDER" = "openai" ] && [ -z "${OPENAI_API_KEY:-}" ]; then
    print_warning "OPENAI_API_KEY is empty. The platform agent will run but cannot authenticate with OpenAI until updated."
  elif [ "$MODEL_PROVIDER" = "anthropic" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    print_warning "ANTHROPIC_API_KEY is empty. The platform agent will run but cannot authenticate with Anthropic until updated."
  fi

  # pipefail so a failure in the generate or label stage is not masked by a
  # successful-looking apply at the end of the pipe.
  print_info "Writing Kubernetes Secret 'platform-agent-secrets' into '$NAMESPACE'..."
  (
    set -o pipefail
    kubectl create secret generic platform-agent-secrets \
        --namespace="$NAMESPACE" \
        --from-literal=GEMINI_API_KEY="$GEMINI_API_KEY" \
        --from-literal=API_SERVER_KEY="$API_SERVER_KEY" \
        --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY" \
        --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
        --from-literal=SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN:-}" \
        --from-literal=SLACK_APP_TOKEN="${SLACK_APP_TOKEN:-}" \
        --dry-run=client -o yaml | apply_labelled_secret
  )

  if [ -n "${GITHUB_APP_ID}" ]; then
    print_info "Writing Kubernetes Secret 'github-app-credentials' into '$NAMESPACE'..."
    (
      set -o pipefail
      kubectl create secret generic github-app-credentials \
          --namespace="$NAMESPACE" \
          --from-literal=app-id="${GITHUB_APP_ID}" \
          --dry-run=client -o yaml | apply_labelled_secret
    )
  fi
}


# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_step "2. Write GKE Namespace Secrets" verify_k8s_secrets execute_k8s_secrets 0

echo -e "\n${C_MAGENTA}${C_BOLD}>>>  Secrets Configured & Synchronized Successfully!  <<<${C_RESET}"
