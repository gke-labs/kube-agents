#!/usr/bin/env bash
# ==============================================================================
# Unified Provisioning Script for CI Pool Projects
# ==============================================================================
# Provisions all GCP, GKE, IAM, Artifact Registry, Seeded Fleet, and Token
# Minter infrastructure required to onboard a GCP project into the Prow Boskos
# evaluation pool (kube-agents-evals-project).
#
# Follows the sequence codified in
# docs/site/src/content/docs/deploy/ci-pool-projects.md.
#
# The project itself and its billing link are preconditions: this script
# provisions *into* a project that already exists and already bills.
#
# Usage:
#   ./scripts/provision_ci_pool_project.sh --project-id=kube-agents-evals-4
#   ./scripts/provision_ci_pool_project.sh --project-id=kube-agents-evals-3 --pem-file=/path/to/app.pem
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT_ID=""
REGION="us-central1"
APP_ID="4675512"
PEM_FILE=""
SKIP_FLEET="false"
SKIP_HOST_CLUSTER="false"
ALLOW_UNMAPPED="false"

# The host cluster's name is not a preference: scripts/verify_ci_pool_project.py
# asserts it, hack/ci-env.sh selects it, and the Boskos lease resolves to it.
HOST_CLUSTER_NAME="platform-agent-host"

usage() {
  cat <<EOF
Usage: $(basename "$0") --project-id=PROJECT_ID [OPTIONS]

Required:
  --project-id=ID           GCP Project ID (e.g. kube-agents-evals-4)

Options:
  --region=REGION           GCP region. Only us-central1 is supported today;
                            bench/tf/fleet is pinned to us-central1-a.
  --app-id=APP_ID           GitHub App ID (default: 4675512)
  --pem-file=PATH           Path to GitHub App private key PEM file for KMS import
  --skip-host-cluster       Skip terraform/examples/full-install (if host cluster already exists)
  --skip-fleet              Skip bench/tf/fleet (if seeded fleet clusters already exist)
  --allow-unmapped          Proceed even though the project is not yet mapped in
                            hack/ci-deploy.sh. The run will still end red at the
                            verification step -- see Step 0.
  -h, --help                Show this help message
EOF
  exit 1
}

for arg in "$@"; do
  case "$arg" in
    --project-id=*) PROJECT_ID="${arg#*=}" ;;
    --region=*) REGION="${arg#*=}" ;;
    --app-id=*) APP_ID="${arg#*=}" ;;
    --pem-file=*) PEM_FILE="${arg#*=}" ;;
    --skip-host-cluster) SKIP_HOST_CLUSTER="true" ;;
    --skip-fleet) SKIP_FLEET="true" ;;
    --allow-unmapped) ALLOW_UNMAPPED="true" ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $arg" >&2; usage ;;
  esac
done

if [ -z "${PROJECT_ID}" ]; then
  echo "ERROR: --project-id is required." >&2
  usage
fi

if [ -n "${PEM_FILE}" ]; then
  if [ ! -f "${PEM_FILE}" ]; then
    echo "FATAL: Specified PEM file '${PEM_FILE}' does not exist." >&2
    exit 1
  fi
  # Absolutize now. Step 4 imports from inside a `cd "${MINTY_DIR}"` subshell,
  # so a relative --pem-file passes the check above and then resolves against
  # the wrong directory -- failing after steps 1-3 have applied APIs, IAM, AR,
  # GKE, the fleet and the minter stack, the most expensive place in the run to
  # fail. install.sh:1053 does the same before its own cd into minty.
  PEM_FILE="$(cd "$(dirname "${PEM_FILE}")" && pwd)/$(basename "${PEM_FILE}")"
fi

