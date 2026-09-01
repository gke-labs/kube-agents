#!/usr/bin/env bash
# ==============================================================================
# 🤖 Kubernetes Agentic Harness (kube-agents) Zero-Friction Installer
# ==============================================================================
# Usage (Interactive):
#   curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/main/install.sh | bash
#
# Usage (AI Agents & Non-Interactive Automation):
#   curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/main/install.sh | bash -s -- \
#     --non-interactive --project-id="my-gcp-project" --cluster-name="platform-agent"
#
# Designed for Google Cloud Shell, Linux, macOS, and AI Agent harnesses.
# ==============================================================================

set -Eeuo pipefail

# ─── ANSI Colors & Terminal Responsive Helpers ─────────────────────────────────
# A function because k8s-operator/scripts/common.sh defines the same variables
# unconditionally: sourcing it would re-enable colour under NO_COLOR or in a pipe,
# so the installer re-applies its own policy afterwards.
configure_colors() {
  if [ -n "${NO_COLOR:-}" ] || [ ! -t 1 ]; then
    C_CYAN='' C_GREEN='' C_YELLOW='' C_MAGENTA='' C_RED='' C_RESET='' C_BOLD='' C_UNDERLINE=''
  else
    # Use \033 rather than \e: bash 3.2 (the /bin/bash macOS ships) does not
    # expand \e in `echo -e`, so the raw escapes leak into the terminal.
    C_CYAN='\033[96m' C_GREEN='\033[92m' C_YELLOW='\033[93m' C_MAGENTA='\033[95m' C_RED='\033[91m' C_RESET='\033[0m' C_BOLD='\033[1m' C_UNDERLINE='\033[4m'
  fi
}
configure_colors

# ─── Process Lock File & Error Trap Handling ────────────────────────────────
LOCK_FILE="/tmp/kube-agents-install.lock"
if command -v flock >/dev/null 2>&1; then
  if ( : >"$LOCK_FILE" ) 2>/dev/null && exec 200>"$LOCK_FILE"; then
    if ! flock -n 200 2>/dev/null; then
      echo -e "  \033[93m⚠ Another instance of kube-agents installer is currently running. Exiting.\033[0m" >&2
      exit 1
    fi
  fi
fi

on_error() {
  local exit_code="$1"
  local line_no="$2"
  local bash_cmd="$3"
  echo -e "\n\033[91m\033[1m✗ Error encountered at line ${line_no} (exit code ${exit_code}): ${bash_cmd}\033[0m" >&2
  write_json_report "FAILED" "${line_no}" "${bash_cmd}" 2>/dev/null || true
  if [[ "${vars_file:-}" == *.tmp ]]; then
    rm -f -- "$vars_file"
  fi
  exit "$exit_code"
}
trap 'on_error $? $LINENO "$BASH_COMMAND"' ERR

# Sourced/baked release version. On developer checkouts (main), this is empty.
# Release automation stamps this value (e.g. BAKED_RELEASE_VERSION="0.2.0") when publishing a GA release.
BAKED_RELEASE_VERSION=""

# ─── Agentic & Automation Parameter States ────────────────────────────────────
PARAM_NON_INTERACTIVE="${NONINTERACTIVE:-false}"
PARAM_DRY_RUN="${DRY_RUN:-false}"
PARAM_PROJECT_ID="${PROJECT_ID:-}"
PARAM_REGION="${REGION:-}"
PARAM_CLUSTER_NAME="${CLUSTER_NAME:-}"
# Only consulted when this run creates the cluster. Against one that already
# exists the tfvars generator's live probe decides the shape; see
# write_tfvars_from_state in k8s-operator/scripts/installer_common.sh. Empty
# means "not chosen yet" — the interview asks, and falls back to
# installer_common.sh's DEFAULT_CLUSTER_MODE when there is nobody to ask. The
# shape is deliberately not named here: that table is the one home for it.
PARAM_CLUSTER_MODE="${CLUSTER_MODE:-}"
# Left empty on purpose: resolved from installer_common.sh's DEFAULT_* once
# the installer helpers are sourced, so no default is spelled twice.
PARAM_MODEL_PROVIDER="${MODEL_PROVIDER:-}"
PARAM_VERTEX_PROJECT_ID="${VERTEX_PROJECT_ID:-}"
PARAM_VERTEX_LOCATION="${VERTEX_LOCATION:-}"
PARAM_GEMINI_API_KEY="${GEMINI_API_KEY:-}"
PARAM_OPENAI_API_KEY="${OPENAI_API_KEY:-}"
PARAM_ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
PARAM_GITOPS_ORG="${GITHUB_ORG:-}"
PARAM_GITOPS_REPO="${GITHUB_REPO:-}"
PARAM_PERMISSION_SET="${PLATFORM_AGENT_PERMISSION_SET:-read-only}"
PARAM_CUSTOM_ROLES="${PLATFORM_AGENT_CUSTOM_ROLES:-}"
PARAM_ENABLE_GVISOR="${ENABLE_GVISOR:-true}"
PARAM_ENABLE_WEBUI="${ENABLE_WEBUI:-false}"
PARAM_MEMORY="${MEMORY:-file}"
PARAM_IMAGE_TAG="${IMAGE_TAG:-}"
PARAM_ALLOW_UNVERIFIED_SOURCE="${ALLOW_UNVERIFIED_SOURCE:-false}"
# "<repo_dir>@<ref>" already checked by verify_local_source_ref, so the pre-flight
# check and the one at the workspace step do not report the same verdict twice.
SOURCE_REF_VERIFIED=""
PARAM_REGISTRY_PREFIX="${REGISTRY_PREFIX:-}"
# Empty means "leave the third-party images on their upstream registries", the
# supported default. Unlike REGISTRY_PREFIX this has no fallback in common.sh,
# because widening REGISTRY_PREFIX to cover images its mirror was never given is
# exactly the failure third_party_registry_prefix() exists to avoid.
PARAM_THIRD_PARTY_REGISTRY_PREFIX="${THIRD_PARTY_REGISTRY_PREFIX:-}"

show_help() {
  cat << EOF
🤖 kube-agents Zero-Friction Installer

Usage:
  ./install.sh [FLAGS]

Flags for AI Agents & Automation:
  -y, --yes, --non-interactive  Run in non-interactive mode (use flags/defaults)
  --dry-run                     Validate prerequisites & output config/plan without creating resources
  --project-id=ID               Target GCP Project ID
  --region=REGION               Target GCP Region (default: k8s-operator/scripts/common.sh
                                DEFAULT_REGION, currently us-central1)
  --cluster-name=NAME           GKE Cluster Name (default: DEFAULT_CLUSTER_NAME,
                                currently platform-agent-host)
  --cluster-mode=MODE           Shape of a cluster this run creates: autopilot | standard
                                (default: DEFAULT_CLUSTER_MODE, currently autopilot).
                                Autopilot clusters are regional. Passing this flag with
                                autopilot and a zonal --region is an error; leaving it
                                unset at a zonal --region builds Standard instead.
                                Ignored when installing onto a cluster that already
                                exists — its live shape wins.
  --model-provider=PROVIDER     Model provider: gemini | vertex_ai | anthropic | openai
                                (default: gemini)
  --model-default-name=NAME     Default model name for the provider
  --vertex-project-id=ID        GCP project serving Vertex AI models (default: --project-id)
  --vertex-location=LOCATION    Vertex AI serving location, a region or "global"
                                (default: DEFAULT_VERTEX_LOCATION, currently global)
  --gemini-api-key=KEY          Gemini API Key
  --openai-api-key=KEY          OpenAI API Key
  --anthropic-api-key=KEY       Anthropic API Key
  --gitops-org=ORG              GitHub Org/Username for GitOps repo
  --gitops-repo=REPO            GitOps IaC Repository Name (default: gke-fleet-iac)
  --permission-set=SET          Agent GCP IAM permission set: read-only | custom
                                (default: read-only)
  --custom-roles=ROLES          Roles for --permission-set=custom (space- or comma-separated)
  --gvisor=true|false           Enable GKE Sandbox (gVisor) runtime isolation (default: true)
  --enable-web-ui=true|false    Enable Hermes Web UI port 9119 dashboard (default: false)
  --user-profile-enabled=BOOL   Enable user profile persona extensions (default: false)
  --memory=MODE                 Long-term agent memory: file | hindsight | off
                                (default: file)
                                  file      SMALL / PERSONAL deployments, and the default —
                                            it is what every install got before the searchable
                                            store existed, so an upgrade that says nothing
                                            keeps the store it already has. Per-user Markdown
                                            files inside the pod (multiuser_memory). No extra
                                            services, but the whole store is loaded into the
                                            model's context every turn, so it stops scaling
                                            once there is more than a few pages of it.
                                  hindsight ENTERPRISE deployments. Searchable, ranked recall
                                            that stays affordable as the store grows
                                            (kube_agents_memory). Deploys the Hindsight API
                                            and a Postgres database into the cluster.
                                  off       nothing is retained between sessions. No memory
                                            provider, and no database to run.
  --image-tag=TAG               Validated immutable release tag or full commit SHA
                                (default: this checkout's HEAD; required via curl | bash)
  --registry-prefix=PATH        Container registry path without a URL scheme, for the images
                                this project builds (operator, agent, credential proxy, replay
                                proxy)
  --third-party-registry-prefix=PATH
                                Registry path holding the mirrored third-party images
                                (LiteLLM, fluent-bit, the GitHub token minter, Hindsight).
                                Unset, they follow --registry-prefix when that is set and
                                stay upstream otherwise. See 'make mirror-images'
  --allow-unverified-source     Provision from a dirty or mismatched checkout (local script edits
                                are applied even though the deployed image was built elsewhere)
  --enable-google-chat          Enable Google Chat integration
  --chat-topic-name=TOPIC       Pub/Sub topic name for Google Chat (default: platform-agent-chat-events)
  --google-chat-mode=MODE       Google Chat output mode: default | debug (default: default)
  --menu, --config              Launch interactive Day-2 Control Panel Menu (raspi-config style)
  -h, --help, -?                Show this help message
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -y|--yes|--non-interactive) PARAM_NON_INTERACTIVE="true"; shift ;;
      --dry-run) PARAM_DRY_RUN="true"; shift ;;
      --menu|--config|--configure|menu|config) PARAM_MENU_MODE="true"; shift ;;
      --project-id=*) PARAM_PROJECT_ID="${1#*=}"; shift ;;
      --region=*) PARAM_REGION="${1#*=}"; shift ;;
      --cluster-name=*) PARAM_CLUSTER_NAME="${1#*=}"; shift ;;
      --cluster-mode=*) PARAM_CLUSTER_MODE="${1#*=}"; shift ;;
      --model-provider=*) PARAM_MODEL_PROVIDER="${1#*=}"; shift ;;
      --model-default-name=*) PARAM_MODEL_DEFAULT_NAME="${1#*=}"; shift ;;
      --vertex-project-id=*) PARAM_VERTEX_PROJECT_ID="${1#*=}"; shift ;;
      --vertex-location=*) PARAM_VERTEX_LOCATION="${1#*=}"; shift ;;
      --gemini-api-key=*) PARAM_GEMINI_API_KEY="${1#*=}"; shift ;;
      --openai-api-key=*) PARAM_OPENAI_API_KEY="${1#*=}"; shift ;;
      --anthropic-api-key=*) PARAM_ANTHROPIC_API_KEY="${1#*=}"; shift ;;
      --gitops-org=*) PARAM_GITOPS_ORG="${1#*=}"; shift ;;
      --gitops-repo=*) PARAM_GITOPS_REPO="${1#*=}"; shift ;;
      --permission-set=*) PARAM_PERMISSION_SET="${1#*=}"; shift ;;
      --custom-roles=*) PARAM_CUSTOM_ROLES="${1#*=}"; shift ;;
      --gvisor=*) PARAM_ENABLE_GVISOR="${1#*=}"; shift ;;
      --enable-web-ui=*|--enable-webui=*|--webui=*) PARAM_ENABLE_WEBUI="${1#*=}"; shift ;;
      --enable-web-ui|--enable-webui|--webui) PARAM_ENABLE_WEBUI="true"; shift ;;
      --user-profile-enabled=*) PARAM_USER_PROFILE_ENABLED="${1#*=}"; shift ;;
      --memory=*) PARAM_MEMORY="${1#*=}"; shift ;;
      --image-tag=*) PARAM_IMAGE_TAG="${1#*=}"; shift ;;
      --registry-prefix=*) PARAM_REGISTRY_PREFIX="${1#*=}"; shift ;;
      --third-party-registry-prefix=*) PARAM_THIRD_PARTY_REGISTRY_PREFIX="${1#*=}"; shift ;;
      --allow-unverified-source|--allow-dirty) PARAM_ALLOW_UNVERIFIED_SOURCE="true"; shift ;;
      --enable-google-chat|--google-chat) PARAM_ENABLE_GOOGLE_CHAT="true"; shift ;;
      --chat-topic-name=*) PARAM_CHAT_TOPIC_NAME="${1#*=}"; shift ;;
      --google-chat-mode=*) PARAM_GOOGLE_CHAT_MODE="${1#*=}"; shift ;;
      -h|--help|-\?|help) show_help; exit 0 ;;
      *) print_error "Unknown parameter: $1"; show_help >&2; return 2 ;;
    esac
  done
}

get_term_width() {
  local cols
  cols=$(tput cols 2>/dev/null || echo 80)
  if ! [[ "$cols" =~ ^[0-9]+$ ]] || [ "$cols" -lt 40 ]; then
    cols=80
  fi
  echo "$cols"
}

draw_separator() {
  local width
  width=$(get_term_width)
  if [ "$width" -gt 75 ]; then
    width=75
  fi
  printf '%*s' "$width" '' | tr ' ' '='
  printf '\n'
}

print_banner() {
  local term_w
  term_w=$(get_term_width)

  printf '%b\n' "${C_CYAN}${C_BOLD}"
  draw_separator

  if [ "$term_w" -ge 60 ]; then
    cat << "EOF"
    __ ____  ______  ______     ___   _____________   _____________
   / //_/ / / / __ )/ ____/    /   | / ____/ ____/ | / /_  __/ ___/
  / ,< / / / / __  / __/______/ /| |/ / __/ __/ /  |/ / / /  \__ \
 / /| / /_/ / /_/ / /__/_____/ ___ / /_/ / /___/ /|  / / /  ___/ /
/_/ |_\____/_____/_____/    /_/  |_\____/_____/_/ |_/ /_/  /____/
EOF
  else
    printf '%b\n' "🤖 KUBE-AGENTS PLATFORM HARNESS"
  fi

  printf '\n%b\n' "🤖 Kubernetes Agentic Harness (kube-agents) Zero-Friction Installer"
  draw_separator
  printf '%b\n\n' "${C_RESET}"
}

# Re-applied after sourcing common.sh, which defines its own print_* helpers
# formatted for the state file.
define_print_helpers() {
  print_step() { echo -e "\n${C_MAGENTA}${C_BOLD}>>> $1 <<<${C_RESET}"; }
  print_success() { echo -e "  ${C_GREEN}✓ $1${C_RESET}"; }
  print_info() { echo -e "  ${C_CYAN}ℹ $1${C_RESET}"; }
  print_warning() { echo -e "  ${C_YELLOW}⚠ $1${C_RESET}"; }
  print_error() { echo -e "  ${C_RED}✗ $1${C_RESET}"; }
}
define_print_helpers

# Minimum tool versions, kept in k8s-operator/scripts/min_versions.sh so the
# numbers live in exactly one place. This installer is also downloaded and run
# on its own, before any checkout exists, so the source is guarded: in that
# case the workspace step clones the repository and the check runs against the
# clone's copy.
_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || echo "")"
_min_versions="${_script_dir}/k8s-operator/scripts/min_versions.sh"
if [ -r "$_min_versions" ]; then
  # CI runs shellcheck without -x, so the source= hint alone still raises
  # SC1091 for a file it was not handed as input.
  # shellcheck source=k8s-operator/scripts/min_versions.sh disable=SC1091
  source "$_min_versions"
else
  require_min_gcloud_version() { return 0; }
  require_min_terraform_version() { return 0; }
fi
unset _min_versions

