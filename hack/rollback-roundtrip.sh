#!/usr/bin/env bash
# ==============================================================================
# Rollback round trip: spec.mode next -> today -> next on a live install
# ==============================================================================
# The executable form of what docs/designs/spec-mode-switch.md says a mode
# change keeps: flipping a `next` install back to `today` tears the A2A stack
# down and keeps two objects, so flipping forward again reuses them instead of
# starting over. Run it against an install that is healthy under `next`:
#
#   hack/rollback-roundtrip.sh [namespace] [platformagent-name]
#
# Defaults: kubeagents-system and platform-agent. ROLLBACK_KUBE_CONTEXT pins
# every kubectl call to one context. hack/ci-eval-pr.sh runs it after the
# next lane's matrix and reports the result beside the eval verdict, not in it.
#
# The two kept objects, as the operator renders them for a CR named <cr>
# (k8s-operator/internal/controller/platformagent_a2a_manifests.go, cleanupA2A
# and the a2aPreBusTeardown and a2aBusTeardown lists it walks, neither of
# which names either object):
#   - the JetStream PVC data-<cr>-a2a-nats-0. The NATS StatefulSet's
#     volumeClaimTemplate (a2aNATSDataClaim, "data") stamps it out, so it has
#     no owner reference, and the StatefulSet sets no PVC retention policy,
#     whose default keeps the claim when the StatefulSet goes. The flip
#     forward renders the same template under the same StatefulSet name, and
#     the StatefulSet controller binds the claim of that name that is already
#     there.
#   - the bus creds Secret <cr>-a2a-nats-creds (A2ACredsSecretName in the API
#     package). ensureA2ACredsSecret creates it once, and on the flip forward
#     reads the one that exists and only fills a missing or empty key.
#
# What the run needs from the install: the inject door
# (A2A_INJECT_BACKEND=true on the operator, which is how the next lane deploys
# it), because a task over the bus is submitted through it, as the lane's
# matrix submits its cases; and python3, which runs that submission through
# the lane's own client (bench/kube_agents_bench/inject_transport.py, stdlib
# only, so no virtualenv). The CR's spec.deployment.sidecars, whatever they
# are, is unset before the flip to today, as a2a/docs/hermes-bridge.md
# requires, and declared again, byte for byte, once the bus is back; a run
# that fails or is stopped with the list unset and the CR at next declares it
# again on its way out.
#
# Every assertion prints one PASS or FAIL line with its name. The first FAIL
# stops the run, which exits non-zero after a line naming it. Every wait has a
# bound, named below, that ROLLBACK_<NAME> in the environment overrides.
# ==============================================================================

set -euo pipefail

# ─── Defaults: which install ─────────────────────────────────────────────────
readonly DEFAULT_NAMESPACE="kubeagents-system"
readonly DEFAULT_CR_NAME="platform-agent"

# ─── Names the operator renders off the CR's name ────────────────────────────
# Suffixes from k8s-operator/api/v1alpha1/common_types.go (a2aNATSNameSuffix,
# a2aCredsSecretSuffix, a2aCalloutNameSuffix, a2aCalloutKeysSuffix) and
# platformagent_a2a_manifests.go (a2aGatewayName, a2aInjectName,
# a2aVerifierName, a2aNATSDataClaim). The agent Deployment is "<cr>-gateway"
# and the agent API Service is the CR's own name (platformagent_manifests.go).
# tests/test_rollback_roundtrip.py holds these to the operator source.
readonly NATS_NAME_SUFFIX="-a2a-nats"
readonly CREDS_SECRET_SUFFIX="-creds"
readonly NATS_DATA_CLAIM="data"
readonly NATS_ORDINAL_SUFFIX="-0"
readonly CALLOUT_NAME_SUFFIX="-a2a-callout"
readonly CALLOUT_KEYS_SUFFIX="-keys"
readonly A2A_GATEWAY_SUFFIX="-a2a-gateway"
readonly A2A_VERIFIER_SUFFIX="-a2a-verifier"
readonly INJECT_NAME_SUFFIX="-a2a-inject"
readonly AGENT_DEPLOYMENT_SUFFIX="-gateway"

# The A2A objects' labels: part-of on every one (a2aLabels), and the
# provisioning Job's component (a2aComponentLabel, a2aProvisionComponent).
readonly A2A_PART_OF_LABEL="app.kubernetes.io/part-of"
readonly A2A_PART_OF_VALUE="a2a-next"
readonly A2A_PROVISION_JOB_SELECTOR="kubeagents.x-k8s.io/a2a-component=provision"

# The CR's status words (updateStatusReady and updateStatusDegraded in
# platformagent_controller.go), and the two reasons that are refusals rather
# than a rollout still settling, so the Ready wait stops on them at once.
# Every other Degraded is read as not-yet until the wait's bound: that is
# what absorbs a pod waiting a minute for an Autopilot node (#2414) without
# depending on the deploy's gate for it.
readonly MODE_NEXT="next"
readonly MODE_TODAY="today"
readonly CR_PHASE_READY="Ready"
readonly CR_PHASE_DEGRADED="Degraded"
readonly CR_CONDITION_TRUE="True"
readonly CR_REFUSAL_REASONS="A2AProvisionFailed ModeNotRecognized"
readonly JOB_CONDITION_COMPLETE="Complete"
readonly JOB_CONDITION_FAILED="Failed"

# A container waiting for one of these is not coming up on its own, and a pod
# holding one is stuck rather than starting.
readonly STUCK_WAITING_REASONS="CrashLoopBackOff ImagePullBackOff ErrImagePull CreateContainerConfigError CreateContainerError InvalidImageName RunContainerError"

# ─── The today path's turn ───────────────────────────────────────────────────
# The same request and the same acceptance test as the deploy's connectivity
# check (hack/ci-deploy.sh, step 7): one POST to the agent API through a fresh
# port-forward per attempt, the key from the install's credentials Secret.
readonly AGENT_API_PORT=8642
readonly AGENT_API_PATH="/v1/responses"
readonly AGENT_API_KEY_SECRET_DEFAULT="platform-agent-secrets"
readonly AGENT_API_KEY_FIELD="API_SERVER_KEY"
readonly AGENT_API_MODEL="model-default"
readonly AGENT_API_PROMPT="ping"
readonly AGENT_API_ACCEPT_MARKERS="output assistant pong"
readonly AGENT_LOCAL_PORT_DEFAULT=28541

# ─── The bus task ────────────────────────────────────────────────────────────
# One task through the inject door (a2aInjectPort; token under a2aInjectTokenKey
# in the Secret beside it), awaited to its terminal by the lane's client.
# Completed is the pass; any other terminal, or none by the bound, fails.
readonly INJECT_PORT=8099
readonly INJECT_TOKEN_FIELD="token"
readonly INJECT_LOCAL_PORT_DEFAULT=29041
readonly BUS_TASK_PROMPT="Reply with the single word OK. Do not run any tools."
readonly BUS_TASK_CONVERSATION_PREFIX="rollback-roundtrip"
# What the client's script exits when the door could not be reached before
# anything was submitted, or answered with a retryable status: worth one
# fresh tunnel, nothing else is. A refusal it answered is not.
readonly BUS_TASK_UNREACHABLE_STATUS=2

