#!/usr/bin/env bash
# ==============================================================================
# Prow CI Deployment Pipeline Script
# ==============================================================================
# The evaluation cluster and its IAM are pre-configured; this script builds
# the PR's images and deploys the kube-agents chart onto that cluster.
# ==============================================================================

set -euo pipefail

# ─── 1. Validation & Pre-checks ───────────────────────────────────────────────
if [ -z "${GEMINI_API_KEY:-}" ]; then
  echo "ERROR: GEMINI_API_KEY environment variable is required"
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

export MODEL_PROVIDER="gemini"
export MODEL_DEFAULT_NAME="gemini-3.1-pro-preview"
# Default to enforcing CMEK database encryption on CI evaluation clusters.
# Set ALLOW_UNENCRYPTED_SECRETS=true to bypass CMEK checks on unencrypted test clusters.
export ALLOW_UNENCRYPTED_SECRETS="${ALLOW_UNENCRYPTED_SECRETS:-false}"

export KSA_NAME="kubeagents-platform-agent"
export GSA_NAME="kubeagents-platform-gsa"
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
# Everything this job deploys — chart, operator, agent — is built from the pull
# request, so a PR that blanks `platformAgent.integration.github.gitRepo` in
# values.yaml, or breaks the CR-to-SETTINGS.md rendering, is precisely the
# regression the eval should catch as a failed scenario. It can only catch it
# if the value the run is supposed to use arrives from outside the artefacts
# under test. Note this is a *correctness* argument, not the containment
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
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Deploying PR #${PULL_NUMBER:-local} (${TAG}) to Namespace: ${NAMESPACE} ==="

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
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Building Container Images (platform, credential-proxy, operator) ==="
# One submit, not three. The two agent images share the agent-base chain, so
# building them as consecutive steps on one worker lets the second reuse the
# first's layers instead of rebuilding that chain on a cold daemon; the operator
# build runs alongside them. See the header of cloudbuild-ci.yaml, and #635.
# Set REQUIRE_CACHE=true in the job environment to fail the build on a cache
# miss instead of cold-building. Default false so a broken cache source cannot
# block the PR that fixes it.
export CACHE_IMAGE="${CACHE_IMAGE:-us-docker.pkg.dev/kube-agents-prow/kube-agents/platform-agent:latest}"
# The postsubmit's mode=max cache manifests; CACHE_IMAGE stays the fallback.
export BUILDCACHE_IMAGE="${BUILDCACHE_IMAGE:-us-docker.pkg.dev/kube-agents-prow/kube-agents/platform-agent:buildcache}"
export PROXY_BUILDCACHE_IMAGE="${PROXY_BUILDCACHE_IMAGE:-us-docker.pkg.dev/kube-agents-prow/kube-agents/credential-proxy:buildcache}"
gcloud builds submit --config="deploy/docker/cloudbuild-ci.yaml" \
  --substitutions="_PLATFORM_URI=${AR_REPO}/platform-agent:${TAG},_PROXY_URI=${AR_REPO}/credential-proxy:${TAG},_OPERATOR_URI=${AR_REPO}/kube-agents-operator:${TAG},_CACHE_IMAGE=${CACHE_IMAGE},_BUILDCACHE_IMAGE=${BUILDCACHE_IMAGE},_PROXY_BUILDCACHE_IMAGE=${PROXY_BUILDCACHE_IMAGE},_HERMES_AGENT_TAG=${HERMES_AGENT_TAG},_REQUIRE_CACHE=${REQUIRE_CACHE:-false}" \
  --project="${PROJECT_ID}" "${BUILD_WORKER_ARGS[@]}" --quiet .
echo "✓ Container image builds finished in $((SECONDS - STEP_START))s"

# ─── 5. Chart Deployment ──────────────────────────────────────────────────────
# One helm release carries the whole install — operator, credentials Secret,
# agent CR, and LiteLLM — so there is nothing to apply piecemeal or keep in order.
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
API_SERVER_KEY="${API_SERVER_KEY:-$(openssl rand -hex 16)}"
helm upgrade --install kube-agents ./charts/kube-agents \
  --namespace "${NAMESPACE}" --create-namespace \
  --set-string "operator.image.repository=${AR_REPO}/kube-agents-operator" \
  --set-string "operator.image.tag=${TAG}" \
  --set-string "platformAgent.deployment.image.repository=${AR_REPO}/platform-agent" \
  --set-string "platformAgent.deployment.image.tag=${TAG}" \
  --set-string "platformAgent.harness.clusterName=${CLUSTER_NAME}" \
  --set-string "platformAgent.harness.location=${REGION}" \
  --set-string "platformAgent.harness.projectId=${PROJECT_ID}" \
  --set-string "platformAgent.security.serviceAccountAnnotations.iam\.gke\.io/gcp-service-account=${GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-string "platformAgent.integration.github.gitRepo=${GITOPS_REPO}" \
  "${GITHUB_MINTER_ARGS[@]}" \
  --set "platformAgent.credentials.create=true" \
  --set-string "platformAgent.credentials.data.API_SERVER_KEY=${API_SERVER_KEY}" \
  --set-string "platformAgent.credentials.data.GEMINI_API_KEY=${GEMINI_API_KEY}" \
  --set-string "litellm.modelProvider=${MODEL_PROVIDER}" \
  --set-string "litellm.modelDefaultName=${MODEL_DEFAULT_NAME}" \
  --set "platformAgent.deployment.availability.runtimeClassName=" \
  --wait --timeout 15m
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
