#!/usr/bin/env bash
# ==============================================================================
# Shared definitions for the kube-agents installer front-ends.
# ==============================================================================
# Sourced by install.sh, uninstall.sh, and upgrade.sh — and this is where the
# terraform.tfvars generator lives, so the three front-ends describe the same
# install to the same engine (terraform/examples/full-install).
#
# Contract: the caller defines print_info / print_warning / print_error before
# calling anything here that reports. Functions read the install.env variable
# set from the environment (load it first); none of them prompt.
# ==============================================================================

# ─── Shared Installer Defaults ────────────────────────────────────────────────
# Every default an install gets for saying nothing lives in install.defaults.env
# at the repository root, beside install.env. One file, one job: this one has
# none of them inline, and a point of use reads its DEFAULT_* key rather than
# repeating the value. The exception is a fallback that deliberately differs
# from the fresh-install default because it describes an install that already
# exists -- `${ENABLE_GVISOR:-false}` here and in install.sh's control panel,
# each with the argument beside it. Precedence is
# install.defaults.env → an exported environment variable → install.env → a flag.
# install.env is sourced with `set -a`, which is what puts it above the export.
#
# Sourced WITHOUT `set -a`, unlike install.env. These stay shell variables: they
# are this project's defaults, not the install's configuration, and exporting
# them would put DEFAULT_* into the environment Terraform and the agent see.
#
# Resolved relative to this file rather than the working directory, because
# upgrade.sh and uninstall.sh source these helpers from a fresh clone whose
# path nobody knows in advance.
_installer_common_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || echo "")"
INSTALL_DEFAULTS_FILE="${KUBE_AGENTS_INSTALL_DEFAULTS:-${_installer_common_dir}/../../install.defaults.env}"
unset _installer_common_dir
if [ -r "$INSTALL_DEFAULTS_FILE" ]; then
  # shellcheck source=/dev/null
  . "$INSTALL_DEFAULTS_FILE"
else
  # Reachable only from a broken checkout. Every front door needs these to
  # decide anything at all, so guessing here would produce an install nobody
  # asked for; refuse and name the file.
  echo "  ✗ Cannot find the install defaults at ${INSTALL_DEFAULTS_FILE}." >&2
  echo "  ℹ It ships with the repository. Re-clone, or point KUBE_AGENTS_INSTALL_DEFAULTS at a copy." >&2
  return 1 2>/dev/null || exit 1
fi

# ─── Control-plane endpoint selection ─────────────────────────────────────────
# gke_dns_endpoint_flag, the one predicate deciding whether a cluster is reached
# over its IP or its DNS control-plane endpoint. Sourced here rather than left
# to the caller: common.sh and install.sh source the helper themselves, but
# uninstall.sh sources only this file, and calling an undefined function under
# `set -e` would end the run. Sourcing it twice is harmless — it defines a
# function and initialises its memo, both at load time, before anything probes.
#
# Resolved relative to this file, like the defaults above and for the same
# reason. Unlike upgrade.sh, which reads the helper out of a checkout because it
# also runs piped from curl, this file is only ever sourced from one.
#
# A tree without the helper gets a stub rather than a refusal, which is the
# answer the helper itself gives for a cluster it cannot describe: the empty
# flag is the command that ran before the helper existed, and it still reaches
# every cluster with a routable IP endpoint. The defaults above refuse because
# they decide what gets installed; this only picks an endpoint to dial, and an
# uninstall or upgrade is not worth stopping over it.
#
# uninstall.sh and upgrade.sh reach the stub: both source this file before
# anything else loads the helper. install.sh does not — common.sh sources the
# helper unguarded ahead of this file and would already have stopped on the
# same missing file. It says so rather than falling back in silence, because
# the next thing that caller sees is an unrelated-looking missing key.
_gke_dns_endpoint_helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/gke_dns_endpoint.sh"
if [ -r "$_gke_dns_endpoint_helper" ]; then
  # shellcheck source=scripts/installer/gke_dns_endpoint.sh
  . "$_gke_dns_endpoint_helper"
else
  echo "  ⚠ Cannot find ${_gke_dns_endpoint_helper}; reaching clusters over their IP endpoint." >&2
  gke_dns_endpoint_flag() { GKE_DNS_ENDPOINT_FLAG=""; }
fi
unset _gke_dns_endpoint_helper

# Request timeout for kubectl probes against live clusters in the installer.
readonly KUBECTL_PROBE_REQUEST_TIMEOUT="10s"

# ─── Scope cap ─────────────────────────────────────
# The bounds the PlatformAgent puts on spec.scope.maxProjects (SCOPE_MAX_PROJECTS),
# and its default, which the live-scope check reads an absent key as.
readonly SCOPE_MAX_PROJECTS_MIN=1
readonly SCOPE_MAX_PROJECTS_MAX=5000
readonly SCOPE_MAX_PROJECTS_DEFAULT=100

# ─── Scoped service account pool ──────────────────────────────────────────────
# SCOPED_SA_POOL_ENABLED arms the pool (the composition's scoped_pool_enabled,
# spec.security.scopedServiceAccountPool.enabled on the CR); off by default,
# because a pool member holds no IAM grant yet. SCOPED_SA_POOL_MAX_ACCOUNTS is
# the bound on the pool declared from the management project's free
# service-account quota (scoped_pool_max_accounts); the module's default of
# 100 is GCP's default quota, not the headroom, which the plan cannot read.
readonly SCOPED_SA_POOL_ENABLED_DEFAULT="false"
readonly SCOPED_SA_POOL_MAX_ACCOUNTS_MIN=1
readonly SCOPED_SA_POOL_MAX_ACCOUNTS_DEFAULT=100
# A pool member's account id starts with this prefix and its description
# carries the install's identity (the agent's service account id) as a marker.
# scoped_pool.tf writes the same two strings; check_service_account_ownership
# lists members by them, since the installer cannot enumerate members from its
# inputs (a selector's members are resolved inside the plan).
readonly SCOPED_SA_POOL_ACCOUNT_ID_PREFIX="ka-"
readonly SCOPED_SA_POOL_MEMBER_MARKER_FORMAT="Pool member of %s for "

# ─── Helm Release Management Defaults ─────────────────────────────────────────
# Operation timeout for an in-flight Helm install/upgrade across deploy workflows (10m).
readonly HELM_OPERATION_TIMEOUT_DEFAULT=600
# Polling interval while waiting for an in-flight Helm operation to finish.
readonly HELM_LOCK_POLL_INTERVAL_DEFAULT=10
# Timeout for rolling back a stuck pending release to the last healthy revision.
readonly HELM_ROLLBACK_TIMEOUT_DEFAULT="2m"

# ─── Chart contract ───────────────────────────────────────────────────────────
# Names the kube-agents chart fixes and the front doors address by name: the
# Helm release, the operator and agent Deployments, and the Secret the agent's
# credentials live in. The chart does not take them as values, so they are not
# install configuration and do not belong in install.defaults.env; they are
# named here so that no front door spells one differently from the others.
readonly KUBE_AGENTS_HELM_RELEASE="kube-agents"
# shellcheck disable=SC2034  # read by install.sh and upgrade.sh
readonly KUBE_AGENTS_OPERATOR_DEPLOYMENT="kube-agents-controller-manager"
readonly PLATFORM_AGENT_DEPLOYMENT="platform-agent-gateway"
# The Hermes container in that Deployment's pod. The pod runs three and sets no
# default-container annotation, so `kubectl exec` without -c lands on whichever
# is first.
# shellcheck disable=SC2034  # read by install.sh
readonly PLATFORM_AGENT_CONTAINER="platform-agent"
# The Hermes profile the Platform Agent answers on. A bare `hermes` reaches the
# `default` profile instead -- the Planning Agent front door.
# shellcheck disable=SC2034  # read by install.sh
readonly PLATFORM_AGENT_HERMES_PROFILE="platform"
readonly PLATFORM_AGENT_SECRET="platform-agent-secrets"
# The chart's LiteLLM Deployment, and the objects the operator composes from
# the PlatformAgent's name (platform-agent, which the composition leaves at
# the chart's default): the shell sandbox StatefulSet, the credential proxy
# Deployment, and the sandbox's authorized-keys Secret.
# shellcheck disable=SC2034  # read by install.sh and upgrade.sh
readonly LITELLM_DEPLOYMENT="litellm"
# shellcheck disable=SC2034
readonly PLATFORM_AGENT_SHELL_STATEFULSET="platform-agent-shell"
# shellcheck disable=SC2034
readonly PLATFORM_AGENT_CREDENTIAL_PROXY_DEPLOYMENT="platform-agent-credential-proxy"
# The design document a spec.mode switch points the operator at.
readonly PLATFORM_AGENT_MODE_SWITCH_DOC="docs/designs/spec-mode-switch.md"
# The Slack app setup page's allowlist section, which says how to find a
# member ID; refuse_next_slack_gateway_settings points at it.
readonly SLACK_ALLOWLIST_DOC="https://gke-labs.github.io/kube-agents/install/slack-app/#allowed-users"
# A Slack home channel the A2A gateway arms its notify route on: a public
# (C...) or private (G...) channel id, the prefix letter then two or more of
# A-Z0-9. The gateway's slackIsHomeChannelID and the operator's
# a2aSlackHomeChannel hold the same rule.
readonly SLACK_HOME_CHANNEL_ID_RE='^[CG][A-Z0-9]{2,}$'
# What platform_agent_mode_in_values and platform_agent_mode_on_cr print for
# a field that is not there: a record that carries no platformAgent.mode, or a
# CR with no spec.mode. Told apart from printing nothing, which means there is
# no CR.
readonly PLATFORM_AGENT_MODE_UNSET="unset"
# How each of read_platform_agent_install_state's reads came out.
readonly PLATFORM_AGENT_READ_OK="ok"
readonly PLATFORM_AGENT_READ_NONE="none"
readonly PLATFORM_AGENT_READ_FAILED="failed"
readonly PLATFORM_AGENT_READ_NO_CONTEXT="no-context"
# The front doors pass this to announce_platform_agent_mode_for_apply when
# no confirmation stands between the notice and the apply (upgrade.sh's full
# arm, the Day-2 menu's apply, install.sh -y), so the notice does not offer
# "run again" as a way out of a run that will not wait for one.
readonly PLATFORM_AGENT_MODE_NOTICE_UNGATED="ungated"
# The composition's two Helm releases, by their Terraform type and name: the
# front doors ask the state whether it manages one before deciding what a
# cert-manager or kube-agents release already on the cluster means.
readonly TF_HELM_RELEASE_TYPE="helm_release"
readonly TF_CERT_MANAGER_RELEASE_NAME="cert_manager"
readonly TF_KUBE_AGENTS_RELEASE_NAME="kube_agents"
# The composition's Google Chat Pub/Sub subscription in Terraform state (#1397).
readonly TF_PUBSUB_SUBSCRIPTION_TYPE="google_pubsub_subscription"
readonly TF_CHAT_EVENTS_SUBSCRIPTION_NAME="chat_events"
readonly TF_RESOURCE_MODE_MANAGED="managed"
# The Helm status of a release whose last operation failed, the one of a first
# install still in flight or interrupted, and the statuses `helm history`
# gives a revision that served at some point.
readonly HELM_STATUS_FAILED="failed"
readonly HELM_STATUS_PENDING_INSTALL="pending-install"
readonly HELM_SERVED_REVISION_STATUSES="deployed superseded"
# Timeout for uninstalling a failed release no revision of which ever served.
readonly HELM_FAILED_RELEASE_UNINSTALL_TIMEOUT="5m"
# shellcheck disable=SC2034
readonly PLATFORM_AGENT_SHELL_AUTHORIZED_KEYS_SECRET="platform-agent-shell-authorized-keys"

# The image tag the generator and the dev prompt fall back to when none was
# given. Not an install default: every front door rejects it through
# validate_immutable_ref, so only a direct caller of the generator or the
# interactive dev prompt ever reaches it.
readonly IMAGE_TAG_FALLBACK="latest"

# ─── Multi-project scope ──────────────────────────────────────────────────────
# One SCOPE_EXCLUDE_CLUSTERS entry names a cluster as project/location/cluster:
# the separator, and the shape an entry has to have to be rendered at all.
readonly SCOPE_CLUSTER_TRIPLE_SEPARATOR="/"
readonly SCOPE_CLUSTER_TRIPLE_PATTERN='^[^/]+/[^/]+/[^/]+$'
# A SCOPE_FOLDERS or SCOPE_ORGANIZATIONS entry is the bare numeric Resource
# Manager ID, the pattern the CRD accepts for the same fields.
readonly SCOPE_CONTAINER_ID_PATTERN='^[0-9]{1,20}$'
# What check_scope_container_access verifies before an apply that binds a
# container: the API the reconcile's container search calls, the permission
# the applying identity needs on each container kind, the permission and the
# role the same identity needs on every container while the pool lists its
# members at plan time, the constraints an organisation policy forbids an API
# through, and where the permission probe is asked.
readonly SCOPE_ASSET_API="cloudasset.googleapis.com"
readonly SCOPE_FOLDER_SET_IAM_PERMISSION="resourcemanager.folders.setIamPolicy"
readonly SCOPE_ORGANIZATION_SET_IAM_PERMISSION="resourcemanager.organizations.setIamPolicy"
readonly SCOPE_CONTAINER_ASSET_SEARCH_PERMISSION="cloudasset.assets.searchAllResources"
readonly SCOPE_CONTAINER_ASSET_SEARCH_ROLE="roles/cloudasset.viewer"
readonly SCOPE_SERVICE_USAGE_CONSTRAINTS="gcp.restrictServiceUsage serviceuser.services"
readonly RESOURCE_MANAGER_API_URL="https://cloudresourcemanager.googleapis.com/v3"
readonly SCOPE_PREFLIGHT_HTTP_TIMEOUT_SECONDS=20
# gcloud's own credential overrides, cleared for every gcloud call the
# preflight makes, the property read included, because the google provider
# does not read them.
readonly SCOPE_GCLOUD_AUTH_OVERRIDES="CLOUDSDK_AUTH_ACCESS_TOKEN CLOUDSDK_AUTH_ACCESS_TOKEN_FILE CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"
# The same overrides as gcloud configuration properties, which live in the
# active configuration file where env -u cannot reach them; a mint under
# either would answer for an identity Terraform never uses, so a set one
# makes the probe undecided rather than wrong. Read with the variables above
# cleared, so only the configuration file's value is reported and the remedy
# (unset the property) is one that works.
readonly SCOPE_GCLOUD_AUTH_PROPERTIES="auth/impersonate_service_account auth/access_token_file"
# The reasons in a 403 from Resource Manager that are not a permission answer:
# the API not enabled in the credential's quota project, or no quota project
# at all. The apply enables cloudresourcemanager.googleapis.com before its
# own binding call, so these read as undecided, not denied.
readonly SCOPE_PROBE_NOT_A_PERMISSION_ANSWER_PATTERN="SERVICE_DISABLED|has not been used in project|quota project|USER_PROJECT_DENIED"
# And the 403 a token minted without the cloud-platform scope gets: the role
# may well be held, so it is not a permission answer either, and the remedy
# is the token's scope, not a grant.
readonly SCOPE_PROBE_TOKEN_SCOPE_PATTERN="insufficient authentication scopes|ACCESS_TOKEN_SCOPE_INSUFFICIENT"
# The APIs the plan-time resolution of a selector reads
# (terraform/modules/kube-agents-scope-resolver), per selector kind, billed to
# the management project, which the resolver sends as the quota project: a
# Metrics Scope's reads use the Monitoring and Resource Manager APIs, a Shared
# VPC host's the Compute API, the same split the composition's API list makes
# (terraform/examples/full-install/main.tf). The reads run in the plan and the
# composition enables the APIs only in the apply that follows, so
# enable_scope_selector_apis enables the ones not yet on before an install.sh
# apply, and the dry-run skips its plan while one is off.
readonly SCOPE_METRICS_SCOPE_APIS="cloudresourcemanager.googleapis.com monitoring.googleapis.com"
readonly SCOPE_SHARED_VPC_HOST_APIS="compute.googleapis.com"
# The scoped service account pool, armed beside a folder or organisation,
# lists the container's members in the same plan through the Asset API
# (SCOPE_ASSET_API), which the composition otherwise enables for the
# reconcile in the apply; so enable_scope_selector_apis treats the pair as a
# third selector kind. The two clauses the enabling message names its reason
# with, one per kind of plan-time read.
readonly SCOPE_SELECTOR_READ_REASON="resolves the declared Shared VPC host or Metrics Scope"
readonly SCOPE_POOL_CONTAINER_READ_REASON="lists the declared folder's or organisation's members for the scoped service account pool"
# The three answers a container permission probe gives.
readonly SCOPE_PROBE_GRANTED=0
readonly SCOPE_PROBE_DENIED=1
readonly SCOPE_PROBE_UNDECIDED=2
# kubectl's answers for a cluster that serves no PlatformAgent type at all, and
# helm's for a release that does not exist: on a first adoption both mean there
# is nothing live to protect, not that the read failed.
readonly KUBECTL_NO_RESOURCE_TYPE_PATTERN="the server doesn't have a resource type|could not find the requested resource"
readonly HELM_RELEASE_NOT_FOUND_PATTERN="release: not found"
# The two verdicts refuse_apply_over_undeclared_scope's python half prints
# on its first line.
readonly SCOPE_VERDICT_OK="ok"
readonly SCOPE_VERDICT_REFUSE="refuse"
# Its two modes: refuse (the default, every apply) or warn (upgrade.sh --plan,
# which applies nothing).
readonly SCOPE_CHECK_MODE_REFUSE="refuse"
readonly SCOPE_CHECK_MODE_WARN="warn"

# Suffix appended when deriving Google Chat Pub/Sub subscription name from a custom topic (#1397).
readonly CHAT_SUBSCRIPTION_SUFFIX="-sub"

# ─── Terraform state in GCS ───────────────────────────────────────────────────
# The object the gcs backend writes under the prefix, and how gcloud spells
# "there is no such object" -- as opposed to "I could not look", which
# tf_state_read reports separately with this return code.
readonly TF_STATE_OBJECT="default.tfstate"
# gcloud's own phrasings, and not a bare "404": the message echoes the object
# URL, and a project id containing those digits would read every transient
# error as absence.
readonly GCS_OBJECT_ABSENT_PATTERN='matched no objects|NotFoundException|HTTPError 404|not found|does not exist'
readonly TF_STATE_RC_UNREADABLE=2

# The Hindsight memory store: the provider that deploys it, and the two objects
# whose presence live_hindsight_state takes as proof that it is there.
readonly HINDSIGHT_MEMORY_PROVIDER="kube_agents_memory"
readonly HINDSIGHT_STATEFULSET="hindsight-postgresql"
readonly HINDSIGHT_API_DEPLOYMENT="hindsight-api"

# Memory mode (the input spelling, recorded in install.env as MEMORY) → the
# provider name everything downstream reads. The inverse of install.sh's
# memory_mode_from_provider, and needed here because install.env records the
# mode while write_tfvars_from_state emits the provider: upgrade.sh and the
# Day-2 menu load the file and never pass through install.sh's parameter block,
# so without this they would generate memory_provider = "multiuser_memory" for
# a Hindsight install and the apply would delete it.
memory_provider_from_mode() {
  case "${1:-}" in
    hindsight) echo "$HINDSIGHT_MEMORY_PROVIDER" ;;
    off) echo "none" ;;
    file) echo "multiuser_memory" ;;
    *) echo "" ;;
  esac
}

# Translate install.env's MEMORY into MEMORY_PROVIDER for the front doors that
# load install.env and then generate tfvars directly (upgrade.sh, uninstall.sh,
# and install.sh's Day-2 menu).
#
# install.env and the tfvars generator spell the setting differently: the
# hand-authored input records MEMORY, while write_tfvars_from_state prefers
# MEMORY_PROVIDER (install.sh's own run exports the interview's answer there,
# and must keep winning). Without this step, an inherited MEMORY_PROVIDER in the
# shell environment (or both keys in install.env) would shadow MEMORY and
# regenerate the tfvars against the wrong store -- an apply then deleting the
# Hindsight API and its Postgres.
#
# Call after loading install.env and before anything reads the pair. Not called
# by install.sh's own run, which resolves the same precedence in its parameter
# block (PARAM_MEMORY) and exports MEMORY_PROVIDER from the interview later.
#
# Note: on these three front doors a recognised MEMORY in install.env beats
# `MEMORY_PROVIDER=… ./upgrade.sh`, and upgrade.sh has no --memory flag to
# override it with. MEMORY_PROVIDER is not a documented install.env or
# environment input (it appears nowhere in install.env.example, and
# docs/designs/memory.md names MEMORY as the recorded spelling). To force one
# for a single run, set MEMORY instead.
normalize_memory_vars() {
  local from_mode
  [ -n "${MEMORY:-}" ] || return 0
  from_mode="$(memory_provider_from_mode "${MEMORY}")"
  # An unrecognised MEMORY leaves whatever was already there rather than
  # blanking it: a typo in install.env must not silently retarget the store.
  [ -n "$from_mode" ] || return 0
  export MEMORY_PROVIDER="$from_mode"
}

# Model provider → the model the install defaults to for that provider. The
# names live in install.defaults.env; vertex_ai (and anything unrecognised,
# which the validator rejects separately) takes the Gemini one.
default_model_for_provider() {
  case "${1:-}" in
    openai) echo "$DEFAULT_MODEL_OPENAI" ;;
    anthropic) echo "$DEFAULT_MODEL_ANTHROPIC" ;;
    *) echo "$DEFAULT_MODEL_GEMINI" ;;
  esac
}

is_valid_model_provider() {
  [[ "${1:-}" =~ ^(gemini|vertex_ai|anthropic|openai)$ ]]
}

# MODEL_MAX_TOKENS: a whole number of output tokens, 0 meaning none. Digits
# only, so a sign, a decimal point or a unit suffix is refused before it
# reaches terraform.tfvars as a bare word HCL cannot parse.
is_non_negative_integer() {
  [[ "${1:-}" =~ ^[0-9]+$ ]]
}

# LITELLM_REDACTION_IP_ACTION: what the gateway does with IP literals, the
# chart's litellm.redaction.ip.action.
is_valid_redaction_ip_action() {
  [[ "${1:-}" =~ ^(mask|pseudonym|off)$ ]]
}

# PLATFORM_AGENT_MODE: the PlatformAgent CRD's spec.mode enum, which the
# chart's platformAgent.mode and the composition's platform_agent_mode take too.
is_valid_platform_agent_mode() {
  [[ "${1:-}" =~ ^(today|next)$ ]]
}

# The PlatformAgent and its release, read once for the scope check and the
# mode notice beside it. Each front door calls refuse_apply_over_undeclared_scope
# and then announce_platform_agent_mode_for_apply against the same namespace,
# with nothing applied in between, so the reads this fills are held for the
# notice (PLATFORM_AGENT_READS_HELD_FOR) and the notice takes and clears them
# instead of asking the cluster again. A run whose scope check did not run
# (an install.sh --dry-run, a first install) has the notice read for itself.
#
# Fills, for $1 namespace:
#   PLATFORM_AGENT_CR_READ      PLATFORM_AGENT_READ_OK, _NONE (no PlatformAgent
#                               type served: a cluster this chart never
#                               reached), _FAILED, or _NO_CONTEXT (the
#                               kubeconfig has no context for this cluster:
#                               nothing was asked)
#   PLATFORM_AGENT_CR_JSON      the `kubectl get platformagents -o json` list
#   PLATFORM_AGENT_RECORD_READ  _OK, _NONE (no release) or _FAILED; empty when
#                               the CR read did not answer, or $2 is "cr-only"
#   PLATFORM_AGENT_RECORD_JSON  the latest revision's values ({} with no release)
#   PLATFORM_AGENT_SERVED_JSON  the last served revision's values, only when
#                               the latest did not serve (a failed or pending
#                               upgrade); {} otherwise
#   PLATFORM_AGENT_SERVED_REVISION  that revision's number, else empty
#   PLATFORM_AGENT_SERVED_READ  _FAILED when the release history, or that
#                               revision's values, could not be read, else
#                               empty
#   PLATFORM_AGENT_READ_ERROR   the failed read's stderr, on one line
# A missing type and a missing release are answers, told apart from a read
# that failed by kubectl's and helm's exit status and their own error text,
# never by empty output. Caller has PROJECT_ID, REGION and CLUSTER_NAME set
# (gke_context_name).
read_platform_agent_install_state() {
  local namespace="${1:-${NAMESPACE:-$DEFAULT_NAMESPACE}}" scope="${2:-}" expected_ctx err_file served_rev history_json
  PLATFORM_AGENT_READS_HELD_FOR="$namespace"
  PLATFORM_AGENT_CR_READ="" PLATFORM_AGENT_CR_JSON="" PLATFORM_AGENT_RECORD_READ=""
  PLATFORM_AGENT_RECORD_JSON="{}" PLATFORM_AGENT_SERVED_JSON="{}" PLATFORM_AGENT_SERVED_READ=""
  PLATFORM_AGENT_SERVED_REVISION=""
  PLATFORM_AGENT_READ_ERROR=""
  expected_ctx="$(gke_context_name)"
  if ! kubectl config get-contexts "$expected_ctx" >/dev/null 2>&1; then
    PLATFORM_AGENT_CR_READ="$PLATFORM_AGENT_READ_NO_CONTEXT"
    return 0
  fi
  err_file="$(mktemp)"
  # `trap - ERR` for the bash 3.2 reason write_tfvars_from_state gives: the
  # misses classified below are ordinary answers, not aborts.
  if ! PLATFORM_AGENT_CR_JSON="$(trap - ERR; kubectl --context "$expected_ctx" --request-timeout="$KUBECTL_PROBE_REQUEST_TIMEOUT" \
    get platformagents.kubeagents.x-k8s.io -n "$namespace" -o json 2>"$err_file")"; then
    if grep -qiE "$KUBECTL_NO_RESOURCE_TYPE_PATTERN" "$err_file"; then
      PLATFORM_AGENT_CR_READ="$PLATFORM_AGENT_READ_NONE"
    else
      PLATFORM_AGENT_CR_READ="$PLATFORM_AGENT_READ_FAILED"
      PLATFORM_AGENT_READ_ERROR="$(tr '\n' ' ' <"$err_file" | sed 's/[[:space:]]*$//')"
    fi
    rm -f "$err_file"
    return 0
  fi
  PLATFORM_AGENT_CR_READ="$PLATFORM_AGENT_READ_OK"
  if [ "$scope" = "cr-only" ]; then
    rm -f "$err_file"
    return 0
  fi
  if ! PLATFORM_AGENT_RECORD_JSON="$(trap - ERR; helm get values "$KUBE_AGENTS_HELM_RELEASE" -n "$namespace" --kube-context "$expected_ctx" -o json 2>"$err_file")"; then
    if grep -qiE "$HELM_RELEASE_NOT_FOUND_PATTERN" "$err_file"; then
      PLATFORM_AGENT_RECORD_READ="$PLATFORM_AGENT_READ_NONE"
    else
      PLATFORM_AGENT_RECORD_READ="$PLATFORM_AGENT_READ_FAILED"
      PLATFORM_AGENT_READ_ERROR="$(tr '\n' ' ' <"$err_file" | sed 's/[[:space:]]*$//')"
    fi
    PLATFORM_AGENT_RECORD_JSON="{}"
    rm -f "$err_file"
    return 0
  fi
  PLATFORM_AGENT_RECORD_READ="$PLATFORM_AGENT_READ_OK"
  # The last revision that served, and only when the latest did not (a
  # failed or pending upgrade): its values are what the CR still holds, and
  # what Helm diffs the next render against. On a healthy release the latest
  # revision is the one record, so a hand edit that happens to restore an
  # earlier scope is still a hand edit. A history that cannot be read, or
  # parsed, is PLATFORM_AGENT_SERVED_READ failed: the scope check compares
  # against the latest revision alone, but the mode notice, which picks one
  # record, says it did not run rather than guess the latest served.
  if ! history_json="$(trap - ERR; helm history "$KUBE_AGENTS_HELM_RELEASE" -n "$namespace" --kube-context "$expected_ctx" -o json 2>"$err_file")" \
    || ! served_rev="$(trap - ERR; printf '%s' "$history_json" | python3 -c '
import json, sys
statuses = sys.argv[1].split()
revisions = json.load(sys.stdin) or []
latest = max(revisions, key=lambda r: r["revision"], default=None)
served = [] if latest is None or latest.get("status") in statuses else [
    r["revision"] for r in revisions if r.get("status") in statuses
]
print(max(served) if served else "")
' "$HELM_SERVED_REVISION_STATUSES" 2>>"$err_file")"; then
    PLATFORM_AGENT_SERVED_READ="$PLATFORM_AGENT_READ_FAILED"
    PLATFORM_AGENT_READ_ERROR="the release history did not answer: $(tr '\n' ' ' <"$err_file" | sed 's/[[:space:]]*$//')"
    rm -f "$err_file"
    return 0
  fi
  rm -f "$err_file"
  PLATFORM_AGENT_SERVED_REVISION="$served_rev"
  if [ -n "$served_rev" ]; then
    if ! PLATFORM_AGENT_SERVED_JSON="$(trap - ERR; helm get values "$KUBE_AGENTS_HELM_RELEASE" -n "$namespace" --kube-context "$expected_ctx" --revision "$served_rev" -o json 2>/dev/null)"; then
      PLATFORM_AGENT_SERVED_JSON="{}"
      PLATFORM_AGENT_SERVED_READ="$PLATFORM_AGENT_READ_FAILED"
      PLATFORM_AGENT_READ_ERROR="revision ${served_rev}, the last one that served, did not answer"
    fi
  fi
}

