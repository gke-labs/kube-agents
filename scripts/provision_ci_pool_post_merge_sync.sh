#!/usr/bin/env bash
# ==============================================================================
# The post-merge sync's repository side, for one CI pool project
# ==============================================================================
# Config Sync on the seeded fleet's slot-a cluster syncs one directory of the
# pool project's GitOps repository into the seeded-post-merge namespace
# (bench/tf/fleet/post-merge-sync.tf). This script makes what that sync reads:
#
#   1. the order-ledger Deployment's declaration in that directory, which the
#      fleet audit writes the workload's PodDisruptionBudget beside. It carries
#      configmanagement.gke.io/managed: disabled, so Config Sync leaves the
#      Deployment to the fleet stack and applies only what else the directory
#      holds;
#   2. a read-only deploy key on that one repository, its private half and
#      GitHub's SSH host keys kept as the current version of a Secret Manager
#      secret in the project, which the fleet stack reads into the namespace's
#      git-creds Secret;
#   3. secretAccessor on that secret alone for the fleet reconciler.
#
# Re-runs converge: a file already there is left as it is, and a new key is
# made only when the stored one is missing, is not the key GitHub holds under
# KEY_TITLE, is not read-only, or the stored host keys differ from GitHub's.
# The new key is added before the old one is deleted, so a sync running on the
# old key keeps working until the secret moves. The private key never reaches
# stdout, stderr, a command line or an xtrace.
#
# Usage:
#   ./scripts/provision_ci_pool_post_merge_sync.sh <PROJECT_ID> [<owner>/<repo>]
# The repository defaults to the pool convention, gke-agentic/<PROJECT_ID>-infra.
# ==============================================================================

set -euo pipefail
{ set +x; } 2>/dev/null
# Every file this script writes holds or may hold the private key.
umask 077

readonly GITOPS_ORG="gke-agentic"
readonly GITOPS_REPO_SUFFIX="-infra"
# bench/tf/fleet/post-merge-sync.tf reads this secret by name.
readonly DEPLOY_KEY_SECRET_NAME="kube-agents-eval-gitops-deploy-key"
readonly DEPLOY_KEY_TITLE="seeded-fleet config-sync (read-only)"
readonly DEPLOY_KEY_TYPE="ed25519"
readonly DEPLOY_KEY_FILE="key"
readonly DEPLOY_KEY_REPLICATION_POLICY="automatic"
readonly DEPLOY_KEY_ACCESSOR="seeded-fleet-reconciler@kube-agents-prow.iam.gserviceaccount.com"
readonly DEPLOY_KEY_ACCESSOR_ROLE="roles/secretmanager.secretAccessor"
readonly KNOWN_HOSTS_HOST="github.com"
# What `gh api` prints on stderr for a path that is not there, and gcloud's for
# an absent secret or one with no readable version.
readonly GH_NOT_FOUND="HTTP 404"
readonly GCLOUD_NOT_FOUND="NOT_FOUND"
readonly GCLOUD_FAILED_PRECONDITION="FAILED_PRECONDITION"
readonly EX_USAGE=64
# The Deployment the post-merge case fixes; defects-a.tf plants the same object.
readonly DECLARATION_PATH="clusters/seeded-a/seeded-post-merge/order-ledger.yaml"
readonly DECLARATION_MESSAGE="Declare seeded-a's order-ledger Deployment in seeded-post-merge"
readonly DECLARATION_CONTENT='apiVersion: apps/v1
kind: Deployment
metadata:
  name: order-ledger
  namespace: seeded-post-merge
  annotations:
    configmanagement.gke.io/managed: disabled
spec:
  replicas: 2
  selector:
    matchLabels:
      app: order-ledger
  template:
    metadata:
      labels:
        app: order-ledger
    spec:
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 65534
        seccompProfile:
          type: RuntimeDefault
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: kubernetes.io/hostname
          whenUnsatisfiable: ScheduleAnyway
          labelSelector:
            matchLabels:
              app: order-ledger
      containers:
        - name: ledger
          image: registry.k8s.io/pause:3.9
          resources:
            requests:
              cpu: 10m
              memory: 16Mi
            limits:
              memory: 32Mi'

die() {
  echo "ERROR: [${PROJECT_ID:-?}] $*" >&2
  exit 1
}

