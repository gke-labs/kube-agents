#!/usr/bin/env bash
# ==============================================================================
# 🔄 Kubernetes Agentic Harness (kube-agents) Lifecycle Upgrade Engine
# ==============================================================================
# Modular CLI tool for Day-2 upgrades of the Platform Agent harness and operator.
#
# Usage:
#   ./upgrade.sh [options]
#   curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/upgrade.sh | bash -s -- \
#     --non-interactive --gcp-project-id="my-gcp-project" --gke-cluster-name="platform-agent-host"
#
# The release-pinned script carries its own version, so no image tag is passed.
# It upgrades the install whose checkout the installer left in $HOME/kube-agents,
# reading the install.env in it; KUBE_AGENTS_INSTALL_ENV points at a
# configuration held somewhere else. The upgrade refuses to re-render cluster
# configuration without one.
# ==============================================================================

set -Eeuo pipefail

# This script's own name, for the abort banner when it runs piped through
# stdin (curl | bash): bash then has no file to name its frames after.
UPGRADE_SCRIPT_NAME="upgrade.sh"

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

# Where the upgrade engine is fetched from when this script runs outside a
# checkout. install.sh and uninstall.sh carry the same URL, each needing it
# before it has a checkout to read it from; tests/test_install_script.py pins
# the three equal.
KUBE_AGENTS_REPO_URL="https://github.com/gke-labs/kube-agents.git"
# Where install.sh leaves the sources when it runs as curl | bash, and so where
# this script looks for them rather than fetching its own copy: that checkout is
# also where the install's install.env lives.
#
# A function rather than a constant so that HOME expands only when a clone is
# needed: a run from a checkout never looks for one, and HOME is unset in some
# service environments (a systemd system unit, a container with no passwd
# entry), where `set -u` would otherwise stop the script on this line.
kube_agents_clone_dir() { printf '%s/kube-agents' "${HOME:?the upgrader looks for the install checkout under HOME when it does not run from one}"; }
# A path every kube-agents revision tracks, back to release 0.1.0. An existing
# clone is moved to the requested release only when its HEAD tracks this file,
# so a repository that merely shares the directory name is left alone.
KUBE_AGENTS_CLONE_MARKER="install.sh"
KUBE_AGENTS_INSTALLER_COMMON_MARKER="scripts/installer/installer_common.sh"
# The fetch depth the fresh clone uses, and that a clone which is already
# shallow (one an earlier install left) keeps; a complete clone is fetched
# without it so it does not become shallow.
KUBE_AGENTS_FETCH_DEPTH_OPT="--depth=1"
# The file an unpacked release bundle carries to say which release it is.
# package_release_bundle.sh writes it into every bundle it produces, with
# `version=` and `tag=` both set to the release tag.
readonly KUBE_AGENTS_RELEASE_BUNDLE_MARKER=".release-bundle"

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
# Empty means "whatever the loaded configuration says", which is how this ran
# before the flag existed: the namespace came from install.env alone.
PARAM_AGENT_NAMESPACE=""
PARAM_IMAGE_TAG="${IMAGE_TAG:-${BAKED_RELEASE_VERSION:-}}"
TEMP_REPO_DIR=""
# Set when this run detached the install's own checkout onto the target
# release. Everything that can still refuse the upgrade — the configuration
# load, the missing-project exit, the credentials fetch, the Helm release
# guard, the KMS and service-account guards, Terraform itself — runs after that
# move, so a run that fails one of them would otherwise leave the operator's
# directory on a revision the cluster is not on, and the next install.sh,
# uninstall.sh or hand-run terraform in it would drive an N+1 engine against an
# N install. The move is undone on a failed exit, and only while nothing has
# been applied yet: once the first object is on the cluster the checkout and
# the install are converging, and putting it back would be the lie instead.
MOVED_CHECKOUT_DIR=""
MOVED_CHECKOUT_PREV_HEAD=""
MOVED_CHECKOUT_PREV_BRANCH=""
MOVED_CHECKOUT_TFVARS_PATH=""
MOVED_CHECKOUT_PREV_TFVARS=""
UPGRADE_APPLY_STARTED="false"
HELM_RELEASE_REPAIRED="false"
# Set when the sources came from a checkout that was already on disk rather
# than from a fetch this run made. See verify_local_source_ref: a tag in a
# directory this run did not fetch is only as trustworthy as wherever it came
# from, and before this arm existed the tagged path always fetched from
# KUBE_AGENTS_REPO_URL.
SOURCES_ADOPTED_CHECKOUT="false"

# ─── Process Lock File ───────────────────────────────────────────────────────
# install.sh and uninstall.sh have always taken one; the upgrade had not, and
# it now moves $HOME/kube-agents onto the release it is applying. Two runs at
# once would take turns detaching that one checkout while both read terraform
# and charts out of it, so the second would apply a composition assembled from
# whichever revision the first had it on at the time.
#
# Same lock file as install.sh: both front doors adopt and move the one
# checkout in $HOME/kube-agents (each detaches its HEAD with its own
# refresh_existing_clone; this script also returns it with
# restore_moved_checkout on a pre-apply refusal), and both write
# gitignored terraform.tfvars under its composition directory. An install and an
# upgrade running at once would take turns moving that one directory while both
# read terraform and charts out of it.
#
# The KUBE_AGENTS_SOURCE_ONLY guard is install.sh's and is load-bearing here
# too -- the test suite sources this file, and a lock taken at source time
# would make the suite serialise against itself.
LOCK_FILE="${KUBE_AGENTS_LOCK_FILE:-/tmp/kube-agents-install.lock}"
if [ "${KUBE_AGENTS_SOURCE_ONLY:-false}" != "true" ] && command -v flock >/dev/null 2>&1; then
  if ( : >"$LOCK_FILE" ) 2>/dev/null && exec 200>"$LOCK_FILE"; then
    if ! flock -n 200 2>/dev/null; then
      echo -e "  \033[93m⚠ Another instance of the kube-agents installer or upgrade is currently running. Exiting.\033[0m" >&2
      exit 1
    fi
  fi
fi

# Remember the gitignored terraform.tfvars in an adopted checkout before
# write_tfvars_from_state overwrites it for the target release. A git checkout
# back to the previous revision leaves untracked and gitignored files behind, so
# without this a run that refuses after write_tfvars_from_state (the KMS or
# service-account guard in full mode) would return the checkout to N while
# leaving N+1's terraform.tfvars sitting in its composition directory.
snapshot_moved_checkout_tfvars() {
  local tfvars_path="$1"
  [ -n "$MOVED_CHECKOUT_DIR" ] || return 0
  MOVED_CHECKOUT_TFVARS_PATH="$tfvars_path"
  if [ -f "$tfvars_path" ]; then
    MOVED_CHECKOUT_PREV_TFVARS="$(umask 077 && mktemp "${TMPDIR:-/tmp}/kube-agents-prev-tfvars.XXXXXX")"
    cp -p "$tfvars_path" "$MOVED_CHECKOUT_PREV_TFVARS"
  fi
}

