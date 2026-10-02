#!/usr/bin/env bash
#
# Makes `apply` and `destroy` repeatable for this composition.
#
# Several things in this stack are not symmetric — applying them is not the
# inverse of destroying them — and every one of them turns the second `terraform
# apply` of a project's life into a failure. Terraform cannot express any of
# them, so they live here rather than in a README telling you to remember them:
#
#   1. Cloud KMS key rings and crypto keys CANNOT be deleted, ever. `terraform
#      destroy` drops them from state and leaves them in the project, so the next
#      apply fails with a 409 — and worse, destroying the crypto key SCHEDULES ITS
#      VERSIONS for destruction, so even an imported key cannot encrypt and the
#      cluster refuses to come back. `destroy` forgets them from state first so
#      Terraform never touches them; `adopt-kms` imports them back and restores
#      any version a bare `terraform destroy` already scheduled.
#   2. The PlatformAgent CR carries a finalizer only the operator can clear, and
#      `terraform destroy` removes the CR and the operator together. The chart's
#      pre-delete hook handles the ordinary case; this deletes the CR up front so
#      the hook is a fast no-op, and force-clears the finalizer if the operator is
#      already gone or wedged.
#   3. A GKE BackupPlan cannot be deleted while it still owns backups.
#   4. The cluster is created with deletion_protection = true, which a destroy
#      cannot override on its own — the attribute has to be applied as false first.
#   5. A Pub/Sub topic or subscription that already exists makes the create 409.
#      Reachable on a FIRST install too: configuring the Chat app in the Cloud
#      console creates the topic before the installer ever runs. `adopt_pubsub`
#      imports whichever of the two is already there before applying.
#   6. The agent GSA (account_id) is ForceNew. A lost agent_service_account_id
#      override in install.env resolves back to the default name and plans a
#      destructive replacement under -auto-approve. `guard_gsa_identity` refuses
#      the apply before Terraform runs. The release namespace is ForceNew the
#      same way; `guard_release_namespace` refuses a move on apply and destroy.
#   7. A state left by an interrupted install can hold the cluster's CMEK
#      entries next to a create_cluster that the retry computes as false, and
#      the module then plans their destruction. `forget_unmanaged_cluster_kms`
#      drops them from state first; adopt_kms brings them back when the state
#      creates a cluster again. `guard_cluster_ownership` refuses the other
#      shape, a create over a cluster that already exists.
#   8. The CMEK key ring and crypto key names are ForceNew as well, and the
#      installer writes them into terraform.tfvars from install.env. A changed
#      or lost GKE_DB_KMS_KEYRING / GKE_DB_KMS_KEY on a Terraform-created
#      cluster plans the key's replacement and schedules the live key's
#      versions for destruction under -auto-approve. `guard_kms_identity`
#      refuses the apply first.
#
# Usage:
#   ./lifecycle.sh apply    [extra terraform args...]
#   ./lifecycle.sh plan     [extra terraform args...]
#   ./lifecycle.sh destroy  [extra terraform args...]
#   ./lifecycle.sh adopt-kms
#
# `plan` reports what an apply would change and touches nothing — no state lock,
# no state bucket creation, no adoption imports. Pass -detailed-exitcode to get
# 0 for "in sync" and 2 for "there are changes".
#
# `plan`, `apply` and `destroy` hide helm_release's `metadata` block, which
# may contain secrets, from Terraform's output; an `apply` that will ask for
# approval at a terminal is left as is, so its prompt shows. A raw
# `terraform` run on this composition prints the block.
#
# Remote state (opt-in): set KUBE_AGENTS_STATE_BUCKET to a GCS bucket name, or
# to "auto" for <project_id>-kube-agents-tfstate. On `apply` and `destroy` the
# bucket is created if missing (versioned, uniform access); `plan` creates
# nothing and fails instead, per its read-only contract above. A gitignored
# backend_override.tf points Terraform at gs://<bucket>/<KUBE_AGENTS_STATE_PREFIX,
# default kube-agents/<cluster_name>>. Unset, state stays local as before.
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m %s\n' "$*" >&2; }

# The repository's install defaults, for what this script has to agree on with
# the front doors and cannot read from terraform.tfvars: where the state lives
# (the bucket a KUBE_AGENTS_STATE_BUCKET of "auto" derives and the prefix under
# it) and the agent GSA's default name, which a hand-written tfvars leaves
# null. The composition sources its modules from ../../modules, so this script
# already runs only inside the repository, and the file is three levels up for
# the same reason installer_common.sh finds it two up. Sourced without `set -a`,
# as everywhere: defaults, not the install's configuration.
INSTALL_DEFAULTS_FILE="${KUBE_AGENTS_INSTALL_DEFAULTS:-../../../install.defaults.env}"
if [[ -r "$INSTALL_DEFAULTS_FILE" ]]; then
  # shellcheck source=../../../install.defaults.env
  . "$INSTALL_DEFAULTS_FILE"
else
  warn "cannot find the install defaults at ${INSTALL_DEFAULTS_FILE}; they ship with the repository (or point KUBE_AGENTS_INSTALL_DEFAULTS at a copy)."
  return 1 2>/dev/null || exit 1
fi

# gke_dns_endpoint_flag: whether a given cluster is reached over its IP or its
# DNS control-plane endpoint. Three levels up like the defaults above, and
# resolved the same way, since this script runs only inside the repository.
#
# This is the composition's one dependency on scripts/installer/. The helper is
# deliberately free of that directory's state file and print helpers so it can
# be sourced from anywhere — hack/ci-env.sh and scripts/release/common.sh
# already do — and teardown has to reach a cluster over the same endpoint the
# install used. A local copy of the predicate would be the alternative, and it
# would drift.
#
# Absent, a stub keeps the pre-helper command rather than stopping the run: the
# defaults above decide what gets applied, while this only picks an endpoint to
# dial, and a teardown is the worst place to refuse over the difference.
GKE_DNS_ENDPOINT_HELPER="../../../scripts/installer/gke_dns_endpoint.sh"
if [[ -r "$GKE_DNS_ENDPOINT_HELPER" ]]; then
  # shellcheck source=../../../scripts/installer/gke_dns_endpoint.sh
  . "$GKE_DNS_ENDPOINT_HELPER"
else
  warn "cannot find the control-plane endpoint helper at ${GKE_DNS_ENDPOINT_HELPER}; reaching clusters over their IP endpoint."
  gke_dns_endpoint_flag() { GKE_DNS_ENDPOINT_FLAG=""; }
fi

# Remote state, opt-in. The composition ships no backend block — a hand-driven
# example works fine on local state — but an installer-driven one cannot:
# install.sh may run from a disposable clone, and uninstall.sh and upgrade.sh
# clone fresh temporary directories, so state left on disk is state lost. With
# KUBE_AGENTS_STATE_BUCKET set, this writes a gitignored backend override
# (backend_override.tf) pointing at a GCS bucket and creates the bucket if it
# does not exist — versioned, uniform bucket-level access, in the install's
# region. "auto" derives the bucket name as <project_id>-kube-agents-tfstate;
# the prefix defaults to kube-agents/<cluster_name> so two installs in one
# project do not collide. Without the variable nothing here runs and local
# state behaves exactly as before.
BACKEND_OVERRIDE_FILE="backend_override.tf"

# The second import-time override, written into the scope resolver module's
# directory — the path main.tf sources the module from — for the duration of
# each terraform import; with_override below says why and what it pins.
readonly SCOPE_RESOLVER_MODULE_DIR="../../modules/kube-agents-scope-resolver"
readonly SCOPE_OVERRIDE_FILE="$SCOPE_RESOLVER_MODULE_DIR/scope_resolver_lifecycle_override.tf"