# The line the Hermes bridge logs once its durable consumer is bound
# (a2a/hermes-bridge/bridge.go, Run). A task submitted before it is one no
# executor takes within the gateway's first-event grace.
readonly BRIDGE_CONSUMING_LOG_MSG='"msg":"hermes bridge consuming"'
readonly BRIDGE_LOG_TAIL_LINES=200

# ─── Bounds, in seconds ──────────────────────────────────────────────────────
readonly DEFAULT_POLL_SECONDS=5
# The CR Ready at its current generation, before anything is changed.
readonly DEFAULT_PRE_READY_TIMEOUT=300
# The agent Deployment's generation moving after a CR patch: the operator has
# reconciled it. Then the rollout of that generation finishing.
readonly DEFAULT_GENERATION_TIMEOUT=300
readonly DEFAULT_ROLLOUT_TIMEOUT=600
# The CR reporting Ready at the patched generation, for every patch but the
# flip forward (which shares BUS_UP_TIMEOUT below).
readonly DEFAULT_READY_TIMEOUT=900
# The flip to today's teardown reaching its end (the NATS StatefulSet, which
# cleanupA2A deletes last).
readonly DEFAULT_TEARDOWN_TIMEOUT=300
# The bus back after the flip forward, one deadline for all of it: the
# StatefulSet, the callout, the provisioning Job, the agent's rollout and the
# CR's Ready (the deploy's MODE_NEXT_PROVISION_JOB_TIMEOUT_SECONDS; leg 2
# below has the arithmetic). Also the bound on the re-run after the bridge is
# declared again.
readonly DEFAULT_BUS_UP_TIMEOUT=1500
# The bridge logging that it consumes, after its pod rolled.
readonly DEFAULT_BRIDGE_TIMEOUT=300
# Pods and Jobs settling after Ready: nothing pending, terminating or backing
# off, and under today nothing of the A2A stack left.
readonly DEFAULT_SETTLE_TIMEOUT=300
# One today turn's request, and how many fresh tunnels it gets.
readonly DEFAULT_TURN_TIMEOUT=120
readonly DEFAULT_TURN_ATTEMPTS=5
# One bus task, submission to terminal; and how many fresh tunnels a door
# that could not be reached gets.
readonly DEFAULT_BUS_TASK_TIMEOUT=900
readonly DEFAULT_BUS_TASK_ATTEMPTS=2
# A port-forward listening.
readonly DEFAULT_PORT_FORWARD_WAIT=30
# The one patch that declares the sidecars again on a failed or stopped run:
# inside the 60s grace ci-eval-pr.sh gives a stopped run before its KILL.
readonly RESTORE_REQUEST_TIMEOUT="20s"
# One read retried this many times before it counts as failed.
readonly READ_ATTEMPTS=3
# How much of a port-forward's or the bus client's own output a failure
# carries.
readonly PF_LOG_TAIL_LINES=5
readonly CLIENT_ERR_TAIL_LINES=5

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly BENCH_DIR="${SCRIPT_DIR}/../bench"

NAMESPACE="${1:-${ROLLBACK_NAMESPACE:-${DEFAULT_NAMESPACE}}}"
CR_NAME="${2:-${ROLLBACK_CR_NAME:-${DEFAULT_CR_NAME}}}"
readonly NAMESPACE CR_NAME

POLL_SECONDS="${ROLLBACK_POLL_SECONDS:-${DEFAULT_POLL_SECONDS}}"
PRE_READY_TIMEOUT="${ROLLBACK_PRE_READY_TIMEOUT:-${DEFAULT_PRE_READY_TIMEOUT}}"
GENERATION_TIMEOUT="${ROLLBACK_GENERATION_TIMEOUT:-${DEFAULT_GENERATION_TIMEOUT}}"
ROLLOUT_TIMEOUT="${ROLLBACK_ROLLOUT_TIMEOUT:-${DEFAULT_ROLLOUT_TIMEOUT}}"
READY_TIMEOUT="${ROLLBACK_READY_TIMEOUT:-${DEFAULT_READY_TIMEOUT}}"
TEARDOWN_TIMEOUT="${ROLLBACK_TEARDOWN_TIMEOUT:-${DEFAULT_TEARDOWN_TIMEOUT}}"
BUS_UP_TIMEOUT="${ROLLBACK_BUS_UP_TIMEOUT:-${DEFAULT_BUS_UP_TIMEOUT}}"
BRIDGE_TIMEOUT="${ROLLBACK_BRIDGE_TIMEOUT:-${DEFAULT_BRIDGE_TIMEOUT}}"
SETTLE_TIMEOUT="${ROLLBACK_SETTLE_TIMEOUT:-${DEFAULT_SETTLE_TIMEOUT}}"
TURN_TIMEOUT="${ROLLBACK_TURN_TIMEOUT:-${DEFAULT_TURN_TIMEOUT}}"
TURN_ATTEMPTS="${ROLLBACK_TURN_ATTEMPTS:-${DEFAULT_TURN_ATTEMPTS}}"
BUS_TASK_TIMEOUT="${ROLLBACK_BUS_TASK_TIMEOUT:-${DEFAULT_BUS_TASK_TIMEOUT}}"
BUS_TASK_ATTEMPTS="${ROLLBACK_BUS_TASK_ATTEMPTS:-${DEFAULT_BUS_TASK_ATTEMPTS}}"
PORT_FORWARD_WAIT="${ROLLBACK_PORT_FORWARD_WAIT:-${DEFAULT_PORT_FORWARD_WAIT}}"
AGENT_API_KEY_SECRET="${ROLLBACK_AGENT_API_KEY_SECRET:-${AGENT_API_KEY_SECRET_DEFAULT}}"
AGENT_LOCAL_PORT="${ROLLBACK_AGENT_LOCAL_PORT:-${AGENT_LOCAL_PORT_DEFAULT}}"
INJECT_LOCAL_PORT="${ROLLBACK_INJECT_LOCAL_PORT:-${INJECT_LOCAL_PORT_DEFAULT}}"
# A base URL for either door instead of a port-forward, the way the harness's
# AGENT_INJECT_URL works: for an install already reachable from here.
AGENT_URL="${ROLLBACK_AGENT_URL:-}"
INJECT_URL="${ROLLBACK_INJECT_URL:-}"
# Optional: every PASS, FAIL and SKIP line is appended here too.
RESULTS_FILE="${ROLLBACK_RESULTS_FILE:-}"

readonly NATS_NAME="${CR_NAME}${NATS_NAME_SUFFIX}"
readonly NATS_PVC="${NATS_DATA_CLAIM}-${NATS_NAME}${NATS_ORDINAL_SUFFIX}"
readonly NATS_POD="${NATS_NAME}${NATS_ORDINAL_SUFFIX}"
readonly CREDS_SECRET="${NATS_NAME}${CREDS_SECRET_SUFFIX}"
readonly CALLOUT_NAME="${CR_NAME}${CALLOUT_NAME_SUFFIX}"
readonly CALLOUT_KEYS_SECRET="${CALLOUT_NAME}${CALLOUT_KEYS_SUFFIX}"
readonly A2A_GATEWAY_NAME="${CR_NAME}${A2A_GATEWAY_SUFFIX}"
readonly A2A_VERIFIER_NAME="${CR_NAME}${A2A_VERIFIER_SUFFIX}"
readonly INJECT_NAME="${CR_NAME}${INJECT_NAME_SUFFIX}"
readonly AGENT_DEPLOYMENT="${CR_NAME}${AGENT_DEPLOYMENT_SUFFIX}"

