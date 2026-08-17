#!/usr/bin/env bash
#
# Makes `apply` and `destroy` repeatable for this composition.
#
# Four things in this stack are not symmetric — applying them is not the inverse
# of destroying them — and every one of them turns the second `terraform apply`
# of a project's life into a failure. Terraform cannot express any of them, so
# they live here rather than in a README telling you to remember them:
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
#
# Usage:
#   ./lifecycle.sh apply    [extra terraform args...]
#   ./lifecycle.sh destroy  [extra terraform args...]
#   ./lifecycle.sh adopt-kms
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m %s\n' "$*" >&2; }

# Runs before anything reads the configuration. init is idempotent and cheap
# when nothing changed, and skipping it is how a routine `git pull` that adds a
# module turns every subcommand below into a failure.
ensure_init() {
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
tfvar() {
  local out
  if ! out=$(echo "var.$1" | terraform console 2>&1); then
    printf '%s\n' "$out" >&2
    warn "could not evaluate var.$1 (see the terraform error above)"
    exit 1
  fi
  printf '%s\n' "$out" | tail -1 | tr -d '"'
}

# The state list is read once and matched in memory. Piping it straight into
# `grep -q` looks equivalent but is not: grep exits at the first match, terraform
# dies of SIGPIPE, and `set -o pipefail` reports the whole pipeline as failed — so
# an address that IS in state reads as absent purely because it sorts early.
STATE_LIST=""
load_state() { STATE_LIST=$(terraform state list 2>/dev/null || true); }
in_state() { grep -Fxq "$1" <<<"$STATE_LIST"; }

# terraform import configures every provider, and the helm provider here is built
# from module.gke_cluster.cluster_endpoint — unknown until the cluster exists. On
# a fresh apply that makes import impossible ("configuration ... depends on values
# that cannot be determined until apply") precisely when it is needed. A temporary
# override pins the provider at a placeholder for the duration; it is never used to
# talk to anything, because import performs no Helm operation.
OVERRIDE_FILE="providers_lifecycle_override.tf"
drop_override() { rm -f "$OVERRIDE_FILE"; }

with_override() {
  cat >"$OVERRIDE_FILE" <<'EOF'
# Written by lifecycle.sh for the duration of a terraform import; always removed
# again. If you are reading this in a committed diff, something went wrong.
provider "helm" {
  kubernetes = {
    host  = "https://127.0.0.1"
    token = "placeholder"
  }
}
EOF
  trap drop_override EXIT
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
  location=$(tfvar location)

  # address <TAB> gcloud-kind <TAB> resource id
  local -a targets=()

  if [[ "$(tfvar enable_database_encryption)" != "false" ]]; then
    keyring=$(tfvar kms_keyring_name)
    key=$(tfvar kms_key_name)
    targets+=(
      "module.gke_cluster.google_kms_key_ring.gke_keyring[0]	keyring	projects/$project/locations/$location/keyRings/$keyring"
      "module.gke_cluster.google_kms_crypto_key.gke_key[0]	key	projects/$project/locations/$location/keyRings/$keyring/cryptoKeys/$key"
    )
  fi

  if [[ "$(tfvar enable_github_minter)" == "true" ]]; then
    # Not surfaced as composition variables; these are the github-minter module's
    # defaults, which mirror k8s-operator/scripts/common.sh.
    targets+=(
      "module.github_minter[0].google_kms_key_ring.minter	keyring	projects/$project/locations/$location/keyRings/github-token-minter-keyring"
      "module.github_minter[0].google_kms_crypto_key.minter	key	projects/$project/locations/$location/keyRings/github-token-minter-keyring/cryptoKeys/github-token-minter-key"
    )
  fi

  local adopted=0 address kind id
  for target in "${targets[@]}"; do
    IFS=$'\t' read -r address kind id <<<"$target"

    if in_state "$address"; then
      continue
    fi

    case "$kind" in
      keyring) gcloud kms keyrings describe "${id##*/}" --location "$location" \
                 --project "$project" >/dev/null 2>&1 || continue ;;
      key)     gcloud kms keys describe "${id##*/}" --location "$location" \
                 --keyring "$(echo "$id" | sed -E 's|.*/keyRings/([^/]+)/.*|\1|')" \
                 --project "$project" >/dev/null 2>&1 || continue ;;
    esac

    log "adopting undeletable KMS resource: $id"
    [[ -f "$OVERRIDE_FILE" ]] || with_override
    if terraform import -input=false "$address" "$id" >/dev/null 2>&1; then
      adopted=$((adopted + 1))
      if [[ "$kind" == "key" ]]; then
        restore_key_versions "$id" "$location" "$project"
      fi
    else
      warn "could not import $address ($id); the apply will fail with a 409"
    fi
  done

  drop_override
  trap - EXIT
  log "KMS adoption complete: $adopted imported"
}

