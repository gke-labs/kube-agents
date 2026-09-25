#!/usr/bin/env bash
# ==============================================================================
# 🧹 Kubernetes Agentic Harness (kube-agents) Complete Uninstall Engine
# ==============================================================================
# Refactored interactive tty confirmation & subshell handling thanks to review by @eLeontev
# Discovers and safely deletes all provisioned GCP resources, GKE clusters,
# IAM service accounts, secrets, and Kubernetes control plane components.
#
# Usage:
#   ./uninstall.sh [options]
#   curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/uninstall.sh | bash
#
# The release-pinned script tears the install down with its own release's
# engine. A copy carrying no baked version falls back to the engine on main,
# which is not the one that built the install.
# ==============================================================================

set -Eeuo pipefail

# This script's own name, for the abort banner when it runs piped through
# stdin (curl | bash): bash then has no file to name its frames after.
UNINSTALL_SCRIPT_NAME="uninstall.sh"

# ANSI Color Tokens
C_CYAN="\033[1;36m"
C_GREEN="\033[1;32m"
C_YELLOW="\033[1;33m"
C_RED="\033[1;31m"
C_BOLD="\033[1m"
C_RESET="\033[0m"

# Where the teardown engine is fetched from when this script runs outside a
# checkout. install.sh and upgrade.sh carry the same URL, each needing it
# before it has a checkout to read it from; tests/test_install_script.py pins
# the three equal.
KUBE_AGENTS_REPO_URL="https://github.com/gke-labs/kube-agents.git"
# The install checkout install.sh leaves when it runs outside one. install.sh
# and upgrade.sh define the same function; tests/test_upgrade_script.py pins
# the paths all three build equal. Every caller here checks HOME first.
kube_agents_clone_dir() { printf '%s/kube-agents' "${HOME:?the teardown looks for the install checkout under HOME when it does not run from one}"; }
# The file whose presence makes a directory a kube-agents checkout this script
# can drive, and the shared installer library inside it.
KUBE_AGENTS_ENGINE_MARKER="terraform/examples/full-install/lifecycle.sh"
KUBE_AGENTS_INSTALLER_COMMON_MARKER="scripts/installer/installer_common.sh"

# Process Lock File & Error Trap Handling
#
# The 2>/dev/null probes whether the lock file can be opened; it must NOT ride
# on the `exec`. `exec` with no command applies its redirections to the shell
# PERMANENTLY, so `exec 200>"$LOCK_FILE" 2>/dev/null` sent every later error
# message — this script's abort banner, lifecycle.sh's, terraform's — to
# /dev/null for the rest of the run. The release pipeline's teardown failed
# that way on every scheduled run for weeks: uninstall.sh exited non-zero with
# nothing on stderr to say why. Same shape as install.sh's lock, deliberately.
LOCK_FILE="/tmp/kube-agents-uninstall.lock"
if command -v flock >/dev/null 2>&1 && ( : >"$LOCK_FILE" ) 2>/dev/null && exec 200>"$LOCK_FILE"; then
  if ! flock -n 200 2>/dev/null; then
    echo -e "  \033[93m⚠ Another instance of kube-agents uninstaller is currently running. Exiting.\033[0m" >&2
    exit 1
  fi
fi

# The one non-zero exit that is not a failure: the target holds no Terraform
# state anywhere, so this engine has nothing to tear down. Expected against a
# clean project, and the case an automated caller must be able to tell apart
# from a teardown that tried and failed — scripts/release/
# provision_environment.sh branches on exactly this. Defined above on_error
# because on_error reads it.
EXIT_NOTHING_TO_TEAR_DOWN=3

on_error() {
  local exit_code="$1"
  local line_no="$2"
  local bash_cmd="$3"
  # Reserve the code. on_error exits with the FAILING COMMAND's status, so any
  # child that happens to exit 3 — a gcloud wrapper, a nested script under
  # lifecycle.sh — would otherwise speak this script's "nothing to tear down"
  # contract and tell an automated caller to install over a live environment.
  # Anything that reaches this trap is a failure by definition.
  if [ "$exit_code" = "$EXIT_NOTHING_TO_TEAR_DOWN" ]; then
    exit_code=1
  fi
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
  # number sent the reader to that line of uninstall.sh instead. Piped through
  # stdin, bash labels this script's frames `main` or not at all, and $0 is
  # `bash`; both read as the script by name.
  local source_file="${BASH_SOURCE[1]:-}"
  case "$source_file" in
    ""|main) source_file="$UNINSTALL_SCRIPT_NAME" ;;
  esac
  local func_name="${FUNCNAME[1]:-main}"
  echo -e "\n\033[91m\033[1m✗ Teardown error encountered at ${source_file}:${line_no} in ${func_name} (exit code ${exit_code}): ${bash_cmd}\033[0m" >&2
  write_report "FAILED" "true" "${line_no}" "${bash_cmd}" 2>/dev/null || true
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

# Sourced/baked release version. On developer checkouts (main), this is empty.
# Release automation stamps this value (e.g. BAKED_RELEASE_VERSION="0.2.0") when publishing a GA release.
BAKED_RELEASE_VERSION=""

PARAM_NON_INTERACTIVE="false"
PARAM_DRY_RUN="false"
PARAM_PROJECT_ID=""
PARAM_CLUSTER_NAME=""
PARAM_REGION=""
# Empty means "whatever the loaded configuration says". The teardown
# regenerates terraform.tfvars before destroying and namespace is one of its
# keys, so this is not cosmetic: it names the release being torn down.
PARAM_AGENT_NAMESPACE=""
PARAM_SOURCE_REF=""
TEMP_REPO_DIR=""

cleanup() {
  if [ -n "$TEMP_REPO_DIR" ] && [ -d "$TEMP_REPO_DIR" ]; then
    rm -rf -- "$TEMP_REPO_DIR"
  fi
}
# Known gap in the exit-code contract above, left in place deliberately. On
# bash 3.2 a `set -u` abort reports $?=0 to this trap and does not fire the ERR
# trap, so such a crash would exit 0. It cannot be rescued from here — the
# status is already lost by the time the trap runs, and returning non-zero from
# an EXIT trap does not change the shell's status (measured). The alternative,
# a sentinel set true before each of the four deliberate exits, turns a
# successful teardown red the first time someone adds a fifth and forgets. No
# reachable unbound variable exists today; a new one would be the real bug.
trap cleanup EXIT