# State the failure report reads. SIDECARS_FILE is set while this run has the
# CR's sidecars unset, and cleared once they are declared again.
CURRENT_LEG="preflight"
BUS_SIDECARS=""
SIDECARS_FILE=""
SIDECAR_NAMES=""
# Set once the API accepts the flip to today (patch_and_reconcile), until leg
# 2's provisioning passes: the bus is down or not yet back, and the sidecars
# must not be declared again.
BUS_DOWN=""
PF_PID=""
PF_LOG=""

# ─── Output ──────────────────────────────────────────────────────────────────
record() {
  if [ -n "${RESULTS_FILE}" ]; then
    printf '%s\n' "$*" >>"${RESULTS_FILE}" || true
  fi
}

note() {
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*"
}

pass() {
  local name="$1"
  shift
  echo "PASS ${name}: $*"
  record "PASS ${name}: $*"
}

skip() {
  local name="$1"
  shift
  echo "SKIP ${name}: $*"
  record "SKIP ${name}: $*"
}

# The first failed assertion ends the run.
fail() {
  local name="$1"
  shift
  echo "FAIL ${name}: $*"
  record "FAIL ${name}: $*"
  report_state_at_failure
  echo "ROLLBACK ROUND TRIP FAILED at ${name}"
  exit 1
}

# What a person picking up a failed run needs: where the install was left,
# and what this run took off it. No sidecar contents: they carry the agent's
# environment.
#
# The sidecars go back on the way out only when the bus is up: the CR at next
# and BUS_DOWN unset, which is until the API accepts the flip to today (a
# refused flip included) or once leg 2's provisioning has passed. Then the list is what the install ran before the
# run, so it is patched back once, not awaited. Anywhere else (under today,
# or at next with the bus still coming back) the list may hold a bus
# sidecar, and declaring one there is the outage a2a/docs/hermes-bridge.md
# describes, so the saved list stays in its file and the line says where.
STATE_REPORTED=""
report_state_at_failure() {
  local mode
  STATE_REPORTED=1
  mode="$(k get platformagent "${CR_NAME}" -o jsonpath='{.spec.mode}' 2>/dev/null || true)"
  echo "install left at spec.mode=${mode:-unset} during ${CURRENT_LEG}"
  [ -n "${SIDECARS_FILE}" ] || return 0
  if [ "${mode}" = "${MODE_NEXT}" ] && [ -z "${BUS_DOWN}" ] \
    && k patch platformagent "${CR_NAME}" --type merge --request-timeout="${RESTORE_REQUEST_TIMEOUT}" -p "$(cat "${SIDECARS_FILE}")" >/dev/null 2>&1; then
    echo "this run had unset the sidecar(s) ${SIDECAR_NAMES}; declared them again on the way out (patch applied, rollout not awaited)"
    rm -f "${SIDECARS_FILE}"
    SIDECARS_FILE=""
    return 0
  fi
  echo "this run unset the sidecar(s) ${SIDECAR_NAMES}; the CR's original spec.deployment.sidecars is in ${SIDECARS_FILE} (merge-patch it back once spec.mode is next and the bus is up)"
}

# On every exit. A run that dies outside fail() and on_signal (a set -e
# death) has not been through the failure report, and may still have the
# sidecars unset: it goes through it here.
cleanup() {
  local status=$?
  stop_port_forward
  if [ -n "${SIDECARS_FILE}" ] && [ -z "${STATE_REPORTED}" ]; then
    echo "the run exited with status ${status} outside an assertion"
    report_state_at_failure
  fi
}

on_signal() {
  echo "FAIL ${CURRENT_LEG}.interrupted: the run was stopped by a signal (an outer bound, or by hand)"
  record "FAIL ${CURRENT_LEG}.interrupted: the run was stopped by a signal"
  report_state_at_failure
  echo "ROLLBACK ROUND TRIP FAILED at ${CURRENT_LEG}.interrupted"
  exit 1
}
trap cleanup EXIT
trap on_signal TERM INT

# ─── kubectl ─────────────────────────────────────────────────────────────────
k() {
  if [ -n "${ROLLBACK_KUBE_CONTEXT:-}" ]; then
    kubectl --context "${ROLLBACK_KUBE_CONTEXT}" -n "${NAMESPACE}" "$@"
  else
    kubectl -n "${NAMESPACE}" "$@"
  fi
}

# A read retried READ_ATTEMPTS times, so one dropped API call is not a FAIL.
k_read() {
  local attempt out
  for ((attempt = 1; attempt <= READ_ATTEMPTS; attempt++)); do
    if out="$(k "$@" 2>/dev/null)"; then
      printf '%s' "${out}"
      return 0
    fi
    [ "${attempt}" -lt "${READ_ATTEMPTS}" ] && sleep "${POLL_SECONDS}"
  done
  return 1
}

# "<uid>|<deletionTimestamp>" for an object, empty when it does not exist;
# non-zero only when the API could not be read.
identity() {
  k_read get "$1" "$2" --ignore-not-found -o jsonpath='{.metadata.uid}|{.metadata.deletionTimestamp}'
}

# A Secret key's decoded value, empty when absent.
secret_value() {
  local encoded
  encoded="$(k_read get secret "$1" --ignore-not-found -o jsonpath="{.data.$2}")" || return 1
  [ -n "${encoded}" ] || return 0
  printf '%s' "${encoded}" | python3 -c 'import base64, sys; sys.stdout.write(base64.b64decode(sys.stdin.read()).decode())'
}

# The CR's state as one line of "|"-separated fields: mode, generation,
# phase, Ready status, Ready reason, Ready observedGeneration, Degraded
# status, Degraded reason, Ready message.
readonly PY_CR_STATE='
import json, sys
cr = json.load(sys.stdin)
status = cr.get("status") or {}
conds = {c.get("type"): c for c in status.get("conditions") or []}
ready = conds.get("Ready") or {}
degraded = conds.get("Degraded") or {}
def clean(v):
    return str(v if v is not None else "").replace("|", "/").replace("\n", " ").replace("\t", " ")
print("|".join(clean(v) for v in (
    (cr.get("spec") or {}).get("mode") or "today",
    cr.get("metadata", {}).get("generation", ""),
    status.get("phase", ""),
    ready.get("status", ""), ready.get("reason", ""), ready.get("observedGeneration", ""),
    degraded.get("status", ""), degraded.get("reason", ""),
    ready.get("message", ""),
)))
'
CR_MODE="" CR_GEN="" CR_PHASE="" CR_READY="" CR_READY_REASON="" CR_READY_GEN="" CR_DEGRADED="" CR_DEGRADED_REASON="" CR_READY_MSG=""
read_cr_state() {
  local json line
  json="$(k_read get platformagent "${CR_NAME}" -o json)" || return 1
  line="$(printf '%s' "${json}" | python3 -c "${PY_CR_STATE}")" || return 1
  IFS='|' read -r CR_MODE CR_GEN CR_PHASE CR_READY CR_READY_REASON CR_READY_GEN CR_DEGRADED CR_DEGRADED_REASON CR_READY_MSG <<<"${line}"
}

cr_state_summary() {
  echo "mode=${CR_MODE} generation=${CR_GEN} phase=${CR_PHASE:-none} Ready=${CR_READY:-none}/${CR_READY_REASON:-none} (observedGeneration ${CR_READY_GEN:-none}) Degraded=${CR_DEGRADED:-none}/${CR_DEGRADED_REASON:-none}; ${CR_READY_MSG}"
}