# State addresses the guards below read. The cluster has three spellings:
# one per mode, plus the index-less autopilot address a state predating the
# mode switch still carries until its first plan renames it.
CLUSTER_ADDRESSES=(
  "module.gke_cluster.google_container_cluster.autopilot[0]"
  "module.gke_cluster.google_container_cluster.standard[0]"
  "module.gke_cluster.google_container_cluster.autopilot"
)
readonly CLUSTER_ADDRESSES
# The cluster's CMEK resources, every one of which the gke-cluster module
# manages only alongside a cluster it creates. The ring and the key are named
# on their own because adopt_kms imports them and guard_kms_identity reads
# them; the other two ride along in the list.
readonly CLUSTER_KMS_KEYRING_ADDRESS="module.gke_cluster.google_kms_key_ring.gke_keyring[0]"
readonly CLUSTER_KMS_KEY_ADDRESS="module.gke_cluster.google_kms_crypto_key.gke_key[0]"
CLUSTER_KMS_ADDRESSES=(
  "$CLUSTER_KMS_KEY_ADDRESS"
  "$CLUSTER_KMS_KEYRING_ADDRESS"
  "module.gke_cluster.google_kms_crypto_key_iam_member.gke_kms_binding[0]"
  "module.gke_cluster.google_project_service_identity.gke_service_agent[0]"
)
readonly CLUSTER_KMS_ADDRESSES
# The token minter's signing key ring and key, adopted and forgotten the same
# way (KMS cannot delete either).
readonly MINTER_KMS_KEYRING_ADDRESS="module.github_minter[0].google_kms_key_ring.minter"
readonly MINTER_KMS_KEY_ADDRESS="module.github_minter[0].google_kms_crypto_key.minter"
# What "the minter's signing key is not there" looks like coming back from
# gcloud, as against "the guard could not ask". NOT_FOUND is the key ring or key
# itself missing. The other two are Cloud KMS not being enabled on the project,
# which on a first apply says the same thing: main.tf enables cloudkms in the
# very apply guard_minter_key runs ahead of, so a fresh project answers
# SERVICE_DISABLED where an established one answers NOT_FOUND. Same shape as
# GCS_OBJECT_ABSENT_PATTERN in scripts/installer/installer_common.sh.
readonly MINTER_KEY_ABSENT_PATTERN='NOT_FOUND|SERVICE_DISABLED|has not been used in project'
readonly HELM_RELEASE_ADDRESS="helm_release.kube_agents"
readonly CERT_MANAGER_RELEASE_ADDRESS="helm_release.cert_manager[0]"
# The releases' own `timeout = 600` in main.tf, for the uninstall that runs ahead of terraform destroy.
readonly HELM_UNINSTALL_TIMEOUT="10m"
readonly AGENT_GSA_ADDRESS="module.kube_agents_iam.google_service_account.agent"
readonly CHAT_SUBSCRIPTION_ADDRESS="module.chat_pubsub[0].google_pubsub_subscription.chat_events"
readonly STATE_LOCK_MESSAGE_PATTERN='(Acquiring|Releasing) state lock\.'
# The drift-pubsub module's three importable resources, adopted by adopt_kms
# the way the stockout trio is. Their names are read from the composition's
# drift_pubsub_topic, drift_pubsub_subscription and drift_pubsub_sink
# variables, which main.tf passes to the module, so the name adopted is always
# the name this state would create.
readonly DRIFT_TOPIC_ADDRESS="module.drift_pubsub[0].google_pubsub_topic.drift_audit"
readonly DRIFT_SUBSCRIPTION_ADDRESS="module.drift_pubsub[0].google_pubsub_subscription.drift_audit"
readonly DRIFT_SINK_ADDRESS="module.drift_pubsub[0].google_logging_project_sink.drift_audit"
# The stockout trio, the other three adopt_kms imports. It is declared at the
# composition's top level rather than in a module, so its addresses carry no
# module prefix.
readonly STOCKOUT_TOPIC_ADDRESS="google_pubsub_topic.stockout_alerts[0]"
readonly STOCKOUT_SUBSCRIPTION_ADDRESS="google_pubsub_subscription.stockout_alerts[0]"
readonly STOCKOUT_SINK_ADDRESS="google_logging_project_sink.stockout_alerts[0]"

# Every Pub/Sub subscription this composition can manage, as
# "<enable flag>|<state address>|<name variable>|<topic variable>|<what a
# recreate drops>|<install.env name key>|<install.env topic key>".
#
# The install.env keys are empty for the drift and stockout trios on purpose:
# install.env.example carries CHAT_TOPIC_NAME and CHAT_SUB_NAME and nothing for
# the other four, so subscription_name_advice points those at the TF_VAR_
# passthrough instead of naming a key that does not exist.
readonly GUARDED_SUBSCRIPTIONS=(
  "enable_google_chat|$CHAT_SUBSCRIPTION_ADDRESS|chat_subscription_name|chat_topic_name|unacknowledged Google Chat events|CHAT_SUB_NAME|CHAT_TOPIC_NAME"
  "enable_drift_pubsub|$DRIFT_SUBSCRIPTION_ADDRESS|drift_pubsub_subscription|drift_pubsub_topic|unacknowledged GKE audit records, the out-of-band changes the drift detector exists to report||"
  "enable_stockout_investigator|$STOCKOUT_SUBSCRIPTION_ADDRESS|stockout_pubsub_subscription|stockout_pubsub_topic|unacknowledged stockout alerts||"
)

#
# One argument, "readonly", suppresses the bucket creation for `plan`. A plan
# is meant to report on an install without changing anything about it, and a
# plan that creates the state bucket has both changed the project and answered
# the wrong question — an empty bucket plans the whole composition as new,
# which reads as total drift when the truth is that this is not the install's
# backend at all.
ensure_backend() {
  local mode="${1:-}"
  [[ -n "${KUBE_AGENTS_STATE_BUCKET:-}" ]] || return 0

  # project/cluster/location come from tfvars, which terraform console only
  # serves from an initialized directory — so init once without a backend
  # before the backend can be described.
  terraform init -backend=false -input=false >/dev/null || {
    warn "terraform init -backend=false failed; run it by hand to see why"
    exit 1
  }

  local project bucket prefix region
  project=$(tfvar project_id)
  bucket="$KUBE_AGENTS_STATE_BUCKET"
  # The same derivation as installer_common.sh's tf_state_bucket, from the same
  # two defaults, so the front doors and a hand-driven run name one bucket.
  [[ "$bucket" == "$DEFAULT_KUBE_AGENTS_STATE_BUCKET" ]] && bucket="${project}${DEFAULT_TF_STATE_BUCKET_SUFFIX}"
  prefix="$(state_prefix)"
  # The bucket lives where the cluster does; strip a zone suffix to its region.
  region=$(sed -E 's/-[a-z]$//' <<<"$(tfvar location)")

  if ! gcloud storage buckets describe "gs://$bucket" --project "$project" >/dev/null 2>&1; then
    if [[ "$mode" == "readonly" ]]; then
      warn "no Terraform state bucket at gs://$bucket — nothing has ever been applied here,"
      warn "or KUBE_AGENTS_STATE_BUCKET/KUBE_AGENTS_STATE_PREFIX name a different install."
      exit 1
    fi
    log "creating Terraform state bucket gs://$bucket in $region (versioned, uniform access)"
    gcloud storage buckets create "gs://$bucket" --project "$project" \
      --location "$region" --uniform-bucket-level-access >/dev/null
    # Versioning is what makes a corrupted or mistakenly-overwritten state
    # recoverable; a state bucket without it is a single point of failure.
    gcloud storage buckets update "gs://$bucket" --versioning >/dev/null
  fi

  local desired
  desired=$(printf 'terraform {\n  backend "gcs" {\n    bucket = "%s"\n    prefix = "%s"\n  }\n}\n' \
    "$bucket" "$prefix")
  if [[ ! -f "$BACKEND_OVERRIDE_FILE" ]] || [[ "$(cat "$BACKEND_OVERRIDE_FILE")" != "$desired" ]]; then
    printf '%s' "$desired" >"$BACKEND_OVERRIDE_FILE"
    log "state backend: gs://$bucket/$prefix"
    # -reconfigure, not -migrate-state: the installer path never has local
    # state worth carrying, and migrating whatever happens to sit in a reused
    # checkout into the bucket is how an unrelated experiment overwrites a
    # real install's state.
    terraform init -input=false -reconfigure >/dev/null || {
      warn "terraform init -reconfigure against gs://$bucket failed"
      exit 1
    }
  fi
}

# Where this install's state lives under the bucket. One spelling, shared by
# the backend override and the messages that tell an operator what to clear;
# installer_common.sh's tf_state_prefix derives the front doors' answer from
# the same default.
state_prefix() {
  echo "${KUBE_AGENTS_STATE_PREFIX:-${DEFAULT_TF_STATE_PREFIX_ROOT}/$(tfvar cluster_name)}"
}

# Runs before anything reads the configuration. init is idempotent and cheap
# when nothing changed, and skipping it is how a routine `git pull` that adds a
# module turns every subcommand below into a failure.
ensure_init() {
  log "initializing Terraform..."
  ensure_backend "${1:-}"
  terraform init -input=false >/dev/null || {
    warn "terraform init failed; run it by hand to see why"
    exit 1
  }
}

# Reads a resolved input variable. terraform console loads terraform.tfvars the
# same way apply does, so defaults and overrides are honoured without this script
# re-implementing Terraform's precedence rules.
#
# The error is printed rather than discarded. With stderr sent to /dev/null a
# failing console left an empty value, `set -e` killed the script on the
# assignment, and the run ended with no output whatsoever — which is exactly what
# an uninitialised module did before ensure_init existed.
#
# A variable nobody set and whose default is null -- agent_service_account_id
# in a hand-written tfvars -- prints as `tostring(null)` (a typed null; older
# releases print `null`). Callers want "unset", not that spelling: read as a
# name, it made guard_gsa_identity refuse every apply whose tfvars left the
# variable alone, which is what broke the autopush deploys after #1309.
tfvar() {
  local out value
  if ! out=$(echo "var.$1" | terraform console 2>&1); then
    printf '%s\n' "$out" >&2
    warn "could not evaluate var.$1 (see the terraform error above)"
    exit 1
  fi
  value=$(printf '%s\n' "$out" | grep -vE "$STATE_LOCK_MESSAGE_PATTERN" | grep -v '^[[:space:]]*$' | tail -1 | tr -d '"')
  case "$value" in
    null | "tostring(null)") value="" ;;
  esac
  printf '%s\n' "$value"
}