validate_immutable_ref() {
  local ref="${1:-}"
  if [ -z "$ref" ]; then
    print_error "An immutable image/source ref is required. Pass --image-tag with a validated release tag or full commit SHA."
    return 1
  fi
  case "$ref" in
    latest|main|master|HEAD)
      print_error "Mutable image/source ref '$ref' is not supported. Use a validated release tag or full commit SHA."
      return 1
      ;;
  esac
  if [[ ! "$ref" =~ ^[0-9a-fA-F]{40}$ ]] \
    && [[ ! "$ref" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
    print_error "Image/source ref must be a full 40-character commit SHA or a pure numeric SemVer release tag (X.Y.Z, e.g. 0.1.0)."
    return 1
  fi
}

# Resolves the shape a run that CREATES a cluster will build, given what the
# caller asked for ($1, may be empty) and the location ($2). Echoes the mode
# and nothing else, so the caller can compare and explain.
#
# A function rather than an inline `:-` because this one line is what a bare
# ./install.sh actually builds, and the inline form was untestable: install.sh
# writes CLUSTER_MODE into vars.sh before the generator ever reads it, so
# installer_common.sh's own fallback never decides anything for this front
# door, and a test of that fallback proves nothing about this.
resolve_creatable_cluster_mode() {
  local requested="${1:-}" location="${2:-}"
  if [ -n "$requested" ]; then
    echo "$requested"
    return 0
  fi
  # A defaulted Autopilot steps aside at a zonal location rather than failing:
  # nobody asked for Autopilot here, and the alternative is an abort blaming
  # --region for a shape the installer chose itself. An explicit
  # --cluster-mode=autopilot still fails in require_creatable_cluster_mode —
  # that request is impossible, not merely inconvenient.
  if [ "${DEFAULT_CLUSTER_MODE}" = "autopilot" ] && ! location_is_region "$location"; then
    echo "standard"
    return 0
  fi
  echo "${DEFAULT_CLUSTER_MODE}"
}

# A cluster shape this install can create in this location. is_valid_cluster_mode
# comes from installer_common.sh, so this runs after the workspace step.
#
# The region rule is the gke-cluster module's Autopilot precondition, checked
# here as well because reaching it costs the whole interview first: a location
# that only turns out to be wrong at terraform validate has already collected
# every API key and integration answer.
require_creatable_cluster_mode() {
  local mode="${1:-}" location="${2:-}"
  if ! is_valid_cluster_mode "$mode"; then
    print_error "--cluster-mode must be either autopilot or standard (got '${mode}')."
    exit 1
  fi
  if [ "$mode" = "autopilot" ] && ! location_is_region "$location"; then
    print_error "GKE Autopilot clusters are regional: --region must be a region such as us-central1, not '${location}'."
    print_info "For a zonal cluster, pass --cluster-mode=standard."
    exit 1
  fi
}

# vars.sh must record the shape the install HAS, not the one the interview
# asked for. The two differ whenever a cluster already existed: the tfvars
# generator probed its live shape and the request had no say. Leaving the
# request on disk is how the value that later rebuilds a deleted cluster —
# and that uninstall.sh and upgrade.sh regenerate from, with no flag to
# correct it — ends up naming the wrong shape.
#
# Runs after write_tfvars_from_state, which is what sets TFVARS_CLUSTER_MODE;
# save_var rewrites the one key rather than the file, so the line the state
# step already wrote is replaced rather than duplicated.
persist_effective_cluster_mode() {
  local requested="${1:-}" effective="${TFVARS_CLUSTER_MODE:-}"
  [ -n "$effective" ] || return 0
  [ "$effective" != "$requested" ] || return 0
  print_info "Cluster '${CLUSTER_NAME:-}' is GKE $(cluster_mode_label "$effective"), not $(cluster_mode_label "$requested"); recording its live shape in vars.sh."
  # DRY_RUN=0 pins save_var's dry-run guard, which tests a variable install.sh
  # never sets (it has PARAM_DRY_RUN, spelled "true", so `DRY_RUN=1` here is a
  # REAL install whose write save_var would skip, and `DRY_RUN=true` is an
  # integer-comparison error). Every other vars.sh key goes through
  # write_state_var, which has no guard; this one is written on the same terms.
  DRY_RUN=0 save_var CLUSTER_MODE "$effective"
}

# How GKE writes the shape. bash 3.2, still macOS's /bin/bash, has no ${var^}.
cluster_mode_label() {
  case "${1:-}" in
    autopilot) echo "Autopilot" ;;
    *) echo "Standard" ;;
  esac
}