# Rollout state of a Deployment or StatefulSet: "ok", "absent", or what is
# missing.
readonly PY_ROLLOUT='
import json, sys
raw = sys.stdin.read().strip()
if not raw:
    print("absent"); sys.exit()
obj = json.loads(raw)
spec, st = obj.get("spec") or {}, obj.get("status") or {}
want = spec.get("replicas", 1)
gen, seen = obj.get("metadata", {}).get("generation", 0), st.get("observedGeneration", 0)
updated = st.get("updatedReplicas", 0)
ready = st.get("readyReplicas", 0)
current = st.get("replicas", 0)
if obj.get("kind") == "Deployment":
    ready = min(ready, st.get("availableReplicas", 0))
problems = []
if seen < gen: problems.append("observedGeneration %s < generation %s" % (seen, gen))
if want < 1: problems.append("scaled to %s" % want)
if updated < want: problems.append("%s/%s updated" % (updated, want))
if ready < want: problems.append("%s/%s ready" % (ready, want))
if current > want: problems.append("%s replicas still running for %s wanted" % (current, want))
print("; ".join(problems) or "ok")
'
rollout_state() {
  local json
  json="$(k_read get "$1" "$2" --ignore-not-found -o json)" || {
    echo "unreadable"
    return 0
  }
  printf '%s' "${json}" | python3 -c "${PY_ROLLOUT}"
}

agent_generation() {
  k_read get deployment "${AGENT_DEPLOYMENT}" -o jsonpath='{.metadata.generation}'
}

# ─── Waits ───────────────────────────────────────────────────────────────────
# Waits for the CR to report Ready at its current generation. A refusal
# (CR_REFUSAL_REASONS) at that generation fails at once; any other Degraded
# is waited through to the bound, and printed when it changes.
wait_cr_ready() {
  local name="$1" bound="$2" deadline last="" now
  deadline=$((SECONDS + bound))
  while :; do
    if read_cr_state; then
      if [ "${CR_READY_GEN}" = "${CR_GEN}" ] && [ "${CR_PHASE}" = "${CR_PHASE_READY}" ] && [ "${CR_READY}" = "${CR_CONDITION_TRUE}" ]; then
        pass "${name}" "Ready at generation ${CR_GEN} under mode ${CR_MODE} ($(cr_state_summary))"
        return 0
      fi
      if [ "${CR_READY_GEN}" = "${CR_GEN}" ] && [[ " ${CR_REFUSAL_REASONS} " == *" ${CR_READY_REASON} "* ]] && [ -n "${CR_READY_REASON}" ]; then
        fail "${name}" "the operator refused the render: $(cr_state_summary)"
      fi
      now="$(cr_state_summary)"
      if [ "${now}" != "${last}" ]; then
        note "${name}: waiting: ${now}"
        last="${now}"
      fi
    fi
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      fail "${name}" "not Ready at the current generation within ${bound}s; last read: ${last:-the CR could not be read}"
    fi
    sleep "${POLL_SECONDS}"
  done
}

# Waits for the agent Deployment's generation to move past the one given:
# the operator reconciled the patch.
wait_generation_past() {
  local name="$1" before="$2" bound="$3" deadline after
  deadline=$((SECONDS + bound))
  while :; do
    after="$(agent_generation)" || after="${before}"
    if [ -n "${after}" ] && [ "${after}" != "${before}" ]; then
      pass "${name}" "deployment/${AGENT_DEPLOYMENT} generation ${before} -> ${after}"
      return 0
    fi
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      fail "${name}" "deployment/${AGENT_DEPLOYMENT} stayed at generation ${before} for ${bound}s; the operator did not reconcile the patch"
    fi
    sleep "${POLL_SECONDS}"
  done
}

wait_rolled() {
  local name="$1" kind="$2" object="$3" bound="$4" deadline state
  deadline=$((SECONDS + bound))
  while :; do
    state="$(rollout_state "${kind}" "${object}")"
    if [ "${state}" = "ok" ]; then
      pass "${name}" "${kind}/${object} rolled out and ready"
      return 0
    fi
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      fail "${name}" "${kind}/${object} not rolled out within ${bound}s: ${state}"
    fi
    sleep "${POLL_SECONDS}"
  done
}

# Waits for every "kind name" pair given to be gone.
wait_absent() {
  local name="$1" bound="$2"
  shift 2
  local deadline present pair listing out
  listing="$(printf '%s, ' "$@")"
  deadline=$((SECONDS + bound))
  while :; do
    present=""
    for pair in "$@"; do
      # shellcheck disable=SC2086 # the pair is "kind name", split on purpose
      if ! out="$(k_read get ${pair} --ignore-not-found -o name)"; then
        present="${present} ${pair} (unreadable);"
      elif [ -n "${out}" ]; then
        present="${present} ${pair};"
      fi
    done
    if [ -z "${present}" ]; then
      pass "${name}" "gone: ${listing%, }"
      return 0
    fi
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      fail "${name}" "still present after ${bound}s:${present}"
    fi
    sleep "${POLL_SECONDS}"
  done
}

# Waits for the provisioning Jobs to settle: none failed, none still running,
# and one complete. A failed one fails at once.
readonly PY_PROVISION='
import json, sys
jobs = json.load(sys.stdin).get("items") or []
done, failed, running = [], [], []
for job in jobs:
    name = job["metadata"]["name"]
    true = {c.get("type") for c in (job.get("status") or {}).get("conditions") or [] if c.get("status") == "True"}
    if sys.argv[2] in true: failed.append(name)
    elif sys.argv[1] in true: done.append(name)
    else: running.append(name)
if failed: print("failed " + failed[-1])
elif running: print("running " + running[-1])
elif done: print("complete " + done[-1])
else: print("none -")
'
provision_jobs() {
  k_read get jobs -l "${A2A_PROVISION_JOB_SELECTOR}" -o json
}

wait_provision_complete() {
  local name="$1" bound="$2" deadline json state job
  deadline=$((SECONDS + bound))
  while :; do
    state="unreadable -"
    if json="$(provision_jobs)"; then
      state="$(printf '%s' "${json}" | python3 -c "${PY_PROVISION}" "${JOB_CONDITION_COMPLETE}" "${JOB_CONDITION_FAILED}")"
    fi
    job="${state#* }"
    case "${state}" in
      complete\ *)
        pass "${name}" "provisioning Job ${job} complete, none running or failed"
        return 0
        ;;
      failed\ *) fail "${name}" "provisioning Job ${job} failed" ;;
    esac
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      fail "${name}" "the provisioning Jobs did not settle within ${bound}s (last: ${state})"
    fi
    sleep "${POLL_SECONDS}"
  done
}

