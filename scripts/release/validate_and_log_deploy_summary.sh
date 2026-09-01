#!/usr/bin/env bash
# Formats, validates, and logs deployment configuration summary for CI runs.
set -euo pipefail

REQUIRED_VARS=(
  "GCP_PROJECT_ID"
  "GCP_REGION"
  "GKE_CLUSTER_NAME"
  "GCP_SERVICE_ACCOUNT"
)

MISSING_VARS=()
for var in "${REQUIRED_VARS[@]}"; do
  if [ -z "${!var:-}" ]; then
    MISSING_VARS+=("$var")
  fi
done

# Validate API key only if using Gemini model provider
PROVIDER="${MODEL_PROVIDER:-gemini}"
if [ "${PROVIDER}" = "gemini" ] && [ -z "${GEMINI_API_KEY:-}" ]; then
  MISSING_VARS+=("GEMINI_API_KEY (required when MODEL_PROVIDER is 'gemini')")
fi

if [ ${#MISSING_VARS[@]} -ne 0 ]; then
  echo "❌ ERROR: The following required environment variables/secrets are not set:" >&2
  for var in "${MISSING_VARS[@]}"; do
    echo "  - $var" >&2
  done
  exit 1
fi

API_KEY_STATUS="<NOT SET>"
if [ -n "${GEMINI_API_KEY:-}" ]; then
  API_KEY_STATUS="<SET>"
fi

echo "======================================================================"
echo "🚀 RELEASE CANDIDATE DEPLOYMENT CONFIGURATION"
echo "======================================================================"
echo "Target Project ID:          ${GCP_PROJECT_ID}"
echo "Target GCP Region:          ${GCP_REGION}"
echo "Target Cluster Name:        ${GKE_CLUSTER_NAME}"
echo "Service Account:            ${GCP_SERVICE_ACCOUNT}"
echo "----------------------------------------------------------------------"
echo "Release Tag:                ${RC_TAG:-<NOT SET>}"
echo "Candidate Commit SHA:       ${COMMIT_SHA:-<NOT SET>}"
echo "Candidate Image Tag:        ${IMAGE_TAG:-<NOT SET>}"
echo "Registry Prefix:            ${REGISTRY_PREFIX:-<DEFAULT (ghcr.io/gke-labs/kube-agents)>}"
echo "----------------------------------------------------------------------"
echo "Google Chat Enabled:        ${GOOGLE_CHAT_ENABLED:-<NOT SET>}"
echo "Google Chat Mode:           ${GOOGLE_CHAT_MODE:-<NOT SET>}"
echo "Google Chat Topic Name:     ${CHAT_TOPIC_NAME:-<NOT SET>}"
echo "----------------------------------------------------------------------"
echo "Model Provider:             ${MODEL_PROVIDER:-gemini}"
echo "Model Default Name:         ${MODEL_DEFAULT_NAME:-<NOT SET>}"
echo "Gemini API Key:             ${API_KEY_STATUS}"
# Unset does not mean off here: provision_rc_environment.sh omits --gvisor when
# this is empty, and install.sh then applies its own default, which is the
# sandbox. Say so rather than logging a bare <NOT SET> beside a run that
# provisions a gvisor node pool.
echo "Enable gVisor Isolation:    ${ENABLE_GVISOR:-<NOT SET — install.sh default applies>}"
echo "Enable GKE Backup Plan:     ${ENABLE_GKE_BACKUP_PLAN:-<NOT SET>}"
echo "Agent K8s Permission Set:   ${PLATFORM_AGENT_PERMISSION_SET:-<NOT SET>}"
echo "Memory Provider:            ${MEMORY_PROVIDER:-<NOT SET>}"
echo "User Profile Enabled:       ${USER_PROFILE_ENABLED:-<NOT SET>}"
echo "======================================================================"