# The state list is read once and matched in memory. Piping it straight into
# `grep -q` looks equivalent but is not: grep exits at the first match, terraform
# dies of SIGPIPE, and `set -o pipefail` reports the whole pipeline as failed — so
# an address that IS in state reads as absent purely because it sorts early.
#
# Read once per run and reused: every guard calls load_state, and a state list
# is a backend round-trip each time. Anything that writes state -- import,
# state rm, the targeted apply -- calls state_changed first, so the next
# load_state reads again rather than trusting a snapshot it just invalidated.
STATE_LIST=""
STATE_LIST_FRESH=false
load_state() {
  [[ "$STATE_LIST_FRESH" == "true" ]] && return 0
  STATE_LIST=$(terraform state list 2>/dev/null || true)
  STATE_LIST_FRESH=true
}
state_changed() { STATE_LIST_FRESH=false; }
in_state() { grep -Fxq "$1" <<<"$STATE_LIST"; }

# The recorded value of a string attribute of a resource in state, for the
# guards that compare it against the configuration. `head -1` keeps the
# resource's own attribute when a nested block repeats the name (a key's
# `name` before its `primary` version's): terraform state show prints the
# top-level attributes first.
state_attr() {
  terraform state show -no-color "$1" 2>/dev/null |
    sed -n "s/^ *$2 *= *\"\([^\"]*\)\".*/\1/p" | head -1
}

# terraform import configures every provider, and the helm provider here is built
# from module.gke_cluster.cluster_endpoint — unknown until the cluster exists. On
# a fresh apply that makes import impossible ("configuration ... depends on values
# that cannot be determined until apply") precisely when it is needed. A temporary
# override pins the provider at a placeholder for the duration; it is never used to
# talk to anything, because import performs no Helm operation.
OVERRIDE_FILE="providers_lifecycle_override.tf"

# The same import walk has a second casualty. It gives every resource not yet
# in state an unknown value, data sources included, and the scope resolver
# module keys its monitored-project lookup's for_each on the metrics-scope
# read, so the key set is unknown and Terraform refuses every import before it
# calls the provider ("Invalid for_each argument"), with or without a scope
# declared. A second override pins that for_each to an empty set, and pins the
# module's `members` output to an empty member list under each declared
# selector's name: the IAM module keys its scope bindings on that output,
# which the same walk leaves unknown once a Shared VPC host or Metrics Scope
# is declared, and its precondition wants an entry per declared selector, so
# a bare {} would put a "Resource precondition failed" warning on every
# import. Import plans nothing (it refreshes the reads whose keys are known
# and changes no resource), so what the selectors resolve to is consulted
# by nothing while the file exists. Terraform merges
# *_override.tf per module directory, so the file goes where main.tf sources
# the module from; neither file needs an init. An override of a block the
# module no longer defines fails the import loudly, which is what a rename in
# the module should do here. SCOPE_OVERRIDE_FILE, above with the other paths,
# is where it goes.
drop_override() { rm -f "$OVERRIDE_FILE" "$SCOPE_OVERRIDE_FILE"; }

with_override() {
  cat >"$OVERRIDE_FILE" <<'EOF'
# Written by lifecycle.sh for the duration of a terraform import; always removed
# again. If you are reading this in a committed diff, something went wrong.
provider "helm" {
  kubernetes = {
    host                   = "https://127.0.0.1"
    token                  = "placeholder"
    cluster_ca_certificate = ""
  }
}
EOF
  cat >"$SCOPE_OVERRIDE_FILE" <<'EOF'
# Written by lifecycle.sh for the duration of a terraform import; always removed
# again. If you are reading this in a committed diff, something went wrong.
# A plan or apply that merged this file would resolve every declared selector
# to no members and plan the removal of the bindings those members hold;
# lifecycle.sh removes it before either runs.
data "http" "scope_monitored_project" {
  for_each = toset([])
}

output "members" {
  value = merge(
    { for host in var.shared_vpc_hosts : "sharedVpcHosts/${host}" => [] },
    { for scope in var.metrics_scopes : "metricsScopes/${scope}" => [] },
  )
}
EOF
  trap drop_override EXIT
}

# Runs an import with both overrides in place. Successful imports stay quiet;
# a failed one prints Terraform's own error under the warning, because the
# warning alone cannot say whether the resource, the credentials or the
# configuration was the problem. Only the lines from the first `Error` on are
# printed: the "Importing from ID", "Import prepared!" and "Refreshing state"
# lines above it are what a successful import prints too. An output with no
# such line is printed whole. -no-color keeps the escapes Terraform writes
# even into a pipe out of the log.
import_resource() { # <address> <id>
  local output errors line
  if output=$(terraform import -input=false -no-color "$1" "$2" 2>&1); then
    return 0
  fi
  warn "could not import $1 ($2); the apply will fail with a 409"
  warn "terraform import said:"
  errors=$(awk '/Error/ { found = 1 } found' <<<"$output")
  [[ -n "$errors" ]] || errors="$output"
  while IFS= read -r line; do printf '     %s\n' "$line"; done <<<"$errors" >&2
  return 1
}

# Destroying a google_kms_crypto_key does not delete the key — GCP will not — but
# it DOES schedule every one of its versions for destruction, which leaves the key
# present and unusable. A cluster then fails to come back with
# "Failed to test encryption operation ... is not enabled, current state is:
# DESTROY_SCHEDULED". Scheduled destruction is reversible until the destroy time,
# so anything still pending is restored and re-enabled here. `destroy` avoids
# creating this situation at all; this recovers from a bare `terraform destroy`.
restore_key_versions() {
  local id="$1" location="$2" project="$3" keyring key versions
  keyring=$(sed -E 's|.*/keyRings/([^/]+)/.*|\1|' <<<"$id")
  key="${id##*/}"

  versions=$(gcloud kms keys versions list --key "$key" --keyring "$keyring" \
    --location "$location" --project "$project" --filter='state=DESTROY_SCHEDULED' \
    --format='value(name.basename())' 2>/dev/null || true)
  [[ -n "$versions" ]] || return 0

  while read -r version; do
    [[ -n "$version" ]] || continue
    log "restoring key version $key/$version (was DESTROY_SCHEDULED)"
    # restore lands the version in DISABLED; it has to be enabled separately.
    gcloud kms keys versions restore "$version" --key "$key" --keyring "$keyring" \
      --location "$location" --project "$project" >/dev/null 2>&1 &&
      gcloud kms keys versions enable "$version" --key "$key" --keyring "$keyring" \
        --location "$location" --project "$project" >/dev/null 2>&1 ||
      warn "could not restore $key/$version; CMEK will fail until it is enabled"
  done <<<"$versions"
}