# Pods and Jobs that are stuck, one per line; nothing when the namespace is
# settled. Under today, anything left of the A2A stack counts too. The pod
# list and the Job list come on stdin, one after the other: a namespace's
# pod list runs past the 128 KiB Linux allows one argument.
readonly PY_STUCK='
import json, sys
text, decoder = sys.stdin.read().lstrip(), json.JSONDecoder()
pods_doc, end = decoder.raw_decode(text)
jobs_doc, _ = decoder.raw_decode(text[end:].lstrip())
pods = pods_doc.get("items") or []
jobs = jobs_doc.get("items") or []
mode, part_of, part_of_value = sys.argv[1], sys.argv[2], sys.argv[3]
stuck_reasons = set(sys.argv[4].split())
failed_word = sys.argv[5]
for pod in pods:
    meta, st = pod.get("metadata") or {}, pod.get("status") or {}
    name = meta.get("name", "?")
    if meta.get("deletionTimestamp"):
        print("pod/%s terminating since %s" % (name, meta["deletionTimestamp"])); continue
    if mode == "today" and (meta.get("labels") or {}).get(part_of) == part_of_value:
        print("pod/%s is an A2A pod still present under today (phase %s)" % (name, st.get("phase"))); continue
    if st.get("phase") == "Pending":
        why = [c.get("message") or c.get("reason") or "" for c in st.get("conditions") or [] if c.get("status") == "False"]
        print("pod/%s Pending %s" % (name, "; ".join(w for w in why if w))); continue
    for cs in (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or []):
        waiting = (cs.get("state") or {}).get("waiting") or {}
        if waiting.get("reason") in stuck_reasons:
            print("pod/%s container %s %s" % (name, cs.get("name"), waiting["reason"])); break
for job in jobs:
    meta = job.get("metadata") or {}
    name = meta.get("name", "?")
    if mode == "today":
        print("job/%s is an A2A Job still present under today" % name); continue
    true = {c.get("type") for c in (job.get("status") or {}).get("conditions") or [] if c.get("status") == "True"}
    if failed_word in true:
        print("job/%s Failed" % name)
'
wait_settled() {
  local name="$1" mode="$2" bound="$3" deadline pods jobs problems
  deadline=$((SECONDS + bound))
  while :; do
    problems="the namespace could not be read"
    if pods="$(k_read get pods -o json)" && jobs="$(provision_jobs)"; then
      problems="$(printf '%s\n%s' "${pods}" "${jobs}" | python3 -c "${PY_STUCK}" "${mode}" "${A2A_PART_OF_LABEL}" "${A2A_PART_OF_VALUE}" "${STUCK_WAITING_REASONS}" "${JOB_CONDITION_FAILED}")"
    fi
    if [ -z "${problems}" ]; then
      pass "${name}" "no pod pending, terminating or backing off, no provisioning Job failed$([ "${mode}" = "${MODE_TODAY}" ] && echo ', and no A2A pod or Job left')"
      return 0
    fi
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      fail "${name}" "still unsettled after ${bound}s: $(printf '%s' "${problems}" | tr '\n' ';')"
    fi
    sleep "${POLL_SECONDS}"
  done
}

# ─── Assertions on the kept objects ──────────────────────────────────────────
PVC_UID=""
CREDS_UID=""

# Records the UID of an object that must exist and is not being deleted.
assert_present() {
  local name="$1" kind="$2" object="$3" ident uid deleting
  ident="$(identity "${kind}" "${object}")" || fail "${name}" "${kind}/${object} could not be read"
  uid="${ident%%|*}"
  deleting="${ident#*|}"
  [ -n "${uid}" ] || fail "${name}" "${kind}/${object} does not exist"
  [ -z "${deleting}" ] || fail "${name}" "${kind}/${object} is being deleted (deletionTimestamp ${deleting})"
  pass "${name}" "${kind}/${object} uid ${uid}"
  ASSERTED_UID="${uid}"
}

assert_same_uid() {
  local name="$1" kind="$2" object="$3" want="$4" ident uid deleting
  ident="$(identity "${kind}" "${object}")" || fail "${name}" "${kind}/${object} could not be read"
  uid="${ident%%|*}"
  deleting="${ident#*|}"
  [ -n "${uid}" ] || fail "${name}" "${kind}/${object} is gone (it had uid ${want})"
  [ -z "${deleting}" ] || fail "${name}" "${kind}/${object} is being deleted (deletionTimestamp ${deleting})"
  [ "${uid}" = "${want}" ] || fail "${name}" "${kind}/${object} was replaced: uid ${want} before the flip, ${uid} now"
  pass "${name}" "${kind}/${object} kept, uid ${uid}"
}

# The Degraded condition the install carried before anything was changed:
# its reason when Degraded=True, empty otherwise. The operator sets
# Degraded=True beside phase Ready for causes that are not the workload's
# (MinterPruningHeld; RBACIncomplete it preserves), so a healthy next install
# can start with one, and the flip is not what put it there.
PRE_DEGRADED_REASON=""
record_degraded_baseline() {
  local name="$1"
  read_cr_state || fail "${name}" "the CR could not be read"
  if [ "${CR_DEGRADED}" = "${CR_CONDITION_TRUE}" ]; then
    PRE_DEGRADED_REASON="${CR_DEGRADED_REASON:-unspecified}"
    pass "${name}" "Degraded=True/${PRE_DEGRADED_REASON} before the flip; the not-degraded checks fail only on a Degraded that is new or changed from this"
  else
    pass "${name}" "no Degraded condition set before the flip"
  fi
}

degraded_baseline_summary() {
  if [ -n "${PRE_DEGRADED_REASON}" ]; then
    echo "baseline Degraded=True/${PRE_DEGRADED_REASON}"
  else
    echo "baseline: none"
  fi
}

# Fails on a Degraded phase, or on a Degraded condition the baseline did not
# carry with the same reason.
assert_not_degraded() {
  local name="$1"
  read_cr_state || fail "${name}" "the CR could not be read"
  if [ "${CR_PHASE}" = "${CR_PHASE_DEGRADED}" ]; then
    fail "${name}" "$(cr_state_summary) ($(degraded_baseline_summary))"
  fi
  if [ "${CR_DEGRADED}" = "${CR_CONDITION_TRUE}" ]; then
    if [ "${CR_DEGRADED_REASON:-unspecified}" != "${PRE_DEGRADED_REASON}" ]; then
      fail "${name}" "a Degraded condition new since the flip: $(cr_state_summary) ($(degraded_baseline_summary))"
    fi
    pass "${name}" "phase ${CR_PHASE}; Degraded=True/${CR_DEGRADED_REASON:-unspecified} is the one the install carried before the flip, not new"
    return 0
  fi
  pass "${name}" "phase ${CR_PHASE}, no Degraded condition set ($(degraded_baseline_summary))"
}

# The NATS pod mounts the kept claim, by name, now that the bus is back.
assert_nats_on_claim() {
  local name="$1" claims
  claims="$(k_read get pod "${NATS_POD}" -o jsonpath='{.spec.volumes[*].persistentVolumeClaim.claimName}')" || fail "${name}" "pod/${NATS_POD} could not be read"
  [[ " ${claims} " == *" ${NATS_PVC} "* ]] || fail "${name}" "pod/${NATS_POD} mounts claim(s) '${claims}', not ${NATS_PVC}"
  pass "${name}" "pod/${NATS_POD} mounts ${NATS_PVC}"
}

