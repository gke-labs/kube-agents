#!/usr/bin/env bash
# ==============================================================================
# Shared Bash Utilities for Provision & Teardown Pipeline
# ==============================================================================

# Determine paths relative to where this helper is loaded
if [ -z "${SCRIPT_DIR:-}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
# Honour a caller-provided path. Scripts under scripts/dev/ set SCRIPT_DIR to
# their own directory but keep the single state file in scripts/, so deriving
# the path from SCRIPT_DIR here would point them at a scripts/dev/vars.sh that
# load_state then creates empty — silently blanking IMAGE_TAG and AGENT_IMAGE.
VARS_FILE="${VARS_FILE:-${SCRIPT_DIR}/vars.sh}"

# Minimum tool versions. Sourced from the helper's own directory rather than
# SCRIPT_DIR, which callers under scripts/dev/ override to point at themselves.
# shellcheck source=k8s-operator/scripts/min_versions.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/min_versions.sh"

# gke_dns_endpoint_flag, shared with hack/ci-env.sh and scripts/release/common.sh.
# Resolved from BASH_SOURCE for the same reason as the line above.
# shellcheck source=k8s-operator/scripts/gke_dns_endpoint.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/gke_dns_endpoint.sh"

# Defaults, validators, vars.sh persistence, and the terraform.tfvars
# generator, shared with the installer front-ends (install.sh, uninstall.sh,
# upgrade.sh). The definitions moved there so the installers do not have to
# source this whole pipeline helper; this file keeps only what the numbered
# provision/teardown steps need on top.
# shellcheck source=k8s-operator/scripts/installer_common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/installer_common.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
# Empty unless stdout is a terminal and NO_COLOR is unset. This pipeline's output
# is routinely redirected — install.sh tees it to a log, CI captures it — and
# unconditional escapes turn those files into "^[[95m^[[1m>>> ..." noise. Every
# use is decorative interpolation, so empty values simply render plain text.
if [ -n "${NO_COLOR:-}" ] || [ ! -t 1 ]; then
  C_CYAN='' C_GREEN='' C_YELLOW='' C_MAGENTA='' C_BLUE='' C_RED='' C_RESET='' C_BOLD='' C_WHITE=''
else
  C_CYAN='\033[96m'
  C_GREEN='\033[92m'
  C_YELLOW='\033[93m'
  C_MAGENTA='\033[95m'
  C_BLUE='\033[94m'
  C_RED='\033[91m'
  C_RESET='\033[0m'
  C_BOLD='\033[1m'
  C_WHITE='\033[97m'
fi

# Stable project-level discovery marker for the GKE cluster hosting kube-agents.
# Keep this value aligned with the Terraform full-install composition and admin portal.
KUBE_AGENTS_HOST_LABEL="kube-agents-host"

# ─── UI Helpers ───────────────────────────────────────────────────────────────
print_step() { echo -e "\n${C_MAGENTA}${C_BOLD}>>>  $1  <<<${C_RESET}"; }
print_success() { echo -e "  ${C_GREEN}✓ $1${C_RESET}"; }
print_info() { echo -e "  ${C_CYAN}ℹ $1${C_RESET}"; }
print_warning() { echo -e "  ${C_YELLOW}⚠ $1${C_RESET}"; }
print_error() { echo -e "  ${C_RED}✗ $1${C_RESET}"; }

wait_for_a_bit() {
  local seconds=$1
  local msg=$2
  local spinner=( "⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏" )
  echo -ne "  ${C_YELLOW}${msg} (${seconds}s)...  "
  tput civis 2>/dev/null || true
  for (( i=0; i<seconds*10; i++ )); do
    local idx=$(( i % 10 ))
    echo -ne "\b${spinner[$idx]}"
    sleep 0.1
  done
  echo -ne "\b ${C_RESET}\n"
  tput cnorm 2>/dev/null || true
}

cleanup() { tput cnorm 2>/dev/null || true; }
trap cleanup EXIT

# ─── Universal Argument Parsing ──────────────────────────────────────────────
DRY_RUN="${DRY_RUN:-0}"
NO_CONFIRM="${NO_CONFIRM:-0}"
for arg in "$@"; do
  case $arg in
    --dry-run) DRY_RUN=1 ;;
    --no-confirm|-y) NO_CONFIRM=1 ;;
  esac
done

is_ci_pipeline() {
  is_truthy "${CI:-}"
}

init_var() {
  local var_name=$1
  local default_val=$2
  local prompt_msg=$3
  local current_val="${!var_name:-}"
  if [ -z "$current_val" ]; then
    local final_val
    if is_non_interactive; then
      final_val="$default_val"
    else
      echo -ne "  ${C_CYAN}${prompt_msg} [${C_WHITE}${default_val}${C_CYAN}]: ${C_RESET}"
      read -r input_val
      final_val="${input_val:-$default_val}"
    fi
    export "${var_name}=${final_val}"
    save_var "$var_name" "$final_val"
  fi
}