adopt_kms() {
  local project location keyring key
  load_state
  project=$(tfvar project_id)
  # KMS locations are regional; a zonal cluster location maps to its region,
  # matching the modules' own derivation.
  location=$(sed -E 's/-[a-z]$//' <<<"$(tfvar location)")

  # address <TAB> gcloud-kind <TAB> resource id
  local -a targets=()

  # With create_cluster = false the module manages no KMS resources — CMEK on
  # an existing cluster is the caller's gcloud step — so there is nothing to
  # adopt for the cluster half.
  if [[ "$(tfvar create_cluster)" != "false" && "$(tfvar enable_database_encryption)" != "false" ]]; then
    keyring=$(tfvar kms_keyring_name)
    key=$(tfvar kms_key_name)
    targets+=(
      "$CLUSTER_KMS_KEYRING_ADDRESS	keyring	projects/$project/locations/$location/keyRings/$keyring"
      "$CLUSTER_KMS_KEY_ADDRESS	key	projects/$project/locations/$location/keyRings/$keyring/cryptoKeys/$key"
    )
  fi

  if [[ "$(tfvar enable_github_minter)" == "true" ]]; then
    local minter_keyring minter_key
    minter_keyring=$(tfvar github_minter_kms_keyring)
    minter_key=$(tfvar github_minter_kms_key)
    targets+=(
      "$MINTER_KMS_KEYRING_ADDRESS	keyring	projects/$project/locations/$location/keyRings/$minter_keyring"
      "$MINTER_KMS_KEY_ADDRESS	key	projects/$project/locations/$location/keyRings/$minter_keyring/cryptoKeys/$minter_key"
    )
  fi

  if [[ "$(tfvar enable_stockout_investigator)" == "true" ]]; then
    # Each of the three variables has a default in variables.tf, and tfvar
    # exits rather than returning empty, so no fallback is spelled here.
    local stockout_topic stockout_sub stockout_sink
    stockout_topic=$(tfvar stockout_pubsub_topic)
    stockout_sub=$(tfvar stockout_pubsub_subscription)
    stockout_sink=$(tfvar stockout_pubsub_sink)
    targets+=(
      "$STOCKOUT_TOPIC_ADDRESS	pubsub_topic	projects/$project/topics/$stockout_topic"
      "$STOCKOUT_SUBSCRIPTION_ADDRESS	pubsub_sub	projects/$project/subscriptions/$stockout_sub"
      "$STOCKOUT_SINK_ADDRESS	logging_sink	projects/$project/sinks/$stockout_sink"
    )
  fi

  if [[ "$(tfvar enable_drift_pubsub)" == "true" ]]; then
    # Adoption is by name, and the names are one fixed default per project,
    # so an install that shares a project with another one names its own trio
    # (the README's second-install section); this block cannot tell a
    # resource an earlier install left behind from one another live install
    # owns. As with the stockout trio, each variable has a default, so tfvar
    # never returns empty here.
    local drift_topic drift_sub drift_sink
    drift_topic=$(tfvar drift_pubsub_topic)
    drift_sub=$(tfvar drift_pubsub_subscription)
    drift_sink=$(tfvar drift_pubsub_sink)
    targets+=(
      "$DRIFT_TOPIC_ADDRESS	pubsub_topic	projects/$project/topics/$drift_topic"
      "$DRIFT_SUBSCRIPTION_ADDRESS	pubsub_sub	projects/$project/subscriptions/$drift_sub"
      "$DRIFT_SINK_ADDRESS	logging_sink	projects/$project/sinks/$drift_sink"
    )
  fi

  # Skipping both halves — cluster KMS (create_cluster or database encryption
  # off) and the minter — leaves targets empty, and macOS's bash 3.2 treats an
  # empty array expansion as unbound under `set -u`. The ${arr[@]+...} form
  # expands to nothing instead, so the loop runs zero times and the tail below
  # still clears any stale import override and logs what happened.
  local adopted=0 address kind id
  for target in ${targets[@]+"${targets[@]}"}; do
    IFS=$'\t' read -r address kind id <<<"$target"

    if in_state "$address"; then
      continue
    fi

    case "$kind" in
      keyring)      gcloud kms keyrings describe "${id##*/}" --location "$location" \
                      --project "$project" >/dev/null 2>&1 || continue ;;
      key)          gcloud kms keys describe "${id##*/}" --location "$location" \
                      --keyring "$(echo "$id" | sed -E 's|.*/keyRings/([^/]+)/.*|\1|')" \
                      --project "$project" >/dev/null 2>&1 || continue ;;
      pubsub_topic) gcloud pubsub topics describe "${id##*/}" \
                      --project "$project" >/dev/null 2>&1 || continue ;;
      pubsub_sub)   gcloud pubsub subscriptions describe "${id##*/}" \
                      --project "$project" >/dev/null 2>&1 || continue ;;
      logging_sink) gcloud logging sinks describe "${id##*/}" \
                      --project "$project" >/dev/null 2>&1 || continue ;;
    esac

    log "adopting pre-existing resource: $id"
    [[ -f "$OVERRIDE_FILE" && -f "$SCOPE_OVERRIDE_FILE" ]] || with_override
    state_changed
    if import_resource "$address" "$id"; then
      adopted=$((adopted + 1))
      if [[ "$kind" == "key" ]]; then
        restore_key_versions "$id" "$location" "$project"
      fi
    fi
  done

  drop_override
  trap - EXIT
  log "resource adoption complete: $adopted imported"
}

# Pub/Sub topics and subscriptions are deletable, so they are not undeletable
# the way a KMS key ring is — but they are routinely created outside this
# composition, and creating one that exists is a hard 409 rather than a no-op:
#
#   Error 409: Resource already exists in the project
#     with module.chat_pubsub[0].google_pubsub_topic.chat_events
#
# Two ways in, and neither is a mistake. Configuring the Google Chat app in the
# Cloud console walks you through creating the topic before you ever run the
# installer; and an earlier install in the same project leaves both behind
# whenever the destroy did not reach them. The install then cannot proceed at
# all until someone deletes a topic by hand, which is not a thing to ask of a
# scheduled reconcile running unattended.
#
# Adopting is safe in a way that deleting is not: import moves an existing
# resource under management without touching it, and the apply that follows
# reconciles its settings. A topic this composition already manages is skipped
# by the in_state check, so a steady-state apply does no gcloud work here.
adopt_pubsub() {
  [[ "$(tfvar enable_google_chat)" == "true" ]] || return 0

  local project topic sub
  load_state
  project=$(tfvar project_id)
  topic=$(tfvar chat_topic_name)
  sub=$(tfvar chat_subscription_name)

  # The subscription is listed after the topic on purpose, and is skipped
  # unless the topic is in state by the time its turn comes. A subscription
  # whose topic Terraform is about to CREATE has a `topic` attribute that is
  # about to change, so importing it produces a plan that immediately replaces
  # it — which is the outcome adopting was meant to avoid. That happens
  # whenever the topic is absent from GCP while the subscription is not: a
  # half-finished console setup, or a destroy that stopped partway.
  #
  # address <TAB> gcloud-kind <TAB> resource id <TAB> requires-in-state
  local topic_address="module.chat_pubsub[0].google_pubsub_topic.chat_events"
  local -a targets=(
    "$topic_address	topics	projects/$project/topics/$topic	"
    "module.chat_pubsub[0].google_pubsub_subscription.chat_events	subscriptions	projects/$project/subscriptions/$sub	$topic_address"
  )

  local adopted=0 address kind id requires
  for target in "${targets[@]}"; do
    IFS=$'\t' read -r address kind id requires <<<"$target"

    in_state "$address" && continue
    if [[ -n "$requires" ]] && ! in_state "$requires"; then
      log "not adopting $id: its topic is not under management, so importing it would plan a replacement"
      continue
    fi
    gcloud pubsub "$kind" describe "${id##*/}" --project "$project" >/dev/null 2>&1 || continue

    log "adopting existing Pub/Sub resource: $id"
    [[ -f "$OVERRIDE_FILE" && -f "$SCOPE_OVERRIDE_FILE" ]] || with_override
    state_changed
    if import_resource "$address" "$id"; then
      adopted=$((adopted + 1))
      # STATE_LIST is a snapshot taken by load_state above, so the
      # subscription's check below would not see the topic this import just
      # added without re-reading it.
      load_state
    fi
  done

  drop_override
  trap - EXIT
  [[ "$adopted" -eq 0 ]] || log "Pub/Sub adoption complete: $adopted imported"
}

# create_cluster = false means "somebody else's cluster" — but if THIS state
# already manages the cluster, flipping the variable off does not hand the
# cluster back: it removes the resource from configuration, and the next apply
# plans the cluster's destruction. The installer derives create_cluster from a
# liveness probe, so a re-run against an install whose cluster Terraform
# created is exactly the run that would hit this.
#
# The other direction is guarded too. create_cluster = true with the cluster
# absent from state is a create -- and a create over a cluster that already
# exists is a 409 halfway through the apply, after the IAM and KMS resources
# ahead of it have been applied. The installer derives create_cluster from
# the state object, and state left by an interrupted run can answer "ours"
# for a cluster it never finished creating (#1296); a hand-written tfvars can
# say the same by mistake. Either way the remedy is a decision, not a retry,
# so it is refused here with the choices spelled out.
guard_cluster_ownership() {
  load_state
  local addr managed=false
  for addr in "${CLUSTER_ADDRESSES[@]}"; do
    in_state "$addr" && managed=true && break
  done

  if [[ "$(tfvar create_cluster)" == "false" ]]; then
    [[ "$managed" == "true" ]] || return 0
    # Which cluster the entry names decides the remedy. Under a shared custom
    # KUBE_AGENTS_STATE_PREFIX the state can manage some OTHER cluster, and
    # "set create_cluster = true" would then plan that one's replacement.
    local recorded cluster
    recorded=$(state_attr "$addr" name)
    cluster=$(tfvar cluster_name)
    if [[ -n "$recorded" && -n "$cluster" && "$recorded" != "$cluster" ]]; then
      warn "create_cluster is false, and this state manages a DIFFERENT cluster, '$recorded' ($addr), not '$cluster'."
      warn "Applying now would plan the replacement of '$recorded'. Two installs are sharing one state prefix"
      warn "($(state_prefix)); give this install its own KUBE_AGENTS_STATE_PREFIX, or unset it to take the per-cluster default."
      exit 1
    fi
    warn "create_cluster is false, but this state already manages the cluster ($addr)."
    warn "Applying now would plan the cluster's DESTRUCTION. Set create_cluster = true"
    warn "in terraform.tfvars — this state created the cluster, so it is Terraform's to keep."
    exit 1
  fi

  [[ "$managed" == "false" ]] || return 0
  local cluster location project
  cluster=$(tfvar cluster_name)
  location=$(tfvar location)
  project=$(tfvar project_id)
  gcloud container clusters describe "$cluster" --location "$location" \
    --project "$project" --format='value(name)' >/dev/null 2>&1 || return 0
  warn "create_cluster is true, but cluster '$cluster' already exists in $project/$location and this state does not manage it."
  warn "Applying now would try to CREATE it and fail with a 409 after the resources ahead of it have applied."
  warn "If the cluster is somebody else's to install onto, set create_cluster = false."
  warn "If this state created it and lost it, import it back first (the mode's address from ${CLUSTER_ADDRESSES[0]%%.google*}):"
  warn "  terraform import 'module.gke_cluster.google_container_cluster.<autopilot|standard>[0]' projects/$project/locations/$location/clusters/$cluster"
  warn "with the two override files this script writes for its own imports in place first ($OVERRIDE_FILE and"
  warn "$SCOPE_OVERRIDE_FILE; README.md's BackupPlan import recipe writes both), or the import is refused"
  warn "on the helm provider's unknown endpoint and the scope resolver's for_each before it reaches the cluster."
  warn "Through install.sh, run uninstall.sh or clear the state under gs://<bucket>/$(state_prefix)/ and re-run"
  warn "install.sh, which derives create_cluster from the state and adopts the cluster."
  exit 1
}

