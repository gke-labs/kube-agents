#!/usr/bin/env bash
# Executes environment teardown and provisioning for release candidate deployment.
set -euo pipefail

export CLOUDSDK_CORE_DISABLE_PROMPTS="${CLOUDSDK_CORE_DISABLE_PROMPTS:-1}"

echo "==> Tearing down existing RC environment via canonical uninstall.sh..."
if ! ./uninstall.sh --non-interactive -y \
  --project-id="${GCP_PROJECT_ID}" \
  --region="${GCP_REGION}" \
  --cluster-name="${GKE_CLUSTER_NAME}"; then
  echo "⚠️ Warning: uninstall.sh exited with non-zero status (environment may not exist yet or teardown had partial errors). Proceeding with provisioning..." >&2
fi

INSTALL_ARGS=(
  --non-interactive -y
  --project-id="${GCP_PROJECT_ID}"
  --region="${GCP_REGION}"
  --cluster-name="${GKE_CLUSTER_NAME}"
  --image-tag="${IMAGE_TAG}"
)

if [ "${GOOGLE_CHAT_ENABLED:-false}" = "true" ]; then
  INSTALL_ARGS+=(--enable-google-chat)
fi

if [ -n "${GOOGLE_CHAT_MODE:-}" ]; then
  INSTALL_ARGS+=(--google-chat-mode="${GOOGLE_CHAT_MODE}")
fi

if [ -n "${CHAT_TOPIC_NAME:-}" ]; then
  INSTALL_ARGS+=(--chat-topic-name="${CHAT_TOPIC_NAME}")
fi

if [ -n "${MODEL_PROVIDER:-}" ]; then
  INSTALL_ARGS+=(--model-provider="${MODEL_PROVIDER}")
fi

if [ -n "${MODEL_DEFAULT_NAME:-}" ]; then
  INSTALL_ARGS+=(--model-default-name="${MODEL_DEFAULT_NAME}")
fi

if [ -n "${ENABLE_GVISOR:-}" ]; then
  INSTALL_ARGS+=(--gvisor="${ENABLE_GVISOR}")
fi

if [ -n "${PLATFORM_AGENT_PERMISSION_SET:-}" ]; then
  INSTALL_ARGS+=(--permission-set="${PLATFORM_AGENT_PERMISSION_SET}")
fi

if [ -n "${REGISTRY_PREFIX:-}" ]; then
  INSTALL_ARGS+=(--registry-prefix="${REGISTRY_PREFIX}")
fi

if [ -n "${USER_PROFILE_ENABLED:-}" ]; then
  INSTALL_ARGS+=(--user-profile-enabled="${USER_PROFILE_ENABLED}")
fi

# Memory mode mapping: kube_agents_memory/hindsight -> hindsight, none/off -> off, else -> file
if [ "${MEMORY_PROVIDER:-}" = "kube_agents_memory" ] || [ "${MEMORY_PROVIDER:-}" = "hindsight" ]; then
  INSTALL_ARGS+=(--memory=hindsight)
elif [ "${MEMORY_PROVIDER:-}" = "none" ] || [ "${MEMORY_PROVIDER:-}" = "off" ]; then
  INSTALL_ARGS+=(--memory=off)
else
  INSTALL_ARGS+=(--memory=file)
fi

echo "==> Provisioning fresh RC environment via canonical install.sh..."
./install.sh "${INSTALL_ARGS[@]}"