# ─── Container Registry ───────────────────────────────────────────────────────
# DEFAULT_REGISTRY_PREFIX comes from installer_common.sh; individual *_IMAGE
# variables still win over the prefix.
registry_prefix() {
  local prefix="${REGISTRY_PREFIX:-$DEFAULT_REGISTRY_PREFIX}"
  echo "${prefix%/}"
}

init_var_registry_prefix() {
  init_var "REGISTRY_PREFIX" "$DEFAULT_REGISTRY_PREFIX" "Enter Container Registry Prefix"
  case "$REGISTRY_PREFIX" in
    *"://"*)
      print_error "REGISTRY_PREFIX must be a bare registry path without a scheme (got '$REGISTRY_PREFIX'). Use e.g. 'registry.example.com/kube-agents'."
      exit 1
      ;;
  esac
  # init_var only saves values it prompted for; persist an env-exported
  # prefix too, so the remaining steps and later re-runs reuse it.
  save_var "REGISTRY_PREFIX" "$REGISTRY_PREFIX"

  # Deliberately not prompted for: leaving third-party images upstream is the
  # supported default, so a prompt would ask every installer to answer a
  # question only a mirrored install has. Export-only — persisted like every
  # other knob once it has been given.
  if [ -n "${THIRD_PARTY_REGISTRY_PREFIX:-}" ]; then
    case "$THIRD_PARTY_REGISTRY_PREFIX" in
      *"://"*)
        print_error "THIRD_PARTY_REGISTRY_PREFIX must be a bare registry path without a scheme (got '$THIRD_PARTY_REGISTRY_PREFIX'). Use e.g. 'registry.example.com/mirror'."
        exit 1
        ;;
    esac
    save_var "THIRD_PARTY_REGISTRY_PREFIX" "$THIRD_PARTY_REGISTRY_PREFIX"
  fi

  warn_unmirrored_third_party
}

# ─── Third-party images ───────────────────────────────────────────────────────
# Images an install pulls that this project does not build: the LiteLLM
# gateway, the fluent-bit logging sidecar, the GitHub token minter, and
# cert-manager. A mirror commonly keeps those under a different path from the
# kube-agents images, and an install may mirror one set without the other, so
# they get their own prefix rather than sharing REGISTRY_PREFIX.
#
# Their upstream references and pins live in images.json at the repo root — the
# same file `make mirror-images` copies from — so the pin the mirror was
# populated with and the pin an install asks for cannot drift apart. That is
# not hypothetical: the chart and the LiteLLM kustomization each carried their
# own pin, and one upgrade moved only one of them.
IMAGES_JSON="${IMAGES_JSON:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)/images.json}"

# The prefix third-party images resolve under, or empty for "leave them
# upstream". Set by THIRD_PARTY_REGISTRY_PREFIX and by nothing else.
#
# Deliberately not inherited from REGISTRY_PREFIX. That variable predates this
# inventory and has always meant "the registry holding the images this project
# builds"; a mirror populated to it holds those four and nothing more. Widening
# it to cert-manager, LiteLLM, fluent-bit, the token minter and Hindsight would redirect an
# existing install to references its mirror was never given — cert-manager first,
# where the wait in execute_cert_manager times out on ImagePullBackOff with the
# cluster already created. A single-prefix mirror is still one export away; it is
# just no longer assumed. warn_unmirrored_third_party below says so at the point
# the assumption used to fire.
third_party_registry_prefix() {
  local prefix="${THIRD_PARTY_REGISTRY_PREFIX:-}"
  echo "${prefix%/}"
}

# Warn once when a custom REGISTRY_PREFIX is set but third-party images are
# still resolving upstream. That combination is legitimate — it is what every
# pre-inventory install did — but it is also what a user who expected one
# prefix to cover everything would see, and the symptom otherwise arrives much
# later as a pull from a registry they thought they had left behind.
warn_unmirrored_third_party() {
  local prefix
  prefix="$(registry_prefix)"
  [ "$prefix" = "$DEFAULT_REGISTRY_PREFIX" ] && return 0
  [ -n "$(third_party_registry_prefix)" ] && return 0
  print_warning "REGISTRY_PREFIX is '${prefix}', but the third-party images (cert-manager, LiteLLM, fluent-bit, the GitHub token minter and Hindsight) will still be pulled from their upstream registries. Export THIRD_PARTY_REGISTRY_PREFIX (commonly the same value) to mirror those too — see 'make mirror-images'."
}