restore_moved_checkout() {
  [ -n "$MOVED_CHECKOUT_DIR" ] || return 0
  [ -d "$MOVED_CHECKOUT_DIR" ] || return 0
  local target="$MOVED_CHECKOUT_PREV_HEAD" what="$MOVED_CHECKOUT_PREV_HEAD"
  if [ -n "$MOVED_CHECKOUT_PREV_BRANCH" ]; then
    target="$MOVED_CHECKOUT_PREV_BRANCH"
    what="branch '${MOVED_CHECKOUT_PREV_BRANCH}'"
  fi
  if [ -n "$MOVED_CHECKOUT_TFVARS_PATH" ]; then
    if [ -n "$MOVED_CHECKOUT_PREV_TFVARS" ] && [ -f "$MOVED_CHECKOUT_PREV_TFVARS" ]; then
      mv -f "$MOVED_CHECKOUT_PREV_TFVARS" "$MOVED_CHECKOUT_TFVARS_PATH"
    else
      rm -f -- "$MOVED_CHECKOUT_TFVARS_PATH"
    fi
    MOVED_CHECKOUT_TFVARS_PATH=""
    MOVED_CHECKOUT_PREV_TFVARS=""
  fi
  # Best effort by necessity: this runs from the EXIT trap of a run that has
  # already failed, and a checkout the operator has since edited is theirs to
  # resolve. Say so either way rather than restoring silently.
  if git -C "$MOVED_CHECKOUT_DIR" checkout --quiet "$target" 2>/dev/null; then
    local pre_apply_repairs=""
    if [ "${SESSION_KV_KEYS_PATCHED:-false}" = "true" ] || [ "${SANDBOX_KEYS_PATCHED:-false}" = "true" ]; then
      pre_apply_repairs="reconciling Secret keys in step 3"
    fi
    if [ "${HELM_RELEASE_REPAIRED:-false}" = "true" ]; then
      if [ -n "$pre_apply_repairs" ]; then
        pre_apply_repairs="${pre_apply_repairs} and repairing the pending Helm release"
      else
        pre_apply_repairs="repairing the pending Helm release"
      fi
    fi
    if [ -n "$pre_apply_repairs" ]; then
      print_info "The new release was not applied (after ${pre_apply_repairs}), so ${MOVED_CHECKOUT_DIR} was returned to ${what}."
    else
      print_info "Nothing was applied, so ${MOVED_CHECKOUT_DIR} was returned to ${what}."
    fi
  else
    print_warning "Could not return ${MOVED_CHECKOUT_DIR} to ${what}; it is still on the revision this run checked out. 'git -C ${MOVED_CHECKOUT_DIR} checkout ${target}' undoes it."
  fi
  MOVED_CHECKOUT_DIR=""
}

cleanup() {
  local exit_code="$?"
  if [ "$exit_code" -ne 0 ] && [ "$UPGRADE_APPLY_STARTED" != "true" ]; then
    restore_moved_checkout
  fi
  if [ -n "$MOVED_CHECKOUT_PREV_TFVARS" ] && [ -f "$MOVED_CHECKOUT_PREV_TFVARS" ]; then
    rm -f -- "$MOVED_CHECKOUT_PREV_TFVARS"
  fi
  if [ -n "$TEMP_REPO_DIR" ] && [ -d "$TEMP_REPO_DIR" ]; then
    rm -rf -- "$TEMP_REPO_DIR"
  fi
}
trap cleanup EXIT

on_error() {
  local exit_code="$1"
  local line_no="$2"
  local bash_cmd="$3"
  # An inherited firing inside a subshell: `set -E` hands this trap to every
  # `$(...)`, and a probe whose miss the caller handles (`if !`, `||`) still
  # fires it there on bash 3.2 (macOS's /bin/bash) before the caller is
  # consulted. The parent decides: it prints the banner and writes the report
  # itself when the failure reaches it, and nothing when it is handled. Exit,
  # not return: command substitution does not inherit errexit, so a returning
  # handler would let a multi-step probe run on past its failure. Process
  # substitution (`< <(...)`) keeps the counter at 0 on bash 3.2 and clears
  # the trap inline instead.
  if [ "${BASH_SUBSHELL:-0}" -gt 0 ]; then
    exit "$exit_code"
  fi
  # The frame that ran the failing command: a sourced library's file and the
  # function it was in, or this script and `main` at top level. $LINENO alone
  # counts from the top of whichever file the command sat in, so a bare line
  # number sent the reader to that line of upgrade.sh instead. Piped through
  # stdin, bash labels this script's frames `main` or not at all, and $0 is
  # `bash`; both read as the script by name.
  local source_file="${BASH_SOURCE[1]:-}"
  case "$source_file" in
    ""|main) source_file="$UPGRADE_SCRIPT_NAME" ;;
  esac
  local func_name="${FUNCNAME[1]:-main}"
  echo -e "\n${C_RED}${C_BOLD}✗ Upgrade error encountered at ${source_file}:${line_no} in ${func_name} (exit code ${exit_code}): ${bash_cmd}${C_RESET}" >&2
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
  --gcp-project-id ID      GCP Target Project ID
  --gke-cluster-name NAME  GKE Target Cluster Name
  --gcp-region REGION      GKE GCP Region
  --agent-namespace NS     Kubernetes namespace the release lives in
                           (default: the install's own, else kubeagents-system)
  --image-tag TAG          Validated immutable release tag or full commit SHA.
                           Developer and CI/CD testing only: an official release
                           bakes its own version into this script and upgrades to
                           it, so an upgrade to a published release passes no tag.
  --keep-image-tag         Upgrade everything except the images, leaving them on
                           the tag the install already serves. Use instead of
                           --image-tag, not alongside it; a script carrying a
                           baked release version already has one, and refuses it.
  --help, -h               Show this help message

Examples:
  # Perform full atomic upgrade of harness, operator, and skills
  ./upgrade.sh --non-interactive --gcp-project-id="my-gcp-project" --gke-cluster-name="platform-agent-host"

  # Dry-run upgrade preview
  ./upgrade.sh --dry-run --upgrade-mode=full

  # What has this install drifted from? Holds the image tag at the one
  # Terraform state records, so the report is composition drift, not image lag.
  ./upgrade.sh --plan
EOF
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
  local secret_name="$PLATFORM_AGENT_SECRET"

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
  local secret_name="$PLATFORM_AGENT_SECRET"

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
  local bundle_file="${repo_dir}/${KUBE_AGENTS_RELEASE_BUNDLE_MARKER}"

  if [ -f "$bundle_file" ]; then
    local bundle_version bundle_tag
    bundle_version="$(grep -E "^version=" "$bundle_file" 2>/dev/null | cut -d'=' -f2- | tr -d '[:space:]' || echo "")"
    bundle_tag="$(grep -E "^tag=" "$bundle_file" 2>/dev/null | cut -d'=' -f2- | tr -d '[:space:]' || echo "")"
    # Either field answers the question. This repository's packager writes both,
    # but deploy/release-versioning.md documents a match on "version or tag",
    # and requiring version= first turned a marker carrying only tag= into a
    # refusal of the very release it names.
    if [ -n "$expected_ref" ] && { [ "$bundle_version" = "$expected_ref" ] || [ "$bundle_tag" = "$expected_ref" ]; }; then
      echo "${bundle_version:-$bundle_tag}"
      return 0
    fi
  fi
  return 1
}

# What release a non-Git source tree says it is, or "" when it says nothing.
#
# Two statements are consulted, because the bundle marker is not the only shape
# a stale tree arrives in. A bundle this repository packaged carries the marker.
# A copy of one with the marker removed, or a tree from a release predating the
# marker, still carries the version package_release_bundle.sh stamps into every
# root script, so the tree's own root scripts answer where the marker cannot.
#
# The order among those scripts matters, and upgrade.sh is deliberately last.
# The way an operator runs a newer upgrader against an older unpacked tree is
# `curl -o upgrade.sh …/<new>/upgrade.sh` inside it — which overwrites the one
# file being consulted, so it would report the new version and the tree would
# pass as matching while carrying the old release's Terraform, charts and CRDs.
# The packager stamps install.sh and uninstall.sh with the same version, and
# neither is in the way of that download, so they answer for the tree rather
# than for the script that happens to be running.
#
# What this still cannot see: a tree that was never stamped at all -- GitHub's
# auto-generated "Source code" archive of a tag is the plain repository content,
# where BAKED_RELEASE_VERSION is empty. Such a directory is indistinguishable
# from any other unversioned directory, and the arm below accepts it on the
# running script's baked version, which is what tests/test_upgrade_script.py's
# test_verify_local_source_ref_accepts_baked_release_in_non_git_dir pins.
release_version_of_source_tree() {
  local repo_dir="$1"
  local marker="${repo_dir}/${KUBE_AGENTS_RELEASE_BUNDLE_MARKER}"
  local declared=""

  if [ -f "$marker" ]; then
    declared="$(grep -E "^version=" "$marker" 2>/dev/null | cut -d'=' -f2- | tr -d '[:space:]' || echo "")"
    if [ -z "$declared" ]; then
      declared="$(grep -E "^tag=" "$marker" 2>/dev/null | cut -d'=' -f2- | tr -d '[:space:]' || echo "")"
    fi
  fi
  if [ -z "$declared" ]; then
    local stamped_script
    for stamped_script in install.sh uninstall.sh upgrade.sh; do
      [ -f "${repo_dir}/${stamped_script}" ] || continue
      declared="$(grep -m1 -E '^BAKED_RELEASE_VERSION=' "${repo_dir}/${stamped_script}" 2>/dev/null | cut -d'=' -f2- | tr -d '"'"'"'[:space:]' || echo "")"
      # An empty stamp is the plain repository content, not an answer; keep
      # asking, so a tree carrying one unstamped script and one stamped one
      # still reports the release it came from.
      if [ -n "$declared" ]; then
        break
      fi
    done
  fi
  echo "$declared"
}

