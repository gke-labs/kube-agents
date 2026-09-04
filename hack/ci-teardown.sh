#!/usr/bin/env bash
# ==============================================================================
# Prow CI Teardown Pipeline Script
# ==============================================================================
# Cleans up PR-scoped Kubernetes resources from target GKE cluster.
# Preserves static cluster & GCP IAM setup for fast re-use across PR runs.
#
# One `helm uninstall` (the release owns every Kubernetes object ci-deploy.sh
# created) plus a CRD delete, since the chart leaves CRDs behind by Helm's own
# design, plus an unconditional label sweep of every cluster-scoped and
# namespaced kind the chart or the operator can create *with the part-of label*
# — because a run killed mid-install leaves objects with no Helm release record
# for `helm uninstall` to act on, and Helm then refuses to adopt them on the
# project's next lease (#1006). Step 4 names the kinds the label cannot reach.
#
# The CRD delete is the one conditional step: a release record that outlives
# Step 1 needs its CRDs, or the next lease can neither uninstall nor upgrade
# over it (#1172).
# ==============================================================================

set -uo pipefail

# The release ci-deploy.sh installs; Step 1 uninstalls it and, when that
# fails, falls back on deleting its record Secrets by the label pair Helm
# stamps on every record it writes (`owner=helm` plus `name=<release>`) —
# selecting every revision's record of this release and nothing else in the
# namespace (#1172).
readonly HELM_RELEASE_NAME="kube-agents"
readonly HELM_RELEASE_SECRET_SELECTOR="owner=helm,name=${HELM_RELEASE_NAME}"
# Bounds the uninstall's --wait; generous because the chart's pre-delete hook
# waits for the operator to clear the PlatformAgent finalizer.
readonly RELEASE_UNINSTALL_TIMEOUT="10m"
# What a healthy revision looks like in `helm history -o json` (the encoder
# emits compact `"status":"deployed"`; the pattern tolerates spacing so a
# Helm formatting change cannot silently blind the fallback's check), and the
# name prefix `kubectl delete -o name` prints per deleted Secret.
readonly HELM_DEPLOYED_STATUS_RE='"status"[[:space:]]*:[[:space:]]*"deployed"'
readonly SECRET_NAME_PREFIX_RE='^secret/'
# The chart's CRDs, and a bound on waiting for them to go. `kubectl delete`
# defaults to waiting forever, and deleting a CRD blocks on
# customresourcecleanup.apiextensions.k8s.io until every CR of that kind is
# gone — which a PlatformAgent finalizer no live operator can clear never is.
# Unbounded, that hangs teardown until Prow's job timeout and Steps 3 and 4
# never run, leaving exactly the orphans they exist to sweep. Five minutes is
# well clear of the 206s a healthy-but-slow delete has been observed to take.
readonly CHART_CRD_DIR="charts/kube-agents/crds/"
readonly CRD_DELETE_TIMEOUT="5m"

# Exit nonzero when any step failed; default off so the Prow wrapper still
# reaches its Boskos release — the honest signal is the ✗ lines and the final
# summary. `readonly VAR="${VAR:-default}"` throughout this block: env still
# overrides, and the step tests lift `readonly ` head lines along.
readonly CI_TEARDOWN_STRICT="${CI_TEARDOWN_STRICT:-0}"

# Bounds for the sweep deletes in Steps 3 and 4 (Steps 1 and 2 carry their
# own budgets above). --timeout bounds the wait-for-deletion phase; the
# request timeout bounds each API call, the phase an unreachable control
# plane hangs — where the 2026-09-01 incident spent 546s.
readonly SWEEP_DELETE_TIMEOUT="${CI_TEARDOWN_SWEEP_TIMEOUT:-60s}"
readonly KUBECTL_REQUEST_TIMEOUT="${CI_TEARDOWN_KUBECTL_REQUEST_TIMEOUT:-30s}"

# Endpoint and owner convention the Prow wrapper leases with
# (BOSKOS_OWNER="${JOB_NAME}-${BUILD_ID}", oss-test-infra
# prow/prowjobs/gke-labs/kube-agents/kube-agents-presubmits.yaml), so the
# heartbeat below arms itself with no wrapper change. If the convention ever
# changes, beats 401 — one loud daemon line, harmless to the teardown.
readonly PROW_BOSKOS_DEFAULT_HOST="http://boskos.boskos.svc.cluster.local"

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