# --region reaches full-install and ci-pool-minter, but bench/tf/fleet is zonal
# and defaults to us-central1-a, so any other region splits the fleet away from
# the host cluster: wrong-region cost and quota, and two clusters claiming one
# slot if the stack is later re-applied at a different zone. Refuse rather than
# land it silently -- the verifier matches clusters by name and reports green.
if [ "${REGION}" != "us-central1" ]; then
  echo "FATAL: --region=${REGION} is not supported. bench/tf/fleet is pinned to" >&2
  echo "       us-central1-a, so the seeded trio would not follow the host cluster." >&2
  echo "       Give bench/tf/fleet a zone in ${REGION} first." >&2
  exit 1
fi

GITOPS_REPO="gke-agentic/${PROJECT_ID}-infra"

# Set by step 1.4 to the App installation this org has for ${APP_ID}, so step 5
# can link straight to it. Empty means no installation was found, which is a
# different problem and gets a different link.
INST_ID=""

echo "================================================================================"
echo " Provisioning CI Pool Project: ${PROJECT_ID}"
echo " Region:       ${REGION}"
echo " Host cluster: ${HOST_CLUSTER_NAME}"
echo " GitOps Repo:  ${GITOPS_REPO}"
echo " GitHub App:   ${APP_ID}"
echo "================================================================================"

# ─── Step 0: Preconditions ────────────────────────────────────────────────────
# Everything here is read-only and cheap. It runs before the first Terraform
# apply on purpose: the mapping is a code change this script cannot make, and
# discovering that after two applies wastes the applies.
echo -e "\n==> [Step 0/5] Checking preconditions..."

# Both Terraform binaries, because this script needs both and they are not
# interchangeable here: Step 2.1 and Step 3 drive `terraform` (lifecycle.sh
# hardcodes it too), Step 2.2 drives `tofu`. Each state prefix was written by
# whichever binary owns its step, so this is not a mix to resolve by picking one
# -- swapping a binary would point it at state the other wrote. Checked here for
# the reason at the top of Step 0: without it, a missing `tofu` surfaces as
# "command not found" at Step 2.2, twelve minutes and two applies in.
MISSING_TOOLS=()
for tool in gcloud gh git go jq python3 terraform tofu; do
  command -v "${tool}" >/dev/null 2>&1 || MISSING_TOOLS+=("${tool}")