# Sets RECORDED_PLUGIN_IMAGE_TAG_KEYS to the Helm keys, one per line, of the
# plugin image tags the release's user-supplied values record
# (`plugins.<name>.image.tag`), for the harness step to re-tag with the agent
# and sandbox tags. Read from the recorded values rather than from the enabled
# flags because the composition records the tag for a disabled plugin too;
# derived from the values rather than from a list of plugin names so that a
# plugin added to the chart and the composition is covered without a change
# here. The chart the keys are applied to is pinned to this script's commit by
# the source check, so its `plugins` block matches.
#
# A read that fails is an error, not an empty list: an empty list would run
# the pre-fix re-tag and leave the plugin images behind, with the omission
# surfacing only from the image check after the Helm move. It assigns rather
# than prints, as gke_dns_endpoint_flag does, so that the caller runs it as a
# plain command: print_error writes to stdout, which a command substitution
# would swallow, and under `set -E` the ERR trap would fire in the
# substitution's subshell and again in the parent. `trap - ERR` inside its own
# substitutions for the same reason, on bash 3.2 in particular. Arguments:
# release, namespace.
RECORDED_PLUGIN_IMAGE_TAG_KEYS=""
recorded_plugin_image_tag_keys() {
  local release="$1" namespace="$2" values keys stderr_file
  RECORDED_PLUGIN_IMAGE_TAG_KEYS=""
  # stderr kept apart from the JSON: Helm writes warnings there on successful
  # commands too (a group-readable kubeconfig, for one), and merged into the
  # capture they would break the jq parse of values that are fine.
  stderr_file="$(mktemp)"
  if ! values="$(trap - ERR; helm get values "$release" -n "$namespace" -o json 2>"$stderr_file")"; then
    print_error "Could not read the values of Helm release '${release}' in '${namespace}' to find the plugin image tags: $(cat "$stderr_file")"
    rm -f "$stderr_file"
    return 1
  fi
  if ! keys="$(trap - ERR; jq -r '(.plugins // {}) | if type == "object" then to_entries[] | select(((.value.image.tag? // "") | tostring) != "") | "plugins.\(.key).image.tag" else error("plugins is not an object") end' <<<"$values" 2>"$stderr_file")"; then
    print_error "Could not read the plugin image tags from the values of Helm release '${release}': $(cat "$stderr_file")"
    rm -f "$stderr_file"
    return 1
  fi
  rm -f "$stderr_file"
  RECORDED_PLUGIN_IMAGE_TAG_KEYS="$keys"
}