# The held reads for $1 namespace when the scope check just made them, or
# fresh ones; either way none are held afterwards, so a later call (the
# menu's next apply) reads again.
take_platform_agent_install_state() {
  local namespace="${1:-${NAMESPACE:-$DEFAULT_NAMESPACE}}"
  if [ -z "${PLATFORM_AGENT_READS_HELD_FOR:-}" ] || [ "$PLATFORM_AGENT_READS_HELD_FOR" != "$namespace" ]; then
    read_platform_agent_install_state "$namespace" "${2:-}"
  fi
  PLATFORM_AGENT_READS_HELD_FOR=""
}

# The spec.mode of the one PlatformAgent in $1 (a `kubectl get -o json`
# list), PLATFORM_AGENT_MODE_UNSET when it carries none, nothing when the
# list is empty. The CR, not the record, is the mode the install runs: a mode
# set by hand with kubectl is on the CR and in no record.
platform_agent_mode_on_cr() {
  printf '%s' "${1:-}" | (trap - ERR; python3 -c '
import json, sys
items = (json.load(sys.stdin) or {}).get("items") or []
if items:
    sys.stdout.write((items[0].get("spec") or {}).get("mode") or sys.argv[1])
' "$PLATFORM_AGENT_MODE_UNSET" 2>/dev/null) || true
}

# The platformAgent.mode of $1 (a release's values as JSON),
# PLATFORM_AGENT_MODE_UNSET when it records none.
platform_agent_mode_in_values() {
  printf '%s' "${1:-}" | (trap - ERR; python3 -c '
import json, sys
values = json.load(sys.stdin) or {}
sys.stdout.write((values.get("platformAgent") or {}).get("mode") or sys.argv[1])
' "$PLATFORM_AGENT_MODE_UNSET" 2>/dev/null) || true
}

# A mode as the operator reads it: an absent one is today.
effective_platform_agent_mode() {
  if [ "${1:-}" = "$PLATFORM_AGENT_MODE_UNSET" ]; then
    printf '%s' "$DEFAULT_PLATFORM_AGENT_MODE"
  else
    printf '%s' "${1:-}"
  fi
}

# The spec.mode the CR carries after a full apply of PLATFORM_AGENT_MODE: $1
# the record's (the served revision's platformAgent.mode), $2 the live CR's,
# $3 the key.
# The composition renders "next" as mode: next and "today" as no field, and
# Helm patches a custom resource from the difference between the served
# render and the new one. So a render that changes the mode sets it, one
# that drops a recorded mode removes it, and one the record already matches
# changes nothing, leaving whatever the CR carries, a mode set by hand
# included. The render is modelled from the key alone: a platformAgent.mode
# in the composition's extra_helm_values is merged over it and wins, and is
# not seen here (platform_agent_mode_extra_values_caveat says so).
platform_agent_mode_after_apply() {
  local record="${1:-}" live="${2:-}" key="${3:-}" rendered="$PLATFORM_AGENT_MODE_UNSET"
  [ "$key" = "next" ] && rendered="$key"
  if [ "$rendered" = "$record" ]; then
    printf '%s' "$live"
  else
    printf '%s' "$rendered"
  fi
}

# Said under every mode notice that names what an apply does: the
# composition merges extra_helm_values over the document it computes from
# PLATFORM_AGENT_MODE, so a platformAgent.mode set there decides, and the
# installer cannot read it (it lives in a *.auto.tfvars or
# TF_VAR_extra_helm_values, beside the tfvars the generator writes).
platform_agent_mode_extra_values_caveat() {
  print_info "This compares PLATFORM_AGENT_MODE in install.env with the cluster. A platformAgent.mode set through the composition's extra_helm_values wins over the key, and this check does not see it."
}

# Says what a full apply does to the install's spec.mode: $1 the record's
# mode, $2 the live CR's, $3 what PLATFORM_AGENT_MODE resolves to now, $4
# PLATFORM_AGENT_MODE_NOTICE_UNGATED when no confirmation follows. A
# switch is a rollout of a different component stack, not a setting, and the
# only trace of it in install.env is the one key someone edited, so the run
# that applies it names it first. A key the apply cannot bring the CR to,
# because the CR carries a mode set outside this installer, is named too.
# Silent when either value is empty (no CR, no release: a first install),
# and on a re-apply of the same mode. A read that failed is not empty: the
# front-door call says so itself (announce_platform_agent_mode_for_apply).
# Caller defines print_warning / print_info.
announce_platform_agent_mode_switch() {
  local record="${1:-}" live="${2:-}" key="${3:-}" route="${4:-}" from to
  [ -n "$record" ] && [ -n "$live" ] || return 0
  from="$(effective_platform_agent_mode "$live")"
  to="$(effective_platform_agent_mode "$(platform_agent_mode_after_apply "$record" "$live" "$key")")"
  if [ "$from" = "$to" ]; then
    if [ "$to" != "$key" ]; then
      print_warning "install.env sets PLATFORM_AGENT_MODE=${key}, but the PlatformAgent carries spec.mode ${to}, set outside this installer, and this apply leaves it there: the release already renders ${key}, so Helm has no change to send."
      print_info "To switch it, patch spec.mode on the CR, or apply once with PLATFORM_AGENT_MODE=${to} and then with ${key} (${PLATFORM_AGENT_MODE_SWITCH_DOC})."
      platform_agent_mode_extra_values_caveat
    fi
    return 0
  fi
  print_warning "This apply switches the install from spec.mode ${from} to ${to} (PLATFORM_AGENT_MODE in install.env): a mode switch, not a settings change."
  if [ "$to" = "next" ]; then
    print_info "The operator renders the NATS bus and the A2A gateway beside today's stack and rolls the agent onto them. Under next, Google Chat moves from the agent to the A2A gateway."
  else
    print_info "The operator retires the A2A stack and rolls the agent back onto today's path."
  fi
  if [ "$route" = "$PLATFORM_AGENT_MODE_NOTICE_UNGATED" ]; then
    print_info "What a switch does, either way: ${PLATFORM_AGENT_MODE_SWITCH_DOC}. This run applies it without asking first: to keep spec.mode ${from}, stop it now (Ctrl-C) and set PLATFORM_AGENT_MODE=${from} in install.env. ./upgrade.sh --plan previews an apply without making it."
  else
    print_info "What a switch does, either way: ${PLATFORM_AGENT_MODE_SWITCH_DOC}. To keep spec.mode ${from}, set PLATFORM_AGENT_MODE=${from} in install.env and run again."
  fi
  platform_agent_mode_extra_values_caveat
}

# The retag modes' half: upgrade.sh --upgrade-mode=harness or operator moves
# image tags on the release's recorded values and applies no tfvars, so the
# CR keeps the mode it has whatever PLATFORM_AGENT_MODE says. Said when a full
# upgrade would move it, rather than left for that upgrade to switch the
# install without a word from this run. $1 the record's mode, $2 the live
# CR's, $3 the key, $4 the upgrade mode. Silent when either is empty.
note_platform_agent_mode_not_applied() {
  local record="${1:-}" live="${2:-}" key="${3:-}" upgrade_mode="${4:-}" running full
  [ -n "$record" ] && [ -n "$live" ] || return 0
  running="$(effective_platform_agent_mode "$live")"
  full="$(effective_platform_agent_mode "$(platform_agent_mode_after_apply "$record" "$live" "$key")")"
  [ "$running" != "$full" ] || return 0
  print_warning "install.env sets PLATFORM_AGENT_MODE=${key}, but this --upgrade-mode=${upgrade_mode} run re-tags the release's recorded values and leaves it at spec.mode ${running}."
  print_info "A full upgrade applies the switch to ${full} (${PLATFORM_AGENT_MODE_SWITCH_DOC}). To stay on ${running}, set PLATFORM_AGENT_MODE=${running} in install.env."
  platform_agent_mode_extra_values_caveat
}

# Whether Slack is consumed by the A2A gateway after this apply: the
# operator's a2aSlackArmed (k8s-operator platformagent_a2a_manifests.go),
# which is spec.mode next, Slack enabled and Google Chat not armed. Chat holds
# the gateway when both are on, and Slack stays on the today path. $1 the
# mode the apply renders, $2 SLACK_ENABLED, $3 GOOGLE_CHAT_ENABLED.
slack_moves_to_a2a_gateway() {
  [ "${1:-}" = "next" ] && is_truthy "${2:-}" && ! is_truthy "${3:-}"
}

# Three Slack settings that the today path accepts and the A2A gateway does
# not. They are refused before the front door applies anything (#2812), and
# only when Slack moves to the gateway (slack_moves_to_a2a_gateway). Every
# failing setting is reported in one run. There is no email-to-ID lookup.
#
# - SLACK_ALLOWED_USERS is matched against member IDs (U.../W...) exactly.
#   The installer's prompt in 0.7 and 0.8 asked for "User IDs / Emails", and
#   an email matches nobody, so that person is refused after the switch. A
#   list of only emails is non-empty, so it does not fall back to allow-all.
#   The list is split the way hcl_csv_list renders it.
# - SLACK_HOME_CHANNEL, when set, must be a channel id
#   (SLACK_HOME_CHANNEL_ID_RE). With anything else, a DM (D...) or a user
#   (U...), the gateway leaves its notify route unarmed, so proactive posts
#   and board-card reports stop.
# - SLACK_BOT_TOKEN must be one token. The legacy broker takes a
#   comma-separated list for several workspaces; the gateway takes the
#   string whole, so it fails at connect and restarts without end. Only the
#   count is printed, never the value. An empty token is skipped: it is
#   recovered from the live Secret later, so the front doors call again
#   after recovery.
#
# $1 the mode the apply renders, $2 SLACK_ENABLED, $3 GOOGLE_CHAT_ENABLED,
# $4 the allowlist, $5 the home channel, $6 the bot token, $7 "flags" when
# install.sh's flags can set them too, so the remedy names each flag.
refuse_next_slack_gateway_settings() {
  local allowlist="${4:-}" home="${5:-}" token="${6:-}" flags="${7:-}"
  local item emails="" count=0 refused=false via
  slack_moves_to_a2a_gateway "${1:-}" "${2:-}" "${3:-}" || return 0
  while IFS= read -r item; do
    case "$item" in *@*) emails="${emails:+${emails}, }${item}" ;; esac
  done <<< "$(csv_list_items "$allowlist")"
  if [ -n "$emails" ]; then
    via="install.env"; [ "$flags" = "flags" ] && via="install.env or --slack-allowed-users"
    print_error "SLACK_ALLOWED_USERS holds entries that look like emails: ${emails}. Under spec.mode next the A2A gateway matches this allowlist against Slack member IDs exactly (such as U0123ABCD), so an email matches nobody and the person it names would be locked out after this apply."
    print_info "Find each person's member ID in Slack: open their profile, choose ⋮ (More), then Copy member ID. Replace the emails with those IDs in ${via} and run again. Slack app setup for next: ${SLACK_ALLOWLIST_DOC}"
    refused=true
  fi
  home="${home#"${home%%[![:space:]]*}"}"
  home="${home%"${home##*[![:space:]]}"}"
  if [ -n "$home" ] && ! [[ "$home" =~ $SLACK_HOME_CHANNEL_ID_RE ]]; then
    via="install.env"; [ "$flags" = "flags" ] && via="install.env or --slack-home-channel"
    print_error "SLACK_HOME_CHANNEL is '${home}', which is not a Slack channel id (C... or G...). Under spec.mode next the A2A gateway does not arm its notify route with it, so the agent's proactive posts and every board card's report back stop."
    print_info "Use the channel's id: in Slack, open the channel's details; the id is at the bottom of the About tab. Set it in ${via} and run again."
    refused=true
  fi
  while IFS= read -r item; do
    [ -n "$item" ] && count=$((count + 1))
  done <<< "$(csv_list_items "$token")"
  if [ "$count" -gt 1 ]; then
    via="install.env"; [ "$flags" = "flags" ] && via="install.env or --slack-bot-token"
    print_error "SLACK_BOT_TOKEN holds ${count} tokens, a list for several workspaces. Under spec.mode next the A2A gateway's Slack backend takes one workspace's bot token, and with a list it fails to connect and restarts without end."
    print_info "Set SLACK_BOT_TOKEN to one workspace's bot token (xoxb-...) in ${via} and run again, or keep this install on PLATFORM_AGENT_MODE=today."
    refused=true
  fi
  if $refused; then return 1; fi
  return 0
}

# refuse_next_slack_gateway_settings on the exported keys: what install.env
# loaded, or what install.sh exported, plus a token the tfvars generator
# recovered from the live Secret. $1 as the other's $7.
refuse_next_slack_gateway_settings_from_env() {
  refuse_next_slack_gateway_settings "${PLATFORM_AGENT_MODE:-$DEFAULT_PLATFORM_AGENT_MODE}" \
    "${SLACK_ENABLED:-$DEFAULT_SLACK_ENABLED}" "${GOOGLE_CHAT_ENABLED:-$DEFAULT_GOOGLE_CHAT_ENABLED}" \
    "${SLACK_ALLOWED_USERS:-}" "${SLACK_HOME_CHANNEL:-}" "${SLACK_BOT_TOKEN:-}" "${1:-}"
}

# Said when a read the notice needs failed, so the run does not go on as if
# the notice had been weighed and found nothing to say. $1 what could not be
# read, $2 the key, $3 namespace.
_platform_agent_mode_unread() {
  print_warning "The mode-switch check did not run: ${1}. This apply renders spec.mode from PLATFORM_AGENT_MODE=${2} in install.env whatever the PlatformAgent carries now, so if that differs it is a mode switch (${PLATFORM_AGENT_MODE_SWITCH_DOC})."
  print_info "Check before confirming: kubectl get platformagents -n ${3} --context $(gke_context_name) -o jsonpath='{.items[*].spec.mode}' (empty is today)."
}

# The front door's call, after the scope check beside it: $1 namespace, $2
# the key, $3 PLATFORM_AGENT_MODE_NOTICE_UNGATED on a route with no
# confirmation after the notice. Quiet where there is nothing to switch (a cluster the tfvars
# generator found NOT_FOUND, so TFVARS_CLUSTER_EXISTS=false, which is every
# first install: nothing is read, since a kubeconfig context left by an
# earlier install of the same name points at an endpoint that is gone; no
# PlatformAgent type, no CR, no release), loud where a read failed. Past that
# guard the cluster exists, so a kubeconfig with no context for it (kubectl
# absent, or a get-credentials that did not land on the --dry-run and
# --generate-only routes the scope check skips) is a read that did not
# happen, and is said with the scope check's remedy.
announce_platform_agent_mode_for_apply() {
  local namespace="${1:-${NAMESPACE:-$DEFAULT_NAMESPACE}}" key="${2:-}" live record
  if [ "${TFVARS_CLUSTER_EXISTS:-}" = "false" ]; then
    PLATFORM_AGENT_READS_HELD_FOR=""
    return 0
  fi
  take_platform_agent_install_state "$namespace"
  case "$PLATFORM_AGENT_CR_READ" in
    "$PLATFORM_AGENT_READ_FAILED")
      _platform_agent_mode_unread "the PlatformAgent in namespace '${namespace}' could not be read (${PLATFORM_AGENT_READ_ERROR})" "$key" "$namespace"
      return 0
      ;;
    "$PLATFORM_AGENT_READ_NO_CONTEXT")
      _platform_agent_mode_unread "the kubeconfig has no context '$(gke_context_name)' to read the PlatformAgent through (run: gcloud container clusters get-credentials ${CLUSTER_NAME} --location ${REGION} --project ${PROJECT_ID})" "$key" "$namespace"
      return 0
      ;;
    "$PLATFORM_AGENT_READ_OK") ;;
    *) return 0 ;;
  esac
  live="$(platform_agent_mode_on_cr "$PLATFORM_AGENT_CR_JSON")"
  [ -n "$live" ] || return 0
  if [ "$PLATFORM_AGENT_RECORD_READ" = "$PLATFORM_AGENT_READ_FAILED" ] || [ "$PLATFORM_AGENT_SERVED_READ" = "$PLATFORM_AGENT_READ_FAILED" ]; then
    _platform_agent_mode_unread "the values of release '${KUBE_AGENTS_HELM_RELEASE}' in namespace '${namespace}' could not be read (${PLATFORM_AGENT_READ_ERROR})" "$key" "$namespace"
    return 0
  fi
  [ "$PLATFORM_AGENT_RECORD_READ" = "$PLATFORM_AGENT_READ_OK" ] || return 0
  if [ -n "$PLATFORM_AGENT_SERVED_REVISION" ]; then
    record="$(platform_agent_mode_in_values "$PLATFORM_AGENT_SERVED_JSON")"
  else
    record="$(platform_agent_mode_in_values "$PLATFORM_AGENT_RECORD_JSON")"
  fi
  announce_platform_agent_mode_switch "$record" "$live" "$key" "${3:-}"
}

# upgrade.sh's retag arms, after retag_values: $1 namespace, $2 the key, $3
# the upgrade mode, $4 RETAG_VALUES_JSON, the values the retag re-applies and
# so the record a later full upgrade diffs against. Only the CR is read here.
note_platform_agent_mode_for_retag() {
  local namespace="${1:-${NAMESPACE:-$DEFAULT_NAMESPACE}}" live
  read_platform_agent_install_state "$namespace" cr-only
  PLATFORM_AGENT_READS_HELD_FOR=""
  if [ "$PLATFORM_AGENT_CR_READ" = "$PLATFORM_AGENT_READ_FAILED" ]; then
    print_info "Whether a full upgrade would switch spec.mode was not checked: the PlatformAgent in namespace '${namespace}' could not be read (${PLATFORM_AGENT_READ_ERROR}). This run leaves the mode where it is either way."
    return 0
  fi
  [ "$PLATFORM_AGENT_CR_READ" = "$PLATFORM_AGENT_READ_OK" ] || return 0
  live="$(platform_agent_mode_on_cr "$PLATFORM_AGENT_CR_JSON")"
  note_platform_agent_mode_not_applied "$(platform_agent_mode_in_values "${4:-}")" "$live" "${2:-}" "${3:-}"
}

# The GCP IAM role bundles the install knows how to grant. Kubernetes RBAC is
# read-only in every one of them; see the site's reference/security-and-iam.
is_valid_permission_set() {
  [[ "${1:-}" =~ ^(read-only|custom)$ ]]
}

# Accept a permission set, or report why not and return non-zero. Every front
# door routes its check through here so the three of them cannot drift into
# accepting different vocabularies -- and so the one value that needs an
# explanation gets the same one everywhere.
#
# `gke-admin` was removed rather than deprecated because it did not merely widen
# the ceiling, it removed one. GKE authorizes an action if EITHER IAM or
# Kubernetes RBAC allows it, so a GSA holding roles/container.admin is authorized
# by IAM no matter how narrow the KSA's RBAC is -- and roles/container.admin is
# the one predefined GKE role carrying container.clusters.impersonate. GKE grants
# IAM roles at the project level (a cluster is not a resource an IAM policy can
# attach to), so that impersonation covers every cluster in the project and the
# grant cannot be narrowed to one. An operator who genuinely needs broad roles
# uses `custom` and lists them, which makes the grant explicit and reviewable
# instead of hiding it behind one word.
#
# The removed value is named separately from the generic error so that a cached
# vars.sh, a GitHub environment variable, or a --permission-set flag written
# before the removal fails with an explanation rather than a bare "invalid".
# Follows this file's contract: the caller defines print_error.
require_supported_permission_set() {
  # Normalised here rather than left to the caller. common.sh trims and
  # lowercases before calling; install.sh passes --permission-set through raw,
  # so without this `--permission-set=GKE-ADMIN` fell past the named arm and
  # got the generic "invalid" instead of the explanation this function exists
  # to give. The uppercase spelling is not hypothetical: it is what a GitHub
  # environment variable or a hand-edited vars.sh tends to carry.
  local value
  value=$(printf '%s' "${1:-}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
  if [ "$value" = "gke-admin" ]; then
    print_error "The 'gke-admin' permission set has been removed: roles/container.admin authorizes the agent through IAM regardless of its Kubernetes RBAC, and the container.clusters.impersonate it carries applies to every cluster in the project. Use 'read-only', or 'custom' with an explicit role list if you accept that risk."
    return 1
  fi
  if ! is_valid_permission_set "$value"; then
    print_error "Invalid Platform Agent Permission Set '$value'. Must be one of: read-only, custom."
    return 1
  fi
  return 0
}

# Roles that hand the agent the authority the removed `gke-admin` bundle did.
# Kept in step with FORBIDDEN_ROLES in tests/test_agent_iam_ceiling.py, which is
# what asserts no built-in set grants any of them.
OVERREACHING_AGENT_ROLES="roles/container.admin roles/container.clusterAdmin roles/container.developer roles/container.hostServiceAgentUser roles/monitoring.admin roles/logging.admin roles/owner roles/editor roles/iam.serviceAccountTokenCreator"

# Warn when a `custom` role list reaches the ceiling `gke-admin` was removed for.
#
# `custom` is the supported way to widen, and the argument for it is that naming
# each role puts the grant somewhere a reviewer sees it. That argument does not
# hold on the installer path: --custom-roles goes into a machine-generated
# terraform.tfvars nobody opens, so `--permission-set=custom
# --custom-roles="roles/container.admin"` reaches IAM-identical authority to the
# bundle that was removed, silently. This does not refuse it -- an operator who
# means it is entitled to it -- it just declines to let it happen quietly.
#
# Returns 0 always: this is a warning, not a gate. Caller defines print_warning.
warn_on_overreaching_custom_roles() {
  local roles="${1:-}"
  local found=""
  local role listed
  for role in $OVERREACHING_AGENT_ROLES; do
    for listed in ${roles//,/ }; do
      if [ "$listed" = "$role" ]; then
        found="${found}${found:+, }${role}"
      fi
    done
  done
  if [ -n "$found" ]; then
    print_warning "The custom role list grants ${found}. GKE authorizes on either IAM or Kubernetes RBAC, so a role like roles/container.admin authorizes the agent through IAM regardless of how narrow its Kubernetes RBAC is -- this is the authority the removed 'gke-admin' set granted, reached the long way round. Continuing; grant it only if you mean to."
  fi
  return 0
}

# The cluster shapes the gke-cluster module can build. Matches the module's own
# variable validation, so a bad value fails at the interview rather than at
# terraform validate with the cluster interview already paid for.
# True when a location names a region (us-central1) rather than a zone
# (us-central1-a). Autopilot clusters are regional, so this is what decides
# whether the default shape is creatable at a given location. One home for the
# pattern: install.sh both demotes the default and validates an explicit
# --gke-cluster-mode against it, and the two must agree.
location_is_region() {
  [[ "${1:-}" =~ ^[a-z]+-[a-z]+[0-9]+$ ]]
}

is_valid_cluster_mode() {
  [[ "${1:-}" =~ ^(autopilot|standard)$ ]]
}

# ─── Boolean Parsing ──────────────────────────────────────────────────────────
# Interpret a value as a boolean toggle. Returns 0 (success) for common
# affirmative spellings and 1 otherwise. Matching is case-insensitive and
# surrounding whitespace is ignored, so all of the following are truthy:
#   true, yes, y, 1, on  (in any letter case, e.g. "True", "YES", "On")
# Everything else — including false, no, n, 0, off, and empty/unset — is falsy.
is_truthy() {
  local val="${1:-}"
  val="${val//[[:space:]]/}"
  case "$val" in
    [Tt][Rr][Uu][Ee] | [Yy][Ee][Ss] | [Yy] | 1 | [Oo][Nn]) return 0 ;;
    *) return 1 ;;
  esac
}