done
if [ ${#MISSING_TOOLS[@]} -gt 0 ]; then
  echo "FATAL: not on PATH: ${MISSING_TOOLS[*]}" >&2
  echo "       This script needs terraform and tofu both; see the comment above." >&2
  exit 1
fi
echo "✓ Toolchain present (gcloud, gh, git, go, jq, python3, terraform, tofu)"

# The project must exist and bill. `gcloud services enable` against an unbilled
# project fails with a message that does not obviously say "billing", so the
# check is here to make the first failure legible.
if ! gcloud projects describe "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "FATAL: project ${PROJECT_ID} does not exist or is not visible to $(gcloud config get-value account 2>/dev/null)." >&2
  echo "       Creating the project and linking billing are preconditions of this script." >&2
  exit 1
fi

# Reads the boolean and nothing else. The unfiltered `describe` output also
# carries billingAccountName -- the billing account ID, which is internal. This
# script is run by hand, so the realistic exposure is not a log but a paste:
# onboarding evidence for a project ends up in issues and pull requests on a
# public repository. Keep the --format filter, and do not echo raw output.
#
# Failing to read the status is not the same as the status being false: an
# operator without billing visibility on the project gets a non-zero exit here
# and should not be blocked by it.
if BILLING_ENABLED="$(gcloud billing projects describe "${PROJECT_ID}" --format='value(billingEnabled)' 2>/dev/null)"; then
  if [ "${BILLING_ENABLED}" != "True" ]; then
    echo "FATAL: billing is not enabled on ${PROJECT_ID}." >&2
    echo "       Link a billing account before provisioning. Linking needs the billing" >&2
    echo "       account ID, which is internal and deliberately not recorded here --" >&2
    echo "       discover it with: gcloud billing accounts list" >&2
    exit 1
  fi
  echo "✓ Project exists and billing is enabled"
else
  echo "⚠️ Could not read billing status for ${PROJECT_ID} (no billing read access?)." >&2
  echo "   Continuing -- this is a visibility limit, not a proven misconfiguration." >&2
fi

# Anchored to the body of gitops_repo_for_project() and to the exact case arm.
# A bare `grep "${PROJECT_ID}"` also matches comments and any longer project ID
# that contains this one as a prefix.
CI_DEPLOY="${REPO_ROOT}/hack/ci-deploy.sh"
if ! awk '/gitops_repo_for_project\(\)[[:space:]]*\{/,/^\}/' "${CI_DEPLOY}" 2>/dev/null \
     | grep -qE "^[[:space:]]*${PROJECT_ID}\)[[:space:]]+echo[[:space:]]+\"${GITOPS_REPO}\""; then
  echo "⚠️ ${PROJECT_ID} is not mapped in hack/ci-deploy.sh." >&2
  echo "   Add to gitops_repo_for_project():" >&2
  echo "       ${PROJECT_ID}) echo \"${GITOPS_REPO}\" ;;" >&2
  echo "   and add the same pair to _EXPECTED_MAPPING in tests/test_ci_gitops_repo.py." >&2
  if [ "${ALLOW_UNMAPPED}" != "true" ]; then
    echo "   Refusing to provision: an unmapped project fails every lease at" >&2
    echo "   gitops_repo_for_project()'s refusal, and Step 5 would fail anyway." >&2
    echo "   Land the mapping first, or re-run with --allow-unmapped." >&2
    exit 1
  fi
  echo "   --allow-unmapped set: continuing. Step 5 will still report this as a failure."
else
  echo "✓ Mapped to ${GITOPS_REPO} in hack/ci-deploy.sh"
fi

# ─── Step 1: APIs, IAM & Artifact Registry ────────────────────────────────────
echo -e "\n==> [Step 1/5] Enabling GCP APIs and Configuring IAM..."
gcloud services enable \
  compute.googleapis.com \
  container.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  logging.googleapis.com \
  monitoring.googleapis.com \
  iam.googleapis.com \
  cloudkms.googleapis.com \
  --project="${PROJECT_ID}"

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
echo "Project Number: ${PROJECT_NUMBER}"

# kubeagents-platform-gsa is deliberately not created here. terraform/examples/
# full-install owns it, along with its project roles and its Workload Identity
# binding, as module.kube_agents_iam.google_service_account.agent -- the module's
# defaults are exactly this account, kubeagents-system, and
# kubeagents-platform-agent, so the composition needs no overrides to produce it.
# Creating it with gcloud first put the account outside Terraform's state and the
# step 2.1 apply died on `Error 409: Service account kubeagents-platform-gsa
# already exists`, which is what a project that had never been applied before
# found on its first run. Projects 1-3 never showed it: their GSAs predate this
# script and were already in state.

# All six pool projects build as the Compute Engine default SA, measured
# 2026-08-26 with `gcloud builds list --format='value(serviceAccount)'`. The
# legacy <number>@cloudbuild.gserviceaccount.com is granted too, as an inert
# no-op in case a project ever defaults the other way.
CLOUDBUILD_SA="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"
COMPUTE_SA="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

for member in "${CLOUDBUILD_SA}" "${COMPUTE_SA}"; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="${member}" \
    --role="roles/artifactregistry.writer" \
    --quiet >/dev/null
done

# The GKE nodes pull the operator and agent images from this project's registry.
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="${COMPUTE_SA}" \
  --role="roles/artifactregistry.reader" \
  --quiet >/dev/null

# Cross-project read of the warm cache image. hack/ci-deploy.sh defaults
# CACHE_IMAGE to us-docker.pkg.dev/kube-agents-prow/kube-agents/platform-agent:latest,
# which lives in the `us` multi-region repository -- not us-central1.
echo "Granting Artifact Registry reader on kube-agents-prow (location: us)..."
for member in "${CLOUDBUILD_SA}" "${COMPUTE_SA}"; do
  gcloud artifacts repositories add-iam-policy-binding kube-agents \
    --project=kube-agents-prow \
    --location=us \
    --member="${member}" \
    --role="roles/artifactregistry.reader" \
    --quiet >/dev/null
done

# The pull-kube-agents-smoke-test job runs on the build-kube-agents cluster, not
# in this project. It leases this project from Boskos and reaches in as
# prowjob-default-sa@kube-agents-prow for cluster credentials, the chart deploy
# and the build. Without these it leases a fully provisioned project and dies on
# the first gcloud call (gke-labs/kube-agents#966).
#
# The set kube-agents-evals holds, kept as measured rather than trimmed. No
# Artifact Registry role: AR_REPO and CACHE_IMAGE reach hack/ci-deploy.sh's
# `gcloud builds submit` as substitutions, so Cloud Build does the push and the
# GKE nodes do the pull. This account touches the registry at no point.
PROW_RUNNER_SA="serviceAccount:prowjob-default-sa@kube-agents-prow.iam.gserviceaccount.com"
echo "Granting the Prow runner access to ${PROJECT_ID}..."
for role in \
  roles/cloudbuild.builds.editor \
  roles/cloudbuild.builds.viewer \
  roles/container.admin \
  roles/container.developer \
  roles/iam.serviceAccountAdmin \
  roles/iam.serviceAccountUser \
  roles/logging.logWriter \
  roles/logging.viewer \
  roles/resourcemanager.projectIamAdmin \
  roles/serviceusage.serviceUsageConsumer \
  roles/storage.admin \
  roles/viewer; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="${PROW_RUNNER_SA}" \
    --role="${role}" \
    --quiet >/dev/null
done

# ─── Artifact Registry Creation & Cleanup Policy ──────────────────────────────
echo -e "\n==> [Step 1.3] Creating Regional Docker Artifact Registry & Cleanup Policy..."
if ! gcloud artifacts repositories describe kube-agents --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud artifacts repositories create kube-agents \
    --repository-format=docker \
    --location="${REGION}" \
    --description="PR evaluation container images" \
    --project="${PROJECT_ID}"
fi

CLEANUP_POLICY_FILE="$(mktemp)"
trap 'rm -f "${CLEANUP_POLICY_FILE}"' EXIT
cat > "${CLEANUP_POLICY_FILE}" <<'EOF'
[
  {
    "name": "delete-pr-images-older-than-14-days",
    "action": { "type": "Delete" },
    "condition": {
      "tagState": "tagged",
      "tagPrefixes": ["pr-"],
      "olderThan": "14d"
    }
  },
  {
    "name": "delete-untagged-older-than-1-day",
    "action": { "type": "Delete" },
    "condition": {
      "tagState": "untagged",
      "olderThan": "1d"
    }
  },
  {
    "name": "keep-latest",
    "action": { "type": "Keep" },
    "condition": {
      "tagState": "tagged",
      "tagPrefixes": ["latest"]
    }
  }
]
EOF

gcloud artifacts repositories set-cleanup-policies kube-agents \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --policy="${CLEANUP_POLICY_FILE}" \
  --quiet

# ─── GitOps Repo & App Installation Check ─────────────────────────────────────
echo -e "\n==> [Step 1.4] Checking GitOps Repository & App Installation..."
if ! gh repo view "${GITOPS_REPO}" >/dev/null 2>&1; then
  echo "Creating private GitOps repository ${GITOPS_REPO}..."
  gh repo create "${GITOPS_REPO}" --private --description="GitOps eval repository for ${PROJECT_ID}"
fi

INST_JSON="$(gh api /orgs/gke-agentic/installations --jq ".installations[] | select(.app_id==${APP_ID})" 2>/dev/null || echo "")"
if [ -n "${INST_JSON}" ]; then
  INST_ID="$(echo "${INST_JSON}" | jq -r .id)"
  echo "Found GitHub App installation ID: ${INST_ID}"
  # Adding the repo to the installation is the security review (see
  # terraform/examples/ci-pool-minter/README.md), so it happens in the UI.
  echo "⚠ ${GITOPS_REPO} must be added to GitHub App ${APP_ID}'s installation by hand."
  echo "  That edit widens where a minted token can write. Do it while this runs:"
  echo "  https://github.com/organizations/gke-agentic/settings/installations/${INST_ID}"
  echo "  Step 5 asks you to confirm it before ${PROJECT_ID} can be marked verified."
else
  # No installation at all is a different problem from a repo missing off one,
  # and it used to print nothing: the whole block above is inside the `if`, so a
  # project whose App was never installed reached step 5 with no mention of it.
  echo "⚠ GitHub App ${APP_ID} has no installation on the gke-agentic org, or the"
  echo "  token in use cannot see it. The minter cannot mint until that is fixed:"
  echo "  https://github.com/organizations/gke-agentic/settings/installations"
fi

# ─── Step 2: Host GKE Cluster & Seeded Fleet ──────────────────────────────────
# One bucket per project, one prefix per stack. Versioning and uniform
# bucket-level access are not optional: this bucket holds the only record of
# what Terraform owns, and an unversioned state bucket has no recovery from a
# truncated write. terraform/examples/full-install/lifecycle.sh sets both when
# it creates the bucket itself -- but only when the bucket is missing, so
# pre-creating it here without them would silently drop both.
STATE_BUCKET="${PROJECT_ID}-tf-state"
if ! gcloud storage buckets describe "gs://${STATE_BUCKET}" >/dev/null 2>&1; then
  echo "Creating remote state bucket gs://${STATE_BUCKET} (versioned, uniform access)..."
  gcloud storage buckets create "gs://${STATE_BUCKET}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --uniform-bucket-level-access
  gcloud storage buckets update "gs://${STATE_BUCKET}" --versioning
fi

if [ "${SKIP_HOST_CLUSTER}" != "true" ]; then
  echo -e "\n==> [Step 2.1] Provisioning Host GKE Cluster (${HOST_CLUSTER_NAME}) with remote state..."
  (
    cd "${REPO_ROOT}/terraform/examples/full-install"

    # terraform.tfvars is the operator's file, and lifecycle.sh reads it through
    # `terraform console` (see its tfvar() helper), so the values cannot be
    # passed as -var-file. Back up whatever is there and put it back on the way
    # out -- including on failure, which is when a clobbered config hurts most.
    TFVARS="terraform.tfvars"
    TFVARS_BACKUP=""
    if [ -f "${TFVARS}" ]; then
      TFVARS_BACKUP="$(mktemp)"
      cp "${TFVARS}" "${TFVARS_BACKUP}"
      echo "  (backed up existing terraform.tfvars; it will be restored)"
    fi
    # shellcheck disable=SC2329  # invoked by the EXIT trap below, not by name
    restore_tfvars() {
      if [ -n "${TFVARS_BACKUP}" ]; then
        mv -f "${TFVARS_BACKUP}" "${TFVARS}"
      else
        rm -f "${TFVARS}"
      fi
    }
    trap restore_tfvars EXIT

    # full-install declares four variables with no default: project_id,
    # cluster_name, location and api_server_key. lifecycle.sh applies with
    # -input=false, so a missing one is a hard "No value for required variable"
    # rather than a prompt. api_server_key is generated the same way
    # hack/ci-deploy.sh generates it when unset (openssl rand -hex 16).
    cat > "${TFVARS}" <<EOF
project_id     = "${PROJECT_ID}"
cluster_name   = "${HOST_CLUSTER_NAME}"
location       = "${REGION}"
api_server_key = "$(openssl rand -hex 16)"
EOF

    KUBE_AGENTS_STATE_BUCKET="${STATE_BUCKET}" \
    KUBE_AGENTS_STATE_PREFIX="full-install/${HOST_CLUSTER_NAME}" \
    ./lifecycle.sh apply -auto-approve
  )
else
  echo -e "\n==> [Step 2.1] Skipping Host GKE Cluster (--skip-host-cluster set)..."
fi

if [ "${SKIP_FLEET}" != "true" ]; then
  echo -e "\n==> [Step 2.2] Provisioning Seeded Dirty Fleet (bench/tf/fleet) with remote state..."
  (
    cd "${REPO_ROOT}/bench/tf/fleet"
    tofu init -reconfigure \
      -backend-config="bucket=${STATE_BUCKET}" \
      -backend-config="prefix=seeded-fleet"
    # fleet_reader_token_creators is left at its empty default: the member is the
    # Prow runner identity, and hack/fleet-kubeconfigs.sh only uses the reader
    # account when FLEET_READONLY_SA is exported (see bench/tf/fleet/README.md,
    # "A read-only credential for evaluations"). Until then the binding grants
    # nothing that gets used, and seeded-fleet-reader sits unimpersonated.
    tofu apply -auto-approve -var="project_id=${PROJECT_ID}"
  )
else
  echo -e "\n==> [Step 2.2] Skipping Seeded Fleet (--skip-fleet set)..."
fi

# The Workload Identity binding that used to sit here is gone for the same
# reason the GSA creation above is: module.kube_agents_iam already declares it,
# with this exact member, and depends_on there orders it after the cluster --
# which is what makes the ${PROJECT_ID}.svc.id.goog pool exist by the time the
# binding runs. GCP creates that pool implicitly with the project's first
# Workload-Identity-enabled cluster; without that edge the binding fires minutes
# early and the apply fails with "Identity Pool does not exist" on any project
# that has never had one.

# ─── Step 3: GitHub Token Minter GCP Resources ────────────────────────────────
echo -e "\n==> [Step 3/5] Provisioning GitHub Token Minter Resources with remote state..."
(
  cd "${REPO_ROOT}/terraform/examples/ci-pool-minter"

  # Removed on the way out even when the apply fails. A stale backend_override.tf
  # is gitignored, so it will not be committed -- but it silently redirects the
  # next hand-driven `terraform init` in this directory at another project's state.
  trap 'rm -f backend_override.tf' EXIT

  cat > backend_override.tf <<EOF
terraform {
  backend "gcs" {
    bucket = "${STATE_BUCKET}"
    prefix = "ci-pool-minter/${PROJECT_ID}"
  }
}
EOF
  terraform init -reconfigure
  terraform apply -auto-approve \
    -var="project_id=${PROJECT_ID}" \
    -var="location=${REGION}" \
    -var="gitops_repo=${GITOPS_REPO}"
)

# ─── Step 4: Import GitHub App Private Key PEM ────────────────────────────────
if [ -n "${PEM_FILE}" ] && [ -n "$(gcloud kms keys versions list \
  --project="${PROJECT_ID}" --location="${REGION}" \
  --keyring="github-token-minter-keyring" --key="github-token-minter-key" \
  --filter="state=ENABLED" --format='value(name)' 2>/dev/null)" ]; then
  echo -e "\n==> [Step 4/5] ✓ github-token-minter-key already has an ENABLED version; skipping import."
  echo "    The chart pins cryptoKeyVersions/1 (values.yaml:305), so a second import would"
  echo "    add a version the minter never uses but the verifier probes instead."
  PEM_FILE=""
  SKIP_PEM_IMPORT=true
fi

if [ -n "${PEM_FILE}" ]; then
  echo -e "\n==> [Step 4/5] Importing GitHub App Private Key into Cloud KMS via Minty..."
  MINTY_DIR="$(mktemp -d)"
  git clone --depth 1 --branch v2.7.1 https://github.com/abcxyz/github-token-minter.git "${MINTY_DIR}"
  (
    cd "${MINTY_DIR}"
    # minty reads Application Default Credentials, and the Google Go client
    # libraries send the ADC's quota_project_id as x-goog-user-project -- so the
    # KMS API-enablement check runs against whatever project the operator's ADC
    # happens to name, not against the one holding the key ring. An operator
    # whose ADC points at a personal project gets "Cloud KMS API has not been
    # used in project <theirs> before or it is disabled", naming a project that
    # appears nowhere in this script and has nothing to do with the failure.
    # GOOGLE_CLOUD_QUOTA_PROJECT overrides it for this call only, rather than
    # asking the operator to repoint their global ADC or -- worse -- to enable
    # KMS on a project that should never have been in the request.
    GOOGLE_CLOUD_QUOTA_PROJECT="${PROJECT_ID}" \
    go run ./cmd/minty tools import-pk \
      -project-id="${PROJECT_ID}" \
      -location="${REGION}" \
      -key-ring="github-token-minter-keyring" \
      -key="github-token-minter-key" \
      -private-key="@${PEM_FILE}"
  ) || {
    rm -rf "${MINTY_DIR}"
    echo "FATAL: minty tools import-pk failed." >&2
    exit 1
  }
  rm -rf "${MINTY_DIR}"
  echo "✓ Successfully imported App PEM into KMS key github-token-minter-key"
elif [ "${SKIP_PEM_IMPORT:-false}" != "true" ]; then
  echo -e "\n==> [Step 4/5] ⚠️ Note: No --pem-file provided."
  echo "Cloud KMS key 'github-token-minter-key' is in PENDING_IMPORT state."
  echo "You MUST run 'minty tools import-pk' to enable version 1 before setting EVAL_GITHUB_APP_ID in Prow."
fi

# ─── Step 5: Automated Pre-Flight Verification ────────────────────────────────
echo -e "\n==> [Step 5/5] Running Pre-Flight Verification..."
# Exit 2 -- "nothing failed, but something could not be checked" -- is the
# expected outcome of this first run rather than an error: listing an App
# installation's selected repositories needs a token authorized to the App
# itself, and this script does not hold one. Under `set -e` a bare call would
# abort the whole run on that, so the code is captured instead.
VERIFY_RC=0
python3 "${REPO_ROOT}/scripts/verify_ci_pool_project.py" \
  --project-id="${PROJECT_ID}" --location="${REGION}" --app-id="${APP_ID}" || VERIFY_RC=$?

if [ "${VERIFY_RC}" -eq 1 ]; then
  echo -e "\nFATAL: pre-flight verification failed for ${PROJECT_ID}." >&2
  echo "       Do not register it in Boskos until the failures above are cleared." >&2
  exit 1
fi

# Membership is the one unverifiable item a human can settle by reading a page,
# so ask instead of printing homework. Only on exit 2, and only on a terminal:
# a hard failure is not something the prompt can clear, and with no stdin `read`
# sees EOF at once and would silently record an unattended "no".
if [ "${VERIFY_RC}" -eq 2 ] && [ -t 0 ]; then
  echo
  if [ -n "${INST_ID}" ]; then
    echo "Open https://github.com/organizations/gke-agentic/settings/installations/${INST_ID}"
  else
    # Step 1.4 found no installation, so there is no per-installation page to
    # link to. The org list is the one URL that resolves.
    echo "Open https://github.com/organizations/gke-agentic/settings/installations"
  fi
  echo "and check that ${GITOPS_REPO} is listed. To add it: Configure -> Repository"
  echo "access -> leave \"Only select repositories\" selected -> add the repo -> Save."
  echo "Do not switch to \"All repositories\"; that list is what keeps the App off"
  echo "every other repository in the org."
  # Every `read` is `|| ...`: at EOF it returns nonzero, which under `set -e`
  # would kill the run at the prompt, and an unanswered prompt has to land on
  # "unconfirmed" rather than fall through into asserting the opposite.
  REPO_CONFIRMED=""
  read -r -p "Is it listed? [y/N] " REPO_CONFIRMED || REPO_CONFIRMED=""
  # "No" is a pause, not a dead end -- the operator is at the keyboard and the
  # fix is a browser tab away, so waiting beats ending the run and making them
  # go and find the verifier's re-run invocation. Ctrl-C here is safe: nothing
  # after this point mutates the project.
  case "${REPO_CONFIRMED}" in
    [yY] | [yY][eE][sS]) REPO_CONFIRMED="yes" ;;
    *)
      # Re-ask rather than treating any keypress as the attestation. Bare Enter
      # answered the question it was asking -- the operator confirmed nothing.
      echo "Add it now, then answer again (Ctrl-C to finish later)."
      REPO_CONFIRMED=""
      read -r -p "Is ${GITOPS_REPO} listed? [y/N] " REPO_CONFIRMED || REPO_CONFIRMED=""
      case "${REPO_CONFIRMED}" in
        [yY] | [yY][eE][sS]) REPO_CONFIRMED="yes" ;;
        *) REPO_CONFIRMED="" ;;
      esac
      ;;
  esac

  if [ "${REPO_CONFIRMED}" = "yes" ]; then
    echo
    VERIFY_RC=0
    python3 "${REPO_ROOT}/scripts/verify_ci_pool_project.py" \
      --project-id="${PROJECT_ID}" --location="${REGION}" --app-id="${APP_ID}" \
      --confirmed-repo-in-app-installation || VERIFY_RC=$?
  fi