# The image tag doubles as the source ref that verify_local_source_ref checks the
# checkout against. When downloaded as an official release via curl | bash, the baked
# release tag takes precedence. In local Git checkouts, an exact SemVer release tag or
# HEAD commit SHA is used as the default.
default_image_tag() {
  local repo_dir="${1:-.}"
  # 1. Baked release version takes precedence (for curl | bash from official release URLs)
  if [ -n "${BAKED_RELEASE_VERSION:-}" ]; then
    echo "$BAKED_RELEASE_VERSION"
    return 0
  fi
  # Only a kube-agents checkout may supply the default. Without this guard,
  # running the curl | bash one-liner from inside any unrelated Git repository
  # would offer that repository's HEAD, which then fails at `git fetch` for a
  # ref the kube-agents clone has never heard of.
  if [ ! -f "${repo_dir}/k8s-operator/scripts/installer_common.sh" ]; then
    return 0
  fi
  # 2. Check if local git repo is checked out at an exact SemVer release tag
  local exact_tag=""
  exact_tag="$(git -C "$repo_dir" describe --tags --exact-match --match="[0-9]*" 2>/dev/null || echo "")"
  if [[ "$exact_tag" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
    echo "$exact_tag"
    return 0
  fi
  # 3. Check if running inside an unpacked release archive directory (e.g. kube-agents-0.1.0 or kube-agents-0.2.0)
  local base_dir=""
  base_dir="$(basename "$(cd "$repo_dir" 2>/dev/null && pwd || echo "$repo_dir")")"
  if [[ "$base_dir" =~ ^kube-agents-([0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?)$ ]]; then
    echo "${BASH_REMATCH[1]}"
    return 0
  fi
  # 4. Fall back to local HEAD commit SHA for developer iterations
  git -C "$repo_dir" rev-parse HEAD 2>/dev/null || echo ""
}

# How that default is shown in a prompt: the full SHA is unreadable, so abbreviate
# it the way git does and say where it came from. Empty outside a Git worktree.
default_image_tag_label() {
  local repo_dir="${1:-.}"
  local tag
  tag="$(default_image_tag "$repo_dir")"
  if [ -z "$tag" ]; then
    return 0
  fi

  if [ -n "${BAKED_RELEASE_VERSION:-}" ] && [ "$tag" = "$BAKED_RELEASE_VERSION" ]; then
    printf 'official release %s' "$tag"
  elif [ "$tag" = "$(git -C "$repo_dir" describe --tags --exact-match --match="[0-9]*" 2>/dev/null || echo "")" ]; then
    printf 'release tag %s' "$tag"
  elif [[ "$(basename "$(cd "$repo_dir" 2>/dev/null && pwd || echo "$repo_dir")")" =~ ^kube-agents-${tag}$ ]]; then
    printf 'release archive %s' "$tag"
  else
    printf 'local HEAD checkout %s' "${tag:0:7}"
  fi
}

json_escape() {
  local value="${1:-}"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '%s' "$value"
}

write_state_var() {
  local destination="$1"
  local var_name="$2"
  local var_value="$3"
  printf 'export %s=%q\n' "$var_name" "$var_value" >> "$destination"
}

# Credentials follow PERSIST_SECRETS_ON_DISK: false keeps them out of
# vars.sh. Exported for this run either way, so the tfvars generator still
# sees them; later runs recover them from the live Secret (see
# write_tfvars_from_state).
write_secret_state_var() {
  local destination="$1"
  local var_name="$2"
  local var_value="$3"
  export "${var_name}=${var_value}"
  if is_truthy "${PERSIST_SECRETS_ON_DISK:-true}"; then
    write_state_var "$destination" "$var_name" "$var_value"
  fi
}

verify_local_source_ref() {
  local repo_dir="$1"
  local expected_ref="$2"
  # The installer runs k8s-operator/scripts/* from this checkout while deploying
  # the container image built from $expected_ref, so a mismatch means the cluster
  # gets manifests from one revision and an agent runtime from another. --dry-run
  # touches nothing, and --allow-unverified-source is the explicit opt-out for
  # developing against locally modified scripts; both downgrade this to a warning.
  local lenient="false"
  local unverified="false"
  if [ "$PARAM_DRY_RUN" = "true" ] || [ "$PARAM_ALLOW_UNVERIFIED_SOURCE" = "true" ]; then
    lenient="true"
  fi
  if [ "$SOURCE_REF_VERIFIED" = "${repo_dir}@${expected_ref}" ]; then
    return 0
  fi

  if ! git -C "$repo_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # In official stamped release archives (unpacked tarball/zip outside Git),
    # BAKED_RELEASE_VERSION is stamped during release automation.
    if [ -n "${BAKED_RELEASE_VERSION:-}" ] && [ "${BAKED_RELEASE_VERSION}" = "${expected_ref}" ]; then
      SOURCE_REF_VERIFIED="${repo_dir}@${expected_ref}"
      print_success "Verified install sources match baked official release ${BAKED_RELEASE_VERSION}."
      return 0
    fi
    if [ "$lenient" = "true" ]; then
      print_warning "Cannot verify source/image alignment because '$repo_dir' is not a Git worktree."
      SOURCE_REF_VERIFIED="${repo_dir}@${expected_ref}"
      return 0
    fi
    print_error "Refusing to provision from an unversioned source directory: $repo_dir"
    print_info "Pass --allow-unverified-source to provision anyway."
    return 1
  fi

  local expected_commit current_commit
  if ! expected_commit="$(git -C "$repo_dir" rev-parse --verify "${expected_ref}^{commit}" 2>/dev/null)"; then
    if [ "$lenient" = "true" ]; then
      print_warning "Cannot verify source/image alignment: ref '$expected_ref' is not present in this checkout."
      SOURCE_REF_VERIFIED="${repo_dir}@${expected_ref}"
      return 0
    fi
    print_error "The requested image/source ref '$expected_ref' is not present in the current checkout. Check out that exact revision first."
    print_info "Pass --allow-unverified-source to provision anyway."
    return 1
  fi
  current_commit="$(git -C "$repo_dir" rev-parse HEAD)"
  if [ "$current_commit" != "$expected_commit" ]; then
    if [ "$lenient" = "true" ]; then
      unverified="true"
      print_warning "Source/image version mismatch: checkout is ${current_commit}, requested ref resolves to ${expected_commit}."
    else
      print_error "Source/image version mismatch: checkout is ${current_commit}, requested ref resolves to ${expected_commit}."
      print_info "Pass --allow-unverified-source to provision anyway."
      return 1
    fi
  fi

  if [ -n "$(git -C "$repo_dir" status --porcelain --untracked-files=no)" ]; then
    if [ "$lenient" = "true" ]; then
      unverified="true"
      print_warning "Provisioning scripts have uncommitted changes; they do not match '$expected_ref'."
    else
      print_error "Refusing to provision from a dirty checkout because its sources do not exactly match '$expected_ref'."
      print_info "Pass --allow-unverified-source to provision anyway, or stash the changes first."
      return 1
    fi
  fi

  SOURCE_REF_VERIFIED="${repo_dir}@${expected_ref}"
  if [ "$unverified" = "true" ]; then
    print_warning "Continuing with unverified install sources: the cluster will get this checkout's configuration plus the image built from ${expected_ref}."
    return 0
  fi
  print_success "Verified install sources and image ref resolve to commit ${expected_commit}."
}

# Put the install sources on disk and return the directory holding them.
# Runs before the interview so a bad source ref or a dirty tree fails immediately,
# and so installer_common.sh — which owns every installer default — can be sourced.
acquire_source_repo() {
  # Stores the directory in the variable named by $1 rather than echoing it: the
  # progress lines below would otherwise be captured along with the path.
  local dest_var="$1"
  local expected_ref="$2"
  local resolved_dir=""
  local script_dir=""
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || echo "")"
  if [ -n "$script_dir" ] && [ -f "${script_dir}/k8s-operator/scripts/installer_common.sh" ]; then
    resolved_dir="$script_dir"
    print_success "Using repository directory: $resolved_dir"
  elif [ -f "k8s-operator/scripts/installer_common.sh" ]; then
    resolved_dir="$(pwd)"
    print_success "Using current repository directory: $resolved_dir"
  else
    resolved_dir="$HOME/kube-agents"
    if [ -d "$resolved_dir" ]; then
      print_info "Using existing repository at $resolved_dir without modifying local changes."
    else
      print_info "Cloning kube-agents install sources at '$expected_ref' into $resolved_dir..."
      git clone --filter=blob:none --no-checkout https://github.com/gke-labs/kube-agents.git "$resolved_dir"
      if [[ "$expected_ref" =~ ^[0-9a-fA-F]{40}$ ]]; then
        git -C "$resolved_dir" fetch --depth=1 https://github.com/gke-labs/kube-agents.git "$expected_ref"
      else
        git -C "$resolved_dir" fetch --depth=1 https://github.com/gke-labs/kube-agents.git "+refs/tags/${expected_ref}:refs/tags/${expected_ref}"
      fi
      git -C "$resolved_dir" checkout --detach FETCH_HEAD
    fi
    cd "$resolved_dir"
  fi
  verify_local_source_ref "$resolved_dir" "$expected_ref"
  printf -v "$dest_var" '%s' "$resolved_dir"
}

# k8s-operator/scripts/installer_common.sh is the source of truth for install
# defaults, validation rules, and the terraform.tfvars generator. The installer
# sources it rather than keeping its own copies, which is how the two drifted
# apart before (an installer menu whose permission-set default disagreed with
# the provisioner's, a us-central1 default against us-east4, a second copy of
# derive_kms_location).
source_provisioning_helpers() {
  local repo_dir="$1"
  local helper_script="${repo_dir}/k8s-operator/scripts/installer_common.sh"
  if [ ! -f "$helper_script" ]; then
    print_error "Cannot find installer helpers at $helper_script."
    exit 1
  fi
  SCRIPT_DIR="${repo_dir}/k8s-operator/scripts"
  VARS_FILE="${SCRIPT_DIR}/vars.sh"
  # shellcheck source=/dev/null
  source "$helper_script"
  # gke_dns_endpoint_flag, for the credentials fetch before the health checks.
  # shellcheck source=/dev/null
  source "${SCRIPT_DIR}/gke_dns_endpoint.sh"
  print_success "Loaded installer defaults from k8s-operator/scripts/installer_common.sh"
}

# Fill in the parameters whose default lives in common.sh. Called once, after
# sourcing, so a flag or environment variable still wins over the shared default.
resolve_shared_defaults() {
  PARAM_MODEL_PROVIDER="${PARAM_MODEL_PROVIDER:-$DEFAULT_MODEL_PROVIDER}"
  PARAM_REGISTRY_PREFIX="${PARAM_REGISTRY_PREFIX:-$DEFAULT_REGISTRY_PREFIX}"
}

# Wait for one deployment to roll out, animating a spinner with the elapsed time
# and kubectl's own latest progress line. Falls back to plain streaming output
# when stdout is not a terminal (CI, piped logs). Returns kubectl's exit status.
wait_for_rollout() {
  local deployment="$1"
  local namespace="$2"
  local timeout_secs="$3"

  if [ ! -t 1 ]; then
    kubectl rollout status "deployment/${deployment}" -n "$namespace" --timeout="${timeout_secs}s"
    return $?
  fi

  local log_file=""
  log_file="$(mktemp -t kube-agents-rollout.XXXXXX)"
  kubectl rollout status "deployment/${deployment}" -n "$namespace" --timeout="${timeout_secs}s" \
    >"$log_file" 2>&1 &
  local kubectl_pid=$!

  local frames=("⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏")
  local frame=0
  local started=$SECONDS
  local status_line=""
  local term_width=0
  term_width="$(get_term_width)"
  # Everything except the kubectl line: two spaces, spinner, name, "(NNNs)",
  # separators. Keep one column spare so the line never wraps — a wrapped line
  # cannot be rewritten with \r and would scroll the spinner down the screen.
  local status_width=$((term_width - ${#deployment} - 15))
  if [ "$status_width" -lt 10 ]; then
    status_width=10
  fi
  tput civis 2>/dev/null || true
  while kill -0 "$kubectl_pid" 2>/dev/null; do
    status_line="$(tail -n 1 "$log_file" 2>/dev/null | tr -d '\r' | cut -c1-"$status_width")"
    printf '\r  %b%s%b %s %b(%ss)%b %-*s' \
      "$C_CYAN" "${frames[$((frame % 10))]}" "$C_RESET" "$deployment" \
      "$C_YELLOW" "$((SECONDS - started))" "$C_RESET" "$status_width" "$status_line"
    frame=$((frame + 1))
    sleep 0.2
  done
  tput cnorm 2>/dev/null || true
  printf '\r%*s\r' "$term_width" ''

  local rc=0
  wait "$kubectl_pid" || rc=$?
  if [ "$rc" -eq 0 ]; then
    print_success "$deployment rolled out in $((SECONDS - started))s"
  else
    tail -n 3 "$log_file" | tr -d '\r' | while IFS= read -r line; do
      [ -n "$line" ] && print_info "$line"
    done
  fi
  rm -f -- "$log_file"
  return "$rc"
}

# Wait for a Deployment object to exist, ahead of waiting for it to roll out.
# `kubectl rollout status` on a Deployment that is not there yet fails
# immediately rather than waiting, and the operator writes the agent's after the
# apply returns — later still when it has a RuntimeClass to resolve first. This
# is the difference between "the operator has not got to it" and "the operator
# refuses to create it", which is worth the wait to tell apart.
wait_for_deployment_object() {
  local deployment="$1"
  local namespace="$2"
  local timeout_secs="$3"

  local deadline=$((SECONDS + timeout_secs))
  while ! kubectl get deployment "$deployment" -n "$namespace" >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      return 1
    fi
    sleep "$DEPLOYMENT_POLL_INTERVAL_SECS"
  done
  return 0
}

has_controlling_tty() {
  [ -c /dev/tty ] && ( : </dev/tty ) 2>/dev/null
}

# Safe prompt helper: supports non-interactive mode and /dev/tty fallback
prompt_read() {
  local prompt_text="$1"
  local var_name="$2"
  local default_val="${3:-}"
  local secret_mode="${4:-false}"
  # What "[default: …]" shows, when the stored value reads badly (a 40-character
  # SHA) or does not read at all (an empty list) but must still be what an empty
  # answer selects. Supplying a label also makes the hint appear for an empty default.
  local default_label="${5:-}"

  # Non-interactive mode override
  if [ "$PARAM_NON_INTERACTIVE" = "true" ] || ! has_controlling_tty; then
    local current_val="${!var_name:-}"
    if [ -n "$current_val" ]; then
      printf -v "$var_name" '%s' "$current_val"
    else
      printf -v "$var_name" '%s' "$default_val"
    fi
    if [ "$secret_mode" = "true" ]; then
      print_info "Auto-selected ($var_name): [REDACTED]"
    else
      print_info "Auto-selected ($var_name): ${!var_name}"
    fi
    return 0
  fi

  if [ -n "$default_val" ] || [ -n "$default_label" ]; then
    prompt_text="$prompt_text [default: ${C_BOLD}${default_label:-$default_val}${C_RESET}]: "
  else
    prompt_text="$prompt_text: "
  fi

  local input_val=""
  echo -ne "${C_CYAN}${prompt_text}${C_RESET}" >/dev/tty
  if [ "$secret_mode" = "true" ]; then
    read -r -s input_val </dev/tty
    echo "" >/dev/tty
  else
    read -r input_val </dev/tty
  fi

  if [ -z "$input_val" ] && [ -n "$default_val" ]; then
    printf -v "$var_name" '%s' "$default_val"
  else
    printf -v "$var_name" '%s' "$input_val"
  fi
}

prompt_menu() {
  local prompt_text="$1"
  shift
  local options=("$@")
  local var_name="${options[${#options[@]}-1]}"
  unset 'options[${#options[@]}-1]'

  if [ "$PARAM_NON_INTERACTIVE" = "true" ]; then
    local current_choice="${!var_name:-1}"
    printf -v "$var_name" '%s' "$current_choice"
    print_info "Auto-selected option ($var_name): $current_choice"
    return 0
  fi

  if has_controlling_tty; then
    echo -e "\n${C_BOLD}$prompt_text${C_RESET}" >/dev/tty
    for i in "${!options[@]}"; do
      echo -e "  ${C_YELLOW}$((i+1)))${C_RESET} ${options[$i]}" >/dev/tty
    done
  else
    echo -e "\n${C_BOLD}$prompt_text${C_RESET}"
    for i in "${!options[@]}"; do
      echo -e "  ${C_YELLOW}$((i+1)))${C_RESET} ${options[$i]}"
    done
  fi

  local choice=""
  while true; do
    prompt_read "Select an option (1-${#options[@]})" choice "1"
    if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#options[@]}" ]; then
      printf -v "$var_name" '%s' "$choice"
      break
    else
      print_error "Invalid selection. Please enter a number between 1 and ${#options[@]}." >/dev/tty
    fi
  done
}

# How long each deployment gets to report ready in the post-install health check.
ROLLOUT_TIMEOUT_SECS=300

# How long each deployment gets to exist at all before that check calls it
# missing. The operator creates the agent Deployment asynchronously and, when a
# RuntimeClass is asked for, only after that RuntimeClass resolves — retrying on
# a 30s requeue (validateRuntimeClass in
# k8s-operator/internal/controller/platformagent_controller.go). Three requeues
# is the budget: below one, "not yet" and "never" are indistinguishable.
DEPLOYMENT_APPEAR_TIMEOUT_SECS=90
DEPLOYMENT_POLL_INTERVAL_SECS=5

# Number of projects listed in the interactive project picker. Accounts with
# more projects than this can still type an ID that the list does not show.
PROJECT_LIST_LIMIT=5

# GCP project IDs are 6-30 characters, start with a lowercase letter, and hold
# only lowercase letters, digits, and hyphens. A valid ID is never all digits,
# so a numeric answer is unambiguously a menu index.
is_valid_project_id() {
  local id="${1:-}"
  # Legacy domain-scoped IDs ("example.com:my-project") keep the same rules on
  # each side of the colon.
  if [[ "$id" == *:* ]]; then
    [[ "${id%%:*}" =~ ^[a-z0-9][a-z0-9.-]*[a-z0-9]$ ]] || return 1
    id="${id#*:}"
  fi
  [[ "$id" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]
}

# Interactive GCP project picker. Accepts either a menu number or a project ID
# typed in full, so an account whose project is missing from the truncated list
# is not stuck. Stores the result in the variable named by $1.
select_gcp_project() {
  local dest_var="$1"
  local current_proj="${2:-}"
  local listed=""
  local ids=()
  local labels=()
  local p_id="" p_name="" idx=0

  print_info "Fetching available GCP projects from your account..."
  listed=$(gcloud projects list --sort-by=projectId \
    --format="value(projectId,name)" --limit="$PROJECT_LIST_LIMIT" 2>/dev/null || echo "")

  # The active project leads the menu even when it falls outside the listing.
  if [ -n "$current_proj" ]; then
    ids+=("$current_proj")
    labels+=("$current_proj ${C_GREEN}[active]${C_RESET}")
  fi
  while IFS=$'\t' read -r p_id p_name; do
    if [ -n "$p_id" ] && [ "$p_id" != "$current_proj" ]; then
      ids+=("$p_id")
      if [ -n "$p_name" ] && [ "$p_name" != "$p_id" ]; then
        labels+=("$p_id ($p_name)")
      else
        labels+=("$p_id")
      fi
    fi
  done <<< "$listed"

  if [ "${#ids[@]}" -eq 0 ]; then
    prompt_read "Target GCP Project ID" "$dest_var" "$current_proj"
    return 0
  fi

  local sink="/dev/stdout"
  if has_controlling_tty; then
    sink="/dev/tty"
  fi
  {
    echo -e "\n${C_BOLD}Select target GCP Project:${C_RESET}"
    for idx in "${!labels[@]}"; do
      echo -e "  ${C_YELLOW}$((idx+1)))${C_RESET} ${labels[$idx]}"
    done
    if [ "$(printf '%s\n' "$listed" | grep -c '[^[:space:]]')" -ge "$PROJECT_LIST_LIMIT" ]; then
      echo -e "  ${C_CYAN}ℹ Showing the first ${PROJECT_LIST_LIMIT} projects — type a project ID to use one that is not listed.${C_RESET}"
    fi
  } > "$sink"

  local answer=""
  while true; do
    prompt_read "Select a number, or type a GCP Project ID" answer "${ids[0]}"
    if [[ "$answer" =~ ^[0-9]+$ ]]; then
      if [ "$answer" -ge 1 ] && [ "$answer" -le "${#ids[@]}" ]; then
        printf -v "$dest_var" '%s' "${ids[$((answer-1))]}"
        return 0
      fi
      print_error "Invalid selection. Enter a number between 1 and ${#ids[@]}, or type a project ID."
    elif is_valid_project_id "$answer"; then
      printf -v "$dest_var" '%s' "$answer"
      return 0
    else
      print_error "'$answer' is neither a menu number nor a valid GCP project ID (6-30 characters: lowercase letters, digits, hyphens)."
    fi
  done
}

# Auto-install missing CLI tool if possible
auto_install_tool() {
  local tool="$1"
  print_warning "Missing required CLI tool: $tool"

  if [ "$PARAM_DRY_RUN" = "true" ]; then
    print_error "Dry-run validation will not install missing tools. Install '$tool' and retry."
    exit 1
  fi

  if [ "$PARAM_NON_INTERACTIVE" = "true" ]; then
    print_info "Non-interactive mode: Auto-installing $tool..."
    local install_choice="y"
  else
    local install_choice=""
    prompt_read "Attempt automatic installation of '$tool'? (y/N)" install_choice "y"
  fi

  if [[ "$install_choice" =~ ^[Yy]$ ]]; then
    if command -v brew >/dev/null 2>&1; then
      print_info "Installing $tool via Homebrew..."
      if [ "$tool" = "terraform" ]; then
        # homebrew-core disabled the terraform formula after the licence
        # change; HashiCorp's tap is the supported source.
        brew install hashicorp/tap/terraform || true
      else
        brew install "$tool" || true
      fi
    elif command -v apt-get >/dev/null 2>&1; then
      print_info "Installing $tool via apt..."
      if [ "$tool" = "terraform" ]; then
        # Stock apt has no terraform package; add HashiCorp's repository the
        # way their docs prescribe.
        type -p curl >/dev/null || sudo apt-get install curl -y
        type -p gpg >/dev/null || sudo apt-get install gnupg -y
        curl -fsSL https://apt.releases.hashicorp.com/gpg | sudo gpg --yes --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
        # shellcheck disable=SC1091  # /etc/os-release exists on every apt host; shellcheck cannot follow it
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(. /etc/os-release && echo "$VERSION_CODENAME") main" | sudo tee /etc/apt/sources.list.d/hashicorp.list > /dev/null
        sudo apt-get update >/dev/null 2>&1 || true
        sudo apt-get install terraform -y || true
      elif [ "$tool" = "gh" ]; then
        type -p curl >/dev/null || sudo apt-get install curl -y
        curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
        sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
        sudo apt-get install gh -y || true
      else
        sudo apt-get update >/dev/null 2>&1 || true
        sudo apt-get install -y "$tool" || true
      fi
    else
      print_error "Could not auto-install $tool. Package manager not recognized."
    fi
  fi

  if command -v "$tool" >/dev/null 2>&1; then
    print_success "CLI tool '$tool' installed successfully!"
  else
    print_error "Tool '$tool' is still missing. Please install $tool manually."
    exit 1
  fi
}

# Generate Machine-Readable JSON Report for AI Agents
write_json_report() {
  local status="$1"
  local report_file="/tmp/kube-agents-install-report.json"
  local timestamp
  timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "2026-08-05T00:00:00Z")

  local report_gitops_repo=""
  if [ -n "${github_org:-}" ] && [ -n "${github_repo:-}" ]; then
    report_gitops_repo="https://github.com/${github_org}/${github_repo}"
  fi

  cat << EOF > "$report_file"
{
  "status": "$(json_escape "$status")",
  "dry_run": ${PARAM_DRY_RUN},
  "non_interactive": ${PARAM_NON_INTERACTIVE},
  "project_id": "$(json_escape "${project_id:-}")",
  "project_number": "$(json_escape "${project_number:-}")",
  "cluster_name": "$(json_escape "${cluster_name:-}")",
  "cluster_mode": "$(json_escape "${TFVARS_CLUSTER_MODE:-${cluster_mode:-}}")",
  "region": "$(json_escape "${region:-}")",
  "model_provider": "$(json_escape "${model_provider:-}")",
  "permission_set": "$(json_escape "${permission_set:-}")",
  "gvisor_enabled": ${enable_gvisor:-false},
  "memory_mode": "$(json_escape "${memory_mode:-file}")",
  "gitops_repo": "$(json_escape "$report_gitops_repo")",
  "vars_file": "$(json_escape "${vars_file:-}")",
  "timestamp": "$(json_escape "$timestamp")"
}
EOF
  print_success "Machine-readable report written to: ${C_BOLD}${report_file}${C_RESET}"
}

# ─── Terraform Engine ─────────────────────────────────────────────────────────
# The install engine is terraform/examples/full-install driven through its
# lifecycle.sh (which adopts undeletable KMS resources before every apply).
# State lives in a GCS bucket derived from the install coordinates — see
# installer_common.sh's tf_state_bucket/tf_state_prefix — so uninstall.sh and
# upgrade.sh can find it from a fresh clone.
tf_compose_dir() {
  echo "${1}/terraform/examples/full-install"
}

# Runs lifecycle.sh apply against the generated terraform.tfvars. Reads the
# install coordinates from the environment (source vars.sh first).
run_lifecycle_apply() {
  local repo_dir="$1"
  local log_file="$2"
  (
    cd "$(tf_compose_dir "$repo_dir")"
    export KUBE_AGENTS_STATE_BUCKET="${KUBE_AGENTS_STATE_BUCKET:-auto}"
    export KUBE_AGENTS_STATE_PREFIX
    KUBE_AGENTS_STATE_PREFIX="$(tf_state_prefix)"
    ./lifecycle.sh apply -auto-approve -input=false
  ) 2>&1 | tee "$log_file"
}

# CMEK on a pre-existing cluster is the one create-path behaviour Terraform
# cannot express: a data source cannot mutate the cluster it reads. Ensures
# the keyring/key and the GKE service agent's binding, then updates the
# cluster, and skips clusters that are already encrypted, do
# not exist yet (Terraform creates those encrypted), or where the operator
# explicitly allowed unencrypted secrets.
ensure_existing_cluster_cmek() {
  local project_id="$1" cluster_name="$2" region="$3"
  local enc_state
  enc_state=$(gcloud container clusters describe "$cluster_name" \
    --location="$region" --project="$project_id" \
    --format="value(databaseEncryption.state)" 2>/dev/null || echo "")
  [ -n "$enc_state" ] || return 0
  if is_valid_cmek_encryption_state "$enc_state"; then
    print_success "Existing cluster '$cluster_name' already has CMEK database encryption ($enc_state)."
    return 0
  fi
  if is_truthy "${ALLOW_UNENCRYPTED_SECRETS:-false}"; then
    print_warning "Existing cluster '$cluster_name' has no CMEK encryption ('$enc_state'), but ALLOW_UNENCRYPTED_SECRETS=true is set. Skipping."
    return 0
  fi

  local kms_location keyring="${GKE_DB_KMS_KEYRING:-platform-agent-keyring}" key="${GKE_DB_KMS_KEY:-k8s-secret-encryption-key}"
  kms_location="$(derive_kms_location "$region")"
  local key_resource="projects/${project_id}/locations/${kms_location}/keyRings/${keyring}/cryptoKeys/${key}"
  print_info "Enabling CMEK database encryption on existing cluster '$cluster_name' (key: $key_resource)..."
  gcloud services enable cloudkms.googleapis.com --project="$project_id"
  gcloud kms keyrings create "$keyring" --location="$kms_location" --project="$project_id" 2>/dev/null || true
  gcloud kms keys create "$key" --keyring="$keyring" --location="$kms_location" \
    --purpose="encryption" --project="$project_id" 2>/dev/null || true
  local project_number service_agent
  project_number=$(gcloud projects describe "$project_id" --format="value(projectNumber)")
  service_agent="service-${project_number}@container-engine-robot.iam.gserviceaccount.com"
  gcloud beta services identity create --service=container.googleapis.com --project="$project_id" 2>/dev/null || true
  gcloud kms keys add-iam-policy-binding "$key" --keyring="$keyring" --location="$kms_location" \
    --member="serviceAccount:${service_agent}" \
    --role="roles/cloudkms.cryptoKeyEncrypterDecrypter" --project="$project_id" --quiet >/dev/null
  print_info "Updating the live cluster control plane; this can take several minutes..."
  gcloud container clusters update "$cluster_name" --location "$region" \
    --database-encryption-key="$key_resource" --project "$project_id" --quiet
}

# Workload Identity on a pre-existing cluster is the other such behaviour:
# kube-agents requires the pool (every KSA→GSA binding rides it — without it
# the pods silently run as the node's service account), and the module's
# data source can only read it, so it is enabled here. No-op when the
# cluster does not exist yet:
# Terraform creates those with the pool on. The gke-cluster module's
# postcondition backstops installs driven through bare Terraform.
ensure_existing_cluster_workload_identity() {
  local project_id="$1" cluster_name="$2" region="$3"
  local pool is_autopilot

  is_autopilot=$(trap - ERR; gcloud container clusters describe "$cluster_name" \
    --location="$region" --project="$project_id" \
    --format="value(autopilot.enabled)" 2>/dev/null) || is_autopilot="false"
  if [ "$is_autopilot" = "True" ]; then
    print_success "Existing cluster '$cluster_name' is GKE Autopilot (Workload Identity enabled natively)."
    return 0
  fi

  # `trap - ERR` inside the substitution: bash 3.2 (macOS's default, the
  # curl|bash audience) runs the inherited ERR trap in the subshell even
  # though the outer failure is handled, printing a spurious abort banner
  # and writing a FAILED report mid-run.
  pool=$(trap - ERR; gcloud container clusters describe "$cluster_name" \
    --location="$region" --project="$project_id" \
    --format="value(workloadIdentityConfig.workloadPool)" 2>/dev/null) || return 0
  if [ "$pool" = "${project_id}.svc.id.goog" ]; then
    print_success "Existing cluster '$cluster_name' already has Workload Identity ($pool)."
  else
    print_info "Enabling the Workload Identity pool on existing cluster '$cluster_name'..."
    print_info "Updating the live cluster control plane; this can take several minutes..."
    gcloud container clusters update "$cluster_name" --location "$region" \
      --project "$project_id" --workload-pool="${project_id}.svc.id.goog" --quiet
  fi

  # Enabling the pool does not migrate node pools off the legacy GCE metadata
  # server, and pods on such pools still get the node's service account.
  # Standard-cluster concern: Autopilot pools are managed onto GKE_METADATA
  # already.
  local legacy_pool
  while IFS= read -r legacy_pool; do
    [ -n "$legacy_pool" ] || continue
    print_warning "Node pool '${legacy_pool}' uses the legacy GCE metadata server; migrating to GKE_METADATA (this recreates the pool's nodes)..."
    gcloud container node-pools update "$legacy_pool" \
      --cluster="$cluster_name" --location="$region" --project="$project_id" \
      --workload-metadata=GKE_METADATA --quiet
  done < <(trap - ERR; gcloud container node-pools list --cluster="$cluster_name" \
      --location="$region" --project="$project_id" \
      --format="csv[no-heading](name,config.workloadMetadataConfig.mode)" 2>/dev/null \
    | awk -F',' '$2 != "GKE_METADATA" {print $1}' || true)
}

# NetworkPolicy enforcement on a pre-existing cluster is the third such
# behaviour: every NetworkPolicy this install ships — LiteLLM's, the
# minter's, Hindsight's, and the ones the operator generates around the
# agent — is accepted and silently inert on a cluster with neither Dataplane
# V2 nor the legacy Calico addon, which is GKE Standard's default shape.
# Terraform-created clusters always have Dataplane V2; adopted ones get the
# legacy addon enabled here. The gke-cluster module's postcondition backstops
# bare-Terraform installs.
ensure_existing_cluster_network_policy() {
  local project_id="$1" cluster_name="$2" region="$3"
  local dp_provider
  # trap - ERR: same bash-3.2 subshell-trap suppression as the Workload
  # Identity probe above.
  dp_provider=$(trap - ERR; gcloud container clusters describe "$cluster_name" \
    --location="$region" --project="$project_id" \
    --format="value(networkConfig.datapathProvider)" 2>/dev/null) || return 0
  if [ "$dp_provider" = "ADVANCED_DATAPATH" ]; then
    print_success "Existing cluster '$cluster_name' runs Dataplane V2; NetworkPolicy enforcement is built in."
    return 0
  fi
  local legacy_np
  legacy_np=$(trap - ERR; gcloud container clusters describe "$cluster_name" \
    --location="$region" --project="$project_id" \
    --format="value(networkPolicy.enabled)" 2>/dev/null || echo "")
  if [ "$legacy_np" = "True" ] || [ "$legacy_np" = "true" ]; then
    print_success "Existing cluster '$cluster_name' already enforces NetworkPolicy (legacy Calico addon)."
    return 0
  fi
  # Two calls, in this order. GKE rejects --enable-network-policy with "The
  # network policy addon must be enabled before updating the nodes" (HTTP 400)
  # until the Calico addon is on the control plane, and gcloud puts
  # --update-addons and --enable-network-policy in the same "exactly one of
  # these must be specified" argparse group, so they cannot be combined into a
  # single invocation.
  #
  # Unconditional, matching Google's documented procedure. Gating it on
  # addonsConfig.networkPolicyConfig.disabled looks tempting for the re-run
  # case — the guard above reads networkPolicy.enabled, so a re-run after the
  # enforcement call failed arrives here with the addon already on — but that
  # field cannot express it: GKE omits false booleans from addonsConfig, so
  # "off" prints True and "on" prints nothing, which is also what a failed
  # describe prints. A gate that skips on empty reintroduces the 400 the
  # moment describe fails. Re-enabling an already-enabled addon is a no-op.
  print_info "Enabling the NetworkPolicy addon on existing cluster '$cluster_name'..."
  gcloud container clusters update "$cluster_name" --location "$region" \
    --update-addons=NetworkPolicy=ENABLED --project "$project_id" --quiet
  print_info "Enabling NetworkPolicy enforcement on existing cluster '$cluster_name' (node pools may be recreated; this can take several minutes)..."
  gcloud container clusters update "$cluster_name" --location "$region" \
    --enable-network-policy --project "$project_id" --quiet
  local active_op
  active_op=$(gcloud container operations list --location="$region" --project="$project_id" \
    --filter="targetLink:$cluster_name AND status=RUNNING" --format="value(name)" 2>/dev/null | head -n1)
  if [ -n "$active_op" ]; then
    print_info "Waiting for operation $active_op to complete..."
    gcloud container operations wait "$active_op" --location="$region" --project="$project_id" ||
      print_warning "Operation wait returned non-zero (it may have finished between list and wait); proceeding..."
  fi
  print_warning "Legacy Network Policy enabled. FQDN-based NetworkPolicies stay unsupported without Dataplane V2."
}

# Neither google provider has a field for --managed-otel-scope, so it is set
# out-of-band after the apply. Best-effort by design: on a gcloud where the
# update surface lacks the flag, the install is
# still complete — only managed OpenTelemetry collection needs a manual step.
apply_managed_otel_scope() {
  local project_id="$1" cluster_name="$2" region="$3"
  if gcloud container clusters update "$cluster_name" --location "$region" --project "$project_id" \
    --managed-otel-scope=COLLECTION_AND_INSTRUMENTATION_COMPONENTS --quiet >/dev/null 2>&1; then
    print_success "Managed OpenTelemetry scope set on '$cluster_name'."
  else
    print_warning "Could not set --managed-otel-scope on '$cluster_name' (create-only on this gcloud?)."
    print_info "Set it manually if you want managed OTel collection: gcloud container clusters update $cluster_name --location $region --managed-otel-scope=COLLECTION_AND_INSTRUMENTATION_COMPONENTS"
  fi
}

# One-shot import of the GitHub App private key into the minter's KMS signing
# key, via the Minty CLI. The PEM never enters Terraform state — that is why
# this is not a Terraform resource. Skipped when a key version is already
# ENABLED (the import happened on an earlier run) and downgraded to printed
# instructions when Go is unavailable.
import_github_pem() {
  local project_id="$1" region="$2"
  [ -n "${GITHUB_ORG:-}" ] && [ -n "${GITHUB_REPO:-}" ] && [ -n "${GITHUB_APP_ID:-}" ] || return 0
  local pem_path="${GITHUB_PEM_PATH:-}"
  local kms_location keyring="${KMS_KEYRING:-github-token-minter-keyring}" key="${KMS_KEY:-github-token-minter-key}"
  kms_location="$(derive_kms_location "$region")"

  local enabled_version
  enabled_version=$(gcloud kms keys versions list --key "$key" --keyring "$keyring" \
    --location "$kms_location" --project "$project_id" \
    --filter='state=ENABLED' --format='value(name.basename())' 2>/dev/null | head -1 || echo "")
  if [ -n "$enabled_version" ]; then
    print_success "GitHub minter KMS key already has an ENABLED version ($enabled_version); skipping PEM import."
    return 0
  fi

  # Clone the tag and run the CLI from the tree:
  # `go run github.com/abcxyz/github-token-minter/cmd/minty@v2.7.1`
  # cannot work: the upstream go.mod declares the module without the /v2 suffix
  # its v2 tags require, so Go rejects the version with or without /v2 in the
  # path. The gcloud-only recovery recipe lives in
  # k8s-operator/config/integrations/github/README.md.
  local import_cmd="git clone --depth 1 --branch v2.7.1 https://github.com/abcxyz/github-token-minter.git /tmp/minty && cd /tmp/minty && go run ./cmd/minty tools import-pk -project-id=${project_id} -location=${kms_location} -key-ring=${keyring} -key=${key} -private-key=@<path-to-pem>"
  if [ -z "$pem_path" ] || [ ! -f "$pem_path" ]; then
    print_warning "No GitHub App private key PEM available (GITHUB_PEM_PATH='${pem_path}')."
    print_info "The minter deployment stays unready until the key is imported: ${import_cmd}"
    return 0
  fi
  if ! command -v go >/dev/null 2>&1; then
    print_warning "Go is not installed, so the App key cannot be imported automatically."
    print_info "Import it manually: ${import_cmd/<path-to-pem>/$pem_path}"
    print_info "Without Go, the gcloud-only import recipe is in k8s-operator/config/integrations/github/README.md."
    return 0
  fi
  # The ring and key normally come from Terraform, but this import runs
  # BEFORE the apply — the minter Deployment cannot pass readiness without an
  # imported key, and the composition's helm release waits on every
  # Deployment, so importing after the apply would wedge it. Ensure they
  # exist first, matching terraform/modules/github-minter exactly; adopt-kms
  # imports them into state at apply time, the same way it re-adopts them
  # after a destroy.
  print_info "Ensuring the minter's KMS keyring and import-only signing key exist..."
  gcloud services enable cloudkms.googleapis.com --project="$project_id"

  # Both creates keep their errors instead of discarding them. Re-running the
  # installer is the common case and "already exists" is the expected answer to
  # it, so the output is only surfaced when the resource is missing afterwards —
  # which is the check that actually matters. Discarding stderr outright is what
  # hid the bug below; tolerating one specific error would still have hidden a
  # permission denial, a disabled API or a quota refusal, all of which end the
  # same way: no key, and an import that fails against something that is not there.
  #
  # `trap - ERR` inside each substitution, for the reason spelled out at the
  # Workload Identity probe above: bash 3.2 runs the inherited ERR trap in the
  # subshell even though `|| true` handles the failure, so a re-run — where
  # "already exists" is the expected answer — would print two fatal-looking
  # abort banners and leave a FAILED install report behind mid-run.
  local kms_ring_err="" kms_key_err=""
  kms_ring_err="$(trap - ERR; gcloud kms keyrings create "$keyring" --location="$kms_location" \
    --project="$project_id" 2>&1)" || true

  # --skip-initial-version-creation is required, not optional: KMS answers
  # `INVALID_ARGUMENT: Import-only keys must skip initial version creation` without
  # it. It matches skip_initial_version_creation in terraform/modules/github-minter,
  # which is where the key normally comes from.
  kms_key_err="$(trap - ERR; gcloud kms keys create "$key" --keyring="$keyring" --location="$kms_location" \
    --purpose=asymmetric-signing --default-algorithm=rsa-sign-pkcs1-2048-sha256 \
    --import-only --skip-initial-version-creation \
    --protection-level=software --project="$project_id" 2>&1)" || true

  # The assertion, not the create, is what makes a failure visible. Whatever went
  # wrong above, the import cannot work without this key, and saying so here names
  # the cause instead of leaving a confusing failure two steps later.
  if ! gcloud kms keys describe "$key" --keyring="$keyring" --location="$kms_location" \
    --project="$project_id" >/dev/null 2>&1; then
    # Deliberately says "could not be confirmed" rather than "does not exist":
    # describe also fails on an IAM denial for cloudkms.cryptoKeys.get or an API
    # blip, and asserting absence from that would be stating more than was
    # established. Whatever the cause, the import cannot safely proceed.
    print_warning "The minter's KMS signing key ${kms_location}/${keyring}/${key} could not be confirmed to exist."
    [ -n "$kms_ring_err" ] && print_info "Keyring create said: ${kms_ring_err}"
    [ -n "$kms_key_err" ] && print_info "Key create said: ${kms_key_err}"
    print_info "The PEM import needs the keyring and the key, so it is being skipped; the minter deployment stays unready until both exist."
    # Not the README's import recipe: that one presupposes the key and only covers
    # loading a PEM into it. What failed here is the creation, so print the two
    # commands that create it. --skip-initial-version-creation is the one that is
    # easy to lose and the one KMS refuses an import-only key without.
    print_info "Create them by hand with:"
    print_info "  gcloud kms keyrings create ${keyring} --location=${kms_location} --project=${project_id}"
    print_info "  gcloud kms keys create ${key} --keyring=${keyring} --location=${kms_location} --purpose=asymmetric-signing --default-algorithm=rsa-sign-pkcs1-2048-sha256 --import-only --skip-initial-version-creation --protection-level=software --project=${project_id}"
    print_info "Then import the PEM with the recipe in k8s-operator/config/integrations/github/README.md."
    return 0
  fi

  print_info "Importing the GitHub App private key into KMS via the Minty CLI..."
  local minty_dir pem_abs
  minty_dir="$(mktemp -d "${TMPDIR:-/tmp}/minty-XXXXXX")"
  pem_abs="$(realpath "$pem_path" 2>/dev/null || echo "$pem_path")"
  if git clone --quiet --depth 1 --branch v2.7.1 \
      https://github.com/abcxyz/github-token-minter.git "$minty_dir" &&
    (cd "$minty_dir" && retry 6 5 go run ./cmd/minty tools import-pk \
      -project-id="$project_id" -location="$kms_location" -key-ring="$keyring" -key="$key" \
      -private-key=@"$pem_abs"); then
    print_success "GitHub App private key imported into ${keyring}/${key}."
  else
    print_warning "PEM import failed; the minter deployment stays unready until it succeeds."
    print_info "Retry manually: ${import_cmd/<path-to-pem>/$pem_path}"
    print_info "If Go itself is the problem (killed compiler, no toolchain), the gcloud-only recipe is in k8s-operator/config/integrations/github/README.md."
  fi
  rm -rf "$minty_dir"
}

# ─── Day-2 Control Panel Menu System (raspi-config style) ──────────────────────
run_menu_system() {
  # The control panel is inherently interactive: without a terminal its menu
  # loop would auto-select the first option forever instead of ever exiting.
  if [ "$PARAM_NON_INTERACTIVE" = "true" ] || ! has_controlling_tty; then
    print_error "The Day-2 control panel requires an interactive terminal."
    print_info "Re-run './install.sh --menu' from a TTY, without -y/--non-interactive."
    exit 1
  fi

  local repo_dir
  repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local vars_file="${repo_dir}/k8s-operator/scripts/vars.sh"
  local helper_script="${repo_dir}/k8s-operator/scripts/installer_common.sh"

  if [ ! -f "$helper_script" ]; then
    print_error "Cannot find installer helpers at $helper_script."
    exit 1
  fi
  export VARS_FILE="$vars_file"
  # shellcheck disable=SC1090
  source "$helper_script"

  if [ -f "$vars_file" ]; then
    # shellcheck disable=SC1090
    if ! source "$vars_file"; then
      print_error "Configuration state is invalid and could not be loaded: $vars_file"
      exit 1
    fi
  fi

  local project_id="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo "")}"
  local project_number="${PROJECT_NUMBER:-}"
  local cluster_name="${CLUSTER_NAME:-$DEFAULT_CLUSTER_NAME}"
  local region="${REGION:-$DEFAULT_REGION}"
  local model_provider="${MODEL_PROVIDER:-$DEFAULT_MODEL_PROVIDER}"
  local model_default_name="${MODEL_DEFAULT_NAME:-$(default_model_for_provider "${MODEL_PROVIDER:-$DEFAULT_MODEL_PROVIDER}")}"
  local vertex_project_id="${VERTEX_PROJECT_ID:-$project_id}"
  local vertex_location="${VERTEX_LOCATION:-$DEFAULT_VERTEX_LOCATION}"
  local gemini_api_key="${GEMINI_API_KEY:-}"
  local openai_api_key="${OPENAI_API_KEY:-}"
  local anthropic_api_key="${ANTHROPIC_API_KEY:-}"
  local google_chat_enabled="${GOOGLE_CHAT_ENABLED:-false}"
  local slack_enabled="${SLACK_ENABLED:-false}"
  local allowed_users="${ALLOWED_USERS:-}"
  local chat_topic_name="${CHAT_TOPIC_NAME:-platform-agent-chat-events}"
  local chat_sub_name="${CHAT_SUB_NAME:-platform-agent-chat-events-sub}"
  local permission_set="${PLATFORM_AGENT_PERMISSION_SET:-read-only}"
  local custom_roles="${PLATFORM_AGENT_CUSTOM_ROLES:-}"
  # Not the fresh-install default. The control panel describes an install that
  # already exists and its Save & Apply re-applies what it displays, so a
  # vars.sh with no ENABLE_GVISOR has to read as the standard runtime — that is
  # what such a cluster is actually running. Defaulting on here would show
  # "gVisor Sandbox" for an unsandboxed install and then provision a node pool
  # nobody asked for on the next apply.
  local enable_gvisor="${ENABLE_GVISOR:-false}"
  local enable_webui="${HERMES_DASHBOARD_ENABLED:-false}"
  local github_org="${GITHUB_ORG:-}"
  local github_repo="${GITHUB_REPO:-gke-fleet-iac}"
  local github_app_id="${GITHUB_APP_ID:-}"
  local kms_keyring="${KMS_KEYRING:-}"
  local kms_key="${KMS_KEY:-}"
  local github_pem_path="${GITHUB_PEM_PATH:-}"
  local image_tag="${IMAGE_TAG:-}"

  while true; do
    echo -e "\n${C_CYAN}${C_BOLD}"
    draw_separator
    echo "🛠️  Kubernetes Agentic Harness (kube-agents) Day-2 Control Panel"
    draw_separator
    echo -e "${C_RESET}"
    echo -e "${C_BOLD}Active Configuration State:${C_RESET}"
    echo -e "  • ${C_CYAN}GCP Project ID:${C_RESET} ${project_id:-Not Set}"
    echo -e "  • ${C_CYAN}GKE Cluster:${C_RESET} ${cluster_name:-Not Set} (${region:-$DEFAULT_REGION})"
    echo -e "  • ${C_CYAN}Hermes Web UI (Port 9119):${C_RESET} $([ "$enable_webui" = "true" ] && echo -e "${C_GREEN}ENABLED${C_RESET}" || echo -e "${C_YELLOW}DISABLED${C_RESET}")"
    echo -e "  • ${C_CYAN}Chat Integrations:${C_RESET} Google Chat: $([ "$google_chat_enabled" = "true" ] && echo -e "${C_GREEN}ON${C_RESET}" || echo "OFF"), Slack: $([ "$slack_enabled" = "true" ] && echo -e "${C_GREEN}ON${C_RESET}" || echo "OFF")"
    echo -e "  • ${C_CYAN}AI Model Provider:${C_RESET} ${model_provider} (${model_default_name})$([ "$model_provider" = "vertex_ai" ] && echo " @ ${vertex_project_id}/${vertex_location}" || echo "")"
    echo -e "  • ${C_CYAN}Permission Boundary:${C_RESET} ${permission_set}"
    echo -e "  • ${C_CYAN}Runtime Isolation:${C_RESET} $([ "$enable_gvisor" = "true" ] && echo -e "${C_GREEN}gVisor Sandbox${C_RESET}" || echo "Standard")"

    local menu_choice=""
    prompt_menu "Select configuration task:" \
      "🌐 Toggle Hermes Web UI (Port 9119 Dashboard)" \
      "💬 Manage Chat & Messaging Integrations (Google Chat / Slack)" \
      "🔑 Manage AI Model Provider & Credentials (Gemini / Vertex / OpenAI)" \
      "🛡️ Modify Security & Permission Boundaries (gVisor / SRE vs Read-Only)" \
      "🗄️ Manage GitOps Repository & GitHub Auth (gke-fleet-iac)" \
      "🚀 Save & Apply Configuration Changes (~15s update)" \
      "🚪 Exit Control Panel" \
      menu_choice

    case "$menu_choice" in
      1)
        if [ "$enable_webui" = "true" ]; then
          enable_webui="false"
          print_success "Hermes Web UI disabled."
        else
          enable_webui="true"
          print_success "Hermes Web UI enabled!"
        fi
        ;;
      2)
        local c_opt=""
        prompt_menu "Select Chat Integration:" \
          "Google Chat (Pub/Sub Event Streaming)" \
          "Slack (Socket Mode App)" \
          "Disable All Chat Integrations" \
          c_opt
        case "$c_opt" in
          1)
            google_chat_enabled="true"
            local gchat_users_hint=""
            if [ -z "$allowed_users" ]; then
              gchat_users_hint="empty list"
            fi
            prompt_read "Allowed Google Chat User Emails (comma-separated, empty allows all users)" \
              allowed_users "$allowed_users" false "$gchat_users_hint"
            ;;
          2) slack_enabled="true" ;;
          3) google_chat_enabled="false"; slack_enabled="false" ;;
        esac
        ;;
      3)
        local m_opt=""
        prompt_menu "Select AI Model Provider:" \
          "Google Gemini ($(default_model_for_provider gemini))" \
          "Google Vertex AI / Model Garden (no API key — Workload Identity)" \
          "OpenAI ($(default_model_for_provider openai))" \
          "Anthropic ($(default_model_for_provider anthropic))" \
          m_opt
        case "$m_opt" in
          1)
            model_provider="gemini"
            model_default_name="$(default_model_for_provider gemini)"
            prompt_read "Gemini API Key" gemini_api_key "$gemini_api_key" true
            ;;
          2)
            model_provider="vertex_ai"
            prompt_read "Vertex AI Project ID" vertex_project_id "$vertex_project_id"
            prompt_read "Vertex AI Location" vertex_location "$vertex_location"
            prompt_read "Vertex Model ID (publisher model, e.g. gemini-3.5-flash)" model_default_name "${model_default_name:-$(default_model_for_provider vertex_ai)}"
            # Same notice main() prints on the first-install path: switching a
            # running install to Vertex through this panel lands on the global
            # endpoint too, and must not do so silently.
            if [ "$vertex_location" = "global" ]; then
              print_warning "The global endpoint gives no in-region ML processing guarantee. Set a region above if you have a data-residency requirement."
            fi
            ;;
          3)
            model_provider="openai"
            model_default_name="$(default_model_for_provider openai)"
            prompt_read "OpenAI API Key" openai_api_key "$openai_api_key" true
            ;;
          4)
            model_provider="anthropic"
            model_default_name="$(default_model_for_provider anthropic)"
            prompt_read "Anthropic API Key" anthropic_api_key "$anthropic_api_key" true
            ;;
        esac
        ;;
      4)
        local p_opt=""
        prompt_menu "Select GCP IAM Permission Set:" \
          "read-only — auditing and observability, no GCP write capability (Default)" \
          "custom — exactly the roles you list, no built-in bundle" \
          p_opt
        case "$p_opt" in
          1) permission_set="read-only" ;;
          2)
            permission_set="custom"
            while true; do
              prompt_read "Custom GCP IAM Roles (space- or comma-separated)" custom_roles "$custom_roles"
              [ -n "$custom_roles" ] && break
              print_error "The custom permission set needs at least one role, e.g. roles/container.viewer."
            done
            warn_on_overreaching_custom_roles "$custom_roles"
            ;;
        esac
        ;;
      5)
        prompt_read "GitHub Org / Username" github_org "$github_org"
        prompt_read "GitOps Repository Name" github_repo "$github_repo"
        ;;
      6)
        print_step "Saving & Re-applying Configuration State"
        if [ -z "$image_tag" ]; then
          prompt_read "Container image tag (validated release tag or full commit SHA)" \
            image_tag "$(default_image_tag "$repo_dir")" false "$(default_image_tag_label "$repo_dir")"
        fi
        validate_immutable_ref "$image_tag"
        verify_local_source_ref "$repo_dir" "$image_tag"
        export PARAM_PROJECT_ID="$project_id" PARAM_CLUSTER_NAME="$cluster_name" PARAM_REGION="$region"
        export PARAM_ENABLE_WEBUI="$enable_webui" PARAM_MODEL_PROVIDER="$model_provider"
        export PARAM_PERMISSION_SET="$permission_set" PARAM_ENABLE_GVISOR="$enable_gvisor"
        export GOOGLE_CHAT_ENABLED="$google_chat_enabled" SLACK_ENABLED="$slack_enabled"

        save_var PROJECT_ID "$project_id"
        save_var PROJECT_NUMBER "$project_number"
        save_var CLUSTER_NAME "$cluster_name"
        save_var REGION "$region"
        save_var KMS_LOCATION "$(derive_kms_location "$region")"
        save_var MODEL_PROVIDER "$model_provider"
        save_var MODEL_DEFAULT_NAME "$model_default_name"
        save_var VERTEX_PROJECT_ID "$vertex_project_id"
        save_var VERTEX_LOCATION "$vertex_location"
        save_secret_var GEMINI_API_KEY "$gemini_api_key"
        save_secret_var OPENAI_API_KEY "$openai_api_key"
        save_secret_var ANTHROPIC_API_KEY "$anthropic_api_key"
        save_var ALLOWED_USERS "$allowed_users"
        save_var CHAT_TOPIC_NAME "$chat_topic_name"
        save_var CHAT_SUB_NAME "$chat_sub_name"
        save_var GOOGLE_CHAT_ENABLED "$google_chat_enabled"
        save_var SLACK_ENABLED "$slack_enabled"
        save_var PLATFORM_AGENT_PERMISSION_SET "$permission_set"
        if [ "$permission_set" = "custom" ]; then
          save_var PLATFORM_AGENT_CUSTOM_ROLES "$custom_roles"
        fi
        save_var ENABLE_GVISOR "$enable_gvisor"
        save_var HERMES_DASHBOARD_ENABLED "$enable_webui"
        save_var GITHUB_ORG "$github_org"
        save_var GITHUB_REPO "$github_repo"
        save_var GITHUB_APP_ID "$github_app_id"
        save_var KMS_KEYRING "$kms_keyring"
        save_var KMS_KEY "$kms_key"
        save_var GITHUB_PEM_PATH "$github_pem_path"
        save_var NO_CONFIRM "1"
        print_success "Updated configuration saved to: $vars_file"

        # One engine for every kind of change: a full terraform apply
        # reconciles GCP resources and chart values alike, so a Vertex switch
        # lands its IAM, the gateway, and the agent in one pass. When nothing
        # GCP-side moved, the apply is a fast no-op around the Helm upgrade.
        # shellcheck disable=SC1090
        source "$vars_file"
        write_tfvars_from_state "$(tf_compose_dir "$repo_dir")/terraform.tfvars" "$image_tag"
        print_info "Re-applying the install to GKE cluster '$cluster_name' (terraform apply)..."
        run_lifecycle_apply "$repo_dir" "/tmp/kube-agents-apply-$(date -u +%Y%m%dT%H%M%SZ).log"
        print_success "Configuration applied!"
        ;;
      7)
        print_info "Exiting Control Panel."
        break
        ;;
    esac
  done
}