# A spelling is_truthy reads as true, or one that plainly means false. For a
# security toggle whose off state must not be reachable by a typo.
is_bool_spelling() {
  is_truthy "${1:-}" && return 0
  local val="${1:-}"
  val="${val//[[:space:]]/}"
  case "$val" in
    [Ff][Aa][Ll][Ss][Ee] | [Nn][Oo] | [Nn] | 0 | [Oo][Ff][Ff]) return 0 ;;
    *) return 1 ;;
  esac
}

# Checks if GKE databaseEncryption.state is a valid CMEK-encrypted state.
#   - ENCRYPTED: Standard CMEK database encryption state in GKE
#   - ALL_OBJECTS_ENCRYPTION_ENABLED: GKE 1.35+ Application-layer Secrets Encryption
is_valid_cmek_encryption_state() {
  local state="${1:-}"
  local valid_states=(
    "ENCRYPTED"
    "ALL_OBJECTS_ENCRYPTION_ENABLED"
  )

  for valid in "${valid_states[@]}"; do
    if [ "$state" = "$valid" ]; then
      return 0
    fi
  done
  return 1
}

# ─── GKE Version Comparison ───────────────────────────────────────────────────
# The first GKE version whose Autopilot clusters ship the gvisor RuntimeClass:
# https://cloud.google.com/kubernetes-engine/docs/how-to/sandbox-pods
GVISOR_AUTOPILOT_MIN_VERSION="1.27.4-gke.800"

# True when GKE version $1 is at or above $2. `sort -V` reads both the dotted
# fields and the -gke.N suffix numerically, so it puts gke.800 below gke.1500
# where a lexical compare does the opposite. Callers check the version's shape
# themselves: an unparseable string here is "unknown", not "too old", and the
# two deserve different answers.
gke_version_at_least() {
  local have="${1:-}" want="${2:-}"
  [ "$have" = "$want" ] ||
    [ "$(printf '%s\n%s\n' "$have" "$want" | sort -V | head -n1)" = "$want" ]
}

retry() {
  local max_retries=$1
  local delay=$2
  shift 2
  local count=0

  while [ $count -lt $max_retries ]; do
    count=$((count + 1))
    if "$@"; then
      return 0
    fi
    if [ $count -lt $max_retries ]; then
      echo -e "  ⚠ [Retry $count/$max_retries] Waiting ${delay}s before next attempt..." >&2
      sleep "$delay"
    fi
  done

  return 1
}

# ─── install.env: the hand-authored install configuration ─────────────────────
# The input every front door reads. install.sh carries its own copy of this
# loader because it must run before its parameter block and has to work as a
# standalone curl | bash download with no checkout to source from; the two are
# kept in step by tests/test_install_script.py. upgrade.sh and uninstall.sh use
# these.
#
# Where the file is, given a repository directory. An explicit
# KUBE_AGENTS_INSTALL_ENV wins, which is how CI renders one from its own
# variables rather than keeping install state on an ephemeral runner.
default_install_env_file() {
  local repo_dir="${1:-.}"
  if [ -n "${KUBE_AGENTS_INSTALL_ENV:-}" ]; then
    echo "${KUBE_AGENTS_INSTALL_ENV}"
    return 0
  fi
  echo "${repo_dir}/install.env"
}

# Load it into the environment. `set -a` rather than a K=V parser because these
# values have to reach write_tfvars_from_state and the TF_VAR_* handoff at the
# end of it, both of which read the environment: a conventional dotenv without
# `export` would parse and then not travel.
#
# Returns 1 when there is no file, so a caller can tell "nothing to load" from
# "loaded". A file that exists but does not parse is fatal — continuing would
# provision from defaults, which is the failure this whole input model exists
# to remove.
load_install_env() {
  local file="${1:-}"
  # NAMESPACE reaches terraform.tfvars, and it is a name kubectl tooling
  # commonly exports. Only install.env may set it: a value inherited from the
  # shell would put a fresh release into a namespace the agent's fixed gateway
  # endpoint does not serve, and record nothing that says why. Cleared before
  # the file is read (and whether or not there is one), so the file's own key
  # is the only way in.
  unset NAMESPACE
  # The scope keys likewise: they render into the PlatformAgent, and only
  # install.sh's first install records them, so on upgrade.sh, uninstall.sh
  # and the Day-2 menu the file is the only way in. A value inherited from the
  # shell would declare a project the file does not record, and the next run
  # from a clean shell would drop it again and retire its profiles.
  unset SCOPE_PROJECTS SCOPE_FOLDERS SCOPE_ORGANIZATIONS SCOPE_SHARED_VPC_HOSTS SCOPE_METRICS_SCOPES SCOPE_MAX_PROJECTS SCOPE_EXCLUDE_PROJECTS SCOPE_EXCLUDE_CLUSTERS
  # The scoped service account pool's switch and cap for the same reason: an
  # inherited SCOPED_SA_POOL_ENABLED=true would arm the pool for one run, on
  # accounts the next run from a clean shell deletes again.
  unset SCOPED_SA_POOL_ENABLED SCOPED_SA_POOL_MAX_ACCOUNTS
  # The PlatformAgent's mode too: an inherited PLATFORM_AGENT_MODE=next would
  # switch the install's component stack for one run, and the next run from a
  # clean shell would switch it back and retire the A2A stack.
  unset PLATFORM_AGENT_MODE
  # The GitOps forge keys too: an inherited GITOPS_FORGE=gitlab would render a
  # GitLab forge, and drop the GitHub alias and minter, for one run of an
  # install the file records as GitHub. --gitops-forge is the per-run way in.
  unset GITOPS_FORGE GITOPS_HOST GITLAB_TOKEN_SECRET
  [ -n "$file" ] && [ -f "$file" ] || return 1
  # Checked before sourcing: a stray quote would otherwise abort the caller
  # through its ERR trap with a bash parse error naming no file.
  if ! bash -n "$file" 2>/dev/null; then
    print_error "Install configuration '$file' is not valid shell and could not be loaded."
    print_info "Each line is NAME=value; quote any value containing spaces."
    exit 1
  fi
  set -a
  # shellcheck disable=SC1090
  . "$file"
  set +a
  return 0
}

# ─── install.env Persistence ──────────────────────────────────────────────────
# Rewrite ONE key in install.env, leaving every other line -- including the
# operator's comments and ordering -- exactly as it was. INSTALL_ENV_FILE must
# be set by the caller.
#
# The Day-2 control panel is the only caller, and it is the one place that may
# write here: "Save & Apply Configuration Changes" is an explicit instruction
# to record a change, not the installer quietly overwriting an input. install.sh
# itself creates the file when there is none, and otherwise only appends a chat
# key the file does not assign (record_flags_into_install_env); it never
# rewrites a line.
#
# No `export` keyword in the output: install.env is a dotenv, loaded with
# `set -a`. The value is still %q-quoted, so spaces and quotes survive.
save_env_var() {
  local var_name=$1
  local var_val=$2
  export "${var_name}=${var_val}"

  local old_umask
  old_umask=$(umask)
  umask 077

  if [ -f "$INSTALL_ENV_FILE" ]; then
    chmod 600 "$INSTALL_ENV_FILE" 2>/dev/null || true
    # Drops the previous assignment whichever spelling it used, so a file
    # migrated from vars.sh does not end up carrying both.
    grep -E -v "^[[:space:]]*(export[[:space:]]+)?${var_name}=" "$INSTALL_ENV_FILE" \
      > "$INSTALL_ENV_FILE.tmp" 2>/dev/null || true
    chmod 600 "$INSTALL_ENV_FILE.tmp" 2>/dev/null || true
    mv "$INSTALL_ENV_FILE.tmp" "$INSTALL_ENV_FILE"
  fi
  printf "%s=%q\n" "$var_name" "$var_val" >> "$INSTALL_ENV_FILE"
  chmod 600 "$INSTALL_ENV_FILE" 2>/dev/null || true

  umask "$old_umask"
}

# The same, for a credential. PERSIST_SECRETS_ON_DISK=false keeps it out of the
# file and removes any copy already there, while still exporting it for this
# run -- the live Secret is its home, and write_tfvars_from_state recovers it.
save_secret_env_var() {
  local var_name=$1
  local var_val=$2
  export "${var_name}=${var_val}"
  if is_truthy "${PERSIST_SECRETS_ON_DISK:-$DEFAULT_PERSIST_SECRETS_ON_DISK}"; then
    save_env_var "$var_name" "$var_val"
  elif [ -f "$INSTALL_ENV_FILE" ]; then
    local old_umask
    old_umask=$(umask)
    umask 077
    chmod 600 "$INSTALL_ENV_FILE" 2>/dev/null || true
    grep -E -v "^[[:space:]]*(export[[:space:]]+)?${var_name}=" "$INSTALL_ENV_FILE" \
      > "$INSTALL_ENV_FILE.tmp" 2>/dev/null || true
    chmod 600 "$INSTALL_ENV_FILE.tmp" 2>/dev/null || true
    mv "$INSTALL_ENV_FILE.tmp" "$INSTALL_ENV_FILE"
    chmod 600 "$INSTALL_ENV_FILE" 2>/dev/null || true
    umask "$old_umask"
  fi
}

# ─── Operator-supplied paths ──────────────────────────────────────────────────
# Expands a leading ~ in an operator-supplied path.
#
# `${path/#\~/$HOME}` looks equivalent and is not: the replacement expands
# $HOME whether or not the pattern matched, so under `set -Eeuo pipefail` an
# ordinary absolute path aborted the caller with `HOME: unbound variable`
# whenever HOME happened to be unset. install.sh's kube_agents_clone_dir is a
# function rather than a constant for the same reason, and names the same
# environments: a systemd system unit, or a container with no passwd entry.
#
# So HOME is read only when there is a ~ to resolve, and when it is genuinely
# needed and missing the failure names the path that could not be expanded
# rather than the variable the operator never set.
#
# Per this file's contract the caller owns print_error, and because callers
# read this function's stdout the message is redirected to stderr; the failure
# travels by return code. Note that a ~ usually never reaches here at all --
# an unquoted one on the command line is expanded by the operator's own shell
# before the installer sees it. What does reach here is a quoted flag value or
# a path read out of install.env.
expand_tilde_path() {
  local path="$1"
  case "$path" in
    # The slash sits outside the quotes so shellcheck's SC2088 stays enforced in
    # this block rather than being suppressed across it. The tilde stays quoted
    # because the pattern has to match one the shell did not expand.
    "~" | "~"/*)
      if [ -z "${HOME:-}" ]; then
        print_error "Cannot expand '~' in '${path}': HOME is unset. Pass an absolute path instead." >&2
        return 1
      fi
      printf '%s' "${HOME}${path#\~}"
      ;;
    *)
      printf '%s' "$path"
      ;;
  esac
}

# ─── GitOps repository input names ────────────────────────────────────────────
# The installer's GitOps coordinates are GITOPS_ORG / GITOPS_REPO. They used to
# be GITHUB_ORG / GITHUB_REPO, which collided with two other things that mean
# something else:
#
#   - GH_ORG / GH_REPO on the rc and nightly environments name the RELEASE
#     repository (gke-labs/kube-agents), read by scripts/release/common.sh.
#     GITOPS_ORG / GITOPS_REPO there name the GitOps repository. A workflow
#     wiring the installer therefore had to write
#     `GITHUB_ORG: ${{ vars.GITOPS_ORG }}`, which reads like a mistake and
#     invites someone to "fix" it to vars.GH_ORG -- scoping a live GitHub App
#     token at the release repository.
#   - tests/e2e declares GITHUB_ORG / GITHUB_REPO for the repository a TEST
#     acts on. Those keep their names; only the installer's inputs move.
#
# Call this once, after the install configuration is loaded and before anything
# reads the coordinates. One home for the fallback, so no reader needs to know
# both spellings.
normalize_gitops_repo_vars() {
  if [ -z "${GITOPS_ORG:-}" ] && [ -n "${GITHUB_ORG:-}" ]; then
    export GITOPS_ORG="${GITHUB_ORG}"
    print_warning "GITHUB_ORG is deprecated as an installer input; rename it to GITOPS_ORG (it still works this release)."
  fi
  if [ -z "${GITOPS_REPO:-}" ] && [ -n "${GITHUB_REPO:-}" ]; then
    export GITOPS_REPO="${GITHUB_REPO}"
    print_warning "GITHUB_REPO is deprecated as an installer input; rename it to GITOPS_REPO (it still works this release)."
  fi
  # Exported back under the old names too. The agent runtime, the chart values
  # and k8s-operator/config/integrations/github all still speak GITHUB_*, and
  # this is one release of overlap rather than a second source of truth: the
  # value always comes from GITOPS_*.
  export GITHUB_ORG="${GITOPS_ORG:-}"
  export GITHUB_REPO="${GITOPS_REPO:-}"
}

# ─── Identity input names ─────────────────────────────────────────────────────
# PLATFORM_AGENT_GSA_NAME is the install.env key for the agent's service
# account id. For the one release before it existed, the documented way to
# name a second install's account was TF_VAR_agent_service_account_id in
# install.env -- a Terraform variable rather than an installer input, which
# the generated terraform.tfvars now outranks. Honoured for one release, with
# a warning, the way GITHUB_ORG was; the generator calls this first so every
# front door agrees.
normalize_identity_vars() {
  if [ -z "${PLATFORM_AGENT_GSA_NAME:-}" ] && [ -n "${TF_VAR_agent_service_account_id:-}" ]; then
    export PLATFORM_AGENT_GSA_NAME="${TF_VAR_agent_service_account_id}"
    print_warning "TF_VAR_agent_service_account_id is deprecated as an installer input; rename it to PLATFORM_AGENT_GSA_NAME in install.env (it still works this release)."
  fi
}

# ─── Dev Scratch State Persistence (scripts/installer/vars.sh) ────────────────
# No installer front door reads or writes vars.sh any more (install.env is the
# sole install configuration). These helpers stay only for the dev scripts under
# scripts/dev/, which record throwaway scratch state (such as
# DEV_ARTIFACT_REGISTRY_CREATED) into scripts/installer/vars.sh through
# common.sh's init_var helpers. VARS_FILE must be set by the caller.
save_var() {
  local var_name=$1
  local var_val=$2
  export "${var_name}=${var_val}"
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    return 0
  fi

  local old_umask
  old_umask=$(umask)
  umask 077

  if [ -f "$VARS_FILE" ]; then
    chmod 600 "$VARS_FILE" 2>/dev/null || true
    grep -E -v "^[[:space:]]*export[[:space:]]+${var_name}=" "$VARS_FILE" > "$VARS_FILE.tmp" 2>/dev/null || true
    chmod 600 "$VARS_FILE.tmp" 2>/dev/null || true
    mv "$VARS_FILE.tmp" "$VARS_FILE"
  fi
  printf "export %s=%q\n" "$var_name" "$var_val" >> "$VARS_FILE"
  chmod 600 "$VARS_FILE" 2>/dev/null || true

  umask "$old_umask"
}

# ─── Locations ────────────────────────────────────────────────────────────────
# Cloud KMS has no zonal locations, so a zonal cluster's REGION (eg.
# "us-central1-c") is not a valid key location. Default to the enclosing region.
derive_kms_location() {
  local loc="${1:-}"
  if [[ "$loc" =~ ^(.+)-[a-z]$ ]]; then
    loc="${BASH_REMATCH[1]}"
  fi
  echo "$loc"
}

# ─── Pub/Sub Derivations ──────────────────────────────────────────────────────
# Derive the Google Chat Pub/Sub subscription name from the topic name.
# When CHAT_SUB_NAME is unset (empty), derive "${topic}${CHAT_SUBSCRIPTION_SUFFIX}"
# if CHAT_TOPIC_NAME is non-default, or DEFAULT_CHAT_SUB_NAME on the default topic (#1397).
# An explicit CHAT_SUB_NAME (even if set to the default subscription name) always wins
# to prevent destroying working subscriptions on existing installs during upgrade.
derive_chat_sub_name() {
  local topic="${1:-${CHAT_TOPIC_NAME:-$DEFAULT_CHAT_TOPIC_NAME}}"
  local sub="${2:-}"

  if [ -n "$sub" ]; then
    echo "$sub"
    return
  fi
  if [ -n "$topic" ] && [ "$topic" != "$DEFAULT_CHAT_TOPIC_NAME" ]; then
    echo "${topic}${CHAT_SUBSCRIPTION_SUFFIX}"
  else
    echo "$DEFAULT_CHAT_SUB_NAME"
  fi
}

# ─── GitHub Account Classification ────────────────────────────────────────────
# Classifies a GitHub account name against the public API, echoing exactly one
# of: organization | user | missing | unknown.
#
# "unknown" is the catch-all for every inconclusive answer — curl absent, the
# network down, rate limiting, an unexpected payload — so a caller can tell
# "GitHub says no" apart from "we could not ask". Never exits and never prints,
# so it is safe to call from an interactive prompt loop; callers decide whether
# an answer is fatal. install.sh uses it to validate before provisioning starts.
github_account_type() {
  local name="${1:-}"
  if [ -z "$name" ] || ! command -v curl &>/dev/null; then
    echo "unknown"
    return 0
  fi

  # Status is appended on its own line so a transport failure (curl non-zero)
  # stays distinguishable from an HTTP error (curl zero, status in the body).
  local response status body
  if ! response=$(trap - ERR; curl -sS --max-time 10 -H "Accept: application/vnd.github+json" \
      -w '\n%{http_code}' "https://api.github.com/users/${name}" 2>/dev/null); then
    echo "unknown"
    return 0
  fi
  status="${response##*$'\n'}"
  body="${response%$'\n'*}"

  if [ "$status" = "404" ]; then
    echo "missing"
    return 0
  fi
  if [ "$status" != "200" ]; then
    echo "unknown"
    return 0
  fi

  # Organization is matched first so it wins even if the payload somehow carries
  # both spellings, and both spacings are covered because the API is not
  # guaranteed to keep pretty-printing its JSON.
  case "$body" in
    *'"type": "Organization"'*|*'"type":"Organization"'*) echo "organization" ;;
    *'"type": "User"'*|*'"type":"User"'*) echo "user" ;;
    *) echo "unknown" ;;
  esac
}

# Minty resolves App installations with GET /orgs/{org}/installation and has no
# fallback to the /users/{user}/installation endpoint that serves personal
# accounts, so a user-owned GitOps repo can never mint a token. Left unchecked
# that surfaces far downstream, as an HTTP 500 from a Minty that deployed and
# passed its readiness probes, so catch it while GITOPS_ORG is still being set.
#
# This exits, so it is the wrong entry point for anything that can still
# re-prompt: install.sh calls github_account_type directly and settles the value
# before provisioning starts. An inconclusive lookup is never fatal — an
# unreachable api.github.com must not block a provision that is otherwise fine.
check_github_org_is_organization() {
  local org="${1:-}"
  [ -z "$org" ] && return 0
  # A GitLab install has no token minter, so no GitHub account to check.
  [ "${GITOPS_FORGE:-${DEFAULT_GITOPS_FORGE:-github}}" = "gitlab" ] && return 0

  if is_truthy "${SKIP_GITHUB_ORG_CHECK:-false}"; then
    print_warning "SKIP_GITHUB_ORG_CHECK=true is set; not verifying that '${org}' is an organization."
    return 0
  fi

  case "$(github_account_type "$org")" in
    organization) return 0 ;;
    user)
      print_error "GITOPS_ORG='${org}' is a GitHub user account, not an organization."
      print_error "The GitHub Token Minter looks installations up at /orgs/${org}/installation,"
      print_error "which does not exist for personal accounts, so every token request would"
      print_error "fail with a 404 after deployment."
      print_error "Move the GitOps repository to an organization (a free one is enough) and set"
      print_error "GITOPS_ORG in install.env to it, or re-run with"
      print_error "SKIP_GITHUB_ORG_CHECK=true to bypass this check."
      print_error "See the chart's githubMinter values and terraform/modules/github-minter."
      exit 1
      ;;
    missing)
      print_error "GITOPS_ORG='${org}' does not exist on GitHub."
      print_error "Check the spelling. The Token Minter resolves installations at"
      print_error "/orgs/${org}/installation, so a name that does not exist fails every"
      print_error "token request after deployment."
      print_error "Edit GITOPS_ORG in install.env, or re-run with"
      print_error "SKIP_GITHUB_ORG_CHECK=true to bypass this check."
      print_error "(GitHub Enterprise Server is not supported: this check, and the Minter,"
      print_error "both talk to api.github.com.)"
      exit 1
      ;;
    *)
      print_warning "Could not determine whether '${org}' is an organization; continuing."
      return 0
      ;;
  esac
}

# ─── Terraform State Location ─────────────────────────────────────────────────
# The bucket and prefix are derivable from the install coordinates alone, so a
# fresh clone (uninstall.sh, upgrade.sh) can find the state without any file
# from the original install. lifecycle.sh's ensure_backend and state_prefix
# derive the same two answers from the same install.defaults.env values.
tf_state_bucket() {
  local project="${1:-${PROJECT_ID:-}}"
  local bucket="${KUBE_AGENTS_STATE_BUCKET:-$DEFAULT_KUBE_AGENTS_STATE_BUCKET}"
  [ "$bucket" = "$DEFAULT_KUBE_AGENTS_STATE_BUCKET" ] && bucket="${project}${DEFAULT_TF_STATE_BUCKET_SUFFIX}"
  echo "$bucket"
}

tf_state_prefix() {
  local cluster="${1:-${CLUSTER_NAME:-}}"
  echo "${KUBE_AGENTS_STATE_PREFIX:-${DEFAULT_TF_STATE_PREFIX_ROOT}/${cluster}}"
}

# The basename of the first ENABLED version of a Cloud KMS key, or nothing.
# The token minter cannot pass readiness without one, so the generator,
# install.sh and upgrade.sh all ask before enabling it; one pipeline here so
# the three cannot come to disagree. Every failure -- no key, no API, no
# permission -- reads as "none", which the callers treat as "do not enable".
kms_key_enabled_version() {
  local key="$1" keyring="$2" location="$3" project="$4"
  { gcloud kms keys versions list --key "$key" --keyring "$keyring" \
      --location "$location" --project "$project" \
      --filter='state=ENABLED' --format='value(name.basename())' 2>/dev/null || true; } | head -1
}

# ─── terraform.tfvars Generation ──────────────────────────────────────────────
# HCL string literal with backslashes and double quotes escaped.
hcl_str() {
  local s="${1//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '"%s"' "$s"
}

hcl_bool() {
  if is_truthy "${1:-}"; then printf 'true'; else printf 'false'; fi
}

# Comma- or space-separated string → its items, one per line, trimmed, empty
# items dropped. Both separators, because --custom-roles documents "space- or
# comma-separated". hcl_csv_list renders every list key through this, so a
# check that reads a list sees the items the tfvars carry.
csv_list_items() {
  local csv="${1:-}" item had_noglob=false
  local IFS=$', \t\n'
  # Globbing off around the unquoted split: an entry such as *-sandbox (a
  # scope exclusion) would otherwise be replaced by whatever files match it
  # in the working directory. Saved and restored by hand; bash 3.2 has no
  # `local -`.
  case "$-" in *f*) had_noglob=true ;; esac
  set -f
  for item in $csv; do
    item="${item#"${item%%[![:space:]]*}"}"
    item="${item%"${item##*[![:space:]]}"}"
    [ -n "$item" ] || continue
    printf '%s\n' "$item"
  done
  $had_noglob || set +f
}

# Comma- or space-separated string → HCL list of strings (csv_list_items).
hcl_csv_list() {
  local out="[" first=true item
  while IFS= read -r item; do
    [ -n "$item" ] || continue
    $first || out+=", "
    out+="$(hcl_str "$item")"
    first=false
  done <<< "$(csv_list_items "${1:-}")"
  printf '%s]' "$out"
}

# The eight SCOPE_* keys as the composition's `scope` object: `projects`,
# `folders`, `organizations`, `shared_vpc_hosts`, `metrics_scopes` and
# `exclude.projects` are lists like every other list key, `exclude.clusters`
# is one project/location/cluster triple per entry, `max_projects` is the cap,
# written only when SCOPE_MAX_PROJECTS is set. Always a full block, empty
# lists included -- the reconcile reads an emptied projects list as the
# declaration that drops projects, a container or selector leaving the list as
# the declaration that retires its members, and a missing block as no
# declaration (docs/designs/multi-project-scope.md §7). Shape only: the caller
# has already run require_scope_cluster_triples and
# require_scope_container_ids, and the patterns, caps and repeats the CRD
# enforces are the module variable's validations, which fail the plan before
# any binding.
# SCOPE_MAX_PROJECTS is empty (the default cap) or a whole number within the
# bounds the CRD puts on spec.scope.maxProjects, or the run stops before a
# file is written and names the key and the bounds: the module's validation
# would otherwise name neither. The digit count is checked before the
# arithmetic: bash's base#digits wraps at 2^64 without a word, so a twenty-digit
# value could otherwise read as one inside the bounds. $1 the value.
require_scope_max_projects() {
  local value="${1:-}" digits
  [ -n "$value" ] || return 0
  if [[ "$value" =~ ^[0-9]+$ ]]; then
    digits="$(printf '%s' "$value" | sed 's/^0*//')"
    [ -n "$digits" ] || digits=0
    if [ "${#digits}" -le "${#SCOPE_MAX_PROJECTS_MAX}" ] && [ "$((10#$digits))" -ge "$SCOPE_MAX_PROJECTS_MIN" ] && [ "$((10#$digits))" -le "$SCOPE_MAX_PROJECTS_MAX" ]; then
      return 0
    fi
  fi
  print_error "SCOPE_MAX_PROJECTS='${value}' is not a whole number from ${SCOPE_MAX_PROJECTS_MIN} to ${SCOPE_MAX_PROJECTS_MAX}, the bounds the PlatformAgent puts on spec.scope.maxProjects. Set one, or leave it empty for the default (${SCOPE_MAX_PROJECTS_DEFAULT}), in install.env."
  return 1
}

# SCOPED_SA_POOL_MAX_ACCOUNTS is empty (the module's default) or a whole number
# of at least one, or the run stops before a file is written and names the key:
# the module's validation would name neither. No arithmetic, so no width to
# overflow: the regex is the whole check. $1 the value.
require_scoped_sa_pool_max_accounts() {
  local value="${1:-}"
  [ -n "$value" ] || return 0
  [[ "$value" =~ ^0*[1-9][0-9]*$ ]] && return 0
  print_error "SCOPED_SA_POOL_MAX_ACCOUNTS='${value}' is not a whole number of at least ${SCOPED_SA_POOL_MAX_ACCOUNTS_MIN}, the number of service accounts the scoped pool may create in the management project. Set one, or leave it empty for the default (${SCOPED_SA_POOL_MAX_ACCOUNTS_DEFAULT}), in install.env."
  return 1
}

