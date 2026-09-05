#!/usr/bin/env bash
# ==============================================================================
# Prow CI Deployment Pipeline Script
# ==============================================================================
# The evaluation cluster and its IAM are pre-configured; this script builds
# the PR's images and deploys the kube-agents chart onto that cluster.
#
# Setting RC_COMMIT_SHA switches it to the release-candidate path: no build at
# all, and the chart's published GHCR images at that commit instead. Section 2a
# is the whole of the difference, and with the variable unset nothing below
# behaves differently from the day this line was added.
# ==============================================================================

set -euo pipefail

# The session daemon caps Warning alerts at 5 per UTC day, fleet-wide per
# install (ALERT_DAILY_LIMIT_WARNING, #641). That cap is alert-storm
# protection for a human-watched channel; an eval install's whole job is
# generating alerts. Every smoke build that leases a pool project that day
# spends the same shared budget on its own crash-loop scenarios, and once it
# is gone the daemon quota-suppresses the very alert
# autoops-warning-event-triage waits 300s for, timing out the plant (#1101).
# 0 is the documented off-switch — `_alert_daily_limit` in
# agents/platform/scripts/session_kv_server.py parses it as "cap off" and
# `_claim_alert_quota` lets `limit <= 0` through uncapped — and setting it
# here, on the deploy, leaves the production
# default untouched. tests/test_ci_deploy_alert_quota.py pins the whole
# chain: this flag, the chart rendering it onto the CR, and the operator's
# env allowlist letting it through to the container.
readonly EVAL_ALERT_DAILY_LIMIT_WARNING="0"

# The release step 5 installs, and — for the poisoned-record guard (#1172) —
# the label pair Helm stamps on every release-record Secret it writes
# (`owner=helm` plus `name=<release>`), selecting every revision's record of
# this release and nothing else in the namespace.
readonly HELM_RELEASE_NAME="kube-agents"
readonly HELM_RELEASE_SECRET_SELECTOR="owner=helm,name=${HELM_RELEASE_NAME}"
# What a healthy revision looks like in `helm history -o json` output. The
# encoder emits compact `"status":"deployed"`; the pattern tolerates spacing
# so a Helm formatting change cannot silently blind the guard.
readonly HELM_DEPLOYED_STATUS_RE='"status"[[:space:]]*:[[:space:]]*"deployed"'

# The keypair the agent uses to reach its shell sandbox over SSH. Generated per
# run and thrown away with the lease: nothing outside this cluster ever sees it,
# and the next run's install gets a pair of its own.
readonly SANDBOX_SSH_KEY_TYPE="ed25519"
readonly SANDBOX_SSH_KEY_COMMENT="kube-agents-ci-eval"

# ─── 1. Validation & Pre-checks ───────────────────────────────────────────────
# Still required with the agent path on vertex_ai below: the judge reads it
# (JUDGE_API_KEY in ci-eval-pr.sh) and the chart's credentials secret carries it.
if [ -z "${GEMINI_API_KEY:-}" ]; then
  echo "ERROR: GEMINI_API_KEY environment variable is required"
  exit 1
fi

# Checked here rather than where the key is generated, because the failure it
# prevents is invisible for fifteen minutes: with no public half in
# platform-agent-secrets the chart renders no <name>-shell-authorized-keys, and
# the sandbox pod then sits in ContainerCreating on a `secret not found` mount
# error until step 6's rollout gate times out. Fail at second zero instead.
if ! command -v ssh-keygen >/dev/null 2>&1; then
  echo "ERROR: ssh-keygen is required to generate the shell sandbox keypair"
  exit 1
fi

# ─── 2. Configuration Environment Variables ───────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/ci-env.sh"
source "${SCRIPT_DIR}/../tags.env"
trap dump_prow_artifacts_on_failure EXIT
ensure_helm

RAW_PULL_SHA="${PULL_PULL_SHA:-latest}"
PULL_SHA_SHORT="${RAW_PULL_SHA:0:7}"
export TAG="pr-${PULL_NUMBER:-local}-${PULL_SHA_SHORT:-latest}"
export AR_REPO="${AR_REPO:-us-central1-docker.pkg.dev/${PROJECT_ID}/kube-agents}"

export IMG="${AR_REPO}/kube-agents-operator:${TAG}"
export AGENT_IMAGE="${AR_REPO}/platform-agent"
export AGENT_TAG="${TAG}"
export IMAGE_TAG="${TAG}"

