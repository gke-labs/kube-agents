#!/usr/bin/env bash
# ==============================================================================
# Shared Prow CI Environment Configuration
# ==============================================================================
# Centralizes common variables sourced by ci-deploy.sh, ci-eval-pr.sh, and ci-teardown.sh.
# ==============================================================================

# gke_dns_endpoint_flag, so every CI get-credentials picks the same endpoint the
# installer would. Only this helper is pulled in, not scripts/installer/common.sh,
# whose state file and print_* helpers CI has no use for.
# shellcheck source=scripts/installer/gke_dns_endpoint.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts/installer" && pwd)/gke_dns_endpoint.sh"

# TODO(boskos): Once oss-test-infra#2655 merges and deploys Boskos project leasing,
# consider failing closed if JOB_NAME is set and PROJECT_ID is unset.
export PROJECT_ID="${PROJECT_ID:-kube-agents-evals}"
export GCP_PROJECT_ID="${PROJECT_ID}"
export REGION="${REGION:-us-central1}"

export HOST_CLUSTER_NAME="platform-agent-host"
export CLUSTER_NAME="${HOST_CLUSTER_NAME}"
# GKE_CLUSTER_NAME (the per-run task cluster devops-bench provisions) is set by
# ci-eval-pr.sh, derived from the Prow BUILD_ID so concurrent runs never share
# a name. Deploy and teardown never touch a task cluster, so it is not set here.

export TARGET_NAMESPACE="kubeagents-system"
export NAMESPACE="${TARGET_NAMESPACE}"
export PR_ID="${PULL_NUMBER:-local}"

# ─── Helm Bootstrap ──────────────────────────────────────────────────────────
# The Prow job image carries gcloud, kubectl, and go, but no helm — and
# ci-deploy.sh / ci-teardown.sh drive the kube-agents chart with it. Install a
# checksum-pinned binary when the image has none; a machine that already has
# helm on PATH (a developer laptop, a GitHub runner) is left alone.
HELM_VERSION="v3.21.4"
HELM_SHA256_LINUX_AMD64="61f88ab166748cb19604d7884cb100ae9ccb13804ddeb98e08af167eacbb6a14"
HELM_SHA256_LINUX_ARM64="b54c04b4e0b2540bbdc08c17a121dab70e9a2ed0de5705528fec68a5fd3b85a7"
# get.helm.sh has bad seconds, and a failed transfer here fails a Prow job that
# had nothing to do with helm.
readonly HELM_DOWNLOAD_RETRIES=5

# ─── Gateway log capture ─────────────────────────────────────────────────────
# collect_gateway_log keeps the tail of the platform-agent-gateway log as an
# artifact of every eval run, green ones included. The failure-only dump used
# to be the only capture, so a passing nightly whose repetitions ran to the
# delegation ceiling left no record of what the dispatcher and the workers
# were doing (429 storms and a stuck dispatcher both live only in this log).
# Bounded twice: kubectl's line tail, then a byte cap on what is written. At
# the ~110 bytes/line a nightly's gateway log runs, the line tail is about
# 2 MB per run; the byte cap is the belt for a log with long lines.
readonly GATEWAY_LOG_TAIL_LINES=20000
readonly GATEWAY_LOG_MAX_BYTES=$((8 * 1024 * 1024))
# The failure dump's litellm and envoy tails. At 1000 and 2000 lines a
# six-hour run came back clipped to its last minutes, without the 429s and
# token failures those two logs are collected for.
readonly LITELLM_LOG_TAIL_LINES=20000
readonly ENVOY_LOG_TAIL_LINES=20000