# Resolve a third-party image by its images.json name: the upstream reference
# for a default install, or "<prefix>/<name>:<tag>" once the images have been
# mirrored. The mirrored form is named after the inventory entry, matching what
# scripts/mirror_images.sh writes — the entry's .name, not the repository's
# trailing segment. The two differ for hindsight-postgresql
# (docker.io/ankane/pgvector), which is exactly the case this line used to get
# wrong: it said "the trailing image name only" while the code below has always
# used $name.
third_party_image() {
  local name=$1
  local repository tag prefix

  if [ ! -f "$IMAGES_JSON" ]; then
    print_error "images.json not found at '${IMAGES_JSON}'; cannot resolve the '${name}' image."
    return 1
  fi

  repository="$(jq -r --arg n "$name" '.images[] | select(.name == $n) | .repository' "$IMAGES_JSON")"
  tag="$(jq -r --arg n "$name" '.images[] | select(.name == $n) | .tag' "$IMAGES_JSON")"
  if [ -z "$repository" ] || [ "$repository" = "null" ] || [ -z "$tag" ] || [ "$tag" = "null" ]; then
    print_error "No image named '${name}' with a pinned tag in ${IMAGES_JSON}."
    return 1
  fi

  prefix="$(third_party_registry_prefix)"
  if [ -n "$prefix" ]; then
    # A pin can carry a digest in the tag position ("0.9.1@sha256:..."), which
    # names the upstream manifest. It cannot name the mirrored copy: you push
    # to a tag, never to a digest, so scripts/mirror_images.sh writes
    # "<prefix>/<name>:<tag>" with the digest stripped. Ask for the copy the
    # same way it was pushed — keeping the digest here would work only when the
    # mirror was populated with crane or skopeo, and resolve against nothing at
    # all after the docker fallback the script warns about.
    echo "${prefix}/${name}:${tag%%@*}"
  else
    echo "${repository}:${tag}"
  fi
}

# Export VAR with the resolved reference for an images.json entry, unless it is
# already set, and warn when an explicitly-set value sits outside the mirror.
#
# Deliberately not init_var: that would prompt for, and persist to vars.sh, a
# pin that images.json already owns. A saved pin is a second copy of the
# version, and a second copy is what let the chart sit on LiteLLM v1.92.0 for
# an entire release after the kustomize base moved to v1.95.0. Resolving on
# every run instead means upgrading the repo upgrades the pin. An operator who
# genuinely wants a different image still exports the variable, and that value
# wins here exactly as a saved one would.
init_third_party_image() {
  local var_name=$1
  local image_name=$2
  if [ -z "${!var_name:-}" ]; then
    local resolved
    resolved="$(third_party_image "$image_name")" || return 1
    export "${var_name}=${resolved}"
  fi
  warn_on_third_party_prefix_mismatch "$var_name"
}

# Warn when a persisted *_IMAGE value no longer lives under the effective
# registry prefix — e.g. REGISTRY_PREFIX was exported after a first run
# already saved image defaults derived from another registry. The saved
# value still wins (state reuse), so surface the mixed state instead of
# silently applying it halfway.
warn_on_registry_prefix_mismatch() {
  local var_name=$1
  local image_val="${!var_name:-}"
  [ -z "$image_val" ] && return 0
  case "$image_val" in
    "$(registry_prefix)"/*) ;;
    *)
      print_warning "${var_name}='${image_val}' does not match REGISTRY_PREFIX '$(registry_prefix)'. The saved value wins; edit ${VARS_FILE} (or unset ${var_name}) to migrate this image to the new registry."
      ;;
  esac
}

# The same check for an image this project does not build, which belongs under
# the third-party prefix rather than REGISTRY_PREFIX. A default install leaves
# that prefix empty and the image upstream, so there is nothing to compare.
warn_on_third_party_prefix_mismatch() {
  local var_name=$1
  local image_val="${!var_name:-}"
  local prefix
  prefix="$(third_party_registry_prefix)"
  if [ -z "$image_val" ] || [ -z "$prefix" ]; then
    return 0
  fi
  case "$image_val" in
    "$prefix"/*) ;;
    *)
      print_warning "${var_name}='${image_val}' is not under the third-party registry prefix '${prefix}'. That value still wins; unset ${var_name} (or edit ${VARS_FILE} if it was persisted there) to pull this image from the mirror."
      ;;
  esac
}

# Attach IMAGE_TAG to an image reference that carries neither a tag nor a
# digest. The saved *_IMAGE values are deliberately bare repository paths:
# IMAGE_TAG is scoped to one pipeline run and is never persisted to vars.sh
# (see init_var_image_tag), so the tag has to be re-attached where the
# reference is used. Handing a bare path to Kubernetes resolves it to
# ':latest', which the provisioner never builds or pushes.
# A reference that already names a tag or digest is returned untouched, so
# this is safe to apply to a user-supplied override.
#
# This is the shell twin of resolveAgentImage() in
# k8s-operator/internal/controller/manifest_helpers.go, which applies the same
# split-at-the-last-slash rule to CR-supplied images. The two differ on purpose
# when no tag is available: the operator is serving a live CR and falls back to
# "latest", while a provisioning run can still fail and so does, loudly. Change
# one and check the other.
qualify_image_ref() {
  local ref="$1"
  local tag="${2:-${IMAGE_TAG:-}}"
  if [ -z "$ref" ]; then
    print_error "qualify_image_ref: called with an empty image reference"
    return 1
  fi
  # Only the final path segment can hold the tag — a registry host may carry
  # a port, as in 'registry.example.com:5000/kube-agents/platform-agent'.
  case "${ref##*/}" in
    *:* | *@*) ;;
    *)
      if [ -z "$tag" ]; then
        print_error "qualify_image_ref: no tag available for bare reference '${ref}' (IMAGE_TAG is unset). Set IMAGE_TAG, or pin the reference with an explicit tag or digest."
        return 1
      fi
      ref="${ref}:${tag}"
      ;;
  esac
  echo "$ref"
}