print_banner() {
  echo -e "${C_RED}${C_BOLD}"
  echo '==========================================================================='
  echo '🧹  Kubernetes Agentic Harness (kube-agents) Complete Uninstall Engine'
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

json_escape() {
  local value="${1:-}"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '%s' "$value"
}

show_help() {
  print_banner
  cat << EOF
Usage: ./uninstall.sh [OPTIONS]

Options:
  -y, --yes, --non-interactive  Automated execution mode (no interactive confirmation prompt)
  --dry-run                     Preview uninstall plan without deleting resources
  --gcp-project-id ID           GCP Target Project ID
  --gke-cluster-name NAME       GKE Target Cluster Name (default: platform-agent-host)
  --gcp-region REGION           GKE GCP Region
  --agent-namespace NS          Kubernetes namespace the release lives in
                                (default: the install's own, else kubeagents-system)
  --source-ref REF              Tag or commit SHA of the release that made the install; that
                                release's own uninstall.sh is fetched and run in place of this one
  --help, -h, -?                Show this help message

Examples:
  # Interactively discover and remove kube-agents cluster & GCP resources
  ./uninstall.sh

  # Automated teardown for a known project and cluster
  ./uninstall.sh --non-interactive --gcp-project-id="my-gcp-project" --gke-cluster-name="platform-agent-host"

Exit codes:
  0  Teardown completed (or --dry-run finished, or you declined the
     confirmation).
  3  Nothing to tear down: no Terraform state for this cluster in GCS or
     locally. Either nothing is installed here, or the install predates the
     Terraform engine — see --source-ref. Not a failure.
  1  Anything else — the teardown could not start, or started and did not
     finish.

  --source-ref hands over to the pinned release's own uninstall.sh wholesale,
  so from then on the exit code is that release's, not this contract.
EOF
  exit 0
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -y|--yes|--non-interactive) PARAM_NON_INTERACTIVE="true"; shift ;;
      --dry-run) PARAM_DRY_RUN="true"; shift ;;
      --uninstall|--delete) shift ;;
      --gcp-project-id=*) PARAM_PROJECT_ID="${1#*=}"; shift ;;
      --gcp-project-id) PARAM_PROJECT_ID="$2"; shift 2 ;;
      --gke-cluster-name=*) PARAM_CLUSTER_NAME="${1#*=}"; shift ;;
      --gke-cluster-name) PARAM_CLUSTER_NAME="$2"; shift 2 ;;
      --gcp-region=*) PARAM_REGION="${1#*=}"; shift ;;
      --gcp-region) PARAM_REGION="$2"; shift 2 ;;
      --agent-namespace=*) PARAM_AGENT_NAMESPACE="${1#*=}"; shift ;;
      --agent-namespace) PARAM_AGENT_NAMESPACE="$2"; shift 2 ;;
      --source-ref=*) PARAM_SOURCE_REF="${1#*=}"; shift ;;
      --source-ref) PARAM_SOURCE_REF="$2"; shift 2 ;;
      --help|-h|-\?|help) show_help ;;
      *) print_error "Unknown parameter: $1"; return 2 ;;
    esac
  done
}

write_report() {
  local status="$1"
  local report_file="/tmp/kube-agents-uninstall-report.json"
  cat << EOF > "$report_file"
{
  "status": "$(json_escape "$status")",
  "dry_run": ${PARAM_DRY_RUN},
  "non_interactive": ${PARAM_NON_INTERACTIVE},
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "2026-08-05T00:00:00Z")"
}
EOF
  print_success "Uninstall report written to: $report_file"
}

# Decide WHERE the state lives before pinning the backend. Exporting
# KUBE_AGENTS_STATE_BUCKET first would make lifecycle.sh's ensure_backend
# `terraform init -reconfigure` onto the (possibly empty) remote prefix —
# abandoning a hand-driven install's local state, so the destroy plans
# nothing and reports success with the CR and backups already gone and
# every GCP resource still live.
#
# Needs installer_common.sh sourced (tf_state_bucket/tf_state_prefix) and the
# target coordinates exported. Unit-tested in tests/test_uninstall_script.py;
# keep the branch order — remote state wins, a probe that could not read wins
# over every "absent" branch below it, an explicitly named bucket with no state
# is an error, local state is honoured only when nothing is remote, and no
# state anywhere refuses rather than guesses.
resolve_state_location() {
  local compose_dir="$1"
  local state_object probe_err="" probe_rc=0
  state_object="gs://$(tf_state_bucket)/$(tf_state_prefix)/${TF_STATE_OBJECT}"

  # The probe's stderr is kept, not discarded, because "the object is not
  # there" and "I could not look" are different answers and only the first one
  # means there is nothing to tear down. A bare `>/dev/null 2>&1` collapses 404,
  # 403, expired credentials, a network timeout and a missing gcloud into one
  # bit — and with exit 3 wired to "clean project", the 403 case would tell
  # scripts/release/provision_environment.sh to install over a live cluster.
  #
  # `trap - ERR` inside the substitution: under bash 3.2 the inherited ERR trap
  # fires in this subshell even though the failure is the tested condition. The
  # rc is captured on the assignment rather than declared with `local`, which
  # would report `local`'s own status instead of the substitution's.
  probe_err="$(trap - ERR; gcloud storage cat "$state_object" 2>&1 >/dev/null)" || probe_rc=$?

  if [ "$probe_rc" -eq 0 ]; then
    # Remote state where the installer keeps it (or where the caller pointed
    # us): pin the backend for lifecycle.sh.
    export KUBE_AGENTS_STATE_BUCKET="${KUBE_AGENTS_STATE_BUCKET:-$DEFAULT_KUBE_AGENTS_STATE_BUCKET}"
  elif ! printf '%s' "$probe_err" | grep -qiE "$GCS_OBJECT_ABSENT_PATTERN"; then
    # Anything that is not a clean "absent" — refuse rather than report an
    # empty target. Reaching the local-state branches below on a permission
    # error would be the same mistake one level down.
    print_error "Could not read the Terraform state at ${state_object}: ${probe_err:-unknown gcloud failure}"
    print_info "That is not the same as 'nothing is installed here', so this is a failure rather than an empty target. Fix the access or transport error and re-run."
    return 1
  elif [ -n "${KUBE_AGENTS_STATE_BUCKET:-}" ]; then
    # The caller named a bucket and it holds no state for this cluster:
    # error out rather than fall back to guessing.
    print_error "No Terraform state at gs://$(tf_state_bucket)/$(tf_state_prefix) (KUBE_AGENTS_STATE_BUCKET was set explicitly)."
    return 1
  elif [ -f "${compose_dir}/terraform.tfstate" ] || [ -f "${compose_dir}/backend_override.tf" ]; then
    # A hand-driven install: local state, or an existing backend override
    # pointing wherever its author keeps state. Leave the backend variable
    # unset so lifecycle.sh touches neither.
    print_info "Using the composition's own state (local terraform.tfstate or existing backend_override.tf)."
  else
    print_error "No Terraform state found for '${CLUSTER_NAME}' (gs://$(tf_state_bucket)/$(tf_state_prefix)) and none locally."
    print_info "If this install was made by a pre-Terraform release, re-run with --source-ref=<that release> so its own teardown runs."
    return "$EXIT_NOTHING_TO_TEAR_DOWN"
  fi
}