# ─── Main Installer Procedure ──────────────────────────────────────────────────
main() {
  parse_args "$@"
  print_banner

  if [ "${PARAM_MENU_MODE:-false}" = "true" ]; then
    run_menu_system
    exit 0
  fi

  # 1. Environment Detection (Google Cloud Shell vs Linux/macOS Terminal)
  local is_cloud_shell="false"
  if [ "${CLOUD_SHELL:-false}" = "true" ] || [ -n "${DEVSHELL_PROJECT_ID:-}" ]; then
    is_cloud_shell="true"
    print_success "Environment Detected: ${C_BOLD}Google Cloud Shell${C_RESET} ☁️"
  else
    print_info "Environment Detected: ${C_BOLD}Standard Workstation / Linux Terminal${C_RESET} 💻"
  fi

  if [ "$PARAM_NON_INTERACTIVE" = "true" ]; then
    print_info "Execution Mode: ${C_BOLD}Non-Interactive / AI Agent Automated Mode${C_RESET} 🤖"
  fi

  local image_tag="${PARAM_IMAGE_TAG:-}"
  if [ -z "$image_tag" ]; then
    local head_sha=""
    head_sha="$(default_image_tag)"
    if [ "$PARAM_NON_INTERACTIVE" = "true" ]; then
      if [ -z "$head_sha" ]; then
        print_error "--image-tag is required; use a validated release tag or full commit SHA."
        exit 1
      fi
      image_tag="$head_sha"
      print_info "Defaulting image tag to $(default_image_tag_label): ${C_BOLD}${image_tag}${C_RESET}"
    else
      prompt_read "Container image tag (validated release tag or full commit SHA)" \
        image_tag "$head_sha" false "$(default_image_tag_label)"
    fi
  fi
  validate_immutable_ref "$image_tag"

  # 2. Prerequisite CLI Tools Check & Auto-Installation
  print_step "1. Checking Prerequisites & Installing Missing Tools"
  # terraform is the install engine (terraform/examples/full-install through
  # lifecycle.sh); kubectl is used by lifecycle.sh and the health checks; helm
  # serves upgrade.sh's fast path; jq and gh remain for the surrounding
  # tooling. Everything is checked up front rather than discovered halfway
  # through with the cluster already created.
  for tool in git gcloud kubectl gh helm jq terraform; do
    if command -v "$tool" >/dev/null 2>&1; then
      print_success "Found CLI tool: $tool"
    else
      auto_install_tool "$tool"
    fi
  done
  require_min_gcloud_version || exit 1
  require_min_terraform_version || exit 1

  # 3. Provisioning Sources & Shared Defaults
  print_step "2. Setting up Workspace Repository"
  local repo_dir=""
  acquire_source_repo repo_dir "$image_tag"
  source_provisioning_helpers "$repo_dir"
  resolve_shared_defaults

  # 3. Google Cloud Authentication Check
  print_step "3. Verifying Google Cloud Authentication"
  local active_account=""
  active_account=$(gcloud config get-value account 2>/dev/null || echo "")

  if [ -z "$active_account" ] || ! gcloud auth print-access-token >/dev/null 2>&1; then
    if [ "$PARAM_NON_INTERACTIVE" = "true" ]; then
      print_error "gcloud CLI is not authenticated and non-interactive mode is enabled."
      print_info "Please run 'gcloud auth login' before executing the installer."
      exit 1
    fi
    print_warning "gcloud CLI is not authenticated."
    print_info "Launching Google Cloud authentication..."
    gcloud auth login </dev/tty >/dev/tty
    gcloud auth application-default login </dev/tty >/dev/tty
    active_account=$(gcloud config get-value account 2>/dev/null || echo "")
  fi
  print_success "Authenticated as: ${C_BOLD}${active_account:-Google Cloud User}${C_RESET}"

  # 4. GCP Project Target Configuration
  print_step "4. Google Cloud Target Configuration"
  local active_proj=""
  if [ "$is_cloud_shell" = "true" ] && [ -n "${DEVSHELL_PROJECT_ID:-}" ]; then
    active_proj="${DEVSHELL_PROJECT_ID}"
  else
    active_proj=$(gcloud config get-value project 2>/dev/null || echo "")
  fi

  local project_id=""
  if [ -n "$PARAM_PROJECT_ID" ]; then
    project_id="$PARAM_PROJECT_ID"
  elif [ "$PARAM_NON_INTERACTIVE" = "true" ] || ! has_controlling_tty; then
    prompt_read "Target GCP Project ID" project_id "$active_proj"
  else
    select_gcp_project project_id "$active_proj"
  fi

  if [ -z "$project_id" ]; then
    print_error "No GCP project selected. Re-run with --project-id=<project-id>."
    exit 1
  fi

  if [ "$PARAM_DRY_RUN" = "true" ]; then
    print_info "Dry-run: leaving the active gcloud project unchanged (target: ${project_id})."
  elif ! gcloud config set project "$project_id" >/dev/null; then
    print_error "Unable to select GCP project '$project_id'. Verify the project ID and your access."
    exit 1
  fi
  print_success "Selected Project ID: ${C_BOLD}${project_id}${C_RESET}"

  # Auto-resolve Project Number
  local project_number=""
  project_number=$(gcloud projects describe "$project_id" --format="value(projectNumber)" 2>/dev/null || echo "")
  if [ -z "$project_number" ]; then
    print_error "Unable to resolve the project number for '$project_id'. Verify the project ID and your access."
    exit 1
  fi
  print_success "Resolved Project Number: ${C_BOLD}${project_number}${C_RESET}"

  # Region Selection
  local active_region=""
  active_region=$(gcloud config get-value compute/region 2>/dev/null || echo "")
  local region="${PARAM_REGION:-}"
  if [ -z "$region" ]; then
    prompt_read "Target GCP Region" region "${active_region:-$DEFAULT_REGION}"
  fi

  # Checked here as well as after the menu below so a bad --cluster-mode fails
  # before the rest of the interview, not after it.
  local cluster_mode="${PARAM_CLUSTER_MODE:-}"
  [ -z "$cluster_mode" ] || require_creatable_cluster_mode "$cluster_mode" "$region"

  # 5. GKE Cluster Selection & Provisioning Strategy
  print_step "5. GKE Cluster Topology & Capacity Setup"
  local cluster_choice=""
  if [ "$PARAM_NON_INTERACTIVE" = "true" ] || [ -n "$PARAM_CLUSTER_NAME" ]; then
    if [ -n "$PARAM_CLUSTER_NAME" ]; then
      cluster_choice="2"
    else
      cluster_choice="1"
    fi
  else
    prompt_menu "How would you like to handle the GKE Cluster?" \
      "Provision a NEW GKE Cluster from scratch (Recommended)" \
      "Use an EXISTING GKE Cluster" \
      cluster_choice
  fi

  local cluster_name="${PARAM_CLUSTER_NAME:-}"
  # Set on the branches where the user has demonstrably asked for a cluster
  # that does not exist yet, which is the only case --cluster-mode decides.
  # Picking one out of the discovered list, or naming one with --cluster-name,
  # does not qualify: the generator probes those and the live shape wins.
  local ask_cluster_shape="false"
  if [ "$cluster_choice" = "1" ]; then
    if [ -z "$cluster_name" ]; then
      prompt_read "New GKE Cluster Name" cluster_name "$DEFAULT_CLUSTER_NAME"
    fi
    ask_cluster_shape="true"
  else
    if [ -n "$PARAM_CLUSTER_NAME" ]; then
      cluster_name="$PARAM_CLUSTER_NAME"
    else
      # Auto-discover existing clusters
      print_info "Querying existing GKE clusters in project '$project_id'..."
      local cluster_lines=""
      cluster_lines=$(gcloud container clusters list --project="$project_id" --format="value(name,location)" 2>/dev/null || echo "")

      if [ -n "$cluster_lines" ]; then
        local cluster_opts=()
        local cluster_names=()
        local cluster_locations=()
        while IFS=$'\t' read -r c_name c_loc; do
          if [ -n "$c_name" ]; then
            cluster_names+=("$c_name")
            cluster_locations+=("$c_loc")
            cluster_opts+=("$c_name (location: $c_loc)")
          fi
        done <<< "$cluster_lines"
        cluster_opts+=("Type an unlisted cluster name manually")

        local c_choice=""
        prompt_menu "Select existing GKE cluster:" "${cluster_opts[@]}" c_choice
        if [ "$c_choice" -le "${#cluster_names[@]}" ]; then
          cluster_name="${cluster_names[$((c_choice-1))]}"
          region="${cluster_locations[$((c_choice-1))]}"
          print_success "Using discovered cluster location: ${C_BOLD}${region}${C_RESET}"
        else
          prompt_read "Existing GKE Cluster Name" cluster_name "$DEFAULT_CLUSTER_NAME"
          # A name the project's own cluster list did not offer, so it very
          # likely does not exist and this run creates it.
          ask_cluster_shape="true"
        fi
      else
        print_warning "No existing GKE clusters found in project '$project_id'."
        prompt_read "Existing GKE Cluster Name" cluster_name "$DEFAULT_CLUSTER_NAME"
        # Nothing to adopt in this project, so whatever is named here is about
        # to be created.
        ask_cluster_shape="true"
      fi
    fi
  fi
  # Only when --cluster-mode said nothing: a flag the caller passed is an
  # answer already, and re-asking would let a mis-keyed menu choice override
  # it.
  #
  # The order comes from resolve_creatable_cluster_mode rather than being
  # hardcoded, because prompt_menu's enter default is option 1. A fixed
  # Autopilot-first order makes pressing enter an *explicit* autopilot
  # request, which the resolver is then right to refuse to demote — so a
  # zonal interactive install would abort here rather than build Standard,
  # which is what it did before this default changed. Deriving the order
  # keeps the "(Default)" label, the enter key and the resolver saying the
  # same thing at both kinds of location.
  if [ -z "$cluster_mode" ] && [ "$ask_cluster_shape" = "true" ] &&
    [ "$PARAM_NON_INTERACTIVE" != "true" ]; then
    local mode_choice="" menu_default=""
    local autopilot_option="Autopilot — Google manages the nodes and you pay per Pod; regional only, and gVisor comes from its built-in RuntimeClass"
    local standard_option="Standard — you size and pay for the node pool; carries the GKE Sandbox pool for --gvisor, and is the only shape that can be zonal"
    menu_default="$(resolve_creatable_cluster_mode "" "$region")"
    if [ "$menu_default" = "autopilot" ]; then
      prompt_menu "Which shape should the GKE cluster be, if this run creates it?" \
        "${autopilot_option} (Default)" \
        "${standard_option}" \
        mode_choice
      case "$mode_choice" in
        1) cluster_mode="autopilot" ;;
        2) cluster_mode="standard" ;;
      esac
    else
      # Zonal location. Autopilot stays on the menu so picking it is still an
      # explicit request that require_creatable_cluster_mode rejects by name,
      # rather than a shape that silently turns into something else.
      prompt_menu "Which shape should the GKE cluster be, if this run creates it?" \
        "${standard_option} (Default)" \
        "${autopilot_option} — not available at a zonal location" \
        mode_choice
      case "$mode_choice" in
        1) cluster_mode="standard" ;;
        2) cluster_mode="autopilot" ;;
      esac
    fi
  fi
  # Nothing asked, nothing passed: installer_common.sh owns the default, and
  # resolve_creatable_cluster_mode applies it. Explaining the demotion is the
  # caller's job so the resolver can echo the mode and nothing else.
  #
  # This matters most on the --cluster-name path, where ask_cluster_shape is
  # false and the check below therefore never runs: a named cluster that does
  # not exist yet would otherwise be written as autopilot at a zone and
  # rejected by the module's precondition at terraform validate, after the
  # whole interview had already been collected.
  local cluster_mode_requested="$cluster_mode"
  cluster_mode="$(resolve_creatable_cluster_mode "$cluster_mode" "$region")"
  # ask_cluster_shape gates the message for the same reason it gates the check
  # below: on both adoption paths no cluster is created by this run, so the
  # advice to "pass --region with a region" would point at a location the
  # target cluster does not live at. On --cluster-name that is not merely
  # noise — write_tfvars_from_state probes with --location "$REGION", so
  # re-running with the suggested region misses the live cluster, takes the
  # confirmed-NOT_FOUND branch, and creates a second one under -auto-approve.
  if [ "$ask_cluster_shape" = "true" ] && [ -z "$cluster_mode_requested" ] &&
    [ "$cluster_mode" != "$DEFAULT_CLUSTER_MODE" ]; then
    print_info "Location '${region}' is a zone and Autopilot clusters are regional, so a cluster created by this run will be Standard. Pass --region with a region to get the default Autopilot shape."
  fi

  # Only where a cluster is about to be created. Adopting a discovered cluster
  # replaced $region with that cluster's own location, which may be a zone —
  # and failing an adoption over a location the installer chose itself, for a
  # shape the generator is about to overrule anyway, blames the wrong input.
  # The flag/region pair the caller did supply was already checked above.
  if [ "$ask_cluster_shape" = "true" ]; then
    require_creatable_cluster_mode "$cluster_mode" "$region"
  fi
  print_success "Selected Cluster Name: ${C_BOLD}${cluster_name}${C_RESET}"

  # 6. Chat & Messaging Platform Integration
  print_step "6. Chat & Messaging Integrations Setup"
  local chat_choice=""
  if [ "$PARAM_NON_INTERACTIVE" = "true" ] || [ "${PARAM_ENABLE_GOOGLE_CHAT:-false}" = "true" ] || [ "${SLACK_ENABLED:-false}" = "true" ]; then
    # SLACK_ENABLED (with SLACK_BOT_TOKEN / SLACK_APP_TOKEN and the other
    # SLACK_* variables) is the non-interactive spelling of the Slack
    # interview, the same variables the Day-2 menu reads. Without it Slack
    # would be reachable only through a controlling tty.
    if [ "${PARAM_ENABLE_GOOGLE_CHAT:-false}" = "true" ] && [ "${SLACK_ENABLED:-false}" = "true" ]; then
      chat_choice="3"
    elif [ "${PARAM_ENABLE_GOOGLE_CHAT:-false}" = "true" ]; then
      chat_choice="1"
    elif [ "${SLACK_ENABLED:-false}" = "true" ]; then
      chat_choice="2"
    else
      chat_choice="4"
    fi
  else
    prompt_menu "Select Chat Channel Integration(s):" \
      "Google Chat (Pub/Sub Event Streaming)" \
      "Slack (Socket Mode App)" \
      "Both Google Chat and Slack" \
      "None (CLI & REST API Gateway only)" \
      chat_choice
  fi

  local google_chat_enabled="false"
  local slack_enabled="false"
  # Empty by default: the allowlist is opt-in, and an unset list allows all users.
  local allowed_users="${ALLOWED_USERS:-}"
  local allowed_users_hint=""
  if [ -z "$allowed_users" ]; then
    allowed_users_hint="empty list"
  fi
  local chat_topic_name="${PARAM_CHAT_TOPIC_NAME:-${CHAT_TOPIC_NAME:-platform-agent-chat-events}}"
  local chat_sub_name="${CHAT_SUB_NAME:-platform-agent-chat-events-sub}"
  local google_chat_mode="${PARAM_GOOGLE_CHAT_MODE:-${GOOGLE_CHAT_MODE:-default}}"
  if [[ ! "$google_chat_mode" =~ ^(default|debug)$ ]]; then
    print_error "--google-chat-mode must be either 'default' or 'debug'."
    exit 1
  fi
  # Seeded from the environment so the non-interactive path can carry the
  # Slack settings: prompt_read keeps a non-empty current value there, and
  # prompts with it as the prefill when there is a tty.
  local slack_bot_token="${SLACK_BOT_TOKEN:-}"
  local slack_app_token="${SLACK_APP_TOKEN:-}"
  local slack_allowed_users="${SLACK_ALLOWED_USERS:-}"
  local slack_home_channel="${SLACK_HOME_CHANNEL:-}"
  local slack_home_channel_name="${SLACK_HOME_CHANNEL_NAME:-}"

  case "$chat_choice" in
    1)
      google_chat_enabled="true"
      prompt_read "Allowed User Email(s) for Google Chat (comma-separated, empty allows all users)" \
        allowed_users "$allowed_users" false "$allowed_users_hint"
      prompt_read "Pub/Sub Topic Name for Google Chat" chat_topic_name "$chat_topic_name"
      ;;
    2)
      slack_enabled="true"
      prompt_read "Slack Bot Token (xoxb-...)" slack_bot_token "" true
      prompt_read "Slack App Token (xapp-...)" slack_app_token "" true
      prompt_read "Allowed Slack User IDs / Emails (comma-separated)" slack_allowed_users "$allowed_users"
      prompt_read "Slack Home Channel ID (optional, e.g. C0123456789)" slack_home_channel ""
      prompt_read "Slack Home Channel Name (optional, e.g. #gke-alerts)" slack_home_channel_name ""
      ;;
    3)
      google_chat_enabled="true"
      slack_enabled="true"
      prompt_read "Allowed User Email(s) for Google Chat (comma-separated, empty allows all users)" \
        allowed_users "$allowed_users" false "$allowed_users_hint"
      prompt_read "Pub/Sub Topic Name for Google Chat" chat_topic_name "$chat_topic_name"
      prompt_read "Slack Bot Token (xoxb-...)" slack_bot_token "" true
      prompt_read "Slack App Token (xapp-...)" slack_app_token "" true
      prompt_read "Allowed Slack User IDs / Emails (comma-separated)" slack_allowed_users "$allowed_users"
      prompt_read "Slack Home Channel ID (optional, e.g. C0123456789)" slack_home_channel ""
      prompt_read "Slack Home Channel Name (optional, e.g. #gke-alerts)" slack_home_channel_name ""
      ;;
    4)
      print_info "Chat integrations disabled. Agent will operate via CLI / REST API Gateway."
      ;;
  esac

  # 7. LLM Model Provider Selection & API Key Auto-Discovery
  print_step "7. AI Model Provider Credentials"
  local model_provider="$PARAM_MODEL_PROVIDER"
  if ! is_valid_model_provider "$model_provider"; then
    print_error "Unsupported model provider '$model_provider'. Use gemini, vertex_ai, anthropic, or openai."
    exit 1
  fi
  local model_default_name="${PARAM_MODEL_DEFAULT_NAME:-${MODEL_DEFAULT_NAME:-}}"
  if [ -z "$model_default_name" ]; then
    model_default_name="$(default_model_for_provider "$model_provider")"
  fi

  # Vertex authenticates with Workload Identity rather than an API key, so these
  # two are the only credentials it needs. The project defaults to the install
  # target; the location does not, because a model is only callable from a
  # location that serves it and the cluster's region often is not one — see
  # DEFAULT_VERTEX_LOCATION in k8s-operator/scripts/installer_common.sh.
  local vertex_project_id="${PARAM_VERTEX_PROJECT_ID:-$project_id}"
  local vertex_location="${PARAM_VERTEX_LOCATION:-$DEFAULT_VERTEX_LOCATION}"

  local detected_gemini_key="${PARAM_GEMINI_API_KEY:-${GEMINI_API_KEY:-}}"
  if [ -z "$detected_gemini_key" ]; then
    detected_gemini_key=$(gcloud secrets versions access latest --secret="gemini-api-key" --project="$project_id" 2>/dev/null || echo "")
  fi
  local gemini_api_key="${detected_gemini_key:-}"
  local openai_api_key="${PARAM_OPENAI_API_KEY:-}"
  local anthropic_api_key="${PARAM_ANTHROPIC_API_KEY:-}"

  if [ "$PARAM_NON_INTERACTIVE" != "true" ]; then
    local model_choice=""
    prompt_menu "Select Model Provider for the Platform Agent:" \
      "Google Gemini (Recommended: $(default_model_for_provider gemini) / Gemini API)" \
      "Google Vertex AI / Model Garden (no API key — Workload Identity)" \
      "OpenAI ($(default_model_for_provider openai) / OpenAI API)" \
      "Anthropic ($(default_model_for_provider anthropic) / Anthropic API)" \
      model_choice

    case "$model_choice" in
      1)
        model_provider="gemini"
        model_default_name="$(default_model_for_provider gemini)"
        local detected_key="${GEMINI_API_KEY:-}"
        if [ -z "$detected_key" ]; then
          detected_key=$(gcloud secrets versions access latest --secret="gemini-api-key" --project="$project_id" 2>/dev/null || echo "")
        fi
        prompt_read "Gemini API Key" gemini_api_key "$detected_key" true
        ;;
      2)
        model_provider="vertex_ai"
        prompt_read "Vertex AI Project ID" vertex_project_id "$vertex_project_id"
        prompt_read "Vertex AI Location" vertex_location "$vertex_location"
        prompt_read "Vertex Model ID (publisher model, e.g. gemini-3.5-flash)" model_default_name "$(default_model_for_provider vertex_ai)"
        ;;
      3)
        model_provider="openai"
        model_default_name="$(default_model_for_provider openai)"
        prompt_read "OpenAI API Key" openai_api_key "${OPENAI_API_KEY:-}" true
        ;;
      4)
        model_provider="anthropic"
        model_default_name="$(default_model_for_provider anthropic)"
        prompt_read "Anthropic API Key" anthropic_api_key "${ANTHROPIC_API_KEY:-}" true
        ;;
    esac
  fi

  case "$model_provider" in
    gemini)
      [ -n "$gemini_api_key" ] || print_warning "No Gemini API key was provided; the agent will require a credential update before model calls can succeed."
      ;;
    vertex_ai)
      print_info "Vertex AI needs no API key: LiteLLM authenticates as ${LITELLM_GSA_NAME:-kubeagents-litellm-gsa}@${project_id}.iam.gserviceaccount.com via Workload Identity."
      print_info "Serving ${model_default_name} from projects/${vertex_project_id}/locations/${vertex_location}."
      # The literal, not $DEFAULT_VERTEX_LOCATION: this warns about a property
      # of the global endpoint, not about the default being in effect. Tying it
      # to the constant would fire with false text if the default ever moved to
      # a region, and stay silent for an explicit --vertex-location=global.
      #
      # An `if` rather than `[ ... ] && ...`: the AND-list form returns non-zero
      # whenever the test fails, which is a live hazard under this file's
      # `set -Eeuo pipefail` the moment it becomes the last statement in a
      # function. The `||` idiom used elsewhere in this case block always
      # returns 0; the `&&` form does not.
      if [ "$vertex_location" = "global" ]; then
        print_warning "The global endpoint gives no in-region ML processing guarantee. Pass --vertex-location=<region> if you have a data-residency requirement."
      fi
      ;;
    openai)
      [ -n "$openai_api_key" ] || print_warning "No OpenAI API key was provided; the agent will require a credential update before model calls can succeed."
      ;;
    anthropic)
      [ -n "$anthropic_api_key" ] || print_warning "No Anthropic API key was provided; the agent will require a credential update before model calls can succeed."
      ;;
  esac

  # 8. GitOps Infrastructure Repository Connection
  print_step "8. GitOps Infrastructure Repository Setup"
  local github_org="${PARAM_GITOPS_ORG:-}"
  local github_repo="${PARAM_GITOPS_REPO:-gke-fleet-iac}"
  # Env fallbacks, not bare empties: the non-interactive path never reaches
  # the interview prompts below, so GITHUB_APP_ID / GITHUB_PEM_PATH exported
  # into the run are the only way an automated install can enable the minter.
  local github_app_id="${GITHUB_APP_ID:-}"
  local kms_keyring="${KMS_KEYRING:-github-token-minter-keyring}"
  local kms_key="${KMS_KEY:-github-token-minter-key}"
  local github_pem_path="${GITHUB_PEM_PATH:-}"

  if [ "$PARAM_NON_INTERACTIVE" != "true" ]; then
    local gitops_choice=""
    prompt_menu "Would you like to connect or create a GitOps repo for automated PRs?" \
      "Create a NEW GitHub Repository automatically (Recommended)" \
      "Connect an EXISTING GitHub Repository" \
      "Skip for now (Can be enabled later)" \
      gitops_choice

    if [ "$gitops_choice" = "1" ] || [ "$gitops_choice" = "2" ]; then
      # The repo must be organization-owned: the token minter resolves App
      # installations at /orgs/{org}/installation, which does not exist for
      # personal accounts. So the default offered here is the operator's first
      # organization, never their login — suggesting a username would guarantee
      # the failure below.
      local detected_gh_org=""
      detected_gh_org=$(gh api user/orgs -q '.[0].login' 2>/dev/null || echo "")
      print_info "The GitOps repo must belong to a GitHub organization; a personal account cannot"
      print_info "mint tokens. A free organization is enough."
      while true; do
        prompt_read "GitHub Organization" github_org "${detected_gh_org}"

        local org_problem=""
        if [ -z "$github_org" ]; then
          org_problem="A GitHub organization is required to connect a GitOps repo."
        elif ! is_truthy "${SKIP_GITHUB_ORG_CHECK:-false}"; then
          case "$(github_account_type "$github_org")" in
            organization) ;;
            user) org_problem="'${github_org}' is a personal GitHub account, not an organization. The token minter cannot mint tokens for it." ;;
            missing) org_problem="'${github_org}' does not exist on GitHub. Check the spelling." ;;
            *) print_warning "Could not reach GitHub to verify '${github_org}'; continuing." ;;
          esac
        fi
        [ -z "$org_problem" ] && break

        print_error "$org_problem"
        # The minter cannot mint tokens for a personal account, and a
        # non-organization owner would only surface as a failure after the
        # cluster, node pools and operator are already built. Settle it
        # here, while nothing has been created yet.
        if [ "$PARAM_NON_INTERACTIVE" = "true" ] || ! has_controlling_tty; then
          print_error "Set GITHUB_ORG to an organization and re-run, or export SKIP_GITHUB_ORG_CHECK=true to bypass this check."
          exit 1
        fi
      done
      prompt_read "GitOps Repository Name" github_repo "gke-fleet-iac"

      print_info "GitHub access uses the short-lived GitHub App token minter."
      prompt_read "GitHub App ID" github_app_id ""
      prompt_read "Cloud KMS Keyring Name" kms_keyring "github-token-minter-keyring"
      prompt_read "Cloud KMS Key Name" kms_key "github-token-minter-key"
      prompt_read "Path to downloaded GitHub App Private Key (.pem)" github_pem_path ""
    fi
  fi

  # 9. Agent Permissions & Sandbox Isolation Boundary
  print_step "9. Agent Security & Runtime Isolation Boundary"
  local permission_set="${PARAM_PERMISSION_SET:-read-only}"
  # Normalise and keep the normalised value, the way common.sh does. The gate
  # below normalises its own argument so that every spelling reaches the right
  # message, but it cannot fix the caller's variable -- and everything
  # downstream compares against the lowercase literal: the custom-roles check
  # and the over-reach warning just below, write_state_var's PLATFORM_AGENT_*
  # pair, and terraform's case-sensitive contains() on permission_set. Passing
  # `Custom` through raw would clear the gate and then miss all four.
  permission_set=$(printf '%s' "$permission_set" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
  # require_supported_permission_set (installer_common.sh) is the one home for
  # the accepted vocabulary and for the explanation the removed admin bundle
  # gets -- a PLATFORM_AGENT_PERMISSION_SET inherited from a vars.sh or a CI
  # environment variable written before the removal lands here.
  require_supported_permission_set "$permission_set" || exit 1
  local custom_roles="${PARAM_CUSTOM_ROLES:-}"
  # This rule is also written in init_var_platform_agent_permission_set
  # (k8s-operator/scripts/common.sh), which has no caller left in the repository
  # -- the numbered provision scripts that used to invoke it went with #797. So
  # this is the only place it runs, not a duplicate of somewhere it also runs.
  if [ "$permission_set" = "custom" ] && [ "$PARAM_NON_INTERACTIVE" = "true" ] && [ -z "$custom_roles" ]; then
    print_error "--permission-set=custom requires --custom-roles with at least one role."
    exit 1
  fi
  if [ "$permission_set" = "custom" ] && [ -n "$custom_roles" ]; then
    warn_on_overreaching_custom_roles "$custom_roles"
  fi
  # ${VAR-default}, not ${VAR:-default}: PARAM_ENABLE_GVISOR is always set (see
  # its declaration), so the only way it arrives empty is `--gvisor=` with no
  # value. Substituting on empty would silently read that as the default; this
  # form lets it reach the validator below and be rejected.
  local enable_gvisor="${PARAM_ENABLE_GVISOR-true}"
  if [[ ! "$enable_gvisor" =~ ^(true|false)$ ]]; then
    print_error "--gvisor must be either true or false."
    exit 1
  fi
  if [[ ! "${PARAM_ENABLE_WEBUI:-false}" =~ ^(true|false)$ ]]; then
    print_error "--enable-web-ui must be either true or false."
    exit 1
  fi
  # An agent that forgets every conversation is the worse default, so memory is
  # on unless it is turned off. The choice decides two things: whether the
  # harness keeps memory at all, and — when it does — whether that costs an
  # extra API server and Postgres database in the cluster. Nothing downstream
  # infers one from the other, so both are recorded.
  #
  # `file` is the default because it is what every install got before the
  # searchable store existed: an upgrade that says nothing about memory keeps
  # the store it already has, and no install grows a Postgres database it never
  # asked for. Enterprise deployments opt in with --memory=hindsight.
  local memory_mode="${PARAM_MEMORY:-file}"
  if [[ ! "$memory_mode" =~ ^(off|file|hindsight)$ ]]; then
    print_error "--memory must be one of: off, file, hindsight."
    exit 1
  fi
  if [ "$PARAM_NON_INTERACTIVE" != "true" ]; then
    # These are GCP IAM role bundles for the agent's GSA, nothing else. Kubernetes
    # RBAC stays read-only in every set, and the GitOps pull-request path works in
    # every set, so neither belongs in these labels. read-only leads because it is
    # the documented default and the only set that enforces no cloud-plane writes.
    # See docs/site/src/content/docs/reference/security-and-iam.md.
    local perm_choice=""
    prompt_menu "Select Platform Agent GCP IAM Permission Set:" \
      "read-only — auditing and observability, no GCP write capability (Default)" \
      "custom — exactly the roles you list, no built-in bundle" \
      perm_choice

    case "$perm_choice" in
      1) permission_set="read-only" ;;
      2) permission_set="custom" ;;
    esac

    while [ "$permission_set" = "custom" ] && [ -z "$custom_roles" ]; do
      prompt_read "Custom GCP IAM Roles (space- or comma-separated)" custom_roles ""
      if [ -z "$custom_roles" ]; then
        # An empty custom list would only be rejected once the cluster and
        # operator are already provisioned; catch it at the prompt.
        print_error "The custom permission set needs at least one role, e.g. roles/container.viewer."
      fi
    done
    # Repeated rather than moved: the call above runs on the --custom-roles flag
    # path, which is decided before this prompt exists. An operator who runs
    # ./install.sh and types the roles in reaches only this one.
    if [ "$permission_set" = "custom" ] && [ -n "$custom_roles" ]; then
      warn_on_overreaching_custom_roles "$custom_roles"
    fi

    # prompt_menu answers an empty line with option 1, so the current value has
    # to be listed first — otherwise the "(Default)" label contradicts what a
    # bare Enter actually produces. The value reaching here is the sandbox
    # unless --gvisor=false said otherwise, so the usual order is Yes first;
    # the else branch keeps an explicit --gvisor=false from being re-enabled by
    # someone confirming the prompt. Option 2 is "the other one" either way.
    local gvisor_choice=""
    local gvisor_yes="Yes - gVisor Secure Kernel Sandbox (Hardened Workload Isolation)"
    local gvisor_no="No - Standard Container Runtime"
    local gvisor_prompt="Enable GKE Sandbox (gVisor) Runtime Isolation for Agent Workloads?"
    if [ "$enable_gvisor" = "true" ]; then
      prompt_menu "$gvisor_prompt" "${gvisor_yes} (Default)" "$gvisor_no" gvisor_choice
      if [ "$gvisor_choice" = "2" ]; then
        enable_gvisor="false"
      fi
    else
      prompt_menu "$gvisor_prompt" "${gvisor_no} (Default)" "$gvisor_yes" gvisor_choice
      if [ "$gvisor_choice" = "2" ]; then
        enable_gvisor="true"
      fi
    fi

    local webui_choice=""
    prompt_menu "Enable Hermes Web UI (Port 9119 Dashboard) for Agent Observability?" \
      "No - Disabled for reduced attack surface (Default)" \
      "Yes - Enabled for local browser debugging (port 9119)" \
      webui_choice

    if [ "$webui_choice" = "2" ]; then
      PARAM_ENABLE_WEBUI="true"
    fi

    # The two stores differ in what they cost to run and in how far they scale,
    # and the label says which so the choice can be made without reading a design
    # doc: the file store adds no services but is loaded into the model's context
    # whole on every turn, so it is bounded by the window; Hindsight retrieves only
    # what a question needs, at the price of an API server and a database.
    #
    # The file store is listed first because prompt_menu's default answer is
    # always option 1, and this is the one an install should get for saying
    # nothing — it is what installs got before the searchable store existed, and
    # it is the only option that adds no services to the cluster.
    local memory_choice=""
    prompt_menu "Should the agent remember things between conversations?" \
      "Files on the agent's own disk (Default) - For small or personal deployments. Per-user Markdown, no extra services to run, does not scale past a few pages" \
      "Searchable store - For enterprise deployments. Ranked recall that scales, deploys Hindsight (API + Postgres) into the cluster" \
      "No - Nothing is retained once a session ends" \
      memory_choice

    # Every branch assigns, rather than letting option 1 fall through to
    # --memory=: an answer given at the prompt is the more recent instruction of
    # the two, and the permission-set and gVisor prompts above already work this way.
    case "$memory_choice" in
      1) memory_mode="file" ;;
      2) memory_mode="hindsight" ;;
      3) memory_mode="off" ;;
    esac
  fi

  # MEMORY_PROVIDER carries the whole choice — including "no memory at all",
  # which is what `none` means. Everything downstream reads it and nothing else:
  # provisioning step 13 deploys Hindsight only for a Hindsight-backed provider,
  # the specialist overlay blanks anything that cannot be made read-only, and the
  # entrypoint gates the one-way file import the same way.
  #
  # MEMORY_ENABLED is a different switch and stays false. It turns on Hermes'
  # *built-in* MEMORY.md/USER.md, which has no per-user scoping and would sit
  # alongside whichever provider is chosen — two competing stores in front of one
  # agent. Every provider here replaces it rather than supplementing it. Nothing
  # about memory keys off this flag, so an upgrade cannot read a false left in an
  # old vars.sh as "this install wanted no memory".
  #
  # `none` rather than an empty string: the choice has to survive the trip
  # through the CR, and an absent provider takes the CRD default. The operator
  # translates `none` back to Hermes' own spelling — see MEMORY_PROVIDER_CHOICES
  # in k8s-operator/scripts/common.sh.
  #
  # `multiuser_memory` is the default provider everywhere it is named with no
  # install to ask (the CRD default, common.sh, and both profiles' config.yaml),
  # and `file` is what an install that says nothing about memory gets — the same
  # store those installs already had before the searchable one existed.
  local memory_enabled="false"
  local memory_provider="multiuser_memory"
  case "$memory_mode" in
    hindsight) memory_provider="kube_agents_memory" ;;
    off) memory_provider="none" ;;
  esac

  print_step "10. Generating Configuration State (k8s-operator/scripts/vars.sh)"
  local vars_file="${repo_dir}/k8s-operator/scripts/vars.sh"
  local registry_prefix="${PARAM_REGISTRY_PREFIX%/}"
  if [ -z "$registry_prefix" ] || [[ "$registry_prefix" == *"://"* ]]; then
    print_error "--registry-prefix must be a non-empty registry path without a URL scheme."
    exit 1
  fi
  # Empty is the default and means "upstream", so only the scheme is rejected.
  local third_party_registry_prefix="${PARAM_THIRD_PARTY_REGISTRY_PREFIX%/}"
  if [[ "$third_party_registry_prefix" == *"://"* ]]; then
    print_error "--third-party-registry-prefix must be a registry path without a URL scheme."
    exit 1
  fi

  local api_server_key
  api_server_key="$(openssl rand -hex 16 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(16))" 2>/dev/null || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  if [ -z "$api_server_key" ]; then
    print_error "Unable to generate API_SERVER_KEY from a secure random source."
    exit 1
  fi

  local old_umask
  old_umask="$(umask)"
  umask 077
  local final_vars_file="$vars_file"
  vars_file="${vars_file}.tmp"
  printf '%s\n' '# Auto-generated by kube-agents zero-friction installer' > "$vars_file"
  write_state_var "$vars_file" PROJECT_ID "$project_id"
  write_state_var "$vars_file" PROJECT_NUMBER "$project_number"
  write_state_var "$vars_file" CLUSTER_NAME "$cluster_name"
  write_state_var "$vars_file" CLUSTER_MODE "$cluster_mode"
  write_state_var "$vars_file" REGION "$region"
  write_state_var "$vars_file" KMS_LOCATION "$(derive_kms_location "$region")"
  write_state_var "$vars_file" ENABLE_GVISOR "$enable_gvisor"
  write_state_var "$vars_file" GVISOR_POOL_NAME "gvisor-pool"
  write_state_var "$vars_file" MODEL_PROVIDER "$model_provider"
  write_state_var "$vars_file" MODEL_DEFAULT_NAME "$model_default_name"
  write_state_var "$vars_file" VERTEX_PROJECT_ID "$vertex_project_id"
  write_state_var "$vars_file" VERTEX_LOCATION "$vertex_location"
  write_secret_state_var "$vars_file" GEMINI_API_KEY "$gemini_api_key"
  write_secret_state_var "$vars_file" OPENAI_API_KEY "$openai_api_key"
  write_secret_state_var "$vars_file" ANTHROPIC_API_KEY "$anthropic_api_key"
  write_state_var "$vars_file" ALLOWED_USERS "$allowed_users"
  write_state_var "$vars_file" CHAT_TOPIC_NAME "$chat_topic_name"
  write_state_var "$vars_file" CHAT_SUB_NAME "$chat_sub_name"
  write_state_var "$vars_file" GOOGLE_CHAT_ENABLED "$google_chat_enabled"
  write_state_var "$vars_file" GOOGLE_CHAT_MODE "$google_chat_mode"
  write_state_var "$vars_file" SLACK_ENABLED "$slack_enabled"
  write_secret_state_var "$vars_file" SLACK_BOT_TOKEN "$slack_bot_token"
  write_secret_state_var "$vars_file" SLACK_APP_TOKEN "$slack_app_token"
  write_state_var "$vars_file" SLACK_ALLOWED_USERS "$slack_allowed_users"
  write_state_var "$vars_file" SLACK_HOME_CHANNEL "$slack_home_channel"
  write_state_var "$vars_file" SLACK_HOME_CHANNEL_NAME "$slack_home_channel_name"
  write_secret_state_var "$vars_file" API_SERVER_KEY "$api_server_key"
  write_state_var "$vars_file" PLATFORM_AGENT_PERMISSION_SET "$permission_set"
  if [ "$permission_set" = "custom" ]; then
    write_state_var "$vars_file" PLATFORM_AGENT_CUSTOM_ROLES "$custom_roles"
  fi
  write_state_var "$vars_file" GITHUB_ORG "$github_org"
  write_state_var "$vars_file" GITHUB_REPO "$github_repo"
  write_state_var "$vars_file" GITHUB_APP_ID "$github_app_id"
  write_state_var "$vars_file" KMS_KEYRING "$kms_keyring"
  write_state_var "$vars_file" KMS_KEY "$kms_key"
  write_state_var "$vars_file" GITHUB_PEM_PATH "$github_pem_path"
  write_state_var "$vars_file" MEMORY_ENABLED "$memory_enabled"
  write_state_var "$vars_file" MEMORY_PROVIDER "$memory_provider"
  write_state_var "$vars_file" USER_PROFILE_ENABLED "${PARAM_USER_PROFILE_ENABLED:-${USER_PROFILE_ENABLED:-false}}"
  write_state_var "$vars_file" HERMES_DASHBOARD_ENABLED "${PARAM_ENABLE_WEBUI:-false}"
  write_state_var "$vars_file" REGISTRY_PREFIX "$registry_prefix"
  # Written only when asked for. An empty value here would be sourced over an
  # exported one, turning "leave them upstream" from a default into an override
  # the installer never took a flag for.
  if [ -n "$third_party_registry_prefix" ]; then
    write_state_var "$vars_file" THIRD_PARTY_REGISTRY_PREFIX "$third_party_registry_prefix"
  fi
  # Bare repository paths on purpose: IMAGE_TAG is scoped to a single pipeline
  # run and is never persisted here, so the consuming step attaches it with
  # qualify_image_ref.
  #
  # Two images are absent on purpose. REPLAY_IMAGE belongs to the dev-only
  # inference-replay deploy, whose make target requires it from the caller.
  # CREDENTIAL_PROXY_IMAGE would pin the sidecar for
  # every PlatformAgent in the cluster: the operator otherwise derives it from
  # each CR's own agent image, and a cluster-wide env override beats that
  # derivation, so a later re-render of the CR at a new tag would leave the
  # sidecar behind on the tag of the install that wrote this file.
  write_state_var "$vars_file" OPERATOR_IMAGE "${registry_prefix}/k8s-operator"
  write_state_var "$vars_file" PLATFORM_AGENT_IMAGE "${registry_prefix}/platform-agent"
  write_state_var "$vars_file" ENABLE_GKE_BACKUP_PLAN "${ENABLE_GKE_BACKUP_PLAN:-false}"
  write_state_var "$vars_file" NO_CONFIRM "1"
  chmod 600 "$vars_file"
  mv -f -- "$vars_file" "$final_vars_file"
  vars_file="$final_vars_file"
  umask "$old_umask"
  print_success "Configuration saved to: $vars_file"

  # The engine input, generated from the state file just written so the two
  # can never disagree. Re-sourcing vars.sh is what puts that state in the
  # environment write_tfvars_from_state reads.
  # shellcheck disable=SC1090
  source "$vars_file"

  local tfvars_file
  tfvars_file="$(tf_compose_dir "$repo_dir")/terraform.tfvars"
  write_tfvars_from_state "$tfvars_file" "$image_tag"
  persist_effective_cluster_mode "$cluster_mode"
  print_success "Terraform input saved to: $tfvars_file"

  # Pre-Flight Summary & Final Confirmation Checkpoint
  print_step "11. Pre-Flight Configuration Summary"
  echo -e "${C_CYAN}${C_BOLD}"
  draw_separator
  echo -e "${C_RESET}${C_BOLD}Please review your selections before provisioning begins:${C_RESET}"
  echo -e "  • ${C_CYAN}GCP Target Project:${C_RESET} ${C_BOLD}${project_id}${C_RESET} (Project Number: ${project_number:-unknown})"
  # The generator's answer, not the interview's: on an existing cluster it
  # probed the live shape and the flag had no say.
  echo -e "  • ${C_CYAN}GKE Cluster:${C_RESET} ${C_BOLD}${cluster_name}${C_RESET} (${region}, GKE $(cluster_mode_label "${TFVARS_CLUSTER_MODE:-$cluster_mode}"))"
  echo -e "  • ${C_CYAN}gVisor Sandbox Isolation:${C_RESET} ${enable_gvisor}"
  echo -e "  • ${C_CYAN}AI Model Provider:${C_RESET} ${model_provider} (${model_default_name})"
  if [ "$model_provider" = "vertex_ai" ]; then
    echo -e "  • ${C_CYAN}Vertex AI Endpoint:${C_RESET} projects/${vertex_project_id}/locations/${vertex_location}"
  fi
  echo -e "  • ${C_CYAN}Permission Boundary:${C_RESET} ${permission_set}"
  echo -e "  • ${C_CYAN}Long-Term Memory:${C_RESET} ${memory_mode}"
  # Only shown for a mirrored install: on a default one both lines restate the
  # defaults. The second line is the one worth seeing before confirming, because
  # a mirror that covers only the first-party images fails at cert-manager, with
  # the cluster already built.
  if [ "$registry_prefix" != "$DEFAULT_REGISTRY_PREFIX" ] || [ -n "$third_party_registry_prefix" ]; then
    echo -e "  • ${C_CYAN}Container Registry:${C_RESET} ${registry_prefix}"
    echo -e "  • ${C_CYAN}Third-Party Images:${C_RESET} ${third_party_registry_prefix:-upstream registries (quay.io, ghcr.io, docker.io, us-docker.pkg.dev)}"
  fi
  if [ -n "$github_org" ] && [ -n "$github_repo" ]; then
    echo -e "  • ${C_CYAN}GitOps Infrastructure Repo:${C_RESET} https://github.com/${github_org}/${github_repo}"
  fi
  echo -e "${C_CYAN}${C_BOLD}"
  draw_separator
  echo -e "${C_RESET}"

  if [ "$PARAM_DRY_RUN" = "true" ]; then
    # A real resource preview, not just a config write: validate always, and
    # plan when Application Default Credentials exist. Local state only —
    # a dry run must not create the state bucket.
    print_info "Dry-run: validating the Terraform configuration (local state; nothing is created)."
    (
      cd "$(tf_compose_dir "$repo_dir")"
      terraform init -backend=false -input=false >/dev/null
      terraform validate >/dev/null
    )
    print_success "Terraform configuration is valid."
    if gcloud auth application-default print-access-token >/dev/null 2>&1; then
      print_info "Previewing the resources a real run would create (terraform plan)..."
      (
        cd "$(tf_compose_dir "$repo_dir")"
        terraform plan -input=false -lock=false
      )
    else
      print_warning "No Application Default Credentials; skipping the resource preview (terraform plan)."
      print_info "Run 'gcloud auth application-default login' for a full dry-run preview."
    fi
    print_success "Dry-run execution complete! Configuration generated without touching cloud resources."
    write_json_report "DRY_RUN_SUCCESS"
    exit 0
  fi

  if [ "$PARAM_NON_INTERACTIVE" != "true" ]; then
    local confirm_choice=""
    prompt_read "\nProceed with automated GKE cluster & Platform Agent provisioning? (Y/n)" confirm_choice "y"
    if [[ ! "$confirm_choice" =~ ^[Yy]$ ]]; then
      print_warning "Provisioning paused by user. Configuration saved to: $vars_file"
      print_info "To launch provisioning later, run: ${C_BOLD}cd terraform/examples/full-install && KUBE_AGENTS_STATE_BUCKET=auto ./lifecycle.sh apply${C_RESET}"
      write_json_report "PAUSED"
      exit 0
    fi
  fi

  # 12. Execute the Terraform Engine
  print_step "12. Applying the Install (Terraform + Helm)"
  print_info "Provisioning GCP APIs, GKE Cluster, cert-manager, Operator, LiteLLM gateway, and Platform Agent..."

  # Re-validate the GitOps org before spending an apply on it. The interview
  # already settled it interactively; this catches a vars.sh edited by hand
  # and the non-interactive flag path. Warns-only when GitHub is unreachable;
  # SKIP_GITHUB_ORG_CHECK=true bypasses it.
  check_github_org_is_organization "${GITHUB_ORG:-}"

  # The three script behaviours a data source cannot express: CMEK, the
  # Workload Identity pool, and NetworkPolicy enforcement on a cluster that
  # already exists. All are no-ops when the cluster does not exist yet or is
  # already configured.
  ensure_existing_cluster_cmek "$project_id" "$cluster_name" "$region"
  ensure_existing_cluster_workload_identity "$project_id" "$cluster_name" "$region"
  ensure_existing_cluster_network_policy "$project_id" "$cluster_name" "$region"

  # The App key import sits here — after the dry-run exit and the operator's
  # confirmation (it enables the KMS API, creates permanent key rings, and
  # uploads the key, none of which a preview or a declined run may do), and
  # before the apply, whose helm release waits on a minter that can only
  # pass readiness once the key is imported. The generator enabled the
  # minter on the promise of this import, so a failed one stops the run
  # here rather than wedging the apply.
  import_github_pem "$project_id" "$region"
  local minter_enabled_version=""
  minter_enabled_version="$({ gcloud kms keys versions list --key "${KMS_KEY:-github-token-minter-key}" \
    --keyring "${KMS_KEYRING:-github-token-minter-keyring}" \
    --location "$(derive_kms_location "$region")" --project "$project_id" \
    --filter='state=ENABLED' --format='value(name)' 2>/dev/null || true; } | head -1)"
  if grep -q '^enable_github_minter = true$' "$tfvars_file" 2>/dev/null && [ -z "$minter_enabled_version" ]; then
    print_error "The GitHub minter is enabled in the generated configuration, but its KMS signing key still has no ENABLED version — the apply would wait on a minter that can never become ready."
    print_info "Fix the App key import (see the messages above) and re-run, or unset GITHUB_APP_ID to install without the minter."
    exit 1
  fi

  local provisioning_log
  provisioning_log="/tmp/kube-agents-provision-$(date -u +%Y%m%dT%H%M%SZ).log"
  print_info "Provisioning output is also being saved to: ${C_BOLD}${provisioning_log}${C_RESET}"
  run_lifecycle_apply "$repo_dir" "$provisioning_log"

  # The one post-apply step Terraform cannot carry: the managed-OTel scope
  # (no provider field; the GitHub App key import runs BEFORE the apply,
  # since the minter's readiness depends on it and the apply waits on the
  # minter). The OTel scope is set only on a cluster this install created —
  # silently changing the telemetry scope of a cluster somebody else made is
  # not an install's call.
  if [ "${TFVARS_CREATE_CLUSTER:-true}" = "true" ]; then
    apply_managed_otel_scope "$project_id" "$cluster_name" "$region"
  else
    print_info "Existing cluster: leaving its managed-OTel scope untouched. Set it yourself if you want managed OTel collection: gcloud container clusters update $cluster_name --location $region --managed-otel-scope=COLLECTION_AND_INSTRUMENTATION_COMPONENTS"
  fi

  # 12. Workload & Pod Health Verification Checkpoint
  print_step "13. Verifying Workload & Pod Health"
  print_info "Verifying deployment rollouts in namespace 'kubeagents-system'..."
  GKE_DNS_ENDPOINT_FLAG=""
  gke_dns_endpoint_flag "$cluster_name" "$region" "$project_id" || true
  # shellcheck disable=SC2086
  gcloud container clusters get-credentials "$cluster_name" --location "$region" \
    --project "$project_id" $GKE_DNS_ENDPOINT_FLAG >/dev/null
  if ! kubectl get ns kubeagents-system >/dev/null 2>&1; then
    print_error "Namespace 'kubeagents-system' was not created. Installation is incomplete."
    exit 1
  fi
  local slow_rollouts=()
  # kube-agents-controller-manager, not kubeagents-: the chart prefixes the
  # operator Deployment with the release name.
  for deployment in kube-agents-controller-manager litellm platform-agent-gateway; do
    if ! wait_for_deployment_object "$deployment" kubeagents-system "$DEPLOYMENT_APPEAR_TIMEOUT_SECS"; then
      print_error "Expected deployment '$deployment' was not created within ${DEPLOYMENT_APPEAR_TIMEOUT_SECS}s."
      # platform-agent-gateway is the agent, and the sandbox is the one thing
      # that stops the operator writing it while leaving everything else
      # healthy: no gvisor RuntimeClass, no Deployment, and the reason is on the
      # CR rather than in any of the logs an operator would reach for first.
      if [ "$deployment" = "platform-agent-gateway" ] && [ "$enable_gvisor" = "true" ]; then
        print_info "The agent asks for the ${C_BOLD}gvisor${C_RESET} RuntimeClass; the operator will not create its Deployment until that RuntimeClass exists."
        print_info "Read the reason with: ${C_BOLD}kubectl get platformagent -n kubeagents-system -o jsonpath='{.items[*].status.conditions}'${C_RESET}"
        print_info "Re-run with ${C_BOLD}--gvisor=false${C_RESET} to run the agent on the standard container runtime instead."
      fi
      exit 1
    fi
    # The agent pulls a large image and waits on LiteLLM before it reports ready,
    # so a couple of minutes is normal. Running past the budget means "still
    # coming up", not "broken": say so and keep the summary below, which carries
    # the chat links and port-forward command.
    if ! wait_for_rollout "$deployment" kubeagents-system "$ROLLOUT_TIMEOUT_SECS"; then
      slow_rollouts+=("$deployment")
      print_warning "$deployment did not report ready within ${ROLLOUT_TIMEOUT_SECS}s."
    fi
  done
  if [ "${#slow_rollouts[@]}" -eq 0 ]; then
    print_success "All core control plane deployments are healthy and available!"
    write_json_report "SUCCESS"
  else
    print_warning "Still waiting on: ${slow_rollouts[*]}"
    print_info "Keep watching with: ${C_BOLD}kubectl rollout status deployment/${slow_rollouts[0]} -n kubeagents-system${C_RESET}"
    print_info "Inspect a stuck pod with: ${C_BOLD}kubectl describe pod -l app=${slow_rollouts[0]} -n kubeagents-system${C_RESET}"
    write_json_report "SUCCESS_PENDING_ROLLOUT"
  fi

  # 13. Installation Summary & Next Steps
  print_step "🎉 Installation Complete!"
  echo -e "${C_GREEN}${C_BOLD}"
  echo '============================================================================='
  echo '🏆  Kubernetes Agentic Harness (kube-agents) is Live & Operational!'
  echo '============================================================================='
  echo -e "${C_RESET}"

  echo -e "${C_BOLD}Component Status Summary:${C_RESET}"
  echo -e "  • ${C_CYAN}GCP Project:${C_RESET} ${project_id} (Project Number: ${project_number})"
  echo -e "  • ${C_CYAN}GKE Cluster:${C_RESET} ${cluster_name} (${region}, GKE $(cluster_mode_label "${TFVARS_CLUSTER_MODE:-$cluster_mode}"))"
  echo -e "  • ${C_CYAN}Runtime Isolation:${C_RESET} ${enable_gvisor:-false} (gVisor Sandbox)"
  echo -e "  • ${C_CYAN}Model Provider:${C_RESET} ${model_provider} (${model_default_name})"
  echo -e "  • ${C_CYAN}Permission Mode:${C_RESET} ${permission_set}"
  if [ "${google_chat_enabled:-false}" = "true" ]; then
    echo -e "  • ${C_CYAN}Google Chat Direct Bot Link:${C_RESET} ${C_UNDERLINE}https://chat.google.com/dm/${project_number}${C_RESET}"
    echo -e "  • ${C_CYAN}Google Chat App Console:${C_RESET} ${C_UNDERLINE}https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project=${project_id}${C_RESET}"
  fi
  if [ "${slack_enabled:-false}" = "true" ]; then
    echo -e "  • ${C_CYAN}Slack App Link:${C_RESET} ${C_UNDERLINE}https://app.slack.com/client${C_RESET}"
  fi
  if [ "${PARAM_ENABLE_WEBUI:-false}" = "true" ] || [ "${HERMES_DASHBOARD_ENABLED:-false}" = "true" ]; then
    echo -e "  • ${C_CYAN}Hermes Web UI (Port 9119):${C_RESET} ${C_GREEN}Enabled${C_RESET}"
    # A sandboxed pod cannot be reached with `kubectl port-forward`: the forward
    # is established in the host-side CNI netns while the dashboard listens in
    # the sandbox's own network stack, so the connection is refused. The relay
    # in scripts/exec_tunnel.py goes through `kubectl exec` instead; print
    # whichever one will actually work here.
    if [ "${enable_gvisor:-false}" = "true" ]; then
      echo -e "    ${C_YELLOW}Workstation Access Command:${C_RESET} ${repo_dir}/scripts/hermes-dashboard-tunnel.py"
      echo -e "      (the agent is sandboxed under gVisor, which kubectl port-forward cannot reach)"
    else
      echo -e "    ${C_YELLOW}Workstation Access Command:${C_RESET} kubectl port-forward deploy/platform-agent-gateway -n kubeagents-system 9119:9119"
    fi
    echo -e "    ${C_YELLOW}Browser Dashboard URL:${C_RESET} ${C_UNDERLINE}http://localhost:9119${C_RESET}"
  fi

  if [ "${google_chat_enabled:-false}" = "true" ]; then
    echo ""
    IMAGE_TAG="$image_tag" bash "${repo_dir}/k8s-operator/scripts/print_instructions_gchat.sh" || true
  fi
  if [ "${slack_enabled:-false}" = "true" ]; then
    echo ""
    IMAGE_TAG="$image_tag" bash "${repo_dir}/k8s-operator/scripts/print_instructions_slack.sh" || true
  fi
}

if [ "${KUBE_AGENTS_SOURCE_ONLY:-false}" != "true" ]; then
  main "$@"
else
  echo "ℹ️ Sourced install.sh functions without executing main (KUBE_AGENTS_SOURCE_ONLY=true)." >&2
fi