init_var_kms_location() {
  init_var "KMS_LOCATION" "$(derive_kms_location "${REGION:-}")" "Enter Cloud KMS Location (a region; zones are not valid)"
}

init_var_model_provider() {
  init_var "MODEL_PROVIDER" "$DEFAULT_MODEL_PROVIDER" "Enter Model Provider (gemini, vertex_ai, anthropic, openai)"

  MODEL_PROVIDER=$(echo "$MODEL_PROVIDER" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
  if ! is_valid_model_provider "$MODEL_PROVIDER"; then
    print_error "Invalid Model Provider '$MODEL_PROVIDER'. Must be one of: gemini, vertex_ai, anthropic, openai."
    exit 1
  fi

  local DEFAULT_MODEL
  DEFAULT_MODEL="$(default_model_for_provider "$MODEL_PROVIDER")"

  init_var "MODEL_DEFAULT_NAME" "$DEFAULT_MODEL" "Enter Model Default Name"

  # Vertex has no API key; it needs a billing project and a serving location,
  # which is not always the cluster's region — Model Garden serves each partner
  # model from its own subset.
  if [ "$MODEL_PROVIDER" = "vertex_ai" ]; then
    init_var "VERTEX_PROJECT_ID" "${PROJECT_ID:-}" "Enter Vertex AI Project ID"
    init_var "VERTEX_LOCATION" "$DEFAULT_VERTEX_LOCATION" "Enter Vertex AI Location"
  fi
}

init_var_platform_agent_permission_set() {
  init_var "PLATFORM_AGENT_PERMISSION_SET" "read-only" "Enter Platform Agent Permission Set (read-only, gke-admin, custom)"

  PLATFORM_AGENT_PERMISSION_SET=$(echo "$PLATFORM_AGENT_PERMISSION_SET" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
  if ! is_valid_permission_set "$PLATFORM_AGENT_PERMISSION_SET"; then
    print_error "Invalid Platform Agent Permission Set '$PLATFORM_AGENT_PERMISSION_SET'. Must be one of: read-only, gke-admin, custom."
    exit 1
  fi

  if [ "$PLATFORM_AGENT_PERMISSION_SET" = "custom" ]; then
    init_var "PLATFORM_AGENT_CUSTOM_ROLES" "" "Enter Custom GCP IAM Roles (space or comma-separated)"
    if [ -z "${PLATFORM_AGENT_CUSTOM_ROLES:-}" ]; then
      print_error "Custom permission set selected, but PLATFORM_AGENT_CUSTOM_ROLES is empty."
      exit 1
    fi
  fi
}

# ─── Memory Provider ──────────────────────────────────────────────────────────
# The accepted values for MEMORY_PROVIDER.
#
# Two of these ship in this repo, and the difference between them is the whole
# choice: `kube_agents_memory` wraps the upstream `hindsight` plugin and needs an
# API server and a Postgres database in the cluster, while `multiuser_memory`
# keeps a per-user Markdown file inside the pod and needs nothing at all. The
# rest are the external plugins Hermes ships — see `memory.provider` in its
# hermes_cli/config.py.
#
# `multiuser_memory` is the default because it is what this repo shipped before
# `kube_agents_memory` existed: re-running provisioning against an install that
# never chose a provider must not silently grow it a Postgres database.
#
# `none` is this installer's spelling of "no external provider — keep Hermes'
# built-in store". Hermes itself spells that as the empty string, but an empty
# string cannot survive the trip through the CR: an absent field takes the CRD
# default, and the operator only overrides a non-empty one. So the choice is
# carried as `none` and the operator translates it back to "" when it renders
# config.yaml.
MEMORY_PROVIDER_CHOICES="none kube_agents_memory multiuser_memory hindsight mem0 openviking holographic retaindb byterover"

init_var_memory_provider() {
  init_var "MEMORY_PROVIDER" "multiuser_memory" \
    "Enter agent memory provider (${MEMORY_PROVIDER_CHOICES// /, })"

  MEMORY_PROVIDER=$(echo "$MEMORY_PROVIDER" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')

  # Someone answering the prompt with a bare Enter after clearing the default
  # means "no memory", which is `none` here.
  if [ -z "$MEMORY_PROVIDER" ]; then
    MEMORY_PROVIDER="none"
  fi

  local choice valid=1
  for choice in $MEMORY_PROVIDER_CHOICES; do
    if [ "$MEMORY_PROVIDER" = "$choice" ]; then
      valid=0
      break
    fi
  done
  if [ "$valid" -ne 0 ]; then
    print_error "Invalid agent memory provider '$MEMORY_PROVIDER'. Must be one of: ${MEMORY_PROVIDER_CHOICES// /, }."
    exit 1
  fi

  # Persist the normalised value so the migration and the lower-casing stick,
  # and so the later steps that read vars.sh see what this step decided.
  save_var "MEMORY_PROVIDER" "$MEMORY_PROVIDER"
}

# True when the selected provider is backed by the in-cluster Hindsight service.
# `kube_agents_memory` wraps the upstream `hindsight` plugin, so both talk to the
# same API server and both need the Hindsight store deployed; nothing else does.
memory_provider_uses_hindsight() {
  local provider
  provider=$(echo "${1:-}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
  case "$provider" in
    kube_agents_memory | hindsight) return 0 ;;
    *) return 1 ;;
  esac
}

is_non_interactive() {
  [ ! -t 0 ] || [ "${NO_CONFIRM:-0}" -eq 1 ] || [ "${DRY_RUN:-0}" -eq 1 ] || is_ci_pipeline
}

# IMAGE_TAG is deliberately NOT persisted to vars.sh: the tag usually changes
# between deploys, so it is scoped to a single execution. Callers export it
# (or are prompted when run standalone).
#
# Only the steps that deploy an image built from this repo need one, and they
# say so by setting REQUIRES_IMAGE_TAG=1 before calling load_state. Demanding it
# from every step made the secrets and integration steps — none of which mention
# IMAGE_TAG — fail outright in non-interactive mode.
init_var_image_tag() {
  if [ -z "${IMAGE_TAG:-}" ]; then
    if is_non_interactive; then
      print_error "IMAGE_TAG is required in non-interactive mode. Set it to an immutable release tag or validated commit SHA."
      exit 1
    else
      local default_tag="latest"
      echo -e "  ${C_CYAN}The base image tag is used for all images built from the kube-agents repo.${C_RESET}"
      echo -ne "  ${C_CYAN}Enter Base Image Tag (a commit SHA; 'latest' = latest commit on main) [${C_WHITE}${default_tag}${C_CYAN}]: ${C_RESET}"
      read -r input_tag
      export IMAGE_TAG="${input_tag:-$default_tag}"
    fi
  fi
}

load_state() {
  local env_registry_prefix="${REGISTRY_PREFIX:-}"
  local env_third_party_prefix="${THIRD_PARTY_REGISTRY_PREFIX:-}"
  if [ -f "$VARS_FILE" ]; then
    chmod 600 "$VARS_FILE" 2>/dev/null || true
    source "$VARS_FILE"
  elif [ "${DRY_RUN:-0}" -ne 1 ]; then
    local old_umask
    old_umask=$(umask)
    umask 077
    echo "# SRE Sourced Variables for GKE & GCP Setup" > "$VARS_FILE"
    chmod 600 "$VARS_FILE" 2>/dev/null || true
    umask "$old_umask"
    source "$VARS_FILE"
  fi
  # Sourcing vars.sh restores the saved REGISTRY_PREFIX over a freshly
  # exported one (saved state wins, as for every knob). Say so instead of
  # silently ignoring the export.
  if [ -n "$env_registry_prefix" ] && [ -n "${REGISTRY_PREFIX:-}" ] \
    && [ "$env_registry_prefix" != "$REGISTRY_PREFIX" ]; then
    print_warning "Ignoring exported REGISTRY_PREFIX='${env_registry_prefix}': the saved value '${REGISTRY_PREFIX}' from ${VARS_FILE} wins. Edit ${VARS_FILE} (REGISTRY_PREFIX and the saved *_IMAGE values) to change registries."
  fi
  # And the same for the third-party prefix, which is the one an operator is
  # most likely to export on a re-run after pointing cert-manager and
  # fluent-bit at a different mirror.
  if [ -n "$env_third_party_prefix" ] && [ -n "${THIRD_PARTY_REGISTRY_PREFIX:-}" ] \
    && [ "$env_third_party_prefix" != "$THIRD_PARTY_REGISTRY_PREFIX" ]; then
    print_warning "Ignoring exported THIRD_PARTY_REGISTRY_PREFIX='${env_third_party_prefix}': the saved value '${THIRD_PARTY_REGISTRY_PREFIX}' from ${VARS_FILE} wins. Edit ${VARS_FILE} to change it."
  fi
  if [ "${REQUIRES_IMAGE_TAG:-0}" -eq 1 ]; then
    init_var_image_tag
  fi
  init_var_registry_prefix
  export NAMESPACE="kubeagents-system"
  export PLATFORM_AGENT_KSA_NAME="kubeagents-platform-agent"
  export PLATFORM_AGENT_SANDBOX_KSA_NAME="platform-agent-sandbox"
  export PLATFORM_AGENT_GSA_NAME="kubeagents-platform-gsa"
  export CONTROLLER_KSA_NAME="kubeagents-controller"
  export CONTROLLER_GSA_NAME="kubeagents-controller-gsa"
  export GITHUB_MINTER_KSA_NAME="kubeagents-github-minter"
  export GITHUB_MINTER_GSA_NAME="kubeagents-github-minter-gsa"
  export LITELLM_KSA_NAME="kubeagents-litellm"
  export LITELLM_GSA_NAME="kubeagents-litellm-gsa"
}

ensure_teardown_state() {
  if [ -f "$VARS_FILE" ]; then
    chmod 600 "$VARS_FILE" 2>/dev/null || true
    source "$VARS_FILE"
    export GKE_DB_KMS_KEYRING="${GKE_DB_KMS_KEYRING:-}"
    export GKE_DB_KMS_KEY="${GKE_DB_KMS_KEY:-}"
    export GCP_ARTIFACT_REGISTRY_REPO_NAME="${GCP_ARTIFACT_REGISTRY_REPO_NAME:-${REPO_NAME:-kube-agents}}"
    export DEV_ARTIFACT_REGISTRY_CREATED="${DEV_ARTIFACT_REGISTRY_CREATED:-false}"
    export NAMESPACE="kubeagents-system"
    export PLATFORM_AGENT_KSA_NAME="kubeagents-platform-agent"
    export PLATFORM_AGENT_SANDBOX_KSA_NAME="platform-agent-sandbox"
    export PLATFORM_AGENT_GSA_NAME="kubeagents-platform-gsa"
    export CONTROLLER_KSA_NAME="kubeagents-controller"
    export CONTROLLER_GSA_NAME="kubeagents-controller-gsa"
    export GITHUB_MINTER_KSA_NAME="kubeagents-github-minter"
    export GITHUB_MINTER_GSA_NAME="kubeagents-github-minter-gsa"
    export LITELLM_KSA_NAME="kubeagents-litellm"
    export LITELLM_GSA_NAME="kubeagents-litellm-gsa"
  else
    echo -e "  ${C_YELLOW}⚠ State file ${VARS_FILE} not found. Prompting for target values...${C_RESET}"
    local ACTIVE_PROJECT
    ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
    if is_non_interactive; then
      export PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT_ID:-${ACTIVE_PROJECT:-}}}"
      if [ -z "$PROJECT_ID" ] && [ "${DRY_RUN:-0}" -eq 1 ]; then
        export PROJECT_ID="dummy-project"
      fi
      if [ -z "$PROJECT_ID" ]; then
        echo -e "  ${C_RED}✗ Project ID is required. Please export PROJECT_ID.${C_RESET}" >&2
        exit 1
      fi
      export REGION="${REGION:-${GCP_REGION:-$DEFAULT_REGION}}"
      export CLUSTER_NAME="${CLUSTER_NAME:-${GKE_CLUSTER_NAME:-$DEFAULT_CLUSTER_NAME}}"
    else
      echo -ne "  ${C_CYAN}Enter Target GCP Project ID [${C_WHITE}${ACTIVE_PROJECT}${C_CYAN}]: ${C_RESET}"
      read -r INPUT_PROJECT_ID
      export PROJECT_ID="${INPUT_PROJECT_ID:-$ACTIVE_PROJECT}"
      if [ -z "$PROJECT_ID" ]; then
        echo -e "  ${C_RED}✗ Project ID is required.${C_RESET}"
        exit 1
      fi
      export REGION="${REGION:-$DEFAULT_REGION}"
      echo -ne "  ${C_CYAN}Enter GKE GCP Region [${C_WHITE}${REGION}${C_CYAN}]: ${C_RESET}"
      read -r INPUT_REGION
      export REGION="${INPUT_REGION:-$REGION}"

      export CLUSTER_NAME="${CLUSTER_NAME:-platform-agent-host}"
      echo -ne "  ${C_CYAN}Enter GKE Cluster Name [${C_WHITE}${CLUSTER_NAME}${C_CYAN}]: ${C_RESET}"
      read -r INPUT_CLUSTER_NAME
      export CLUSTER_NAME="${INPUT_CLUSTER_NAME:-$CLUSTER_NAME}"
    fi
    export NAMESPACE="kubeagents-system"
    export GKE_DB_KMS_KEYRING="${GKE_DB_KMS_KEYRING:-}"
    export GKE_DB_KMS_KEY="${GKE_DB_KMS_KEY:-}"
    export GCP_ARTIFACT_REGISTRY_REPO_NAME="${GCP_ARTIFACT_REGISTRY_REPO_NAME:-${REPO_NAME:-kube-agents}}"
    export DEV_ARTIFACT_REGISTRY_CREATED="${DEV_ARTIFACT_REGISTRY_CREATED:-false}"
    if [ "${GOOGLE_CHAT_ENABLED:-false}" = "true" ]; then
      export CHAT_TOPIC_NAME="${CHAT_TOPIC_NAME:-platform-agent-chat-events}"
      export CHAT_SUB_NAME="${CHAT_SUB_NAME:-platform-agent-chat-events-sub}"
    else
      export CHAT_TOPIC_NAME="${CHAT_TOPIC_NAME:-}"
      export CHAT_SUB_NAME="${CHAT_SUB_NAME:-}"
    fi
    export PLATFORM_AGENT_KSA_NAME="kubeagents-platform-agent"
    export PLATFORM_AGENT_SANDBOX_KSA_NAME="platform-agent-sandbox"
    export PLATFORM_AGENT_GSA_NAME="kubeagents-platform-gsa"
    export CONTROLLER_KSA_NAME="kubeagents-controller"
    export CONTROLLER_GSA_NAME="kubeagents-controller-gsa"
    export GITHUB_MINTER_KSA_NAME="kubeagents-github-minter"
    export GITHUB_MINTER_GSA_NAME="kubeagents-github-minter-gsa"
    export LITELLM_KSA_NAME="kubeagents-litellm"
    export LITELLM_GSA_NAME="kubeagents-litellm-gsa"
  fi
}

