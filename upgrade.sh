#!/usr/bin/env bash
# ==============================================================================
# 🔄 Kubernetes Agentic Harness (kube-agents) Lifecycle Upgrade Engine
# ==============================================================================
# Modular CLI tool for Day-2 upgrades of the Platform Agent harness and operator.
#
# Usage:
#   ./upgrade.sh [options]
#   curl -fsSL https://gke-labs.github.io/kube-agents/upgrade.sh | bash -s -- \
#     --upgrade-mode=full --image-tag=<SEMVER_TAG_OR_FULL_COMMIT_SHA>
#
# Run this from the directory holding your original install checkout: the
# upgrade refuses to re-render cluster configuration without the install's
# install.env (a legacy k8s-operator/scripts/vars.sh also satisfies it).
# ==============================================================================

set -Eeuo pipefail

# ANSI Color Tokens
C_CYAN="\033[1;36m"
C_GREEN="\033[1;32m"
C_YELLOW="\033[1;33m"
C_RED="\033[1;31m"
C_BOLD="\033[1m"
C_RESET="\033[0m"

# Sourced/baked release version. On developer checkouts (main), this is empty.
# Release automation stamps this value (e.g. BAKED_RELEASE_VERSION="0.2.0") when publishing a GA release.
BAKED_RELEASE_VERSION=""

# Default CLI Configuration
PARAM_UPGRADE_MODE="full"
PARAM_NON_INTERACTIVE="false"
PARAM_DRY_RUN="false"
# --plan and --dry-run are both previews and are deliberately not the same one.
# --dry-run answers offline, from configuration alone, and never contacts the
# install. --plan answers from the install's real Terraform state, so it needs
# credentials and it is the only one of the two that can tell you an
# environment has drifted from the composition on main.
PARAM_PLAN="false"
PARAM_KEEP_IMAGE_TAG="false"
PARAM_PROJECT_ID=""
PARAM_CLUSTER_NAME=""
PARAM_REGION=""
PARAM_IMAGE_TAG="${IMAGE_TAG:-${BAKED_RELEASE_VERSION:-}}"
TEMP_REPO_DIR=""

cleanup() {
  if [ -n "$TEMP_REPO_DIR" ] && [ -d "$TEMP_REPO_DIR" ]; then
    rm -rf -- "$TEMP_REPO_DIR"
  fi
}
trap cleanup EXIT

on_error() {
  local exit_code="$1"
  local line_no="$2"
  local bash_cmd="$3"
  echo -e "\n${C_RED}${C_BOLD}✗ Upgrade error encountered at line ${line_no} (exit code ${exit_code}): ${bash_cmd}${C_RESET}" >&2
  write_report "FAILED" 2>/dev/null || true
  # A tfvars the generator was midway through writing is mode 600, carries
  # every secret this run was given, and is named one character from the file
  # the next reader would open. write_tfvars_from_state publishes the path
  # while the write is in flight and clears it after the mv.
  if [ -n "${TFVARS_TMP_FILE:-}" ] && [ -f "${TFVARS_TMP_FILE}" ]; then
    rm -f -- "${TFVARS_TMP_FILE}"
  fi
  exit "$exit_code"
}
trap 'on_error $? $LINENO "$BASH_COMMAND"' ERR

print_banner() {
  echo -e "${C_CYAN}${C_BOLD}"
  echo '==========================================================================='
  echo '🔄  Kubernetes Agentic Harness (kube-agents) Lifecycle Upgrade Engine'
  echo '==========================================================================='
  echo -e "${C_RESET}"
}

print_step() {
  echo -e "\n${C_CYAN}${C_BOLD}>>> $1 <<<${C_RESET}"
}

print_info() {
  echo -e "  ${C_CYAN}ℹ $1${C_RESET}"
}

print_success() {
  echo -e "  ${C_GREEN}✓ $1${C_RESET}"
}

print_warning() {
  echo -e "  ${C_YELLOW}⚠ $1${C_RESET}"
}

print_error() {
  echo -e "  ${C_RED}✗ $1${C_RESET}"
}

show_help() {
  print_banner
  cat << EOF
Usage: ./upgrade.sh [OPTIONS]

Options:
  --upgrade-mode, -m MODE  Upgrade mode: full, harness, operator (Default: full)
  --non-interactive, -y    Automated execution mode (no interactive prompts)
  --plan                   Report what a full upgrade would change, against the
                           install's real Terraform state. Changes nothing.
                           Exit 0 = in sync, 2 = there are changes, 1 = error.
  --dry-run                Preview upgrade plan and configuration state without touching cloud resources
  --project-id ID          GCP Target Project ID
  --cluster-name NAME      GKE Target Cluster Name
  --region REGION          GKE GCP Region
  --image-tag TAG          Validated immutable release tag or full commit SHA (required)
  --keep-image-tag         Upgrade everything except the images, leaving them on
                           the tag the install already serves. Use instead of
                           --image-tag, not alongside it.
  --help, -h               Show this help message

Examples:
  # Perform full atomic upgrade of harness, operator, and skills
  ./upgrade.sh --non-interactive --project-id="my-gcp-project" --cluster-name="platform-agent-host"

  # Dry-run upgrade preview
  ./upgrade.sh --dry-run --upgrade-mode=full

  # What has this install drifted from? Holds the image tag at the one
  # Terraform state records, so the report is composition drift, not image lag.
  ./upgrade.sh --plan
EOF
}