# The release's namespace is ForceNew on helm_release, and the installer now
# writes it into terraform.tfvars from install.env's NAMESPACE. An install
# whose configuration names a different namespace from the one its release
# runs in -- a key that used to reach nothing, or a stray NAMESPACE in the
# shell -- would otherwise have the release destroyed and recreated elsewhere
# under -auto-approve, into a namespace the agent's fixed gateway endpoint
# does not serve. Same shape as guard_gsa_identity, for the same reason.
guard_release_namespace() {
  load_state
  local addr="$HELM_RELEASE_ADDRESS"
  in_state "$addr" || return 0

  local recorded
  recorded=$(state_attr "$addr" namespace)
  [[ -n "$recorded" ]] || return 0

  local desired
  desired=$(tfvar namespace)
  [[ "$recorded" == "$desired" ]] && return 0

  warn "namespace resolved to '$desired', but this state's release runs in '$recorded' ($addr)."
  warn "Applying now would plan the release's DESTRUCTION and recreation in '$desired' under -auto-approve,"
  warn "and the agent's gateway endpoint is fixed to the release namespace, so the moved release would not work."
  warn "Set NAMESPACE=\"$recorded\" in install.env (or drop the key to take the default), or set namespace in"
  warn "terraform.tfvars for a hand-driven apply."
  exit 1
}

# account_id on google_service_account.agent is ForceNew, and the resource
# carries neither create_before_destroy nor prevent_destroy. If a custom
# override line in install.env goes missing, the next apply resolves
# agent_service_account_id back to the module default (kubeagents-platform-gsa)
# and plans the GSA's destruction and replacement under -auto-approve. If
# install #1 in the same project already holds the default name, the apply
# destroys install #2's GSA and then 409s creating the default name, leaving
# install #2 with no identity.
guard_gsa_identity() {
  load_state
  local addr="$AGENT_GSA_ADDRESS"
  in_state "$addr" || return 0

  local recorded
  recorded=$(state_attr "$addr" account_id)
  [[ -n "$recorded" ]] || return 0

  # The front doors always write agent_service_account_id, so empty here is a
  # hand-written tfvars that left it null -- which Terraform resolves to the
  # kube-agents-iam module's default, the same name the defaults file holds.
  local desired
  if ! desired=$(tfvar agent_service_account_id 2>/dev/null); then
    desired=""
  fi
  [[ -n "$desired" ]] || desired="$DEFAULT_PLATFORM_AGENT_GSA_NAME"

  if [[ "$recorded" != "$desired" ]]; then
    warn "agent_service_account_id resolved to '$desired', but this state manages GSA '$recorded' ($addr)."
    warn "Applying now would plan the service account's DESTRUCTION and recreation under -auto-approve."
    warn "If this install uses a custom GSA name, record it in install.env, which the front doors regenerate terraform.tfvars from:"
    warn "  PLATFORM_AGENT_GSA_NAME=\"$recorded\""
    warn "A hand-driven apply sets agent_service_account_id in terraform.tfvars instead."
    exit 1
  fi
}

# Where a reader whose recorded name disagrees with the resolved one records
# it. The front doors regenerate terraform.tfvars from install.env on every
# run, so a name written into terraform.tfvars by hand does not survive them,
# and install.env is the answer either way -- but by two different routes. A
# name with a key of its own is that key. A name without one reaches Terraform
# as a TF_VAR_ passthrough, because the generator writes no line for it and so
# has nothing to overwrite it with; the enable_drift_pubsub description and the
# composition README's second-install section say the same. Naming the key that
# does not exist would send the reader nowhere, and so would omitting the
# passthrough: the likeliest way this guard fires on a front-door install is
# that TF_VAR_ line having gone missing from install.env, which is a one-line
# repair.
subscription_name_advice() { # <noun> <variable> <install.env key, empty when there is none> <recorded value>
  local noun="$1" variable="$2" env_key="$3" recorded="$4"
  if [[ -n "$env_key" ]]; then
    warn "If this install uses an existing $noun, record it in install.env, which the front doors regenerate terraform.tfvars from:"
    warn "  ${env_key}=\"${recorded}\""
    warn "A hand-driven apply sets $variable in terraform.tfvars instead."
    return 0
  fi
  warn "No install.env key carries this $noun. Through the front doors it is a passthrough line in install.env, which every front door sources with 'set -a':"
  warn "  TF_VAR_${variable}=\"${recorded}\""
  warn "A hand-driven apply sets $variable in terraform.tfvars instead."
}

# `name` and `topic` are ForceNew on google_pubsub_subscription, and `name` is
# ForceNew on google_pubsub_topic. The subscription resource carries neither
# create_before_destroy nor prevent_destroy. If a subscription is managed in
# state and its name or topic variable resolves to something else, the next
# apply destroys the live subscription (and its topic) and recreates it under
# -auto-approve, dropping whatever it had not acknowledged.
#
# Every subscription in GUARDED_SUBSCRIPTIONS is checked, not the chat one
# alone: the drift and stockout trios are the same resource type with the same
# ForceNew fields, and the drift one's unacknowledged messages are the audit
# records of out-of-band changes -- the very thing the detector reports, lost
# in a way nothing surfaces, because the detector stays Ready either way.
# Same shape as guard_gsa_identity.
guard_pubsub_subscription() {
  load_state
  local entry flag addr name_variable topic_variable loses name_key topic_key
  for entry in "${GUARDED_SUBSCRIPTIONS[@]}"; do
    IFS='|' read -r flag addr name_variable topic_variable loses name_key topic_key <<<"$entry"

    # State first: in_state reads the list already in memory, while tfvar is a
    # `terraform console` round trip, so a feature whose subscription this
    # state does not manage costs nothing.
    in_state "$addr" || continue

    # Assigned rather than compared inline. tfvar ends in `exit 1`, which
    # inside $( ) kills only the subshell, so comparing the substitution
    # directly reads a failed console as "not enabled" and skips the guard,
    # while an assignment fails under `set -e` and stops the apply. A guard
    # that cannot read the configuration has to fail closed. The name is not
    # written out here as a call: hack/check-tfvar-console.sh greps this file
    # for `$(tfvar <name>)` and evaluates every name it finds, so one in a
    # comment is a variable the composition does not declare and reds the
    # check.
    local enabled
    enabled=$(tfvar "$flag")
    [[ "$enabled" == "true" ]] || continue

    local recorded_name recorded_topic desired_name desired_topic stripped_topic
    recorded_name=$(state_attr "$addr" name)
    recorded_topic=$(state_attr "$addr" topic)

    # Every one of these variables declares a non-empty default in
    # variables.tf, so terraform console answers with that default when nothing
    # sets it and the guard needs no second copy of the name. An empty answer
    # is therefore a variable someone blanked -- a `TF_VAR_drift_pubsub_subscription=`
    # line in install.env exports "" through `set -a`, which overrides the
    # default -- and it is compared like any other mismatch rather than waved
    # through: the recreate it plans destroys the live subscription and then
    # fails on the create.
    desired_name=$(tfvar "$name_variable")
    if [[ -n "$recorded_name" && "$recorded_name" != "$desired_name" ]]; then
      warn "$name_variable resolved to '$desired_name', but this state manages Pub/Sub subscription '$recorded_name' ($addr)."
      warn "Applying now would plan the subscription's DESTRUCTION and recreation under -auto-approve,"
      warn "dropping $loses."
      [[ -n "$desired_name" ]] || warn "An empty resolution is a blanked variable rather than a rename, and the recreate would fail after the destroy had run."
      subscription_name_advice "subscription name" "$name_variable" "$name_key" "$recorded_name"
      exit 1
    fi

    desired_topic=$(tfvar "$topic_variable")
    stripped_topic="${recorded_topic##*/}"
    if [[ -n "$stripped_topic" && "$stripped_topic" != "$desired_topic" ]]; then
      warn "$topic_variable resolved to '$desired_topic', but this state's Pub/Sub subscription is attached to topic '$stripped_topic' ($addr)."
      warn "Applying now would plan the topic and subscription's DESTRUCTION and recreation under -auto-approve,"
      warn "dropping $loses."
      [[ -n "$desired_topic" ]] || warn "An empty resolution is a blanked variable rather than a rename, and the recreate would fail after the destroy had run."
      subscription_name_advice "topic name" "$topic_variable" "$topic_key" "$stripped_topic"
      exit 1
    fi
  done
}