# $8, the cap, is written only when set: unset, the module's and the CRD's
# default (100) apply, and a tfvars that names no cap keeps reading the default
# an operator never chose.
hcl_scope_block() {
  local projects="${1:-}" folders="${2:-}" organizations="${3:-}" shared_vpc_hosts="${4:-}" metrics_scopes="${5:-}" exclude_projects="${6:-}" exclude_clusters="${7:-}" max_projects="${8:-}"
  local clusters="[" first=true entry project location cluster had_noglob=false
  local IFS=$', \t\n'
  case "$-" in *f*) had_noglob=true ;; esac
  set -f
  for entry in $exclude_clusters; do
    [ -n "$entry" ] || continue
    IFS="$SCOPE_CLUSTER_TRIPLE_SEPARATOR" read -r project location cluster <<<"$entry"
    $first || clusters+=", "
    clusters+="{ project_id = $(hcl_str "$project"), location = $(hcl_str "$location"), cluster_name = $(hcl_str "$cluster") }"
    first=false
  done
  $had_noglob || set +f
  clusters+="]"
  printf 'scope = {\n  projects         = %s\n  folders          = %s\n  organizations    = %s\n  shared_vpc_hosts = %s\n  metrics_scopes   = %s\n' \
    "$(hcl_csv_list "$projects")" "$(hcl_csv_list "$folders")" "$(hcl_csv_list "$organizations")" \
    "$(hcl_csv_list "$shared_vpc_hosts")" "$(hcl_csv_list "$metrics_scopes")"
  if [ -n "$max_projects" ]; then
    printf '  max_projects     = %s\n' "$max_projects"
  fi
  printf '  exclude = {\n    projects = %s\n    clusters = %s\n  }\n}\n' \
    "$(hcl_csv_list "$exclude_projects")" "$clusters"
}

# LITELLM_REDACTION_RULES, a JSON array of rule objects, as an HCL list for the
# composition's litellm_redaction.rules. Shape only -- a list of objects whose
# keys are name, pattern, literal and action, each a string; the name, the one
# source and the action are the variable's validations. Strings are re-escaped
# rather than copied: a regular expression may hold ${ or %{, which HCL would
# read as a template. Prints nothing and returns 1, naming the key, on a value
# it cannot use. Caller defines print_error.
hcl_redaction_rules() {
  local rules_json="${1:-}" out
  if ! out="$(trap - ERR; printf '%s' "$rules_json" | python3 -c '
import json, sys
try:
    rules = json.load(sys.stdin)
except ValueError as e:
    sys.exit(f"is not valid JSON ({e})")
if not isinstance(rules, list):
    sys.exit("must be a JSON array of rule objects")
def hcl(s):
    out = []
    for ch in s:
        if ch in "\\\"":
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7f:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return "\"" + "".join(out).replace("${", "$${").replace("%{", "%%{") + "\""
items = []
for i, rule in enumerate(rules):
    if not isinstance(rule, dict):
        sys.exit(f"entry {i} is not an object")
    if "name" not in rule:
        sys.exit(f"entry {i} has no name; every rule needs one")
    unknown = sorted(set(rule) - {"name", "pattern", "literal", "action"})
    if unknown:
        sys.exit(f"entry {i} has unknown key(s) {unknown}; a rule takes name, pattern or literal, and action")
    for key, value in rule.items():
        if not isinstance(value, str):
            sys.exit(f"entry {i}: {key} must be a string")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            sys.exit(f"entry {i}: {key} is not valid UTF-8 text (a lone \\u surrogate escape?)")
    items.append("{ " + ", ".join(f"{k} = {hcl(v)}" for k, v in rule.items()) + " }")
print("[" + ", ".join(items) + "]")
' 2>&1)"; then
    # To stderr: the caller runs this inside $(...), which would swallow it.
    print_error "LITELLM_REDACTION_RULES ${out}. Fix it in install.env; the site's inference-gateway page shows the rule format." >&2
    return 1
  fi
  printf '%s' "$out"
}

# Every SCOPE_FOLDERS and SCOPE_ORGANIZATIONS entry is a bare numeric ID, or
# the run stops before a file is written and names the entry and its key: a
# folders/<id> spelling would otherwise reach terraform's variable validation
# with a message naming neither. $1 the folders, $2 the organisations. Caller
# defines print_error.
require_scope_container_ids() {
  local folders="${1:-}" organizations="${2:-}" key entries entry had_noglob=false
  local IFS=$', \t\n'
  case "$-" in *f*) had_noglob=true ;; esac
  set -f
  for key in SCOPE_FOLDERS SCOPE_ORGANIZATIONS; do
    [ "$key" = SCOPE_FOLDERS ] && entries="$folders" || entries="$organizations"
    for entry in $entries; do
      [ -n "$entry" ] || continue
      if ! [[ "$entry" =~ $SCOPE_CONTAINER_ID_PATTERN ]]; then
        $had_noglob || set +f
        print_error "${key} entry '${entry}' is not a numeric Resource Manager ID. Name each folder or organisation by its bare number in install.env (123456789012, not folders/123456789012)."
        return 1
      fi
    done
  done
  $had_noglob || set +f
}

# Every SCOPE_EXCLUDE_CLUSTERS entry is project/location/cluster, or the run
# stops before a file is written and names the entry. Caller defines
# print_error.
require_scope_cluster_triples() {
  local entries="${1:-}" entry had_noglob=false
  local IFS=$', \t\n'
  case "$-" in *f*) had_noglob=true ;; esac
  set -f
  for entry in $entries; do
    [ -n "$entry" ] || continue
    if ! [[ "$entry" =~ $SCOPE_CLUSTER_TRIPLE_PATTERN ]]; then
      $had_noglob || set +f
      print_error "SCOPE_EXCLUDE_CLUSTERS entry '${entry}' is not project${SCOPE_CLUSTER_TRIPLE_SEPARATOR}location${SCOPE_CLUSTER_TRIPLE_SEPARATOR}cluster. Name each excluded cluster by its project ID, location and name in install.env."
      return 1
    fi
  done
  $had_noglob || set +f
}

# Refuses a full apply that would replace a scope the live PlatformAgent
# carries and nothing this install wrote accounts for.
#
# The chart renders spec.scope from the keys on every full apply, and Helm
# patches a custom resource from the difference between its rendered
# manifests, so the first apply that renders the block replaces whatever the
# CR holds -- and the reconcile reads the emptied projects list as the
# declaration that retires those projects' profiles. Three values decide:
#
#   L  the live CR's spec.scope;
#   R  the release record's platformAgent.scope (helm get values): what the
#      installer last rendered, absent before the first apply that carried
#      the value, and never anything else, because the retag modes reuse the
#      record as it is. Read from the latest revision and from the last
#      revision that served (deployed or superseded), because a failed
#      upgrade leaves its values as the latest revision while the CR still
#      holds the served one, and the retry must not read that as a hand edit;
#   K  the scope the SCOPE_* keys declare now.
#
# Refused when L is present and non-empty (a list, an exclusion, or a cap off
# the CRD's default), L != R and L != K: the CR carries a
# declaration the installer did not write and the keys do not reproduce. Every
# other case passes -- nothing live to protect; L == R, the installer wrote it
# and the keys are the new declaration, emptying it included; L == K, the
# operator recorded it. No PlatformAgent type served, no CR, no release: pass.
# L, R and K are the projects, folders, organisations, Shared VPC hosts,
# Metrics Scopes and exclusions, every list the chart renders, since a list it
# renders is one the apply replaces, the cap, which the apply sets the same
# way (a CR at the CRD's default reads as the key unset), and the scoped
# pool's switch, spec.security.scopedServiceAccountPool.enabled, which the
# generator writes from SCOPED_SA_POOL_ENABLED alone (false when the key is
# absent): a live true the key does not record would be disarmed by the apply,
# its members destroyed and the broker put on the ambient credential, so it
# weighs like a cap off the default, an armed pool alone included. The members
# list is derived from the scope and is not compared.
# Anything that stops the read -- no context for this install in the
# kubeconfig, the CR or the record unreadable -- is a refusal, because the
# apply itself needs no kubeconfig (the helm provider authenticates with a
# token against the endpoint) and would go ahead over a scope nobody read.
# $1 namespace; $2 "warn" turns every refusal into a warning (upgrade.sh
# --plan applies nothing). Caller defines print_error / print_info /
# print_warning.
refuse_apply_over_undeclared_scope() {
  local namespace="${1:-${NAMESPACE:-$DEFAULT_NAMESPACE}}" mode="${2:-$SCOPE_CHECK_MODE_REFUSE}"
  local expected_ctx cr_json record_json served_json err_file verdict first_line
  expected_ctx="$(gke_context_name)"
  # The reads stay held for the mode notice each front door calls next
  # (announce_platform_agent_mode_for_apply), which takes them rather than
  # asking the cluster the same questions again.
  read_platform_agent_install_state "$namespace"
  case "$PLATFORM_AGENT_CR_READ" in
    "$PLATFORM_AGENT_READ_NO_CONTEXT")
      _scope_check_failed "$mode" "the kubeconfig has no context '${expected_ctx}' to read the PlatformAgent through (run: gcloud container clusters get-credentials ${CLUSTER_NAME} --location ${REGION} --project ${PROJECT_ID})"
      return $?
      ;;
    "$PLATFORM_AGENT_READ_NONE") return 0 ;;
    "$PLATFORM_AGENT_READ_FAILED")
      _scope_check_failed "$mode" "the PlatformAgent in namespace '${namespace}' could not be read through context '${expected_ctx}': ${PLATFORM_AGENT_READ_ERROR}"
      return $?
      ;;
  esac
  if [ "$PLATFORM_AGENT_RECORD_READ" = "$PLATFORM_AGENT_READ_FAILED" ]; then
    _scope_check_failed "$mode" "the values of release '${KUBE_AGENTS_HELM_RELEASE}' in namespace '${namespace}' could not be read: ${PLATFORM_AGENT_READ_ERROR}"
    return $?
  fi
  cr_json="$PLATFORM_AGENT_CR_JSON"
  record_json="$PLATFORM_AGENT_RECORD_JSON"
  served_json="$PLATFORM_AGENT_SERVED_JSON"
  err_file="$(mktemp)"
  # Normalises L, R and K to sorted lists and rules. Values arrive as argv and
  # on stdin, never interpolated into the program text. stderr goes to the
  # file, not into the verdict: an interpreter that warns at startup and exits
  # 0 would otherwise put its warning where the first line is read.
  if ! verdict="$(trap - ERR; printf '%s\n\x1e\n%s\n\x1e\n%s\n' "$cr_json" "$record_json" "$served_json" | python3 -c '
import json, re, sys
cr_text, record_text, served_text = sys.stdin.read().split("\x1e\n", 2)
ok, refuse = sys.argv[1], sys.argv[2]
keys = sys.argv[3:11]
default_cap = int(sys.argv[11])
pool_key_armed = sys.argv[12] == "true"

def split(value):
    return [item for item in re.split(r"[,\s]+", value) if item]

def pool_armed(spec_or_values):
    # spec.security.scopedServiceAccountPool.enabled on the CR, and the same
    # path under platformAgent in the record; absent reads as false.
    security = (spec_or_values or {}).get("security") or {}
    return bool((security.get("scopedServiceAccountPool") or {}).get("enabled"))

def normalise(scope, armed):
    scope = scope or {}
    exclude = scope.get("exclude") or {}
    clusters = sorted(
        {(c["projectId"], c["location"], c["clusterName"]) for c in exclude.get("clusters") or []}
    )
    return {
        "projects": sorted(set(scope.get("projects") or [])),
        "folders": sorted(set(scope.get("folders") or [])),
        "organizations": sorted(set(scope.get("organizations") or [])),
        "sharedVpcHosts": sorted(set(scope.get("sharedVpcHosts") or [])),
        "metricsScopes": sorted(set(scope.get("metricsScopes") or [])),
        "maxProjects": int(scope.get("maxProjects") or default_cap),
        "exclude": {"projects": sorted(set(exclude.get("projects") or [])), "clusters": clusters},
        "scopedPoolEnabled": bool(armed),
    }

def is_empty(scope):
    # Nothing live to protect: no list, no exclusion, the cap at the default
    # the CRD sets, which is what the apply renders for a key left unset, and
    # the pool off, which is what it renders for SCOPED_SA_POOL_ENABLED absent.
    return not (scope["projects"] or scope["folders"] or scope["organizations"]
                or scope["sharedVpcHosts"] or scope["metricsScopes"]
                or scope["exclude"]["projects"] or scope["exclude"]["clusters"]
                or scope["maxProjects"] != default_cap
                or scope["scopedPoolEnabled"])

items = json.loads(cr_text).get("items") or []
if len(items) > 1:
    sys.exit("more than one PlatformAgent is served: " + ", ".join(i["metadata"]["name"] for i in items))
spec = items[0].get("spec") or {} if items else {}
# A CR with no scope block normalises to the empty declaration, so the switch
# is weighed whether or not a scope stands beside it.
live = normalise(spec.get("scope"), pool_armed(spec))
if is_empty(live):
    print(ok)
    sys.exit(0)
def recorded(text):
    values = (json.loads(text or "{}") or {}).get("platformAgent") or {}
    raw = values.get("scope")
    return normalise(raw, pool_armed(values)) if raw is not None else None

records = [r for r in (recorded(record_text), recorded(served_text)) if r is not None]
declared = normalise({
    "projects": split(keys[0]),
    "folders": split(keys[1]),
    "organizations": split(keys[2]),
    "sharedVpcHosts": split(keys[3]),
    "metricsScopes": split(keys[4]),
    "maxProjects": int(keys[7]) if keys[7].strip() else default_cap,
    "exclude": {
        "projects": split(keys[5]),
        "clusters": [dict(zip(("projectId", "location", "clusterName"), t.split("/"))) for t in split(keys[6])],
    },
}, pool_key_armed)
if live in records or live == declared:
    print(ok)
    sys.exit(0)
print(refuse)
print(items[0]["metadata"]["name"])
print("SCOPE_PROJECTS=" + json.dumps(" ".join(live["projects"])))
print("SCOPE_FOLDERS=" + json.dumps(" ".join(live["folders"])))
print("SCOPE_ORGANIZATIONS=" + json.dumps(" ".join(live["organizations"])))
print("SCOPE_SHARED_VPC_HOSTS=" + json.dumps(" ".join(live["sharedVpcHosts"])))
print("SCOPE_METRICS_SCOPES=" + json.dumps(" ".join(live["metricsScopes"])))
print("SCOPE_EXCLUDE_PROJECTS=" + json.dumps(" ".join(live["exclude"]["projects"])))
print("SCOPE_EXCLUDE_CLUSTERS=" + json.dumps(" ".join("/".join(c) for c in live["exclude"]["clusters"])))
# Always among the lines: when the cap is why the compare refused, the key the
# operator has to change may be one they must blank, not set.
print("SCOPE_MAX_PROJECTS=" + json.dumps(str(live["maxProjects"]) if live["maxProjects"] != default_cap else ""))
# The same for the pool switch: blank when the live pool is off, so the lines
# never reproduce a false, and the key an operator may have to blank is named.
print("SCOPED_SA_POOL_ENABLED=" + json.dumps("true" if live["scopedPoolEnabled"] else ""))
' "$SCOPE_VERDICT_OK" "$SCOPE_VERDICT_REFUSE" "${SCOPE_PROJECTS:-}" "${SCOPE_FOLDERS:-}" "${SCOPE_ORGANIZATIONS:-}" "${SCOPE_SHARED_VPC_HOSTS:-}" "${SCOPE_METRICS_SCOPES:-}" "${SCOPE_EXCLUDE_PROJECTS:-}" "${SCOPE_EXCLUDE_CLUSTERS:-}" "${SCOPE_MAX_PROJECTS:-}" "$SCOPE_MAX_PROJECTS_DEFAULT" "$(hcl_bool "${SCOPED_SA_POOL_ENABLED:-$SCOPED_SA_POOL_ENABLED_DEFAULT}")" 2>"$err_file")"; then
    _scope_check_failed "$mode" "the live and recorded scope could not be compared: $(tr '\n' ' ' <"$err_file" | sed 's/[[:space:]]*$//')"
    local rc=$?
    rm -f "$err_file"
    return $rc
  fi
  rm -f "$err_file"
  first_line="${verdict%%$'\n'*}"
  [ "$first_line" != "$SCOPE_VERDICT_OK" ] || return 0
  local lines cr_name
  lines="${verdict#*$'\n'}"
  cr_name="${lines%%$'\n'*}"
  lines="${lines#*$'\n'}"
  if [ "$mode" = "$SCOPE_CHECK_MODE_WARN" ]; then
    print_warning "The PlatformAgent '${cr_name}' in namespace '${namespace}' declares a scope this install did not write and install.env does not carry (the scoped service account pool's switch counts as part of it). A full upgrade would replace it, and the reconcile would retire the projects it drops, or disarm the pool; the plan below shows the change. Record the live declaration in install.env first:"
  else
    print_error "The PlatformAgent '${cr_name}' in namespace '${namespace}' declares a scope this install did not write and install.env does not carry (the scoped service account pool's switch counts as part of it). The first apply whose rendered scope differs from the recorded one replaces it, and the reconcile then retires the projects it drops, deleting their Cluster Agent profiles over its next two clean runs; a pool armed on the CR that SCOPED_SA_POOL_ENABLED does not record is disarmed the same way, its members destroyed and the credential broker put on the agent's own credential."
    print_info "Record the live declaration in install.env and re-run:"
  fi
  while IFS= read -r first_line; do
    print_info "  ${first_line}"
  done <<<"$lines"
  [ "$mode" = "$SCOPE_CHECK_MODE_WARN" ] && return 0
  print_info "Or, if install.env is right and the PlatformAgent is not, edit the PlatformAgent's spec.scope (and spec.security.scopedServiceAccountPool.enabled) to what install.env declares and re-run; the apply then renders the same value it already holds."
  return 1
}

# Preflight for a declared folder or organisation, before the apply that
# would bind the agent's roles on the container. The IAM probe is made as the
# identity Terraform applies with, read the way the google provider reads its
# credentials (_scope_terraform_access_token), which need not be gcloud's
# active account; the API and policy reads go through gcloud's active
# account, whose answer does not depend on who asks: (1) the Cloud Asset API
# the reconcile's container search calls can be enabled in the host project,
# meaning it is enabled already or no effective organisation policy forbids
# it, (2) this identity can set IAM policy on every container named, so
# the apply does not stop partway with the API enabled and some containers
# bound, and (3) while the scoped service account pool is armed
# (scope_pool_lists_containers), this identity can search every container's
# members through that API, which the plan does for the pool after
# enable_scope_selector_apis has enabled it and refuses without. Every
# container is probed and every failure named before the run
# stops (docs/designs/multi-project-scope.md §6); a probe that cannot decide
# (no curl, no token, a transport error) warns and lets the apply speak,
# because an apply that fails to bind fails loudly, unlike the scope replace
# refuse_apply_over_undeclared_scope guards against. An install that declares
# no container returns silently and never touches the Asset API. $1 "warn"
# turns the refusal into a warning: upgrade.sh --plan applies nothing, and
# install.sh --generate-only hands the apply to an identity that may not be
# the one at the keyboard.
# Caller defines print_error / print_info / print_warning.
check_scope_container_access() {
  local mode="${1:-$SCOPE_CHECK_MODE_REFUSE}"
  local folders="${SCOPE_FOLDERS:-}" organizations="${SCOPE_ORGANIZATIONS:-}"
  [[ "${folders}${organizations}" == *[![:space:],]* ]] || return 0
  local project="${PROJECT_ID:-}" entry token="" constraint failures=() undecided=() rc had_noglob=false identity properties reason pool_lists=false
  identity="$(_scope_terraform_identity_label)"
  ! scope_pool_lists_containers || pool_lists=true
  if [[ "$organizations" == *[![:space:],]* ]]; then
    print_warning "SCOPE_ORGANIZATIONS binds the agent's read roles on the whole organisation, every project in it included. The design recommends folders until the scoped service account pool grants authority (docs/designs/multi-project-scope.md §9)."
  fi
  # Every probe below is called in a `||` list: the front doors run under
  # `set -eE` with an ERR trap, and a probe answering "denied" is an answer,
  # not an error, on the bare call upgrade.sh --plan makes as well as on the
  # `|| exit 1` call the applying modes make.
  if ! _scope_asset_api_enabled "$project"; then
    rc=0
    constraint="$(_scope_policy_denying_asset_api "$project")" || rc=$?
    if [ "$rc" -eq "$SCOPE_PROBE_DENIED" ]; then
      failures+=("${SCOPE_ASSET_API} cannot be enabled in project '${project}': the effective organisation policy ${constraint} denies it, and the reconcile resolves a folder or organisation through that API. Ask the organisation's administrator for an exception, or declare explicit SCOPE_PROJECTS instead.")
    elif [ "$rc" -eq "$SCOPE_PROBE_UNDECIDED" ]; then
      undecided+=("whether ${SCOPE_ASSET_API} can be enabled in project '${project}' (the organisation policy could not be read)")
    fi
  fi
  if ! command -v curl >/dev/null 2>&1; then
    undecided+=("whether ${identity} can set IAM policy on the declared containers (curl is not installed)")
  elif properties="$(_scope_gcloud_auth_properties_in_the_way)" && [ -n "$properties" ]; then
    undecided+=("whether ${identity} can set IAM policy on the declared containers (gcloud's active configuration sets ${properties}, which its token mint honours and Terraform does not; run: gcloud config unset <property>, or set GOOGLE_IMPERSONATE_SERVICE_ACCOUNT to apply as that account)")
  elif ! token="$(_scope_terraform_access_token)" || [ -z "$token" ]; then
    undecided+=("whether ${identity} can set IAM policy on the declared containers ($(_scope_terraform_token_remedy))")
  else
    local IFS=$', \t\n'
    case "$-" in *f*) had_noglob=true ;; esac
    set -f
    for entry in $folders; do
      [ -n "$entry" ] || continue
      rc=0
      reason="$(_scope_container_can_set_iam "folders/${entry}" "$SCOPE_FOLDER_SET_IAM_PERMISSION" "$token")" || rc=$?
      if [ "$rc" -eq "$SCOPE_PROBE_DENIED" ]; then
        failures+=("${identity} cannot set IAM policy on folders/${entry} (${SCOPE_FOLDER_SET_IAM_PERMISSION}), or the folder does not exist; the apply would fail binding the agent's roles there. Ask for roles/resourcemanager.folderIamAdmin on the folder for that identity, or drop it from SCOPE_FOLDERS.")
      elif [ "$rc" -ne "$SCOPE_PROBE_GRANTED" ]; then
        # Undecided, and any status the probe does not define: nothing but a
        # granted answer passes silently.
        undecided+=("whether ${identity} can set IAM policy on folders/${entry}${reason:+ ($reason)}")
      fi
      $pool_lists || continue
      rc=0
      reason="$(_scope_container_can_set_iam "folders/${entry}" "$SCOPE_CONTAINER_ASSET_SEARCH_PERMISSION" "$token")" || rc=$?
      if [ "$rc" -eq "$SCOPE_PROBE_DENIED" ]; then
        failures+=("${identity} cannot list the members of folders/${entry} (${SCOPE_CONTAINER_ASSET_SEARCH_PERMISSION}); the plan ${SCOPE_POOL_CONTAINER_READ_REASON} and would refuse. Ask for ${SCOPE_CONTAINER_ASSET_SEARCH_ROLE} on the folder for that identity, set SCOPED_SA_POOL_ENABLED=false, or drop it from SCOPE_FOLDERS.")
      elif [ "$rc" -ne "$SCOPE_PROBE_GRANTED" ]; then
        undecided+=("whether ${identity} can list the members of folders/${entry}${reason:+ ($reason)}")
      fi
    done
    for entry in $organizations; do
      [ -n "$entry" ] || continue
      rc=0
      reason="$(_scope_container_can_set_iam "organizations/${entry}" "$SCOPE_ORGANIZATION_SET_IAM_PERMISSION" "$token")" || rc=$?
      if [ "$rc" -eq "$SCOPE_PROBE_DENIED" ]; then
        failures+=("${identity} cannot set IAM policy on organizations/${entry} (${SCOPE_ORGANIZATION_SET_IAM_PERMISSION}), or the organisation is not visible to it; the apply would fail binding the agent's roles there. Ask for roles/resourcemanager.organizationAdmin for that identity, or drop it from SCOPE_ORGANIZATIONS.")
      elif [ "$rc" -ne "$SCOPE_PROBE_GRANTED" ]; then
        # Undecided, and any status the probe does not define: nothing but a
        # granted answer passes silently.
        undecided+=("whether ${identity} can set IAM policy on organizations/${entry}${reason:+ ($reason)}")
      fi
      $pool_lists || continue
      rc=0
      reason="$(_scope_container_can_set_iam "organizations/${entry}" "$SCOPE_CONTAINER_ASSET_SEARCH_PERMISSION" "$token")" || rc=$?
      if [ "$rc" -eq "$SCOPE_PROBE_DENIED" ]; then
        failures+=("${identity} cannot list the members of organizations/${entry} (${SCOPE_CONTAINER_ASSET_SEARCH_PERMISSION}); the plan ${SCOPE_POOL_CONTAINER_READ_REASON} and would refuse. Ask for ${SCOPE_CONTAINER_ASSET_SEARCH_ROLE} on the organisation for that identity, set SCOPED_SA_POOL_ENABLED=false, or drop it from SCOPE_ORGANIZATIONS.")
      elif [ "$rc" -ne "$SCOPE_PROBE_GRANTED" ]; then
        undecided+=("whether ${identity} can list the members of organizations/${entry}${reason:+ ($reason)}")
      fi
    done
    $had_noglob || set +f
  fi
  for entry in ${undecided[@]+"${undecided[@]}"}; do
    print_warning "The scope container preflight could not decide ${entry}; the apply will report it if it fails."
  done
  [ "${#failures[@]}" -eq 0 ] && return 0
  if [ "$mode" = "$SCOPE_CHECK_MODE_WARN" ]; then
    # Route-neutral: the warn mode serves upgrade.sh --plan, install.sh
    # --generate-only and the interactive g answer alike, and only the last
    # two hand the apply to lifecycle.sh under credentials this run cannot see.
    for entry in "${failures[@]}"; do print_warning "An applying run would be refused: ${entry}"; done
    return 0
  fi
  for entry in "${failures[@]}"; do print_error "Refusing to apply: ${entry}"; done
  print_info "Nothing was changed. Fix what is named above, or edit SCOPE_FOLDERS and SCOPE_ORGANIZATIONS in install.env, and re-run."
  return 1
}

