#!/usr/bin/env bash
#
# Seed b-0011's broken state from the GitOps repository instead of applying it.
# Runs during `tofu apply`, after the cluster exists and the run branch has been
# cut, and before the agent starts. Steps:
#
#   1. kubectl credentials for the task cluster;
#   2. metrics-server, exactly as the original b-0011 setup.sh (kubectl top is
#      part of the scenario);
#   3. Argo CD core (controller, repo-server, redis; no API server or UI) at a
#      pinned release;
#   4. a repository credential and one Application whose source is the task
#      directory on the run branch, automated sync with prune and self-heal;
#   5. wait for the Application to report Synced, then assert the seeded
#      condition holds: pricer 2/2 ready, checkout 2/4 ready with the rest
#      blocked by the payments quota, spec at 256Mi / :1.0.0 / 4 replicas.
#
# Why 2 and not the original's 3: the original reaches 3 only because its
# in-place rollout leaves one old 64Mi pod behind. A single sync of the broken
# state never creates that pod. Sync waves in the manifests (gating, then
# pricer, then the rest) keep pricer at 2/2, which the task's ready-floor
# safeguard requires from the first sample. See render-broken-base.sh.
#
# Nothing here writes to the cluster after the Application exists; from this
# point on, Argo is the only writer.
set -euo pipefail

: "${INFRA_PROVIDER:?}" "${CLUSTER_NAME:?}" "${KUBECONFIG:?}" "${WAIT_TIMEOUT:?}"
: "${GITOPS_REPO:?}" "${GITOPS_RUN_BRANCH:?}" "${GITOPS_TASK_PATH:?}" "${GITOPS_TOKEN_FILE:?}" "${ARGOCD_VERSION:?}"

export KUBECONFIG
APP_NAME="b-0011"
METRICS_SERVER_MANIFEST="https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.9.0/components.yaml"
POLL_SECONDS=3
PROFILE_HOME_MODE=2770
KUBECONFIG_FILE_MODE=664
PROFILE_NAME_PATTERN='^[a-z0-9-]+$'

# ---------------------------------------------------------------------------
# 1. credentials
# ---------------------------------------------------------------------------
if [ "${INFRA_PROVIDER}" = "gcp" ]; then
  : "${PROJECT_ID:?}" "${LOCATION:?}"
  echo "==> Fetching GKE credentials for ${CLUSTER_NAME} (${PROJECT_ID}, ${LOCATION})"
  gcloud container clusters get-credentials "${CLUSTER_NAME}" --location "${LOCATION}" --project "${PROJECT_ID}" --quiet
fi
kubectl cluster-info >/dev/null

# Bounded poll: wait_for <description> <timeout-seconds> <command...>
# Succeeds when the command's stdout equals $EXPECT (or is non-empty when
# EXPECT is unset); fails loudly with the last observed value.
wait_for() {
  local what="$1" timeout="$2"; shift 2
  local deadline=$((SECONDS + timeout)) val=""
  while :; do
    val="$("$@" 2>/dev/null || true)"
    if [ -n "${EXPECT-}" ]; then [ "${val}" = "${EXPECT}" ] && return 0
    else [ -n "${val}" ] && return 0; fi
    if (( SECONDS >= deadline )); then
      echo "SEED FAIL: ${what}: timed out after ${timeout}s; expected '${EXPECT-<non-empty>}', last observed '${val}'" >&2
      return 1
    fi
    sleep "${POLL_SECONDS}"
  done
}

# ---------------------------------------------------------------------------
# 2. metrics-server (same version and kubelet flag as the original stack)
# ---------------------------------------------------------------------------
# GKE ships a managed metrics-server (kube-system, addon-managed; patches to
# it are reverted). Only install one where none exists, i.e. on kind.
if kubectl -n kube-system get deploy metrics-server >/dev/null 2>&1; then
  echo "==> metrics-server already present (managed by the platform); not installing"
else
  echo "==> Installing metrics-server"
  kubectl apply -f "${METRICS_SERVER_MANIFEST}" >/dev/null
  kubectl -n kube-system patch deploy metrics-server --type=json \
    -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' >/dev/null
  kubectl -n kube-system rollout status deploy/metrics-server --timeout="${WAIT_TIMEOUT}s"
fi