# `name` is ForceNew on google_kms_key_ring and google_kms_crypto_key, neither
# carries prevent_destroy, and the installer writes both names into
# terraform.tfvars from install.env's GKE_DB_KMS_KEYRING / GKE_DB_KMS_KEY. On
# a cluster this state created, a name that disagrees with state -- a rotation
# attempted by renaming, or a second install's recorded line going missing --
# plans the key's destruction and recreation under -auto-approve; destroying
# the crypto key schedules every version of the LIVE key for destruction, and
# the cluster can no longer read its own etcd. Same shape as guard_gsa_identity.
# Only the create_cluster = true shape manages these entries;
# forget_unmanaged_cluster_kms owns the other one.
guard_kms_identity() {
  [[ "$(tfvar create_cluster)" != "false" ]] || return 0
  load_state
  # address <TAB> tfvars variable <TAB> install.env key
  local -a checks=(
    "$CLUSTER_KMS_KEYRING_ADDRESS	kms_keyring_name	GKE_DB_KMS_KEYRING"
    "$CLUSTER_KMS_KEY_ADDRESS	kms_key_name	GKE_DB_KMS_KEY"
  )
  local check addr variable key recorded desired
  for check in "${checks[@]}"; do
    IFS=$'\t' read -r addr variable key <<<"$check"
    in_state "$addr" || continue
    recorded=$(state_attr "$addr" name)
    [[ -n "$recorded" ]] || continue
    desired=$(tfvar "$variable")
    [[ "$recorded" != "$desired" ]] || continue
    warn "$variable resolved to '$desired', but this state manages the CMEK resource '$recorded' ($addr)."
    warn "Applying now would plan its DESTRUCTION and recreation under -auto-approve, and destroying the crypto key"
    warn "schedules the live key's versions for destruction, after which the cluster cannot read its own etcd."
    warn "Set ${key}=\"$recorded\" in install.env (or drop the key to take the default), or set $variable in"
    warn "terraform.tfvars for a hand-driven apply. A key is rotated in Cloud KMS, not by renaming it here."
    exit 1
  done
}

# create_cluster = false hands the cluster's CMEK resources back as well: the
# gke-cluster module manages them only alongside a cluster it creates
# (manage_kms = create_cluster && enable_database_encryption), so any of them
# still in state -- adopted by a retry that believed it was creating the
# cluster, the state an interrupted install leaves behind (#1296) -- goes
# from count = 1 to 0 and the apply DESTROYS it. For the crypto key that
# schedules every version for destruction and leaves the live cluster unable
# to read its own etcd. Forgetting them first is the treatment forget_kms
# gives them on destroy: the key ring and key stay in GCP, and adopt_kms
# imports them back the day this state creates a cluster again.
forget_unmanaged_cluster_kms() {
  [[ "$(tfvar create_cluster)" == "false" ]] || return 0
  load_state
  local address forgot=false
  for address in "${CLUSTER_KMS_ADDRESSES[@]}"; do
    in_state "$address" || continue
    log "forgetting $address (create_cluster = false; kept in GCP, re-adopted when this state creates a cluster)"
    state_changed
    terraform state rm "$address" >/dev/null 2>&1 ||
      warn "could not forget $address; the apply may schedule its key versions for destruction"
    forgot=true
  done
  [[ "$forgot" == "true" ]] || return 0
  # A bare apply on this state may already have scheduled the versions.
  local project location
  project=$(tfvar project_id)
  location=$(sed -E 's/-[a-z]$//' <<<"$(tfvar location)")
  restore_key_versions \
    "projects/$project/locations/$location/keyRings/$(tfvar kms_keyring_name)/cryptoKeys/$(tfvar kms_key_name)" \
    "$location" "$project"
}

# When enable_github_minter is true, the minter Deployment cannot pass readiness
# probes without an ENABLED private key version in KMS. Terraform creates the key
# with skip_initial_version_creation = true (import-only), and the helm release
# waits on every Deployment (wait = true), so applying without an imported key
# wedges the apply with the cluster already built.
#
# Two distinct ways the key can fail to be usable, and they are not the same
# question. NOT_FOUND means the keyring or key does not exist yet -- the state
# before the very first apply, since Terraform is what creates them -- which is
# exactly the wedge above, so refuse. PERMISSION_DENIED or a transient API error
# means the guard could not ask, which is not evidence that the key is missing:
# warn and proceed rather than blocking an apply on the guard's own blind spot.
guard_minter_key() {
  [[ "$(tfvar enable_github_minter)" == "true" ]] || return 0
  local project location keyring key versions list_err err_file reason="" list_rc=0
  project=$(tfvar project_id)
  location=$(sed -E 's/-[a-z]$//' <<<"$(tfvar location)")
  keyring=$(tfvar github_minter_kms_keyring)
  key=$(tfvar github_minter_kms_key)

  # stderr is captured apart from the version list rather than merged into it: a
  # gcloud that exits 0 after writing an impersonation or quota-project notice to
  # stderr would otherwise have that notice read back as an ENABLED version.
  err_file=$(mktemp)
  versions=$(gcloud kms keys versions list --key "$key" --keyring "$keyring" \
    --location "$location" --project "$project" \
    --filter='state=ENABLED' --format='value(name)' 2>"$err_file") || list_rc=$?
  list_err=$(cat "$err_file")
  rm -f "$err_file"

  if [[ $list_rc -ne 0 ]]; then
    if ! printf '%s' "$list_err" | grep -qiE "$MINTER_KEY_ABSENT_PATTERN"; then
      warn "could not verify Cloud KMS signing key '$location/$keyring/$key' for GitHub minter ($list_err)."
      warn "Proceeding with apply, but note that the minter requires an ENABLED imported private key to pass readiness."
      return 0
    fi
    reason="does not exist yet"
  elif [[ -z "$(head -1 <<<"$versions")" ]]; then
    reason="has no ENABLED version"
  else
    return 0
  fi

  warn "enable_github_minter is true, but KMS signing key '$location/$keyring/$key' $reason."
  warn "Applying now would deploy the minter and wedge waiting on its readiness probe."
  warn "Import the GitHub App private key before applying (see https://github.com/abcxyz/github-token-minter),"
  warn "or set enable_github_minter = false in terraform.tfvars."
  exit 1
}

# Fetches credentials for this install's cluster, once per run, for the
# in-cluster steps of a destroy, and sets CLUSTER_CONTEXT to the kubeconfig
# context gcloud writes for it, which every kubectl and helm call after it
# names. Returns 1 when get-credentials fails.
#
# Through the helper, so teardown reaches the cluster over the endpoint the
# install used. Without the flag a cluster whose IP endpoint this host cannot
# route to gets that IP written into the kubeconfig, and the callers' guard does
# not catch it: get-credentials is a describe plus a file write, neither of
# which touches the control plane, so it exits 0. The kubectl after it then
# reads an unreachable cluster as a namespace holding no PlatformAgent, and
# teardown reports success having left the finalizer's cluster-scoped RBAC
# behind — the objects nothing else garbage-collects.
CLUSTER_CONTEXT=""
fetch_cluster_credentials() {
  [[ -n "$CLUSTER_CONTEXT" ]] && return 0
  local cluster location project
  cluster=$(tfvar cluster_name)
  location=$(tfvar location)
  project=$(tfvar project_id)
  GKE_DNS_ENDPOINT_FLAG=""
  gke_dns_endpoint_flag "$cluster" "$location" "$project" || true
  # Unquoted on purpose: empty must contribute no argument. See gke_dns_endpoint.sh.
  # shellcheck disable=SC2086
  gcloud container clusters get-credentials "$cluster" --location "$location" \
    --project "$project" $GKE_DNS_ENDPOINT_FLAG >/dev/null 2>&1 || return 1
  # gcloud's own naming, as installer_common.sh's gke_context_name has it. Named
  # rather than read back from current-context, which another get-credentials
  # sharing this kubeconfig can move under a running teardown.
  CLUSTER_CONTEXT="gke_${project}_${location}_${cluster}"
}