# The Prow wrapper kills its boskosctl heartbeat BEFORE invoking this script
# (so an orphaned heartbeat cannot 401-spam a released resource), which left
# every teardown lease-blind against Boskos's ~5m reaper: on 2026-09-01 a
# 546s CRD delete outlived the window, the pool re-leased the project
# mid-teardown, and the final release got 401 OwnerNotMatch. The daemon
# below heartbeats for exactly this script's lifetime; the EXIT trap kills
# it before the wrapper's release, so the orphan problem cannot come back.
# Outside Prow (no JOB_NAME/BUILD_ID) nothing is derived and the daemon
# disables itself with one line; explicit BOSKOS_* env overrides everything.
if [ -n "${JOB_NAME:-}" ] && [ -n "${BUILD_ID:-}" ]; then
  export BOSKOS_HOST="${BOSKOS_HOST:-${PROW_BOSKOS_DEFAULT_HOST}}"
  export BOSKOS_RESOURCE_NAME="${BOSKOS_RESOURCE_NAME:-${PROJECT_ID}}"
  export BOSKOS_OWNER_NAME="${BOSKOS_OWNER_NAME:-${JOB_NAME}-${BUILD_ID}}"
fi
"${SCRIPT_DIR}/boskos_heartbeat.sh" &
HEARTBEAT_PID=$!
trap 'kill "${HEARTBEAT_PID}" 2>/dev/null || true' EXIT

START_TIME=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Cleaning Up GKE Resources ==="

# Truthful step accounting: every step still runs on every path (no step may
# stop the sweep of the ones after it), but a failed step now prints ✗ and is
# counted instead of being masked by an unconditional ✓. Defined below
# START_TIME on purpose: the step tests lift the file from that line, and the
# lifted steps call this function.
FAILED_STEPS=0
FAILED_STEP_NAMES=""
finish_step() { # <name> <exit-status>
  if [ "$2" -eq 0 ]; then
    echo "✓ $1 finished in $((SECONDS - STEP_START))s"
  else
    echo "✗ $1 FAILED (status $2) after $((SECONDS - STEP_START))s; continuing"
    FAILED_STEPS=$((FAILED_STEPS + 1))
    FAILED_STEP_NAMES="${FAILED_STEP_NAMES}${FAILED_STEP_NAMES:+, }$1"
  fi
}

STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Step 1: Uninstalling the kube-agents release ==="
# The chart's pre-delete hook removes the PlatformAgent CR and waits for the
# operator to clear its finalizer, so one uninstall replaces the old
# per-step teardown scripts (09 LiteLLM, 08 CR, 07 secrets, 03 operator).
#
# When the uninstall fails and no revision is deployed, fall back to
# deleting the release-record Secrets directly (#1172). Teardown never
# deletes the namespace, so a no-deployed-revision record that survives here
# greets the project's next lease as `UPGRADE FAILED: "kube-agents" has no
# deployed releases` at ci-deploy.sh's `helm upgrade --install` — and the
# poisoned run's own teardown cannot remove it either, so the state is
# self-sustaining until something drops the record. The sweeps in Steps 3 and
# 4 already handle the objects the release owned, so this strands nothing a
# failed uninstall was not going to strand anyway.
#
# The deployed-revision gate is what makes the fallback safe on a *healthy*
# release whose uninstall failed transiently (pre-delete hook stuck, --wait
# past the timeout): its record is exactly what lets the next lease take the
# clean upgrade path over the surviving objects, so it stays. With no
# deployed revision the record is pure poison, and a probe that itself
# fails loses nothing — the delete is --ignore-not-found against a record
# that, if it exists at all, is already unusable. Same discipline as the
# rest of the file: nothing here may change the teardown's exit code, and
# the fallback logs what it deleted so a red run's artifacts show the heal
# happened.
#
# --debug because Helm hands deleteRelease's real errors to cfg.Log
# (pkg/action/uninstall.go), which drops them without it, and returns only
# `failed to delete release: <name>`. That line was all CI recorded for the
# 25 hours #1172 went unnoticed.
# --ignore-not-found: the wrapper also runs this script at job start to sweep
# a hard-killed predecessor, where no release exists and that is not a failure.
UNINSTALL_STATUS=0
if ! helm uninstall "${HELM_RELEASE_NAME}" -n "${NAMESPACE}" --wait --timeout "${RELEASE_UNINSTALL_TIMEOUT}" --debug --ignore-not-found; then
  UNINSTALL_STATUS=1
  if RELEASE_HISTORY_JSON="$(helm history "${HELM_RELEASE_NAME}" -n "${NAMESPACE}" -o json 2>/dev/null)" \
    && grep -Eq "${HELM_DEPLOYED_STATUS_RE}" <<<"${RELEASE_HISTORY_JSON}"; then
    echo "WARNING: helm uninstall failed with a deployed revision still recorded; leaving the release record for the next lease to upgrade over (#1172)"
  else
    echo "WARNING: helm uninstall failed with no deployed revision; removing any release record left behind so the next lease starts clean (#1172)"
    RECORD_SECRETS_DELETED="$(kubectl delete secret -n "${NAMESPACE}" \
      -l "${HELM_RELEASE_SECRET_SELECTOR}" --ignore-not-found -o name)" || true
    RECORD_SECRETS_COUNT="$(printf '%s\n' "${RECORD_SECRETS_DELETED}" | grep -c "${SECRET_NAME_PREFIX_RE}")" || true
    echo "${RECORD_SECRETS_DELETED}"
    echo "✓ Release-record fallback deleted ${RECORD_SECRETS_COUNT} Helm record Secret(s)"
  fi
