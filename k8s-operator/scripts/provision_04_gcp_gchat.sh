#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 5: Google Chat & Pub/Sub Setup
# ==============================================================================
# Configures the Google Chat backend: Pub/Sub routing, the Agent's Service Account,
# and grants the Service Account permission to read incoming chat messages.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State for GChat Setup"
load_state

if [ "${DRY_RUN:-0}" -eq 1 ]; then
  export GOOGLE_CHAT_ENABLED="${GOOGLE_CHAT_ENABLED:-false}"
else
  current_gc_val="${GOOGLE_CHAT_ENABLED:-false}"
  default_gc_prompt="y/N"
  if [ "$current_gc_val" = "true" ]; then
    default_gc_prompt="Y/n"
  fi
  echo -ne "  ${C_CYAN}Do you want to enable Google Chat integration? (${default_gc_prompt}): ${C_RESET}"
  read -r REPLY_GC
  if [ -z "$REPLY_GC" ]; then
    export GOOGLE_CHAT_ENABLED="$current_gc_val"
  elif [[ "$REPLY_GC" =~ ^[Yy]$ ]]; then
    export GOOGLE_CHAT_ENABLED="true"
  else
    export GOOGLE_CHAT_ENABLED="false"
  fi
fi
save_var "GOOGLE_CHAT_ENABLED" "${GOOGLE_CHAT_ENABLED}"

if [ "${GOOGLE_CHAT_ENABLED}" != "true" ]; then
  print_info "Google Chat integration is disabled. Skipping Google Chat Pub/Sub setup."
  save_var "CHAT_TOPIC_NAME" ""
  save_var "CHAT_SUB_NAME" ""
  save_var "ALLOWED_USERS" ""
  exit 0
fi

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"

if [ -z "${PROJECT_NUMBER:-}" ]; then
  print_info "Resolving numeric Project Number for $PROJECT_ID..."
  PROJECT_NUMBER_VAL=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)" 2>/dev/null || echo "")
  if [ -z "$PROJECT_NUMBER_VAL" ]; then
    if [ "${DRY_RUN:-0}" -eq 1 ]; then
      PROJECT_NUMBER_VAL="123456789012"
    else
      echo -ne "  ${C_YELLOW}Failed to resolve project number automatically. Please enter it manually: ${C_RESET}"
      read -r PROJECT_NUMBER_VAL
    fi
  fi
  if [ -z "$PROJECT_NUMBER_VAL" ]; then
    print_error "Project number is required to configure Google Chat integration. Exiting."
    exit 1
  fi
  save_var "PROJECT_NUMBER" "${PROJECT_NUMBER_VAL}"
  print_success "Project Number resolved: $PROJECT_NUMBER"
fi

DEFAULT_USERS=""
init_var "ALLOWED_USERS" "$DEFAULT_USERS" "Enter Allowed Google Chat Users Emails (comma separated). Leaving it empty will allow all users."
init_var "CHAT_TOPIC_NAME" "platform-agent-chat-events" "Enter Pub/Sub Topic Name"
init_var "CHAT_SUB_NAME" "platform-agent-chat-events-sub" "Enter Pub/Sub Subscription Name"
init_var "GOOGLE_CHAT_MODE" "default" "Enter Google Chat Output Mode (default or debug)"


# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Enable Chat & PubSub APIs
verify_apis() {
  local out=$(gcloud services list --enabled --project="$PROJECT_ID" --format="value(config.name)" 2>/dev/null || echo "")
  echo "$out" | grep -q 'pubsub.googleapis.com' && \
  echo "$out" | grep -q 'chat.googleapis.com' && \
  echo "$out" | grep -q 'gsuiteaddons.googleapis.com'
}
execute_apis() {
  gcloud services enable \
      pubsub.googleapis.com \
      chat.googleapis.com \
      gsuiteaddons.googleapis.com \
      --project="$PROJECT_ID"
}

