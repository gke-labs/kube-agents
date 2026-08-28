#!/usr/bin/env bash
# ==============================================================================
# Shared definitions for the kube-agents installer front-ends.
# ==============================================================================
# Sourced by install.sh, uninstall.sh, and upgrade.sh — and this is where the
# terraform.tfvars generator lives, so the three front-ends describe the same
# install to the same engine (terraform/examples/full-install).
#
# Contract: the caller defines print_info / print_warning / print_error before
# calling anything here that reports. Functions read the vars.sh variable set
# from the environment (source vars.sh first); none of them prompt.
# ==============================================================================

# ─── Shared Installer Defaults ────────────────────────────────────────────────
# The values every installer front-end must agree on. Each default has exactly
# one home here so the entry points cannot drift apart.
DEFAULT_CLUSTER_NAME="platform-agent-host"
DEFAULT_REGION="us-central1"
# Autopilot, because it is the shape this project is developed and validated
# against: the autopush and staging installs already run it, the chart needs no
# node pool, and gVisor comes from Autopilot's built-in RuntimeClass rather than
# a dedicated pool. Standard remains a first-class choice and is what
# --cluster-mode=standard, CLUSTER_MODE=standard, or a hand-written tfvars
# selects; it is also required for a zonal install, which Autopilot cannot do.
#
# This decides ONE thing: the shape a FRESH install creates. It never reaches a
# cluster that already exists -- see write_tfvars_from_state, where every branch
# with a live cluster takes its mode from the probe instead.
DEFAULT_CLUSTER_MODE="autopilot"
# Vertex serves each model from its own subset of locations, and the cluster's
# region is usually not one of them -- gemini-3.5-flash, the vertex_ai default,
# is not served from us-central1 (DEFAULT_REGION), so a region-derived default
# 404s on a stock install. The global endpoint serves the first-party Gemini
# models from wherever has capacity, which is the only default that works
# without knowing the cluster's region. Two reasons to override it: it gives no
# in-region ML processing guarantee, and a Model Garden partner model (Claude,
# Llama, Mistral) may be served only from specific regions. Which locations
# serve which model:
# https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/locations
DEFAULT_VERTEX_LOCATION="global"
DEFAULT_MODEL_PROVIDER="gemini"

# All kube-agents images (k8s-operator, platform-agent, credential-proxy,
# replay-proxy) default to this public registry prefix. Behind-the-firewall
# installs set REGISTRY_PREFIX to pull mirrored images instead.
DEFAULT_REGISTRY_PREFIX="ghcr.io/gke-labs/kube-agents"

# Model provider → the model the install defaults to for that provider.
default_model_for_provider() {
  case "${1:-}" in
    openai) echo "gpt-5.4" ;;
    anthropic) echo "claude-opus-5" ;;
    *) echo "gemini-3.5-flash" ;;
  esac
}