# True when the plan lists a container's members for the scoped service
# account pool: SCOPED_SA_POOL_ENABLED is a true spelling and a folder or
# organisation is declared. With the pool off no container is read at plan.
scope_pool_lists_containers() {
  is_truthy "${SCOPED_SA_POOL_ENABLED:-$SCOPED_SA_POOL_ENABLED_DEFAULT}" || return 1
  [[ "${SCOPE_FOLDERS:-}${SCOPE_ORGANIZATIONS:-}" == *[![:space:],]* ]]
}

# Prints, space-separated, the APIs the plan reads for the selectors the
# environment declares (SCOPE_METRICS_SCOPE_APIS for SCOPE_METRICS_SCOPES,
# SCOPE_SHARED_VPC_HOST_APIS for SCOPE_SHARED_VPC_HOSTS, SCOPE_ASSET_API for
# the pool's listing of a declared folder's or organisation's members);
# nothing when none is declared.
scope_selector_apis() {
  local apis=""
  if [[ "${SCOPE_METRICS_SCOPES:-}" == *[![:space:],]* ]]; then
    apis="$SCOPE_METRICS_SCOPE_APIS"
  fi
  if [[ "${SCOPE_SHARED_VPC_HOSTS:-}" == *[![:space:],]* ]]; then
    apis+="${apis:+ }$SCOPE_SHARED_VPC_HOST_APIS"
  fi
  if scope_pool_lists_containers; then
    apis+="${apis:+ }$SCOPE_ASSET_API"
  fi
  printf '%s' "$apis"
}

# Prints what the plan does through the APIs scope_selector_apis names, as
# the clause(s) after "the plan": the selector read, the pool's container
# listing, or both joined; nothing when scope_selector_apis prints nothing.
scope_selector_apis_reason() {
  local reason=""
  if [[ "${SCOPE_SHARED_VPC_HOSTS:-}${SCOPE_METRICS_SCOPES:-}" == *[![:space:],]* ]]; then
    reason="$SCOPE_SELECTOR_READ_REASON"
  fi
  if scope_pool_lists_containers; then
    reason+="${reason:+ and }$SCOPE_POOL_CONTAINER_READ_REASON"
  fi
  printf '%s' "$reason"
}

# Prints the APIs of scope_selector_apis not enabled in project $1,
# space-separated (nothing when all are on); returns 1 when the listing
# failed, and the caller decides what that means. Read through gcloud's
# active account, whose answer does not depend on who asks.
scope_selector_apis_missing() {
  local project="$1" enabled api missing=""
  enabled="$(trap - ERR; gcloud services list --enabled --project "$project" --format='value(config.name)' 2>/dev/null)" || return 1
  for api in $(scope_selector_apis); do
    grep -qx "$api" <<<"$enabled" || missing+="${missing:+ }$api"
  done
  printf '%s' "$missing"
}

# Enables, in the management project, whichever of the APIs the plan-time
# resolution of the declared Shared VPC hosts or Metrics Scopes reads, or the
# scoped service account pool's listing of a declared folder's or
# organisation's members (scope_selector_apis), is not enabled yet. The reads
# run in the plan and are billed to that project, and the composition enables
# the APIs only in the apply that follows, so a first install that declared a
# selector, or armed the pool beside a container, would otherwise be refused
# at plan with the API reported disabled. Nothing is called when every API is
# on already, which is every re-run and every Day-2 apply of an existing
# install, and nothing when no selector is declared and no container is
# listed. The
# enable runs as gcloud's active account, like the KMS enablement beside it;
# a failure is a warning and the run goes on, because the plan reports a
# disabled API with the same command as the remedy, and an account that
# cannot enable services may still not be the one Terraform applies with.
# Runs where the apply is about to happen, never on a plan or a generate-only
# run, whose handoff names the command instead. $1 the project (PROJECT_ID by
# default). Caller defines print_info / print_warning.
enable_scope_selector_apis() {
  [ -n "$(scope_selector_apis)" ] || return 0
  local project="${1:-${PROJECT_ID:-}}" missing rc=0 reason
  reason="$(scope_selector_apis_reason)"
  missing="$(scope_selector_apis_missing "$project")" || rc=$?
  if [ "$rc" -ne 0 ]; then
    missing="$(scope_selector_apis)"
    print_info "The enabled APIs of project '${project}' could not be listed; enabling ${missing// /, }, which the plan reads when it ${reason}, and which is idempotent..."
  elif [ -z "$missing" ]; then
    return 0
  else
    print_info "Enabling ${missing// /, } in project '${project}': the plan ${reason} through it, before the apply that would otherwise enable it..."
  fi
  # shellcheck disable=SC2086
  if ! (trap - ERR; gcloud services enable $missing --project="$project"); then
    print_warning "Could not enable ${missing// /, } in project '${project}' as gcloud's active account. If the plan is refused with the API reported disabled, enable it with an account that can (gcloud services enable ${missing} --project=${project}) and re-run."
  fi
}

# The credentials the google provider will apply with, read in its own order
# of precedence: GOOGLE_OAUTH_ACCESS_TOKEN; else the key file or inline key
# JSON in GOOGLE_CREDENTIALS, GOOGLE_CLOUD_KEYFILE_JSON or GCLOUD_KEYFILE_JSON
# (an existing path is a file, anything else is the key's JSON, the provider's
# own rule); else the Application Default Credentials, which read
# GOOGLE_APPLICATION_CREDENTIALS first; each impersonating
# GOOGLE_IMPERSONATE_SERVICE_ACCOUNT when the operator set it, the token
# serving only as the source credential then. gcloud's own CLOUDSDK_AUTH_*
# variables are cleared for every mint, because the provider does not read
# them and a gcloud impersonation the operator configured for other work
# would otherwise answer for an identity Terraform never uses. The value of a
# credential variable is never printed: the variable's name is enough, and an
# inline key is a private key. Three readers of that one rule: the variable
# that holds the credentials, a label for messages, and the token itself.
_scope_terraform_credentials_var() {
  if [ -n "${GOOGLE_OAUTH_ACCESS_TOKEN:-}" ]; then printf 'GOOGLE_OAUTH_ACCESS_TOKEN'
  elif [ -n "${GOOGLE_CREDENTIALS:-}" ]; then printf 'GOOGLE_CREDENTIALS'
  elif [ -n "${GOOGLE_CLOUD_KEYFILE_JSON:-}" ]; then printf 'GOOGLE_CLOUD_KEYFILE_JSON'
  elif [ -n "${GCLOUD_KEYFILE_JSON:-}" ]; then printf 'GCLOUD_KEYFILE_JSON'
  fi
}

# Names the identity the probe is made as, so a refusal sends the operator to
# grant a role to the principal that will apply, not to whatever ADC the
# workstation holds.
_scope_terraform_identity_label() {
  local var base
  var="$(_scope_terraform_credentials_var)"
  case "$var" in
    GOOGLE_OAUTH_ACCESS_TOKEN) base="the identity behind GOOGLE_OAUTH_ACCESS_TOKEN" ;;
    "") base="the Application Default Credentials${GOOGLE_APPLICATION_CREDENTIALS:+ in GOOGLE_APPLICATION_CREDENTIALS}" ;;
    *) base="the credentials in ${var}" ;;
  esac
  if [ -n "${GOOGLE_IMPERSONATE_SERVICE_ACCOUNT:-}" ]; then
    printf '%s impersonating %s (the identity Terraform applies with)' "$base" "$GOOGLE_IMPERSONATE_SERVICE_ACCOUNT"
  else
    printf '%s (the identity Terraform applies with)' "$base"
  fi
}

# What to do when no token could be minted for those credentials, by source.
_scope_terraform_token_remedy() {
  local var creds
  var="$(_scope_terraform_credentials_var)"
  case "$var" in
    GOOGLE_OAUTH_ACCESS_TOKEN)
      printf 'gcloud could not mint an impersonated token from GOOGLE_OAUTH_ACCESS_TOKEN; check the token is current and may impersonate %s' "${GOOGLE_IMPERSONATE_SERVICE_ACCOUNT:-}" ;;
    "")
      if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
        printf 'gcloud could not mint an access token for them; run: gcloud auth application-default login'
      elif [ -f "$GOOGLE_APPLICATION_CREDENTIALS" ]; then
        printf 'the key file GOOGLE_APPLICATION_CREDENTIALS names could not mint a token; check it is a valid service-account key%s, or unset the variable to use the login credentials' "${GOOGLE_IMPERSONATE_SERVICE_ACCOUNT:+ that may impersonate $GOOGLE_IMPERSONATE_SERVICE_ACCOUNT}"
      else
        printf 'GOOGLE_APPLICATION_CREDENTIALS names a file that does not exist; the apply would fail the same way'
      fi ;;
    *)
      creds="$(_scope_expand_home "${!var}")"
      if [ -f "$creds" ]; then
        printf 'the key file %s names could not mint a token; check it is a valid service-account key%s' "$var" "${GOOGLE_IMPERSONATE_SERVICE_ACCOUNT:+ that may impersonate $GOOGLE_IMPERSONATE_SERVICE_ACCOUNT}"
      else
        # Not an existing file, and gcloud refused it as key JSON. Which of
        # the two the operator meant is not decidable here and the value is
        # never printed, so the remedy names both readings.
        printf '%s is neither a file that exists nor key JSON gcloud accepts; check the path, or that the value is the key'"'"'s JSON itself (not base64)%s' "$var" "${GOOGLE_IMPERSONATE_SERVICE_ACCOUNT:+, and that it may impersonate $GOOGLE_IMPERSONATE_SERVICE_ACCOUNT}"
      fi ;;
  esac
}

# Prints, space-separated, the gcloud auth properties set in the active
# configuration that a gcloud mint would honour and the google provider would
# not: auth/access_token_file always, auth/impersonate_service_account unless
# GOOGLE_IMPERSONATE_SERVICE_ACCOUNT names the account explicitly (the flag
# the mint passes then overrides the property). Empty when none is in the
# way, and when no gcloud mint will be made at all (a raw token with no
# impersonation). A property gcloud cannot report reads as unset.
_scope_gcloud_auth_properties_in_the_way() {
  local property value out=""
  local -a clear=()
  if [ -n "${GOOGLE_OAUTH_ACCESS_TOKEN:-}" ] && [ -z "${GOOGLE_IMPERSONATE_SERVICE_ACCOUNT:-}" ]; then
    return 0
  fi
  # shellcheck disable=SC2207
  clear=($(_scope_gcloud_env_clear_args))
  for property in $SCOPE_GCLOUD_AUTH_PROPERTIES; do
    if [ "$property" = "auth/impersonate_service_account" ] && [ -n "${GOOGLE_IMPERSONATE_SERVICE_ACCOUNT:-}" ]; then
      continue
    fi
    # Under the same cleared environment as the mint: `config get-value`
    # reports the effective value, and a CLOUDSDK_AUTH_* variable would
    # otherwise read as a property the operator never set.
    value="$(trap - ERR; env "${clear[@]}" gcloud config get-value "$property" 2>/dev/null || true)"
    case "$value" in ""|"(unset)") continue ;; esac
    out="${out:+$out }${property}"
  done
  printf '%s' "$out"
}

# Prints the `-u VAR` pairs that clear gcloud's own credential overrides for
# one `env` call. Word-split by the caller into an array (the values are
# variable names, so splitting is safe); no nameref, because the front doors
# run on bash 3.2 as well.
_scope_gcloud_env_clear_args() {
  local keep="${1:-}" var
  for var in $SCOPE_GCLOUD_AUTH_OVERRIDES; do
    [ "$var" = "$keep" ] || printf -- '-u %s ' "$var"
  done
}

# A credential path as the google provider reads it: a leading ~ is the
# home directory (its pathOrContents expands it), which a quoted install.env
# line or a CI environment block hands over unexpanded.
_scope_expand_home() {
  # The literal tilde is the point: shellcheck reads the pattern as a path.
  # shellcheck disable=SC2088
  case "$1" in
    "~") printf '%s' "$HOME" ;;
    "~/"*) printf '%s%s' "$HOME" "${1#\~}" ;;
    *) printf '%s' "$1" ;;
  esac
}

# Prints the access token for those credentials; fails when none can be
# minted. Inline key JSON reaches gcloud through a file that lives only inside
# the subshell that mints the token and is removed on that subshell's exit,
# a signal included, so an interrupted run leaves no private key at rest.
_scope_terraform_access_token() {
  local var creds
  local -a impersonate=() clear=()
  [ -z "${GOOGLE_IMPERSONATE_SERVICE_ACCOUNT:-}" ] || impersonate=("--impersonate-service-account=${GOOGLE_IMPERSONATE_SERVICE_ACCOUNT}")
  # shellcheck disable=SC2207
  clear=($(_scope_gcloud_env_clear_args))
  var="$(_scope_terraform_credentials_var)"
  case "$var" in
    GOOGLE_OAUTH_ACCESS_TOKEN)
      if [ "${#impersonate[@]}" -eq 0 ]; then
        printf '%s' "$GOOGLE_OAUTH_ACCESS_TOKEN"
        return 0
      fi
      # gcloud takes a raw token as its credential through this variable and
      # impersonates on top of it, as the provider does with access_token. The
      # variable is set in the shell's environment for the call, never as an
      # argument to env, whose argv any local user can read; env therefore
      # clears every override but this one.
      # shellcheck disable=SC2207
      clear=($(_scope_gcloud_env_clear_args CLOUDSDK_AUTH_ACCESS_TOKEN))
      (trap - ERR; CLOUDSDK_AUTH_ACCESS_TOKEN="$GOOGLE_OAUTH_ACCESS_TOKEN" env "${clear[@]}" gcloud auth print-access-token "${impersonate[@]}" 2>/dev/null)
      return ;;
    "")
      (trap - ERR; env "${clear[@]}" gcloud auth application-default print-access-token ${impersonate[@]+"${impersonate[@]}"} 2>/dev/null)
      return ;;
  esac
  creds="$(_scope_expand_home "${!var}")"
  if [ -f "$creds" ]; then
    (trap - ERR; env "${clear[@]}" GOOGLE_APPLICATION_CREDENTIALS="$creds" gcloud auth application-default print-access-token ${impersonate[@]+"${impersonate[@]}"} 2>/dev/null)
    return
  fi
  # Not an existing file, so the key's JSON, whatever byte it starts with
  # (a leading newline or space from a heredoc or a secret store is JSON to
  # the provider too). gcloud validates it; the preflight never parses or
  # prints it.
  (
    trap - ERR
    key_file="$(mktemp)"
    trap 'rm -f "$key_file"' EXIT
    trap 'rm -f "$key_file"; exit 130' INT TERM HUP
    (umask 077; printf '%s' "$creds" >"$key_file")
    env "${clear[@]}" GOOGLE_APPLICATION_CREDENTIALS="$key_file" gcloud auth application-default print-access-token ${impersonate[@]+"${impersonate[@]}"} 2>/dev/null
  )
}

# True when the Asset API is already enabled in the host project. A listing
# that fails reads as not enabled, which sends the caller to the policy read.
_scope_asset_api_enabled() {
  local project="$1" enabled
  enabled="$(trap - ERR; gcloud services list --enabled --project "$project" \
    --filter="config.name=${SCOPE_ASSET_API}" --format="value(config.name)" 2>/dev/null)" || return 1
  [ "$enabled" = "$SCOPE_ASSET_API" ]
}

# Prints the constraint whose effective policy on the host project denies the
# Asset API and returns SCOPE_PROBE_DENIED; returns SCOPE_PROBE_GRANTED with
# nothing printed when both policies were read and neither denies it,
# SCOPE_PROBE_UNDECIDED when either could not be fetched or, fetched, could
# not be parsed (a constraint that has no policy set reads as not denying). Both service-usage constraints are read, because an
# organisation may still carry the legacy one. Only the enforced `spec` is
# read: a `dryRunSpec` enforces nothing, and an organisation trialling a
# constraint in dry run must not be refused for it. A parse failure is its
# own answer, never "not denied": the reader exits with the probe's three
# statuses, as the IAM probe's does.
_scope_policy_denying_asset_api() {
  local project="$1" constraint policy unread=false rc
  for constraint in $SCOPE_SERVICE_USAGE_CONSTRAINTS; do
    if ! policy="$(trap - ERR; gcloud org-policies describe "$constraint" --project="$project" --effective --format=json 2>/dev/null)"; then
      unread=true
      continue
    fi
    rc=0
    (trap - ERR; printf '%s' "$policy" | python3 -c '
import json, sys
api = sys.argv[1]
denied, not_denied, undecided = (int(a) for a in sys.argv[2:5])
try:
    doc = json.load(sys.stdin) or {}
except Exception:
    sys.exit(undecided)
if not isinstance(doc, dict):
    sys.exit(undecided)
rules = ((doc.get("spec") or {}).get("rules")) or []
for rule in rules:
    if rule.get("denyAll"):
        sys.exit(denied)
    values = rule.get("values") or {}
    if api in (values.get("deniedValues") or []):
        sys.exit(denied)
    allowed = values.get("allowedValues") or []
    if allowed and api not in allowed:
        sys.exit(denied)
sys.exit(not_denied)
' "$SCOPE_ASSET_API" "$SCOPE_PROBE_DENIED" "$SCOPE_PROBE_GRANTED" "$SCOPE_PROBE_UNDECIDED" 2>/dev/null) || rc=$?
    case "$rc" in
      "$SCOPE_PROBE_DENIED")
        printf '%s' "constraints/${constraint}"
        return "$SCOPE_PROBE_DENIED" ;;
      "$SCOPE_PROBE_GRANTED") ;;
      *) unread=true ;;
    esac
  done
  # A constraint that could not be read may still deny: only a full read
  # that found no denial passes.
  $unread && return "$SCOPE_PROBE_UNDECIDED"
  return "$SCOPE_PROBE_GRANTED"
}

# Asks Resource Manager whether the token's identity holds one permission on a
# container, through testIamPermissions, which never mutates and answers for
# the caller alone. $1 folders/<id> or organizations/<id>; $2 the permission;
# $3 the access token. Returns SCOPE_PROBE_GRANTED, _DENIED (a 200 without the
# permission, a 404, or a 403 that is a permission answer: a container the
# caller cannot see cannot be bound) or _UNDECIDED (transport failure, a 403
# for the API being off in the quota project or for a token minted without
# the cloud-platform scope, any other status, an unreadable body), printing
# for an undecided answer the reason, which the caller's warning carries so
# the remedy names the cause rather than a grant.
_scope_container_can_set_iam() {
  local resource="$1" permission="$2" token="$3" response status body rc=0
  # The bearer token goes to curl on its stdin (-H @-), never argv, where any
  # local user could read it from the process table for the request's
  # duration, and never a file, which an interrupted run would leave behind.
  if ! response="$(trap - ERR; printf 'Authorization: Bearer %s\n' "$token" | curl -sS --max-time "$SCOPE_PREFLIGHT_HTTP_TIMEOUT_SECONDS" -X POST \
    -H @- -H "Content-Type: application/json" \
    -d "{\"permissions\":[\"${permission}\"]}" \
    -w $'\n%{http_code}' "${RESOURCE_MANAGER_API_URL}/${resource}:testIamPermissions" 2>/dev/null)"; then
    printf 'the request to Resource Manager did not complete'
    return "$SCOPE_PROBE_UNDECIDED"
  fi
  status="${response##*$'\n'}"
  body="${response%$'\n'*}"
  case "$status" in
    200)
      # The parser exits with the probe's own answer: granted, denied, or
      # undecided for a body that is not the JSON the API documents.
      (trap - ERR; printf '%s' "$body" | python3 -c '
import json, sys
granted, denied, undecided = (int(a) for a in sys.argv[2:5])
try:
    held = (json.load(sys.stdin) or {}).get("permissions") or []
except Exception:
    sys.exit(undecided)
sys.exit(granted if sys.argv[1] in held else denied)
' "$permission" "$SCOPE_PROBE_GRANTED" "$SCOPE_PROBE_DENIED" "$SCOPE_PROBE_UNDECIDED" 2>/dev/null) || rc=$?
      [ "$rc" -ne "$SCOPE_PROBE_UNDECIDED" ] || printf 'Resource Manager answered 200 with a body that is not the JSON it documents'
      return "$rc" ;;
    403)
      # Two 403s are not permission answers: a token minted without the
      # cloud-platform scope (the remedy is the token's scope, not a grant),
      # and the Resource Manager API being off in the credential's quota
      # project (the apply enables that API before it binds).
      if printf '%s' "$body" | grep -qE "$SCOPE_PROBE_TOKEN_SCOPE_PATTERN"; then
        printf 'the token lacks the cloud-platform scope; mint it with that scope, as gcloud does'
        return "$SCOPE_PROBE_UNDECIDED"
      fi
      if printf '%s' "$body" | grep -qE "$SCOPE_PROBE_NOT_A_PERMISSION_ANSWER_PATTERN"; then
        printf 'Resource Manager answered 403 for its API being off in the credentials'"'"' quota project, which the apply enables before it binds'
        return "$SCOPE_PROBE_UNDECIDED"
      fi
      return "$SCOPE_PROBE_DENIED" ;;
    404) return "$SCOPE_PROBE_DENIED" ;;
    *)
      printf 'Resource Manager answered HTTP %s' "$status"
      return "$SCOPE_PROBE_UNDECIDED" ;;
  esac
}

# The check above could not decide. A refusal, except under "warn" (a plan
# applies nothing), where it is a warning and the run goes on.
_scope_check_failed() {
  local mode="$1" reason="$2"
  if [ "$mode" = "$SCOPE_CHECK_MODE_WARN" ]; then
    print_warning "The scope check did not run: ${reason}. The full upgrade runs it and refuses if it cannot."
    return 0
  fi
  print_error "Refusing to apply: ${reason}."
  print_info "The apply would render spec.scope from install.env over whatever the PlatformAgent holds, and a scope it holds that install.env does not declare would be replaced. Fix the read, or record the live declaration in install.env, and re-run."
  return 1
}

# Helm never touches the crds/ directory on upgrade -- that is Helm's own
# documented behaviour, and the Terraform helm provider inherits it -- so CRD
# schema changes are applied here first, by every front door that re-applies
# the chart to a cluster that already runs it: upgrade.sh's full and operator
# modes, an install.sh re-run and the Day-2 menu (INSTALL.md names the last
# two as the way to change configuration). A field the served CRD lacks is
# pruned from the CR on write, and pruned for good on this path: the release
# record then carries the value, the next render equals the record, and Helm
# sends only the difference between its rendered manifests. Server-side apply,
# because these objects are large and have had several owners; through the
# install's own context by name, never the current one. $1 is the checkout.
apply_crd_upgrades() {
  local repo_dir="${1:?repository directory}"
  print_info "Applying CRD updates from charts/kube-agents/crds..."
  kubectl --context "$(gke_context_name)" apply --server-side --force-conflicts \
    -f "${repo_dir}/charts/kube-agents/crds/" >/dev/null
}

# The raw state object, as this install keeps it in GCS. Read straight from
# the bucket rather than through terraform: it is cheaper and earlier than an
# init, it works from a fresh clone, and it runs BEFORE terraform.tfvars exists
# -- the answers it gives are inputs to that file. Returns 1 with no output
# when there is no state, and TF_STATE_RC_UNREADABLE (with a warning) when it
# could not look: a transient GCS error read as "no state" is how an install
# gets retargeted, or told to delete its own service account.
tf_state_read() {
  # Always called as `$(tf_state_read)`, so this runs in a subshell that
  # inherits the front doors' ERR trap (set -E). The `return 1` below for a
  # missing object -- the ordinary fresh-install answer -- would fire that
  # trap in here, printing an abort banner and writing a FAILED report from a
  # probe whose miss is expected, while the caller's `|| return` carries on
  # regardless. Dropping the trap affects this subshell only.
  trap - ERR
  local project="${1:-${PROJECT_ID:-}}"
  local cluster="${2:-${CLUSTER_NAME:-}}"
  if [ -z "$project" ] || [ -z "$cluster" ]; then
    return 1
  fi
  local object state err_file err
  object="gs://$(tf_state_bucket "$project")/$(tf_state_prefix "$cluster")/${TF_STATE_OBJECT}"
  err_file="$(mktemp)"
  if ! state="$(gcloud storage cat "$object" 2>"$err_file")"; then
    err="$(cat "$err_file" 2>/dev/null)"
    rm -f "$err_file"
    if printf '%s' "$err" | grep -qiE "$GCS_OBJECT_ABSENT_PATTERN"; then
      return 1
    fi
    # >&2 because the front doors' print_warning writes to stdout, and this
    # function's stdout is the state the caller is capturing.
    print_warning "Could not read the Terraform state at ${object}: ${err:-unknown gcloud failure}. Proceeding as though this install has none; lifecycle.sh refuses the apply if that turns out to be wrong." >&2
    return "$TF_STATE_RC_UNREADABLE"
  fi
  rm -f "$err_file"
  printf '%s' "$state"
}

# Whether this install's Terraform state already MANAGES this cluster, which
# is what decides create_cluster. Three things have to hold, and each one has
# misled the installer on its own:
#
#   - the entry is managed, not a data source. An existing-cluster install
#     records a data-mode "google_container_cluster" in the same state, and
#     matching on type alone flipped such an install's create_cluster back to
#     true on every re-run -- planning a second cluster over the real one;
#   - it has an instance. An apply that died before the create finished leaves
#     a managed entry with an empty instances list, which manages nothing;
#   - the instance IS this cluster: project, location and name all match. State
#     accretes across runs that computed different answers (#1296), and an
#     entry that names some other cluster is not ownership of this one.
#
# Parsed, not grepped, for the reasons above and one more: a `gcloud | grep -q`
# pipeline under pipefail can report a cluster that IS in state as absent when
# grep exits before gcloud finishes writing (the same trap lifecycle.sh
# documents for its own state reads).
tf_state_has_cluster() {
  local state
  state=$(tf_state_read) || return 1
  printf '%s' "$state" | python3 -c '
import json, sys
project, location, name = sys.argv[1:4]
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(1)
wanted_id = f"projects/{project}/locations/{location}/clusters/{name}"
def is_this_cluster(attrs):
    if not isinstance(attrs, dict):
        return False
    if attrs.get("id") == wanted_id:
        return True
    return (attrs.get("project"), attrs.get("location"), attrs.get("name")) == (project, location, name)
managed = any(
    r.get("type") == "google_container_cluster"
    and r.get("mode") == "managed"
    and any(is_this_cluster(i.get("attributes")) for i in r.get("instances", []))
    for r in doc.get("resources", [])
)
sys.exit(0 if managed else 1)
' "${PROJECT_ID}" "${REGION}" "${CLUSTER_NAME}"
}