# ─── 2a. Image Source: Pull Request Build, or Published Release Candidate ─────
# RC_COMMIT_SHA unset is the presubmit and everything this script did before the
# variable existed: build the pull request's images into the leased project's
# Artifact Registry and install those. Set, it is the release-candidate eval —
# the candidate's images are already published, so there is nothing to build and
# rebuilding would measure a different artefact than the one being released.
#
# hack/resolve-rc-target.sh produces the value and documents why the caller, not
# this script, checks the tree out at that commit.
#
# The two paths differ in registry AND in image name: Artifact Registry carries
# `kube-agents-operator`, the published one is `k8s-operator`. That is why the
# RC path drops the repository overrides rather than rewriting them — the chart
# already defaults every image to the published GHCR path, so only the tag has
# to be said. The same drop is what carries the credential-proxy sidecar across:
# it is not a chart value at all, the operator derives it from the agent image by
# rewriting the trailing path element and keeping the tag
# (resolveCredentialProxyImage in k8s-operator/internal/controller/
# platformagent_manifests.go), so it follows whichever repository the agent uses
# without being named here. Both plugin images default to enabled=false and are
# not rendered on either path.
if [ -n "${RC_COMMIT_SHA:-}" ]; then
  # Sourced inside the branch, deliberately. The presubmit path must not acquire
  # a second file's exports and functions just because this one exists.
  # shellcheck source=scripts/release/common.sh
  source "${SCRIPT_DIR}/../scripts/release/common.sh"
  RC_REGISTRY_PREFIX="$(get_registry_prefix)"

  # The checkout contract, enforced rather than only documented. Only the images
  # come from RC_COMMIT_SHA; the chart, the CRDs, bench/tasks and bench/tf/fleet
  # all come from the tree this runs in, so a caller that sets the variable
  # without checking the tree out first gets a run that installs the candidate's
  # images against another revision's everything-else and reports an ordinary
  # green verdict for a combination that will never ship. Nothing else in the
  # run can notice that, which is why it fails here instead of warning.
  RC_TREE_SHA="$(git -C "${SCRIPT_DIR}/.." rev-parse HEAD 2>/dev/null || echo "")"
  if [ "${RC_TREE_SHA}" != "${RC_COMMIT_SHA}" ]; then
    echo "ERROR: RC_COMMIT_SHA is ${RC_COMMIT_SHA} but this tree is at ${RC_TREE_SHA:-an unknown commit}."
    echo "       Check the tree out at the candidate first, then re-run:"
    echo "         git checkout --detach ${RC_COMMIT_SHA}"
    echo "       hack/resolve-rc-target.sh's header explains why the checkout is"
    echo "       the caller's job and cannot be done from inside this script."
    exit 1
  fi

  # Cheap, and 15 minutes earlier than the alternative. Without it a missing
  # image surfaces as `helm --wait` timing out on ImagePullBackOff, which reads
  # as a broken chart rather than an unpublished commit. resolve-rc-target.sh
  # checks the same thing; this repeats it because RC_COMMIT_SHA can be set by
  # hand, and because that script is not what a Prow job is obliged to call.
  if ! check_commit_images_exist "${RC_COMMIT_SHA}"; then
    echo "ERROR: ${RC_REGISTRY_PREFIX} has no complete image set at ${RC_COMMIT_SHA}."
    echo "       Required: ${REQUIRED_RELEASE_IMAGES[*]}"
    echo "       docker-publish-ghcr.yml runs on every push to main; a queued or"
    echo "       failed run leaves the commit with no images to install."
    exit 1
  fi

  export TAG="${RC_COMMIT_SHA}"
  export IMG="${RC_REGISTRY_PREFIX}/k8s-operator:${TAG}"
  export AGENT_IMAGE="${RC_REGISTRY_PREFIX}/platform-agent"
  export AGENT_TAG="${TAG}"
  export IMAGE_TAG="${TAG}"

  IMAGE_ARGS=(
    --set-string "operator.image.tag=${TAG}"
    --set-string "platformAgent.deployment.image.tag=${TAG}"
    --set-string "agentSandbox.image.tag=${TAG}"
  )
  DEPLOY_SOURCE="release candidate ${RC_COMMIT_SHA:0:7} from ${RC_REGISTRY_PREFIX}"
else
  IMAGE_ARGS=(
    --set-string "operator.image.repository=${AR_REPO}/kube-agents-operator"
    --set-string "operator.image.tag=${TAG}"
    --set-string "platformAgent.deployment.image.repository=${AR_REPO}/platform-agent"
    --set-string "platformAgent.deployment.image.tag=${TAG}"
    --set-string "agentSandbox.image.repository=${AR_REPO}/agent-sandbox"
    --set-string "agentSandbox.image.tag=${TAG}"
  )
  DEPLOY_SOURCE="PR #${PULL_NUMBER:-local} build (${TAG})"
fi

# vertex_ai, not gemini. On 2026-09-02 every smoke run redded on 429s from the
# Gemini Developer API key's fixed paid-tier-3 quota -- 8,000,000 input
# tokens/minute for gemini-3.1-pro, named in the error body -- with five
# concurrent builds drawing ~94k-token turns from one shared minute window
# (#1097; the full diagnosis with build artifacts is on #1184). Vertex AI
# serves the same Gemini models under dynamic shared quota, with no fixed
# per-minute wall, and authenticates the LiteLLM pod through Workload Identity
# (the kubeagents-litellm KSA annotation below) instead of the API key.
# Overridable so a rollout problem is a job-env flip rather than a revert.
# Anyone flipping MODEL_DEFAULT_NAME: ci-eval-pr.sh stamps records with its
# own AGENT_MODEL_OVERRIDE, so flip both or the eval version key names a
# model the install is not serving.
export MODEL_PROVIDER="${MODEL_PROVIDER:-vertex_ai}"
export MODEL_DEFAULT_NAME="${MODEL_DEFAULT_NAME:-gemini-3.1-pro-preview}"
# Default to enforcing CMEK database encryption on CI evaluation clusters.
# Set ALLOW_UNENCRYPTED_SECRETS=true to bypass CMEK checks on unencrypted test clusters.
export ALLOW_UNENCRYPTED_SECRETS="${ALLOW_UNENCRYPTED_SECRETS:-false}"