# Sets HARNESS_RETAG_KEYS, the Helm keys the harness step re-tags: the agent
# and sandbox tags, then every plugin tag the release records. A function of
# its own so the assembly runs under test with a stub helm, rather than being
# pinned by the text of the case branch. A read loop rather than the bash 4
# array builtin: operators run this from macOS, whose bash is 3.2. Arguments:
# release, namespace.
HARNESS_RETAG_KEYS=()
harness_retag_keys() {
  local release="$1" namespace="$2" key
  HARNESS_RETAG_KEYS=("platformAgent.deployment.image.tag" "agentSandbox.image.tag")
  recorded_plugin_image_tag_keys "$release" "$namespace"
  # An if, not `[ -n ] &&`: with nothing recorded the here-string is one empty
  # line, the test fails, the loop's status is that failure, and under
  # `set -e` the harness step would stop on an install with no plugins.
  while IFS= read -r key; do
    if [ -n "$key" ]; then
      HARNESS_RETAG_KEYS+=("$key")
    fi
  done <<<"$RECORDED_PLUGIN_IMAGE_TAG_KEYS"
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

# What the canonical repository says a release tag names, printed as a commit
# SHA. Returns 1 when the remote could not be asked, and 2 when it answered and
# carries no such tag: `git ls-remote` exits 0 with no output for a missing ref,
# and the two need different remedies (retry the network, or delete the tag).
#
# Asked for the peeled form first: an annotated tag's own object is not the
# commit, and comparing a checkout's HEAD against it would never match.
# kube-agents' release tags are lightweight today, which is the second query.
remote_release_tag_commit() {
  local expected_ref="$1" listing="" commit=""
  listing="$(git ls-remote --tags "$KUBE_AGENTS_REPO_URL" "$expected_ref" 2>/dev/null)" || return 1
  commit="$(printf '%s\n' "$listing" | awk -v ref="refs/tags/${expected_ref}^{}" -F'\t' '$2 == ref {print $1; exit}')"
  if [ -z "$commit" ]; then
    commit="$(printf '%s\n' "$listing" | awk -v ref="refs/tags/${expected_ref}" -F'\t' '$2 == ref {print $1; exit}')"
  fi
  [ -n "$commit" ] || return 2
  printf '%s' "$commit"
}

# Whether the ref names a commit outright rather than a tag. A 40-character
# object name is self-verifying — a checkout cannot hold a different tree under
# the same SHA — so it needs no second opinion from the remote.
ref_is_commit_sha() {
  printf '%s' "${1:-}" | grep -Eq '^[0-9a-fA-F]{40}$'
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
      # BAKED_RELEASE_VERSION belongs to the script that is RUNNING, not to the
      # directory it was handed: a piped release one-liner carries its own
      # version wherever it is run. So when the directory says which release it
      # is and disagrees, that is the answer -- without this, standing in an
      # unpacked older bundle and piping a newer upgrade.sh applied the old
      # tree's Terraform and charts at the new tag and called it verified.
      local tree_release=""
      tree_release="$(release_version_of_source_tree "$repo_dir")"
      if [ -n "$tree_release" ] && [ "$tree_release" != "$expected_ref" ]; then
        print_error "Refusing to upgrade from '${repo_dir}': it is release '${tree_release}', not '${expected_ref}'."
        return 1
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
  # Whose '0.6.0' is this? For sources this run fetched, the answer is settled:
  # they came from KUBE_AGENTS_REPO_URL under refs/tags. For a checkout that
  # was already on disk it is not, because the tag was resolved out of that
  # checkout's own object database — a tag fetched from a fork, or made by hand
  # with `git tag 0.6.0`, resolves just as well and would be applied to the live
  # install under the release's image tag. Before this script reused the
  # install's checkout, the tagged path always fetched, so this asks the
  # canonical repository the question that fetch used to answer implicitly.
  #
  # Skipped for a commit SHA, which is self-verifying, and only warned about in
  # a preview, which changes nothing and is also the mode an operator reaches
  # for when the network is the thing that is broken. A real upgrade refuses,
  # including when the remote cannot be reached: the fetch it replaces needed
  # the network too.
  if [ "$SOURCES_ADOPTED_CHECKOUT" = "true" ] && ! ref_is_commit_sha "$expected_ref"; then
    local preview_only="false" remote_commit="" remote_rc=0
    if [ "$PARAM_DRY_RUN" = "true" ] || [ "$PARAM_PLAN" = "true" ]; then
      preview_only="true"
    fi
    remote_commit="$(remote_release_tag_commit "$expected_ref")" || remote_rc=$?
    if [ "$remote_rc" -eq 2 ]; then
      if [ "$preview_only" = "true" ]; then
        print_warning "${KUBE_AGENTS_REPO_URL} does not carry '${expected_ref}', so the '${expected_ref}' in ${repo_dir} is not a published release. This preview reads the local one."
      else
        print_error "Refusing to upgrade from ${repo_dir}: ${KUBE_AGENTS_REPO_URL} does not carry '${expected_ref}', so that checkout's '${expected_ref}' is not a published release."
        print_info "That checkout's tag did not come from the release. Delete it ('git -C ${repo_dir} tag -d ${expected_ref}'), or pass a release tag ${KUBE_AGENTS_REPO_URL} publishes."
        return 1
      fi
    elif [ "$remote_rc" -ne 0 ]; then
      if [ "$preview_only" = "true" ]; then
        print_warning "Could not ask ${KUBE_AGENTS_REPO_URL} what '${expected_ref}' names, so this preview is trusting the tag in ${repo_dir}."
      else
        print_error "Could not ask ${KUBE_AGENTS_REPO_URL} what '${expected_ref}' names, and ${repo_dir} was not fetched by this run, so its '${expected_ref}' cannot be confirmed as the release."
        return 1
      fi
    elif [ "$remote_commit" != "$expected_commit" ]; then
      if [ "$preview_only" = "true" ]; then
        print_warning "The '${expected_ref}' in ${repo_dir} is ${expected_commit}, but ${KUBE_AGENTS_REPO_URL} names ${remote_commit}. This preview reads the local one."
      else
        print_error "Refusing to upgrade from ${repo_dir}: its '${expected_ref}' is ${expected_commit}, but ${KUBE_AGENTS_REPO_URL} names ${remote_commit}."
        print_info "That checkout's tag did not come from the release. Delete or re-point it ('git -C ${repo_dir} tag -d ${expected_ref}'), or run the upgrade from a directory this script can fetch into."
        return 1
      fi
    fi
  fi
  if [ -n "$(git -C "$repo_dir" status --porcelain --untracked-files=no)" ]; then
    # Both previews warn, the way verify_local_source_clean's do and for the
    # same reason: they change nothing, and a preview of what the working tree
    # WOULD apply is the one command that answers "what have I edited here".
    # Every tagged --plan reaches this, whichever checkout it runs from: the
    # script's own, the working directory, or an install checkout reused
    # because it is already at the ref. It used to refuse here, which took the
    # drift report away from an operator whose checkout carries a stray edit;
    # it now warns in all three, as --dry-run always did.
    if [ "$PARAM_DRY_RUN" = "true" ] || [ "$PARAM_PLAN" = "true" ]; then
      print_warning "This preview is using uncommitted source changes; a real upgrade would require a clean checkout."
    else
      print_error "Refusing to upgrade from a dirty checkout because its scripts do not exactly match '$expected_ref'."
      return 1
    fi
  fi
  print_success "Verified upgrade scripts and image ref resolve to commit ${expected_commit}."
}

# ─── The install checkout ─────────────────────────────────────────────────────
# The two functions below are copies of install.sh's. They cannot live in
# scripts/installer/installer_common.sh, which every other shared helper does:
# that file is sourced out of the very checkout these two go and find, so they
# have to run before there is anything to source. install.sh carries its own
# copy of the install.env loader for the same reason (installer_common.sh
# explains it there), and tests/test_upgrade_script.py pins the pair equal.

# Fetch one ref from KUBE_AGENTS_REPO_URL into a clone, leaving it in FETCH_HEAD.
# A 40-hex ref is fetched by object name; anything else is a release tag,
# fetched under its own name so verify_local_source_ref can resolve it. $3 is
# the depth option or empty: the fresh clone passes it, an existing clone only
# when it is already shallow.
fetch_source_ref() {
  local repo_dir="$1"
  local expected_ref="$2"
  local depth_opt="${3:-}"
  if [[ "$expected_ref" =~ ^[0-9a-fA-F]{40}$ ]]; then
    git -C "$repo_dir" fetch ${depth_opt:+"$depth_opt"} "$KUBE_AGENTS_REPO_URL" "$expected_ref"
  else
    git -C "$repo_dir" fetch ${depth_opt:+"$depth_opt"} "$KUBE_AGENTS_REPO_URL" "+refs/tags/${expected_ref}:refs/tags/${expected_ref}"
  fi
}

# Move a clone left by an earlier install (or a plain `git clone`) to the
# requested ref. Only the arm that runs without a checkout calls this: the two
# arms that run this script from one never move it. A clean kube-agents
# worktree whose HEAD is not already the ref is detached at it, a branch it was
# on (main, say) being left behind; the ref is fetched first only when the clone
# does not have it. Every other case prints which one applied and returns 0 so
# verify_local_source_ref, which runs next, reports it in its own words: a
# directory that is not the root of a Git worktree or whose HEAD is not a
# kube-agents revision, a dirty tree, or a fetch or checkout that fails
# (offline, a tag that does not exist).
refresh_existing_clone() {
  local repo_dir="$1"
  local expected_ref="$2"
  local head_commit="" expected_commit="" head_branch="" depth_opt=""
  # -e "$repo_dir/.git" (a directory, or the file a linked worktree carries)
  # rules out a plain directory inside a Git-managed HOME, which
  # --is-inside-work-tree alone would accept and every -C command below would
  # then act on: HOME itself would be fetched into and detached.
  if ! git -C "$repo_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1 || [ ! -e "${repo_dir}/.git" ]; then
    print_info "Using existing repository at $repo_dir as-is: it is not the root of a Git worktree."
    return 0
  fi
  if ! head_commit="$(git -C "$repo_dir" rev-parse --verify HEAD 2>/dev/null)"; then
    print_info "Using existing repository at $repo_dir as-is: it has no commit checked out."
    return 0
  fi
  # ls-tree reads the tree object only, so a blobless clone answers without
  # fetching a blob.
  if [ -z "$(git -C "$repo_dir" ls-tree --name-only HEAD -- "$KUBE_AGENTS_CLONE_MARKER" 2>/dev/null)" ]; then
    print_info "Using existing repository at $repo_dir as-is: its HEAD is not a kube-agents revision (no $KUBE_AGENTS_CLONE_MARKER), so it was not moved."
    return 0
  fi
  if [ -n "$(git -C "$repo_dir" status --porcelain --untracked-files=no)" ]; then
    print_info "Using existing repository at $repo_dir without modifying local changes: the checkout is dirty, so '$expected_ref' was not fetched into it."
    return 0
  fi
  head_branch="$(git -C "$repo_dir" symbolic-ref --short -q HEAD || true)"
  if expected_commit="$(git -C "$repo_dir" rev-parse --verify "${expected_ref}^{commit}" 2>/dev/null)"; then
    if [ "$head_commit" = "$expected_commit" ]; then
      print_info "Using existing repository at $repo_dir: already at '$expected_ref' ($head_commit)."
      return 0
    fi
    print_info "Using existing repository at $repo_dir: it already has '$expected_ref' ($expected_commit); checking it out."
  else
    if [ "$(git -C "$repo_dir" rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
      depth_opt="$KUBE_AGENTS_FETCH_DEPTH_OPT"
    fi
    print_info "Using existing repository at $repo_dir: fetching '$expected_ref' from $KUBE_AGENTS_REPO_URL..."
    if ! fetch_source_ref "$repo_dir" "$expected_ref" "$depth_opt"; then
      print_warning "Could not fetch '$expected_ref' into $repo_dir; the checkout stays at $head_commit."
      return 0
    fi
    expected_commit="FETCH_HEAD"
  fi
  if ! git -C "$repo_dir" checkout --detach "$expected_commit"; then
    print_warning "Could not check out '$expected_ref' in $repo_dir; the checkout stays at $head_commit."
    return 0
  fi
  if [ -n "$head_branch" ]; then
    print_info "Moved $repo_dir from branch '$head_branch' ($head_commit) to '$expected_ref' (detached HEAD). The branch is left where it was and 'git checkout $head_branch' returns to it; untracked files such as install.env are kept."
  else
    print_info "Moved $repo_dir from $head_commit to '$expected_ref' (detached HEAD). 'git checkout $head_commit' returns to the previous revision; untracked files such as install.env are kept."
  fi
}

# Whether a directory may be adopted as this run's sources. Stricter than
# [ -d ] on purpose: verify_local_source_ref accepts anything that is not a Git
# worktree as soon as the script's own baked release version equals the
# requested ref — the default on every release copy — so a stale unpacked
# bundle or an unrelated tree left at this path would be announced as verified
# release sources and then applied to a live install. The first three checks
# match refresh_existing_clone's, and the fourth requires kube-agents' own
# installer helper in HEAD alongside install.sh; a directory that fails them is
# left alone and the run fetches its engine instead.
is_kube_agents_clone() {
  local repo_dir="$1"
  [ -n "$repo_dir" ] && [ -d "$repo_dir" ] || return 1
  git -C "$repo_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 1
  [ -e "${repo_dir}/.git" ] || return 1
  git -C "$repo_dir" rev-parse --verify HEAD >/dev/null 2>&1 || return 1
  [ -n "$(git -C "$repo_dir" ls-tree --name-only HEAD -- "$KUBE_AGENTS_CLONE_MARKER" 2>/dev/null)" ] &&
    [ -n "$(git -C "$repo_dir" ls-tree --name-only HEAD -- "$KUBE_AGENTS_INSTALLER_COMMON_MARKER" 2>/dev/null)" ]
}

# Whether a checkout already sits on the requested ref. A preview may take its
# sources from the install's own checkout only in that case, because then
# nothing has to move for it.
clone_head_is_ref() {
  local repo_dir="$1"
  local expected_ref="$2"
  local head_commit="" expected_commit=""
  head_commit="$(git -C "$repo_dir" rev-parse --verify HEAD 2>/dev/null)" || return 1
  expected_commit="$(git -C "$repo_dir" rev-parse --verify "${expected_ref}^{commit}" 2>/dev/null)" || return 1
  [ "$head_commit" = "$expected_commit" ]
}

# Where this run's install configuration is, in install.sh's order of
# preference: an explicit KUBE_AGENTS_INSTALL_ENV, then the checkout the run's
# own sources came from, then the directory the operator is standing in, and
# the checkout discovered in HOME last.
#
# That last position is the point. A workstation that manages two installs has
# one $HOME/kube-agents and one install.env in it, so preferring it would load
# install A's chat space, allowed users, namespace and GitOps repository into a
# run the operator started in install B's directory and pointed at install B's
# cluster — and a full upgrade re-renders B from all of it. The flags name the
# cluster; they do not name the configuration.
resolve_install_env_file() {
  local repo_dir="$1"
  local install_checkout="${2:-}"
  if [ -n "${KUBE_AGENTS_INSTALL_ENV:-}" ]; then
    echo "${KUBE_AGENTS_INSTALL_ENV}"
    return 0
  fi
  if [ "$repo_dir" != "$install_checkout" ] && [ -f "${repo_dir}/install.env" ]; then
    echo "${repo_dir}/install.env"
    return 0
  fi
  if [ -f "$(pwd)/install.env" ]; then
    echo "$(pwd)/install.env"
    return 0
  fi
  if [ -n "$install_checkout" ] && [ -f "${install_checkout}/install.env" ]; then
    echo "${install_checkout}/install.env"
    return 0
  fi
  # Nothing exists yet. Naming the sources' own directory keeps the refusal
  # below pointing at the file the installer would have written.
  default_install_env_file "$repo_dir"
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
      --gcp-project-id=*) PARAM_PROJECT_ID="${1#*=}"; shift ;;
      --gcp-project-id) PARAM_PROJECT_ID="$2"; shift 2 ;;
      --gke-cluster-name=*) PARAM_CLUSTER_NAME="${1#*=}"; shift ;;
      --gke-cluster-name) PARAM_CLUSTER_NAME="$2"; shift 2 ;;
      --gcp-region=*) PARAM_REGION="${1#*=}"; shift ;;
      --gcp-region) PARAM_REGION="$2"; shift 2 ;;
      --agent-namespace=*) PARAM_AGENT_NAMESPACE="${1#*=}"; shift ;;
      --agent-namespace) PARAM_AGENT_NAMESPACE="$2"; shift 2 ;;
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
    KUBE_AGENTS_STATE_BUCKET="${KUBE_AGENTS_STATE_BUCKET:-$DEFAULT_KUBE_AGENTS_STATE_BUCKET}"
    KUBE_AGENTS_STATE_PREFIX="$(tf_state_prefix)"
    export KUBE_AGENTS_STATE_BUCKET KUBE_AGENTS_STATE_PREFIX
    ./lifecycle.sh "$@"
  )
}

