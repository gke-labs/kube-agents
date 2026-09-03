#!/usr/bin/env bash
# Renders an install.env from a GitHub environment's variables and secrets.
#
# install.env is the installer's configuration input (see install.env.example).
# On a workstation it is hand-authored and lives beside install.sh. A GitHub
# runner is ephemeral and has no such file, so every job that drives the
# installer renders one here first and points KUBE_AGENTS_INSTALL_ENV at it.
#
# This is the mapping from `vars.*`/`secrets.*` to install configuration for
# every path that reconciles an environment in place -- the nightly reconcile,
# the drift plan, and a manual dispatch of either.
#
# It is NOT the only such mapping in the repository. provision_environment.sh
# builds an `install.sh` flag list from the same GitHub variables for the
# destroy-and-rebuild path, and carries its own copy of the MEMORY_PROVIDER
# translation below. The two agree today and are checked against each other by
# tests/test_render_install_env.py and tests/test_provision_environment.py,
# which is the only thing holding them together: collapsing them into one is
# worth doing and has not been done.
#
# Usage:
#   render_install_env.sh <output-path> [--strict]
#
# --strict additionally requires every setting whose absence would REMOVE
# something from an install that already exists. See REQUIRED_STRICT below for
# why that list is not the same as the one an ephemeral environment needs.
#
# Reads its inputs from the environment, so the calling workflow step decides
# what a variable resolves to and this script never reaches for `vars.` itself.
set -euo pipefail

OUT_PATH="${1:-}"
STRICT="false"
if [ "${2:-}" = "--strict" ]; then
  STRICT="true"
fi

if [ -z "$OUT_PATH" ]; then
  echo "usage: render_install_env.sh <output-path> [--strict]" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# The variable contract
# ---------------------------------------------------------------------------
# Left of the colon: the install.env key. Right: the GitHub variable or secret
# name the workflow exports it under. They differ because the GitHub side was
# named for CI ("GCP_PROJECT_ID", the project CI deploys to) and the installer
# side for the install ("PROJECT_ID", the project the agent runs in), and
# renaming either now would break every environment at once.
#
# A key with no value is omitted from the file entirely rather than written
# empty. install.defaults.env then supplies the default, which is the whole
# point of #1081's precedence chain — an empty assignment would beat it and
# mean "explicitly nothing", which for MEMORY or PERMISSION_SET is a different
# install from the default one.
MAPPING="
PROJECT_ID:GCP_PROJECT_ID
REGION:GCP_REGION
CLUSTER_NAME:GKE_CLUSTER_NAME
CLUSTER_MODE:CLUSTER_MODE
MODEL_PROVIDER:MODEL_PROVIDER
MODEL_DEFAULT_NAME:MODEL_DEFAULT_NAME
VERTEX_PROJECT_ID:VERTEX_PROJECT_ID
VERTEX_LOCATION:VERTEX_LOCATION
GEMINI_API_KEY:GEMINI_API_KEY
ANTHROPIC_API_KEY:ANTHROPIC_API_KEY
OPENAI_API_KEY:OPENAI_API_KEY
GOOGLE_CHAT_ENABLED:GOOGLE_CHAT_ENABLED
GOOGLE_CHAT_MODE:GOOGLE_CHAT_MODE
CHAT_TOPIC_NAME:CHAT_TOPIC_NAME
CHAT_SUB_NAME:CHAT_SUB_NAME
ALLOWED_USERS:ALLOWED_USERS
SLACK_ENABLED:SLACK_ENABLED
SLACK_BOT_TOKEN:SLACK_BOT_TOKEN
SLACK_APP_TOKEN:SLACK_APP_TOKEN
SLACK_ALLOWED_USERS:SLACK_ALLOWED_USERS
SLACK_HOME_CHANNEL:SLACK_HOME_CHANNEL
SLACK_HOME_CHANNEL_NAME:SLACK_HOME_CHANNEL_NAME
GITOPS_ORG:GITOPS_ORG
GITOPS_REPO:GITOPS_REPO
GITHUB_APP_ID:GITHUB_APP_ID
KMS_KEYRING:KMS_KEYRING
KMS_KEY:KMS_KEY
PLATFORM_AGENT_PERMISSION_SET:PLATFORM_AGENT_PERMISSION_SET
PLATFORM_AGENT_CUSTOM_ROLES:PLATFORM_AGENT_CUSTOM_ROLES
ENABLE_GVISOR:ENABLE_GVISOR
USER_PROFILE_ENABLED:USER_PROFILE_ENABLED
HERMES_DASHBOARD_ENABLED:HERMES_DASHBOARD_ENABLED
ENABLE_GKE_BACKUP_PLAN:ENABLE_GKE_BACKUP_PLAN
REGISTRY_PREFIX:REGISTRY_PREFIX
THIRD_PARTY_REGISTRY_PREFIX:THIRD_PARTY_REGISTRY_PREFIX
NAMESPACE:AGENT_NAMESPACE
"