# ─── Step Runner Framework ────────────────────────────────────────────────────
run_step() {
  local name=$1
  local verify_func=$2
  local execute_func=$3
  local wait_time=${4:-0}
  
  print_step "$name"
  echo -e "  ${C_CYAN}Verifying current state...${C_RESET}"
  
  if $verify_func; then
    print_success "Already completed: $name"
    return 0
  fi
  
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    print_info "[DRY-RUN] Would execute: $name"
    return 0
  fi

  print_info "Executing action..."
  if $execute_func; then
    print_success "Successfully executed."
    if [ "$wait_time" -gt 0 ]; then
      wait_for_a_bit "$wait_time" "Waiting for changes to propagate"
    fi
  else
    print_error "Failed to execute step: $name"
    exit 1
  fi
}

# ─── Smart Deployment Step Runner (Routes based on CI/CD mode) ────────────────
run_deploy_step() {
  local name=$1
  local verify_func=$2
  local execute_func=$3
  local wait_time=${4:-0}

  if is_ci_pipeline; then
    local force_redeploy_verify="false"
    run_step "$name" "$force_redeploy_verify" "$execute_func" "$wait_time"
  else
    run_step "$name" "$verify_func" "$execute_func" "$wait_time"
  fi
}

# ─── Cloud Helpers ────────────────────────────────────────────────────────────
check_prereqs() {
  for cmd in "$@"; do
    echo -ne "  ${C_CYAN}Checking for $cmd... ${C_RESET}"
    if command -v "$cmd" &> /dev/null; then
      echo -e "✅"
    else
      echo -e "❌"
      print_error "$cmd is required but not installed. Please install it and rerun."
      exit 1
    fi
  done
}