fi

echo -e "\n================================================================================"
if [ "${SKIP_FLEET}" != "true" ]; then
  # Conditional because the fleet apply is idempotent: a re-run to clear one
  # amber item replants nothing, and the unconditional wording had the operator
  # push activation dates out over an apply that changed no fixture.
  #
  # No date printed on purpose: a gate date is the newest fleet's age measured
  # against the cost SOP's windows, named in the echo below, and both terms move.
  echo "NOTE: if the fleet apply above created or replaced fixtures, they are now"
  echo "      the newest in the pool. Age-gated scenarios gate on the newest"
  echo "      fleet, so their activation dates just moved pool-wide. The windows"
  echo "      are in"
  echo "      agents/platform/governance/fleet_wide_cost_analysis_sop.md"
  echo "      (§3.4 unattached-disk 30d, §3.7 idle-nodepool 7d); add them to today."
  echo ""
fi
# --app-id and --location are spelled out even though the verifier defaults to
# these same values: the operator may have passed --app-id or --region to this
# script, and a hint that omits them silently verifies a different App or region
# than the one just provisioned.
reverify_hint() {
  echo "    python3 scripts/verify_ci_pool_project.py --project-id ${PROJECT_ID} \\"
  echo "      --app-id ${APP_ID} --location ${REGION} \\"
  echo "      --confirmed-repo-in-app-installation"
}