export KSA_NAME="kubeagents-platform-agent"
export GSA_NAME="kubeagents-platform-gsa"
# The gateway's own identity, deliberately not GSA_NAME: LiteLLM is a
# network-exposed proxy forwarding attacker-influenceable prompt content, so
# it holds roles/aiplatform.user and nothing else (the site's
# security-and-iam.md, "The Vertex AI gateway is a separate identity"). The
# pair must exist in the leased PROJECT_ID before a vertex_ai deploy;
# provision_ci_pool_project.sh creates it, verify_ci_pool_project.py checks
# it, and the site's deploy/ci-pool-projects.md carries the hand repair.
export LITELLM_GSA_NAME="kubeagents-litellm-gsa"
export MEMORY_ENABLED="false"
export USER_PROFILE_ENABLED="false"
export GOOGLE_CHAT_ENABLED="false"
export SLACK_ENABLED="false"

# ─── 2b. GitOps Repository for This Run ───────────────────────────────────────
# Every GitHub-writing eval scenario begins by reading the `Git Repo:` line out
# of /opt/data/SETTINGS.md — the fleet-audit streams do it in `audit_report.py
# start`, before anything else happens. The operator renders that line from
# spec.integration.github.gitRepo on the PlatformAgent CR
# (buildSettingsConfigMap in k8s-operator/internal/controller/
# platformagent_manifests.go); with the field unset it writes the literal
# "None" and those scenarios stop at step 0 with nothing to clone.
#
# CI supplies the value and deliberately does NOT lean on the chart default.
# On the presubmit path everything this job deploys — chart, operator, agent —
# is built from the pull request, so a PR that blanks
# `platformAgent.integration.github.gitRepo` in values.yaml, or breaks the
# CR-to-SETTINGS.md rendering, is precisely the regression the eval should catch
# as a failed scenario. It can only catch it if the value the run is supposed to
# use arrives from outside the artefacts under test. The release-candidate path
# installs published images instead of built ones, which narrows what is under
# test without changing this argument: the chart still comes from the checkout,
# and supplying the value from outside is what keeps either artefact from
# choosing it. Note this is a *correctness* argument, not the containment
# boundary: what a run can actually write to is fixed by which repositories the
# GitHub App is installed on, which no PR can change. See
# docs/site/src/content/docs/deploy/ci-pool-projects.md.
#
# One GitOps repo per leasable project, so two concurrent leases can never
# share a ledger issue or race on a remediation branch. Onboarding a further
# project (issue #637, Boskos leasing) is one line here plus its row in that
# same doc — no other edit in this file.
#
# A mapping here is a claim that the repo exists and that App 4675512 is
# installed on it. It is not self-verifying: with the line present and either
# of those missing, the deploy succeeds and every GitHub-writing scenario
# fails at `audit_report.py start` with a clone or token error instead of the
# named, actionable refusal below. Add the row when the repo and the
# installation are real, not when the project joins the Boskos pool — the two
# are separate events, and kube-agents-evals-3 is what happens when they are
# assumed to be one.
gitops_repo_for_project() {
  case "$1" in
    kube-agents-evals) echo "gke-agentic/kube-agents-evals-infra" ;;
    kube-agents-evals-2) echo "gke-agentic/kube-agents-evals-2-infra" ;;
    kube-agents-evals-3) echo "gke-agentic/kube-agents-evals-3-infra" ;;
    kube-agents-evals-4) echo "gke-agentic/kube-agents-evals-4-infra" ;;
    kube-agents-evals-5) echo "gke-agentic/kube-agents-evals-5-infra" ;;
    kube-agents-evals-6) echo "gke-agentic/kube-agents-evals-6-infra" ;;
    kube-agents-evals-7) echo "gke-agentic/kube-agents-evals-7-infra" ;;
    kube-agents-evals-8) echo "gke-agentic/kube-agents-evals-8-infra" ;;
    kube-agents-evals-9) echo "gke-agentic/kube-agents-evals-9-infra" ;;
    kube-agents-evals-10) echo "gke-agentic/kube-agents-evals-10-infra" ;;
    kube-agents-evals-11) echo "gke-agentic/kube-agents-evals-11-infra" ;;
    kube-agents-evals-12) echo "gke-agentic/kube-agents-evals-12-infra" ;;
    kube-agents-evals-13) echo "gke-agentic/kube-agents-evals-13-infra" ;;
    kube-agents-evals-14) echo "gke-agentic/kube-agents-evals-14-infra" ;;
    kube-agents-evals-15) echo "gke-agentic/kube-agents-evals-15-infra" ;;
    kube-agents-evals-16) echo "gke-agentic/kube-agents-evals-16-infra" ;;
    kube-agents-evals-17) echo "gke-agentic/kube-agents-evals-17-infra" ;;
    kube-agents-evals-18) echo "gke-agentic/kube-agents-evals-18-infra" ;;
    kube-agents-evals-19) echo "gke-agentic/kube-agents-evals-19-infra" ;;
    kube-agents-evals-20) echo "gke-agentic/kube-agents-evals-20-infra" ;;
    kube-agents-evals-21) echo "gke-agentic/kube-agents-evals-21-infra" ;;
    kube-agents-evals-22) echo "gke-agentic/kube-agents-evals-22-infra" ;;
    kube-agents-evals-23) echo "gke-agentic/kube-agents-evals-23-infra" ;;
    kube-agents-evals-24) echo "gke-agentic/kube-agents-evals-24-infra" ;;
    kube-agents-evals-25) echo "gke-agentic/kube-agents-evals-25-infra" ;;
    kube-agents-evals-26) echo "gke-agentic/kube-agents-evals-26-infra" ;;
    kube-agents-evals-27) echo "gke-agentic/kube-agents-evals-27-infra" ;;
    kube-agents-evals-28) echo "gke-agentic/kube-agents-evals-28-infra" ;;
    kube-agents-evals-29) echo "gke-agentic/kube-agents-evals-29-infra" ;;
    kube-agents-evals-30) echo "gke-agentic/kube-agents-evals-30-infra" ;;
    *) return 1 ;;
  esac
}