is_valid_model_provider() {
  [[ "${1:-}" =~ ^(gemini|vertex_ai|anthropic|openai)$ ]]
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
# --cluster-mode against it, and the two must agree.
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

# ─── vars.sh Persistence ──────────────────────────────────────────────────────
# vars.sh is the install's machine-readable record: the admin console, the e2e
# tests, and the Day-2 menu all read it. VARS_FILE must be set by the caller.
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

save_secret_var() {
  local var_name=$1
  local var_val=$2
  export "${var_name}=${var_val}"
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    return 0
  fi
  if is_truthy "${PERSIST_SECRETS_ON_DISK:-true}"; then
    save_var "$var_name" "$var_val"
  else
    if [ -f "$VARS_FILE" ]; then
      local old_umask
      old_umask=$(umask)
      umask 077
      chmod 600 "$VARS_FILE" 2>/dev/null || true
      grep -E -v "^[[:space:]]*export[[:space:]]+${var_name}=" "$VARS_FILE" > "$VARS_FILE.tmp" 2>/dev/null || true
      chmod 600 "$VARS_FILE.tmp" 2>/dev/null || true
      mv "$VARS_FILE.tmp" "$VARS_FILE"
      chmod 600 "$VARS_FILE" 2>/dev/null || true
      umask "$old_umask"
    fi
  fi
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
  if ! response=$(curl -sS --max-time 10 -H "Accept: application/vnd.github+json" \
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
# passed its readiness probes, so catch it while GITHUB_ORG is still being set.
#
# This exits, so it is the wrong entry point for anything that can still
# re-prompt: install.sh calls github_account_type directly and settles the value
# before provisioning starts. An inconclusive lookup is never fatal — an
# unreachable api.github.com must not block a provision that is otherwise fine.
check_github_org_is_organization() {
  local org="${1:-}"
  [ -z "$org" ] && return 0

  if is_truthy "${SKIP_GITHUB_ORG_CHECK:-false}"; then
    print_warning "SKIP_GITHUB_ORG_CHECK=true is set; not verifying that '${org}' is an organization."
    return 0
  fi

  case "$(github_account_type "$org")" in
    organization) return 0 ;;
    user)
      print_error "GITHUB_ORG='${org}' is a GitHub user account, not an organization."
      print_error "The GitHub Token Minter looks installations up at /orgs/${org}/installation,"
      print_error "which does not exist for personal accounts, so every token request would"
      print_error "fail with a 404 after deployment."
      print_error "Move the GitOps repository to an organization (a free one is enough) and set"
      print_error "GITHUB_ORG in ${VARS_FILE:-scripts/vars.sh} to it, or re-run with"
      print_error "SKIP_GITHUB_ORG_CHECK=true to bypass this check."
      print_error "See the chart's githubMinter values and terraform/modules/github-minter."
      exit 1
      ;;
    missing)
      print_error "GITHUB_ORG='${org}' does not exist on GitHub."
      print_error "Check the spelling. The Token Minter resolves installations at"
      print_error "/orgs/${org}/installation, so a name that does not exist fails every"
      print_error "token request after deployment."
      print_error "Edit GITHUB_ORG in ${VARS_FILE:-scripts/vars.sh}, or re-run with"
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
# from the original install. Keep in step with lifecycle.sh's ensure_backend.
tf_state_bucket() {
  local bucket="${KUBE_AGENTS_STATE_BUCKET:-auto}"
  [ "$bucket" = "auto" ] && bucket="${PROJECT_ID}-kube-agents-tfstate"
  echo "$bucket"
}

tf_state_prefix() {
  echo "${KUBE_AGENTS_STATE_PREFIX:-kube-agents/${CLUSTER_NAME}}"
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

# Comma- or space-separated string → HCL list of strings, dropping empty
# items. Both separators, because --custom-roles documents "space- or
# comma-separated".
hcl_csv_list() {
  local csv="${1:-}" out="[" first=true item
  local IFS=$', \t\n'
  for item in $csv; do
    item="${item#"${item%%[![:space:]]*}"}"
    item="${item%"${item##*[![:space:]]}"}"
    [ -n "$item" ] || continue
    $first || out+=", "
    out+="$(hcl_str "$item")"
    first=false
  done
  printf '%s]' "$out"
}

# Whether this install's Terraform state already MANAGES the cluster. Read
# straight from the state object in GCS — cheaper and earlier than an init, and
# it works from a fresh clone. Any read failure means "not ours".
#
# Parsed, not grepped, for two reasons. An existing-cluster install records a
# data-mode "google_container_cluster" entry in the same state, and matching on
# the type alone would flip such an install's create_cluster back to true on
# every re-run — planning a second cluster over the real one. And a
# `gcloud | grep -q` pipeline under pipefail can report a cluster that IS in
# state as absent when grep exits before gcloud finishes writing (the same trap
# lifecycle.sh documents for its own state reads).
tf_state_has_cluster() {
  local state
  state=$(gcloud storage cat "gs://$(tf_state_bucket)/$(tf_state_prefix)/default.tfstate" 2>/dev/null) || return 1
  printf '%s' "$state" | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(1)
managed = any(
    r.get("type") == "google_container_cluster" and r.get("mode") == "managed"
    for r in doc.get("resources", [])
)
sys.exit(0 if managed else 1)
'
}

# Writes the terraform.tfvars the full-install composition consumes, from the
# vars.sh variable set in the environment (source vars.sh first). The same
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
  local image_tag="${2:-${IMAGE_TAG:-latest}}"

  # cluster_mode follows the LIVE cluster when there is one. Hardcoding
  # "standard" here planned the destruction of every existing Autopilot
  # install the moment a front door regenerated tfvars against it — the
  # autopilot resource's count went to 0, so uninstall's targeted
  # deletion-protection apply and upgrade's full apply both became cluster
  # replacements.
  #
  # CLUSTER_MODE (install.sh --cluster-mode, persisted to vars.sh) therefore
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
  # The shape the apply will actually use — probed when a cluster exists, the
  # requested one only on a fresh create. install.sh reports this rather than
  # the flag, so an adoption never claims to have built what it did not.
  TFVARS_CLUSTER_MODE="$cluster_mode"
  export TFVARS_CLUSTER_MODE

  # Installing onto an existing cluster: fetch its credentials now, before the
  # recovery loop below — adoption is exactly the case where the credentials
  # live only in that cluster's Secret (a fresh clone has no vars.sh values),
  # and recovery is gated on the kubectl context actually being this cluster.
  if [ "$create_cluster" = "false" ] && command -v kubectl >/dev/null 2>&1; then
    gcloud container clusters get-credentials "${CLUSTER_NAME}" --location "${REGION}" \
      --project "${PROJECT_ID}" >/dev/null 2>&1 || true
  fi

  # vars.sh does not always carry the credentials: PERSIST_SECRETS_ON_DISK=false
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
  local expected_ctx="gke_${PROJECT_ID}_${REGION}_${CLUSTER_NAME}"
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
      secret_val="$({ kubectl get secret platform-agent-secrets -n "${NAMESPACE:-kubeagents-system}" \
        --request-timeout=10s \
        -o jsonpath="{.data.${secret_key}}" 2>/dev/null || true; } | base64 --decode 2>/dev/null || true)"
      if [ -n "$secret_val" ]; then
        export "${secret_key}=${secret_val}"
        print_info "Recovered ${secret_key} from the live 'platform-agent-secrets' Secret (vars.sh does not persist it)."
      fi
    done
  fi

  # The composition requires api_server_key non-empty, so fail here with the
  # recovery path spelled out rather than aborting the caller on an opaque
  # unbound-variable error under set -u.
  if [ -z "${API_SERVER_KEY:-}" ]; then
    print_error "API_SERVER_KEY is not set, vars.sh does not carry it (PERSIST_SECRETS_ON_DISK=false keeps it out), and it could not be recovered from the live Secret."
    print_info "Recover it and re-run: export API_SERVER_KEY=\"\$(kubectl get secret platform-agent-secrets -n kubeagents-system -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode)\""
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
    if kubectl get deployment cert-manager -n cert-manager >/dev/null 2>&1; then
      enable_cert_manager="false"
      print_info "cert-manager already runs on '${CLUSTER_NAME}'; the composition will not install its own."
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
  if [ -n "${GITHUB_ORG:-}" ] && [ -n "${GITHUB_REPO:-}" ] && [ -n "${GITHUB_APP_ID:-}" ]; then
    local minter_key_version=""
    minter_key_version="$({ gcloud kms keys versions list \
      --key "${KMS_KEY:-github-token-minter-key}" \
      --keyring "${KMS_KEYRING:-github-token-minter-keyring}" \
      --location "$(derive_kms_location "${REGION}")" \
      --project "${PROJECT_ID}" --filter='state=ENABLED' \
      --format='value(name)' 2>/dev/null || true; } | head -1)"
    if [ -n "$minter_key_version" ] || [ -f "${GITHUB_PEM_PATH:-}" ]; then
      enable_github_minter="true"
    else
      print_warning "GitHub minter deferred: its KMS signing key has no ENABLED version, no App private key PEM is at hand (GITHUB_PEM_PATH), and a minter deployed without the key never passes readiness."
      print_info "Provide the PEM (or import the key: k8s-operator/config/integrations/github/README.md) and re-run — the next run adds the minter to the existing install."
    fi
  fi

  # ENABLE_GVISOR is one intent — run the agent sandboxed — and the two things
  # that satisfy it differ by cluster shape. Standard needs the sandbox node
  # pool AND the RuntimeClass on the pod; Autopilot ships the gvisor
  # RuntimeClass natively and has no pool to manage, so asking the gke-cluster
  # module for one there fails the plan. Deriving both from the probed
  # cluster_mode keeps --gvisor=true meaning the same thing on either shape.
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
      # --cluster-mode=autopilot --gvisor=true install.
      print_info "Creating Autopilot cluster '${CLUSTER_NAME}': using its built-in gvisor RuntimeClass, with no sandbox node pool to provision."
    else
      # Autopilot's gvisor RuntimeClass arrived in a specific GKE version, and
      # asking an older cluster for it fails late and unrecognisably: the
      # operator stops at its RuntimeClass check before writing the agent
      # Deployment, the Helm release still reports success because that
      # Deployment is operator-created rather than chart-rendered, and
      # install.sh's post-apply gate exits 1 with "Expected deployment
      # 'platform-agent-gateway' was not created" — after the cluster, IAM,
      # KMS, cert-manager and the release have all been applied, naming
      # nothing about a RuntimeClass. Refuse here instead, which is where the
      # gke-cluster module's precondition refuses the equivalent Standard
      # mistake. This branch is reached only when the describe above found a
      # live Autopilot cluster, so there is always one to ask.
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
        print_info "Upgrade the cluster, or re-run with --gvisor=false. Continuing would apply every GCP and Helm resource and then fail on a missing agent Deployment."
        return 1
      fi
      print_info "Cluster '${CLUSTER_NAME}' is Autopilot: using its built-in gvisor RuntimeClass, with no sandbox node pool to provision."
    fi
  fi

  local old_umask
  old_umask="$(umask)"
  umask 077
  {
    echo "# Generated by the kube-agents installer from vars.sh — regenerated on every"
    echo "# run. Change settings through install.sh (or its --menu) rather than here."
    echo "project_id   = $(hcl_str "${PROJECT_ID}")"
    echo "cluster_name = $(hcl_str "${CLUSTER_NAME}")"
    echo "location     = $(hcl_str "${REGION}")"
    echo ""
    echo "# The DNS endpoint is open and deletion protection is off. cluster_mode is"
    echo "# the live cluster's own shape whenever there is one to probe, and the"
    echo "# --cluster-mode the install asked for only on a create."
    echo "cluster_mode               = $(hcl_str "${cluster_mode}")"
    echo "create_cluster             = ${create_cluster}"
    echo "allow_external_dns_traffic = true"
    echo "deletion_protection        = false"
    echo "enable_gvisor_node_pool    = ${gvisor_node_pool}"
    echo "gvisor_pool_name           = $(hcl_str "${GVISOR_POOL_NAME:-gvisor-pool}")"
    echo "agent_runtime_class        = $(hcl_str "${agent_runtime_class}")"
    echo "enable_cert_manager        = ${enable_cert_manager}"
    echo ""
    echo "image_tag                  = $(hcl_str "${image_tag}")"
    echo "image_registry             = $(hcl_str "${image_registry}")"
    echo "third_party_image_registry = $(hcl_str "${THIRD_PARTY_REGISTRY_PREFIX:-}")"
    echo ""
    echo "model_provider     = $(hcl_str "${MODEL_PROVIDER:-$DEFAULT_MODEL_PROVIDER}")"
    echo "model_default_name = $(hcl_str "${MODEL_DEFAULT_NAME:-}")"
    echo "vertex_project_id  = $(hcl_str "${VERTEX_PROJECT_ID:-}")"
    echo "vertex_location    = $(hcl_str "${VERTEX_LOCATION:-}")"
    echo ""
    if is_truthy "${PERSIST_SECRETS_ON_DISK:-true}"; then
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
    echo "permission_set = $(hcl_str "${PLATFORM_AGENT_PERMISSION_SET:-read-only}")"
    if [ "${PLATFORM_AGENT_PERMISSION_SET:-}" = "custom" ]; then
      echo "project_roles  = $(hcl_csv_list "${PLATFORM_AGENT_CUSTOM_ROLES:-}")"
    fi
    echo ""
    echo "enable_google_chat        = $(hcl_bool "${GOOGLE_CHAT_ENABLED:-false}")"
    echo "chat_topic_name           = $(hcl_str "${CHAT_TOPIC_NAME:-platform-agent-chat-events}")"
    echo "chat_subscription_name    = $(hcl_str "${CHAT_SUB_NAME:-platform-agent-chat-events-sub}")"
    echo "google_chat_allowed_users = $(hcl_csv_list "${ALLOWED_USERS:-}")"
    echo "google_chat_mode          = $(hcl_str "${GOOGLE_CHAT_MODE:-default}")"
    echo ""
    echo "enable_slack            = $(hcl_bool "${SLACK_ENABLED:-false}")"
    if is_truthy "${PERSIST_SECRETS_ON_DISK:-true}"; then
      echo "slack_bot_token         = $(hcl_str "${SLACK_BOT_TOKEN:-}")"
      echo "slack_app_token         = $(hcl_str "${SLACK_APP_TOKEN:-}")"
    fi
    echo "slack_allowed_users     = $(hcl_csv_list "${SLACK_ALLOWED_USERS:-}")"
    echo "slack_home_channel      = $(hcl_str "${SLACK_HOME_CHANNEL:-}")"
    echo "slack_home_channel_name = $(hcl_str "${SLACK_HOME_CHANNEL_NAME:-}")"
    echo ""
    if [ -n "${GITHUB_ORG:-}" ] && [ -n "${GITHUB_REPO:-}" ]; then
      echo "github_repo = $(hcl_str "${GITHUB_ORG}/${GITHUB_REPO}")"
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
    echo "enable_gke_backup_plan = $(hcl_bool "${ENABLE_GKE_BACKUP_PLAN:-false}")"
    echo ""
    echo "# The CRD defaults dashboardEnabled to true; the installer has always"
    echo "# defaulted it to false and asks. Memory settings mirror --memory."
    echo "hermes_dashboard_enabled = $(hcl_bool "${HERMES_DASHBOARD_ENABLED:-false}")"
    echo "memory_enabled           = $(hcl_bool "${MEMORY_ENABLED:-false}")"
    echo "memory_provider          = $(hcl_str "${MEMORY_PROVIDER:-multiuser_memory}")"
    echo "user_profile_enabled     = $(hcl_bool "${USER_PROFILE_ENABLED:-false}")"
  } > "${dest}.tmp"
  chmod 600 "${dest}.tmp"
  mv -f -- "${dest}.tmp" "$dest"
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