cluster_exists() {
  gcloud container clusters list --filter="name=${CLUSTER_NAME} AND location=${REGION}" --format="value(name)" --project="${PROJECT_ID}" 2>/dev/null || echo ""
}

host_label_value() {
  gcloud container clusters describe "$CLUSTER_NAME" \
    --location="$REGION" \
    --project="$PROJECT_ID" \
    --format="value(resourceLabels.${KUBE_AGENTS_HOST_LABEL})" 2>/dev/null
}

verify_host_label() {
  [ "$(host_label_value)" = "true" ]
}

update_host_label() {
  gcloud container clusters update "$CLUSTER_NAME" \
    --location="$REGION" \
    --project="$PROJECT_ID" \
    --update-labels="${KUBE_AGENTS_HOST_LABEL}=true" \
    --quiet
}

execute_host_label() {
  retry 3 5 update_host_label
}

connect_cluster() {
  print_info "Fetching cluster credentials..."
  gke_dns_endpoint_flag "$CLUSTER_NAME" "$REGION" "$PROJECT_ID"
  if [ -n "$GKE_DNS_ENDPOINT_FLAG" ]; then
    print_info "Cluster '$CLUSTER_NAME' publishes an external DNS endpoint; using it."
  fi
  # Unquoted on purpose: empty must contribute no argument at all.
  # shellcheck disable=SC2086
  gcloud container clusters get-credentials "$CLUSTER_NAME" --location "$REGION" --project "$PROJECT_ID" --quiet $GKE_DNS_ENDPOINT_FLAG
}