# PULL_NUMBER and JOB_NAME are set by Prow and by nothing else, which is what
# separates a leased CI run from a laptop. The two get different treatment
# below, but neither gets a silent default: an unmapped project stops the
# deploy rather than installing an agent that writes somewhere unintended or
# nowhere at all.
if [ -n "${PULL_NUMBER:-}" ] || [ -n "${JOB_NAME:-}" ]; then
  IS_PROW_RUN="true"
else
  IS_PROW_RUN="false"
fi

# The override exists for developers, and only for them. Under Boskos the
# project is leased per run, so a value pinned in the job environment would
# eventually point one project's run at another project's GitOps repo — the
# one failure mode worth refusing outright.
if [ "${IS_PROW_RUN}" = "true" ] && [ -n "${EVAL_GITOPS_REPO:-}" ]; then
  echo "ERROR: EVAL_GITOPS_REPO is set in a Prow run (PROJECT_ID=${PROJECT_ID})." >&2
  echo "       The GitOps repo must follow the leased project, so CI resolves it from" >&2
  echo "       gitops_repo_for_project() in hack/ci-deploy.sh. Unset EVAL_GITOPS_REPO," >&2
  echo "       and map the project there if it is missing." >&2
  exit 1
fi

if [ -n "${EVAL_GITOPS_REPO:-}" ]; then
  GITOPS_REPO="${EVAL_GITOPS_REPO}"
  echo "GitOps repo: ${GITOPS_REPO} (from EVAL_GITOPS_REPO)"
elif GITOPS_REPO="$(gitops_repo_for_project "${PROJECT_ID}")"; then
  echo "GitOps repo: ${GITOPS_REPO} (mapped from PROJECT_ID=${PROJECT_ID})"
elif [ "${IS_PROW_RUN}" = "true" ]; then
  echo "ERROR: no GitOps repo is mapped for PROJECT_ID=${PROJECT_ID}." >&2
  echo "       Every project in the kube-agents-evals-project Boskos pool needs its own" >&2
  echo "       private GitOps repo; deploying without one would leave the fleet-audit and" >&2
  echo "       rca-remediation-pr scenarios failing at step 0 for a reason no log explains." >&2
  echo "       Add the project to gitops_repo_for_project() in hack/ci-deploy.sh and follow" >&2
  echo "       docs/site/src/content/docs/deploy/ci-pool-projects.md before registering it" >&2
  echo "       in the pool." >&2
  exit 1
else
  echo "ERROR: no GitOps repo is mapped for PROJECT_ID=${PROJECT_ID}, and this is not a" >&2
  echo "       Prow run. A local deploy has no lease, so it has to say where it writes:" >&2
  echo "         EVAL_GITOPS_REPO=owner/repo  — your own throwaway GitOps repo" >&2
  echo "         EVAL_GITOPS_REPO=none        — deploy with the GitHub integration off" >&2
  echo "                                        (SETTINGS.md gets 'Git Repo: None', and" >&2
  echo "                                        every GitHub-writing scenario will fail)" >&2
  exit 1
fi

# "none" is the explicit opt-out, and the only route to an empty gitRepo. An
# empty string here makes the chart omit spec.integration.github entirely.
if [ "${GITOPS_REPO}" = "none" ]; then
  echo "GitHub integration: disabled for this deploy (EVAL_GITOPS_REPO=none)"
  GITOPS_REPO=""
elif ! printf '%s' "${GITOPS_REPO}" | grep -Eq '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$'; then
  echo "ERROR: GitOps repo '${GITOPS_REPO}' is not in owner/repo form." >&2
  echo "       The minty rule ConfigMap is keyed on the org and repo separately, so the" >&2
  echo "       shorthand is what CI passes — not a URL." >&2
  exit 1
fi