# ---------------------------------------------------------------------------
# 3. Argo CD core
# ---------------------------------------------------------------------------
echo "==> Installing Argo CD core ${ARGOCD_VERSION}"
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f - >/dev/null
# Server-side: the Application CRD exceeds the 256KiB last-applied annotation
# that client-side apply writes ("metadata.annotations: Too long").
kubectl apply --server-side --force-conflicts -n argocd \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/core-install.yaml" >/dev/null
kubectl -n argocd rollout status deploy/argocd-repo-server --timeout="${WAIT_TIMEOUT}s"
kubectl -n argocd rollout status deploy/argocd-redis --timeout="${WAIT_TIMEOUT}s"
kubectl -n argocd rollout status statefulset/argocd-application-controller --timeout="${WAIT_TIMEOUT}s"

# ---------------------------------------------------------------------------
# 4. repository credential + Application
# ---------------------------------------------------------------------------
token_path="${GITOPS_TOKEN_FILE/#\~/$HOME}"
[ -r "${token_path}" ] || { echo "SEED FAIL: token file ${token_path} missing" >&2; exit 1; }
echo "==> Registering repository ${GITOPS_REPO}"
kubectl apply -n argocd -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: gitops-repo
  labels:
    argocd.argoproj.io/secret-type: repository
type: Opaque
stringData:
  type: git
  url: ${GITOPS_REPO}
  username: x-access-token
  password: $(tr -d '\r\n' < "${token_path}")
EOF

# The core install has no API server, and the API server is what creates the
# "default" AppProject on startup. Create it here. Cluster-scoped resources
# (the task's Namespaces) need the whitelist.
kubectl apply -n argocd -f - >/dev/null <<EOF
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: default
  namespace: argocd
spec:
  sourceRepos:
    - '*'
  destinations:
    - server: '*'
      namespace: '*'
  clusterResourceWhitelist:
    - group: '*'
      kind: '*'
EOF

echo "==> Creating Application ${APP_NAME}: ${GITOPS_TASK_PATH} @ ${GITOPS_RUN_BRANCH}"
kubectl apply -n argocd -f - >/dev/null <<EOF
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: ${APP_NAME}
  namespace: argocd
spec:
  project: default
  source:
    repoURL: ${GITOPS_REPO}
    targetRevision: ${GITOPS_RUN_BRANCH}
    path: ${GITOPS_TASK_PATH}
  destination:
    server: https://kubernetes.default.svc
    namespace: default
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    retry:
      limit: 5
      backoff:
        duration: 5s
        factor: 2
        maxDuration: 1m
EOF

# ---------------------------------------------------------------------------
# 5. wait for sync, then assert the seeded condition
# ---------------------------------------------------------------------------
echo "==> Waiting for Application ${APP_NAME} to be Synced"
EXPECT=Synced wait_for "application sync status" $((WAIT_TIMEOUT * 2)) \
  kubectl -n argocd get application "${APP_NAME}" -o jsonpath='{.status.sync.status}'
synced_rev="$(kubectl -n argocd get application "${APP_NAME}" -o jsonpath='{.status.sync.revision}')"
echo "    synced revision ${synced_rev}"

echo "==> Asserting the seeded condition"
EXPECT=256Mi wait_for "checkout memory request" "${WAIT_TIMEOUT}" \
  kubectl -n payments get deploy checkout -o jsonpath='{.spec.template.spec.containers[?(@.name=="web")].resources.requests.memory}'
EXPECT=hashicorp/http-echo:1.0.0 wait_for "checkout image" "${WAIT_TIMEOUT}" \
  kubectl -n payments get deploy checkout -o jsonpath='{.spec.template.spec.containers[?(@.name=="web")].image}'
EXPECT=4 wait_for "checkout spec.replicas" "${WAIT_TIMEOUT}" \
  kubectl -n payments get deploy checkout -o jsonpath='{.spec.replicas}'
EXPECT=2 wait_for "pricer readyReplicas" "${WAIT_TIMEOUT}" \
  kubectl -n payments get deploy pricer -o jsonpath='{.status.readyReplicas}'
EXPECT=2 wait_for "checkout readyReplicas (quota-bound)" "${WAIT_TIMEOUT}" \
  kubectl -n payments get deploy checkout -o jsonpath='{.status.readyReplicas}'
wait_for "quota-denied checkout pod event" "${WAIT_TIMEOUT}" \
  bash -c "kubectl -n payments get events --field-selector reason=FailedCreate -o name | head -1"
wait_for "metrics API returns pod data" "${WAIT_TIMEOUT}" \
  bash -c "kubectl top pods -n payments --no-headers 2>/dev/null | head -1"