ensure_k8s_resource_exists() {
  local resource=$1         # e.g., "deployment/cert-manager-cainjector"
  local namespace=$2        # e.g., "cert-manager"
  local retries=${3:-10}    # Default 10 retries (20s timeout)

  print_info "Checking existence of ${resource} in namespace '${namespace}'..."
  if [ "${DRY_RUN:-0}" -eq 1 ]; then return 0; fi

  _check_resource_exists() {
    kubectl get "${resource}" -n "${namespace}" &>/dev/null
  }

  if ! retry "$retries" 2 _check_resource_exists; then
    print_error "Timeout waiting for ${resource} to be created in '${namespace}'." >&2
    return 1
  fi
  print_success "${resource} exists in '${namespace}'."
}

wait_for_k8s_resource() {
  local resource=$1                 # e.g., "deployment/cert-manager"
  local namespace=$2                # e.g., "cert-manager"
  local condition=${3:-"Available"} # e.g., "Available"
  local timeout=${4:-"120s"}

  # Step 1: Ensure resource exists in API server etcd before calling 'kubectl wait'
  ensure_k8s_resource_exists "${resource}" "${namespace}" 10 || return 1

  print_info "Waiting for ${resource} in namespace '${namespace}' (condition=${condition})..."
  if [ "${DRY_RUN:-0}" -eq 1 ]; then return 0; fi

  # Step 2: Wait for condition availability
  kubectl wait --for="condition=${condition}" "${resource}" -n "${namespace}" --timeout="${timeout}" || return 1
  print_success "${resource} reached state: ${condition}."
}