# The image tag an install is currently serving, read off the agent Deployment.
#
# The Deployment rather than the Helm release: `helm get values` reports what
# the last upgrade was ASKED for, and on these environments the last upgrade was
# a `--reset-then-reuse-values` re-tag whose recorded values are the install-day
# blob. The Deployment reports what is running.
running_image_tag() {
  local namespace="$1" image=""
  # Selected by name, not by index. The operator builds this list and appends
  # the dashboard and fluent-bit after the agent, so a positional read is one
  # reordering away from pinning the composition's image_tag to a sidecar's
  # version — on a scheduled apply, silently.
  image="$(kubectl get deployment platform-agent-gateway -n "$namespace" \
    -o jsonpath='{.spec.template.spec.containers[?(@.name=="platform-agent")].image}' \
    2>/dev/null || true)"
  [ -n "$image" ] || return 0
  # Everything after the last colon, unless that colon belongs to a registry
  # port (no slash may follow it).
  case "${image##*:}" in
    */*) return 0 ;;
    *) printf '%s\n' "${image##*:}" ;;
  esac
}

validate_immutable_ref() {
  local ref="${1:-}"
  if [ -z "$ref" ]; then
    print_error "--image-tag is required; use a validated release tag or full commit SHA."
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

json_escape() {
  local value="${1:-}"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '%s' "$value"
}

# Persist one variable into a legacy vars.sh, for an install that still has
# one. Nothing in this repository re-sources it -- the exports after each call
# are what this run reads -- but a tool still pointed at the old file would
# otherwise name a different target than the one this run acts on.
persist_state_var() {
  local state_file="$1"
  local var_name="$2"
  local var_value="$3"
  if [ -f "$state_file" ]; then
    grep -E -v "^[[:space:]]*export[[:space:]]+${var_name}=" "$state_file" > "${state_file}.tmp" || true
    mv "${state_file}.tmp" "$state_file"
  fi
  printf 'export %s=%q\n' "$var_name" "$var_value" >> "$state_file"
  chmod 600 "$state_file" 2>/dev/null || true
}

random_hex_32() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    # head reads a fixed count from a file, so no SIGPIPE reaches the producer
    # and `set -o pipefail` stays satisfied.
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

# Add the pod-scoped Session KV keys to an existing Secret that predates them.
#
# A fresh install generates these (the composition's random_password
# resources), and the harness/operator fast paths never touch
# platform-agent-secrets — `helm upgrade
# --reset-then-reuse-values` re-tags images and nothing else, so a Secret from an old
# enough install keeps missing the keys until something adds them. The
# operator marks both Secret references optional, so
# a Secret without the keys yields containers without the variables rather than
# a failed mount — and the k8s-event-watcher treats an empty --token-env
# variable as fatal, so it exits on every start and NO cluster events are
# watched from that moment on, in a container that stays Ready throughout. The
# Session KV server answering 503 and unstable pseudonyms are the visible half;
# the dead watcher is the half that needs this backfill.
#
# Additive only. An existing value is never rewritten: rotating SESSION_KV_SALT
# re-anonymises every user, severing their past sessions from their future ones.
SESSION_KV_KEYS_PATCHED="false"
backfill_session_kv_keys() {
  local namespace="$1"
  local secret_name="platform-agent-secrets"

  if ! kubectl get secret "$secret_name" -n "$namespace" >/dev/null 2>&1; then
    print_warning "Secret '$secret_name' not found in '$namespace'; skipping the Session KV key backfill."
    print_info "Whatever manages that Secret (Helm with credentials.create, Terraform, or your own secret store) must supply SESSION_KV_API_KEY and SESSION_KV_SALT."
    return 0
  fi

  local key existing
  for key in SESSION_KV_API_KEY SESSION_KV_SALT; do
    existing="$(kubectl get secret "$secret_name" -n "$namespace" -o jsonpath="{.data.$key}" 2>/dev/null || echo "")"
    if [ -n "$existing" ]; then
      print_info "$key is already present; leaving it untouched."
      continue
    fi
    print_info "Generating the missing $key into Secret '$secret_name'..."
    kubectl patch secret "$secret_name" -n "$namespace" --type=merge \
      -p "{\"stringData\":{\"$key\":\"$(random_hex_32)\"}}" >/dev/null
    SESSION_KV_KEYS_PATCHED="true"
  done

  if [ "$SESSION_KV_KEYS_PATCHED" = "true" ]; then
    print_success "Session KV keys backfilled; the event watcher and Session KV server can authenticate after the rollout."
  fi
}

# Add the shell sandbox's SSH keypair to an install that predates it.
#
# Same additive contract as the Session KV backfill above and for a sharper
# reason: the sandbox copies authorized_keys into place once at startup, so
# replacing a keypair that is already in use locks the agent out of its own
# shell until that pod restarts. An existing pair is therefore never rewritten,
# and a half-written pair (one key present, the other missing) is treated as
# absent rather than patched around — a private key whose public half was lost
# authenticates nothing.
#
# Only platform-agent-secrets is written here. The sandbox mounts a Secret of
# its own, <name>-shell-authorized-keys, and the chart renders that one from
# whatever public half it finds in platform-agent-secrets — so the step 4 helm
# upgrade below picks the pair up on its own. Writing it here too would create
# the object without Helm's ownership metadata, and that same helm upgrade would
# then refuse to adopt it. The sandbox must not mount platform-agent-secrets
# itself; see docs/designs/agent-shell-sandboxing.md#key-management.
#
# Set when this run wrote the pair, so the caller can roll the agent: the
# private half reaches the gateway pod through a Secret volume that an init
# container copies into place once at startup, so a pod that started before the
# backfill keeps the empty directory it was given until it restarts.
SANDBOX_KEYS_PATCHED="false"

# How long step 5 waits for the sandbox and the credential proxy. Longer than
# the gateway's because the sandbox is a StatefulSet with a ReadWriteOnce
# volume: the new pod cannot attach until the old one has detached, so its
# rollout serialises where a Deployment's overlaps.
SANDBOX_ROLLOUT_TIMEOUT="180s"

backfill_sandbox_ssh_key() {
  local namespace="$1"
  local secret_name="platform-agent-secrets"

  if ! command -v ssh-keygen >/dev/null 2>&1; then
    print_warning "ssh-keygen not found; skipping the shell sandbox keypair backfill."
    return 0
  fi
  if ! kubectl get secret "$secret_name" -n "$namespace" >/dev/null 2>&1; then
    print_warning "Secret '$secret_name' not found in '$namespace'; skipping the shell sandbox keypair backfill."
    return 0
  fi

  local existing_private existing_public
  existing_private="$(kubectl get secret "$secret_name" -n "$namespace" -o jsonpath='{.data.SANDBOX_SSH_PRIVATE_KEY}' 2>/dev/null || echo "")"
  existing_public="$(kubectl get secret "$secret_name" -n "$namespace" -o jsonpath='{.data.SANDBOX_SSH_PUBLIC_KEY}' 2>/dev/null || echo "")"
  if [ -n "$existing_private" ] && [ -n "$existing_public" ]; then
    print_info "The shell sandbox keypair is already present; leaving it untouched."
    return 0
  fi

  print_info "Generating the missing shell sandbox SSH keypair into Secret '$secret_name'..."
  local key_dir old_umask
  old_umask="$(umask)"
  umask 077
  key_dir="$(mktemp -d)"
  umask "$old_umask"
  if ! ssh-keygen -q -t ed25519 -N '' -C "kube-agents-shell-sandbox" -f "$key_dir/id_ed25519"; then
    rm -rf "$key_dir"
    print_warning "ssh-keygen failed; skipping the shell sandbox keypair backfill."
    return 0
  fi

  # Patching `data` with base64 rather than `stringData` with the raw key, which
  # is what the Session KV backfill above does: a PEM contains newlines, and
  # this patch is built by string interpolation into JSON. base64's alphabet
  # needs no escaping, so there is nothing here for a newline to break. `tr`
  # because macOS base64 has no -w0.
  local priv_b64 pub_b64
  priv_b64="$(base64 < "$key_dir/id_ed25519" | tr -d '\n')"
  pub_b64="$(base64 < "$key_dir/id_ed25519.pub" | tr -d '\n')"
  rm -rf "$key_dir"
  kubectl patch secret "$secret_name" -n "$namespace" --type=merge \
    -p "{\"data\":{\"SANDBOX_SSH_PRIVATE_KEY\":\"$priv_b64\",\"SANDBOX_SSH_PUBLIC_KEY\":\"$pub_b64\"}}" >/dev/null

  SANDBOX_KEYS_PATCHED="true"
  print_success "Shell sandbox keypair backfilled into '$secret_name'; the upgrade below renders the sandbox's authorized_keys from it."
}

matches_release_bundle_ref() {
  local repo_dir="$1"
  local expected_ref="$2"
  local bundle_file="${repo_dir}/.release-bundle"

  if [ -f "$bundle_file" ]; then
    local bundle_version bundle_tag
    bundle_version="$(grep -E "^version=" "$bundle_file" 2>/dev/null | cut -d'=' -f2- | tr -d '[:space:]' || echo "")"
    bundle_tag="$(grep -E "^tag=" "$bundle_file" 2>/dev/null | cut -d'=' -f2- | tr -d '[:space:]' || echo "")"
    if [ -n "$bundle_version" ] && { [ "$bundle_version" = "$expected_ref" ] || [ "$bundle_tag" = "$expected_ref" ]; }; then
      echo "$bundle_version"
      return 0
    fi
  fi
  return 1
}

# The two refusals that do not need a ref to make sense: an unversioned source
# directory, and a dirty one. Split out of verify_local_source_ref because a
# tagless run still applies this checkout's Terraform and charts to a live
# install -- so skipping the ref COMPARISON, which is the only part a missing
# tag actually makes impossible, must not take these with it. Without this,
# `--keep-image-tag` would apply uncommitted local edits to an environment and
# say nothing, which is the invisible drift #1117 exists to end.
#
# The previews are warned rather than refused. --dry-run and --plan change
# nothing, and a plan of what the working tree WOULD apply is a reasonable thing
# to want from a tree that is mid-edit; refusing it would take away the one
# command that answers "what have I changed here".
verify_local_source_clean() {
  local repo_dir="$1" preview="false"
  if [ "$PARAM_DRY_RUN" = "true" ] || [ "$PARAM_PLAN" = "true" ]; then
    preview="true"
  fi

  if ! git -C "$repo_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if [ -n "${BAKED_RELEASE_VERSION:-}" ]; then
      return 0
    fi
    if [ "$preview" = "true" ]; then
      print_warning "Cannot verify the source directory because '$repo_dir' is not a Git worktree."
      return 0
    fi
    print_error "Refusing to upgrade from an unversioned source directory: $repo_dir"
    return 1
  fi

  if [ -n "$(git -C "$repo_dir" status --porcelain --untracked-files=no)" ]; then
    if [ "$preview" = "true" ]; then
      print_warning "This preview is using uncommitted source changes; a real upgrade would require a clean checkout."
      return 0
    fi
    print_error "Refusing to upgrade from a dirty checkout: its Terraform, charts and scripts match no commit, so what this run would apply exists nowhere else. Commit or stash, or use --plan to preview."
    return 1
  fi
  print_success "Verified the upgrade sources are a clean checkout of $(git -C "$repo_dir" rev-parse --short HEAD)."
}

verify_local_source_ref() {
  local repo_dir="$1"
  local expected_ref="$2"

  if ! git -C "$repo_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # In official stamped release archives (unpacked tarball/zip outside Git),
    # BAKED_RELEASE_VERSION is stamped during release automation.
    if [ -n "${BAKED_RELEASE_VERSION:-}" ] && [ "${BAKED_RELEASE_VERSION}" = "${expected_ref}" ]; then
      local bundle_version=""
      if bundle_version="$(matches_release_bundle_ref "$repo_dir" "$expected_ref")"; then
        print_success "Verified upgrade sources match official release bundle ${bundle_version}."
        return 0
      fi
      print_success "Verified upgrade sources match baked official release ${BAKED_RELEASE_VERSION}."
      return 0
    fi
    if [ "$PARAM_DRY_RUN" = "true" ]; then
      print_warning "Dry-run cannot verify source/image alignment because '$repo_dir' is not a Git worktree."
      return 0
    fi
    print_error "Refusing to upgrade from an unversioned source directory: $repo_dir"
    return 1
  fi

  local expected_commit current_commit
  if ! expected_commit="$(git -C "$repo_dir" rev-parse --verify "${expected_ref}^{commit}" 2>/dev/null)"; then
    print_error "The requested image/source ref '$expected_ref' is not present in the current checkout. Check out that exact revision first."
    return 1
  fi
  current_commit="$(git -C "$repo_dir" rev-parse HEAD)"
  if [ "$current_commit" != "$expected_commit" ]; then
    print_error "Source/image version mismatch: checkout is ${current_commit}, requested ref resolves to ${expected_commit}."
    return 1
  fi
  if [ -n "$(git -C "$repo_dir" status --porcelain --untracked-files=no)" ]; then
    if [ "$PARAM_DRY_RUN" = "true" ]; then
      print_warning "Dry-run is using uncommitted source changes; a real upgrade would require a clean checkout."
    else
      print_error "Refusing to upgrade from a dirty checkout because its scripts do not exactly match '$expected_ref'."
      return 1
    fi
  fi
  print_success "Verified upgrade scripts and image ref resolve to commit ${expected_commit}."
}

# Parameter Parsing
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --upgrade-mode=*|-m=*) PARAM_UPGRADE_MODE="${1#*=}"; shift ;;
      --upgrade-mode|-m) PARAM_UPGRADE_MODE="$2"; shift 2 ;;
      --non-interactive|-y) PARAM_NON_INTERACTIVE="true"; shift ;;
      --plan) PARAM_PLAN="true"; shift ;;
      --keep-image-tag) PARAM_KEEP_IMAGE_TAG="true"; shift ;;
      --dry-run) PARAM_DRY_RUN="true"; shift ;;
      --project-id=*) PARAM_PROJECT_ID="${1#*=}"; shift ;;
      --project-id) PARAM_PROJECT_ID="$2"; shift 2 ;;
      --cluster-name=*) PARAM_CLUSTER_NAME="${1#*=}"; shift ;;
      --cluster-name) PARAM_CLUSTER_NAME="$2"; shift 2 ;;
      --region=*) PARAM_REGION="${1#*=}"; shift ;;
      --region) PARAM_REGION="$2"; shift 2 ;;
      --image-tag=*) PARAM_IMAGE_TAG="${1#*=}"; shift ;;
      --image-tag) PARAM_IMAGE_TAG="$2"; shift 2 ;;
      --help|-h) show_help; exit 0 ;;
      *) print_error "Unknown parameter: $1"; show_help >&2; return 2 ;;
    esac
  done
}

write_report() {
  local status="$1"
  local report_file="/tmp/kube-agents-upgrade-report.json"
  cat << EOF > "$report_file"
{
  "status": "$(json_escape "$status")",
  "upgrade_mode": "$(json_escape "$PARAM_UPGRADE_MODE")",
  "dry_run": ${PARAM_DRY_RUN},
  "non_interactive": ${PARAM_NON_INTERACTIVE},
  "target_image_tag": "$(json_escape "$PARAM_IMAGE_TAG")",
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "2026-08-05T00:00:00Z")"
}
EOF
  print_success "Upgrade report written to: $report_file"
}

# Runs lifecycle.sh from the composition directory with the install's Terraform
# state coordinates in the environment.
#
# One function rather than the same subshell written out at each call site: the
# plan and the apply must not be able to come to disagree about which state they
# are talking to. It also keeps `cd` and the two exports from leaking into the
# rest of the run -- and keeps shellcheck's SC2030/SC2031 quiet, which matters
# because CI runs a bare `shellcheck upgrade.sh` and fails on info severity.
run_lifecycle() {
  local composition_dir="$1"
  shift
  (
    cd "$composition_dir" || return 1
    KUBE_AGENTS_STATE_BUCKET="${KUBE_AGENTS_STATE_BUCKET:-auto}"
    KUBE_AGENTS_STATE_PREFIX="$(tf_state_prefix)"
    export KUBE_AGENTS_STATE_BUCKET KUBE_AGENTS_STATE_PREFIX
    ./lifecycle.sh "$@"
  )
}

main() {
  parse_args "$@"
  print_banner

  # --image-tag may be omitted, and for a plan it usually should be. The tag is
  # then read off the running install further down, which separates the two
  # things a run could be about: an install whose IMAGES are behind main
  # (visible, expected, and what the redeploy workflows exist to fix) and one
  # whose INFRASTRUCTURE is behind main (invisible — #1117).
  #
  # An UPGRADE can ask for the same thing, but only by saying so:
  # --keep-image-tag means "converge everything except the images". That is
  # what a scheduled reconcile of autopush wants, because autopush tracks
  # main's tip through GHCR publishes and pinning it to whichever commit the
  # reconcile ran from would roll its images BACKWARDS to that commit.
  #
  # A flag rather than "empty means keep", because empty already means
  # something: it is the shape of a CI job whose IMAGE_TAG variable did not
  # resolve, and that has to stay the hard error it has always been.
  if [ -z "$PARAM_IMAGE_TAG" ] && [ "$PARAM_KEEP_IMAGE_TAG" = "true" ]; then
    print_info "--keep-image-tag: this run keeps the tag the install is already serving."
  elif [ -z "$PARAM_IMAGE_TAG" ] && [ "$PARAM_PLAN" = "true" ]; then
    print_info "No --image-tag given; the plan will use the tag this install is already running."
  elif [ -z "$PARAM_IMAGE_TAG" ]; then
    if [ "$PARAM_NON_INTERACTIVE" = "true" ]; then
      print_error "--image-tag is required; use a validated release tag or full commit SHA."
      exit 1
    fi
    if [ -c /dev/tty ] && ( : </dev/tty ) 2>/dev/null; then
      printf '%b' "  ${C_CYAN}Target image tag (validated release tag or full commit SHA): ${C_RESET}" >/dev/tty
      read -r PARAM_IMAGE_TAG </dev/tty
      # A bare Enter is still the empty tag this arm exists to reject, and
      # nothing further down catches it: validate_immutable_ref, whose first
      # branch rejects an empty ref, runs only when a tag is present. Without
      # this, pressing Enter would skip verify_local_source_ref and silently
      # become --keep-image-tag.
      if [ -z "$PARAM_IMAGE_TAG" ]; then
        print_error "--image-tag is required; use a validated release tag or full commit SHA. To upgrade everything except the images, pass --keep-image-tag."
        exit 1
      fi
    else
      print_error "--image-tag is required when no interactive terminal is available (e.g. curl | bash)."
      exit 1
    fi
  fi
  if [ -n "$PARAM_IMAGE_TAG" ] && [ "$PARAM_KEEP_IMAGE_TAG" = "true" ]; then
    print_error "--keep-image-tag and --image-tag ask for opposite things. Pass one."
    exit 1
  fi
  if [ -n "$PARAM_IMAGE_TAG" ]; then
    validate_immutable_ref "$PARAM_IMAGE_TAG"
  fi

  case "$PARAM_UPGRADE_MODE" in
    full|harness|operator) ;;
    *) print_error "Unsupported upgrade mode '$PARAM_UPGRADE_MODE'. Use full, harness, or operator."; exit 1 ;;
  esac

  # A tagless run cannot fetch its own engine — there is no tag to fetch — and
  # cannot compare the checkout against a ref it was not given. Both are things
  # the tag makes possible rather than things the run needs.
  #
  # What the tag does NOT excuse is the state of the checkout itself: a tagless
  # run still applies this directory's Terraform and charts to a live install,
  # so verify_local_source_clean runs either way and only the ref comparison is
  # conditional.
  local script_dir repo_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -f "${script_dir}/scripts/installer/installer_common.sh" ]; then
    repo_dir="$script_dir"
    if [ -n "$PARAM_IMAGE_TAG" ]; then
      verify_local_source_ref "$repo_dir" "$PARAM_IMAGE_TAG"
    else
      verify_local_source_clean "$repo_dir"
    fi
  elif [ -f "$(pwd)/scripts/installer/installer_common.sh" ]; then
    repo_dir="$(pwd)"
    if [ -n "$PARAM_IMAGE_TAG" ]; then
      verify_local_source_ref "$repo_dir" "$PARAM_IMAGE_TAG"
    else
      verify_local_source_clean "$repo_dir"
    fi
  elif [ -z "$PARAM_IMAGE_TAG" ]; then
    print_error "--plan and --keep-image-tag have to run from a kube-agents checkout: without --image-tag there is no ref to fetch the engine at."
    exit 1
  else
    TEMP_REPO_DIR="$(mktemp -d)"
    repo_dir="${TEMP_REPO_DIR}/kube-agents"
    print_info "Fetching the upgrade engine for '${PARAM_IMAGE_TAG}'..."
    git clone --filter=blob:none --no-checkout https://github.com/gke-labs/kube-agents.git "$repo_dir"
    git -C "$repo_dir" fetch --depth=1 origin "$PARAM_IMAGE_TAG"
    git -C "$repo_dir" checkout --detach FETCH_HEAD
    verify_local_source_ref "$repo_dir" "$PARAM_IMAGE_TAG"
  fi

  print_step "1. Validating Upgrade Target & Environment"
  print_info "Upgrade Mode: ${C_BOLD}${PARAM_UPGRADE_MODE}${C_RESET}"
  print_info "Target Image Tag: ${C_BOLD}${PARAM_IMAGE_TAG}${C_RESET}"

  local required_tools=(gcloud kubectl helm)
  if [ "$PARAM_UPGRADE_MODE" = "full" ]; then
    required_tools+=(terraform)
  fi
  local tool
  for tool in "${required_tools[@]}"; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      print_error "Required CLI tool '$tool' is not installed."
      exit 1
    fi
  done

  # Shared defaults, the install.env loader, and the terraform.tfvars generator.
  # Sourced here rather than just before the generator, because the state load
  # below needs load_install_env. Print helpers are already defined above, as
  # the file expects.
  # shellcheck disable=SC1091
  source "${repo_dir}/scripts/installer/installer_common.sh"

  # Two sources, in this order, so the hand-authored input wins: a legacy
  # vars.sh from an install that predates install.env, then install.env over
  # the top of it. Either one on its own is enough to upgrade.
  local state_file="${repo_dir}/k8s-operator/scripts/vars.sh"
  local install_env_file
  install_env_file="$(default_install_env_file "$repo_dir")"
  local state_loaded="false"
  if [ -f "$state_file" ]; then
    # shellcheck disable=SC1090,SC1091
    if ! source "$state_file"; then
      print_error "Configuration state is invalid and could not be loaded."
      exit 1
    fi
    state_loaded="true"
    print_success "Loaded existing configuration state from k8s-operator/scripts/vars.sh"
  fi
  if load_install_env "$install_env_file"; then
    state_loaded="true"
    print_success "Loaded install configuration from: ${install_env_file}"
  fi
  if [ "$state_loaded" != "true" ]; then
    print_warning "No install configuration (install.env) and no saved state (k8s-operator/scripts/vars.sh) was found in ${repo_dir}."
  fi
  # GITOPS_ORG / GITOPS_REPO are the names; a configuration still carrying
  # GITHUB_ORG / GITHUB_REPO is accepted with a warning. Runs after the load and
  # before anything reads the coordinates.
  normalize_gitops_repo_vars
  # Same shape, for the memory setting: install.env records MEMORY, a migrated
  # vars.sh still carries the old MEMORY_PROVIDER, and the file loaded second
  # has to win.
  normalize_memory_vars

  local target_project="${PARAM_PROJECT_ID:-${PROJECT_ID:-}}"
  local target_cluster="${PARAM_CLUSTER_NAME:-${CLUSTER_NAME:-$DEFAULT_CLUSTER_NAME}}"
  local target_region="${PARAM_REGION:-${REGION:-$DEFAULT_REGION}}"

  if [ -z "$target_project" ]; then
    target_project="$(gcloud config get-value project 2>/dev/null || true)"
  fi
  if [ -z "$target_project" ]; then
    print_error "A GCP project is required. Pass --project-id or configure one with gcloud."
    exit 1
  fi

  print_info "GCP Target Project: ${C_BOLD}${target_project}${C_RESET}"
  print_info "GKE Target Cluster: ${C_BOLD}${target_cluster}${C_RESET} (${target_region})"

  if [ "$PARAM_DRY_RUN" = "true" ] && [ "$PARAM_PLAN" = "true" ]; then
    print_error "--dry-run and --plan are different previews and cannot be combined: --dry-run answers offline from configuration, --plan answers from the install's Terraform state."
    exit 1
  fi

  if [ "$PARAM_DRY_RUN" = "true" ]; then
    print_step "2. Dry-Run Upgrade Plan Preview"
    echo -e "  • ${C_CYAN}Action:${C_RESET} Perform ${PARAM_UPGRADE_MODE} upgrade on cluster '${target_cluster}'"
    echo -e "  • ${C_CYAN}Image Overrides:${C_RESET} ${REGISTRY_PREFIX:-$DEFAULT_REGISTRY_PREFIX}/*:${PARAM_IMAGE_TAG}"
    echo -e "  • ${C_CYAN}Secrets:${C_RESET} generate SESSION_KV_API_KEY / SESSION_KV_SALT into 'platform-agent-secrets' only if absent (existing values are never rewritten)"
    echo -e "  • ${C_CYAN}Secrets:${C_RESET} generate the shell sandbox SSH keypair into 'platform-agent-secrets' and 'platform-agent-shell-authorized-keys' only if absent"
    write_report "DRY_RUN_COMPLETE"
    exit 0
  fi

  # Fail closed without any configuration: the upgrade re-renders the
  # PlatformAgent Custom Resource from it, so upgrading without it would
  # silently reset chat, allowed users, dashboard, and model-provider
  # configuration to blank defaults.
  if [ "$state_loaded" != "true" ]; then
    print_error "Refusing to upgrade without the installation's configuration."
    print_info "Run upgrade.sh from the directory holding the install's install.env, point KUBE_AGENTS_INSTALL_ENV at one, or restore k8s-operator/scripts/vars.sh."
    exit 1
  fi

  # Keep a legacy vars.sh agreeing with the confirmed target, the way
  # uninstall.sh does, so no tool still pointed at it names another cluster.
  #
  # Only into a vars.sh that is already there, the way uninstall.sh guards the
  # same three calls. persist_state_var's append is unconditional -- only its
  # grep/mv rewrite tests for the file -- so on an install.env-only install the
  # redirect would open a path under k8s-operator/scripts/, a directory this
  # release no longer creates, and `set -Eeuo pipefail` would abort the upgrade
  # at step 1. The exports below are what the rest of this run actually reads.
  if [ -f "$state_file" ]; then
    if [ -n "$PARAM_PROJECT_ID" ]; then
      persist_state_var "$state_file" PROJECT_ID "$target_project"
    fi
    if [ -n "$PARAM_CLUSTER_NAME" ]; then
      persist_state_var "$state_file" CLUSTER_NAME "$target_cluster"
    fi
    if [ -n "$PARAM_REGION" ]; then
      persist_state_var "$state_file" REGION "$target_region"
    fi
  fi
  export PROJECT_ID="$target_project"
  export CLUSTER_NAME="$target_cluster"
  export REGION="$target_region"

  print_step "2. Connecting kubectl to GKE Cluster"
  # Taken from repo_dir rather than beside this script: upgrade.sh is also run
  # piped from curl, where BASH_SOURCE names no directory to look in.
  local dns_helper="${repo_dir}/scripts/installer/gke_dns_endpoint.sh"
  GKE_DNS_ENDPOINT_FLAG=""
  if [ -f "$dns_helper" ]; then
    # source= points -x runs at the real file; disable=SC1091 covers the bare
    # `shellcheck upgrade.sh` that CI runs, where the directive locates the file
    # but following it still needs -x, so the info-level finding fails the job.
    # shellcheck source=scripts/installer/gke_dns_endpoint.sh
    # shellcheck disable=SC1091
    source "$dns_helper"
    gke_dns_endpoint_flag "$target_cluster" "$target_region" "$target_project"
    if [ -n "$GKE_DNS_ENDPOINT_FLAG" ]; then
      print_info "Cluster '${target_cluster}' publishes an external DNS endpoint; using it."
    fi
  fi
  # Unquoted on purpose: empty must contribute no argument at all.
  # shellcheck disable=SC2086
  gcloud container clusters get-credentials "$target_cluster" --location="$target_region" --project="$target_project" $GKE_DNS_ENDPOINT_FLAG

  local target_namespace="${NAMESPACE:-kubeagents-system}"

  if [ -z "$PARAM_IMAGE_TAG" ] && [ "$PARAM_PLAN" = "true" ]; then
    # A PLAN's reference point is Terraform state, not the cluster, so the tag
    # it plans at has to be the one the last apply RECORDED. The two differ by
    # design on these environments: the redeploy workflows move the running tag
    # with `helm upgrade --reset-then-reuse-values` and never run Terraform, so
    # autopush's cluster advances with every push to main while state stays
    # where the last reconcile left it.
    #
    # Planning at the running tag would therefore render an image_tag into
    # terraform.tfvars that state does not have, helm_release.kube_agents would
    # plan an in-place update, and the daily drift report would open on image
    # lag every day main has moved — the exact thing reading the tag off the
    # cluster was meant to keep OUT of the report, and an issue that never
    # reaches the clean plan that closes it.
    PARAM_IMAGE_TAG="$(tf_state_image_tag)"
    if [ -n "$PARAM_IMAGE_TAG" ]; then
      print_success "Planning at the tag this install's Terraform state records: ${PARAM_IMAGE_TAG}"
    else
      # No state, or state written before this composition had the output.
      # Falling back to the cluster keeps the plan possible; it just cannot
      # promise the image tag is out of it, so say so rather than let a reader
      # take an image-lag diff for infrastructure drift.
      print_warning "This install's Terraform state records no image tag, so the plan falls back to the tag the cluster is running. Any difference between the two will appear in the plan as a change to helm_release.kube_agents. The first apply records it and later plans are clean."
    fi
  fi

  if [ -z "$PARAM_IMAGE_TAG" ]; then
    PARAM_IMAGE_TAG="$(running_image_tag "$target_namespace")"
    if [ -z "$PARAM_IMAGE_TAG" ]; then
      print_error "Could not read the running image tag from deployment/platform-agent-gateway in '${target_namespace}'."
      print_info "Pass --image-tag to name one instead."
      exit 1
    fi
    # Validated like any other, because this one is applied like any other. It
    # reaches terraform.tfvars and the composition, so an install that happens
    # to be serving a mutable ref — `:latest` from a hand-rolled redeploy —
    # must not have that ref written into the configuration by an unattended
    # run. Reading the tag off the cluster rather than off a flag is not a
    # reason to trust it any further.
    validate_immutable_ref "$PARAM_IMAGE_TAG"
    print_success "Using the tag this install is running: ${PARAM_IMAGE_TAG}"
  fi

  if [ "$PARAM_PLAN" = "true" ]; then
    # Both backfills PATCH a live Secret when a key is absent, which a plan may
    # not do. Skipping them costs the plan nothing: what they would add is not
    # Terraform-managed and so appears in no plan either way.
    print_info "Plan mode: skipping the Session KV backfill and the shell sandbox key backfill, which would patch the live Secret."
  else
    print_step "3. Reconciling Pod-Scoped Session Keys"
    backfill_session_kv_keys "$target_namespace"
    # Rolls the agent on the same terms as the Session KV keys, and for the same
    # reason: the sandbox StatefulSet the operator renders mounts the public half,
    # and the gateway pod stages the private half from a Secret volume in an init
    # container that runs once. An install that gains the pair without a restart
    # gains a sandbox it cannot ssh into.
    backfill_sandbox_ssh_key "$target_namespace"
  fi

  # Helm never touches the crds/ directory on upgrade — that is Helm's own
  # documented behaviour, and the Terraform helm provider inherits it — so CRD
  # schema changes are applied here first, for every mode that rolls the
  # operator. Server-side apply, because these objects are large and have had
  # several owners.
  apply_crd_upgrades() {
    print_info "Applying CRD updates from charts/kube-agents/crds..."
    kubectl apply --server-side --force-conflicts -f "${repo_dir}/charts/kube-agents/crds/" >/dev/null
  }

  # The chart-only fast path: a mode that moves no GCP resource re-tags the
  # images it owns on the live release and leaves the rest of the values as
  # they are. The regenerated tfvars carry the same new tag, so the next full
  # `terraform apply` agrees with the release instead of reverting it.
  #
  # Takes every key it must move in one `helm upgrade`, not one call per key:
  # two sequential upgrades leave the release briefly holding a new agent
  # against an old sandbox, and the second one's reused values would have to
  # re-read what the first wrote.
  #
  # --reset-then-reuse-values, not --reuse-values. --reuse-values renders this
  # checkout's chart against only the previous release's values, so any key the
  # chart gained since that release is simply absent: upgrading a pre-split
  # install this way hits a nil pointer in operator-deployment.yaml, or renders
  # the sandbox image as ":<tag>" and leaves the StatefulSet unable to start.
  # Resetting first takes the checkout's defaults for the new keys and re-applies
  # the release's own overrides on top, which is what the redeploy workflows
  # already do for the same reason.
  helm_retag() {
    local set_args=()
    local set_key
    for set_key in "$@"; do
      set_args+=(--set "${set_key}=${PARAM_IMAGE_TAG}")
    done
    helm upgrade kube-agents "${repo_dir}/charts/kube-agents" \
      --namespace "$target_namespace" --reset-then-reuse-values \
      "${set_args[@]}" --wait --timeout 10m
  }

  # The release guard runs before the tfvars generation on purpose: a
  # pre-Terraform install deserves this message, not whatever the generator
  # trips over first (its vars.sh may lack the credentials the generator
  # recovers from the live Secret).
  if ! helm status kube-agents -n "$target_namespace" >/dev/null 2>&1; then
    print_error "No Helm release 'kube-agents' in namespace '$target_namespace'."
    print_info "This install predates the Terraform + Helm engine. Upgrade it with the release that installed it (curl the matching versioned upgrade.sh), or re-install with install.sh to adopt the new engine."
    exit 1
  fi

  # NAMESPACE steers the generator's Secret-recovery reads (install.env omits
  # credentials when PERSIST_SECRETS_ON_DISK=false; the live Secret has them).
  NAMESPACE="$target_namespace" \
    write_tfvars_from_state "${repo_dir}/terraform/examples/full-install/terraform.tfvars" "$PARAM_IMAGE_TAG"

  if [ "$PARAM_PLAN" = "true" ]; then
    print_step "4. Planning (read-only)"
    print_info "Comparing this checkout's composition against the install's Terraform state."
    local plan_status=0
    run_lifecycle "${repo_dir}/terraform/examples/full-install" \
      plan -detailed-exitcode || plan_status=$?

    # terraform's -detailed-exitcode contract: 0 no changes, 1 error, 2 changes.
    # It is passed through as this script's own exit code so a caller can act on
    # it without parsing the plan text.
    case "$plan_status" in
      0)
        print_success "In sync: a full upgrade at ${PARAM_IMAGE_TAG} would change nothing."
        write_report "PLAN_IN_SYNC"
        ;;
      2)
        print_warning "Drift: this install differs from the composition in this checkout."
        print_info "The plan above lists every difference. 'terraform apply' is what closes them; ./upgrade.sh --upgrade-mode=full is the supported way to run one."
        write_report "PLAN_DRIFT"
        ;;
      *)
        print_error "The plan could not be produced (exit ${plan_status})."
        write_report "PLAN_FAILED"
        ;;
    esac
    exit "$plan_status"
  fi

  case "$PARAM_UPGRADE_MODE" in
    operator)
      print_step "4. Upgrading Kubernetes Operator (CRDs & Controller Manager)"
      apply_crd_upgrades
      helm_retag "operator.image.tag"
      print_success "Kubernetes Operator upgraded successfully!"
      ;;

    harness)
      print_step "4. Upgrading Platform Agent Deployment & Identity"
      # The sandbox moves with the agent: both are built from this repository
      # at the same commit, and the shell the agent reaches over ssh is the
      # half that runs the new tools. Retagging the agent alone leaves the
      # StatefulSet on the previous image.
      helm_retag "platformAgent.deployment.image.tag" "agentSandbox.image.tag"
      print_success "Platform Agent deployment upgraded successfully!"
      ;;

    full)
      print_step "4. Executing Full Atomic Upgrade (Terraform + Helm)"
      apply_crd_upgrades
      # install.sh's post-generation minter guard, without its import step:
      # an upgrade never imports the App key, so an install.env that enables the
      # minter against a key with no ENABLED version would wedge the apply on
      # the minter's readiness until the helm timeout fails the upgrade.
      # Refuse up front instead and name the two ways out.
      if grep -q '^enable_github_minter = true$' \
        "${repo_dir}/terraform/examples/full-install/terraform.tfvars" 2>/dev/null; then
        minter_enabled_version="$({ gcloud kms keys versions list \
          --key "${KMS_KEY:-$DEFAULT_KMS_KEY}" \
          --keyring "${KMS_KEYRING:-$DEFAULT_KMS_KEYRING}" \
          --location "$(derive_kms_location "${REGION}")" --project "${PROJECT_ID}" \
          --filter='state=ENABLED' --format='value(name)' 2>/dev/null || true; } | head -1)"
        if [ -z "$minter_enabled_version" ]; then
          print_error "The GitHub minter is enabled in the generated configuration, but its KMS signing key has no ENABLED version — the apply would wait on a minter that can never become ready."
          print_info "Import the App key with install.sh (which runs the import before its apply), or unset GITHUB_APP_ID in install.env to upgrade without the minter."
          exit 1
        fi
      fi
      # A full terraform apply against the regenerated tfvars: both image tags
      # move, and every setting recorded in install.env is re-rendered — the successor
      # of the old path's re-render of the CR from saved state.
      run_lifecycle "${repo_dir}/terraform/examples/full-install" \
        apply -auto-approve -input=false
      print_success "Full atomic upgrade completed successfully!"
      ;;
  esac

  # An operator-mode upgrade rolls the controller manager and nothing else, so a
  # Secret patched above would sit unread until some later harness upgrade —
  # with the watcher dead, or the sandbox unreachable, in the meantime. The
  # other two modes re-render the agent Deployment and pick the keys up on their
  # own rollout.
  local restarted_agent="false"
  if [ "$PARAM_UPGRADE_MODE" = "operator" ] &&
    { [ "$SESSION_KV_KEYS_PATCHED" = "true" ] || [ "$SANDBOX_KEYS_PATCHED" = "true" ]; }; then
    if kubectl get deployment platform-agent-gateway -n "$target_namespace" >/dev/null 2>&1; then
      print_info "Restarting the Platform Agent so it reads the newly added Secret keys..."
      kubectl rollout restart deployment/platform-agent-gateway -n "$target_namespace"
      restarted_agent="true"
    else
      print_warning "Secret keys were added but Deployment 'platform-agent-gateway' was not found in '$target_namespace'; restart the agent yourself so it reads them."
    fi
  fi

  print_step "5. Post-Upgrade Health Verification"
  kubectl get ns kubeagents-system >/dev/null
  if [ "$PARAM_UPGRADE_MODE" = "operator" ] || [ "$PARAM_UPGRADE_MODE" = "full" ]; then
    # kube-agents-controller-manager, not kubeagents-: the chart prefixes the
    # operator Deployment with the release name.
    kubectl rollout status deployment/kube-agents-controller-manager -n kubeagents-system --timeout=120s
  fi
  if [ "$PARAM_UPGRADE_MODE" = "harness" ] || [ "$PARAM_UPGRADE_MODE" = "full" ] || [ "$restarted_agent" = "true" ]; then
    kubectl rollout status deployment/platform-agent-gateway -n kubeagents-system --timeout=120s
  fi
  # A healthy gateway is not a working install. The agent runs no command in its
  # own pod: every shell command goes over ssh to the sandbox StatefulSet, and
  # every credentialed one through the proxy. Verifying only the gateway
  # reported an upgrade that succeeded while the agent could not run kubectl.
  #
  # Guarded on the object existing, because the operator creates both and an
  # operator-mode upgrade can reach here before its PlatformAgent has
  # reconciled. A missing object is that, not a failed rollout.
  if kubectl get statefulset platform-agent-shell -n "$target_namespace" >/dev/null 2>&1; then
    kubectl rollout status statefulset/platform-agent-shell -n "$target_namespace" --timeout="$SANDBOX_ROLLOUT_TIMEOUT"
  fi
  if kubectl get deployment platform-agent-credential-proxy -n "$target_namespace" >/dev/null 2>&1; then
    kubectl rollout status deployment/platform-agent-credential-proxy -n "$target_namespace" --timeout="$SANDBOX_ROLLOUT_TIMEOUT"
  fi
  print_success "Upgraded deployments verified healthy."

  write_report "SUCCESS"

  print_step "🎉 Upgrade Complete!"
}

if [ "${KUBE_AGENTS_SOURCE_ONLY:-false}" != "true" ]; then
  main "$@"
else
  echo "ℹ️ Sourced upgrade.sh functions without executing main (KUBE_AGENTS_SOURCE_ONLY=true)." >&2
fi