# ─── Agent pod diagnostics ───────────────────────────────────────────────────
# collect_agent_pod_diagnostics keeps, on every eval run, what says why a pod
# in the install was replaced or a container restarted mid-run: the Hermes
# bridge sidecar's log (the executor under EVAL_MODE_NEXT=1), the previous
# instance of the agent and bridge containers, each pod's restart counts and
# last termination, and the namespace's events. The gateway log alone cannot:
# a replaced pod starts a fresh log, so a green run whose repetitions ended
# "bridge-shutdown" kept no record of whether it was an OOM kill, an eviction
# or a rollout. Same bounds as the gateway log: a line tail, then a byte cap.
#
# The snapshot runs at exit, hours after an early repetition: events have
# aged out by then (the apiserver keeps them an hour), and `logs --previous`
# reads the current pod, so a replaced pod's containers are gone with it. So
# start_agent_pod_watch, started once the eval knows its host cluster, streams
# pod changes and events to a scratch file for the eval's lifetime, and the
# snapshot stops it and keeps the tail. What no capture here recovers is the
# log of a container in a pod that has since been deleted.
readonly AGENT_DIAG_LOG_TAIL_LINES=20000
readonly AGENT_DIAG_LOG_MAX_BYTES=$((8 * 1024 * 1024))
# Events are one line each; the namespace's whole retained window fits.
readonly AGENT_DIAG_EVENTS_TAIL_LINES=5000
# The agent's own container, and the bridge sidecar hack/ci-deploy.sh declares
# under EVAL_MODE_NEXT=1 (its BRIDGE_SIDECAR_NAME). Named apart from that
# constant because ci-deploy.sh sources this file and both are readonly.
readonly AGENT_DIAG_AGENT_CONTAINER="platform-agent"
readonly AGENT_DIAG_BRIDGE_CONTAINER="hermes-bridge"
readonly AGENT_DIAG_DEPLOYMENT="deployment/platform-agent-gateway"
# Every one-shot read gives up after this, so an unreachable cluster costs
# seconds per call on an exit that is often already an infrastructure failure.
readonly AGENT_DIAG_REQUEST_TIMEOUT="30s"
# A watch held if it lasted HEALTHY_SECONDS and exited 0: the request timeout
# below is sent to the apiserver, which ends the watch cleanly, as it does its
# own 30-60 minute close. One that held is reopened at once. Any other exit
# (fast, or a slow dial or hang that errors) is retried after this pause, at
# this pace while the cluster is unreachable.
readonly AGENT_DIAG_WATCH_RESTART_SECONDS=10
readonly AGENT_DIAG_WATCH_HEALTHY_SECONDS=30
# Each watch is also cut at this, so the loop re-checks that the eval is still
# alive every few minutes rather than once per apiserver-closed watch (up to
# an hour): a loop outliving a SIGKILLed eval is bounded by it.
readonly AGENT_DIAG_WATCH_REQUEST_TIMEOUT="300s"
# Absolute times only: a watch line is read hours after it was printed, so the
# relative ages `-o wide` prints would say nothing.
readonly AGENT_DIAG_WATCH_POD_COLUMNS="NAME:.metadata.name,PHASE:.status.phase,REASON:.status.reason,STARTED:.status.startTime,DELETING:.metadata.deletionTimestamp,CONTAINERS:.status.containerStatuses[*].name,RESTARTS:.status.containerStatuses[*].restartCount,LAST:.status.containerStatuses[*].lastState.terminated.reason,EXIT:.status.containerStatuses[*].lastState.terminated.exitCode,FINISHED:.status.containerStatuses[*].lastState.terminated.finishedAt"
readonly AGENT_DIAG_WATCH_EVENT_COLUMNS="FIRST:.firstTimestamp,LAST:.lastTimestamp,EVENT:.eventTime,COUNT:.count,TYPE:.type,REASON:.reason,OBJECT:.involvedObject.kind,NAME:.involvedObject.name,MESSAGE:.message"
# One line per pod, then one per container: restarts, the last termination
# (reason, exit code, when), and since when it has been running. Evicted pods
# carry their reason at pod level, so the pod line has status.reason.
readonly AGENT_DIAG_POD_STATUS_JSONPATH='{range .items[*]}{.metadata.name}{"\tphase="}{.status.phase}{"\treason="}{.status.reason}{"\tstarted="}{.status.startTime}{"\n"}{range .status.containerStatuses[*]}{"  "}{.name}{"\trestarts="}{.restartCount}{"\tlast="}{.lastState.terminated.reason}{"\texit="}{.lastState.terminated.exitCode}{"\tfinished="}{.lastState.terminated.finishedAt}{"\trunningSince="}{.state.running.startedAt}{"\n"}{end}{end}'