register_host_label() {
  print_step "3. Register kube-agents host"
  print_info "Verifying current state..."

  if verify_host_label; then
    print_success "Already completed: 3. Register kube-agents host"
    return 0
  fi

  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    print_info "[DRY-RUN] Would execute: 3. Register kube-agents host"
    return 0
  fi

  print_info "Applying ${KUBE_AGENTS_HOST_LABEL}=true to '${CLUSTER_NAME}'..."
  if execute_host_label; then
    print_success "Registered the kube-agents host."
  else
    print_warning "Could not apply ${KUBE_AGENTS_HOST_LABEL}=true. Provisioning will continue; rerun step 08 to retry host discovery registration."
  fi
  return 0
}

confirm_action() {
  local warning_msg=$1
  shift
  
  if [ "${NO_CONFIRM:-0}" -eq 1 ] || [ "${DRY_RUN:-0}" -eq 1 ] || is_ci_pipeline; then
    return 0
  fi
  
  echo ""
  echo -e "${C_RED}${C_BOLD}🚨 WARNING: ${warning_msg}${C_RESET}"
  echo -e "${C_YELLOW}==============================================================================${C_RESET}"
  for item in "$@"; do
    local key="${item%%:*}"
    local val="${item#*:}"
    printf "  ${C_BOLD}%-15s${C_RESET} %s\n" "$key:" "$val"
  done
  echo -e "${C_YELLOW}==============================================================================${C_RESET}"
  echo ""
  echo -ne "  ${C_CYAN}Are you sure you want to proceed? (y/N): ${C_RESET}"
  read -r -n 1 REPLY
  echo
  if ! is_truthy "$REPLY"; then
      echo -e "  ${C_YELLOW}ℹ Aborted.${C_RESET}"
      exit 0
  fi
}