# Locate the install's configuration:
#   1. KUBE_AGENTS_INSTALL_ENV when explicitly set
#   2. The checkout this script runs from (when it has one)
#   3. The working directory the operator invoked it from
#   4. The install checkout install.sh leaves in $HOME/kube-agents (consulted
#      only on non-checkout runs — when install_checkout is non-empty because
#      neither script_dir nor $(pwd) is a checkout — matching install.sh and
#      upgrade.sh so a developer clone or unpacked bundle without its own
#      install.env never reaches into $HOME/kube-agents/install.env)
# Defined above main() rather than sourced from installer_common.sh because the
# --source-ref arm hands over before any checkout with installer_common.sh is
# sourced, and because the lookup itself decides which checkout's configuration
# to read.
resolve_uninstall_env_file() {
  local candidate_repo_dir="${1:-}"
  local install_checkout="${2:-}"
  if [ -n "${KUBE_AGENTS_INSTALL_ENV:-}" ]; then
    echo "$KUBE_AGENTS_INSTALL_ENV"
  elif [ -n "$candidate_repo_dir" ] && [ -f "${candidate_repo_dir}/install.env" ]; then
    echo "${candidate_repo_dir}/install.env"
  elif [ -f "$(pwd)/install.env" ]; then
    echo "$(pwd)/install.env"
  elif [ -n "$install_checkout" ] && [ -f "${install_checkout}/install.env" ]; then
    echo "${install_checkout}/install.env"
  elif [ -n "$candidate_repo_dir" ]; then
    echo "${candidate_repo_dir}/install.env"
  else
    echo "$(pwd)/install.env"
  fi
}

# What a guessed install.env says about the three command-line coordinates,
# printed as one word:
#   confirms   -- it records all three, and each matches its flag;
#   differs    -- it records one that contradicts its flag;
#   incomplete -- nothing contradicts, but it lacks one of the three, so the
#                 flags cannot confirm it (an absent key is not a match);
#   unreadable -- sourcing it failed, e.g. it expands a variable that is unset
#                 under the inherited `set -u`.
# Only "confirms" lets a guess be read. Called only when all three PARAM_* are
# set.
#
# Read in a SUBSHELL, unlike every other read of this file. It is asked only
# about files the lookup guessed at, and sourcing one into this shell merely to
# inspect it would export its NAMESPACE, MEMORY, GITOPS_* and state-bucket keys
# -- and there is no taking an open-ended set of keys back out again. `set -a`
# is still needed inside, because the file's own assignments are plain. The
# file's own output is discarded so only the verdict reaches stdout; a failure
# while sourcing ends the subshell with no verdict, which is what "unreadable"
# stands for, and the warning the caller prints names it.
guessed_env_verdict() {
  local env_file="$1" verdict=""
  verdict="$(
    unset PROJECT_ID CLUSTER_NAME REGION NAMESPACE
    set -a
    # shellcheck disable=SC1090
    . "$env_file" >/dev/null 2>&1
    set +a
    if { [ -n "${PROJECT_ID:-}" ] && [ "$PROJECT_ID" != "$PARAM_PROJECT_ID" ]; } ||
      { [ -n "${CLUSTER_NAME:-}" ] && [ "$CLUSTER_NAME" != "$PARAM_CLUSTER_NAME" ]; } ||
      { [ -n "${REGION:-}" ] && [ "$REGION" != "$PARAM_REGION" ]; }; then
      echo "differs"
    elif [ -z "${PROJECT_ID:-}" ] || [ -z "${CLUSTER_NAME:-}" ] || [ -z "${REGION:-}" ]; then
      echo "incomplete"
    else
      echo "confirms"
    fi
  )" || verdict=""
  printf '%s\n' "${verdict:-unreadable}"
}

# Whether resolve_uninstall_env_file reached env_file only through its
# last-resort step, ${install_checkout}/install.env: a file nobody named, found
# by searching $HOME, rather than one reached through KUBE_AGENTS_INSTALL_ENV,
# the checkout or the working directory. On a workstation holding a current
# install it belongs to THAT install, whichever one the flags name. Shared by
# the local and --source-ref arms so both judge provenance the same way.
env_file_is_a_home_guess() {
  local env_file="$1" candidate_repo_dir="$2" install_checkout="$3"
  [ -z "${KUBE_AGENTS_INSTALL_ENV:-}" ] &&
    { [ -z "$candidate_repo_dir" ] || [ ! -f "${candidate_repo_dir}/install.env" ]; } &&
    [ ! -f "$(pwd)/install.env" ] &&
    [ -n "$install_checkout" ] &&
    [ "$env_file" = "${install_checkout}/install.env" ] &&
    [ -f "$env_file" ]
}