# Always required: without these the script cannot name an install at all, so
# there is nothing for a plan or an apply to be about.
REQUIRED_ALWAYS="GCP_PROJECT_ID GCP_REGION GKE_CLUSTER_NAME"

# Required in --strict mode, which is the mode every job that touches a
# LONG-LIVED environment runs in.
#
# Each of these names something the composition provisions and an omitted value
# un-provisions: the gVisor node pool, the Hindsight API and its Postgres, the
# agent's custom IAM roles, the backup plan, the Pub/Sub topic behind Google
# Chat. On an environment that is torn down and rebuilt every run — `rc` and
# `nightly` — an omitted value costs a feature the tests may not exercise. On
# one that has been running for a month, the same omission is a `terraform
# apply` that plans a DESTROY, which is #1060's failure and the reason #1117
# could not simply be wired up. #1081 closed it for the flag path; this list is
# the same guarantee for the CI path, where the "previous value" the installer
# would inherit lives in a GitHub environment rather than on a disk.
#
# So: an unconfigured long-lived environment fails here, loudly, naming what to
# set — rather than converging on a default and taking the difference out of
# the running install.
REQUIRED_STRICT="
GOOGLE_CHAT_ENABLED
MODEL_PROVIDER
PLATFORM_AGENT_PERMISSION_SET
ENABLE_GVISOR
MEMORY_PROVIDER
USER_PROFILE_ENABLED
ENABLE_GKE_BACKUP_PLAN
"

missing=""
for var in $REQUIRED_ALWAYS; do
  [ -n "${!var:-}" ] || missing="${missing} ${var}"
done
if [ "$STRICT" = "true" ]; then
  for var in $REQUIRED_STRICT; do
    [ -n "${!var:-}" ] || missing="${missing} ${var}"
  done
fi

if [ -n "$missing" ]; then
  # One annotation naming every missing variable at once. Failing on the first
  # one costs a full run per variable, and a strict render of an environment
  # that has never been configured is missing every one of them.
  echo "::error title=Install configuration is incomplete::Set these on the GitHub environment this job binds to:${missing}. Each one is a setting the composition provisions; running without it would apply a default over the value this environment is already installed with, and terraform would plan to destroy the difference. docs/site/src/content/docs/deploy/environment-reconcile.md lists what each one should be."
  echo "==> Missing install configuration:${missing}" >&2
  exit 1
fi

# The token minter is configured by three settings at once, and the installer
# reads them as a unit: all three non-empty provisions it, and ANY of them empty
# renders `enable_github_minter = false` without a word. On a fresh install that
# is an install without a minter, which is the ordinary default. On an
# environment that already has one, it is an apply that destroys it.
#
# autopush is the live example and the reason this is here: #1117 found it
# carrying GH_APP_ID as a secret with neither GitOps variable set. A strict
# render of that configuration is exactly the silent un-provisioning above.
#
# All three empty stays allowed. provision_environment.sh makes the same check
# for the rebuild path, above its teardown, for the same reason.
if [ "$STRICT" = "true" ]; then
  minter_set=""
  minter_missing=""
  for var in GITOPS_ORG GITOPS_REPO GITHUB_APP_ID; do
    if [ -n "${!var:-}" ]; then
      minter_set="${minter_set} ${var}"
    else
      minter_missing="${minter_missing} ${var}"
    fi
  done
  if [ -n "$minter_set" ] && [ -n "$minter_missing" ]; then
    echo "::error title=GitHub token minter is half-configured::Set:${minter_set}; empty:${minter_missing}. The installer provisions the minter only when all three are set, so this configuration would render enable_github_minter = false and destroy a minter this environment already has. Set the missing ones, or clear the ones that are set to reconcile without a minter."
    echo "==> GitHub token minter half-configured — set:${minter_set}; empty:${minter_missing}." >&2
    exit 1
  fi
fi