# Whether this install's Terraform state manages a root-module resource of
# type $1 and name $2 -- managed mode, with at least one instance, for the
# reasons tf_state_has_cluster gives. Returns 1 when it does not, and
# TF_STATE_RC_UNREADABLE when the state could not be read or parsed: for both
# callers "not ours" is the direction that destroys something (the
# composition's cert-manager, a Helm release), so an unreadable state must
# not read as it.
tf_state_manages_resource() {
  local state
  state=$(tf_state_read) || return $?
  printf '%s' "$state" | python3 -c '
import json, sys
rtype, rname, unreadable = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(unreadable)
managed = any(
    r.get("type") == rtype
    and r.get("name") == rname
    and r.get("mode") == "managed"
    and not r.get("module")
    and len(r.get("instances", [])) > 0
    for r in doc.get("resources", [])
)
sys.exit(0 if managed else 1)
' "$1" "$2" "$TF_STATE_RC_UNREADABLE"
}

# The account ids of every google_service_account this install's state
# manages, one per line. Empty when there is no state; tf_state_read's return
# code when there is none or it could not be read, so a caller can tell
# "manages no accounts" from "could not look". A state that downloaded but
# does not parse is the second kind, not the first.
tf_state_service_account_ids() {
  # Called as `$(tf_state_service_account_ids)`, so the same subshell/ERR-trap
  # argument as tf_state_read applies to the non-zero return below.
  trap - ERR
  local state
  state=$(tf_state_read) || return $?
  printf '%s' "$state" | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(int(sys.argv[1]))
for r in doc.get("resources", []):
    if r.get("type") != "google_service_account" or r.get("mode") != "managed":
        continue
    for i in r.get("instances", []):
        account_id = (i.get("attributes") or {}).get("account_id")
        if account_id:
            print(account_id)
' "$TF_STATE_RC_UNREADABLE"
}

# The name of the Google Chat Pub/Sub subscription this install's state
# manages. Empty when there is no state or the subscription is not in state;
# tf_state_read's return code when the state could not be read or parsed.
# shellcheck disable=SC2120
tf_state_chat_subscription_name() {
  trap - ERR
  local state
  state=$(tf_state_read "$@") || return $?
  printf '%s' "$state" | python3 -c '
import json, sys
rtype, rname, rmode, unreadable = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(unreadable)
for r in doc.get("resources", []):
    if r.get("type") == rtype and r.get("name") == rname and r.get("mode") == rmode:
        for i in r.get("instances", []):
            name = (i.get("attributes") or {}).get("name")
            if name:
                print(name)
                sys.exit(0)
sys.exit(0)
' "$TF_PUBSUB_SUBSCRIPTION_TYPE" "$TF_CHAT_EVENTS_SUBSCRIPTION_NAME" "$TF_RESOURCE_MODE_MANAGED" "$TF_STATE_RC_UNREADABLE"
}

# Refuse an apply that would stop on a service account this install does not
# own. Every GSA the composition creates has ONE fixed name per project while
# state is kept per cluster, so the second install in a project -- or a
# re-install after a teardown that never ran uninstall.sh -- reaches the apply
# with an account that exists in GCP and not in its state, and Terraform 409s
# on it halfway through (#1294). Caught here, before anything is applied, the
# failure names the key that un-collides it.
#
# Refused rather than adopted, deliberately. An import cannot tell a leftover
# from another live install's identity, and adopting the second puts that
# account into THIS install's state -- where this install's uninstall then
# deletes it out from under the other one, with no message on that side. A
# 409 is loud; that is silent. Hence the message, and the key to set.
#
# Reads the generator's answers (TFVARS_ENABLE_GITHUB_MINTER) and the install
# coordinates from the environment, so call it after write_tfvars_from_state,
# and from every door that applies -- install.sh, its Day-2 menu, and
# upgrade.sh, since a minter or Vertex GSA is first planned on whichever of
# them enables the feature. A describe that fails for any reason other than
# absence -- no permission to read IAM, say -- counts as absent here and lets
# the apply report it, which is no worse than today. State that cannot be READ
# is different: "not in state" then means nothing, and the remedy below would
# tell a healthy install to delete its own account, so the check stands down
# and says so. Caller defines print_error / print_info / print_warning.
check_service_account_ownership() {
  local in_state line label key account_id email state_rc=0 agent_id marker members
  local -a candidates=() foreign=()
  agent_id="${PLATFORM_AGENT_GSA_NAME:-$DEFAULT_PLATFORM_AGENT_GSA_NAME}"
  candidates+=("the agent	PLATFORM_AGENT_GSA_NAME	${agent_id}")
  if [ "${TFVARS_ENABLE_GITHUB_MINTER:-false}" = "true" ]; then
    candidates+=("the GitHub token minter	GITHUB_MINTER_GSA_NAME	${GITHUB_MINTER_GSA_NAME:-$DEFAULT_GITHUB_MINTER_GSA_NAME}")
  fi
  if [ "${MODEL_PROVIDER:-$DEFAULT_MODEL_PROVIDER}" = "vertex_ai" ]; then
    candidates+=("the LiteLLM gateway	LITELLM_GSA_NAME	${LITELLM_GSA_NAME:-$DEFAULT_LITELLM_GSA_NAME}")
  fi
  in_state="$(tf_state_service_account_ids)" || state_rc=$?
  if [ "$state_rc" -eq "$TF_STATE_RC_UNREADABLE" ]; then
    print_warning "Skipping the service-account ownership check: this install's Terraform state could not be read (see above), so nothing can be said about which accounts it owns. If the apply stops on a 409 creating a service account, re-run once the state is readable."
    return 0
  fi
  for line in "${candidates[@]}"; do
    IFS=$'\t' read -r label key account_id <<<"$line"
    if printf '%s\n' "$in_state" | grep -Fxq "$account_id"; then
      continue
    fi
    email="${account_id}@${PROJECT_ID}.iam.gserviceaccount.com"
    # `trap - ERR` for the bash 3.2 reason write_tfvars_from_state gives: the
    # miss is the ordinary fresh-install answer, not an abort.
    if (trap - ERR; gcloud iam service-accounts describe "$email" --project "${PROJECT_ID}" >/dev/null 2>&1); then
      foreign+=("${label}	${key}	${account_id}	${email}")
    fi
  done
  # The scoped pool's members (scoped_pool.tf) are derived from the agent's id
  # and cannot be enumerated from the installer's inputs, so they are discovered
  # from GCP by the marker their description carries. Checked whether or not
  # the pool is armed: a leftover member with this install's marker is the same
  # lost-state case as a leftover agent account and 409s the moment the pool is
  # armed, so it is listed in the same refusal. A list that fails (no
  # permission, API off) counts as no members, as the describe's miss does.
  # shellcheck disable=SC2059 # the format is the named constant
  marker="$(printf "$SCOPED_SA_POOL_MEMBER_MARKER_FORMAT" "$agent_id")"
  members="$(trap - ERR; gcloud iam service-accounts list --project "${PROJECT_ID}" \
    --filter="email:${SCOPED_SA_POOL_ACCOUNT_ID_PREFIX}* AND description:\"${marker}\"" \
    --format="value(email)" 2>/dev/null)" || members=""
  while IFS= read -r email; do
    [ -n "$email" ] || continue
    account_id="${email%%@*}"
    if printf '%s\n' "$in_state" | grep -Fxq "$account_id"; then
      continue
    fi
    # Keyed on PLATFORM_AGENT_GSA_NAME: members derive their ids from the
    # agent's, so renaming the agent is what un-collides them.
    foreign+=("a scoped pool member	PLATFORM_AGENT_GSA_NAME	${account_id}	${email}")
  done <<<"$members"
  [ "${#foreign[@]}" -eq 0 ] && return 0

  for line in ${foreign[@]+"${foreign[@]}"}; do
    IFS=$'\t' read -r label key account_id email <<<"$line"
    print_error "Service account '${account_id}' (${label}) already exists in project '${PROJECT_ID}' and is not managed by this install's Terraform state (gs://$(tf_state_bucket)/$(tf_state_prefix))."
    print_info "Applying would stop on a 409 creating it. If another kube-agents install in this project owns it, give this install its own name: set ${key}=<a-different-name> in install.env, or on a first install, which has no install.env yet, run ${key}=<a-different-name> ./install.sh ... and the name is recorded in the install.env it creates. If it is left over from an install removed without uninstall.sh, delete it first: gcloud iam service-accounts delete ${email} --project ${PROJECT_ID}"
  done
  return 1
}

# The image tag this install's Terraform state RECORDS, which is not the tag
# the cluster is serving: the redeploy workflows move the running tag with
# `helm upgrade` and never run Terraform. A drift plan needs this one, or every
# out-of-band redeploy reads as a pending change to helm_release.kube_agents.
#
# Read from the state object in GCS rather than through `terraform output`,
# for the reason tf_state_has_cluster gives and one more: this runs BEFORE
# terraform.tfvars is generated -- the tag is an input to that file -- and
# lifecycle.sh's backend setup needs tfvars to resolve the bucket. The
# coordinates here come from the environment instead, so there is no such loop.
#
# Empty on every failure, including the ordinary one: state written before the
# composition had this output carries no value for it. Callers fall back.
tf_state_image_tag() {
  local state
  state=$(tf_state_read) || return 0
  printf '%s' "$state" | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(0)
value = (doc.get("outputs", {}).get("image_tag") or {}).get("value")
if isinstance(value, str) and value:
    sys.stdout.write(value)
'
}

# The image tag an install is currently serving, read off the agent Deployment.
#
# The Deployment rather than the Helm release: `helm get values` reports what
# the last upgrade was ASKED for, and on these environments the last upgrade was
# a `--reset-then-reuse-values` re-tag whose recorded values are the install-day
# blob. The Deployment reports what is running.
#
# Selected by name, not by index. The operator builds this list and appends
# the dashboard and fluent-bit after the agent, so a positional read is one
# reordering away from pinning the composition's image_tag to a sidecar's
# version — on a scheduled apply, silently.
running_image_tag() {
  local namespace="${1:-$DEFAULT_NAMESPACE}" image=""
  command -v kubectl >/dev/null 2>&1 || return 0
  if ! image="$(trap - ERR; kubectl get deployment "${PLATFORM_AGENT_DEPLOYMENT}" -n "${namespace}" \
    -o jsonpath='{.spec.template.spec.containers[?(@.name=="platform-agent")].image}' 2>/dev/null)"; then
    return 0
  fi
  [ -n "${image}" ] || return 0
  # Everything after the last colon, unless that colon belongs to a registry
  # port (no slash may follow it).
  case "${image##*:}" in
    */*) return 0 ;;
    *) printf '%s\n' "${image##*:}" ;;
  esac
}

# Returns the status of a Helm release (e.g., 'deployed', 'pending-upgrade', etc.),
# or empty string if the release does not exist or helm is not installed.
helm_release_status() {
  local release_name="${1:-$KUBE_AGENTS_HELM_RELEASE}"
  local namespace="${2:-$DEFAULT_NAMESPACE}"

  command -v helm >/dev/null 2>&1 || return 0

  local status_json
  # `trap - ERR` inside the substitution: the front doors run `set -E`, so
  # this subshell inherits their ERR trap, and in here `helm status` is a
  # bare failing command the outer `if !` cannot shield. On bash 3.2 (macOS's
  # default) the trap fires in the subshell: abort banner, FAILED report,
  # then the caller carries on. A missing release is the ordinary
  # first-install answer, not an abort. The front doors' own handlers exit
  # a subshell silently, so a probe there needs no guard; this library
  # cannot know its caller's trap, so its tolerated probes guard themselves.
  # scripts/installer/README.md states the rule.
  if ! status_json="$(trap - ERR; helm status "${release_name}" -n "${namespace}" -o json 2>/dev/null)"; then
    return 0
  fi

  if command -v jq >/dev/null 2>&1; then
    printf '%s' "${status_json}" | jq -r '.info.status // empty'
  else
    printf '%s' "${status_json}" | sed -n 's/.*"status": *"\([^"]*\)".*/\1/p' | head -n 1
  fi
}

# Parses an RFC3339 / ISO-8601 UTC timestamp (e.g. 2026-09-04T12:00:00Z) into
# Unix epoch seconds portably across GNU date (Linux) and BSD date (macOS).
parse_rfc3339_epoch() {
  local ts="${1:-}"
  [ -n "${ts}" ] || return 1

  # 1. GNU date (-d) on Linux
  date -d "${ts}" +%s 2>/dev/null && return 0

  # 2. BSD date (-j -f) on macOS
  date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "${ts}" +%s 2>/dev/null && return 0
  date -u -j -f "%Y-%m-%dT%H:%M:%S" "${ts%Z}" +%s 2>/dev/null && return 0

  return 1
}

# Checks whether a Helm release is stuck in a pending-* state (pending-install,
# pending-upgrade, pending-rollback), waits out live in-flight operations, and
# recovers from wedged releases by rolling back to the last successfully
# deployed revision. Never automatically uninstalls an existing release unless
# explicitly permitted via ALLOW_UNINSTALL_PENDING_RELEASE=true, protecting
# credentials and secrets (such as platform-agent-secrets).
ensure_clean_helm_release() {
  local release_name="${1:-$KUBE_AGENTS_HELM_RELEASE}"
  local namespace="${2:-$DEFAULT_NAMESPACE}"

  command -v helm >/dev/null 2>&1 || return 0

  local release_status
  release_status="$(helm_release_status "${release_name}" "${namespace}")"
  [ -n "${release_status}" ] || return 0

  # Only pending-* states represent locks or in-flight operations
  case "${release_status}" in
    pending-install|pending-upgrade|pending-rollback)
      ;;
    *)
      return 0
      ;;
  esac

  # Determine if this operation was started recently and could be an active,
  # in-flight deployment rather than a wedged zombie lock.
  # Helm's standard release timeout across deploy workflows is 10m (600s).
  local operation_timeout="${HELM_OPERATION_TIMEOUT:-$HELM_OPERATION_TIMEOUT_DEFAULT}"
  local pending_age=""
  local created_epoch=""
  if command -v kubectl >/dev/null 2>&1; then
    local creation_ts
    creation_ts="$(kubectl get secret -n "${namespace}" -l "owner=helm,name=${release_name},status=${release_status}" --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1].metadata.creationTimestamp}' 2>/dev/null || true)"
    if [ -n "${creation_ts}" ]; then
      created_epoch="$(parse_rfc3339_epoch "${creation_ts}" 2>/dev/null || echo "")"
      if [ -n "${created_epoch}" ] && [ "${created_epoch}" -gt 0 ]; then
        local now_epoch
        now_epoch="$(date +%s)"
        pending_age=$(( now_epoch - created_epoch ))
      else
        if type print_error >/dev/null 2>&1; then
          print_error "Failed to parse creation timestamp '${creation_ts}' for Helm release '${release_name}' in namespace '${namespace}'"
        else
          echo "❌ ERROR: Failed to parse creation timestamp '${creation_ts}' for Helm release '${release_name}' in namespace '${namespace}'" >&2
        fi
        return 1
      fi
    fi
  fi

  # Calculate wait budget if an in-flight operation might still be running
  local wait_budget=0
  if [ -n "${HELM_LOCK_WAIT_TIMEOUT:-}" ]; then
    wait_budget="${HELM_LOCK_WAIT_TIMEOUT}"
  elif [ -n "${pending_age}" ] && [ "${pending_age}" -ge 0 ] && [ "${pending_age}" -lt "${operation_timeout}" ]; then
    wait_budget=$(( operation_timeout - pending_age ))
    if [ -n "${HELM_PENDING_WAIT_MAX:-}" ] && [ "${wait_budget}" -gt "${HELM_PENDING_WAIT_MAX}" ]; then
      wait_budget="${HELM_PENDING_WAIT_MAX}"
    fi
  fi

  if [ "${wait_budget}" -gt 0 ]; then
    local age_msg=""
    if [ -n "${pending_age}" ]; then
      age_msg=" (started ${pending_age}s ago)"
    fi
    if type print_warning >/dev/null 2>&1; then
      print_warning "Helm release '${release_name}' in namespace '${namespace}' is in '${release_status}'${age_msg}. Waiting up to ${wait_budget}s for in-flight operation to complete..."
    else
      echo "⚠️ WARNING: Helm release '${release_name}' in namespace '${namespace}' is in '${release_status}'${age_msg}. Waiting up to ${wait_budget}s for in-flight operation to complete..." >&2
    fi

    local deadline=$(( SECONDS + wait_budget ))
    local poll_interval="${HELM_LOCK_POLL_INTERVAL:-$HELM_LOCK_POLL_INTERVAL_DEFAULT}"
    while [ "${SECONDS}" -lt "${deadline}" ]; do
      sleep "${poll_interval}"
      release_status="$(helm_release_status "${release_name}" "${namespace}")"
      if [ "${release_status}" = "deployed" ]; then
        if type print_success >/dev/null 2>&1; then
          print_success "In-flight Helm operation completed successfully (status: deployed)."
        else
          echo "✅ In-flight Helm operation completed successfully (status: deployed)." >&2
        fi
        return 0
      elif [ "${release_status}" = "failed" ] || [ -z "${release_status}" ]; then
        if type print_info >/dev/null 2>&1; then
          print_info "In-flight Helm operation finished (status: ${release_status:-cleared}). Lock released."
        else
          echo "ℹ️ In-flight Helm operation finished (status: ${release_status:-cleared}). Lock released." >&2
        fi
        return 0
      fi
    done

    # If the wait timed out and the operation is still in a pending state:
    # verify whether the full operation_timeout has actually elapsed.
    # Never attempt recovery (rollback or uninstall) on an operation that is
    # still within its legitimate operation_timeout budget!
    now_epoch="$(date +%s)"
    if [ -n "${created_epoch:-}" ] && [ $(( now_epoch - created_epoch )) -lt "${operation_timeout}" ]; then
      if type print_error >/dev/null 2>&1; then
        print_error "Timed out waiting for in-flight Helm operation (${wait_budget}s), but release '${release_name}' is still within its ${operation_timeout}s timeout window. Refusing to recover active operation."
      else
        echo "❌ ERROR: Timed out waiting for in-flight Helm operation (${wait_budget}s), but release '${release_name}' is still within its ${operation_timeout}s timeout window. Refusing to recover active operation." >&2
      fi
      return 1
    fi

    if type print_warning >/dev/null 2>&1; then
      print_warning "Timed out waiting for in-flight Helm operation. Release '${release_name}' exceeded ${operation_timeout}s operation timeout and remains in '${release_status}'."
    else
      echo "⚠️ WARNING: Timed out waiting for in-flight Helm operation. Release '${release_name}' exceeded ${operation_timeout}s operation timeout and remains in '${release_status}'." >&2
    fi
  fi

  case "${release_status}" in
    pending-install)
      if [ "${ALLOW_UNINSTALL_PENDING_RELEASE:-false}" = "true" ]; then
        if type print_warning >/dev/null 2>&1; then
          print_warning "Helm release '${release_name}' in namespace '${namespace}' is stuck in 'pending-install'. ALLOW_UNINSTALL_PENDING_RELEASE=true: clearing failed initial release..."
        else
          echo "⚠️ WARNING: Helm release '${release_name}' in namespace '${namespace}' is stuck in 'pending-install'. ALLOW_UNINSTALL_PENDING_RELEASE=true: clearing failed initial release..." >&2
        fi

        if helm uninstall "${release_name}" -n "${namespace}" --wait; then
          HELM_RELEASE_REPAIRED="true"
          if type print_success >/dev/null 2>&1; then
            print_success "Successfully cleaned up stuck pending-install release '${release_name}'."
          else
            echo "✅ Successfully cleaned up stuck pending-install release '${release_name}'." >&2
          fi
          return 0
        else
          if type print_error >/dev/null 2>&1; then
            print_error "Failed to uninstall stuck pending-install release '${release_name}'."
          else
            echo "❌ ERROR: Failed to uninstall stuck pending-install release '${release_name}'." >&2
          fi
          return 1
        fi
      else
        if type print_error >/dev/null 2>&1; then
          print_error "Helm release '${release_name}' in namespace '${namespace}' is stuck in 'pending-install'. Automatic uninstall is blocked to prevent data and secret loss. Set ALLOW_UNINSTALL_PENDING_RELEASE=true to permit uninstall."
        else
          echo "❌ ERROR: Helm release '${release_name}' in namespace '${namespace}' is stuck in 'pending-install'. Automatic uninstall is blocked to prevent data and secret loss. Set ALLOW_UNINSTALL_PENDING_RELEASE=true to permit uninstall." >&2
        fi
        return 1
      fi
      ;;
    pending-upgrade|pending-rollback)
      if type print_warning >/dev/null 2>&1; then
        print_warning "Helm release '${release_name}' in namespace '${namespace}' is stuck in '${release_status}'. Attempting automatic rollback..."
      else
        echo "⚠️ WARNING: Helm release '${release_name}' in namespace '${namespace}' is stuck in '${release_status}'. Attempting automatic rollback..." >&2
      fi

      local history_json
      if ! history_json="$(trap - ERR; helm history "${release_name}" -n "${namespace}" -o json 2>/dev/null)"; then
        if type print_error >/dev/null 2>&1; then
          print_error "Failed to retrieve Helm history for release '${release_name}' in namespace '${namespace}'."
        else
          echo "❌ ERROR: Failed to retrieve Helm history for release '${release_name}' in namespace '${namespace}'." >&2
        fi
        return 1
      fi

      local last_good_rev=""
      if command -v jq >/dev/null 2>&1; then
        last_good_rev="$(trap - ERR; printf '%s' "${history_json}" | jq -r '[.[] | select(.status == "deployed" or .status == "superseded") | .revision] | max // empty' 2>/dev/null)" || last_good_rev=""
      fi

      if [ -n "${last_good_rev}" ]; then
        echo "==> Rolling back '${release_name}' to revision ${last_good_rev}..." >&2
        if helm rollback "${release_name}" "${last_good_rev}" -n "${namespace}" --wait --timeout "${HELM_ROLLBACK_TIMEOUT:-$HELM_ROLLBACK_TIMEOUT_DEFAULT}"; then
          HELM_RELEASE_REPAIRED="true"
          if type print_success >/dev/null 2>&1; then
            print_success "Successfully rolled back Helm release '${release_name}' to revision ${last_good_rev}."
          else
            echo "✅ Successfully rolled back Helm release '${release_name}' to revision ${last_good_rev}." >&2
          fi
          return 0
        else
          if type print_error >/dev/null 2>&1; then
            print_error "Failed to roll back Helm release '${release_name}' to revision ${last_good_rev}."
          else
            echo "❌ ERROR: Failed to roll back Helm release '${release_name}' to revision ${last_good_rev}." >&2
          fi
          return 1
        fi
      else
        if [ "${ALLOW_UNINSTALL_PENDING_RELEASE:-false}" = "true" ]; then
          if type print_warning >/dev/null 2>&1; then
            print_warning "Helm release '${release_name}' is in '${release_status}', but no previous deployed revision exists in history. ALLOW_UNINSTALL_PENDING_RELEASE=true: uninstalling to recover..."
          else
            echo "⚠️ WARNING: Helm release '${release_name}' is in '${release_status}', but no previous deployed revision exists in history. ALLOW_UNINSTALL_PENDING_RELEASE=true: uninstalling to recover..." >&2
          fi
          helm uninstall "${release_name}" -n "${namespace}" --wait
          # shellcheck disable=SC2034 # Read by upgrade.sh's restore_moved_checkout.
          HELM_RELEASE_REPAIRED="true"
          return 0
        else
          if type print_error >/dev/null 2>&1; then
            print_error "Helm release '${release_name}' is in '${release_status}', but no previous deployed revision exists in history. Automatic uninstall is blocked to prevent credential and secret destruction (${PLATFORM_AGENT_SECRET}). Set ALLOW_UNINSTALL_PENDING_RELEASE=true to permit uninstall."
          else
            echo "❌ ERROR: Helm release '${release_name}' is in '${release_status}', but no previous deployed revision exists in history. Automatic uninstall is blocked to prevent credential and secret destruction (${PLATFORM_AGENT_SECRET}). Set ALLOW_UNINSTALL_PENDING_RELEASE=true to permit uninstall." >&2
          fi
          return 1
        fi
      fi
      ;;
    *)
      return 0
      ;;
  esac
}

# The kubeconfig context name `gcloud container clusters get-credentials`
# writes for this install's cluster. Every kubectl read in this file that
# could touch another cluster checks the current context against it first.
gke_context_name() { printf 'gke_%s_%s_%s' "$PROJECT_ID" "$REGION" "$CLUSTER_NAME"; }

# Whether this install's cluster is running the Hindsight memory store.
#
# Sets LIVE_HINDSIGHT_STATE to one of:
#   present  -- one of the two objects is there, so the store is deployed
#   absent   -- the API server answered NotFound for BOTH, so it is not
#   unknown  -- nobody could be asked, and LIVE_HINDSIGHT_REASON says why
#
# The third answer is the point. `kubectl get … || true` collapses a timeout, a
# 403, a missing auth plugin and a stale context into the same verdict as a
# genuine NotFound -- and the caller turns that verdict into
# `memory_provider = "multiuser_memory"`, which plans the destruction of the
# hindsight-api Deployment and the hindsight-postgresql StatefulSet holding the
# database. The cluster-existence probe in write_tfvars_from_state already
# refuses to guess for exactly this reason ("a wrong guess can plan a live
# cluster's replacement"); this probe decides the same kind of question and
# answers it the same way.
#
# Assigns rather than echoes, as gke_dns_endpoint_flag and
# recorded_plugin_image_tag_keys do: the reason has to reach the caller
# alongside the verdict, and a command substitution would run this in a
# subshell that discards it.
LIVE_HINDSIGHT_STATE=""
LIVE_HINDSIGHT_REASON=""
live_hindsight_state() {
  local namespace="${1:-${NAMESPACE:-$DEFAULT_NAMESPACE}}"
  LIVE_HINDSIGHT_STATE="unknown"
  LIVE_HINDSIGHT_REASON=""

  if ! command -v kubectl >/dev/null 2>&1; then
    LIVE_HINDSIGHT_REASON="kubectl is not installed"
    return 0
  fi
  # The same context gate the credential recovery uses. A stale context would
  # answer about somebody else's cluster, and "Hindsight is not deployed over
  # there" is not an answer about this install.
  local expected_ctx current_ctx
  expected_ctx="$(gke_context_name)"
  current_ctx="$(kubectl config current-context 2>/dev/null || true)"
  if [ "$current_ctx" != "$expected_ctx" ]; then
    LIVE_HINDSIGHT_REASON="kubectl's current context is '${current_ctx:-none}', not this cluster's ('${expected_ctx}')"
    return 0
  fi

  # Either object proves the store is there; it takes a definite "no" on both to
  # prove it is not. `${spec%% *}` / `${spec#* }` rather than `set --`, which
  # would clobber this function's own positional parameters.
  local spec kind name out rc absent=0
  for spec in "statefulset ${HINDSIGHT_STATEFULSET}" "deployment ${HINDSIGHT_API_DEPLOYMENT}"; do
    kind="${spec%% *}"
    name="${spec#* }"
    rc=0
    # --ignore-not-found is what makes the answer readable without guessing at
    # prose: the API server having answered "no such object" becomes exit 0 with
    # no output, which nothing else produces. Scoring absence by grepping stderr
    # for "not found" instead is what this function was written to avoid and
    # still got wrong once: kubectl reports a missing exec-credential plugin as
    # `exec: executable gke-gcloud-auth-plugin not found`, which matched, and a
    # host that cannot authenticate to the cluster at all was read as a cluster
    # with no Hindsight on it -- silently, in the arm that takes the default.
    #
    # stdout and stderr are merged because only one of them can matter per
    # outcome, and the present/absent split is decided by looking for the
    # object's own name in `-o name` output rather than by the string being
    # non-empty, so a server-sent `Warning:` line cannot read as an object.
    #
    # `trap - ERR` inside the substitution for the bash 3.2 reason the probes
    # above give -- a non-zero exit here is the tested condition, not an abort.
    out="$({ trap - ERR; kubectl get "$kind" "$name" -n "$namespace" --context "$expected_ctx" \
      --request-timeout="${KUBECTL_PROBE_REQUEST_TIMEOUT}" --ignore-not-found -o name; } 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ]; then
      if printf '%s\n' "$out" | grep -qE "(^|/)${name}\$"; then
        LIVE_HINDSIGHT_STATE="present"
        return 0
      fi
      absent=$((absent + 1))
      continue
    fi
    # The API server's own phrasing, and only that. It is the one NotFound this
    # function is entitled to believe -- it can only come from a server that
    # answered -- and it still arrives on older kubectl builds, and for a
    # namespace that does not exist, where --ignore-not-found does not apply.
    if printf '%s' "$out" | grep -qF 'Error from server (NotFound)'; then
      absent=$((absent + 1))
      continue
    fi
    LIVE_HINDSIGHT_REASON="kubectl could not read ${kind}/${name} in namespace '${namespace}': ${out:-unknown kubectl failure}"
    return 0
  done
  if [ "$absent" -eq 2 ]; then
    LIVE_HINDSIGHT_STATE="absent"
  fi
  return 0
}