delete_agent_cr() {
  local namespace names
  namespace=$(tfvar namespace)

  if ! fetch_cluster_credentials; then
    log "cluster unreachable; nothing to delete in-cluster"
    return 0
  fi

  # Enumerated rather than derived. The composition leaves platformAgent.name at
  # the chart's default, but extra_helm_values can override it, and the admission
  # webhook allows only one PlatformAgent per cluster — so whatever is in the
  # namespace is the one to delete.
  names=$(kubectl --context "$CLUSTER_CONTEXT" get platformagent -n "$namespace" -o name 2>/dev/null || true)
  [[ -n "$names" ]] || { log "no PlatformAgent to delete"; return 0; }

  while read -r ref; do
    [[ -n "$ref" ]] || continue
    log "deleting ${ref} and waiting for its finalizer"
    if kubectl --context "$CLUSTER_CONTEXT" delete "$ref" -n "$namespace" --wait --timeout=180s >/dev/null 2>&1; then
      log "${ref} deleted cleanly"
      continue
    fi

    # Only reachable when the operator cannot clear the finalizer — it is already
    # gone, or wedged. Clearing it by hand skips the finalizer's other job:
    # deleting the agent's cluster-scoped RBAC, which no owner reference
    # garbage-collects (docs/site .../install/uninstall.md). The cluster is
    # normally destroyed moments later, but a destroy can stop between the
    # release and the cluster, so delete the two objects here as well.
    warn "finalizer did not clear in time; removing it so the namespace can terminate"
    kubectl --context "$CLUSTER_CONTEXT" patch "$ref" -n "$namespace" --type=merge \
      -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
    # The operator's naming, kubeagents:minimal:<namespace>:<name>, from
    # k8s-operator/internal/controller/platformagent_manifests.go; a bash
    # script cannot import it, so this must move when that does.
    kubectl --context "$CLUSTER_CONTEXT" delete clusterrolebinding "kubeagents:minimal:${namespace}:${ref##*/}" \
      --ignore-not-found >/dev/null 2>&1 || true
    kubectl --context "$CLUSTER_CONTEXT" delete clusterrole "kubeagents:minimal:${namespace}:${ref##*/}" \
      --ignore-not-found >/dev/null 2>&1 || true
  done <<<"$names"
}