# The in-cluster half of the token path. gitRepo alone only tells the agent
# where to clone; github-token-minter is what turns the platform GSA's OIDC
# identity into a repo-scoped GitHub App token (agents/platform/scripts/
# github_token_refresh.py has no other source, and strips any inherited
# GITHUB_TOKEN).
#
# Off unless EVAL_GITHUB_APP_ID is set, because the minter cannot come up until
# a human has done the two things terraform cannot: install the GitHub App on
# this project's GitOps repo, and import the App's private key into the
# project's KMS signing key. Until then the pod fails its readiness probe, and
# since the minter Deployment is part of this release, `helm --wait` below
# would fail every PR. Setting EVAL_GITHUB_APP_ID is therefore the switch that
# says "the manual half is done for this project" — and if it is not, the
# deploy failing loudly is the right outcome.
#
# The pool's App is kube-agents-evals-token-minter, id 4675512, installed on
# the three *-infra repos above and nothing else. One App for the whole pool, so
# the value is the same in every project's job environment; what is per-project
# is the KMS key its PEM was imported into. That installation list -- not this
# script, and not the minty rule the chart renders -- is what bounds where a
# run can write, because a presubmit deploys the pull request's own chart and
# could otherwise rewrite either of them.
#
# githubMinter.allowedServiceAccount is left at its default, which derives
# kubeagents-platform-gsa@<harness.projectId> — exactly the GSA_NAME/PROJECT_ID
# pair this deploy annotates the agent KSA with, so the rule is keyed on this
# project's platform GSA and no other's.
if [ -n "${GITOPS_REPO}" ] && [ -n "${EVAL_GITHUB_APP_ID:-}" ]; then
  GITHUB_MINTER_ARGS=(
    --set "githubMinter.enabled=true"
    --set-string "githubMinter.org=${GITOPS_REPO%%/*}"
    --set-string "githubMinter.repo=${GITOPS_REPO##*/}"
    --set-string "githubMinter.appId=${EVAL_GITHUB_APP_ID}"
  )
  echo "GitHub token minter: enabled for ${GITOPS_REPO} (app ${EVAL_GITHUB_APP_ID})"
else
  GITHUB_MINTER_ARGS=(--set "githubMinter.enabled=false")
  echo "GitHub token minter: disabled (EVAL_GITHUB_APP_ID unset) — the agent can read" \
    "SETTINGS.md but cannot mint a token, so GitHub-writing scenarios will fail."
fi

