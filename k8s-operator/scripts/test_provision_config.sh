#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_eq() {
  local expected=$1
  local actual=$2
  local message=$3
  [ "$expected" = "$actual" ] || fail "${message}: expected '${expected}', got '${actual}'"
}

test_standard_defaults() (
  local temp_dir
  temp_dir="$(mktemp -d)"
  trap 'rm -rf "$temp_dir"' EXIT
  export KUBE_AGENTS_VARS_FILE="${temp_dir}/vars.sh"
  source "${SCRIPT_DIR}/common.sh" --non-interactive

  touch "$VARS_FILE"
  INTERACTION_MODE=""
  apply_standard_provision_defaults
  configure_platform_agent_read_only_capabilities

  assert_eq "standard" "$PROVISION_SETUP" "setup profile"
  assert_eq "read-only" "$PLATFORM_AGENT_PERMISSION_SET" "permission default"
  assert_eq "" "$PLATFORM_AGENT_READ_ONLY_ROLES" "optional read-only role default"
  assert_eq "roles/container.clusterViewer" "$(get_platform_agent_read_only_roles)" "minimum read-only roles"
  assert_eq "gemini" "$MODEL_PROVIDER" "model provider default"
  assert_eq "api" "$INTERACTION_MODE" "interaction default"
  assert_eq "false" "$GOOGLE_CHAT_ENABLED" "Google Chat default"
  assert_eq "false" "$SLACK_ENABLED" "Slack default"
)

test_interaction_modes() (
  local temp_dir
  temp_dir="$(mktemp -d)"
  trap 'rm -rf "$temp_dir"' EXIT
  export KUBE_AGENTS_VARS_FILE="${temp_dir}/vars.sh"
  source "${SCRIPT_DIR}/common.sh" --non-interactive

  touch "$VARS_FILE"
  INTERACTION_MODE="google-chat"
  normalize_interaction_mode
  assert_eq "true" "$GOOGLE_CHAT_ENABLED" "Google Chat selection"
  assert_eq "false" "$SLACK_ENABLED" "Slack disabled for Google Chat selection"

  INTERACTION_MODE="both"
  normalize_interaction_mode
  assert_eq "true" "$GOOGLE_CHAT_ENABLED" "Google Chat enabled for both"
  assert_eq "true" "$SLACK_ENABLED" "Slack enabled for both"

  INTERACTION_MODE="unsupported"
  if normalize_interaction_mode >/dev/null 2>&1; then
    fail "invalid interaction mode was accepted"
  fi
)

test_cli_overrides_survive_state_loading() (
  local temp_dir
  temp_dir="$(mktemp -d)"
  trap 'rm -rf "$temp_dir"' EXIT
  export KUBE_AGENTS_VARS_FILE="${temp_dir}/vars.sh"
  source "${SCRIPT_DIR}/common.sh" --non-interactive

  cat > "$VARS_FILE" <<'EOF'
export PROJECT_ID=old-project
export CLUSTER_NAME=old-cluster
export REGION=old-region
export PLATFORM_AGENT_PERMISSION_SET=read-only
EOF

  parse_common_args \
    --project=new-project \
    --cluster=new-cluster \
    --region=new-region \
    --permissions=gke-admin \
    --interaction=slack \
    --agent-name=ux-e2e
  collect_provision_configuration >/dev/null

  assert_eq "new-project" "$PROJECT_ID" "project CLI override"
  assert_eq "new-cluster" "$CLUSTER_NAME" "cluster CLI override"
  assert_eq "new-region" "$REGION" "region CLI override"
  assert_eq "gke-admin" "$PLATFORM_AGENT_PERMISSION_SET" "permission CLI override"
  assert_eq "slack" "$INTERACTION_MODE" "interaction CLI override"
  assert_eq "true" "$SLACK_ENABLED" "Slack enabled by CLI"
  assert_eq "false" "$GOOGLE_CHAT_ENABLED" "Google Chat disabled by Slack CLI"
  assert_eq "ux-e2e" "$PLATFORM_AGENT_NAME" "agent name CLI override"
  assert_eq "kubeagents-ux-e2e" "$PLATFORM_AGENT_KSA_NAME" "derived KSA name"
  assert_eq "kubeagents-ux-e2e-gsa" "$PLATFORM_AGENT_GSA_NAME" "derived GSA name"
)