# Clears the one Helm leftover a first apply's failure leaves that no retry
# can get past: the kube-agents release in `failed`, with no revision that
# ever served, and absent from Terraform state because the provider never
# recorded the create it lost (a webhook the fresh cluster could not reach
# yet, a pod that never came up). Helm then answers the retry's create with
# "cannot re-use a name that is still in use", and the identical command
# that #1296 says must work fails for a new reason.
#
# Narrower than ensure_clean_helm_release on purpose. That function protects a
# release that once served -- its Secret may be the only copy of the
# credentials -- and asks for ALLOW_UNINSTALL_PENDING_RELEASE. Nothing here
# served: a release with a deployed or superseded revision in its history is
# left alone, and so is one the state manages, which Terraform will upgrade or
# replace itself. On install.sh's path the credentials the release's Secret
# would hold are already in terraform.tfvars (the generator recovered them
# before this runs), so the re-create writes the same values back.
clear_failed_initial_helm_release() {
  local release_name="${1:-$KUBE_AGENTS_HELM_RELEASE}"
  local namespace="${2:-$DEFAULT_NAMESPACE}"
  command -v helm >/dev/null 2>&1 || return 0

  # Only when kubectl's current context is this install's cluster, the same
  # gate the generator's credential recovery uses: an uninstall is the one
  # destructive step here, and a stale context would point it at whatever
  # cluster the operator last looked at.
  local expected_ctx
  expected_ctx="$(gke_context_name)"
  if ! command -v kubectl >/dev/null 2>&1 ||
    [ "$(kubectl config current-context 2>/dev/null || true)" != "$expected_ctx" ]; then
    print_info "Skipping the failed-release check: kubectl's current context is not this cluster's (${expected_ctx})."
    return 0
  fi

  local release_status
  release_status="$(helm_release_status "${release_name}" "${namespace}")"
  case "${release_status}" in
    "$HELM_STATUS_FAILED") ;;
    "$HELM_STATUS_PENDING_INSTALL")
      # Helm refuses the name in this state too, but an install running
      # right now looks the same from here as one that was interrupted, so
      # the operator decides.
      print_warning "Helm release '${release_name}' in namespace '${namespace}' is '${release_status}': a first install of it is either still running or was interrupted, and Helm refuses the name either way. Leaving it. If no other install is running, clear it and re-run: helm uninstall ${release_name} -n ${namespace}"
      return 0
      ;;
    *) return 0 ;;
  esac

  local state_rc=0
  tf_state_manages_resource "$TF_HELM_RELEASE_TYPE" "$TF_KUBE_AGENTS_RELEASE_NAME" || state_rc=$?
  if [ "$state_rc" -eq 0 ]; then
    print_info "Helm release '${release_name}' in namespace '${namespace}' is '${release_status}' and in this install's Terraform state; the apply reconciles it."
    return 0
  fi
  if [ "$state_rc" -eq "$TF_STATE_RC_UNREADABLE" ]; then
    print_warning "Helm release '${release_name}' in namespace '${namespace}' is '${release_status}', and whether this install's Terraform state owns it could not be decided because the state could not be read (see above); leaving it. If the apply stops on 'cannot re-use a name that is still in use', re-run once the state is readable."
    return 0
  fi

  local history_json
  if ! history_json="$(trap - ERR; helm history "${release_name}" -n "${namespace}" -o json 2>/dev/null)"; then
    print_warning "Helm release '${release_name}' in namespace '${namespace}' is '${release_status}' and its history could not be read; leaving it. If the apply stops on 'cannot re-use a name that is still in use', inspect it with: helm history ${release_name} -n ${namespace}"
    return 0
  fi
  if printf '%s' "${history_json}" | python3 -c '
import json, sys
served = set(sys.argv[1].split())
try:
    revisions = json.load(sys.stdin)
except Exception:
    sys.exit(0)
sys.exit(0 if any(r.get("status") in served for r in revisions) else 1)
' "$HELM_SERVED_REVISION_STATUSES"; then
    print_warning "Helm release '${release_name}' in namespace '${namespace}' is '${release_status}' but a revision of it served before; leaving it. Roll it back or uninstall it yourself before re-running: helm history ${release_name} -n ${namespace}"
    return 0
  fi

  print_warning "Helm release '${release_name}' in namespace '${namespace}' is '${release_status}' and no revision of it ever deployed: an earlier apply died inside it. Uninstalling it so the apply can create it again (Helm refuses to reuse the name otherwise); its ${PLATFORM_AGENT_SECRET} is written back from terraform.tfvars."
  if helm uninstall "${release_name}" -n "${namespace}" --wait --timeout "$HELM_FAILED_RELEASE_UNINSTALL_TIMEOUT"; then
    print_success "Cleared the failed initial release '${release_name}'."
    return 0
  fi
  print_error "Could not uninstall the failed release '${release_name}' in namespace '${namespace}'. Inspect it with: helm status ${release_name} -n ${namespace}"
  return 1
}