checkout_owns_run_config() {
  local candidate="$1"
  [ -f "${candidate}/install.env" ] || return 1
  if [ -n "${KUBE_AGENTS_INSTALL_ENV:-}" ]; then
    # The same file, however it is spelled. An operator can point this at the
    # checkout's own install.env through a relative path, a symlink, or a
    # doubled slash, and a string compare would answer "no" and send a run that
    # is already reading that checkout's config off to a temporary clone. -ef
    # compares device and inode, so it answers for the file. The string compare
    # is only a fallback for a path that does not exist, which main() refuses
    # before this is ever reached (see the KUBE_AGENTS_INSTALL_ENV check next to
    # the --dry-run/--plan conflict); it stays for the source-only callers that
    # reach this function without going through main().
    if [ -e "$KUBE_AGENTS_INSTALL_ENV" ]; then
      [ "$KUBE_AGENTS_INSTALL_ENV" -ef "${candidate}/install.env" ]
    else
      [ "$KUBE_AGENTS_INSTALL_ENV" = "${candidate}/install.env" ]
    fi
    return
  fi
  if [ -f "$(pwd)/install.env" ] && ! [ "$(pwd)/install.env" -ef "${candidate}/install.env" ]; then
    return 1
  fi
  return 0
}

# Put this run's sources on disk, and name the install checkout it found. Both
# land in the variables named by $1 and $2 rather than being echoed, because
# the progress lines would otherwise be captured along with the paths — the
# arrangement, and the arm structure, of install.sh's acquire_source_repo.
#
# A tagless run cannot fetch its own engine — there is no tag to fetch — and
# cannot compare the checkout against a ref it was not given. Both are things
# the tag makes possible rather than things the run needs.
#
# What the tag does NOT excuse is the state of the checkout itself: a tagless
# run still applies this directory's Terraform and charts to a live install, so
# verify_local_source_clean runs either way and only the ref comparison is
# conditional.
acquire_upgrade_sources() {
  local repo_var="$1"
  local checkout_var="$2"
  local expected_ref="$3"
  # Named differently from the caller's variables on purpose: bash scopes
  # locals dynamically, so a local sharing a name with the variable named in
  # $1 or $2 would be the one printf -v writes to, and the caller would read
  # back an empty string.
  local resolved_dir="" found_checkout="" script_dir="" script_path="${BASH_SOURCE[0]:-}"
  # Under `curl … | bash` there is no script file on disk. At the top level
  # `${BASH_SOURCE[0]:-}` is empty there; inside a function — which is where
  # this runs — what bash reports depends on its version: `main` or nothing on
  # the releases on_error's comment above describes, and `$0` on bash 5.3
  # (measured): `bash` for the documented one-liner, or the interpreter's own
  # path (`/bin/bash`) when it is invoked by path. None of them names this
  # script. An unguarded `dirname` turns the empty value, `main` and a bare
  # `bash` into `.`, and `pwd` into the directory the operator is standing in,
  # which would skip the checkout arms below. Two checks share the job:
  # requiring a non-empty path that names an existing file rejects the empty
  # value, `main` and a bare `bash` (short of a file by that name in the
  # working directory), and the installer-helper marker check that follows
  # rejects the interpreter's directory, which carries no checkout.
  if [ -n "$script_path" ] && [ -f "$script_path" ]; then
    script_dir="$(cd "$(dirname "$script_path")" 2>/dev/null && pwd || echo "")"
  fi
  if [ -n "$script_dir" ] && [ -f "${script_dir}/${KUBE_AGENTS_INSTALLER_COMMON_MARKER}" ]; then
    resolved_dir="$script_dir"
  elif [ -f "$(pwd)/${KUBE_AGENTS_INSTALLER_COMMON_MARKER}" ] && {
    [ -z "$expected_ref" ] || ! is_kube_agents_clone "$(pwd)"
  }; then
    resolved_dir="$(pwd)"
  elif [ -z "$expected_ref" ]; then
    print_error "--plan and --keep-image-tag have to run from a kube-agents checkout: without --image-tag there is no ref to fetch the engine at."
    exit 1
  else
    # The checkout install.sh left behind is preferred over a fresh copy of the
    # same sources, because it is not only sources: the install's install.env
    # sits in it, and the upgrade refuses to re-render the install without one.
    # Fetching into a temporary directory instead is what made the documented
    # install and the documented upgrade disagree.
    #
    # Only inside this arm, and only when that checkout owns this run's
    # install.env. A run that already has a checkout keeps it, so a CI job that
    # checked out the ref it is reconciling is never redirected at whatever an
    # earlier install happened to leave in HOME. A developer clone in $(pwd)
    # without install.env yields to HOME's install checkout rather than being
    # detached onto the release tag, and a second install configured from
    # $(pwd)/install.env or KUBE_AGENTS_INSTALL_ENV outside HOME's checkout
    # fetches a temporary engine instead of overwriting HOME's terraform.tfvars.
    local clone_dir=""
    if [ -n "${HOME:-}" ]; then
      clone_dir="$(kube_agents_clone_dir)"
    fi
    if [ -f "$(pwd)/${KUBE_AGENTS_INSTALLER_COMMON_MARKER}" ] && checkout_owns_run_config "$(pwd)"; then
      found_checkout="$(pwd)"
    elif [ -n "$clone_dir" ] && is_kube_agents_clone "$clone_dir" && checkout_owns_run_config "$clone_dir"; then
      found_checkout="$clone_dir"
    fi
    # A preview promises to change nothing, and the operator's checkout is part
    # of "nothing": moving it to the requested release would leave the next
    # thing run from that directory — install.sh, uninstall.sh, terraform by
    # hand — on a revision the cluster is not on, after a command that said it
    # had changed nothing. So a preview never moves the checkout: it reuses it
    # only when it is already at the ref, and otherwise reads its engine from a
    # temporary copy, loading only the install's configuration from the
    # checkout. Reuse is not read-only: `--plan` over an at-ref checkout
    # regenerates its terraform.tfvars and initialises .terraform/ there, as a
    # real upgrade would; neither is tracked, and HEAD stays where it was.
    local preview="false"
    if [ "$PARAM_PLAN" = "true" ] || [ "$PARAM_DRY_RUN" = "true" ]; then
      preview="true"
    fi
    if [ -n "$found_checkout" ] && [ "$preview" = "false" ]; then
      resolved_dir="$found_checkout"
      # Read before the move, so the EXIT trap can undo it if this run refuses
      # later. refresh_existing_clone is pinned byte-equal to install.sh's, so
      # the bookkeeping sits here rather than inside it: install.sh moves a
      # clone it made, this moves the operator's own directory.
      local prev_head="" prev_branch="" now_head="" had_ref="false"
      prev_head="$(git -C "$resolved_dir" rev-parse --verify HEAD 2>/dev/null || echo "")"
      prev_branch="$(git -C "$resolved_dir" symbolic-ref --short -q HEAD || true)"
      if [ -n "$expected_ref" ] && git -C "$resolved_dir" rev-parse --verify "${expected_ref}^{commit}" >/dev/null 2>&1; then
        had_ref="true"
      fi
      refresh_existing_clone "$resolved_dir" "$expected_ref"
      now_head="$(git -C "$resolved_dir" rev-parse --verify HEAD 2>/dev/null || echo "")"
      if [ -n "$prev_head" ] && [ -n "$now_head" ] && [ "$prev_head" != "$now_head" ]; then
        MOVED_CHECKOUT_DIR="$resolved_dir"
        MOVED_CHECKOUT_PREV_HEAD="$prev_head"
        MOVED_CHECKOUT_PREV_BRANCH="$prev_branch"
      fi
      if [ "$had_ref" = "true" ]; then
        SOURCES_ADOPTED_CHECKOUT="true"
      fi
    elif [ -n "$found_checkout" ] && clone_head_is_ref "$found_checkout" "$expected_ref"; then
      resolved_dir="$found_checkout"
      SOURCES_ADOPTED_CHECKOUT="true"
      print_info "Using the install checkout at ${resolved_dir}: it is already at '${expected_ref}'."
    else
      if [ -n "$found_checkout" ]; then
        print_info "Previewing '${expected_ref}' from a temporary copy: ${found_checkout} is on another revision, and a preview does not move it."
      fi
      TEMP_REPO_DIR="$(mktemp -d)"
      resolved_dir="${TEMP_REPO_DIR}/kube-agents"
      print_info "Fetching the upgrade engine for '${expected_ref}'..."
      git clone --filter=blob:none --no-checkout "$KUBE_AGENTS_REPO_URL" "$resolved_dir"
      # Through fetch_source_ref, the way install.sh fetches: by object name for
      # a commit SHA and under refs/tags for a release, and from the repository
      # URL rather than the clone's own origin.
      fetch_source_ref "$resolved_dir" "$expected_ref" "$KUBE_AGENTS_FETCH_DEPTH_OPT"
      git -C "$resolved_dir" checkout --detach FETCH_HEAD
    fi
  fi
  if [ -n "$expected_ref" ]; then
    verify_local_source_ref "$resolved_dir" "$expected_ref"
  else
    verify_local_source_clean "$resolved_dir"
  fi
  printf -v "$repo_var" '%s' "$resolved_dir"
  printf -v "$checkout_var" '%s' "$found_checkout"
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
      print_error "--image-tag is required; this copy of the script carries no baked release version. Re-run the release-pinned script for the version you want, or pass a validated release tag or full commit SHA."
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
      print_error "--image-tag is required when no interactive terminal is available and this copy of the script carries no baked release version. Re-run the release-pinned script for the version you want, or pass --image-tag."
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

  if [ "$PARAM_DRY_RUN" = "true" ] && [ "$PARAM_PLAN" = "true" ]; then
    print_error "--dry-run and --plan are different previews and cannot be combined: --dry-run answers offline from configuration, --plan answers from the install's Terraform state."
    exit 1
  fi

  # An explicit pointer at a file that is not there is a typo, not a lookup
  # order to fall through. Naming it here is the parity install.sh already has
  # in bootstrap_install_env; without it the run ends at "No install
  # configuration (install.env) was found in <somewhere>", which blames a
  # directory the operator never chose and never prints the path they did.
  #
  # Before acquire_upgrade_sources on purpose. The pointer is also what
  # checkout_owns_run_config compares against, so a nonexistent one makes the
  # install checkout look like somebody else's and sends the run off to clone
  # the engine into a temporary directory first -- work thrown away, and a
  # worse directory to be named in the refusal that follows.
  if [ -n "${KUBE_AGENTS_INSTALL_ENV:-}" ] && [ ! -f "${KUBE_AGENTS_INSTALL_ENV}" ]; then
    print_error "KUBE_AGENTS_INSTALL_ENV names '${KUBE_AGENTS_INSTALL_ENV}', which does not exist."
    print_info "Point it at the install's install.env, or unset it to search this run's sources, the current directory, and the install checkout in \$HOME/kube-agents."
    exit 1
  fi

  local required_tools=(gcloud kubectl helm)
  # jq: the harness step's plugin re-tag reads the release's values with it,
  # and the post-upgrade image check that harness and full modes run has
  # needed it all along. The operator step does neither.
  if [ "$PARAM_UPGRADE_MODE" != "operator" ]; then
    required_tools+=(jq)
  fi
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

  local repo_dir="" install_checkout=""
  acquire_upgrade_sources repo_dir install_checkout "$PARAM_IMAGE_TAG"

  print_step "1. Validating Upgrade Target & Environment"
  print_info "Upgrade Mode: ${C_BOLD}${PARAM_UPGRADE_MODE}${C_RESET}"
  print_info "Target Image Tag: ${C_BOLD}${PARAM_IMAGE_TAG}${C_RESET}"

  # Shared defaults, the install.env loader, and the terraform.tfvars generator.
  # Sourced here rather than just before the generator, because the state load
  # below needs load_install_env. Print helpers are already defined above, as
  # the file expects.
  # shellcheck disable=SC1091
  source "${repo_dir}/${KUBE_AGENTS_INSTALLER_COMMON_MARKER}"

  # install.env is the install's configuration, and the only one. It is resolved
  # against the install checkout as well as this run's sources, because a
  # preview reads its engine from a temporary copy, which has no install.env.
  local install_env_file
  install_env_file="$(resolve_install_env_file "$repo_dir" "$install_checkout")"
  # Clear any shell-exported coordinates before sourcing the file: load_install_env
  # only unsets NAMESPACE, so without this an install.env that omits REGION while
  # the caller's shell exports REGION=europe-west1 would make the cross-check
  # below blame install.env for a value the file never recorded.
  unset PROJECT_ID CLUSTER_NAME REGION
  if load_install_env "$install_env_file"; then
    print_success "Loaded install configuration from: ${install_env_file}"
  else
    local searched="${repo_dir}"
    if [ -n "$install_checkout" ] && [ "$install_checkout" != "$repo_dir" ]; then
      searched="${searched} or ${install_checkout}"
    fi
    print_warning "No install configuration (install.env) was found in ${searched}."
    # Fail closed, and do it here: the upgrade re-renders the PlatformAgent
    # Custom Resource from this file, so upgrading without it would silently
    # reset chat, allowed users, dashboard, and model-provider configuration to
    # blank defaults. The refusal used to sit below the --dry-run exit, which
    # made the preview answer "here is what would happen" to a run that would
    # in fact have refused -- and .agents/skills/upgrade-kube-agents/SKILL.md
    # offers --dry-run as the pre-flight check. A preview of a run that cannot
    # happen is worth nothing, so both previews stop here with the real run.
    print_error "Refusing to upgrade without the installation's configuration."
    print_info "Run upgrade.sh from the directory holding the install's install.env, point KUBE_AGENTS_INSTALL_ENV at one, or keep the install checkout the installer left in \$HOME/kube-agents."
    exit 1
  fi
  # GITOPS_ORG / GITOPS_REPO are the names; a configuration still carrying
  # GITHUB_ORG / GITHUB_REPO is accepted with a warning. Runs after the load and
  # before anything reads the coordinates.
  normalize_gitops_repo_vars
  # install.env records the operator-facing MEMORY; MEMORY_PROVIDER is the name
  # the chart and the generator read. Derived here, after the load and before
  # anything reads it.
  normalize_memory_vars

  local target_project="${PARAM_PROJECT_ID:-${PROJECT_ID:-}}"
  local target_cluster="${PARAM_CLUSTER_NAME:-${CLUSTER_NAME:-$DEFAULT_CLUSTER_NAME}}"
  local target_region="${PARAM_REGION:-${REGION:-$DEFAULT_REGION}}"

  if [ -z "$target_project" ]; then
    target_project="$(gcloud config get-value project 2>/dev/null || true)"
  fi
  if [ -z "$target_project" ]; then
    print_error "A GCP project is required. Pass --gcp-project-id or configure one with gcloud."
    exit 1
  fi

  print_info "GCP Target Project: ${C_BOLD}${target_project}${C_RESET}"
  print_info "GKE Target Cluster: ${C_BOLD}${target_cluster}${C_RESET} (${target_region})"

  # The loaded configuration records the install it belongs to, and the flags
  # can name a different one. The lookup order is what usually keeps those in
  # step -- standing in an install's directory upgrades that install -- but the
  # piped one-liner has nowhere to stand, so $HOME/kube-agents/install.env is
  # what gets loaded whatever --gke-cluster-name says. A full upgrade
  # re-renders the PlatformAgent CR from that file, so the mismatch does not
  # merely upgrade the wrong install: it writes A's chat space, allowed users,
  # model provider and NAMESPACE into B. Unlike the dirty-checkout check, both
  # a real run and --plan refuse here; only --dry-run reports it and goes on.
  #
  # Unconditional: a run with no configuration at all has already stopped above.
  local coordinate_conflicts=""
  if [ -n "$PARAM_PROJECT_ID" ] && [ -n "${PROJECT_ID:-}" ] && [ "$PARAM_PROJECT_ID" != "$PROJECT_ID" ]; then
    coordinate_conflicts="${coordinate_conflicts}    --gcp-project-id=${PARAM_PROJECT_ID}, but PROJECT_ID=${PROJECT_ID}"$'\n'
  fi
  if [ -n "$PARAM_CLUSTER_NAME" ] && [ -n "${CLUSTER_NAME:-}" ] && [ "$PARAM_CLUSTER_NAME" != "$CLUSTER_NAME" ]; then
    coordinate_conflicts="${coordinate_conflicts}    --gke-cluster-name=${PARAM_CLUSTER_NAME}, but CLUSTER_NAME=${CLUSTER_NAME}"$'\n'
  fi
  if [ -n "$PARAM_REGION" ] && [ -n "${REGION:-}" ] && [ "$PARAM_REGION" != "$REGION" ]; then
    coordinate_conflicts="${coordinate_conflicts}    --gcp-region=${PARAM_REGION}, but REGION=${REGION}"$'\n'
  fi
  if [ -n "$coordinate_conflicts" ]; then
    # Only --dry-run warns and goes on: it exits immediately below without
    # touching the checkout. --plan would go on to write terraform.tfvars and
    # run lifecycle.sh plan (which reconfigures .terraform/ in repo_dir),
    # diffing one install's configuration against another's state while
    # rewriting the first install's checkout.
    if [ "$PARAM_DRY_RUN" = "true" ]; then
      print_warning "${install_env_file} was written for another install, and this preview reads it anyway:"
      printf '%s' "$coordinate_conflicts" >&2
    else
      print_error "Refusing to upgrade: ${install_env_file} records a different install than the flags name."
      printf '%s' "$coordinate_conflicts" >&2
      print_info "A full upgrade re-renders the PlatformAgent CR from that file, so this would write one install's configuration into another. Point KUBE_AGENTS_INSTALL_ENV at the install.env of the install you are upgrading, run from its checkout, or drop the flags that disagree with it."
      exit 1
    fi
  fi

  if [ "$PARAM_DRY_RUN" = "true" ]; then
    print_step "2. Dry-Run Upgrade Plan Preview"
    echo -e "  • ${C_CYAN}Action:${C_RESET} Perform ${PARAM_UPGRADE_MODE} upgrade on cluster '${target_cluster}'"
    echo -e "  • ${C_CYAN}Image Overrides:${C_RESET} ${REGISTRY_PREFIX:-$DEFAULT_REGISTRY_PREFIX}/*:${PARAM_IMAGE_TAG}"
    echo -e "  • ${C_CYAN}Secrets:${C_RESET} generate SESSION_KV_API_KEY / SESSION_KV_SALT into '${PLATFORM_AGENT_SECRET}' only if absent (existing values are never rewritten)"
    echo -e "  • ${C_CYAN}Secrets:${C_RESET} generate the shell sandbox SSH keypair into '${PLATFORM_AGENT_SECRET}' and '${PLATFORM_AGENT_SHELL_AUTHORIZED_KEYS_SECRET}' only if absent"
    write_report "DRY_RUN_COMPLETE"
    exit 0
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

  # --agent-namespace beats the loaded configuration for this run, the way the
  # three coordinates above do. Empty falls through to install.env's NAMESPACE
  # and then to DEFAULT_NAMESPACE, which is what every run did before the flag.
  local target_namespace="${PARAM_AGENT_NAMESPACE:-${NAMESPACE:-$DEFAULT_NAMESPACE}}"
  export NAMESPACE="$target_namespace"

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
      print_error "Could not read the running image tag from deployment/${PLATFORM_AGENT_DEPLOYMENT} in '${target_namespace}'."
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
    helm upgrade "$KUBE_AGENTS_HELM_RELEASE" "${repo_dir}/charts/kube-agents" \
      --namespace "$target_namespace" --reset-then-reuse-values \
      "${set_args[@]}" --wait --timeout 10m
  }

  # The release guard runs before the tfvars generation on purpose: a
  # pre-Terraform install deserves this message, not whatever the generator
  # trips over first (its install.env may lack the credentials the generator
  # recovers from the live Secret).
  if ! helm status "$KUBE_AGENTS_HELM_RELEASE" -n "$target_namespace" >/dev/null 2>&1; then
    print_error "No Helm release '${KUBE_AGENTS_HELM_RELEASE}' in namespace '$target_namespace'."
    print_info "This install predates the Terraform + Helm engine. Upgrade it with the release that installed it (curl the matching versioned upgrade.sh), or re-install with install.sh to adopt the new engine."
    exit 1
  fi
  # Recover from zombie locks left behind by interrupted or timed-out Helm runs
  if [ "$PARAM_PLAN" != "true" ]; then
    ensure_clean_helm_release "$KUBE_AGENTS_HELM_RELEASE" "$target_namespace"
  else
    # In plan mode, do not mutate state with a rollback, but warn if release is stuck
    local current_helm_status
    current_helm_status="$(helm_release_status "$KUBE_AGENTS_HELM_RELEASE" "$target_namespace")"
    if [[ "$current_helm_status" =~ ^pending- ]]; then
      print_warning "Helm release '${KUBE_AGENTS_HELM_RELEASE}' is currently in '${current_helm_status}'. Note: rollback is skipped in plan mode."
    fi
  fi
  # Snapshot any pre-existing terraform.tfvars in an adopted checkout before
  # write_tfvars_from_state overwrites it: if full or harness later refuses
  # before UPGRADE_APPLY_STARTED, restore_moved_checkout puts the previous
  # tfvars back (or removes the newly created one) alongside the previous commit.
  local tfvars_file="${repo_dir}/terraform/examples/full-install/terraform.tfvars"
  snapshot_moved_checkout_tfvars "$tfvars_file"
  # NAMESPACE steers the generator's Secret-recovery reads (install.env omits
  # credentials when PERSIST_SECRETS_ON_DISK=false; the live Secret has them).
  #
  # KUBE_AGENTS_REQUIRE_MEMORY_ANSWER: an upgrade applies (and --plan renders
  # the same tfvars), so if the generator cannot tell whether this cluster runs
  # the Hindsight memory store and nothing named a memory mode, it must stop
  # rather than fall through to multiuser_memory and plan the database away.
  # An install.env that records MEMORY never reaches that branch. uninstall.sh
  # deliberately does not opt in.
  NAMESPACE="$target_namespace" \
    KUBE_AGENTS_REQUIRE_MEMORY_ANSWER=true \
    write_tfvars_from_state "$tfvars_file" "$PARAM_IMAGE_TAG"

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

  # UPGRADE_APPLY_STARTED is set inside each arm below rather than here, and
  # the difference is load-bearing. Both previews have exited above, so it is
  # tempting to read "past the previews" as "past the point of no return" — but
  # two arms still refuse after the dispatch and before they write anything:
  # full runs the minter/KMS guard and the service-account 409 check, and
  # harness reads the release's values to learn which plugin tags to move. A run
  # that stops on one of those has applied none of the new release, so the
  # checkout this run detached has to go back. Each arm therefore flips the gate
  # on its own last line before its first mutating command, and
  # UpgradeRunContractTest.test_the_apply_gate_sits_after_every_refusal_in_its_arm
  # pins that placement, which is otherwise unreachable from the test suite.
  case "$PARAM_UPGRADE_MODE" in
    operator)
      print_step "4. Upgrading Kubernetes Operator (CRDs & Controller Manager)"
      UPGRADE_APPLY_STARTED="true"
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
      # The plugin images move with them for the same reason, when the
      # release records them: the operator renders them into the gateway as
      # stage-<plugin> init containers or plugin-<name> image volumes, and
      # the image check below reads both. A plain call, not a substitution: a
      # failed read stops the run here, once, with its own message shown.
      harness_retag_keys "$KUBE_AGENTS_HELM_RELEASE" "$target_namespace"
      # After the read, not before it: `helm get values` is the last thing this
      # arm does that can fail without having changed anything.
      UPGRADE_APPLY_STARTED="true"
      helm_retag "${HARNESS_RETAG_KEYS[@]}"
      print_success "Platform Agent deployment upgraded successfully!"
      ;;

    full)
      print_step "4. Executing Full Atomic Upgrade (Terraform + Helm)"
      # install.sh's post-generation minter guard, without its import step:
      # an upgrade never imports the App key, so an install.env that enables the
      # minter against a key with no ENABLED version would wedge the apply on
      # the minter's readiness until the helm timeout fails the upgrade.
      # Refuse up front instead and name the two ways out.
      if grep -q '^enable_github_minter = true$' "$tfvars_file" 2>/dev/null; then
        minter_enabled_version="$(kms_key_enabled_version "${KMS_KEY:-$DEFAULT_KMS_KEY}" \
          "${KMS_KEYRING:-$DEFAULT_KMS_KEYRING}" "$(derive_kms_location "${REGION}")" "${PROJECT_ID}")"
        if [ -z "$minter_enabled_version" ]; then
          print_error "The GitHub minter is enabled in the generated configuration, but its KMS signing key has no ENABLED version — the apply would wait on a minter that can never become ready."
          print_info "Import the App key with install.sh (which runs the import before its apply), or unset GITHUB_APP_ID in install.env to upgrade without the minter."
          exit 1
        fi
      fi
      # Enabling the minter or switching to Vertex through install.env plans a
      # new fixed-name GSA on an install that has been running without one, so
      # the 409 check install.sh runs before its apply runs here too.
      check_service_account_ownership || exit 1
      # Both guards above are refusals, and apply_crd_upgrades is the first
      # write this arm makes, so the gate belongs between them.
      UPGRADE_APPLY_STARTED="true"
      apply_crd_upgrades
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
    if kubectl get deployment "$PLATFORM_AGENT_DEPLOYMENT" -n "$target_namespace" >/dev/null 2>&1; then
      print_info "Restarting the Platform Agent so it reads the newly added Secret keys..."
      kubectl rollout restart "deployment/${PLATFORM_AGENT_DEPLOYMENT}" -n "$target_namespace"
      restarted_agent="true"
    else
      print_warning "Secret keys were added but Deployment '${PLATFORM_AGENT_DEPLOYMENT}' was not found in '$target_namespace'; restart the agent yourself so it reads them."
    fi
  fi

  print_step "5. Post-Upgrade Health Verification"
  kubectl get ns "$target_namespace" >/dev/null
  if [ "$PARAM_UPGRADE_MODE" = "operator" ] || [ "$PARAM_UPGRADE_MODE" = "full" ]; then
    kubectl rollout status "deployment/${KUBE_AGENTS_OPERATOR_DEPLOYMENT}" -n "$target_namespace" --timeout=120s
  fi
  if { [ "$PARAM_UPGRADE_MODE" = "harness" ] || [ "$PARAM_UPGRADE_MODE" = "full" ]; } && \
     [ -n "$PARAM_IMAGE_TAG" ] && [ -f "${repo_dir}/scripts/confirm_agent_image.sh" ]; then
    if kubectl get deployment "$PLATFORM_AGENT_DEPLOYMENT" -n "$target_namespace" >/dev/null 2>&1; then
      "${repo_dir}/scripts/confirm_agent_image.sh" "$target_namespace" "$PLATFORM_AGENT_DEPLOYMENT" "$PARAM_IMAGE_TAG"
    fi
  fi
  if [ "$PARAM_UPGRADE_MODE" = "harness" ] || [ "$PARAM_UPGRADE_MODE" = "full" ] || [ "$restarted_agent" = "true" ]; then
    kubectl rollout status "deployment/${PLATFORM_AGENT_DEPLOYMENT}" -n "$target_namespace" --timeout=900s
  fi
  # A healthy gateway is not a working install. The agent runs no command in its
  # own pod: every shell command goes over ssh to the sandbox StatefulSet, and
  # every credentialed one through the proxy. Verifying only the gateway
  # reported an upgrade that succeeded while the agent could not run kubectl.
  #
  # Guarded on the object existing, because the operator creates both and an
  # operator-mode upgrade can reach here before its PlatformAgent has
  # reconciled. A missing object is that, not a failed rollout.
  if kubectl get statefulset "$PLATFORM_AGENT_SHELL_STATEFULSET" -n "$target_namespace" >/dev/null 2>&1; then
    kubectl rollout status "statefulset/${PLATFORM_AGENT_SHELL_STATEFULSET}" -n "$target_namespace" --timeout="$SANDBOX_ROLLOUT_TIMEOUT"
  fi
  if kubectl get deployment "$PLATFORM_AGENT_CREDENTIAL_PROXY_DEPLOYMENT" -n "$target_namespace" >/dev/null 2>&1; then
    kubectl rollout status "deployment/${PLATFORM_AGENT_CREDENTIAL_PROXY_DEPLOYMENT}" -n "$target_namespace" --timeout="$SANDBOX_ROLLOUT_TIMEOUT"
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