test_existing_interaction_is_preserved() (
  local temp_dir
  temp_dir="$(mktemp -d)"
  trap 'rm -rf "$temp_dir"' EXIT
  export KUBE_AGENTS_VARS_FILE="${temp_dir}/vars.sh"
  source "${SCRIPT_DIR}/common.sh" --non-interactive

  touch "$VARS_FILE"
  GOOGLE_CHAT_ENABLED="true"
  SLACK_ENABLED="false"
  INTERACTION_MODE=""
  normalize_interaction_mode

  assert_eq "google-chat" "$INTERACTION_MODE" "inferred interaction"
  assert_eq "true" "$GOOGLE_CHAT_ENABLED" "existing Google Chat configuration"
)

test_provider_override_selects_matching_model() (
  local temp_dir
  temp_dir="$(mktemp -d)"
  trap 'rm -rf "$temp_dir"' EXIT
  export KUBE_AGENTS_VARS_FILE="${temp_dir}/vars.sh"
  source "${SCRIPT_DIR}/common.sh" --non-interactive

  printf '%s\n' \
    'export MODEL_PROVIDER=gemini' \
    'export MODEL_DEFAULT_NAME=gemini-3.5-flash' > "$VARS_FILE"

  parse_common_args --model-provider=openai
  collect_provision_configuration >/dev/null

  assert_eq "openai" "$MODEL_PROVIDER" "model provider override"
  assert_eq "gpt-5.4" "$MODEL_DEFAULT_NAME" "provider-specific default model"
)

test_read_only_capability_flag() (
  local temp_dir
  temp_dir="$(mktemp -d)"
  trap 'rm -rf "$temp_dir"' EXIT
  export KUBE_AGENTS_VARS_FILE="${temp_dir}/vars.sh"
  source "${SCRIPT_DIR}/common.sh" \
    --non-interactive \
    --read-only-capabilities=monitoring,logging

  touch "$VARS_FILE"
  PLATFORM_AGENT_PERMISSION_SET="read-only"
  configure_platform_agent_read_only_capabilities \
    "$READ_ONLY_CAPABILITIES_ARG_SET" \
    "$PLATFORM_AGENT_READ_ONLY_CAPABILITIES"

  assert_eq \
    "roles/monitoring.viewer roles/logging.viewer" \
    "$PLATFORM_AGENT_READ_ONLY_ROLES" \
    "selected optional read-only roles"
  assert_eq \
    "roles/container.clusterViewer roles/monitoring.viewer roles/logging.viewer" \
    "$(get_platform_agent_read_only_roles)" \
    "effective read-only roles"
)

test_read_only_interactive_toggle_and_persistence() (
  local temp_dir
  temp_dir="$(mktemp -d)"
  trap 'rm -rf "$temp_dir"' EXIT
  export KUBE_AGENTS_VARS_FILE="${temp_dir}/vars.sh"
  source "${SCRIPT_DIR}/common.sh"

  touch "$VARS_FILE"
  PLATFORM_AGENT_PERMISSION_SET="read-only"
  PLATFORM_AGENT_READ_ONLY_ROLES=""
  prompt_platform_agent_read_only_capabilities \
    < <(printf '1,2\n1\n\n') \
    >/dev/null
  assert_eq "roles/logging.viewer" "$PLATFORM_AGENT_READ_ONLY_ROLES" "interactive toggles"

  prompt_platform_agent_read_only_capabilities \
    < <(printf '\n') \
    >/dev/null
  assert_eq "roles/logging.viewer" "$PLATFORM_AGENT_READ_ONLY_ROLES" "persisted selection"
)

test_invalid_read_only_capability_fails_closed() (
  local temp_dir
  temp_dir="$(mktemp -d)"
  trap 'rm -rf "$temp_dir"' EXIT
  export KUBE_AGENTS_VARS_FILE="${temp_dir}/vars.sh"
  source "${SCRIPT_DIR}/common.sh" --non-interactive

  touch "$VARS_FILE"
  PLATFORM_AGENT_PERMISSION_SET="read-only"
  if configure_platform_agent_read_only_capabilities 1 "monitoring,not-a-capability" >/dev/null 2>&1; then
    fail "unsupported read-only capability was accepted"
  fi
)

test_standard_defaults
test_interaction_modes
test_cli_overrides_survive_state_loading
test_existing_interaction_is_preserved
test_provider_override_selects_matching_model
test_read_only_capability_flag
test_read_only_interactive_toggle_and_persistence
test_invalid_read_only_capability_fails_closed

if (
  source "${SCRIPT_DIR}/common.sh"
  parse_common_args --project
); then
  fail "missing CLI option value was accepted"
fi

echo "PASS: provisioning configuration tests"