fi
finish_step "Release uninstall" "${UNINSTALL_STATUS}"

STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Step 2: Deleting CRDs ==="
# Helm leaves crds/ objects behind by design; a PR evaluation cluster should
# not accumulate them. Only when no release record survived Step 1, though.
# Both of Helm's routes over a surviving record need the CRDs: `uninstall`
# resolves a REST mapping for every object in the stored manifest, which
# contains a PlatformAgent, and `upgrade` never reinstalls crds/. Delete them
# under a record and the next lease can do neither — #1172, where every re-run
# deleted the CRDs again and re-armed the trap. No record means the next lease
# is a fresh install, which reinstalls crds/ itself, so #1006's case (CRDs from
# a run killed mid-install) is still swept. Unreadable counts as "may survive":
# keeping a CRD costs one lease's tidiness, deleting one costs the project.
CRD_STEP_RESULT="deleted"
# 0 for the outcomes the design intends (deleted, or kept on purpose under a
# surviving record); 1 for the ones it does not (a timed-out delete, an
# unreadable record probe), so finish_step counts them into the summary.
CRD_STEP_STATUS=0
PROBE_ERROR_LOG="$(mktemp)"
if RELEASE_RECORD_NAMES="$(kubectl get secret -n "${NAMESPACE}" \
  -l "${HELM_RELEASE_SECRET_SELECTOR}" -o name 2>"${PROBE_ERROR_LOG}")"; then
  if [ -n "${RELEASE_RECORD_NAMES}" ]; then
    echo "WARNING: a ${HELM_RELEASE_NAME} release record survived Step 1; keeping the CRDs so the next lease's Helm can read its own manifest (#1172)"
    CRD_STEP_RESULT="skipped, release record survived Step 1"
  elif kubectl delete -f "${CHART_CRD_DIR}" --ignore-not-found --timeout "${CRD_DELETE_TIMEOUT}" \
    --request-timeout="${KUBECTL_REQUEST_TIMEOUT}"; then
    CRD_STEP_RESULT="deleted"
  else
    # Bounded rather than hung: the CRDs may be left Terminating, which the
    # next lease's fresh install can still collide with, but Steps 3 and 4
    # now run and the log says which happened.
    echo "WARNING: the CRD delete did not finish within ${CRD_DELETE_TIMEOUT}; continuing so the sweeps still run (#1172)"
    CRD_STEP_RESULT="delete timed out or failed after ${CRD_DELETE_TIMEOUT}"
    CRD_STEP_STATUS=1
  fi
else
  # The reason matters and used to go to /dev/null: a missing namespace is a
  # knowably-empty read, an RBAC or API-server failure is not, and the branch
  # below treats them alike.
  echo "WARNING: could not read the ${HELM_RELEASE_NAME} release records; keeping the CRDs (#1172). kubectl said: $(tr '\n' ' ' <"${PROBE_ERROR_LOG}")"
  CRD_STEP_RESULT="skipped, release records unreadable"
  CRD_STEP_STATUS=1