# Why a $HOME guess must not be read on a run whose three coordinate flags are
# all set, as a clause for the "Not reading" warning; nothing when it confirms
# them. A guess nobody named must not refuse or abort a teardown the flags fully
# describe: loading exits on a file that is not valid shell or fails while
# sourced, and the coordinate check refuses one that disagrees -- right for a
# file the operator pointed at, wrong for one found by searching $HOME.
guessed_env_skip_reason() {
  local env_file="$1"
  if ! bash -n "$env_file" 2>/dev/null; then
    echo "it is not valid shell"
    return 0
  fi
  case "$(guessed_env_verdict "$env_file")" in
    confirms) ;;
    differs) echo "it records a different install" ;;
    incomplete) echo "it does not record all of PROJECT_ID, CLUSTER_NAME and REGION, so the flags cannot confirm it belongs to the install being torn down" ;;
    *) echo "it failed while being read (such as expanding a variable that is not set)" ;;
  esac
}

# Compare the command-line coordinates against what install.env itself recorded.
# A piped teardown loads $HOME/kube-agents/install.env (with `set -a`) whichever
# cluster the flags name, so without this check every non-coordinate setting in
# install A's file -- NAMESPACE, MEMORY, GITOPS_*, PLATFORM_AGENT_GSA_NAME, and
# custom KUBE_AGENTS_STATE_BUCKET / KUBE_AGENTS_STATE_PREFIX keys -- stays in
# the environment and steers the state lookup and terraform.tfvars generation
# for install B. Same split as upgrade.sh: a real run refuses, a dry-run warns.
check_uninstall_coordinate_conflicts() {
  local env_file="$1"
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
    if [ "$PARAM_DRY_RUN" = "true" ]; then
      print_warning "${env_file} was written for another install, and this preview reads it anyway:"
      printf '%s' "$coordinate_conflicts" >&2
    else
      print_error "Refusing to tear down: ${env_file} records a different install than the flags name."
      printf '%s' "$coordinate_conflicts" >&2
      print_info "Teardown resolves its Terraform state backend and regenerates terraform.tfvars from that file, so this would read one install's configuration while tearing down another. Point KUBE_AGENTS_INSTALL_ENV at the install.env of the install you are tearing down, run from its checkout, or drop the flags that disagree with it."
      exit 1
    fi
  fi
}