# Defence in depth for the release teardown on a cluster this state did not
# create (#2246). terraform destroy uninstalls the releases through the helm
# provider, and the provider can do nothing and report success: hashicorp/helm
# 3.x's Delete returns early when its existence check fails, and that check
# reads ANY error -- a provider with no host, a 403, a timeout -- as "release
# not found". On a cluster this state created that leak dies with the cluster
# moments later, so this step only runs when the cluster outlives the destroy.
# Uninstalled here, the provider's refresh finds the release gone and drops it
# from state.
#
# Only releases this state installed, named from state rather than the
# configuration: a cert-manager the cluster already ran is not in state and is
# left alone. kube-agents goes first: its Certificate and Issuer are
# cert-manager kinds, removed while cert-manager is still running. The
# PlatformAgent's finalizer is already handled (delete_agent_cr), so the
# chart's pre-delete hook is a no-op and --wait has nothing left to hang on.
#
# helm is not a prerequisite of uninstall.sh or of this script, so a host
# without it is told and left to Terraform. A cluster that answered gcloud but
# not Helm stops the destroy here, before Terraform has removed anything:
# going on would report success over a release that is still running.
uninstall_helm_releases() {
  load_state
  local addr addresses=()
  for addr in "${CLUSTER_ADDRESSES[@]}"; do
    in_state "$addr" && return 0
  done
  for addr in "$HELM_RELEASE_ADDRESS" "$CERT_MANAGER_RELEASE_ADDRESS"; do
    in_state "$addr" && addresses+=("$addr")
  done
  [[ ${#addresses[@]} -gt 0 ]] || return 0

  if ! command -v helm >/dev/null 2>&1; then
    warn "helm is not installed; leaving the Helm releases to terraform destroy"
    return 0
  fi
  if ! fetch_cluster_credentials; then
    warn "could not fetch credentials for the cluster; if it still exists, the Helm releases"
    warn "this state installed outlive the teardown and need a manual 'helm uninstall'"
    return 0
  fi

  local name namespace output line
  for addr in "${addresses[@]}"; do
    if ! name=$(state_attr "$addr" name) || ! namespace=$(state_attr "$addr" namespace); then
      warn "could not read $addr from state (terraform state show failed); re-run destroy, which is safe to re-run"
      exit 1
    fi
    if [[ -z "$name" || -z "$namespace" ]]; then
      warn "could not read the release name or namespace of $addr from state; leaving it to terraform destroy"
      continue
    fi
    # --all: a release left pending-install or uninstalling is still one to
    # remove, and the default listing hides it. Exit 0 with no line naming the
    # release is "not there" (a warning on stderr is not a release); a non-zero
    # exit is "could not ask". Checked first rather than passing
    # --ignore-not-found, which older helm 3 releases lack.
    if ! output=$(helm list --all --short --filter "^${name}\$" \
                    -n "$namespace" --kube-context "$CLUSTER_CONTEXT" 2>&1); then
      :
    elif ! grep -qxF -- "$name" <<<"$output"; then
      log "Helm release $namespace/$name is already gone"
      continue
    elif output=$(helm uninstall "$name" -n "$namespace" --kube-context "$CLUSTER_CONTEXT" \
                    --wait --timeout "$HELM_UNINSTALL_TIMEOUT" 2>&1); then
      log "uninstalled Helm release $namespace/$name"
      continue
    fi
    warn "could not uninstall Helm release $namespace/$name from $CLUSTER_CONTEXT; helm said:"
    while IFS= read -r line; do printf '     %s\n' "$line"; done <<<"$output" >&2
    warn "This state did not create the cluster, so the release would outlive the teardown."
    warn "Fix access to the cluster and re-run (destroy is safe to re-run), or remove it by hand:"
    warn "  helm uninstall $name -n $namespace --kube-context $CLUSTER_CONTEXT --wait"
    warn "If this host cannot get access back and the cluster's owner will remove the release, drop it"
    warn "from this install's state instead and re-run destroy: in terraform/examples/full-install, with"
    warn "the same KUBE_AGENTS_STATE_BUCKET as this run and after a plan has initialised that backend,"
    warn "  terraform state rm '$addr'"
    exit 1
  done
}

purge_backups() {
  # Deliberately not gated on enable_gke_backup_plan: a plan created while the
  # variable was true is still in state (and still owns backups) after it is
  # flipped off, and the describe below already handles the plan-absent case.
  local project location plan
  project=$(tfvar project_id)
  # Backup for GKE plans are regional, whatever the cluster location is.
  location=$(sed -E 's/-[a-z]$//' <<<"$(tfvar location)")
  # The gke-backup-plan module's own derivation for a null `name`, which is
  # the only value this composition ever passes (main.tf sets no name), so
  # the plan cannot be called anything else here.
  plan="$(tfvar cluster_name)-backup-plan"

  gcloud beta container backup-restore backup-plans describe "$plan" \
    --project "$project" --location "$location" >/dev/null 2>&1 || return 0

  local backups
  backups=$(gcloud beta container backup-restore backups list --project "$project" \
    --location "$location" --backup-plan "$plan" --format='value(name)' 2>/dev/null || true)
  [[ -n "$backups" ]] || { log "backup plan owns no backups"; return 0; }

  # The BackupPlan resource refuses to delete while any backup still references it,
  # so terraform destroy fails on it until these are gone.
  while read -r backup; do
    [[ -n "$backup" ]] || continue
    log "deleting backup ${backup##*/}"
    gcloud beta container backup-restore backups delete "${backup##*/}" \
      --project "$project" --location "$location" --backup-plan "$plan" --quiet >/dev/null 2>&1 ||
      warn "could not delete $backup; terraform destroy will fail on the backup plan"
  done <<<"$backups"
}

disable_deletion_protection() {
  # The cluster's state address depends on cluster_mode, and on whether the
  # state predates the mode switch (the autopilot resource used to carry no
  # index; the moved block renames it on the first plan, but this script can
  # run against a state that has not planned yet). create_cluster = false has
  # no cluster in state at all and falls through to return 0.
  local address="" candidate
  load_state
  for candidate in "${CLUSTER_ADDRESSES[@]}"; do
    if in_state "$candidate"; then
      address="$candidate"
      break
    fi
  done
  [[ -n "$address" ]] || return 0

  # Read what STATE records, not what the variable is configured to: state is
  # what the provider enforces on delete. The two disagree exactly when it
  # matters — after a destroy that stopped partway (state already false, and
  # the targeted apply below would try to re-create the KMS resources
  # forget_kms removed, 409ing every later run), or when someone set the
  # variable to false without an intervening apply (state still true, and
  # skipping here fails the destroy on the cluster).
  local recorded
  recorded=$(terraform state show -no-color "$address" 2>/dev/null |
    sed -n 's/^ *deletion_protection *= *//p' | head -1)
  [[ "$recorded" == "true" ]] || return 0

  log "clearing deletion_protection so the cluster can be destroyed"
  state_changed
  terraform apply -input=false -auto-approve \
    -var="deletion_protection=false" -target="$address" >/dev/null
}

# Terraform cannot delete a KMS key ring or key, but destroying the crypto key
# resource still schedules every version for destruction — leaving a key that
# exists and cannot encrypt. Forgetting these before the destroy is what keeps
# them genuinely untouched, so the next apply adopts a working key rather than a
# hollow one. They are re-imported by adopt-kms.
forget_kms() {
  load_state
  local address
  for address in \
    "$CLUSTER_KMS_KEY_ADDRESS" \
    "$CLUSTER_KMS_KEYRING_ADDRESS" \
    "$MINTER_KMS_KEY_ADDRESS" \
    "$MINTER_KMS_KEYRING_ADDRESS"; do
    in_state "$address" || continue
    log "forgetting $address (kept in GCP; re-adopted on the next apply)"
    state_changed
    terraform state rm "$address" >/dev/null 2>&1 ||
      warn "could not forget $address; its key versions may be scheduled for destruction"
  done
}

# A helm_release's `metadata` holds the release's full values as JSON, and the
# helm provider does not mark it sensitive. `values` itself prints as
# "(sensitive value)", but whenever the release changes (every image-tag bump)
# metadata turns "(known after apply)" and Terraform prints its OLD value in
# full -- and on destroy it prints it going to null. Those values can include
# the chart's credentials (credentials.data), so the metadata block is no place
# for a log that anyone else may read, a CI log above all.
#
# So everything Terraform prints for a plan, an apply, or a destroy
# goes through this filter: inside a helm_release, an attribute-style
# `metadata = {` (or `= [`) block is replaced by one line saying it was hidden,
# up to the bracket that closes it at the attribute's own column. The block is
# matched with or without a diff symbol before it: `terraform show`, and a
# plan that imports the release, print it with none. Everything else passes
# through unchanged. Colour codes and a trailing CR are stripped only for
# matching; every line that passes through is printed as received.
# Machine-readable output (`-json`) is not this filter's to parse.
#
# It fails closed: a block whose closing bracket never comes hides the rest of
# the output, and says so at the end, rather than guess where the values stop.
#
# Three properties the callers rely on, each easy to lose:
#
#   exit status  this is the right-hand side of a pipe under `set -o pipefail`,
#                so terraform's own code -- including plan's -detailed-exitcode
#                2 -- is what the pipeline returns.
#   Ctrl-C       SIGINT goes to the whole foreground process group. Terraform
#                traps it and shuts down gracefully, writing state and
#                releasing the lock -- but only if it can still print: with the
#                filter dead, its next write hits a closed pipe and SIGPIPE
#                kills it mid-operation. So the filter ignores INT and TERM and
#                ends, as it always does, when terraform closes the pipe.
#   streaming    output goes out line by line, so a long helm wait does not
#                look hung. fflush() covers gawk and BSD awk; mawk also
#                buffers its INPUT and needs -W interactive, which no other awk
#                accepts.
redact_helm_release_metadata() {
  local awk_cmd=(awk) awk_version
  # A probe, not a step: an awk that rejects -W just is not mawk, so its
  # failure is the answer rather than an error. Captured whole, not piped into
  # `head | grep -q`, whose early exit would SIGPIPE awk and, under pipefail,
  # read a real mawk as "not mawk".
  awk_version="$(awk -W version </dev/null 2>&1)" || awk_version=""
  if [[ "$awk_version" == mawk* ]]; then
    awk_cmd=(awk -W interactive)
  fi
  (
    trap '' INT TERM
    exec "${awk_cmd[@]}" '
      function plain(s) { gsub(/\033\[[0-9;]*m/, "", s); sub(/\r$/, "", s); return s }
      function indent(s) { match(s, /^ */); return RLENGTH }
      {
        p = plain($0)
        if (skipping) {
          if (indent(p) == close_col && substr(p, close_col + 1, 1) == closer) {
            skipping = 0
          }
          next
        }
        if (p ~ /(resource|data) "/) {
          in_helm = (p ~ /resource "helm_release"/)
        }
        if (in_helm && p ~ /^ *((-\/\+|\+\/-|[~+-]) +)?metadata += *[{[] *$/) {
          close_col = index(p, "metadata") - 1
          closer = (p ~ /\[ *$/) ? "]" : "}"
          print substr(p, 1, close_col) "metadata = (hidden by lifecycle.sh: it repeats every chart value, credentials included)"
          fflush()
          skipping = 1
          next
        }
        print
        fflush()
      }
      END {
        if (skipping) {
          print "lifecycle.sh: a helm_release metadata block never closed; the rest of this output was hidden with it"
        }
      }
    '
  )
}

if [[ "${KUBE_AGENTS_SOURCE_ONLY:-false}" == "true" ]]; then
  return 0 2>/dev/null || exit 0
fi

# A lifecycle.sh killed by a signal the EXIT trap cannot see (SIGKILL, an
# OOM-kill, a lost machine) leaves the two import overrides behind, and a
# plan or apply that merged the scope one would resolve every declared
# selector to no members. Whatever subcommand runs next clears them before
# terraform reads the configuration; an import rewrites them first anyway.
drop_override

case "${1:-}" in
  adopt-kms)
    shift
    ensure_init
    forget_unmanaged_cluster_kms
    adopt_kms
    ;;
  plan)
    shift
    # Read-only, and every argument here is what makes it so:
    #
    #   readonly       do not create the state bucket (see ensure_backend)
    #   -lock=false    do not take the state lock, so a plan can never block or
    #                  be blocked by the apply it is reporting on
    #   -input=false   never prompt; this runs unattended
    #
    # adopt_kms and adopt_pubsub are deliberately absent: both run
    # `terraform import`, which writes state. A plan against an install that
    # needs adoption therefore shows the resources as "to create" — which is
    # honest about what a plan alone can tell you, and the apply below is what
    # adopts them.
    #
    # -detailed-exitcode passes through from the caller rather than being set
    # here, because it changes the meaning of a non-zero exit: 2 stops meaning
    # "failed" and starts meaning "there are changes". Only a caller that knows
    # to treat 2 as a report should ask for it.
    ensure_init readonly
    log "terraform plan"
    terraform plan -lock=false -input=false "$@" | redact_helm_release_metadata
    ;;
  apply)
    shift
    ensure_init
    log "verifying pre-apply safety guards (cluster, IAM, KMS)..."
    guard_cluster_ownership
    guard_gsa_identity
    guard_kms_identity
    guard_release_namespace
    guard_pubsub_subscription
    forget_unmanaged_cluster_kms
    guard_minter_key
    log "checking pre-existing GCP resources to adopt (KMS, Pub/Sub)..."
    adopt_kms
    adopt_pubsub
    log "terraform apply"
    # No -input=false: this prompts like plain `terraform apply` does. Pass
    # -auto-approve through ARGS for unattended runs.
    #
    # Only an apply that is about to ask someone at a terminal runs unfiltered:
    # a terminal on stdin and stdout, and no approval given here or in
    # TF_CLI_ARGS_apply. Its closing "Enter a value: " has no newline and would
    # wait in the line-based filter until answered. Everything else is
    # filtered: an approved apply (upgrade.sh passes -auto-approve) prints the
    # whole diff without asking, and a pipe (`| tee`), a file or CI is a log.
    if [[ -t 0 && -t 1 && " $* ${TF_CLI_ARGS_apply:-} " != *auto-approve* ]]; then
      terraform apply "$@"
    else
      terraform apply "$@" | redact_helm_release_metadata
    fi
    ;;
  destroy)
    shift
    ensure_init
    # Confirm before the FIRST side effect, not at terraform's own prompt: by
    # the time `terraform destroy` asks, this script has already deleted the
    # PlatformAgent CR, uninstalled the Helm releases on a cluster this state
    # did not create, permanently deleted every backup the plan owns,
    # cleared deletion_protection, and forgotten the KMS state entries — and
    # answering "no" there undoes none of it. One gate, up front; once passed,
    # -auto-approve is appended so terraform does not present a second gate
    # that falsely implies the operation can still be stopped cleanly.
    auto=false
    for arg in "$@"; do [[ "$arg" == "-auto-approve" ]] && auto=true; done
    if [[ "$auto" != "true" ]]; then
      warn "destroy starts with irreversible steps BEFORE terraform runs:"
      warn "  - delete the live PlatformAgent CR (force-clearing its finalizer if wedged)"
      warn "  - on a cluster this state did not create, uninstall the Helm releases it installed"
      warn "  - permanently delete EVERY backup the backup plan owns"
      warn "  - clear the cluster's deletion protection"
      warn "  - forget the KMS resources from state (kept in GCP, re-adopted on apply)"
      read -r -p "Type 'yes' to destroy everything, anything else to abort: " answer
      if [[ "$answer" != "yes" ]]; then
        log "aborted before any change was made"
        exit 1
      fi
      set -- "$@" -auto-approve
    fi
    # The CR is deleted in the configured namespace; a configuration that
    # names a different one from the release's would skip it and leave its
    # finalizer for terraform destroy to trip over.
    guard_release_namespace
    delete_agent_cr
    uninstall_helm_releases
    purge_backups
    disable_deletion_protection
    forget_kms
    log "terraform destroy"
    # deletion_protection is passed again because destroy re-evaluates the config,
    # and the variable's default would otherwise reinstate the guard. Filtered,
    # since destroy prints every release value going to null.
    terraform destroy -var="deletion_protection=false" "$@" | redact_helm_release_metadata
    log "done. The KMS key rings remain in the project by design — GCP cannot"
    log "delete them. The next 'lifecycle.sh apply' adopts them automatically."
    ;;
  *)
    # The line range is the header comment above, so it moves whenever that
    # comment grows. It ends at the blank comment line before `set -euo
    # pipefail`.
    sed -n '2,68p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