[[ $# -ge 1 && $# -le 2 && -n "${1}" ]] || {
  echo "usage: $0 <PROJECT_ID> [<owner>/<repo>]" >&2
  exit "${EX_USAGE}"
}
readonly PROJECT_ID="$1"
readonly GITOPS_REPO="${2:-${GITOPS_ORG}/${PROJECT_ID}${GITOPS_REPO_SUFFIX}}"

WORK_DIR="$(mktemp -d)"
readonly WORK_DIR
trap 'rm -rf "${WORK_DIR}"' EXIT
readonly ERR_FILE="${WORK_DIR}/err"

# ─── Declaration ───────────────────────────────────────────────────────────────
if read_error="$(gh api "repos/${GITOPS_REPO}/contents/${DECLARATION_PATH}" 2>&1 >/dev/null)"; then
  echo "[${PROJECT_ID}] ${GITOPS_REPO} already carries ${DECLARATION_PATH}"
elif grep -q "${GH_NOT_FOUND}" <<<"${read_error}"; then
  echo "[${PROJECT_ID}] Seeding ${GITOPS_REPO} with ${DECLARATION_PATH}"
  gh api -X PUT "repos/${GITOPS_REPO}/contents/${DECLARATION_PATH}" \
    -f message="${DECLARATION_MESSAGE}" \
    -f content="$(printf '%s\n' "${DECLARATION_CONTENT}" | base64 | tr -d '\n')" >/dev/null ||
    die "could not seed ${DECLARATION_PATH} in ${GITOPS_REPO}"
else
  die "could not read ${DECLARATION_PATH} in ${GITOPS_REPO} (${read_error}); not seeding it blind"
fi

# ─── Current state ─────────────────────────────────────────────────────────────
KNOWN_HOSTS="$(gh api meta --jq ".ssh_keys[] | \"${KNOWN_HOSTS_HOST} \" + ." | sort)" ||
  die "could not read GitHub's SSH host keys"
[[ -n "${KNOWN_HOSTS}" ]] || die "GitHub listed no SSH host keys"
# One row per deploy key titled DEPLOY_KEY_TITLE: id, public key, read_only.
KEY_ROWS="$(gh api --paginate "repos/${GITOPS_REPO}/keys" \
  --jq ".[] | select(.title == \"${DEPLOY_KEY_TITLE}\") | [.id, .key, .read_only] | @tsv")" ||
  die "could not list the deploy keys of ${GITOPS_REPO}"

if gcloud secrets describe "${DEPLOY_KEY_SECRET_NAME}" --project="${PROJECT_ID}" \
  --format='value(name)' >/dev/null 2>"${ERR_FILE}"; then
  echo "[${PROJECT_ID}] Secret ${DEPLOY_KEY_SECRET_NAME} exists"
elif grep -q "${GCLOUD_NOT_FOUND}" "${ERR_FILE}"; then
  echo "[${PROJECT_ID}] Creating secret ${DEPLOY_KEY_SECRET_NAME}"
  gcloud secrets create "${DEPLOY_KEY_SECRET_NAME}" --project="${PROJECT_ID}" \
    --replication-policy="${DEPLOY_KEY_REPLICATION_POLICY}" ||
    die "could not create secret ${DEPLOY_KEY_SECRET_NAME}"
else
  cat "${ERR_FILE}" >&2
  die "could not describe secret ${DEPLOY_KEY_SECRET_NAME}"
fi

# The stored public key and host keys, or nothing when there is no readable
# version. The private half goes to a file only ssh-keygen reads.
STORED_PUBLIC=""
STORED_HOSTS=""
if gcloud secrets versions access latest --secret="${DEPLOY_KEY_SECRET_NAME}" \
  --project="${PROJECT_ID}" >"${WORK_DIR}/stored.json" 2>"${ERR_FILE}"; then
  python3 -c '
import json, sys
payload = json.load(open(sys.argv[1]))
open(sys.argv[2], "w").write(payload.get("ssh", ""))
open(sys.argv[3], "w").write(payload.get("known_hosts", ""))
' "${WORK_DIR}/stored.json" "${WORK_DIR}/stored_key" "${WORK_DIR}/stored_hosts" ||
    die "the latest version of secret ${DEPLOY_KEY_SECRET_NAME} is not the JSON this script writes"
  chmod 600 "${WORK_DIR}/stored_key"
  STORED_PUBLIC="$(ssh-keygen -y -f "${WORK_DIR}/stored_key" 2>/dev/null | cut -d' ' -f1,2)" || STORED_PUBLIC=""
  STORED_HOSTS="$(sort "${WORK_DIR}/stored_hosts")"