main() {
  parse_args "$@"
  print_banner

  # An explicit pointer at a file that is not there is a typo, and every arm
  # below reads it: the --source-ref handover forwards the coordinates it holds
  # to the pinned release, and the local arms regenerate terraform.tfvars from
  # it. Continuing would fall back to DEFAULT_CLUSTER_NAME, DEFAULT_REGION and
  # gcloud's active project -- on a GCE host, the machine's own project -- and
  # aim a destroy at whatever that names.
  #
  # This is not the data-protection kind of check a teardown must never be
  # blocked by (see the ENABLE_GVISOR note further down): unsetting the variable
  # or fixing the path clears it, and a teardown with no pointer at all still
  # runs on flags alone. install.sh's bootstrap_install_env and upgrade.sh
  # refuse the same way, on the same INSTALL_ENV_EXPLICIT reasoning.
  if [ -n "${KUBE_AGENTS_INSTALL_ENV:-}" ] && [ ! -f "${KUBE_AGENTS_INSTALL_ENV}" ]; then
    print_error "KUBE_AGENTS_INSTALL_ENV names '${KUBE_AGENTS_INSTALL_ENV}', which does not exist."
    print_info "Point it at the install's install.env, or unset it to search this checkout, the current directory, and the install checkout in \$HOME/kube-agents."
    exit 1
  fi

  print_step "1. Discovering Installed Infrastructure Elements"

  local script_dir repo_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -n "$PARAM_SOURCE_REF" ]; then
    # The pinned release owns its own teardown: clone it and hand over
    # wholesale. This script's engine (installer_common.sh, lifecycle.sh
    # destroy) exists only at post-Terraform refs, and --source-ref exists
    # precisely for the installs those refs did not make — so continuing in
    # this main() would source files the clone does not carry. The exec also
    # releases the flock for the dispatched script and skips the temp-dir
    # cleanup trap, which must not delete a tree that is still executing.
    #
    # Resolve install.env here before handing over: the cloned script's own
    # repo_dir is the fresh temp clone below, which carries no configuration,
    # and pre-0.4.0 releases do not read install.env at all. Forwarding the
    # resolved coordinates in the target script's flag dialect (and exporting
    # KUBE_AGENTS_INSTALL_ENV for 0.4.0+ refs) keeps the documented `--source-ref`
    # one-liner aimed at the install checkout rather than falling back to
    # DEFAULT_CLUSTER_NAME and gcloud's active project in the child.
    local wrapper_checkout=""
    if [ -f "${script_dir}/${KUBE_AGENTS_ENGINE_MARKER}" ]; then
      wrapper_checkout="$script_dir"
    elif [ -f "$(pwd)/${KUBE_AGENTS_ENGINE_MARKER}" ]; then
      wrapper_checkout="$(pwd)"
    fi
    local handoff_install_checkout=""
    if [ -z "$wrapper_checkout" ] && [ -n "${HOME:-}" ]; then
      handoff_install_checkout="$(kube_agents_clone_dir)"
    fi
    local handoff_env_file
    handoff_env_file="$(resolve_uninstall_env_file "$wrapper_checkout" "$handoff_install_checkout")"

    # Provenance decides whether that answer is an instruction or a guess, so
    # the branch it came from is recomputed here. The last-resort arm of the
    # lookup is $HOME/kube-agents/install.env -- the installer's own checkout,
    # which on a workstation holding a current install belongs to THAT install.
    # This arm is where that matters most: --source-ref exists precisely for the
    # installs the post-Terraform refs did not make, and an install old enough
    # to need it has no install.env of its own for the lookup to find first.
    local handoff_env_is_a_guess="false"
    local handoff_env_was_dropped="false"
    if env_file_is_a_home_guess "$handoff_env_file" "$wrapper_checkout" "$handoff_install_checkout"; then
      handoff_env_is_a_guess="true"
    fi
    # A $HOME guess is never read unless all three command-line coordinates
    # confirm it. Any file at $HOME/kube-agents/install.env was written by a
    # >= 0.4.0 install (pre-Terraform releases wrote no install.env), so reading
    # it on a flagless or partially-flagged --source-ref run would fill the
    # unnamed coordinates (such as CLUSTER_NAME when only project and region are
    # given) from the workstation's current install and aim the legacy
    # uninstaller at that cluster. And when all three coordinates ARE on the
    # command line and contradict the guess, dropping it avoids the coordinate
    # conflict refusal below and keeps `set -a` from exporting the stranger's
    # NAMESPACE, MEMORY, GITOPS_* and state-bucket keys into the child release.
    #
    # Only when all three coordinates are given on the command line and the
    # $HOME file records all three and matches each is it read, so its
    # NAMESPACE and other settings travel with the confirmed target. A file
    # that omits a key is not confirmed by it: any flag would "agree" with an
    # absent PROJECT_ID, and loading it would hand the child that file's state
    # backend under another install's name.
    if [ "$handoff_env_is_a_guess" = "true" ] &&
      { [ -z "$PARAM_PROJECT_ID" ] || [ -z "$PARAM_CLUSTER_NAME" ] || [ -z "$PARAM_REGION" ]; } &&
      [ -f "$handoff_env_file" ]; then
      print_warning "Not reading ${handoff_env_file}: --source-ref is for tearing down an older release, this file was found only by searching \$HOME, and not all three of --gcp-project-id, --gke-cluster-name and --gcp-region were given to confirm it belongs to the install being torn down."
      print_info "Pass all three of --gcp-project-id, --gke-cluster-name and --gcp-region to name the install to tear down, or point KUBE_AGENTS_INSTALL_ENV at ${handoff_env_file} (or run from ${handoff_install_checkout}) if that file is the one you mean."
      handoff_env_file=""
      handoff_env_was_dropped="true"
    elif [ "$handoff_env_is_a_guess" = "true" ]; then
      # All three flags are set here.
      local handoff_env_skip_reason
      handoff_env_skip_reason="$(guessed_env_skip_reason "$handoff_env_file")"
      if [ -n "$handoff_env_skip_reason" ]; then
        print_warning "Not reading ${handoff_env_file}: ${handoff_env_skip_reason}, it was found by searching \$HOME rather than named, and the flags already say which install to tear down."
        print_info "Forwarding --gcp-project-id=${PARAM_PROJECT_ID}, --gke-cluster-name=${PARAM_CLUSTER_NAME} and --gcp-region=${PARAM_REGION} to the '${PARAM_SOURCE_REF}' release. Point KUBE_AGENTS_INSTALL_ENV at an install.env to have one read instead."
        handoff_env_file=""
        handoff_env_was_dropped="true"
      fi
    fi

    unset PROJECT_ID CLUSTER_NAME REGION NAMESPACE
    if [ -n "$handoff_env_file" ] && [ -f "$handoff_env_file" ]; then
      if ! bash -n "$handoff_env_file" 2>/dev/null; then
        print_error "Install configuration '$handoff_env_file' is not valid shell and could not be loaded."
        exit 1
      fi
      set -a
      # shellcheck disable=SC1090
      . "$handoff_env_file"
      set +a
      print_success "Loaded install configuration from: ${handoff_env_file}"
      check_uninstall_coordinate_conflicts "$handoff_env_file"
      export KUBE_AGENTS_INSTALL_ENV="$handoff_env_file"
    else
      # Silence here is what makes this arm dangerous. Nothing is forwarded, and
      # the pinned release -- which for pre-0.4.0 refs does not read install.env
      # at all -- falls straight back to DEFAULT_CLUSTER_NAME, DEFAULT_REGION and
      # gcloud's active project, which on a GCE host is the machine's own. Same
      # shape of warning as the local arms print, and a warning rather than a
      # refusal because a teardown on flags alone is a supported way to run this
      # (I4: an install has to keep a working way to remove itself).
      #
      # Skipped when a file was found and deliberately not read: the warning
      # above already named it and said why, and "none was found" would
      # contradict it.
      if [ "$handoff_env_was_dropped" != "true" ]; then
        local handoff_searched="${PWD}"
        if [ -n "$wrapper_checkout" ] && [ "$wrapper_checkout" != "${PWD}" ]; then
          handoff_searched="${wrapper_checkout} or ${handoff_searched}"
        fi
        if [ -n "$handoff_install_checkout" ] && [ "$handoff_install_checkout" != "${PWD}" ]; then
          handoff_searched="${handoff_searched} or ${handoff_install_checkout}"
        fi
        print_warning "No install configuration (install.env) was found in ${handoff_searched}."
      fi
    fi

    # What the child will actually be told, flag-or-file per coordinate. Computed
    # here rather than next to dispatch_args below so that the report after it
    # comes before the clone, alongside the warnings above it.
    local eff_project_id="${PARAM_PROJECT_ID:-${PROJECT_ID:-}}"
    local eff_cluster_name="${PARAM_CLUSTER_NAME:-${CLUSTER_NAME:-}}"
    local eff_region="${PARAM_REGION:-${REGION:-}}"
    local eff_agent_namespace="${PARAM_AGENT_NAMESPACE:-${NAMESPACE:-}}"

    # Every coordinate the child will NOT be told, named once and from the values
    # above rather than per arm. Per-arm warnings covered "no file" and "a $HOME
    # guess not read", but not a loaded install.env that lacks one of the three
    # keys: that coordinate was silently not forwarded and the pinned release
    # fell back to its own default. Asking what is about to be forwarded covers
    # every arm that exists now and any added later. A warning, not a refusal:
    # a teardown on partial configuration is supported (I4).
    local unforwarded=""
    [ -n "$eff_project_id" ] || unforwarded="${unforwarded:+${unforwarded}, }--gcp-project-id"
    [ -n "$eff_cluster_name" ] || unforwarded="${unforwarded:+${unforwarded}, }--gke-cluster-name"
    [ -n "$eff_region" ] || unforwarded="${unforwarded:+${unforwarded}, }--gcp-region"
    if [ -z "$eff_project_id" ] && [ -z "$eff_cluster_name" ] && [ -z "$eff_region" ]; then
      print_warning "No coordinates are being forwarded, so the '${PARAM_SOURCE_REF}' release will aim at its own defaults and gcloud's active project."
    elif [ -n "$unforwarded" ]; then
      local project_fallback=""
      if [ -z "$eff_project_id" ]; then
        project_fallback=" (gcloud's active project, for the project)"
      fi
      print_warning "Not forwarding ${unforwarded} to the '${PARAM_SOURCE_REF}' release: neither the command line nor a loaded install.env gives a value, so that release will fall back to its own default${project_fallback}."
    fi
    # The dropped-guess arm above already named its own remedy, pointing at the
    # file it did not read.
    if [ -n "$unforwarded" ] && [ "$handoff_env_was_dropped" != "true" ]; then
      print_info "Pass --gcp-project-id/--gke-cluster-name/--gcp-region, or point KUBE_AGENTS_INSTALL_ENV at the install's install.env, to name the install you mean."
    fi

    TEMP_REPO_DIR="$(mktemp -d)"
    repo_dir="${TEMP_REPO_DIR}/kube-agents"
    print_info "Fetching the teardown engine pinned at '${PARAM_SOURCE_REF}'..."
    git clone --filter=blob:none --no-checkout "$KUBE_AGENTS_REPO_URL" "$repo_dir"
    git -C "$repo_dir" fetch --depth=1 origin "$PARAM_SOURCE_REF"
    git -C "$repo_dir" checkout --detach FETCH_HEAD
    if [ ! -f "${repo_dir}/uninstall.sh" ]; then
      print_error "'${PARAM_SOURCE_REF}' carries no uninstall.sh; tear the install down with that release's documented procedure."
      exit 1
    fi
    # FLAG DIALECT, chosen from the script we are about to exec rather than
    # assumed. --source-ref reaches in both directions: releases cut before the
    # domain-scoped rename parse --project-id/--cluster-name/--region and reject
    # the new spellings, and releases cut from this commit on do the exact
    # opposite. Hard-coding either one makes the hand-over fail with "Unknown
    # parameter" against half the refs the flag exists for, and that failure
    # exits 2 -- not the 3 an automated caller reads as "nothing to tear down".
    #
    # Grepping the cloned script is the only signal available here: the ref is a
    # tag or a SHA, so there is no version to compare against.
    local flag_project_id="--project-id"
    local flag_cluster_name="--cluster-name"
    local flag_region="--region"
    if grep -q -- '--gcp-project-id' "${repo_dir}/uninstall.sh"; then
      flag_project_id="--gcp-project-id"
      flag_cluster_name="--gke-cluster-name"
      flag_region="--gcp-region"
    fi
    local dispatch_args=()
    if [ "$PARAM_NON_INTERACTIVE" = "true" ]; then
      dispatch_args+=(--non-interactive)
    fi
    if [ "$PARAM_DRY_RUN" = "true" ]; then
      dispatch_args+=(--dry-run)
    fi
    if [ -n "$eff_project_id" ]; then
      dispatch_args+=("${flag_project_id}=$eff_project_id")
    fi
    if [ -n "$eff_cluster_name" ]; then
      dispatch_args+=("${flag_cluster_name}=$eff_cluster_name")
    fi
    if [ -n "$eff_region" ]; then
      dispatch_args+=("${flag_region}=$eff_region")
    fi
    # Only to a release that parses it. Older ones do not, and passing it there
    # is the same "Unknown parameter" exit the dialect choice above avoids.
    if [ -n "$eff_agent_namespace" ] && grep -q -- '--agent-namespace' "${repo_dir}/uninstall.sh"; then
      dispatch_args+=(--agent-namespace="$eff_agent_namespace")
    fi
    print_info "Handing over to the '${PARAM_SOURCE_REF}' release's own uninstall.sh..."
    TEMP_REPO_DIR=""
    exec bash "${repo_dir}/uninstall.sh" "${dispatch_args[@]}"
  elif [ -f "${script_dir}/${KUBE_AGENTS_ENGINE_MARKER}" ]; then
    repo_dir="$script_dir"
  elif [ -f "$(pwd)/${KUBE_AGENTS_ENGINE_MARKER}" ]; then
    repo_dir="$(pwd)"
  else
    TEMP_REPO_DIR="$(mktemp -d)"
    repo_dir="${TEMP_REPO_DIR}/kube-agents"
    if [ -n "${BAKED_RELEASE_VERSION:-}" ]; then
      print_info "Fetching the teardown engine for baked release '${BAKED_RELEASE_VERSION}'..."
      git clone --filter=blob:none --no-checkout "$KUBE_AGENTS_REPO_URL" "$repo_dir"
      git -C "$repo_dir" fetch --depth=1 origin "$BAKED_RELEASE_VERSION"
      git -C "$repo_dir" checkout --detach FETCH_HEAD
    else
      print_warning "No --source-ref given; fetching the teardown engine from main, which may be newer than your installed release."
      git clone --depth=1 "$KUBE_AGENTS_REPO_URL" "$repo_dir"
    fi
  fi
  # Defaults, validators, and the terraform.tfvars generator shared with
  # install.sh. Print helpers are already defined above, as the file expects.
  # shellcheck disable=SC1091
  source "${repo_dir}/${KUBE_AGENTS_INSTALLER_COMMON_MARKER}"
  # install.env is optional here: unlike upgrade.sh, a teardown can proceed on
  # --gcp-project-id/--gke-cluster-name/--gcp-region alone. On a non-checkout
  # run (TEMP_REPO_DIR is non-empty, e.g. curl … | bash), the lookup reaches
  # $HOME/kube-agents/install.env as step 4; on a checkout run without its own
  # install.env, install_checkout stays empty so the run never reaches into
  # $HOME/kube-agents/install.env and destroys another install.
  local install_checkout=""
  if [ -n "${TEMP_REPO_DIR:-}" ] && [ -n "${HOME:-}" ]; then
    install_checkout="$(kube_agents_clone_dir)"
  fi
  local install_env_file
  install_env_file="$(resolve_uninstall_env_file "$repo_dir" "$install_checkout")"
  # The same rule the --source-ref arm applies. With all three coordinates on
  # the command line, a file found only by searching $HOME is read only when it
  # records and matches all three; otherwise it would refuse on the coordinate
  # check (or abort while loading) a teardown the flags fully name, and every
  # way out that refusal offers is closed: the install being torn down has no
  # install.env here to point at or run from, and dropping the flags aims the
  # run at the other install. With fewer flags it is still loaded and still
  # checked, since it supplies the coordinates the flags left out.
  if [ -n "$PARAM_PROJECT_ID" ] && [ -n "$PARAM_CLUSTER_NAME" ] && [ -n "$PARAM_REGION" ] &&
    env_file_is_a_home_guess "$install_env_file" "$repo_dir" "$install_checkout"; then
    local install_env_skip_reason
    install_env_skip_reason="$(guessed_env_skip_reason "$install_env_file")"
    if [ -n "$install_env_skip_reason" ]; then
      print_warning "Not reading ${install_env_file}: ${install_env_skip_reason}, it was found by searching \$HOME rather than named, and the flags already say which install to tear down."
      print_info "Tearing down on --gcp-project-id=${PARAM_PROJECT_ID}, --gke-cluster-name=${PARAM_CLUSTER_NAME} and --gcp-region=${PARAM_REGION}. Point KUBE_AGENTS_INSTALL_ENV at an install.env to have one read instead."
      install_env_file=""
    fi
  fi
  local state_loaded="false"
  # Clear any shell-exported coordinates before sourcing the file: load_install_env
  # only unsets NAMESPACE, so without this an exported REGION or PROJECT_ID in
  # the operator's shell would suppress the guessed-coordinate warning below or
  # trigger a false conflict blaming install.env.
  unset PROJECT_ID CLUSTER_NAME REGION
  if load_install_env "$install_env_file"; then
    state_loaded="true"
    print_success "Loaded install configuration from: ${install_env_file}"
  fi
  # GITOPS_ORG / GITOPS_REPO are the names; a configuration still carrying
  # GITHUB_ORG / GITHUB_REPO is accepted with a warning.
  normalize_gitops_repo_vars
  # install.env records the operator-facing MEMORY; MEMORY_PROVIDER is the name
  # the generator reads. This teardown regenerates tfvars before destroying.
  normalize_memory_vars

  local target_project="${PARAM_PROJECT_ID:-${PROJECT_ID:-}}"
  local target_cluster="${PARAM_CLUSTER_NAME:-${CLUSTER_NAME:-$DEFAULT_CLUSTER_NAME}}"
  local target_region="${PARAM_REGION:-${REGION:-$DEFAULT_REGION}}"
  # Which of the three nobody actually named. A teardown is allowed to run on
  # defaults -- that is what makes `./uninstall.sh` in a checkout work -- but it
  # is not allowed to be quiet about it: the same three lines are printed
  # whether they name the install the operator meant or a guess, and the
  # confirmation prompt below reads exactly those lines.
  local guessed_coordinates=""
  if [ -z "$PARAM_CLUSTER_NAME" ] && [ -z "${CLUSTER_NAME:-}" ]; then
    guessed_coordinates="${guessed_coordinates}    cluster '${target_cluster}' is installer_common.sh's default, not this install's"$'\n'
  fi
  if [ -z "$PARAM_REGION" ] && [ -z "${REGION:-}" ]; then
    guessed_coordinates="${guessed_coordinates}    region '${target_region}' is installer_common.sh's default, not this install's"$'\n'
  fi

  if [ -z "$target_project" ]; then
    target_project="$(gcloud config get-value project 2>/dev/null || true)"
    if [ -n "$target_project" ]; then
      # gcloud answers from the user's configuration, and on a GCE instance
      # (Cloud Shell, a Cloudtop, a CI runner) from the metadata server -- which
      # names the project the machine lives in, not the one being torn down.
      guessed_coordinates="${guessed_coordinates}    project '${target_project}' came from gcloud's active configuration, not from this install"$'\n'
    fi
  fi
  if [ -z "$target_project" ]; then
    print_error "A GCP project is required. Pass --gcp-project-id or configure one with gcloud."
    exit 1
  fi

  print_info "GCP Target Project: ${C_BOLD}${target_project}${C_RESET}"
  print_info "GKE Target Cluster: ${C_BOLD}${target_cluster}${C_RESET} (${target_region})"
  if [ "$state_loaded" = "true" ]; then
    check_uninstall_coordinate_conflicts "$install_env_file"
  fi
  if [ -n "$guessed_coordinates" ]; then
    if [ "$state_loaded" = "true" ]; then
      print_warning "Some of what this teardown is aimed at was not recorded in ${install_env_file}:"
    else
      print_warning "No install configuration (install.env) was found, so some of what this teardown is aimed at is a guess:"
    fi
    printf '%s' "$guessed_coordinates" >&2
    print_info "Pass --gcp-project-id/--gke-cluster-name/--gcp-region, or point KUBE_AGENTS_INSTALL_ENV at the install's install.env, to say which install this is."
  fi
  if [ "$PARAM_DRY_RUN" = "true" ]; then
    print_step "2. Dry-Run Uninstall Preview"
    echo -e "  • ${C_CYAN}Target Cluster:${C_RESET} ${target_cluster} in ${target_project} (${target_region})"
    write_report "DRY_RUN_COMPLETE"
    exit 0
  fi

  # Coordinates are settled, so publish them: tf_state_bucket, tf_state_prefix
  # and write_tfvars_from_state all read the environment rather than arguments.
  export PROJECT_ID="$target_project"
  export CLUSTER_NAME="$target_cluster"
  export REGION="$target_region"
  # write_tfvars_from_state writes `namespace` from this, so --agent-namespace
  # has to win over the loaded configuration here the way the three above do.
  if [ -n "$PARAM_AGENT_NAMESPACE" ]; then
    export NAMESPACE="$PARAM_AGENT_NAMESPACE"
  fi
  export NO_CONFIRM="1"

  # The engine is `lifecycle.sh destroy` against the install's Terraform state
  # in GCS (derived from the coordinates, so a fresh clone finds it). With no
  # state anywhere there is either nothing installed here or an install that
  # predates the Terraform engine — this uninstaller cannot take the second
  # apart, but the release that installed it can, which is what --source-ref
  # pins.
  local compose_dir="${repo_dir}/terraform/examples/full-install"
  export KUBE_AGENTS_STATE_PREFIX
  KUBE_AGENTS_STATE_PREFIX="$(tf_state_prefix)"

  # Deciding there is nothing to tear down costs one `gcloud storage cat` and
  # two file tests, and it runs BEFORE the terraform gate below on purpose: a
  # target with no state needs no teardown and therefore no teardown engine, so
  # gating on terraform first would answer "your machine is missing a tool" to
  # a question that is really "there is nothing here". That ordering is what
  # makes exit 3 reachable on a clean project without terraform installed.
  #
  # `|| exit $?` rather than `|| exit 1`: the no-state-anywhere branch returns
  # EXIT_NOTHING_TO_TEAR_DOWN, and flattening it to 1 is what left a caller
  # unable to tell "nothing was installed" from "the teardown broke".
  resolve_state_location "$compose_dir" || exit $?

  # terraform is the teardown engine, not an optional extra: lifecycle.sh's
  # first act is `terraform init`. Checked before the confirmation prompt and
  # before anything is destroyed, because the alternative is a bare "terraform:
  # command not found" from three subshells down. install.sh auto-installs the
  # binary and this script deliberately does not, so a CI job that only ever
  # runs install.sh has terraform, while the same job's teardown, running
  # first, has none. Three things deliberately run before this and the reasons
  # are with them: the --source-ref hand-over, the --dry-run preview, and the
  # state probe. The cost of not being first is that the engine-fetch branch
  # may already have cloned into a temp dir — a side effect, if a self-cleaning
  # one — before this refuses.
  if ! command -v terraform >/dev/null 2>&1; then
    print_error "terraform is not installed, and it is the teardown engine — nothing can be destroyed without it."
    print_info "Install it (https://developer.hashicorp.com/terraform/install) and re-run. install.sh auto-installs terraform; this script does not."
    exit 1
  fi

  if [ "$PARAM_NON_INTERACTIVE" != "true" ]; then
    echo -e "\n${C_RED}${C_BOLD}⚠️  WARNING: This will PERMANENTLY DELETE all kube-agents infrastructure in GCP project '${target_project}'!${C_RESET}"
    local confirm_choice=""
    if [ -c /dev/tty ] && ( : </dev/tty ) 2>/dev/null; then
      read -rp "Are you sure you want to proceed with complete uninstallation? (y/N): " confirm_choice </dev/tty >/dev/tty || confirm_choice=""
    else
      print_error "No interactive terminal is available. Re-run with --non-interactive only after reviewing the target."
      exit 1
    fi
    if [[ ! "$confirm_choice" =~ ^[Yy]$ ]]; then
      print_warning "Uninstall cancelled by user."
      exit 0
    fi
  fi

  print_step "2. Executing Automated Teardown Engine"

  # terraform destroy still evaluates the configuration, so required variables
  # must be present even from a fresh clone; the placeholder key feeds nothing
  # that survives the destroy.
  export API_SERVER_KEY="${API_SERVER_KEY:-uninstall-placeholder}"

  # A destroy needs no sandbox, so it must not be refusable on the sandbox's
  # account. write_tfvars_from_state runs the Autopilot version-floor check
  # whenever ENABLE_GVISOR is truthy, and the block above has just loaded an
  # install.env that — since the installer default flipped — says "true" on every
  # new install. Against a sub-floor Autopilot cluster that check returns 1
  # under `set -Eeuo pipefail` and the destroy never runs, which is an install
  # with no working way to remove itself. The reachable route there is the
  # probe's own "could not read the GKE version" branch: it proceeds, the
  # install applies everything and then fails its post-apply gate, and the
  # half-built install is exactly what someone then tries to uninstall.
  #
  # Forcing false is safe rather than merely expedient. `terraform destroy`
  # destroys what is in state regardless of a resource's count, and the gvisor
  # node pool goes with the cluster in any case; the only thing these two
  # values change here is whether the floor check gets to abort.
  export ENABLE_GVISOR="false"
  write_tfvars_from_state "${compose_dir}/terraform.tfvars"
  (
    cd "$compose_dir"
    ./lifecycle.sh destroy -auto-approve -input=false
  )
  # install.env stays. It is the operator's own file, not something this tool
  # generated, and deleting it would throw away the configuration a re-install
  # would otherwise reuse. Say so rather than leaving a file behind silently.
  if [ -f "$install_env_file" ]; then
    print_info "Left your install configuration in place: ${install_env_file}"
  fi

  write_report "SUCCESS"

  print_step "🎉 Uninstall Complete!"
  echo -e "${C_GREEN}${C_BOLD}🏆 All kube-agents infrastructure elements have been safely removed.${C_RESET}"
  print_info "Kept by design: the Cloud KMS key rings (GCP cannot delete them; the next install adopts them) and the Terraform state bucket gs://$(tf_state_bucket)."
  print_info "Cluster-level settings kept on pre-existing clusters: CMEK database encryption, Workload Identity pool, GKE_METADATA node pool migrations, and Calico NetworkPolicy are preserved and not reverted."
  print_info "If this project will not host kube-agents again, delete the bucket yourself: gcloud storage rm -r gs://$(tf_state_bucket)"
}

if [ "${KUBE_AGENTS_SOURCE_ONLY:-false}" != "true" ]; then
  main "$@"
else
  echo "ℹ️ Sourced uninstall.sh functions without executing main (KUBE_AGENTS_SOURCE_ONLY=true)." >&2
fi