# Four arms, because three codes reach here and they mean different things. The
# catch-all is 2 and anything unrecognised; it is the only one that gets the
# "nothing failed" wording, which is a lie on the other two.
if [ "${VERIFY_RC}" -eq 0 ]; then
  echo "🎉 ${PROJECT_ID} is provisioned and verified. Register it in Boskos last."
elif [ "${VERIFY_RC}" -eq 1 ]; then
  # Only from the re-run: the first call's exit 1 already left at the guard
  # above. The installation was confirmed and a check still failed, so this is a
  # real failure rather than something the operator can clear by confirming.
  echo "✗ ${PROJECT_ID} is provisioned, but verification FAILED."
  echo "  Do not register it in Boskos. Clear what the report above lists, then:"
  reverify_hint
  exit 1
elif [ "${VERIFY_RC}" -eq 64 ]; then
  # The verifier's usage code. This script builds that command line, so a bad
  # one is a defect here; the operator has nothing to clear and the project is
  # neither verified nor known to be broken.
  echo "✗ this script called the verifier with a bad command line (exit 64)."
  echo "  ${PROJECT_ID} is provisioned but unverified. Report the argument error"
  echo "  above against scripts/provision_ci_pool_project.sh."
  exit 1
else
  echo "⚠ ${PROJECT_ID} is provisioned, but verification has not gone green."
  echo "  Nothing failed; one or more items could not be checked. Clear them, then:"
  reverify_hint
  echo "  Boskos registration waits on that exiting 0."
fi
echo "================================================================================"