elif ! grep -qE "${GCLOUD_NOT_FOUND}|${GCLOUD_FAILED_PRECONDITION}" "${ERR_FILE}"; then
  cat "${ERR_FILE}" >&2
  die "could not read the latest version of secret ${DEPLOY_KEY_SECRET_NAME}"
fi

CONVERGED=0
while IFS=$'\t' read -r _ key read_only; do
  if [[ -n "${STORED_PUBLIC}" && "${key}" == "${STORED_PUBLIC}" && "${read_only}" == "true" ]]; then
    CONVERGED=1
  fi
done <<<"${KEY_ROWS}"
[[ "${STORED_HOSTS}" == "${KNOWN_HOSTS}" ]] || CONVERGED=0

# ─── Key ───────────────────────────────────────────────────────────────────────
if [[ "${CONVERGED}" -eq 1 ]]; then
  echo "[${PROJECT_ID}] ${GITOPS_REPO} holds the stored key read-only under \"${DEPLOY_KEY_TITLE}\""
else
  echo "[${PROJECT_ID}] Adding a read-only deploy key \"${DEPLOY_KEY_TITLE}\" to ${GITOPS_REPO}"
  ssh-keygen -q -t "${DEPLOY_KEY_TYPE}" -N "" -C "${DEPLOY_KEY_TITLE}" -f "${WORK_DIR}/${DEPLOY_KEY_FILE}" ||
    die "could not generate a deploy key"
  # No --allow-write: a deploy key gh adds is read-only unless asked otherwise.
  gh repo deploy-key add "${WORK_DIR}/${DEPLOY_KEY_FILE}.pub" --repo "${GITOPS_REPO}" \
    --title "${DEPLOY_KEY_TITLE}" >/dev/null ||
    die "could not add the deploy key to ${GITOPS_REPO}"
  printf '%s\n' "${KNOWN_HOSTS}" >"${WORK_DIR}/known_hosts"
  python3 -c '
import json, sys
print(json.dumps({"ssh": open(sys.argv[1]).read(), "known_hosts": open(sys.argv[2]).read()}))
' "${WORK_DIR}/${DEPLOY_KEY_FILE}" "${WORK_DIR}/known_hosts" |
    gcloud secrets versions add "${DEPLOY_KEY_SECRET_NAME}" --project="${PROJECT_ID}" --data-file=- >/dev/null ||
    die "could not add a version to secret ${DEPLOY_KEY_SECRET_NAME}; ${GITOPS_REPO} keeps the new key and the old ones until a re-run"
  # The keys this run replaced: every one under the title but the new one.
  while IFS=$'\t' read -r id _ _; do
    [[ -n "${id}" ]] || continue
    echo "[${PROJECT_ID}] Deleting the replaced deploy key ${id} from ${GITOPS_REPO}"
    gh api -X DELETE "repos/${GITOPS_REPO}/keys/${id}" >/dev/null ||
      echo "WARNING: [${PROJECT_ID}] could not delete deploy key ${id}; it no longer matches the secret" >&2
  done <<<"${KEY_ROWS}"
fi

# ─── IAM ───────────────────────────────────────────────────────────────────────
echo "[${PROJECT_ID}] Granting ${DEPLOY_KEY_ACCESSOR_ROLE} on secret ${DEPLOY_KEY_SECRET_NAME} to ${DEPLOY_KEY_ACCESSOR}"
gcloud secrets add-iam-policy-binding "${DEPLOY_KEY_SECRET_NAME}" --project="${PROJECT_ID}" \
  --member="serviceAccount:${DEPLOY_KEY_ACCESSOR}" --role="${DEPLOY_KEY_ACCESSOR_ROLE}" --format=none ||
  die "could not grant ${DEPLOY_KEY_ACCESSOR_ROLE} on secret ${DEPLOY_KEY_SECRET_NAME}"

echo "[${PROJECT_ID}] The post-merge sync can read ${GITOPS_REPO} through secret ${DEPLOY_KEY_SECRET_NAME}"