ensure_helm() {
  if command -v helm >/dev/null 2>&1; then
    return 0
  fi
  local arch sha dir
  if [ "$(uname -s)" != "Linux" ]; then
    echo "ERROR: no helm on PATH and the pinned download covers Linux only. Install helm and re-run." >&2
    return 1
  fi
  case "$(uname -m)" in
    x86_64) arch="amd64"; sha="$HELM_SHA256_LINUX_AMD64" ;;
    aarch64 | arm64) arch="arm64"; sha="$HELM_SHA256_LINUX_ARM64" ;;
    *)
      echo "ERROR: no helm on PATH and no pinned download for architecture '$(uname -m)'. Install helm and re-run." >&2
      return 1
      ;;
  esac
  dir="/tmp/kube-agents-helm-${HELM_VERSION}-${arch}"
  if [ ! -x "${dir}/helm" ]; then
    mkdir -p "$dir"
    curl -fsSL --retry "$HELM_DOWNLOAD_RETRIES" --retry-all-errors "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz" -o "${dir}/helm.tar.gz"
    echo "${sha}  ${dir}/helm.tar.gz" | sha256sum -c - >/dev/null
    tar -xzf "${dir}/helm.tar.gz" -C "$dir" --strip-components=1 "linux-${arch}/helm"
    rm -f "${dir}/helm.tar.gz"
  fi
  export PATH="${dir}:${PATH}"
  echo "Installed helm ${HELM_VERSION} (${arch}) to ${dir}"
}

