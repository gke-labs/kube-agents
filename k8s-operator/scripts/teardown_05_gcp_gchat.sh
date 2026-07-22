#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 5: Teardown Google Chat & Pub/Sub Setup
# ==============================================================================
# Idempotent script to clean up GChat Pub/Sub Topic/Subscription and the Bot GSA.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Configuration State Restoration ──────────────────────────────────────────
ensure_teardown_state

# ─── Confirmation Prompt ──────────────────────────────────────────────────────
confirm_action "This will permanently delete GChat Pub/Sub topic and subscription." \
  "GCP Project:$PROJECT_ID" \
  "Pub/Sub Topic:$CHAT_TOPIC_NAME" \
  "Pub/Sub Sub:$CHAT_SUB_NAME"

gcloud config set project "$PROJECT_ID" --quiet

# ─── Step 1: Delete Pub/Sub Subscription ──────────────────────────────────────
if gcloud pubsub subscriptions describe "${CHAT_SUB_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo -e "  ${C_CYAN}ℹ Deleting Pub/Sub Subscription '${CHAT_SUB_NAME}'...${C_RESET}"
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    echo -e "  ${C_GREEN}[DRY-RUN] Would delete Pub/Sub Subscription '${CHAT_SUB_NAME}'.${C_RESET}"
  else
    gcloud pubsub subscriptions delete "${CHAT_SUB_NAME}" --project="${PROJECT_ID}" --quiet || true
    echo -e "  ${C_GREEN}✓ Pub/Sub Subscription successfully removed.${C_RESET}"
  fi
else
  echo -e "  ${C_GREEN}✓ Pub/Sub Subscription '${CHAT_SUB_NAME}' does not exist.${C_RESET}"
fi

# ─── Step 2: Delete Pub/Sub Topic ─────────────────────────────────────────────
if gcloud pubsub topics describe "${CHAT_TOPIC_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo -e "  ${C_CYAN}ℹ Deleting Pub/Sub Topic '${CHAT_TOPIC_NAME}'...${C_RESET}"
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    echo -e "  ${C_GREEN}[DRY-RUN] Would delete Pub/Sub Topic '${CHAT_TOPIC_NAME}'.${C_RESET}"
  else
    gcloud pubsub topics delete "${CHAT_TOPIC_NAME}" --project="${PROJECT_ID}" --quiet || true
    echo -e "  ${C_GREEN}✓ Pub/Sub Topic successfully removed.${C_RESET}"
  fi
else
  echo -e "  ${C_GREEN}✓ Pub/Sub Topic '${CHAT_TOPIC_NAME}' does not exist.${C_RESET}"
fi