# ─── 2c. Image Build Worker ───────────────────────────────────────────────────
# Where the image builds run. Either a private worker pool or a sized machine
# on the default pool -- never both, because a pool declares its own machine
# and rejects being told a different one.
#
# Opt into a pool by exporting CLOUD_BUILD_WORKER_POOL as a full resource name:
# projects/PROJECT/locations/REGION/workerPools/POOL. Unset by default, which
# is the CI path. The region is read back out of that name because
# `gcloud builds submit` otherwise falls back to the `global` region, which
# cannot reach a regional pool.
if [ -n "${CLOUD_BUILD_WORKER_POOL:-}" ]; then
  case "$CLOUD_BUILD_WORKER_POOL" in
    projects/*/locations/*/workerPools/*) ;;
    *)
      echo "ERROR: CLOUD_BUILD_WORKER_POOL must be a full resource name: projects/PROJECT/locations/REGION/workerPools/POOL"
      exit 1
      ;;
  esac
  BUILD_WORKER_ARGS=(
    --worker-pool="$CLOUD_BUILD_WORKER_POOL"
    --region="$(echo "$CLOUD_BUILD_WORKER_POOL" | cut -d'/' -f4)"
  )
else
  # The default pool's unspecified machine is two vCPUs, which is most of why
  # the image builds are the single largest phase of this job. The build also
  # runs the operator step alongside the agent build (see
  # deploy/docker/cloudbuild-ci.yaml), and that is only real overlap on a
  # worker with cores to spare rather than two contending for the same pair.
  BUILD_WORKER_ARGS=(--machine-type=e2-highcpu-8)
fi

START_TIME=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Deploying ${DEPLOY_SOURCE} to Namespace: ${NAMESPACE} ==="

# ─── 3. Cluster Auth ──────────────────────────────────────────────────────────
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Authenticating to GKE Cluster ==="
gke_dns_endpoint_flag "$CLUSTER_NAME" "$REGION" "$PROJECT_ID"
# Unquoted on purpose: empty must contribute no argument. See gke_dns_endpoint.sh.
# shellcheck disable=SC2086
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet \
  $GKE_DNS_ENDPOINT_FLAG
echo "✓ Cluster authentication finished in $((SECONDS - STEP_START))s"

# ─── 4. Build Container Images ────────────────────────────────────────────────
# Skipped whole on the release-candidate path: the candidate's images are what
# is being evaluated, and a rebuild from the same source is a different artefact
# — different base-image digests, different build timestamps, and a different
# _KUBE_AGENTS_VERSION baked in as the remote-MCP User-Agent. An eval that graded
# a rebuild would not be grading the thing the release ships.
if [ -n "${RC_COMMIT_SHA:-}" ]; then
  echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Skipping image builds: installing published images at ${RC_COMMIT_SHA:0:7} ==="
else
  STEP_START=$SECONDS
  echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Building Container Images (platform, credential-proxy, sandbox, operator) ==="
  # One submit, not four. The two agent images share the agent-base chain, so
  # building them as consecutive steps on one worker lets the second reuse the
  # first's layers instead of rebuilding that chain on a cold daemon; the sandbox
  # and operator builds run alongside them. See the header of cloudbuild-ci.yaml,
  # and #635.
  # Set REQUIRE_CACHE=true in the job environment to fail the build on a cache
  # miss instead of cold-building. Default false so a broken cache source cannot
  # block the PR that fixes it.
  export CACHE_IMAGE="${CACHE_IMAGE:-us-docker.pkg.dev/kube-agents-prow/kube-agents/platform-agent:latest}"
  # The postsubmit's mode=max cache manifests; CACHE_IMAGE stays the fallback.
  export BUILDCACHE_IMAGE="${BUILDCACHE_IMAGE:-us-docker.pkg.dev/kube-agents-prow/kube-agents/platform-agent:buildcache}"
  export PROXY_BUILDCACHE_IMAGE="${PROXY_BUILDCACHE_IMAGE:-us-docker.pkg.dev/kube-agents-prow/kube-agents/credential-proxy:buildcache}"
  gcloud builds submit --config="deploy/docker/cloudbuild-ci.yaml" \
    --substitutions="_PLATFORM_URI=${AR_REPO}/platform-agent:${TAG},_PROXY_URI=${AR_REPO}/credential-proxy:${TAG},_SANDBOX_URI=${AR_REPO}/agent-sandbox:${TAG},_OPERATOR_URI=${AR_REPO}/kube-agents-operator:${TAG},_CACHE_IMAGE=${CACHE_IMAGE},_BUILDCACHE_IMAGE=${BUILDCACHE_IMAGE},_PROXY_BUILDCACHE_IMAGE=${PROXY_BUILDCACHE_IMAGE},_HERMES_AGENT_TAG=${HERMES_AGENT_TAG},_KUBE_AGENTS_VERSION=${TAG},_REQUIRE_CACHE=${REQUIRE_CACHE:-false}" \
    --project="${PROJECT_ID}" "${BUILD_WORKER_ARGS[@]}" --quiet .
  echo "✓ Container image builds finished in $((SECONDS - STEP_START))s"
fi

# ─── 5. Chart Deployment ──────────────────────────────────────────────────────
# One helm release carries the whole install — operator, credentials Secret,
# agent CR, and LiteLLM — so there is nothing to apply piecemeal or keep in order.
#
# The chart is `./charts/kube-agents`, out of the checkout, on both paths. On the
# release-candidate path that makes the checkout load-bearing: the images come
# from RC_COMMIT_SHA and the chart, CRDs and eval tasks come from whatever tree
# this runs in, so the caller has to have checked the tree out at that commit
# first or the run grades the candidate's images against another revision's
# everything-else. hack/resolve-rc-target.sh is where that contract is written
# down, and .github/workflows/deploy-environment.yml is the existing precedent
# for honouring it with a checkout step.
# Webhooks stay at the chart's default (off): a PR evaluation cluster carries
# no cert-manager, and admission-webhook coverage belongs to the operator's
# own test suite rather than this smoke pipeline.
#
# runtimeClassName is pinned empty rather than left at the chart's default,
# which is `gvisor`. Step 7 reaches the agent over `kubectl port-forward`, and
# that does not work against a sandboxed pod -- the forward is set up in the
# host-side CNI netns while the listener lives in the sandbox's own network
# stack, so the connection is refused (scripts/exec_tunnel.py is canonical on
# this). On a pool cluster with no `gvisor` RuntimeClass the pod would not
# schedule at all. Either way this job wants the standard runtime; what the
# sandbox does to the agent is the release pipeline's to exercise, not a smoke
# test's.
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Deploying the kube-agents chart ==="

# ─── 5a. Heal a poisoned release record (#1172) ───────────────────────────────
# A failed or killed prior run can leave the release record behind with no
# deployed revision: its teardown's `helm uninstall` failed, or the teardown
# was killed mid-uninstall — the cause no teardown-side fallback can cover.
# `helm upgrade --install` below then takes the upgrade path and dies with
# `UPGRADE FAILED: "kube-agents" has no deployed releases`, instantly
# failing whichever PR drew this pool project. Heal it here, at lease time,
# where every cause of the no-deployed-revision state converges. (A release
# stuck `pending-upgrade` *above* a deployed revision is a different state —
# upgrade then fails on Helm's in-progress lock, but that run's own teardown
# uninstall clears it, so it burns one run rather than poisoning the pool.)
#
# The probe is `helm history -o json` because it reads the same store the
# failing code path reads: Helm's upgrade errors in Releases.Deployed()
# (pkg/action/upgrade.go) when no release-record Secret carries status
# "deployed", and `helm history` lists exactly those record Secrets with
# their statuses. "History succeeds but no revision is deployed" is
# therefore precisely the state upgrade rejects — including a latest-failed
# release with an older deployed revision, which upgrades fine and is left
# alone. One call; a healthy or absent release costs the probe and nothing
# more.
if RELEASE_HISTORY_JSON="$(helm history "${HELM_RELEASE_NAME}" -n "${NAMESPACE}" -o json 2>/dev/null)" \
  && ! grep -Eq "${HELM_DEPLOYED_STATUS_RE}" <<<"${RELEASE_HISTORY_JSON}"; then
  echo "WARNING: the ${HELM_RELEASE_NAME} release record exists with no deployed revision —"
  echo "         a previous run left this pool project poisoned (#1172). Clearing the"
  echo "         record before installing."
  # --no-hooks: the pre-delete hook waits on an operator a failed install
  # never started. If even the uninstall cannot clear it, drop the
  # release-record Secrets directly — with no deployed revision there is
  # nothing real for Helm to unwind, and the record is all that blocks the
  # install. Both failing leaves the record in place, so let set -e stop
  # the run here, before the upgrade fails less legibly. No --wait and no
  # hooks means Helm's uninstall timeout would bound nothing, so none is
  # passed.
  helm uninstall "${HELM_RELEASE_NAME}" -n "${NAMESPACE}" --no-hooks \
    || kubectl delete secret -n "${NAMESPACE}" -l "${HELM_RELEASE_SECRET_SELECTOR}" --ignore-not-found
  echo "✓ Cleared the poisoned ${HELM_RELEASE_NAME} release record"
fi

API_SERVER_KEY="${API_SERVER_KEY:-$(openssl rand -hex 16)}"

# ─── 5b. The shell sandbox keypair ────────────────────────────────────────────
# The chart cannot generate this one — sprig emits PEM and has no encoder for
# authorized_keys form — so every install surface supplies it: `install.sh`
# through the Terraform composition's tls_private_key, `upgrade.sh` through
# backfill_sandbox_ssh_key, and this job here. Without it the chart renders no
# authorized-keys Secret and the sandbox never starts.
#
# --set-file rather than --set-string: the private half is a PEM, and Helm's
# --set parser reads its newlines and commas as syntax. Both halves go into
# credentials.data, which is where the chart's authorized-keys template reads
# the public one from and where the gateway's init container finds the private
# one.
SANDBOX_KEY_DIR="$(umask 077 && mktemp -d)"
ssh-keygen -q -t "${SANDBOX_SSH_KEY_TYPE}" -N '' -C "${SANDBOX_SSH_KEY_COMMENT}" \
  -f "${SANDBOX_KEY_DIR}/id_sandbox"

helm upgrade --install "${HELM_RELEASE_NAME}" ./charts/kube-agents \
  --namespace "${NAMESPACE}" --create-namespace \
  "${IMAGE_ARGS[@]}" \
  --set-string "platformAgent.harness.clusterName=${CLUSTER_NAME}" \
  --set-string "platformAgent.harness.location=${REGION}" \
  --set-string "platformAgent.harness.projectId=${PROJECT_ID}" \
  --set-string "platformAgent.security.serviceAccountAnnotations.iam\.gke\.io/gcp-service-account=${GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-string "platformAgent.integration.github.gitRepo=${GITOPS_REPO}" \
  "${GITHUB_MINTER_ARGS[@]}" \
  --set "platformAgent.credentials.create=true" \
  --set-string "platformAgent.credentials.data.API_SERVER_KEY=${API_SERVER_KEY}" \
  --set-string "platformAgent.credentials.data.GEMINI_API_KEY=${GEMINI_API_KEY}" \
  --set-file "platformAgent.credentials.data.SANDBOX_SSH_PRIVATE_KEY=${SANDBOX_KEY_DIR}/id_sandbox" \
  --set-file "platformAgent.credentials.data.SANDBOX_SSH_PUBLIC_KEY=${SANDBOX_KEY_DIR}/id_sandbox.pub" \
  --set-string "litellm.modelProvider=${MODEL_PROVIDER}" \
  --set-string "litellm.modelDefaultName=${MODEL_DEFAULT_NAME}" \
  --set-string "litellm.vertex.serviceAccountAnnotations.iam\.gke\.io/gcp-service-account=${LITELLM_GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set "platformAgent.deployment.availability.runtimeClassName=" \
  --set-string "platformAgent.deployment.env[0].name=ALERT_DAILY_LIMIT_WARNING" \
  --set-string "platformAgent.deployment.env[0].value=${EVAL_ALERT_DAILY_LIMIT_WARNING}" \
  --wait --timeout 15m
# Deleted here rather than from the EXIT trap, which two later steps replace.
# A failed install leaves the directory behind in a pod prow destroys with the
# lease, and nothing uploads it — /logs/artifacts is the only path off this box.
rm -rf "${SANDBOX_KEY_DIR}"
echo "✓ Chart deployment finished in $((SECONDS - STEP_START))s"

# ─── 6. Readiness Verification ────────────────────────────────────────────────
# helm --wait covers the chart-created Deployments (operator, LiteLLM); the
# agent Deployment is created by the operator reconciling the CR, so it gets
# its own gate with diagnostics.
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Verifying platform-agent rollout ==="
for i in {1..60}; do
  kubectl get deployment platform-agent-gateway -n "${NAMESPACE}" >/dev/null 2>&1 && break
  sleep 5
done
if ! kubectl rollout status deployment/platform-agent-gateway -n "${NAMESPACE}" --timeout=600s; then
  echo "ERROR: platform-agent-gateway rollout failed"
  kubectl describe deployment/platform-agent-gateway -n "${NAMESPACE}" || true
  kubectl get pods -n "${NAMESPACE}" || true
  kubectl logs -n "${NAMESPACE}" -l app=platform-agent-gateway --all-containers --tail=50 || true
  exit 1
fi

# The shell sandbox is the other half of the agent: everything the model runs
# executes there over ssh, so a gateway that is Ready against a StatefulSet
# stuck on ImagePullBackOff is an install this job must fail rather than pass.
# Gated separately for the same reason the Deployment is -- the operator
# creates it from the CR, so `helm --wait` never saw it.
for i in {1..60}; do
  kubectl get statefulset platform-agent-shell -n "${NAMESPACE}" >/dev/null 2>&1 && break
  sleep 5
done
if ! kubectl rollout status statefulset/platform-agent-shell -n "${NAMESPACE}" --timeout=600s; then
  echo "ERROR: platform-agent-shell rollout failed"
  kubectl describe statefulset/platform-agent-shell -n "${NAMESPACE}" || true
  kubectl get pods -n "${NAMESPACE}" || true
  kubectl logs -n "${NAMESPACE}" statefulset/platform-agent-shell --all-containers --tail=50 || true
  exit 1
fi
echo "✓ Rollout verification finished in $((SECONDS - STEP_START))s"

# ─── 7. Agent API Connectivity Verification ──────────────────────────────────
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Verifying Platform Agent API Connectivity ==="
API_KEY="$(kubectl get secret platform-agent-secrets -n "${NAMESPACE}" -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode)"

# On cold autoscaling pools the API-server tunnel behind `kubectl port-forward`
# drops mid-request ("error: lost connection to pod" with the gateway pod
# healthy throughout), and a dead port-forward never comes back on its own —
# so every attempt gets a fresh tunnel, and only the response decides health.
PF_PID=""
cleanup_pf_and_dump() {
  kill "${PF_PID:-}" 2>/dev/null || true
  dump_prow_artifacts_on_failure
}
trap cleanup_pf_and_dump EXIT

CONNECTIVITY_ATTEMPTS=5
CONNECTIVITY_OK="false"
: >/tmp/pf-8642.log
for ((attempt = 1; attempt <= CONNECTIVITY_ATTEMPTS; attempt++)); do
  # Kill any previous tunnel and start a fresh one; the log is appended so a
  # failure dump shows every attempt, not just the last.
  if [ -n "${PF_PID}" ]; then
    kill "${PF_PID}" 2>/dev/null || true
    wait "${PF_PID}" 2>/dev/null || true
  fi
  echo "--- port-forward attempt ${attempt}/${CONNECTIVITY_ATTEMPTS} ---" >>/tmp/pf-8642.log
  kubectl port-forward svc/platform-agent -n "${NAMESPACE}" 8642:8642 >>/tmp/pf-8642.log 2>&1 &
  PF_PID=$!

  echo "Waiting for platform-agent port-forward on port 8642 (attempt ${attempt}/${CONNECTIVITY_ATTEMPTS})..."
  for i in {1..30}; do
    if nc -z localhost 8642 2>/dev/null; then
      break
    fi
    sleep 1
  done

  HEALTH_RESP="$(curl -s --max-time 120 -X POST http://localhost:8642/v1/responses \
    -H "Authorization: Bearer ${API_KEY}" \
    -H "Content-Type: application/json" \
    -d '{"model": "model-default", "input": "ping"}' || true)"

  if [[ "$HEALTH_RESP" == *"output"* || "$HEALTH_RESP" == *"assistant"* || "$HEALTH_RESP" == *"pong"* ]]; then
    CONNECTIVITY_OK="true"
    break
  fi
  if [ -z "${HEALTH_RESP}" ]; then
    FAIL_REASON="empty response after port-forward drop"
  else
    FAIL_REASON="unexpected response: ${HEALTH_RESP}"
  fi
  if [ "${attempt}" -lt "${CONNECTIVITY_ATTEMPTS}" ]; then
    echo "connectivity attempt ${attempt}/${CONNECTIVITY_ATTEMPTS} failed: ${FAIL_REASON}; respawning tunnel"
  else
    echo "connectivity attempt ${attempt}/${CONNECTIVITY_ATTEMPTS} failed: ${FAIL_REASON}"
  fi
done

kill "${PF_PID:-}" 2>/dev/null || true
trap dump_prow_artifacts_on_failure EXIT

if [ "${CONNECTIVITY_OK}" = "true" ]; then
  echo "✓ Agent API Server responded successfully in $((SECONDS - STEP_START))s!"
else
  echo "ERROR: Platform Agent API server connectivity check failed after ${CONNECTIVITY_ATTEMPTS} attempts!"
  echo "Response received: ${HEALTH_RESP}"
  echo "=== Debug: Port Forward Log (tail) ==="
  tail -n 40 /tmp/pf-8642.log 2>/dev/null || true
  echo "=== Debug: Kubernetes Workloads in Namespace ${NAMESPACE} ==="
  kubectl get pods,svc -n "${NAMESPACE}" || true
  exit 1
fi

TOTAL_DURATION=$((SECONDS - START_TIME))
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Deployment Ready in Namespace: ${NAMESPACE} (Total Duration: ${TOTAL_DURATION}s) ==="