# ─── Bench Result Collection (runs on PASS as well as on failure) ──────────────
# Split out of dump_prow_artifacts_on_failure, which wraps its whole body in
# `if [ "$exit_code" -ne 0 ]`. That meant the eval job had NEVER kept a record
# from a passing run -- exactly backwards for a rate-based gate, whose baseline
# store is built from green runs on main. The copy is one `cp`, so there is no
# reason to condition it; the expensive diagnostics (describe pods, gcloud
# builds list, the controller, LiteLLM and Envoy tails) stay failure-only below.
#
# Callers must invoke this BEFORE dump_prow_artifacts_on_failure: that function
# reads `$?` on its first line, so anything running ahead of it must leave the
# status alone. Every command here ends in `|| true` for that reason.
collect_bench_results() {
  local artifact_dir="${ARTIFACTS:-/tmp/artifacts}"
  mkdir -p "${artifact_dir}" || true

  # Devops-bench Evaluation Results (if run in eval script)
  if [ -d "${SCRIPT_DIR}/../bench/results" ]; then
    cp -r "${SCRIPT_DIR}/../bench/results/"* "${artifact_dir}/" 2>/dev/null || true
  elif [ -d "/app/results" ]; then
    cp -r /app/results/* "${artifact_dir}/" 2>/dev/null || true
  fi
  cp results_*.json "${artifact_dir}/" 2>/dev/null || true
}

# ─── Gateway Log Collection (runs on PASS as well as on failure) ──────────────
# The bounded tail of the platform-agent-gateway log, written on every exit
# (GATEWAY_LOG_TAIL_LINES / GATEWAY_LOG_MAX_BYTES above say why and how much).
# Same contract as collect_bench_results: every command ends in `|| true`, so
# it leaves `$?` alone for a dumper that runs after it, and a cluster that
# cannot be reached costs the run its gateway log and nothing else. The
# failure dumper below calls this rather than taking its own, shorter tail, so
# the failure path never overwrites the every-run capture with less.
collect_gateway_log() {
  local artifact_dir="${ARTIFACTS:-/tmp/artifacts}"
  local ns="${TARGET_NAMESPACE:-${NAMESPACE:-kubeagents-system}}"
  mkdir -p "${artifact_dir}" || true
  # Pinned to the agent cluster when the pin is known: the task loop's tofu
  # stacks repoint kubectl's current context at their own clusters
  # (bench/README.md) and the EXIT trap runs after the last of them, so the
  # ambient context is not reliably the host by then. AGENT_CLUSTER_CONTEXT
  # is the pin ci-eval-pr.sh exports for the bench's own kubectl; unset (the
  # deploy script's failure path) the ambient context is the host cluster.
  # shellcheck disable=SC2086
  kubectl ${AGENT_CLUSTER_CONTEXT:+--context "${AGENT_CLUSTER_CONTEXT}"} logs deployment/platform-agent-gateway \
    -n "${ns}" --tail="${GATEWAY_LOG_TAIL_LINES}" 2>&1 \
    | tail -c "${GATEWAY_LOG_MAX_BYTES}" > "${artifact_dir}/platform-agent-gateway.log" || true
}

# ─── Agent Pod Watch (the eval's lifetime) ───────────────────────────────────
# Re-opens `"$@"` whenever the apiserver closes it, appending to `out`, until
# TERM or until `parent` is gone. The kubectl runs as a child and is killed by
# the trap: killing only this loop would orphan a watch that never ends.
_agent_pod_watch_loop() {
  local parent="$1" out="$2"
  shift 2
  local child="" pause="" only=() opened=0 rc=0
  trap 'kill ${child} ${pause} 2>/dev/null; exit 0' TERM
  while kill -0 "${parent}" 2>/dev/null; do
    opened=${SECONDS}
    "$@" ${only[@]+"${only[@]}"} >> "${out}" 2>&1 &
    child=$!
    rc=0
    wait "${child}" || rc=$?
    child=""
    if (( rc == 0 && SECONDS - opened >= AGENT_DIAG_WATCH_HEALTHY_SECONDS )); then
      # Every open lists everything before it watches. After a held open the
      # stream is continuous but for one kubectl restart, and a reprint per
      # cut would push the record out of the byte cap.
      only=(--watch-only)
      continue
    fi
    # Anything else means the watch could not be held (unreachable, 5xx, auth):
    # the stream has a gap of unknown length, so the next open lists again.
    only=()
    # Backgrounded and waited on, not run in the foreground: bash defers a
    # trap until a foreground command returns, which held every exit for the
    # whole pause.
    sleep "${AGENT_DIAG_WATCH_RESTART_SECONDS}" &
    pause=$!
    wait "${pause}" || true
    pause=""
  done
}

# Starts the pod and event watches the AGENT_DIAG_ header describes, pinned to
# AGENT_CLUSTER_CONTEXT. Call once the context is known; a second call is a
# no-op. `disown` for the reason the Boskos heartbeat gives in ci-eval-pr.sh:
# the fan-out sizes its lanes with `jobs -rp`. The loops' own output goes to
# /dev/null, for the other reason hack/boskos_heartbeat.sh gives: a process
# still holding the job's stdout after the eval is SIGKILLed keeps the Prow
# job open. Never fails the caller.
AGENT_DIAG_WATCH_PIDS=""
AGENT_DIAG_WATCH_DIR=""
start_agent_pod_watch() {
  [ -z "${AGENT_DIAG_WATCH_PIDS}" ] || return 0
  local ns="${TARGET_NAMESPACE:-${NAMESPACE:-kubeagents-system}}"
  local kctl=(kubectl --request-timeout="${AGENT_DIAG_WATCH_REQUEST_TIMEOUT}")
  if [ -n "${AGENT_CLUSTER_CONTEXT:-}" ]; then
    kctl+=(--context "${AGENT_CLUSTER_CONTEXT}")
  fi
  AGENT_DIAG_WATCH_DIR=$(mktemp -d) || return 0
  _agent_pod_watch_loop "$$" "${AGENT_DIAG_WATCH_DIR}/pods" \
    "${kctl[@]}" get pods -n "${ns}" --watch -o custom-columns="${AGENT_DIAG_WATCH_POD_COLUMNS}" \
    >/dev/null 2>&1 &
  AGENT_DIAG_WATCH_PIDS="$!"
  disown "$!" 2>/dev/null || true
  _agent_pod_watch_loop "$$" "${AGENT_DIAG_WATCH_DIR}/events" \
    "${kctl[@]}" get events -n "${ns}" --watch -o custom-columns="${AGENT_DIAG_WATCH_EVENT_COLUMNS}" \
    >/dev/null 2>&1 &
  AGENT_DIAG_WATCH_PIDS="${AGENT_DIAG_WATCH_PIDS} $!"
  disown "$!" 2>/dev/null || true
  return 0
}

# Stops the watches and keeps their tails. Only the first call after a start
# writes, so the failure dumper's second call leaves the files in place.
_stop_agent_pod_watch() {
  local artifact_dir="$1"
  [ -n "${AGENT_DIAG_WATCH_PIDS}" ] || return 0
  local pid
  for pid in ${AGENT_DIAG_WATCH_PIDS}; do
    kill "${pid}" 2>/dev/null || true
  done
  AGENT_DIAG_WATCH_PIDS=""
  tail -c "${AGENT_DIAG_LOG_MAX_BYTES}" "${AGENT_DIAG_WATCH_DIR}/pods" \
    > "${artifact_dir}/agent-pods-watch.txt" 2>/dev/null || true
  tail -c "${AGENT_DIAG_LOG_MAX_BYTES}" "${AGENT_DIAG_WATCH_DIR}/events" \
    > "${artifact_dir}/agent-events-watch.txt" 2>/dev/null || true
}

# ─── Agent Pod Diagnostics (runs on PASS as well as on failure) ───────────────
# The AGENT_DIAG_ constants above say what and why. Same contract as
# collect_gateway_log: every command ends in `|| true`, so `$?` is left for
# the dumper and an unreachable cluster costs these files and nothing else;
# pinned to AGENT_CLUSTER_CONTEXT for the same reason. A `--previous` read of
# a container that never restarted writes kubectl's "not found" into its file,
# which is the answer, not an error. The bridge files are written only when
# the Deployment declares the sidecar, so a today-mode run gains no empty ones.
# Runs once per process: the eval's trap takes it and the failure dumper's
# second call returns at once, rather than repeating every read against a
# cluster that may be what failed, inside the deadline's grace.
AGENT_DIAG_COLLECTED=""
collect_agent_pod_diagnostics() {
  [ -z "${AGENT_DIAG_COLLECTED}" ] || return 0
  AGENT_DIAG_COLLECTED=1
  local artifact_dir="${ARTIFACTS:-/tmp/artifacts}"
  local ns="${TARGET_NAMESPACE:-${NAMESPACE:-kubeagents-system}}"
  local kctl=(kubectl --request-timeout="${AGENT_DIAG_REQUEST_TIMEOUT}")
  if [ -n "${AGENT_CLUSTER_CONTEXT:-}" ]; then
    kctl+=(--context "${AGENT_CLUSTER_CONTEXT}")
  fi
  mkdir -p "${artifact_dir}" || true
  _stop_agent_pod_watch "${artifact_dir}"

  "${kctl[@]}" get pods -n "${ns}" -o jsonpath="${AGENT_DIAG_POD_STATUS_JSONPATH}" \
    > "${artifact_dir}/agent-pod-status.txt" 2>&1 || true
  "${kctl[@]}" get events -n "${ns}" --sort-by=.lastTimestamp -o wide 2>&1 \
    | tail -n "${AGENT_DIAG_EVENTS_TAIL_LINES}" > "${artifact_dir}/k8s-events.txt" || true
  "${kctl[@]}" top pods -n "${ns}" --containers > "${artifact_dir}/agent-pod-top.txt" 2>&1 || true

  "${kctl[@]}" logs "${AGENT_DIAG_DEPLOYMENT}" -c "${AGENT_DIAG_AGENT_CONTAINER}" -n "${ns}" \
    --previous --tail="${AGENT_DIAG_LOG_TAIL_LINES}" 2>&1 \
    | tail -c "${AGENT_DIAG_LOG_MAX_BYTES}" > "${artifact_dir}/platform-agent-previous.log" || true

  local containers=""
  if ! containers=$("${kctl[@]}" get "${AGENT_DIAG_DEPLOYMENT}" -n "${ns}" \
    -o jsonpath='{.spec.template.spec.containers[*].name}' 2>/dev/null); then
    # Unknown is not absent: try the bridge reads, so their error is on record
    # rather than the run looking like one with no sidecar.
    containers="${AGENT_DIAG_BRIDGE_CONTAINER}"
  fi
  case " ${containers} " in
    *" ${AGENT_DIAG_BRIDGE_CONTAINER} "*)
      "${kctl[@]}" logs "${AGENT_DIAG_DEPLOYMENT}" -c "${AGENT_DIAG_BRIDGE_CONTAINER}" -n "${ns}" \
        --tail="${AGENT_DIAG_LOG_TAIL_LINES}" 2>&1 \
        | tail -c "${AGENT_DIAG_LOG_MAX_BYTES}" > "${artifact_dir}/hermes-bridge.log" || true
      "${kctl[@]}" logs "${AGENT_DIAG_DEPLOYMENT}" -c "${AGENT_DIAG_BRIDGE_CONTAINER}" -n "${ns}" \
        --previous --tail="${AGENT_DIAG_LOG_TAIL_LINES}" 2>&1 \
        | tail -c "${AGENT_DIAG_LOG_MAX_BYTES}" > "${artifact_dir}/hermes-bridge-previous.log" || true
      ;;
  esac
}

# ─── Shared Artifact Collection Handler for Prow Job Failures ───────────────────
dump_prow_artifacts_on_failure() {
  local exit_code=$?
  if [ "$exit_code" -ne 0 ]; then
    local artifact_dir="${ARTIFACTS:-/tmp/artifacts}"
    mkdir -p "${artifact_dir}"
    echo "⚠️ Script failed (exit code ${exit_code}). Dumping diagnostics and logs to Prow artifacts (${artifact_dir})..."
    local ns="${TARGET_NAMESPACE:-${NAMESPACE:-kubeagents-system}}"
    
    # 1. Pipeline Summary & Cloud Build / Port-Forward Diagnostics (works even if kubectl fails)
    {
      echo "=== EXIT CODE: ${exit_code} ==="
      echo "=== TIMESTAMP: $(date -u +'%Y-%m-%dT%H:%M:%SZ') ==="
      echo "=== ACTIVE KUBECTL CONTEXT ==="
      kubectl config current-context 2>&1 || true
      echo "=== RECENT CLOUD BUILDS ==="
      gcloud builds list --project="${PROJECT_ID}" --limit=5 2>&1 || true
      echo "=== PORT FORWARD LOG (/tmp/pf-8642.log) ==="
      cat /tmp/pf-8642.log 2>&1 || true
    } > "${artifact_dir}/ci-failure-summary.txt" 2>&1 || true

    # 2. Current running & previous crashed pod logs (crucial for rollout deadline / CrashLoopBackOff failures).
    #    The running pod's log is the every-run capture above, taken again here
    #    so a caller without a green-path collector (ci-deploy.sh) still gets it.
    #    The previous agent container (platform-agent-previous.log), the
    #    bridge sidecar, pod restarts and events come from the every-run
    #    collector, called here for the same reason, with its bounds.
    collect_gateway_log
    collect_agent_pod_diagnostics
    kubectl logs deployment/kube-agents-controller-manager -n "${ns}" --tail=1000 > "${artifact_dir}/controller-manager.log" 2>&1 || true
    # The model path runs through LiteLLM, and with vertex_ai its failure
    # domain (Workload Identity token fetch, aiplatform 403s, model 404s)
    # is visible only in this pod's log -- the gateway just relays the text.
    kubectl logs deployment/litellm -n "${ns}" --tail="${LITELLM_LOG_TAIL_LINES}" > "${artifact_dir}/litellm.log" 2>&1 || true
    # The gateway capture above reads the pod's default container (platform-agent);
    # a dropped port-forward stream is only visible from the auth sidecar's side.
    kubectl logs deployment/platform-agent-gateway -c agent-api-auth -n "${ns}" --tail=2000 > "${artifact_dir}/agent-api-auth.log" 2>&1 || true
    kubectl logs deployment/platform-agent-credential-proxy -c envoy-credential-proxy -n "${ns}" --tail="${ENVOY_LOG_TAIL_LINES}" > "${artifact_dir}/envoy-credential-proxy.log" 2>&1 || true
    # Konnectivity/tunnel churn shows up as kube-system events, not in "${ns}".
    kubectl get events -n kube-system --sort-by=.lastTimestamp 2>&1 | tail -100 > "${artifact_dir}/kube-system-events.txt" 2>&1 || true

    # 3. Detailed Pod Descriptions & K8s Events (explains image pull errors, scheduling blocks, OOMKilled, probe failures)
    kubectl describe pods -n "${ns}" > "${artifact_dir}/k8s-pod-descriptions.txt" 2>&1 || true
    kubectl get pods,svc,events -n "${ns}" -o wide > "${artifact_dir}/k8s-cluster-status.txt" 2>&1 || true

    # 4. Devops-bench Evaluation Results used to be copied here. They now come
    #    from collect_bench_results() above, which runs on green too. Callers
    #    that want both must call it FIRST -- see the note on that function.
    collect_bench_results
  fi
}