# ---------------------------------------------------------------------------
# 6. onboard the cluster with the platform agent (optional)
# ---------------------------------------------------------------------------
# The platform agent hands single-cluster work to a Cluster Agent profile that
# must exist before the card is dispatched: a worker spawned against a missing
# profile fails its preflight (no USER.md) and leaves a 0700 profile directory
# that the credential proxy can never write a kubeconfig into, so the hourly
# reconcile cannot repair it either. Scaffold it here, the way the reconcile
# does: from the shared workspace, under the entrypoint's umask.
if [ -n "${AGENT_HOST_CONTEXT:-}" ]; then
  : "${AGENT_NAMESPACE:=kubeagents-system}" "${PROJECT_ID:?}" "${LOCATION:?}"
  echo "==> Onboarding ${CLUSTER_NAME} with the platform agent on ${AGENT_HOST_CONTEXT}"
  # cluster_agent_profile.py create prints exactly the profile name on stdout
  # and exits non-zero on failure; capture stdout alone so a failure stops the
  # seed here with the scaffold's own stderr, instead of splicing an error
  # message into the shell command below.
  profile="$(kubectl --context "${AGENT_HOST_CONTEXT}" -n "${AGENT_NAMESPACE}" exec deploy/platform-agent-gateway -c platform-agent -- \
    sh -c "cd /opt/data && umask 0002 && exec python3 /opt/data/scripts/cluster_agent_profile.py create --project '${PROJECT_ID}' --cluster '${CLUSTER_NAME}' --location '${LOCATION}'")" \
    || { echo "SEED FAIL: cluster_agent_profile.py create failed for ${CLUSTER_NAME}" >&2; exit 1; }
  profile="$(printf '%s' "${profile}" | tail -n 1)"
  [[ "${profile}" =~ ${PROFILE_NAME_PATTERN} ]] || { echo "SEED FAIL: unexpected profile name '${profile}'" >&2; exit 1; }
  echo "    profile: ${profile}"
  # Mode 2770, like every profile the install created itself: group access is
  # what lets the credential-proxy sidecar (uid 10001, group hermes) read the
  # pinned kubeconfig. A 2775 home gets tightened to 0700 by Hermes on the
  # worker's first start, after which every kubectl through the proxy fails
  # with "kubeconfig is unreadable" and the card blocks (run 2, 2026-09-10).
  # Then prove the path the worker will take: kubectl through the proxy with
  # the pinned kubeconfig, and the preflight the worker runs first.
  # Hermes tightens the profile home to 0700 on the worker's first start
  # (measured: 2770 after scaffold, 0700 one second after spawn), which locks
  # the proxy out of anything inside it. So the pinned kubeconfig lives
  # outside the home, in the shared group-readable .kubeconfigs/ directory the
  # platform MCP server already uses, and the profile's .env points there.
  kubeconfig_rel=".kubeconfigs/kubeconfig_${PROJECT_ID}_${CLUSTER_NAME}_${LOCATION}.yaml"
  kubectl --context "${AGENT_HOST_CONTEXT}" -n "${AGENT_NAMESPACE}" exec deploy/platform-agent-gateway -c platform-agent -- \
    sh -c "set -e; cd /opt/data; umask 0002; d=profiles/${profile}; k=${kubeconfig_rel}; \
      test -s \$d/USER.md && test -s \$d/kubeconfig.yaml; chmod ${PROFILE_HOME_MODE} \$d; \
      mkdir -p .kubeconfigs && cp \$d/kubeconfig.yaml \$k && chmod ${KUBECONFIG_FILE_MODE} \$k; \
      sed -i \"s#^KUBECONFIG=.*#KUBECONFIG=/opt/data/\$k#\" \$d/.env; grep -q \"^KUBECONFIG=/opt/data/\$k\" \$d/.env; \
      KUBECONFIG=/opt/data/\$k kubectl get --raw=/readyz >/dev/null; \
      KUBECONFIG=/opt/data/\$k HERMES_HOME=/opt/data/\$d bash /opt/data/scripts/cluster_preflight.sh --json | grep -q '\"status\": \"ok\"'" \
    || { echo "SEED FAIL: Cluster Agent profile ${profile} is not usable (missing files, or kubectl/preflight through the proxy failed)" >&2; exit 1; }
  echo "    profile usable: kubeconfig pinned at /opt/data/${kubeconfig_rel}; kubectl via proxy and preflight ok"
fi

echo "==> Seed complete."
echo "    Application : argocd/${APP_NAME} -> ${GITOPS_REPO} ${GITOPS_TASK_PATH} @ ${GITOPS_RUN_BRANCH} (${synced_rev})"
echo "    payments    : checkout 2/4 ready (quota-bound at 256Mi), pricer 2/2"