# Writes the terraform.tfvars the full-install composition consumes, from the
# install.env variable set in the environment (load it first). The same
# generator runs from install.sh, upgrade.sh, and uninstall.sh, so the three
# front-ends can never describe different installs.
#
# create_cluster comes from a liveness probe, not from the interview, so
# "use an existing cluster" against a name that does not exist still creates
# it. A cluster that exists but is already in OUR state stays
# create_cluster = true — flipping it off would remove the resource from
# configuration and plan the cluster's destruction (lifecycle.sh guards this
# too).
write_tfvars_from_state() {
  local dest="$1"
  local image_tag="${2:-${IMAGE_TAG:-$IMAGE_TAG_FALLBACK}}"
  normalize_identity_vars

  # MEMORY_PROVIDER when the caller set it (install.sh's own run exports it),
  # otherwise translated from the MEMORY mode install.env records, and only
  # then the project default. Taking the default when a Hindsight install said
  # nothing is what reverted it to the file store.
  #
  # The two branches below are reached by different callers. install.sh's own
  # run arrives with MEMORY_PROVIDER exported from the interview and takes the
  # first. upgrade.sh, uninstall.sh and the Day-2 menu call
  # normalize_memory_vars beforehand, which sets MEMORY_PROVIDER from their
  # MEMORY, so they take the first too and the fallback beneath it is dead for
  # them -- it still covers a caller that sources these helpers directly.
  local memory_provider="${MEMORY_PROVIDER:-}"
  if [ -z "$memory_provider" ]; then
    memory_provider="$(memory_provider_from_mode "${MEMORY:-}")"
  fi
  local memory_provider_explicit="false"
  if [ -n "$memory_provider" ]; then
    memory_provider_explicit="true"
  fi
  : "${memory_provider:=${DEFAULT_MEMORY_PROVIDER}}"

  # cluster_mode follows the LIVE cluster when there is one. Hardcoding
  # "standard" here planned the destruction of every existing Autopilot
  # install the moment a front door regenerated tfvars against it — the
  # autopilot resource's count went to 0, so uninstall's targeted
  # deletion-protection apply and upgrade's full apply both became cluster
  # replacements.
  #
  # CLUSTER_MODE (install.sh --gke-cluster-mode, recorded in install.env) therefore
  # decides ONE case: the fresh create, where the probe found no cluster and
  # the interview is the only information there is. Every branch on which a
  # cluster exists assigns cluster_mode from the probe, so a stale or
  # hand-edited CLUSTER_MODE cannot reach a live cluster's tfvars — which
  # matters because uninstall.sh and upgrade.sh also regenerate through here
  # and have no flag to correct a wrong value with.
  # The initialiser is never the answer: both probe branches below assign, and
  # the fresh-create branch assigns from CLUSTER_MODE. It tracks
  # DEFAULT_CLUSTER_MODE only so the default has one spelling.
  local create_cluster="true" cluster_mode="${DEFAULT_CLUSTER_MODE}" autopilot_enabled=""
  local cluster_exists="false"
  # `trap - ERR` inside the substitution: under bash 3.2 (macOS's default)
  # the caller's inherited ERR trap fires in this subshell even though the
  # failure is the tested condition, printing an abort banner and writing a
  # FAILED report for a probe whose miss is the normal fresh-install path.
  if autopilot_enabled=$(trap - ERR; gcloud container clusters describe "${CLUSTER_NAME}" \
      --location "${REGION}" --project "${PROJECT_ID}" \
      --format="value(autopilot.enabled)" 2>/dev/null); then
    cluster_exists="true"
    # Both branches assign: the probe is the answer, not a chance to override
    # the initialiser.
    if [ "$autopilot_enabled" = "True" ]; then
      cluster_mode="autopilot"
      print_info "Cluster '${CLUSTER_NAME}' is an Autopilot cluster; generating cluster_mode = \"autopilot\"."
    else
      cluster_mode="standard"
    fi
    if tf_state_has_cluster; then
      print_info "Cluster '${CLUSTER_NAME}' exists and is managed by this install's Terraform state."
    else
      create_cluster="false"
      print_info "Cluster '${CLUSTER_NAME}' already exists and is not in Terraform state; installing onto it (create_cluster = false)."
    fi
  else
    # Only a genuine NOT_FOUND means "create it". Reading any other failure —
    # auth expiry, a network blip — as absence would regenerate the configured
    # shape with create_cluster=true against a live cluster of the OTHER shape
    # and plan its replacement under -auto-approve. That is a risk in both
    # directions and does not depend on which mode is the default. Refuse to
    # guess.
    local describe_err
    describe_err="$({ gcloud container clusters describe "${CLUSTER_NAME}" \
      --location "${REGION}" --project "${PROJECT_ID}" \
      --format="value(name)" 2>&1 >/dev/null || true; })"
    if ! printf '%s' "$describe_err" | grep -qiE "not.?found|404"; then
      print_error "Could not probe cluster '${CLUSTER_NAME}': ${describe_err:-unknown gcloud failure}"
      print_info "Refusing to guess between creating and adopting — a wrong guess can plan a live cluster's replacement. Fix the gcloud error and re-run."
      return 1
    fi
    # Confirmed absent, so nothing live can be reshaped by getting this wrong:
    # the interview's choice is the only shape on offer.
    cluster_mode="${CLUSTER_MODE:-$DEFAULT_CLUSTER_MODE}"
    if ! is_valid_cluster_mode "$cluster_mode"; then
      print_error "CLUSTER_MODE='${cluster_mode}' is not a cluster shape this install can create. Use autopilot or standard."
      return 1
    fi
    # Not "creating a cluster": uninstall.sh reaches this branch too, on an
    # install whose cluster is already gone.
    print_info "Cluster '${CLUSTER_NAME}' does not exist; generating cluster_mode = \"${cluster_mode}\" from the configured shape."
  fi

  # The generator's create/adopt decision, exported for the callers that need
  # it after the apply: install.sh sets the managed-OTel scope only on a
  # cluster this install created, never on an adopted one it does not own.
  TFVARS_CREATE_CLUSTER="$create_cluster"
  export TFVARS_CREATE_CLUSTER
  # Whether a cluster is there at all, which is not the same question: a
  # cluster this state created and still manages exists with
  # create_cluster = true. install.sh's pre-apply Helm check needs the first.
  TFVARS_CLUSTER_EXISTS="$cluster_exists"
  export TFVARS_CLUSTER_EXISTS
  # The shape the apply will actually use — probed when a cluster exists, the
  # requested one only on a fresh create. install.sh reports this rather than
  # the flag, so an adoption never claims to have built what it did not.
  TFVARS_CLUSTER_MODE="$cluster_mode"
  export TFVARS_CLUSTER_MODE

  # Installing onto a cluster that already exists (whether adopted or managed
  # by this install's Terraform state): fetch its credentials now, before the
  # recovery loop below — adoption is exactly the case where the credentials
  # live only in that cluster's Secret (a fresh clone has no install.env values),
  # and recovery and the live Hindsight probe are both gated on the kubectl
  # context actually being this cluster.
  if [ "$cluster_exists" = "true" ] && command -v kubectl >/dev/null 2>&1; then
    # Through the helper, as the front doors' other credential fetches are.
    # Without the flag a cluster whose IP endpoint this host cannot route to
    # gets that IP written into the kubeconfig anyway, and the recovery loop
    # below then reaches nothing. It cannot tell that from "no secret to
    # recover" — both leave the keys empty — so the install would go on to
    # generate a fresh SESSION_KV_SALT over the live one, re-anonymising every
    # chat user, and report success.
    GKE_DNS_ENDPOINT_FLAG=""
    gke_dns_endpoint_flag "${CLUSTER_NAME}" "${REGION}" "${PROJECT_ID}" || true
    # Unquoted on purpose: empty must contribute no argument. See gke_dns_endpoint.sh.
    # shellcheck disable=SC2086
    gcloud container clusters get-credentials "${CLUSTER_NAME}" --location "${REGION}" \
      --project "${PROJECT_ID}" $GKE_DNS_ENDPOINT_FLAG >/dev/null 2>&1 || true
  fi

  # install.env does not always carry the credentials: PERSIST_SECRETS_ON_DISK=false
  # keeps them out of it. Their home is the live Secret, so recover any
  # missing key from it — best-effort, since on a fresh install there is
  # no cluster to ask yet and the keys are still in the environment.
  # SESSION_KV_API_KEY and SESSION_KV_SALT are in the list because an adoption
  # re-install must keep the live salt: regenerating it re-anonymises every
  # chat user, severing their past sessions from their future ones.
  #
  # Only when kubectl's CURRENT context is this install's cluster (the name
  # get-credentials writes — set just above for adoption, and by upgrade.sh
  # and uninstall.sh before they generate). A stale context pointing at some
  # other install would otherwise silently donate that environment's
  # credentials to this one.
  local secret_key secret_val
  local expected_ctx
  expected_ctx="$(gke_context_name)"
  if command -v kubectl >/dev/null 2>&1 &&
    [ "$(kubectl config current-context 2>/dev/null || true)" = "$expected_ctx" ]; then
    for secret_key in API_SERVER_KEY GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY SLACK_BOT_TOKEN SLACK_APP_TOKEN SESSION_KV_API_KEY SESSION_KV_SALT; do
      [ -z "${!secret_key:-}" ] || continue
      # Every stage exits 0 on its own (|| true inside the substitution):
      # install.sh runs an inherited ERR trap (set -E), and a failing kubectl
      # here — no cluster yet, stale context — must be a silent no-op, not a
      # trap-and-abort the outer || can never see. --request-timeout bounds
      # the other stale-context failure mode: a context whose cluster was
      # just destroyed black-holes TCP instead of refusing, and eight keys
      # times a hung connect stalls the install for minutes.
      secret_val="$({ kubectl get secret "${PLATFORM_AGENT_SECRET}" -n "${NAMESPACE:-$DEFAULT_NAMESPACE}" \
        --context "$expected_ctx" \
        --request-timeout="${KUBECTL_PROBE_REQUEST_TIMEOUT}" \
        -o jsonpath="{.data.${secret_key}}" 2>/dev/null || true; } | base64 --decode 2>/dev/null || true)"
      if [ -n "$secret_val" ]; then
        export "${secret_key}=${secret_val}"
        print_info "Recovered ${secret_key} from the live '${PLATFORM_AGENT_SECRET}' Secret (install.env does not persist it)."
      fi
    done
  fi

  # MEMORY is the one key whose absence from install.env costs DATA rather than
  # re-creatable infrastructure. Every other key the generator defaults --
  # ENABLE_GVISOR, ENABLE_GKE_BACKUP_PLAN, ENABLE_STOCKOUT_INVESTIGATOR and the
  # rest -- names something Terraform can build again; this one names the
  # hindsight-postgresql StatefulSet and the volume holding the database. So it
  # is the one the generator goes and asks the cluster about when nobody told
  # it, rather than taking the project default.
  #
  # Outside the context gate above, because the probe does its own: it has to
  # be able to report "I could not ask", and a block that is skipped reports
  # nothing at all.
  if [ "$cluster_exists" = "true" ] && [ "$memory_provider_explicit" = "false" ]; then
    live_hindsight_state "${NAMESPACE:-$DEFAULT_NAMESPACE}"
    case "$LIVE_HINDSIGHT_STATE" in
      present)
        memory_provider="$HINDSIGHT_MEMORY_PROVIDER"
        export MEMORY_PROVIDER="$HINDSIGHT_MEMORY_PROVIDER"
        # Said per caller, as the unknown arm below is and on the same signal:
        # "preserved" and "to replace it" are true only for a caller that
        # applies next. uninstall.sh generates through here too, immediately
        # before lifecycle.sh destroy removes the store, and telling it the
        # database is being kept would be false at exactly the wrong moment.
        if is_truthy "${KUBE_AGENTS_REQUIRE_MEMORY_ANSWER:-false}"; then
          print_info "This cluster runs the Hindsight memory store and no memory mode was given, so it is preserved (memory_provider = \"${HINDSIGHT_MEMORY_PROVIDER}\"). Record MEMORY=file or MEMORY=off in install.env (install.sh also takes --memory=file or --memory=off) to replace it."
        else
          print_info "This cluster runs the Hindsight memory store and no memory mode was given; generating memory_provider = \"${HINDSIGHT_MEMORY_PROVIDER}\" to match the live install."
        fi
        ;;
      absent)
        # The API server answered, and answered NotFound for both objects. The
        # project default stands, and it plans nothing away.
        :
        ;;
      *)
        # Could not ask. Refuse for the callers that APPLY, because continuing
        # writes multiuser_memory into terraform.tfvars and the apply then
        # deletes a database this run never managed to look at. uninstall.sh
        # does not opt in: a destroy removes the store either way, and an
        # install has to keep a working way to remove itself.
        if is_truthy "${KUBE_AGENTS_REQUIRE_MEMORY_ANSWER:-false}"; then
          print_error "Cannot tell whether this cluster runs the Hindsight memory store, and no memory mode was given: ${LIVE_HINDSIGHT_REASON:-the cluster could not be reached}."
          print_info "Continuing would generate memory_provider = \"${DEFAULT_MEMORY_PROVIDER}\" and the apply would delete ${HINDSIGHT_API_DEPLOYMENT} and ${HINDSIGHT_STATEFULSET}, including its database."
          # Named per caller, as the Autopilot floor refusal below is and for
          # the same reason: upgrade.sh's parse_args has no --memory and answers
          # "Unknown parameter: --memory=file" with exit 2, so sending every
          # caller to the flag sends half of them to a second, worse error.
          # Recording MEMORY works everywhere and is named first for that
          # reason; MEMORY=… in the environment is the single-run form.
          print_info "State the answer instead: record MEMORY=hindsight|file|off in the install's install.env (install.sh also takes --memory=, and MEMORY=… in the environment answers for one run). Or restore access to the cluster (gcloud container clusters get-credentials ${CLUSTER_NAME} --location ${REGION} --project ${PROJECT_ID}${GKE_DNS_ENDPOINT_FLAG:+ $GKE_DNS_ENDPOINT_FLAG}) and re-run."
          return 1
        fi

        print_warning "Could not tell whether this cluster runs the Hindsight memory store (${LIVE_HINDSIGHT_REASON:-the cluster could not be reached}); generating memory_provider = \"${memory_provider}\"."
        ;;
    esac
  fi

  # Minting the key happens HERE, after the recovery loop above, and only for a
  # caller that opted in — install.sh, which is the one front door entitled to
  # create an install that did not exist. Order matters: the loop above skips
  # any key already set, so a key minted before it would shadow the live Secret
  # and every run would replace it and restart the pods holding it.
  # upgrade.sh and uninstall.sh deliberately do not set this: for them an
  # unfindable key means something is wrong, not that a new install is starting.
  if [ -z "${API_SERVER_KEY:-}" ] && is_truthy "${KUBE_AGENTS_GENERATE_API_SERVER_KEY:-false}"; then
    local generated
    generated="$(openssl rand -hex 16 2>/dev/null \
      || python3 -c "import secrets; print(secrets.token_hex(16))" 2>/dev/null \
      || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    if [ -z "$generated" ]; then
      print_error "Unable to generate API_SERVER_KEY from a secure random source."
      return 1
    fi
    export API_SERVER_KEY="$generated"
    print_info "Generated a new API_SERVER_KEY: this install had none and none could be recovered from a live cluster."
  fi

  # The composition requires api_server_key non-empty, so fail here with the
  # recovery path spelled out rather than aborting the caller on an opaque
  # unbound-variable error under set -u.
  if [ -z "${API_SERVER_KEY:-}" ]; then
    print_error "API_SERVER_KEY is not set, the install configuration does not carry it (PERSIST_SECRETS_ON_DISK=false keeps it out), and it could not be recovered from the live Secret."
    print_info "Recover it and re-run: export API_SERVER_KEY=\"\$(kubectl get secret ${PLATFORM_AGENT_SECRET} -n ${NAMESPACE:-$DEFAULT_NAMESPACE} -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode)\""
    return 1
  fi

  # A pre-existing cert-manager makes the composition's own cert-manager
  # release fail on the existing CRDs, so probe for one on the existing-cluster
  # path. Best-effort: an unreachable cluster leaves the default in place.
  # SKIP_CERT_MANAGER=true is the explicit opt-out — for the cluster whose
  # cert-manager comes from somewhere else, or the air-gapped runner that
  # cannot fetch the Jetstack chart from charts.jetstack.io.
  local enable_cert_manager="true"
  if is_truthy "${SKIP_CERT_MANAGER:-false}"; then
    enable_cert_manager="false"
    print_info "SKIP_CERT_MANAGER=true: the composition will not install cert-manager. The operator webhooks need one serving before the apply."
  elif [ "$create_cluster" = "false" ] && command -v kubectl >/dev/null 2>&1; then
    # Credentials were fetched above, on the same adoption branch.
    # Check that current-context actually points to this cluster; a stale context
    # must not probe another cluster and wrongly disable cert-manager on this one.
    local cert_expected_ctx
    cert_expected_ctx="$(gke_context_name)"
    if [ "$(kubectl config current-context 2>/dev/null || true)" = "$cert_expected_ctx" ] &&
      kubectl get deployment cert-manager -n cert-manager --context "$cert_expected_ctx" \
        --request-timeout="${KUBECTL_PROBE_REQUEST_TIMEOUT}" >/dev/null 2>&1; then
      # The Deployment alone cannot say whose it is. On a retry after an
      # apply that died past the cert-manager release, and on every
      # upgrade.sh regeneration of an existing-cluster install, the
      # cert-manager it finds is the composition's own; turning the flag
      # off then has Terraform destroy that release and the operator's
      # webhooks with it. The state settles it: a managed entry means ours.
      # A state that could not be read settles nothing, and the two wrong
      # answers are not symmetric -- a wrong "true" fails the apply on the
      # existing CRDs, a wrong "false" destroys silently -- so it keeps true.
      local cert_manager_rc=0
      tf_state_manages_resource "$TF_HELM_RELEASE_TYPE" "$TF_CERT_MANAGER_RELEASE_NAME" || cert_manager_rc=$?
      if [ "$cert_manager_rc" -eq 0 ]; then
        print_info "cert-manager on '${CLUSTER_NAME}' is this install's own release (it is in the Terraform state); keeping it."
      elif [ "$cert_manager_rc" -eq "$TF_STATE_RC_UNREADABLE" ]; then
        print_warning "cert-manager runs on '${CLUSTER_NAME}', and whether it is this install's own could not be decided because the Terraform state could not be read (see above). Keeping enable_cert_manager = true: if it is somebody else's, the apply fails on its CRDs and can be re-run with SKIP_CERT_MANAGER=true; turning it off wrongly would destroy this install's own."
      else
        enable_cert_manager="false"
        print_info "cert-manager already runs on '${CLUSTER_NAME}'; the composition will not install its own."
      fi
    fi
  fi

  # Only a mirrored install sets image_registry; the default prefix means
  # "the public registries", which the composition spells as empty.
  local image_registry=""
  if [ -n "${REGISTRY_PREFIX:-}" ] && [ "${REGISTRY_PREFIX%/}" != "$DEFAULT_REGISTRY_PREFIX" ]; then
    image_registry="${REGISTRY_PREFIX%/}"
  fi

  # The minter also needs its App key in KMS: the chart's minter Deployment
  # cannot pass readiness until the key is imported, and the composition's
  # helm release waits on every Deployment — enabling the minter with no
  # ENABLED key version would stall the apply and fail it with the cluster
  # already built. A readable PEM counts too: install.sh imports it after
  # the operator confirms and refuses the apply if that import fails, so a
  # fresh install with a PEM deploys the minter in one pass without the
  # import mutating anything before the confirmation (or on a dry run).
  # With no key and no PEM, defer the minter loudly rather than wedge.
  local enable_github_minter="false"
  # A GitLab install has no GitHub App: the GitOps repository is on GitLab and
  # the composition refuses the minter beside it.
  if [ "${GITOPS_FORGE:-${DEFAULT_GITOPS_FORGE:-github}}" = "gitlab" ]; then
    :
  elif [ -n "${GITOPS_ORG:-}" ] && [ -n "${GITOPS_REPO:-}" ] && [ -n "${GITHUB_APP_ID:-}" ]; then
    local minter_key_version=""
    minter_key_version="$(kms_key_enabled_version "${KMS_KEY:-$DEFAULT_KMS_KEY}" \
      "${KMS_KEYRING:-$DEFAULT_KMS_KEYRING}" "$(derive_kms_location "${REGION}")" "${PROJECT_ID}")"
    if [ -n "$minter_key_version" ] || [ -f "${GITHUB_PEM_PATH:-}" ]; then
      enable_github_minter="true"
    else
      print_warning "GitHub minter deferred: its KMS signing key has no ENABLED version, no App private key PEM is at hand (GITHUB_PEM_PATH), and a minter deployed without the key never passes readiness."
      print_info "Provide the PEM via --github-pem-path (or pre-import the key into Cloud KMS: https://github.com/abcxyz/github-token-minter) and re-run — the next run adds the minter to the existing install."
    fi
  fi

  # Exported like TFVARS_CREATE_CLUSTER, for check_service_account_ownership:
  # the minter's GSA is only planned when the minter is.
  TFVARS_ENABLE_GITHUB_MINTER="$enable_github_minter"
  export TFVARS_ENABLE_GITHUB_MINTER

  # ENABLE_GVISOR is one intent — run the agent sandboxed — and the two things
  # that satisfy it differ by cluster shape. Standard needs the sandbox node
  # pool AND the RuntimeClass on the pod; Autopilot ships the gvisor
  # RuntimeClass natively and has no pool to manage, so asking the gke-cluster
  # module for one there fails the plan. Deriving both from the probed
  # cluster_mode keeps --enable-gvisor=true meaning the same thing on either shape.
  #
  # The fallback stays false even though a fresh install now defaults to the
  # sandbox. install.sh owns that default and exports ENABLE_GVISOR before
  # calling this function, so the fallback here
  # never decides a new install — it only decides the callers that read an
  # install that already exists. uninstall.sh is the one that matters:
  # install.env is optional there, and the documented `curl … | bash` teardown
  # runs from a fresh clone that has none. Defaulting on for that caller would
  # let the Autopilot version-floor check below abort a destroy, leaving an
  # install with no working way to remove itself.
  #
  # The fallback only reaches the teardown that has no install.env, which is not
  # the ordinary one — a teardown from the checkout that installed loads an
  # install.env saying ENABLE_GVISOR="true". uninstall.sh therefore exports false
  # itself before calling this, and the comment there is where that argument
  # lives. Do not read the fallback as protecting the teardown on its own.
  local gvisor_node_pool="false" agent_runtime_class=""
  if is_truthy "${ENABLE_GVISOR:-false}"; then
    agent_runtime_class="gvisor"
    if [ "$cluster_mode" = "standard" ]; then
      gvisor_node_pool="true"
    elif [ "$cluster_exists" = "false" ]; then
      # An Autopilot cluster this run is about to create comes up on its
      # release channel's current version, which has been past the floor since
      # 2023. There is nothing to describe yet, so checking would only produce
      # the "could not read the version" warning below on every fresh
      # --gke-cluster-mode=autopilot --enable-gvisor=true install.
      print_info "Creating Autopilot cluster '${CLUSTER_NAME}': using its built-in gvisor RuntimeClass, with no sandbox node pool to provision."
    else
      # Autopilot's gvisor RuntimeClass arrived in a specific GKE version, and
      # asking an older cluster for it fails late: the operator stops at its
      # RuntimeClass check before writing the agent Deployment, the Helm
      # release still reports success because that Deployment is
      # operator-created rather than chart-rendered, and install.sh's
      # post-apply gate waits out its budget on a Deployment that is never
      # coming and exits 1 — after the cluster, IAM, KMS, cert-manager and the
      # release have all been applied. That gate does name the RuntimeClass,
      # but it names it having already spent the apply. Refuse here instead,
      # which is where the gke-cluster module's precondition refuses the
      # equivalent Standard mistake. This branch is reached only when the
      # describe above found a live Autopilot cluster, so there is always one
      # to ask.
      #
      # `trap - ERR` as well as `|| true`, for the bash 3.2 reason the probe
      # above gives: a gcloud failure here is a best-effort miss, not an abort.
      local master_version
      master_version="$(trap - ERR; gcloud container clusters describe "${CLUSTER_NAME}" \
        --location "${REGION}" --project "${PROJECT_ID}" \
        --format="value(currentMasterVersion)" 2>/dev/null || true)"
      master_version="${master_version//[[:space:]]/}"
      if [[ ! "$master_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+-gke\.[0-9]+$ ]]; then
        print_warning "Could not read the GKE version of Autopilot cluster '${CLUSTER_NAME}'; proceeding as though it supports GKE Sandbox. Below ${GVISOR_AUTOPILOT_MIN_VERSION} the agent Deployment is never created and this run fails at its final check."
      elif ! gke_version_at_least "$master_version" "$GVISOR_AUTOPILOT_MIN_VERSION"; then
        print_error "Autopilot cluster '${CLUSTER_NAME}' runs GKE ${master_version}, and its gvisor RuntimeClass needs ${GVISOR_AUTOPILOT_MIN_VERSION} or later."
        print_info "Upgrade the cluster, or run the agent on the standard runtime: install.sh takes --enable-gvisor=false, and upgrade.sh reads the choice from ENABLE_GVISOR in install.env. Continuing would apply every GCP and Helm resource and then fail on a missing agent Deployment."
        print_info "Tearing down instead? uninstall.sh forces ENABLE_GVISOR=false and is never blocked by this check; if you reach it from some other caller, export ENABLE_GVISOR=false first."
        return 1
      fi
      print_info "Cluster '${CLUSTER_NAME}' is Autopilot: using its built-in gvisor RuntimeClass, with no sandbox node pool to provision."
    fi
  fi

  # Empty takes the default, 0, and both render nothing in the chart. Checked
  # here as well as in install.sh's interview because upgrade.sh and
  # uninstall.sh regenerate from install.env without it, and a bare word in
  # HCL would otherwise fail at terraform's parser with a message naming
  # neither the key nor the file to fix.
  local model_max_tokens="${MODEL_MAX_TOKENS:-$DEFAULT_MODEL_MAX_TOKENS}"
  if ! is_non_negative_integer "$model_max_tokens"; then
    print_error "MODEL_MAX_TOKENS='${model_max_tokens}' is not a whole number of tokens. Set a non-negative integer, or leave it empty, in install.env."
    return 1
  fi
  # A triple the block cannot render, or a container ID that is not a bare
  # number, stops the run here, with the file untouched, rather than at
  # terraform with a message naming neither the key nor the entry.
  require_scope_cluster_triples "${SCOPE_EXCLUDE_CLUSTERS:-}" || return 1
  require_scope_container_ids "${SCOPE_FOLDERS:-}" "${SCOPE_ORGANIZATIONS:-}" || return 1
  require_scope_max_projects "${SCOPE_MAX_PROJECTS:-}" || return 1
  # Checked here for the MODEL_MAX_TOKENS reason: upgrade.sh and uninstall.sh
  # regenerate from install.env without install.sh's checks. A misspelt toggle
  # is refused rather than read as off, which would forward requests
  # unredacted while the file says otherwise. While it is off the other three
  # keys are inert, as they are in the chart, and are neither read nor checked.
  local redaction_enabled="${LITELLM_REDACTION_ENABLED:-$DEFAULT_LITELLM_REDACTION_ENABLED}"
  if ! is_bool_spelling "$redaction_enabled"; then
    print_error "LITELLM_REDACTION_ENABLED='${redaction_enabled}' is neither true nor false. Fix it in install.env."
    return 1
  fi
  local redaction_ip_action="" redaction_rules="[]"
  if is_truthy "$redaction_enabled"; then
    redaction_ip_action="${LITELLM_REDACTION_IP_ACTION:-$DEFAULT_LITELLM_REDACTION_IP_ACTION}"
    if ! is_valid_redaction_ip_action "$redaction_ip_action"; then
      print_error "LITELLM_REDACTION_IP_ACTION='${redaction_ip_action}' is not one of mask, pseudonym, off. Fix it in install.env."
      return 1
    fi
    if [ -n "${LITELLM_REDACTION_RULES:-}" ]; then
      redaction_rules="$(hcl_redaction_rules "$LITELLM_REDACTION_RULES")" || return 1
    fi
  fi
  # The scoped service account pool, for the same reason: a misspelt switch
  # is refused rather than read as off, and a cap the module would refuse is
  # named here with its key. The cap is checked whether or not the pool is
  # armed, so turning the pool on later does not surface a line written long
  # before.
  local scoped_pool_enabled="${SCOPED_SA_POOL_ENABLED:-$SCOPED_SA_POOL_ENABLED_DEFAULT}"
  if ! is_bool_spelling "$scoped_pool_enabled"; then
    print_error "SCOPED_SA_POOL_ENABLED='${scoped_pool_enabled}' is neither true nor false. Fix it in install.env."
    return 1
  fi
  require_scoped_sa_pool_max_accounts "${SCOPED_SA_POOL_MAX_ACCOUNTS:-}" || return 1
  # The mode, for the MODEL_MAX_TOKENS reason: upgrade.sh and the Day-2 menu
  # regenerate from install.env without install.sh's check, and the variable's
  # validation would otherwise refuse it at plan, naming neither the key nor
  # the file.
  local platform_agent_mode="${PLATFORM_AGENT_MODE:-$DEFAULT_PLATFORM_AGENT_MODE}"
  if ! is_valid_platform_agent_mode "$platform_agent_mode"; then
    print_error "PLATFORM_AGENT_MODE='${platform_agent_mode}' is not one of today, next. Fix it in install.env."
    return 1
  fi

  local old_umask
  old_umask="$(umask)"
  umask 077
  # Published for the front doors' ERR traps: the partial file below is mode
  # 600 and holds every secret this run was given, and it sits one character
  # from the real terraform.tfvars. Cleared after the mv succeeds.
  TFVARS_TMP_FILE="${dest}.tmp"
  {
    echo "# Generated by the kube-agents installer from install.env — regenerated on every"
    echo "# run. Change settings through install.sh (or its --menu) rather than here."
    echo "project_id   = $(hcl_str "${PROJECT_ID}")"
    echo "cluster_name = $(hcl_str "${CLUSTER_NAME}")"
    echo "location     = $(hcl_str "${REGION}")"
    echo "namespace    = $(hcl_str "${NAMESPACE:-$DEFAULT_NAMESPACE}")"
    echo ""
    echo "# One fixed name per project each, while this state is per cluster: a"
    echo "# second install in the project names its own in install.env."
    echo "agent_service_account_id         = $(hcl_str "${PLATFORM_AGENT_GSA_NAME:-$DEFAULT_PLATFORM_AGENT_GSA_NAME}")"
    echo "github_minter_service_account_id = $(hcl_str "${GITHUB_MINTER_GSA_NAME:-$DEFAULT_GITHUB_MINTER_GSA_NAME}")"
    echo "litellm_service_account_id       = $(hcl_str "${LITELLM_GSA_NAME:-$DEFAULT_LITELLM_GSA_NAME}")"
    echo ""
    echo "# GKE database encryption. install.sh's existing-cluster step reads the"
    echo "# same two keys, so a create and an adoption encrypt with one key."
    echo "kms_keyring_name = $(hcl_str "${GKE_DB_KMS_KEYRING:-$DEFAULT_GKE_DB_KMS_KEYRING}")"
    echo "kms_key_name     = $(hcl_str "${GKE_DB_KMS_KEY:-$DEFAULT_GKE_DB_KMS_KEY}")"
    echo ""
    echo "# The DNS endpoint is open and deletion protection is off. cluster_mode is"
    echo "# the live cluster's own shape whenever there is one to probe, and the"
    echo "# --gke-cluster-mode the install asked for only on a create."
    echo "cluster_mode               = $(hcl_str "${cluster_mode}")"
    echo "create_cluster             = ${create_cluster}"
    echo "# An adoption that chose to install without NetworkPolicy enforcement."
    echo "# Inert on a created cluster and on one that already enforces."
    echo "accept_no_network_policy   = $(hcl_bool "${ACCEPT_NO_NETWORK_POLICY:-false}")"
    echo "allow_external_dns_traffic = true"
    echo "deletion_protection        = false"
    echo "enable_gvisor_node_pool    = ${gvisor_node_pool}"
    echo "gvisor_pool_name           = $(hcl_str "${GVISOR_POOL_NAME:-$DEFAULT_GVISOR_POOL_NAME}")"
    echo "agent_runtime_class        = $(hcl_str "${agent_runtime_class}")"
    echo "enable_cert_manager        = ${enable_cert_manager}"
    echo ""
    echo "image_tag                  = $(hcl_str "${image_tag}")"
    echo "image_registry             = $(hcl_str "${image_registry}")"
    echo "third_party_image_registry = $(hcl_str "${THIRD_PARTY_REGISTRY_PREFIX:-}")"
    echo ""
    echo "model_provider     = $(hcl_str "${MODEL_PROVIDER:-$DEFAULT_MODEL_PROVIDER}")"
    echo "model_default_name = $(hcl_str "${MODEL_DEFAULT_NAME:-}")"
    echo "model_max_tokens   = ${model_max_tokens}"
    echo "vertex_project_id  = $(hcl_str "${VERTEX_PROJECT_ID:-}")"
    echo "vertex_location    = $(hcl_str "${VERTEX_LOCATION:-}")"
    echo "vertex_manage_serving_project = $(hcl_bool "${VERTEX_MANAGE_SERVING_PROJECT:-$DEFAULT_VERTEX_MANAGE_SERVING_PROJECT}")"
    echo ""
    echo "# The PlatformAgent's spec.mode (PLATFORM_AGENT_MODE in install.env). The"
    echo "# composition passes the chart nothing for \"today\", so its CR carries no mode."
    echo "platform_agent_mode = $(hcl_str "$platform_agent_mode")"
    echo ""
    echo "# Gateway redaction (LITELLM_REDACTION_* in install.env). The composition"
    echo "# renders nothing into the chart while enabled is false."
    if is_truthy "$redaction_enabled"; then
      echo "litellm_redaction = {"
      echo "  enabled     = true"
      echo "  ip_action   = $(hcl_str "$redaction_ip_action")"
      echo "  allow_cidrs = $(hcl_csv_list "${LITELLM_REDACTION_IP_ALLOW_CIDRS:-}")"
      echo "  rules       = ${redaction_rules}"
      echo "}"
    else
      echo "litellm_redaction = { enabled = false }"
    fi
    echo ""
    echo "# The scoped service account pool (SCOPED_SA_POOL_ENABLED,"
    echo "# SCOPED_SA_POOL_MAX_ACCOUNTS in install.env): one reader service account per"
    echo "# project the plan lists in the scope block above, armed by this switch alone."
    echo "scoped_pool_enabled      = $(hcl_bool "$scoped_pool_enabled")"
    if [ -n "${SCOPED_SA_POOL_MAX_ACCOUNTS:-}" ]; then
      echo "scoped_pool_max_accounts = ${SCOPED_SA_POOL_MAX_ACCOUNTS}"
    fi
    echo ""
    if is_truthy "${PERSIST_SECRETS_ON_DISK:-$DEFAULT_PERSIST_SECRETS_ON_DISK}"; then
      echo "api_server_key    = $(hcl_str "${API_SERVER_KEY:-}")"
      echo "gemini_api_key    = $(hcl_str "${GEMINI_API_KEY:-}")"
      echo "openai_api_key    = $(hcl_str "${OPENAI_API_KEY:-}")"
      echo "anthropic_api_key = $(hcl_str "${ANTHROPIC_API_KEY:-}")"
      if [ -n "${SESSION_KV_API_KEY:-}" ] || [ -n "${SESSION_KV_SALT:-}" ]; then
        echo "# Recovered from the live Secret: an adoption re-install must keep the"
        echo "# salt, or every chat identity re-pseudonymises."
        echo "session_kv_api_key = $(hcl_str "${SESSION_KV_API_KEY:-}")"
        echo "session_kv_salt    = $(hcl_str "${SESSION_KV_SALT:-}")"
      fi
    else
      echo "# Credentials omitted: PERSIST_SECRETS_ON_DISK=false keeps them out of"
      echo "# every file the installer writes. The front doors hand them to"
      echo "# terraform as TF_VAR_* environment variables instead."
    fi
    echo ""
    echo "permission_set = $(hcl_str "${PLATFORM_AGENT_PERMISSION_SET:-$DEFAULT_PERMISSION_SET}")"
    if [ "${PLATFORM_AGENT_PERMISSION_SET:-}" = "custom" ]; then
      echo "project_roles  = $(hcl_csv_list "${PLATFORM_AGENT_CUSTOM_ROLES:-}")"
    fi
    echo ""
    echo "# The projects, folders, organisations, Shared VPC hosts and Metrics Scopes"
    echo "# beyond project_id whose GKE clusters get a Cluster Agent, and what to leave"
    echo "# unmanaged (SCOPE_PROJECTS, SCOPE_FOLDERS, SCOPE_ORGANIZATIONS,"
    echo "# SCOPE_SHARED_VPC_HOSTS, SCOPE_METRICS_SCOPES, SCOPE_MAX_PROJECTS, SCOPE_EXCLUDE_PROJECTS,"
    echo "# SCOPE_EXCLUDE_CLUSTERS in install.env). Always written, so this file states"
    echo "# the declaration the composition renders either way, empty lists included; an"
    echo "# emptied list is the declaration that drops what it named."
    hcl_scope_block "${SCOPE_PROJECTS:-}" "${SCOPE_FOLDERS:-}" "${SCOPE_ORGANIZATIONS:-}" \
      "${SCOPE_SHARED_VPC_HOSTS:-}" "${SCOPE_METRICS_SCOPES:-}" \
      "${SCOPE_EXCLUDE_PROJECTS:-}" "${SCOPE_EXCLUDE_CLUSTERS:-}" "${SCOPE_MAX_PROJECTS:-}"
    echo ""
    local chat_topic="${CHAT_TOPIC_NAME:-$DEFAULT_CHAT_TOPIC_NAME}"
    local chat_sub="${CHAT_SUB_NAME:-$DEFAULT_CHAT_SUB_NAME}"
    if [ "${GOOGLE_CHAT_ENABLED:-$DEFAULT_GOOGLE_CHAT_ENABLED}" = "true" ]; then
      local state_sub="" state_rc=0
      state_sub="$(tf_state_chat_subscription_name)" || state_rc=$?
      if [ "$state_rc" -eq "$TF_STATE_RC_UNREADABLE" ]; then
        print_warning "Could not determine if Google Chat Pub/Sub subscription is in Terraform state (see above); proceeding with configuration." >&2
      fi

      if [ -n "$state_sub" ]; then
        chat_sub="$state_sub"
      elif [ -z "${CHAT_SUB_NAME:-}" ] || [ "$CHAT_SUB_NAME" = "$DEFAULT_CHAT_SUB_NAME" ]; then
        chat_sub="$(derive_chat_sub_name "$chat_topic")"
      fi
    fi
    echo "enable_google_chat        = $(hcl_bool "${GOOGLE_CHAT_ENABLED:-$DEFAULT_GOOGLE_CHAT_ENABLED}")"
    echo "chat_topic_name           = $(hcl_str "$chat_topic")"
    echo "chat_subscription_name    = $(hcl_str "$chat_sub")"
    echo "google_chat_allowed_users = $(hcl_csv_list "${ALLOWED_USERS:-}")"
    echo "google_chat_home_channel  = $(hcl_str "${GOOGLE_CHAT_HOME_CHANNEL:-}")"
    echo "google_chat_mode          = $(hcl_str "${GOOGLE_CHAT_MODE:-$DEFAULT_GOOGLE_CHAT_MODE}")"
    echo ""
    echo "enable_slack            = $(hcl_bool "${SLACK_ENABLED:-$DEFAULT_SLACK_ENABLED}")"
    if is_truthy "${PERSIST_SECRETS_ON_DISK:-$DEFAULT_PERSIST_SECRETS_ON_DISK}"; then
      echo "slack_bot_token         = $(hcl_str "${SLACK_BOT_TOKEN:-}")"
      echo "slack_app_token         = $(hcl_str "${SLACK_APP_TOKEN:-}")"
    fi
    echo "slack_allowed_users     = $(hcl_csv_list "${SLACK_ALLOWED_USERS:-}")"
    echo "slack_home_channel      = $(hcl_str "${SLACK_HOME_CHANNEL:-}")"
    echo "slack_home_channel_name = $(hcl_str "${SLACK_HOME_CHANNEL_NAME:-}")"
    echo ""
    # Written only for a GitLab install, so a GitHub one renders the exact
    # lines it always has. GITOPS_REPO is the project's full path there.
    if [ "${GITOPS_FORGE:-${DEFAULT_GITOPS_FORGE:-github}}" = "gitlab" ]; then
      echo "gitops_forge              = \"gitlab\""
      echo "gitops_host               = $(hcl_str "${GITOPS_HOST:-}")"
      echo "gitlab_repo               = $(hcl_str "${GITOPS_REPO:-}")"
      echo "gitlab_token_secret_name  = $(hcl_str "${GITLAB_TOKEN_SECRET:-${DEFAULT_GITLAB_TOKEN_SECRET:-gitlab-forge-token}}")"
    elif [ -n "${GITOPS_ORG:-}" ] && [ -n "${GITOPS_REPO:-}" ]; then
      echo "github_repo = $(hcl_str "${GITOPS_ORG}/${GITOPS_REPO}")"
    fi
    echo "enable_github_minter = ${enable_github_minter}"
    echo "github_app_id        = $(hcl_str "${GITHUB_APP_ID:-}")"
    if [ -n "${KMS_KEYRING:-}" ]; then
      echo "github_minter_kms_keyring = $(hcl_str "${KMS_KEYRING}")"
    fi
    if [ -n "${KMS_KEY:-}" ]; then
      echo "github_minter_kms_key = $(hcl_str "${KMS_KEY}")"
    fi
    echo ""
    echo "enable_gke_backup_plan = $(hcl_bool "${ENABLE_GKE_BACKUP_PLAN:-$DEFAULT_ENABLE_GKE_BACKUP_PLAN}")"
    echo ""
    echo "# The CRD defaults dashboardEnabled to true; the installer has always"
    echo "# defaulted it to false and asks. Memory settings mirror --memory."
    echo "hermes_dashboard_enabled = $(hcl_bool "${HERMES_DASHBOARD_ENABLED:-$DEFAULT_ENABLE_WEBUI}")"
    echo "memory_enabled           = $(hcl_bool "${MEMORY_ENABLED:-$DEFAULT_MEMORY_ENABLED}")"
    echo "memory_provider          = $(hcl_str "$memory_provider")"
    echo "user_profile_enabled     = $(hcl_bool "${USER_PROFILE_ENABLED:-$DEFAULT_USER_PROFILE_ENABLED}")"
    echo ""
    echo "# Optional AgentPlugins"
    echo "enable_pubsub_platform       = $(hcl_bool "${ENABLE_PUBSUB_PLATFORM:-$DEFAULT_ENABLE_PUBSUB_PLATFORM}")"
    echo "enable_stockout_investigator = $(hcl_bool "${ENABLE_STOCKOUT_INVESTIGATOR:-$DEFAULT_ENABLE_STOCKOUT_INVESTIGATOR}")"
    # Written only when on, and both keys together, which is the one place in
    # this file that omits a key rather than writing false.
    #
    # enable_drift_pubsub is also reachable on its own as a TF_VAR_ line in
    # install.env, and a tfvars key beats TF_VAR_. Writing "false" here
    # unconditionally would therefore override an install already running the
    # ingress that way and destroy its sink, topic and subscription on the next
    # upgrade -- from a release note nobody read. Omitted, that install is
    # untouched; the front doors turn drift detection off again by dropping
    # both keys, which is the ordinary teardown the other flags get from
    # writing false.
    # is_truthy, not a compare against the lowercase literal: every other
    # boolean here reaches hcl_bool -> is_truthy, which takes True/yes/y/1/on,
    # and install.env is hand-written. A string compare would read
    # ENABLE_DRIFT_DETECTOR=True as off and silently provision nothing
    # (install.sh says the same about GOOGLE_CHAT_ENABLED, for the same
    # reason).
    if is_truthy "${ENABLE_DRIFT_DETECTOR:-$DEFAULT_ENABLE_DRIFT_DETECTOR}"; then
      echo ""
      echo "# Out-of-band change detection: the audit-log ingress and the consumer"
      echo "# that reads it. Both, because the detector reads the subscription the"
      echo "# first one provisions and the composition refuses the apply otherwise."
      echo "enable_drift_pubsub   = true"
      echo "enable_drift_detector = true"
    fi
  } > "${dest}.tmp"
  chmod 600 "${dest}.tmp"
  mv -f -- "${dest}.tmp" "$dest"
  # In flight no longer: the front doors' ERR traps read this to remove the
  # partial file. It is mode 600 and carries every secret the run was given, so
  # a failure between the redirect above and this line must not leave it in
  # terraform/examples/full-install/ under a name one letter from the real one.
  # shellcheck disable=SC2034  # read by install/upgrade/uninstall's ERR traps
  TFVARS_TMP_FILE=""
  umask "$old_umask"

  # Terraform reads TF_VAR_* only where terraform.tfvars is silent, so with
  # PERSIST_SECRETS_ON_DISK=true (the default) the file above wins and these
  # are inert; with it false they are the only channel the credentials travel.
  # Exported here, at the one place every front door already passes through,
  # so no terraform invocation downstream can miss them.
  export TF_VAR_api_server_key="${API_SERVER_KEY:-}"
  export TF_VAR_gemini_api_key="${GEMINI_API_KEY:-}"
  export TF_VAR_openai_api_key="${OPENAI_API_KEY:-}"
  export TF_VAR_anthropic_api_key="${ANTHROPIC_API_KEY:-}"
  export TF_VAR_slack_bot_token="${SLACK_BOT_TOKEN:-}"
  export TF_VAR_slack_app_token="${SLACK_APP_TOKEN:-}"
  export TF_VAR_session_kv_api_key="${SESSION_KV_API_KEY:-}"
  export TF_VAR_session_kv_salt="${SESSION_KV_SALT:-}"
}