fi
rm -f "${PROBE_ERROR_LOG}"
finish_step "CRD step (${CRD_STEP_RESULT})" "${CRD_STEP_STATUS}"

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
# One kind per call, failures counted but never fatal: an API group missing
# from this cluster (ValidatingAdmissionPolicy needs 1.30+) or one flaky
# delete must not stop the sweep of the kinds that do exist. This block must
# stay reachable on every path through the script — steps before it end in
# `|| true`, run as `if` conditions, or feed finish_step, and there is no
# `set -e`; the only early exits are the two must-not-delete-the-wrong-cluster
# guards above. A failed kind is real, though: on 2026-09-01 the VAP delete
# was skipped because API discovery failed against a down control plane, and
# the old unconditional ✓ hid it.
SWEEP_SELECTOR="app.kubernetes.io/part-of=kube-agents"
SWEEP_KINDS=(
  validatingadmissionpolicies.admissionregistration.k8s.io
  validatingadmissionpolicybindings.admissionregistration.k8s.io
  mutatingwebhookconfigurations.admissionregistration.k8s.io
  validatingwebhookconfigurations.admissionregistration.k8s.io
  clusterroles.rbac.authorization.k8s.io
  clusterrolebindings.rbac.authorization.k8s.io
)
# A kind whose API group this cluster does not serve ("doesn't have a
# resource type") is counted absent, not failed: VAP needs 1.30+, the
# cert-manager and managed-collection groups exist only where those are
# installed, and a permanent ✗ on healthy clusters would teach readers to
# ignore the one that matters. Any other error is a real failure.
sweep_kinds() { # <label> <namespace-or-""> <kind...>
  local label="$1" ns="$2" failed=0 absent=0 total=0 out
  shift 2
  for kind in "$@"; do
    total=$((total + 1))
    # shellcheck disable=SC2086
    if out="$(kubectl delete "${kind}" ${ns:+-n "${ns}"} -l "${SWEEP_SELECTOR}" \
      --ignore-not-found --timeout="${SWEEP_DELETE_TIMEOUT}" \
      --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" 2>&1)"; then
      echo "${out}"
    else
      echo "${out}"
      if grep -q "doesn't have a resource type" <<<"${out}"; then
        absent=$((absent + 1))
      else
        failed=$((failed + 1))
      fi
    fi
  done
  finish_step "${label} (${failed}/${total} kinds failed, ${absent} absent)" "${failed}"
}

sweep_kinds "Cluster-scoped sweep" "" "${SWEEP_KINDS[@]}"

STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Step 4: Sweeping namespaced kube-agents resources ==="
# The same sweep, one scope down, for the same reason: once the release stops
# accounting for a namespaced object, nothing else deletes it. Teardown never
# deletes the kubeagents-system namespace, and the operator's children are
# reaped by owner-reference GC, which stops resolving owners as soon as Step 2
# removes the PlatformAgent CRD.
#
# Same selector as Step 3: every object the operator creates is stamped with
# app.kubernetes.io/part-of=kube-agents — through applyManaged, or, on the two
# paths that bypass it, through withCommonLabels on the PVC and commonLabels
# handed to ReconcileServiceAccount. Helm's release records are Secrets
# labelled owner=helm carrying none of the chart's labels, so this sweep cannot
# delete one — whether a record survives stays Step 1's decision. The chart's
# own CRs are absent for the same reason: on the delete path Step 2 takes them
# with their CRDs, and on the keep path they are what the next lease upgrades
# over.
#
# The claims the operator creates itself are labelled and swept; the ones it
# materialises from a StatefulSet's volumeClaimTemplates are not, because that
# ObjectMeta carries a name and nothing else. That is hindsight's claim and any
# RWO custom storage, and deleting the StatefulSet leaves them behind — no
# selector reaches either. PVCs sweep last so the workloads releasing them are
# already deleted, or kubernetes.io/pvc-protection holds the delete open.
SWEEP_NAMESPACED_KINDS=(
  deployments.apps
  statefulsets.apps
  jobs.batch
  services
  configmaps
  secrets
  serviceaccounts
  roles.rbac.authorization.k8s.io
  rolebindings.rbac.authorization.k8s.io
  networkpolicies.networking.k8s.io
  poddisruptionbudgets.policy
  podmonitorings.monitoring.googleapis.com
  certificates.cert-manager.io
  issuers.cert-manager.io
  persistentvolumeclaims
)
sweep_kinds "Namespaced sweep" "${NAMESPACE}" "${SWEEP_NAMESPACED_KINDS[@]}"

TOTAL_DURATION=$((SECONDS - START_TIME))
if [ "${FAILED_STEPS}" -eq 0 ]; then
  echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Cleanup Complete (Total Duration: ${TOTAL_DURATION}s) ==="
else
  echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Cleanup FINISHED WITH ${FAILED_STEPS} FAILED STEP(S): ${FAILED_STEP_NAMES} (Total Duration: ${TOTAL_DURATION}s) ==="
  if [ "${CI_TEARDOWN_STRICT}" = "1" ]; then
    exit 1
  fi
fi