delete_agent_cr() {
  local namespace cluster location project names
  namespace=$(tfvar namespace)
  cluster=$(tfvar cluster_name)
  location=$(tfvar location)
  project=$(tfvar project_id)

  if ! gcloud container clusters get-credentials "$cluster" --region "$location" \
        --project "$project" >/dev/null 2>&1; then
    log "cluster unreachable; nothing to delete in-cluster"
    return 0
  fi

  # Enumerated rather than derived. The composition leaves platformAgent.name at
  # the chart's default, but extra_helm_values can override it, and the admission
  # webhook allows only one PlatformAgent per cluster — so whatever is in the
  # namespace is the one to delete.
  names=$(kubectl get platformagent -n "$namespace" -o name 2>/dev/null || true)
  [[ -n "$names" ]] || { log "no PlatformAgent to delete"; return 0; }

  while read -r ref; do
    [[ -n "$ref" ]] || continue
    log "deleting ${ref} and waiting for its finalizer"
    if kubectl delete "$ref" -n "$namespace" --wait --timeout=180s >/dev/null 2>&1; then
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
    kubectl patch "$ref" -n "$namespace" --type=merge \
      -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
    kubectl delete clusterrolebinding "kubeagents:minimal:${namespace}:${ref##*/}" \
      --ignore-not-found >/dev/null 2>&1 || true
    kubectl delete clusterrole "kubeagents:minimal:${namespace}:${ref##*/}" \
      --ignore-not-found >/dev/null 2>&1 || true
  done <<<"$names"
}

purge_backups() {
  # Deliberately not gated on enable_gke_backup_plan: a plan created while the
  # variable was true is still in state (and still owns backups) after it is
  # flipped off, and the describe below already handles the plan-absent case.
  local project location plan
  project=$(tfvar project_id)
  location=$(tfvar location)
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
  local address="module.gke_cluster.google_container_cluster.autopilot"
  load_state
  in_state "$address" || return 0

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
    "module.gke_cluster.google_kms_crypto_key.gke_key[0]" \
    "module.gke_cluster.google_kms_key_ring.gke_keyring[0]" \
    "module.github_minter[0].google_kms_crypto_key.minter" \
    "module.github_minter[0].google_kms_key_ring.minter"; do
    in_state "$address" || continue
    log "forgetting $address (kept in GCP; re-adopted on the next apply)"
    terraform state rm "$address" >/dev/null 2>&1 ||
      warn "could not forget $address; its key versions may be scheduled for destruction"
  done
}

case "${1:-}" in
  adopt-kms)
    shift
    ensure_init
    adopt_kms
    ;;
  apply)
    shift
    ensure_init
    adopt_kms
    log "terraform apply"
    # No -input=false: this prompts like plain `terraform apply` does. Pass
    # -auto-approve through ARGS for unattended runs.
    terraform apply "$@"
    ;;
  destroy)
    shift
    ensure_init
    # Confirm before the FIRST side effect, not at terraform's own prompt: by
    # the time `terraform destroy` asks, this script has already deleted the
    # PlatformAgent CR, permanently deleted every backup the plan owns,
    # cleared deletion_protection, and forgotten the KMS state entries — and
    # answering "no" there undoes none of it. One gate, up front; once passed,
    # -auto-approve is appended so terraform does not present a second gate
    # that falsely implies the operation can still be stopped cleanly.
    auto=false
    for arg in "$@"; do [[ "$arg" == "-auto-approve" ]] && auto=true; done
    if [[ "$auto" != "true" ]]; then
      warn "destroy starts with irreversible steps BEFORE terraform runs:"
      warn "  - delete the live PlatformAgent CR (force-clearing its finalizer if wedged)"
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
    delete_agent_cr
    purge_backups
    disable_deletion_protection
    forget_kms
    log "terraform destroy"
    # deletion_protection is passed again because destroy re-evaluates the config,
    # and the variable's default would otherwise reinstate the guard.
    terraform destroy -var="deletion_protection=false" "$@"
    log "done. The KMS key rings remain in the project by design — GCP cannot"
    log "delete them. The next 'lifecycle.sh apply' adopts them automatically."
    ;;
  *)
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
