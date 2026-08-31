#!/usr/bin/env bash
# ==============================================================================
# Prow CI Teardown Pipeline Script
# ==============================================================================
# Cleans up PR-scoped Kubernetes resources from target GKE cluster.
# Preserves static cluster & GCP IAM setup for fast re-use across PR runs.
#
# One `helm uninstall` (the release owns every Kubernetes object ci-deploy.sh
# created) plus an explicit CRD delete, since the chart leaves CRDs behind by
# Helm's own design, plus an unconditional label sweep of every cluster-scoped
# kind the chart or the operator can create — because a run killed mid-install
# leaves cluster-scoped objects with no Helm release record for `helm
# uninstall` to act on, and Helm then refuses to adopt them on the project's
# next lease (#1006).
# ==============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# 1. Target Cluster Context
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/ci-env.sh"
ensure_helm

echo "=== Target Cluster Context ==="
echo "Project:   $PROJECT_ID"
echo "Cluster:   $CLUSTER_NAME"
echo "Location:  $REGION"
echo "Namespace: $NAMESPACE"

# Authenticates kubectl to target GKE cluster
gke_dns_endpoint_flag "$CLUSTER_NAME" "$REGION" "$PROJECT_ID"
# Unquoted on purpose: empty must contribute no argument. See gke_dns_endpoint.sh.
# shellcheck disable=SC2086
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet \
  $GKE_DNS_ENDPOINT_FLAG || {
  echo "ERROR: Failed to authenticate to GKE cluster ${CLUSTER_NAME} in project ${PROJECT_ID}! Aborting teardown for safety."
  exit 1
}

# Safety check: Verify active kubectl context matches target cluster and project before running teardown steps
CURRENT_CTX="$(kubectl config current-context 2>/dev/null || echo "")"
EXPECTED_CTX="gke_${PROJECT_ID}_${REGION}_${CLUSTER_NAME}"
if [[ "$CURRENT_CTX" != "$EXPECTED_CTX" ]]; then
  echo "ERROR: Active kubectl context ('${CURRENT_CTX}') does not match expected context ('${EXPECTED_CTX}')! Aborting teardown for safety."
  exit 1
fi

START_TIME=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Cleaning Up GKE Resources ==="

STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Step 1: Uninstalling the kube-agents release ==="
# The chart's pre-delete hook removes the PlatformAgent CR and waits for the
# operator to clear its finalizer, so one uninstall replaces the old
# per-step teardown scripts (09 LiteLLM, 08 CR, 07 secrets, 03 operator).
helm uninstall kube-agents -n "${NAMESPACE}" --wait --timeout 10m || true
echo "✓ Release uninstall finished in $((SECONDS - STEP_START))s"

STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Step 2: Deleting CRDs ==="
# Helm leaves crds/ objects behind by design; a PR evaluation cluster should
# not accumulate them.
kubectl delete -f charts/kube-agents/crds/ --ignore-not-found || true
echo "✓ CRD deletion finished in $((SECONDS - STEP_START))s"

STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Step 3: Sweeping cluster-scoped kube-agents resources ==="
# Belt-and-braces sweep, independent of Helm release state (#1006). A run
# killed mid-install or mid-teardown can leave cluster-scoped objects behind
# with no release record for Step 1's `helm uninstall` to act on — and Helm
# then refuses to adopt them on the project's next lease ("invalid ownership
# metadata"), failing every subsequent PR that Boskos hands this project.
# Namespace deletion never catches these: they are cluster-scoped by
# construction.
#
# The kinds below are the full audit of what the chart renders
# (agent-rbac-admission-policy.yaml, operator-rbac.yaml, operator-webhooks.yaml)
# and what the operator applies at reconcile time (reconcileRBAC and the
# credential-broker TokenReview pair in platformagent_controller.go). All of
# them carry app.kubernetes.io/part-of=kube-agents — the label contract in
# docs/site/src/content/docs/reference/resource-labels.md — except the CRDs,
# which Step 2 already deletes by file because Helm's crds/ convention installs
# them unlabelled.
#
# One kind per call, each `|| true`: an API group missing from this cluster
# (ValidatingAdmissionPolicy needs 1.30+) or one flaky delete must not stop
# the sweep of the kinds that do exist, and nothing here may change the
# teardown's exit code. This block must stay reachable on every path through
# the script — steps before it end in `|| true` and there is no `set -e`; the
# only early exits are the two must-not-delete-the-wrong-cluster guards above.
SWEEP_SELECTOR="app.kubernetes.io/part-of=kube-agents"
SWEEP_KINDS=(
  validatingadmissionpolicies.admissionregistration.k8s.io
  validatingadmissionpolicybindings.admissionregistration.k8s.io
  mutatingwebhookconfigurations.admissionregistration.k8s.io
  validatingwebhookconfigurations.admissionregistration.k8s.io
  clusterroles.rbac.authorization.k8s.io
  clusterrolebindings.rbac.authorization.k8s.io
)
for SWEEP_KIND in "${SWEEP_KINDS[@]}"; do
  kubectl delete "${SWEEP_KIND}" -l "${SWEEP_SELECTOR}" --ignore-not-found || true
done
echo "✓ Cluster-scoped sweep finished in $((SECONDS - STEP_START))s"

TOTAL_DURATION=$((SECONDS - START_TIME))
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Cleanup Complete (Total Duration: ${TOTAL_DURATION}s) ==="
