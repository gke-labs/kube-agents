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
confirm_action "This will permanently delete GChat Pub/Sub topic, subscription, and the Bot Service Account." \
  "GCP Project:$PROJECT_ID" \
  "Pub/Sub Topic:$CHAT_TOPIC_NAME" \
  "Pub/Sub Sub:$CHAT_SUB_NAME" \
  "Agent GSA:$GSA_NAME"

gcloud config set project "$PROJECT_ID" --quiet

# ─── Step 1: Delete Pub/Sub Subscription ──────────────────────────────────────
SUB_EXISTS=$(gcloud pubsub subscriptions list --filter="name:projects/${PROJECT_ID}/subscriptions/${CHAT_SUB_NAME}" --format="value(name)" --project="${PROJECT_ID}" 2>/dev/null || echo "")
if [ -n "$SUB_EXISTS" ]; then
  echo -e "  ${C_CYAN}ℹ Deleting Pub/Sub Subscription '${CHAT_SUB_NAME}'...${C_RESET}"
  gcloud pubsub subscriptions delete "${CHAT_SUB_NAME}" --project="${PROJECT_ID}" --quiet || true
  echo -e "  ${C_GREEN}✓ Pub/Sub Subscription successfully removed.${C_RESET}"
else
  echo -e "  ${C_GREEN}✓ Pub/Sub Subscription '${CHAT_SUB_NAME}' does not exist.${C_RESET}"
fi

# ─── Step 2: Delete Pub/Sub Topic ─────────────────────────────────────────────
TOPIC_EXISTS=$(gcloud pubsub topics list --filter="name:projects/${PROJECT_ID}/topics/${CHAT_TOPIC_NAME}" --format="value(name)" --project="${PROJECT_ID}" 2>/dev/null || echo "")
if [ -n "$TOPIC_EXISTS" ]; then
  echo -e "  ${C_CYAN}ℹ Deleting Pub/Sub Topic '${CHAT_TOPIC_NAME}'...${C_RESET}"
  gcloud pubsub topics delete "${CHAT_TOPIC_NAME}" --project="${PROJECT_ID}" --quiet || true
  echo -e "  ${C_GREEN}✓ Pub/Sub Topic successfully removed.${C_RESET}"
else
  echo -e "  ${C_GREEN}✓ Pub/Sub Topic '${CHAT_TOPIC_NAME}' does not exist.${C_RESET}"
fi

# ─── Step 3: Delete Agent GSA ─────────────────────────────────────────────────
gsa_email="${GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
GSA_EXISTS=$(gcloud iam service-accounts list --filter="email=${gsa_email}" --format="value(email)" --project="${PROJECT_ID}" 2>/dev/null || echo "")
if [ -n "$GSA_EXISTS" ]; then
  echo -e "  ${C_CYAN}ℹ Deleting Bot GSA '${gsa_email}'...${C_RESET}"
  gcloud iam service-accounts delete "${gsa_email}" --project="${PROJECT_ID}" --quiet || true
  echo -e "  ${C_GREEN}✓ Bot GSA successfully removed.${C_RESET}"
else
  echo -e "  ${C_GREEN}✓ Bot GSA '${gsa_email}' does not exist.${C_RESET}"
fi