# The chat allowlists are the one omission in this file that WIDENS access
# rather than removing a feature, so they cannot sit in REQUIRED_STRICT above:
# that list is unconditional, and an environment with the integration switched
# off has no allowlist to state.
#
# Empty is not "no opinion" here. `emit` drops an empty value, the installer
# renders `google_chat_allowed_users = []`, the chart's `with` omits the key,
# and the operator reads an absent list as GOOGLE_CHAT_ALLOW_ALL_USERS=true
# (platformagent_manifests.go's allowAllUsers). Nothing downstream reads the
# running CR's allowlist back, so an environment variable that is unset, typoed
# or cleared opens a long-lived install to the whole domain on the next
# unattended apply, with no record of what it used to hold.
#
# Allow-all stays reachable, but only by saying it: set the matching
# *_ALLOW_ALL_USERS variable to `true`. It is deliberately NOT mapped into
# install.env — the empty allowlist is already what produces it — so its only
# job is to make the intent explicit here.
#
# "Enabled" has to mean here exactly what it means to the installer, so the
# test is the installer's own `is_truthy` rather than a list of spellings
# written out again: `GOOGLE_CHAT_ENABLED=on` provisions the integration, and a
# guard that did not recognise it would wave through the configuration it
# exists to stop.
if [ "$STRICT" = "true" ]; then
  # shellcheck source=scripts/installer/installer_common.sh
  # shellcheck disable=SC1091
  . "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/installer/installer_common.sh"
  check_allowlist() {
    local enabled_var="$1" list_var="$2" allow_all_var="$3" platform="$4"
    is_truthy "${!enabled_var:-}" || return 0
    # Emptiness is the installer's own, not `-z`. hcl_csv_list splits on
    # `, \t\n` and drops empty items, so a value that is nothing but
    # separators — a list cleared down to a stray comma — is non-empty to
    # `-z` and renders `[]` to Terraform. Testing it any other way is a
    # second expression of the rule that disagrees with the first.
    [ "$(hcl_csv_list "${!list_var:-}")" = "[]" ] || return 0
    ! is_truthy "${!allow_all_var:-}" || return 0
    echo "::error title=${platform} is enabled with no allowlist::${list_var} names no users on this environment — it is unset, or it holds only separators — and an empty allowlist means EVERY user is admitted, because the operator turns an absent list into allow-all for ${platform}. Set ${list_var} to the users this install should admit, or set ${allow_all_var}=true to say the open allowlist is intended."
    echo "==> ${platform} enabled with an empty ${list_var} and no ${allow_all_var}=true." >&2
    return 1
  }
  allowlist_status=0
  check_allowlist GOOGLE_CHAT_ENABLED ALLOWED_USERS GOOGLE_CHAT_ALLOW_ALL_USERS "Google Chat" || allowlist_status=1
  check_allowlist SLACK_ENABLED SLACK_ALLOWED_USERS SLACK_ALLOW_ALL_USERS "Slack" || allowlist_status=1
  [ "$allowlist_status" -eq 0 ] || exit 1
fi

# ---------------------------------------------------------------------------
# Settings that need translating rather than copying
# ---------------------------------------------------------------------------
# MEMORY_PROVIDER is the CI-side name and carries CI-side values; install.env
# records MEMORY, whose vocabulary is file/hindsight/off. provision_environment.sh
# makes the same three-way translation for the destroy-and-rebuild path.
case "${MEMORY_PROVIDER:-}" in
  kube_agents_memory|hindsight) MEMORY="hindsight" ;;
  none|off)                     MEMORY="off" ;;
  "")                           MEMORY="" ;;
  *)                            MEMORY="file" ;;
esac
export MEMORY

# NAMESPACE has three spellings in play: the installer's own NAMESPACE, rc and
# nightly's AGENT_NAMESPACE, and staging's bare NAMESPACE. The mapping above
# reads AGENT_NAMESPACE; this fills in from the other before it, so an
# environment carrying either one is understood and neither has to be renamed
# in the GitHub UI while installs are running against it.
: "${AGENT_NAMESPACE:=${NAMESPACE:-}}"
export AGENT_NAMESPACE

# ---------------------------------------------------------------------------
# Write it
# ---------------------------------------------------------------------------
# umask first, so the file is never briefly world-readable: it carries the model
# provider's API key and the Slack tokens.
umask 077
: >"$OUT_PATH"

{
  echo "# Generated by scripts/release/render_install_env.sh — do not edit."
  echo "# Rendered from the GitHub environment's variables and secrets."
  echo "# Every value here comes from a GitHub environment setting; change it there."
  echo
} >>"$OUT_PATH"

emit() {
  local key="$1" value="$2"
  [ -n "$value" ] || return 0
  # %q, because these are read with `set -a; . install.env; set +a` and a value
  # with a space, a quote or a `$` in it — an allowed-users list, a Slack
  # channel name — would otherwise be re-interpreted as shell.
  printf '%s=%q\n' "$key" "$value" >>"$OUT_PATH"
}

for pair in $MAPPING; do
  key="${pair%%:*}"
  src="${pair##*:}"
  emit "$key" "${!src:-}"
done

# MEMORY is derived above rather than mapped, so it is emitted on its own.
emit MEMORY "${MEMORY}"

chmod 600 "$OUT_PATH"

# The listing is keys only. Every value is either a GitHub variable the reader
# can look up or a secret they must not be able to.
echo "==> Rendered install configuration to ${OUT_PATH}:"
sed -n 's/^\([A-Z_][A-Z0-9_]*\)=.*/    \1/p' "$OUT_PATH"