# ─── Sidecars ────────────────────────────────────────────────────────────────
# The whole of spec.deployment.sidecars is unset before the flip to today and
# declared again after the flip forward, not only the sidecars that look like
# they talk to the bus: a sidecar is an ordinary corev1.Container
# (a2a/docs/hermes-bridge.md), and one the flip breaks can say so in more ways
# than a reading of its data finds (a host in args, a reference to a Secret
# cleanupA2A deletes). A miss there kept a broken sidecar across the flip and
# took the agent down; unsetting them all costs one rollout for a sidecar that
# would have survived.
#
# PY_SIDECARS prints the sidecars' names on the first line, and on the second
# the ones that look like bus clients, for wait_bridges_consuming alone: one
# that references the creds Secret (env[].valueFrom.secretKeyRef,
# envFrom[].secretRef) or names the NATS Service anywhere in an env value. A
# miss there costs that sidecar's log wait, nothing else; a sidecar picked
# that never logs the Hermes bridge's line fails leg2.bridge-consuming at its
# bound, with its name.
readonly PY_SIDECARS='
import json, sys
cr = json.load(sys.stdin)
creds, nats = sys.argv[1:3]
sidecars = ((cr.get("spec") or {}).get("deployment") or {}).get("sidecars") or []
def on_bus(c):
    for e in c.get("env") or []:
        if ((e.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name") == creds or nats in (e.get("value") or ""):
            return True
    return any((f.get("secretRef") or {}).get("name") == creds for f in c.get("envFrom") or [])
print(" ".join(c.get("name", "?") for c in sidecars))
print(" ".join(c.get("name", "?") for c in sidecars if on_bus(c)))
'
# The CR's spec.deployment.sidecars as a merge patch that declares it again.
readonly PY_SIDECARS_PATCH='
import json, sys
cr = json.load(sys.stdin)
print(json.dumps({"spec": {"deployment": {"sidecars": ((cr.get("spec") or {}).get("deployment") or {}).get("sidecars")}}}))
'

# ─── Port-forwards ───────────────────────────────────────────────────────────
# kubectl itself is the background job, not a function or a subshell around
# it: $! must be the process that holds the port, or stop_port_forward kills
# a wrapper and leaves the tunnel bound, and every later attempt's probe is
# answered by the first, possibly dead, tunnel.
start_port_forward() {
  local service="$1" local_port="$2" remote_port="$3" waited=0
  local -a pf=(kubectl)
  stop_port_forward
  [ -n "${PF_LOG}" ] || PF_LOG="$(mktemp)"
  if [ -n "${ROLLBACK_KUBE_CONTEXT:-}" ]; then
    pf+=(--context "${ROLLBACK_KUBE_CONTEXT}")
  fi
  pf+=(-n "${NAMESPACE}" port-forward "svc/${service}" "${local_port}:${remote_port}")
  "${pf[@]}" >>"${PF_LOG}" 2>&1 &
  PF_PID=$!
  while [ "${waited}" -lt "${PORT_FORWARD_WAIT}" ]; do
    if (exec 3<>"/dev/tcp/127.0.0.1/${local_port}") 2>/dev/null; then
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  note "port-forward to svc/${service} did not listen on ${local_port}; its log: $(tail -n "${PF_LOG_TAIL_LINES}" "${PF_LOG}" 2>/dev/null | tr '\n' ' ')"
  return 1
}

stop_port_forward() {
  if [ -n "${PF_PID}" ]; then
    kill "${PF_PID}" 2>/dev/null || true
    wait "${PF_PID}" 2>/dev/null || true
    PF_PID=""
  fi
}

# ─── The today turn ──────────────────────────────────────────────────────────
assert_today_turn() {
  local name="$1" key attempt url response marker detail=""
  key="$(secret_value "${AGENT_API_KEY_SECRET}" "${AGENT_API_KEY_FIELD}")" || fail "${name}" "secret/${AGENT_API_KEY_SECRET} could not be read"
  [ -n "${key}" ] || fail "${name}" "secret/${AGENT_API_KEY_SECRET} has no ${AGENT_API_KEY_FIELD}"
  for ((attempt = 1; attempt <= TURN_ATTEMPTS; attempt++)); do
    url="${AGENT_URL}"
    if [ -z "${url}" ]; then
      if ! start_port_forward "${CR_NAME}" "${AGENT_LOCAL_PORT}" "${AGENT_API_PORT}"; then
        detail="the port-forward to svc/${CR_NAME} did not listen within ${PORT_FORWARD_WAIT}s"
        continue
      fi
      url="http://127.0.0.1:${AGENT_LOCAL_PORT}"
    fi
    response="$(curl -s --max-time "${TURN_TIMEOUT}" -X POST "${url}${AGENT_API_PATH}" \
      -H "Authorization: Bearer ${key}" -H "Content-Type: application/json" \
      -d "{\"model\": \"${AGENT_API_MODEL}\", \"input\": \"${AGENT_API_PROMPT}\"}" || true)"
    stop_port_forward
    for marker in ${AGENT_API_ACCEPT_MARKERS}; do
      if [[ "${response}" == *"${marker}"* ]]; then
        pass "${name}" "the agent API answered a turn on attempt ${attempt}/${TURN_ATTEMPTS}"
        return 0
      fi
    done
    detail="${response:-empty response}"
    note "${name}: attempt ${attempt}/${TURN_ATTEMPTS} got no answer: ${detail:0:200}"
  done
  fail "${name}" "no answer from ${AGENT_API_PATH} in ${TURN_ATTEMPTS} attempts; last: ${detail:0:300}"
}

# ─── The bus task ────────────────────────────────────────────────────────────
# Through the lane's own client. Exits 0 on a completed terminal, the
# unreachable status when nothing could be submitted, 1 otherwise; prints one
# line saying what happened. A door that answered and refused (a 401 for a
# token it does not hold, any other 4xx) was reached, and the same request
# through a fresh tunnel gets the same answer, so that is 1. Every exit that
# leaves the task active is followed by a cancel naming it, as the harness
# does (inject_transport.py, the outcomes' comment): a task nobody took
# stays on the stream for a bridge that binds later, and one at its deadline
# keeps running after this script has failed it.
readonly PY_BUS_TASK='
import sys, time
sys.path.insert(0, sys.argv[1])
from kube_agents_bench import inject_transport as it
base, token, conversation, prompt, timeout, unreachable = sys.argv[2:8]
LEAVES_ACTIVE = (it.OUTCOME_DEADLINE, it.OUTCOME_QUEUED, it.OUTCOME_PARKED, it.OUTCOME_NEVER_STARTED, it.OUTCOME_UNCLASSIFIED)
task = it.InjectTask(base_url=base, conversation=conversation, prompt=prompt, token=token, message_id=conversation)
try:
    task.preflight()
    task_id = task.submit()
except it.InjectUnavailable as exc:
    if exc.answered and not exc.retryable:
        print("the door refused the request, and a fresh tunnel would not change that: %s" % exc)
        sys.exit(1)
    print("the door could not be reached: %s" % exc)
    sys.exit(int(unreachable) if not task.task_id else 1)
if not task_id:
    print("the door started no task (refusal %s): %s" % (task.refusal or "none", task.note))
    sys.exit(1)
try:
    exchange = task.await_terminal(task_id, deadline=time.monotonic() + float(timeout))
except it.InjectUnavailable as exc:
    task.cancel(task_id, settle=0)
    print("task %s: the door was lost while waiting (cancel %s): %s" % (
        task_id, "published" if task.cancel_sent else "not published", exc))
    sys.exit(1)
fold = exchange.fold
cancel = ""
if exchange.outcome in LEAVES_ACTIVE:
    task.cancel(task_id, settle=0)
    cancel = ", cancel %s" % ("published" if task.cancel_sent else "not published")
print("task %s on %s: outcome %s, terminal %s (source %s, reason %s)%s" % (
    task_id, exchange.conversation, exchange.outcome, fold.terminal or "none",
    fold.terminal_source or "none", fold.terminal_reason or "none", cancel))
done = exchange.outcome in (it.OUTCOME_TERMINAL, it.OUTCOME_STREAM_TERMINAL) and fold.terminal == it.STATE_COMPLETED
sys.exit(0 if done else 1)
'
assert_bus_task() {
  local name="$1" token attempt url line rc conversation err
  token="$(secret_value "${INJECT_NAME}" "${INJECT_TOKEN_FIELD}")" || fail "${name}" "secret/${INJECT_NAME} could not be read"
  [ -n "${token}" ] || fail "${name}" "secret/${INJECT_NAME} has no ${INJECT_TOKEN_FIELD}: the install has no inject door (A2A_INJECT_BACKEND=true on the operator)"
  conversation="${BUS_TASK_CONVERSATION_PREFIX}/${name}/$(date -u +%s)"
  line="no attempt ran"
  for ((attempt = 1; attempt <= BUS_TASK_ATTEMPTS; attempt++)); do
    url="${INJECT_URL}"
    if [ -z "${url}" ]; then
      if ! start_port_forward "${INJECT_NAME}" "${INJECT_LOCAL_PORT}" "${INJECT_PORT}"; then
        line="the port-forward to svc/${INJECT_NAME} did not listen within ${PORT_FORWARD_WAIT}s"
        continue
      fi
      url="http://127.0.0.1:${INJECT_LOCAL_PORT}"
    fi
    rc=0
    err="$(mktemp)"
    line="$(python3 -c "${PY_BUS_TASK}" "${BENCH_DIR}" "${url}" "${token}" "${conversation}" \
      "${BUS_TASK_PROMPT}" "${BUS_TASK_TIMEOUT}" "${BUS_TASK_UNREACHABLE_STATUS}" 2>"${err}")" || rc=$?
    stop_port_forward
    if [ "${rc}" -eq 0 ]; then
      rm -f "${err}"
      pass "${name}" "${line}"
      return 0
    fi
    # The client's log and any traceback, which say what the one line cannot.
    note "${name}: the client's stderr: $(tail -n "${CLIENT_ERR_TAIL_LINES}" "${err}" | tr '\n' ' ')"
    rm -f "${err}"
    [ "${rc}" -eq "${BUS_TASK_UNREACHABLE_STATUS}" ] || break
    note "${name}: attempt ${attempt}/${BUS_TASK_ATTEMPTS}: ${line}"
  done
  fail "${name}" "${line:-the client printed nothing}"
}

# Waits for every sidecar that looks like a bus client to log that its
# consumer is bound.
wait_bridges_consuming() {
  local name="$1" bound="$2" deadline sidecar missing
  deadline=$((SECONDS + bound))
  while :; do
    missing=""
    for sidecar in ${BUS_SIDECARS}; do
      if ! k logs "deployment/${AGENT_DEPLOYMENT}" -c "${sidecar}" --tail="${BRIDGE_LOG_TAIL_LINES}" 2>/dev/null | grep -qF "${BRIDGE_CONSUMING_LOG_MSG}"; then
        missing="${missing} ${sidecar}"
      fi
    done
    if [ -z "${missing}" ]; then
      pass "${name}" "${BUS_SIDECARS# } logged ${BRIDGE_CONSUMING_LOG_MSG}"
      return 0
    fi
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      fail "${name}" "not consuming after ${bound}s:${missing}"
    fi
    sleep "${POLL_SECONDS}"
  done
}

# Merge-patches the CR and waits for the operator to reconcile it: the agent
# Deployment's generation moves. BUS_DOWN is set by the flip to today once
# the API has accepted it, not before: a flip that is refused, or never sent
# because the agent could not be read, leaves the CR at next with the bus up,
# and the way out declares the sidecars again.
patch_and_reconcile() {
  local leg="$1" what="$2" patch="$3" before
  before="$(agent_generation)" || fail "${leg}.${what}.patched" "deployment/${AGENT_DEPLOYMENT} could not be read"
  k patch platformagent "${CR_NAME}" --type merge -p "${patch}" >/dev/null || fail "${leg}.${what}.patched" "kubectl patch was refused"
  if [ "${leg}.${what}" = "leg1.mode-today" ]; then
    BUS_DOWN=1
  fi
  pass "${leg}.${what}.patched" "merge patch applied"
  wait_generation_past "${leg}.${what}.reconciled" "${before}" "${GENERATION_TIMEOUT}"
}

# patch_and_reconcile, then the agent rolls and the CR reports Ready. For a
# patch whose Ready waits on nothing slower than the agent's own rollout:
# not the flip forward, whose Ready waits on provisioning (leg 2 below).
patch_and_settle() {
  patch_and_reconcile "$@"
  wait_rolled "$1.$2.agent-rolled" deployment "${AGENT_DEPLOYMENT}" "${ROLLOUT_TIMEOUT}"
  wait_cr_ready "$1.$2.ready" "${READY_TIMEOUT}"
}

# What is left of the leg 2 bring-up's budget, never below zero (a wait given
# zero reads once and fails if it is not there yet).
BRINGUP_DEADLINE=0
bringup_left() {
  local left=$((BRINGUP_DEADLINE - SECONDS))
  echo $((left > 0 ? left : 0))
}

# ─── The run ─────────────────────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || {
  echo "FAIL preflight.tools: python3 is required"
  exit 1
}

note "rollback round trip on platformagent/${CR_NAME} in ${NAMESPACE}${ROLLBACK_KUBE_CONTEXT:+ (context ${ROLLBACK_KUBE_CONTEXT})}"
note "kept across the flip: pvc/${NATS_PVC}, secret/${CREDS_SECRET}"

# Leg 0: a healthy next install, and the two objects as they are now.
CURRENT_LEG="pre"
read_cr_state || fail "pre.cr-readable" "platformagent/${CR_NAME} could not be read in ${NAMESPACE}"
[ "${CR_MODE}" = "${MODE_NEXT}" ] || fail "pre.mode-next" "spec.mode is ${CR_MODE}; the round trip starts from a next install"
pass "pre.mode-next" "spec.mode is next"
wait_cr_ready "pre.ready" "${PRE_READY_TIMEOUT}"
record_degraded_baseline "pre.degraded-baseline"
wait_rolled "pre.nats-ready" statefulset "${NATS_NAME}" "${PRE_READY_TIMEOUT}"
wait_rolled "pre.callout-serving" deployment "${CALLOUT_NAME}" "${PRE_READY_TIMEOUT}"
wait_rolled "pre.gateway-serving" deployment "${A2A_GATEWAY_NAME}" "${PRE_READY_TIMEOUT}"
ASSERTED_UID=""
assert_present "pre.pvc-present" pvc "${NATS_PVC}"
PVC_UID="${ASSERTED_UID}"
assert_present "pre.creds-present" secret "${CREDS_SECRET}"
CREDS_UID="${ASSERTED_UID}"
assert_bus_task "pre.bus-task"

# Leg 1: next -> today.
CURRENT_LEG="leg1"
CR_JSON="$(k_read get platformagent "${CR_NAME}" -o json)" || fail "leg1.cr-readable" "platformagent/${CR_NAME} could not be read"
SIDECAR_SPLIT="$(printf '%s' "${CR_JSON}" | python3 -c "${PY_SIDECARS}" "${CREDS_SECRET}" "${NATS_NAME}")"
UNSET_SIDECARS="$(printf '%s\n' "${SIDECAR_SPLIT}" | sed -n 1p)"
BUS_SIDECARS="$(printf '%s\n' "${SIDECAR_SPLIT}" | sed -n 2p)"
if [ -n "${UNSET_SIDECARS}" ]; then
  # Kept out of the log: a sidecar copied from the agent container carries
  # its environment. Saved before the patch, so a failure from here on
  # finds it.
  SIDECAR_NAMES="${UNSET_SIDECARS}"
  SIDECARS_FILE="$(umask 077 && mktemp)"
  printf '%s' "${CR_JSON}" | python3 -c "${PY_SIDECARS_PATCH}" >"${SIDECARS_FILE}"
  note "unsetting spec.deployment.sidecars (${UNSET_SIDECARS}) before the flip (a2a/docs/hermes-bridge.md)"
  patch_and_settle "leg1" "sidecars-unset" '{"spec":{"deployment":{"sidecars":null}}}'
else
  skip "leg1.sidecars-unset" "the CR declares no sidecars"
fi
patch_and_settle "leg1" "mode-today" "{\"spec\":{\"mode\":\"${MODE_TODAY}\"}}"
# The teardown ran to its end: the StatefulSet is the last thing cleanupA2A
# deletes, and the callout keys Secret is one it deletes rather than keeps,
# so a kept PVC and creds Secret below are kept through a teardown that
# happened, not through one that never started.
wait_absent "leg1.a2a-torn-down" "${TEARDOWN_TIMEOUT}" "statefulset ${NATS_NAME}" "deployment ${CALLOUT_NAME}" "secret ${CALLOUT_KEYS_SECRET}"
assert_same_uid "leg1.pvc-kept" pvc "${NATS_PVC}" "${PVC_UID}"
assert_same_uid "leg1.creds-kept" secret "${CREDS_SECRET}" "${CREDS_UID}"
wait_settled "leg1.nothing-stuck" "${MODE_TODAY}" "${SETTLE_TIMEOUT}"
assert_not_degraded "leg1.not-degraded"
assert_today_turn "leg1.today-turn"

# Leg 2: today -> next, on the kept objects.
CURRENT_LEG="leg2"
# The bring-up, in the deploy's order (hack/ci-deploy.sh, the mode patch):
# NATS, the callout, the provisioning Job, then the agent and the CR's Ready.
# Under next the operator reports Ready only once every split workload is,
# the Job included (readSplitWorkloads), and leg 1's flip dropped the
# BusProvisioned record that would otherwise stand in for it, so Ready here
# waits on a full provisioning run. Waiting on Ready first, under its own
# READY_TIMEOUT, would cut that run short of the budget it has; so the five
# waits share one deadline, BUS_UP_TIMEOUT from the reconcile, each getting
# what the ones before it left, and the slow one fails under its own name.
#
# The arithmetic, at the defaults: patch to Ready is at most
# GENERATION_TIMEOUT + BUS_UP_TIMEOUT = 300 + 1500 = 1800s, whatever the
# split between the five. That leaves 1800s of the 3600s ci-eval-pr.sh gives
# the whole run (EVAL_ROLLBACK_TIMEOUT_SECONDS) for pre, leg 1 and the rest of
# leg 2, which take minutes on a healthy install; a run that needs more is
# stopped there and reports itself interrupted at the leg it was in. The
# job-level bound is ci-eval-pr.sh's, beside EVAL_ROLLBACK_START_BY_SECONDS:
# the run starts by 15600s of job age, deploy included, and with the 3600s
# bound and its 60s kill grace ends by 19260s, inside the 21600s deadline. The
# deploy gives the Job its 1500s (MODE_NEXT_PROVISION_JOB_TIMEOUT_SECONDS,
# which its comment says covers the wait for the callout too) after its NATS
# gate; here NATS comes out of the same 1500s, one StatefulSet pod binding a
# claim that already exists. The 19.5-minute run that comment records
# predates the operator holding the Job until a callout serves (#1702), and
# would still fit.
patch_and_reconcile "leg2" "mode-next" "{\"spec\":{\"mode\":\"${MODE_NEXT}\"}}"
BRINGUP_DEADLINE=$((SECONDS + BUS_UP_TIMEOUT))
note "leg 2 bring-up: NATS, callout, provisioning, agent rollout and Ready share ${BUS_UP_TIMEOUT}s"
wait_rolled "leg2.nats-ready" statefulset "${NATS_NAME}" "$(bringup_left)"
wait_rolled "leg2.callout-serving" deployment "${CALLOUT_NAME}" "$(bringup_left)"
# Any provisioning Job now is this leg's: leg1.nothing-stuck saw none left.
wait_provision_complete "leg2.provisioned" "$(bringup_left)"
BUS_DOWN=""
wait_rolled "leg2.mode-next.agent-rolled" deployment "${AGENT_DEPLOYMENT}" "$(bringup_left)"
wait_cr_ready "leg2.mode-next.ready" "$(bringup_left)"
assert_same_uid "leg2.pvc-kept" pvc "${NATS_PVC}" "${PVC_UID}"
assert_nats_on_claim "leg2.nats-on-kept-pvc"
assert_same_uid "leg2.creds-kept" secret "${CREDS_SECRET}" "${CREDS_UID}"
if [ -n "${SIDECARS_FILE}" ]; then
  patch_and_settle "leg2" "sidecars-restored" "$(cat "${SIDECARS_FILE}")"
  # Declared again: nothing for the failure report to hand back any more.
  rm -f "${SIDECARS_FILE}"
  SIDECARS_FILE=""
  # A restored bridge is an input to the TASKS consumer budget, so the patch
  # can re-render the provisioning Job; its run is measured against the
  # stream the kept PVC still holds.
  wait_provision_complete "leg2.reprovisioned" "${BUS_UP_TIMEOUT}"
  if [ -n "${BUS_SIDECARS}" ]; then
    wait_bridges_consuming "leg2.bridge-consuming" "${BRIDGE_TIMEOUT}"
  else
    skip "leg2.bridge-consuming" "no restored sidecar looks like a bus client (${SIDECAR_NAMES}), so no consumer log is waited on"
  fi
  note "sidecar(s) ${SIDECAR_NAMES} declared again"
else
  skip "leg2.sidecars-restored" "no sidecars were unset in leg 1"
fi
wait_rolled "leg2.verifier-serving" deployment "${A2A_VERIFIER_NAME}" "${ROLLOUT_TIMEOUT}"
wait_rolled "leg2.gateway-serving" deployment "${A2A_GATEWAY_NAME}" "${ROLLOUT_TIMEOUT}"
assert_bus_task "leg2.bus-task"
wait_settled "leg2.nothing-stuck" "${MODE_NEXT}" "${SETTLE_TIMEOUT}"
assert_not_degraded "leg2.not-degraded"

CURRENT_LEG="done"
echo "ROLLBACK ROUND TRIP PASSED: next -> today -> next on platformagent/${CR_NAME}, pvc/${NATS_PVC} (uid ${PVC_UID}) and secret/${CREDS_SECRET} (uid ${CREDS_UID}) kept throughout"
record "PASSED"