# Step 2: Provision Google Workspace Add-ons Service Identity
verify_gsuite_identity() {
  gcloud iam service-accounts describe "service-${PROJECT_NUMBER}@gcp-sa-gsuiteaddons.iam.gserviceaccount.com" --project="$PROJECT_ID" >/dev/null 2>&1
}
execute_gsuite_identity() {
  print_info "Creating service identity for Google Workspace Add-ons..."
  gcloud beta services identity create \
      --service=gsuiteaddons.googleapis.com \
      --project="$PROJECT_ID" \
      --quiet >/dev/null
}

# Step 3: Pub/Sub Setup (Inbound routing from GChat)
verify_pubsub_setup() {
  gcloud pubsub topics describe "${CHAT_TOPIC_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1 && \
  gcloud pubsub subscriptions describe "${CHAT_SUB_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1
}
execute_pubsub_setup() {
  if ! gcloud pubsub topics describe "${CHAT_TOPIC_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    print_info "Creating Pub/Sub Topic ${CHAT_TOPIC_NAME}..."
    gcloud pubsub topics create "${CHAT_TOPIC_NAME}" --project="${PROJECT_ID}" || return 1
  fi

  if ! gcloud pubsub subscriptions describe "${CHAT_SUB_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    print_info "Creating Pub/Sub Subscription ${CHAT_SUB_NAME}..."
    gcloud pubsub subscriptions create "${CHAT_SUB_NAME}" \
        --topic="${CHAT_TOPIC_NAME}" \
        --ack-deadline=60 \
        --project="${PROJECT_ID}" || return 1
  fi

  print_info "Granting Google Chat systems Publisher roles to the Topic..."
  gcloud pubsub topics add-iam-policy-binding "${CHAT_TOPIC_NAME}" \
      --member="serviceAccount:chat-api-push@system.gserviceaccount.com" \
      --role="roles/pubsub.publisher" \
      --project="${PROJECT_ID}" \
      --condition=None \
      --quiet >/dev/null || return 1

  local gsuite_sa="service-${PROJECT_NUMBER}@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"
  gcloud pubsub topics add-iam-policy-binding "${CHAT_TOPIC_NAME}" \
      --member="serviceAccount:${gsuite_sa}" \
      --role="roles/pubsub.publisher" \
      --project="${PROJECT_ID}" \
      --condition=None \
      --quiet >/dev/null || return 1
}

# Step 4: Agent GSA Creation & PubSub Message Read Access
verify_agent_gcp() {
  local gsa_email="${PLATFORM_AGENT_GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
  gcloud iam service-accounts describe "${gsa_email}" --project="${PROJECT_ID}" >/dev/null 2>&1 && \
  gcloud pubsub subscriptions get-iam-policy "${CHAT_SUB_NAME}" --project="${PROJECT_ID}" --format="json" 2>/dev/null | grep -F -q "${gsa_email}"
}
execute_agent_gcp() {
  local gsa_email="${PLATFORM_AGENT_GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

  print_info "Applying Pub/Sub Subscriber Role for Agent GSA..."
  
  # 2. Allow bot to read from Pub/Sub Queue
  gcloud pubsub subscriptions add-iam-policy-binding "${CHAT_SUB_NAME}" \
      --member="serviceAccount:${gsa_email}" \
      --role="roles/pubsub.subscriber" \
      --project="${PROJECT_ID}" \
      --quiet >/dev/null || return 1

  gcloud pubsub subscriptions add-iam-policy-binding "${CHAT_SUB_NAME}" \
      --member="serviceAccount:${gsa_email}" \
      --role="roles/pubsub.viewer" \
      --project="${PROJECT_ID}" \
      --quiet >/dev/null || return 1
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Enable GCP APIs for Chat & PubSub" verify_apis execute_apis 15
run_step "2. Provision Google Workspace Add-ons Service Identity" verify_gsuite_identity execute_gsuite_identity 5
run_step "3. Provision Pub/Sub Routing (Inbound)" verify_pubsub_setup execute_pubsub_setup 5
run_step "4. Setup Agent Identity & Message Read Permissions" verify_agent_gcp execute_agent_gcp 5

# ─── Conclusion Checklist ─────────────────────────────────────────────────────
echo -e "\n${C_MAGENTA}${C_BOLD}>>>  GCP Backend for Google Chat Configured!  <<<${C_RESET}"
"${SCRIPT_DIR}/print_instructions_gchat.sh" "$@"
