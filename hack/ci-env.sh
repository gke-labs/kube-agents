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

# ─── Credential-proxy capture ────────────────────────────────────────────────
# collect_credential_proxy_evidence keeps two artifacts of every eval run,
# green ones included: the broker's log tail (envoy-credential-proxy.log, the
# name the failure dump always used) and one scrape of its Prometheus
# listener (credential-proxy-metrics.prom). The log used to be taken by the
# failure dump alone, so a green run kept no record of the broker's admission
# queue, and nothing in the job read the listener (the chart's PodMonitoring
# and the operator's usage poller do, on an install that has them): the queue
# a presubmit run built up was visible only on a run that also failed. The
# scrape holds the
# admission-wait histogram, the refusal counter and the slot and budget
# gauges as the run left them. Taken with `kubectl exec` into the broker
# container rather than through a port-forward: the listener binds the
# container's own port, which the collector's pod network does not reach
# without a tunnel, and the container's python reads the port from the env
# the operator sets (CREDENTIAL_PROXY_METRICS_PORT), so no copy of it lives
# here. Bounded like the gateway log: kubectl's line tail (at 2000 lines a
# six-hour run came back clipped to its last minutes), then a byte cap. The
# exec is bounded by `timeout` where the runner has one: kubectl's
# --request-timeout governs plain API calls and not the exec upgrade, so a
# wedged node tunnel would otherwise hold the EXIT trap.
readonly CREDENTIAL_PROXY_LOG_TAIL_LINES=20000
readonly CREDENTIAL_PROXY_LOG_MAX_BYTES=$((8 * 1024 * 1024))
readonly CREDENTIAL_PROXY_LOG_ARTIFACT="envoy-credential-proxy.log"
readonly CREDENTIAL_PROXY_METRICS_ARTIFACT="credential-proxy-metrics.prom"
readonly CREDENTIAL_PROXY_DEPLOYMENT="deployment/platform-agent-credential-proxy"
readonly CREDENTIAL_PROXY_CONTAINER="envoy-credential-proxy"
readonly CREDENTIAL_PROXY_REQUEST_TIMEOUT="20s"
readonly CREDENTIAL_PROXY_SCRAPE_BOUND_SECONDS=30
# timeout(1)'s own status for a command it had to stop. It prints nothing
# when it does, and kubectl writes nothing on TERM, so the collector appends
# the note the artifact would otherwise lack.
readonly CREDENTIAL_PROXY_SCRAPE_TIMEOUT_STATUS=124
# The scrape, run by the image's `python3` (the proxy image carries one for
# the Cloud SDK; the broker's own venv interpreter is not on PATH and is not
# needed, the program is stdlib only). One line, so a test that lifts the
# readonly constants line by line gets all of it. A missing port variable or
# a listener that is not up prints its error into the artifact, which then
# says why it holds no series.
readonly CREDENTIAL_PROXY_METRICS_SCRAPE='import os, sys, urllib.request; port = os.environ["CREDENTIAL_PROXY_METRICS_PORT"]; sys.stdout.write(urllib.request.urlopen("http://127.0.0.1:%s/metrics" % port, timeout=10).read().decode())'

# ─── LiteLLM log capture ─────────────────────────────────────────────────────
# collect_litellm_log keeps the inference gateway's log as an artifact of
# every eval run, green ones included, as litellm.log. It used to be taken
# only by the failure dump, so a green run kept no record of the
# thinking-replay hook: its per-request "thinking replay: dropped N" line is
# the only sign it touched the run's traffic. The 429s, Workload Identity
# token fetches and Vertex 403/404s the failure dump took it for are here too.
# Every replica: the chart runs two, each serving its own share of requests,
# and `logs deployment/...` reads one of them. Bounded like the gateway log:
# kubectl's line tail, which it applies per pod (at 1000 lines a six-hour run
# came back clipped to its last minutes), then a byte cap on the whole file,
# twice the gateway log's for the chart's two replicas. kubectl prints the
# pods one after another, so a file over the cap loses the first pod's oldest
# lines first. Two full line tails fit under it at up to ~400 bytes a line,
# so the cap is the belt for a log with long lines.
readonly LITELLM_LOG_TAIL_LINES=20000
readonly LITELLM_LOG_MAX_BYTES=$((16 * 1024 * 1024))
readonly LITELLM_LOG_ARTIFACT="litellm.log"
# The existence check gives up after this, so an unreachable cluster costs
# the trap seconds rather than a hang inside the deadline's grace. The log
# read itself is unbounded, like the gateway log's: a timeout there would cut
# a large read short.
readonly LITELLM_LOG_REQUEST_TIMEOUT="30s"
# The Deployment that says LiteLLM is installed at all, and the pod label and
# container its pods carry; the chart and the kustomize base render the same
# three (charts/kube-agents/templates/litellm.yaml).
readonly LITELLM_LOG_DEPLOYMENT="deployment/litellm"
readonly LITELLM_LOG_POD_SELECTOR="app=litellm"
readonly LITELLM_LOG_CONTAINER="litellm-container"

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
# The agent's own container, and the bridge sidecar the operator renders
# beside it under EVAL_MODE_NEXT=1 (hack/ci-deploy.sh's BRIDGE_SIDECAR_NAME).
# Named apart from that constant because ci-deploy.sh sources this file and
# both are readonly.
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

# ─── A2A component logs ──────────────────────────────────────────────────────
# Under spec.mode: next the operator renders the bus stack (NATS, the auth
# callout, the verifier, the gateway, the console, the provision Job) and the
# gateway spawns session (worker) pods. A crash-looping one of these fails the
# deploy, and `describe` says only "exit code 1": the reason is a line in the
# container's own log. collect_a2a_component_logs keeps, for every such pod,
# the current log and, when a container has restarted, the previous one, as
# a2a-<component>-<pod>.log and a2a-<component>-<pod>-previous.log.
# Operator-rendered pods carry this label, its value the component.
readonly A2A_DIAG_COMPONENT_LABEL="kubeagents.x-k8s.io/a2a-component"
# Session pods are the gateway's, not the operator's, so they carry the
# standard component key instead (sessionRole in a2a/gateway/spawn.go, which
# stamps them). They run the a2a-worker image, and their files are named for
# that.
readonly A2A_DIAG_SESSION_SELECTOR="app.kubernetes.io/component=a2a-session"
readonly A2A_DIAG_SESSION_COMPONENT="worker"
# One line per pod: name, component label (empty for a session pod), and the
# restart count of each container. The label key's dots are escaped for
# kubectl's JSONPath.
readonly A2A_DIAG_PODS_JSONPATH='{range .items[*]}{.metadata.name}{"\t"}{.metadata.labels.kubeagents\.x-k8s\.io/a2a-component}{"\t"}{.status.containerStatuses[*].restartCount}{"\n"}{end}'
# The agent's line tail, half its byte cap: there are up to A2A_DIAG_MAX_PODS
# of these, two files each. Most are quiet (a start, a bus connection, the
# failure), and a crash's previous log is a few lines. The A2A gateway is not:
# it logs every task's ingress, spawn and end, so on a long nightly its file
# keeps the most recent stretch, cut by the byte cap.
readonly A2A_DIAG_LOG_TAIL_LINES=20000
readonly A2A_DIAG_LOG_MAX_BYTES=$((4 * 1024 * 1024))
# A bound on the pods read, so a session leak cannot turn one run's artifacts
# into hundreds of files. The operator renders under a dozen pods and the
# gateway caps sessions at A2A_MAX_SESSIONS, so a healthy install is well under.
readonly A2A_DIAG_MAX_PODS=40

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
# builds list, the controller and auth-sidecar tails) stay failure-only below.
#
# Callers must invoke this BEFORE dump_prow_artifacts_on_failure: that function
# reads `$?` on its first line, so anything running ahead of it must leave the
# status alone. Every command here ends in `|| true` for that reason.
collect_bench_results() {
  local artifact_dir="${ARTIFACTS:-/tmp/artifacts}"
  mkdir -p "${artifact_dir}" || true

  # Devops-bench Evaluation Results (if run in eval script)
  # BENCH_DIR, when the caller set one: on a pull request hack/ci-eval-pr.sh
  # runs devops-bench from a copy of main's bench/, which is where its
  # relative results/ lands.
  local results_dir="${BENCH_DIR:-${SCRIPT_DIR}/../bench}/results"
  if [ -d "${results_dir}" ]; then
    cp -r "${results_dir}/"* "${artifact_dir}/" 2>/dev/null || true
  elif [ -d "/app/results" ]; then
    cp -r /app/results/* "${artifact_dir}/" 2>/dev/null || true
  fi
  cp results_*.json "${artifact_dir}/" 2>/dev/null || true
}

# ─── Byte caps ───────────────────────────────────────────────────────────────
# Every container log the `collect_*` functions below keep is bounded twice:
# kubectl's line tail, then a byte cap, the last N bytes of what kubectl
# printed. The cap used to be `| tail -c N` on the pipe. GNU tail takes
# exactly N there; uutils tail, the coreutils of Ubuntu 26.04 (rust-coreutils
# 0.8.0 and 0.10.0, and upstream 0.12.0), keeps whole read chunks after a
# short read and prints more than N (uutils/coreutils#15804), so a capture on
# such a runner ran past its cap and the tests that pin the cap read
# 8851062 != 8388608. On a regular file both implementations seek and take
# exactly N, so the stream is spooled to a file first and the cap is taken
# from that; the watch tails in _stop_agent_pod_watch already read files and
# are unchanged. Same contract as the collectors: every command ends in
# `|| true`, a read that fails leaves what it printed (or the last N bytes of
# what reached the spool if the disk fills mid-stream), and the spool is
# removed either way.
last_bytes() {
  local max="$1" out="$2" spool
  spool="$(mktemp "${TMPDIR:-/tmp}/last-bytes.XXXXXX" 2>/dev/null)" || spool="${out}.spool"
  cat > "${spool}" || true
  tail -c "${max}" "${spool}" > "${out}" || true
  rm -f -- "${spool}" || true
}

# ─── Gateway Log Collection (runs on PASS as well as on failure) ──────────────
# The bounded tail of the platform-agent-gateway log, written on every exit
# (GATEWAY_LOG_TAIL_LINES / GATEWAY_LOG_MAX_BYTES above say why and how much).
# Same contract as collect_bench_results: every command ends in `|| true`, so
# it leaves `$?` alone for a dumper that runs after it, and a cluster that
# cannot be reached costs the run its gateway log and nothing else. The
# failure dumper below calls this rather than taking its own, shorter tail, so
# the failure path never overwrites the every-run capture with less.
# Once per process, like collect_agent_pod_diagnostics: ci-eval-pr.sh takes it
# before a next-mode run's rollback round trip replaces the pod, and the EXIT
# trap's call after that must not overwrite the eval's log with the new pod's.
# Read with a default, so the function alone, without this assignment, still
# runs under `set -u` (the test suites lift it that way).
GATEWAY_LOG_COLLECTED=""
collect_gateway_log() {
  [ -z "${GATEWAY_LOG_COLLECTED:-}" ] || return 0
  GATEWAY_LOG_COLLECTED=1
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
    | last_bytes "${GATEWAY_LOG_MAX_BYTES}" "${artifact_dir}/platform-agent-gateway.log" || true
}

# ─── LiteLLM Log Collection (runs on PASS as well as on failure) ──────────────
# The LITELLM_LOG_ constants above say what and why. Same contract as
# collect_gateway_log: every command ends in `|| true`, so `$?` is left for
# the dumper; pinned to AGENT_CLUSTER_CONTEXT for the same reason; once per
# process, so the failure dumper's call after the trap's keeps the first
# capture rather than writing the file again with different contents.
# An install without the Deployment (litellm.enabled=false, or one that names
# it otherwise) is a note, not a failure: the note goes to the job log and is
# the file's content, so the artifact says why it holds no log. A `get` that
# fails for any other reason (an unreachable cluster) is written the same way.
# `--ignore-errors` so one replica's failed read does not stop the other's.
LITELLM_LOG_COLLECTED=""
collect_litellm_log() {
  [ -z "${LITELLM_LOG_COLLECTED:-}" ] || return 0
  LITELLM_LOG_COLLECTED=1
  local artifact_dir="${ARTIFACTS:-/tmp/artifacts}"
  local ns="${TARGET_NAMESPACE:-${NAMESPACE:-kubeagents-system}}"
  local log="${artifact_dir}/${LITELLM_LOG_ARTIFACT}"
  local kctl=(kubectl)
  if [ -n "${AGENT_CLUSTER_CONTEXT:-}" ]; then
    kctl+=(--context "${AGENT_CLUSTER_CONTEXT}")
  fi
  mkdir -p "${artifact_dir}" || true
  local found=""
  if ! found=$("${kctl[@]}" get "${LITELLM_LOG_DEPLOYMENT}" -n "${ns}" -o name \
    --request-timeout="${LITELLM_LOG_REQUEST_TIMEOUT}" 2>&1); then
    echo "note: no LiteLLM log for this run: ${LITELLM_LOG_DEPLOYMENT} in ${ns} could not be read: ${found//$'\n'/ }" \
      | tee "${log}" || true
    return 0
  fi
  "${kctl[@]}" logs -l "${LITELLM_LOG_POD_SELECTOR}" -c "${LITELLM_LOG_CONTAINER}" -n "${ns}" \
    --prefix --ignore-errors --tail="${LITELLM_LOG_TAIL_LINES}" 2>&1 \
    | last_bytes "${LITELLM_LOG_MAX_BYTES}" "${log}" || true
}

# ─── Credential-proxy evidence (runs on PASS as well as on failure) ──────────
# The CREDENTIAL_PROXY_ constants above say what and why. Same contract as
# collect_gateway_log: every command ends in `|| true`, so `$?` is left for
# the dumper; pinned to AGENT_CLUSTER_CONTEXT for the same reason; once per
# process, so the eval's capture is the one kept when the trap or the failure
# dumper calls again after a round trip replaced the pod. An install without
# the Deployment is a note, not a failure: the note is the log artifact's
# content, so the file says why it holds nothing, and no scrape is attempted.
CREDENTIAL_PROXY_EVIDENCE_COLLECTED=""
collect_credential_proxy_evidence() {
  [ -z "${CREDENTIAL_PROXY_EVIDENCE_COLLECTED:-}" ] || return 0
  CREDENTIAL_PROXY_EVIDENCE_COLLECTED=1
  local artifact_dir="${ARTIFACTS:-/tmp/artifacts}"
  local ns="${TARGET_NAMESPACE:-${NAMESPACE:-kubeagents-system}}"
  local log="${artifact_dir}/${CREDENTIAL_PROXY_LOG_ARTIFACT}"
  local metrics="${artifact_dir}/${CREDENTIAL_PROXY_METRICS_ARTIFACT}"
  local kctl=(kubectl)
  if [ -n "${AGENT_CLUSTER_CONTEXT:-}" ]; then
    kctl+=(--context "${AGENT_CLUSTER_CONTEXT}")
  fi
  mkdir -p "${artifact_dir}" || true
  local found=""
  if ! found=$("${kctl[@]}" get "${CREDENTIAL_PROXY_DEPLOYMENT}" -n "${ns}" -o name \
    --request-timeout="${CREDENTIAL_PROXY_REQUEST_TIMEOUT}" 2>&1); then
    echo "note: no credential-proxy evidence for this run: ${CREDENTIAL_PROXY_DEPLOYMENT} in ${ns} could not be read: ${found//$'\n'/ }" \
      | tee "${log}" || true
    return 0
  fi
  "${kctl[@]}" logs "${CREDENTIAL_PROXY_DEPLOYMENT}" -c "${CREDENTIAL_PROXY_CONTAINER}" -n "${ns}" \
    --tail="${CREDENTIAL_PROXY_LOG_TAIL_LINES}" 2>&1 \
    | last_bytes "${CREDENTIAL_PROXY_LOG_MAX_BYTES}" "${log}" || true
  local bound=(timeout --foreground "${CREDENTIAL_PROXY_SCRAPE_BOUND_SECONDS}")
  command -v timeout >/dev/null 2>&1 || bound=()
  local scrape_status=0
  ${bound[@]+"${bound[@]}"} "${kctl[@]}" exec "${CREDENTIAL_PROXY_DEPLOYMENT}" -c "${CREDENTIAL_PROXY_CONTAINER}" -n "${ns}" -- \
    python3 -c "${CREDENTIAL_PROXY_METRICS_SCRAPE}" > "${metrics}" 2>&1 || scrape_status=$?
  # A scrape that did not complete leaves whatever reached the file, often
  # nothing; the note says so, and names the bound when that is what ended it.
  if [ "${scrape_status}" -ne 0 ]; then
    local why="exited ${scrape_status}"
    [ "${scrape_status}" -eq "${CREDENTIAL_PROXY_SCRAPE_TIMEOUT_STATUS}" ] && why="timed out after ${CREDENTIAL_PROXY_SCRAPE_BOUND_SECONDS}s"
    echo "note: the metrics scrape ${why}; the series above, if any, are what reached this file before that" >> "${metrics}" || true
  fi
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
#
# `--keep-watch` snapshots and leaves the watch running, for a caller that
# replaces pods after it (ci-eval-pr.sh's rollback round trip). That caller
# then clears AGENT_DIAG_COLLECTED and sets AGENT_DIAG_PREFIX, so the trap's
# call stops the watch, whose tails then cover the replacement too, and
# writes a second snapshot under the prefix beside the first.
AGENT_DIAG_COLLECTED=""
AGENT_DIAG_PREFIX=""
collect_agent_pod_diagnostics() {
  [ -z "${AGENT_DIAG_COLLECTED}" ] || return 0
  AGENT_DIAG_COLLECTED=1
  local prefix="${AGENT_DIAG_PREFIX:-}"
  local artifact_dir="${ARTIFACTS:-/tmp/artifacts}"
  local ns="${TARGET_NAMESPACE:-${NAMESPACE:-kubeagents-system}}"
  local kctl=(kubectl --request-timeout="${AGENT_DIAG_REQUEST_TIMEOUT}")
  if [ -n "${AGENT_CLUSTER_CONTEXT:-}" ]; then
    kctl+=(--context "${AGENT_CLUSTER_CONTEXT}")
  fi
  mkdir -p "${artifact_dir}" || true
  if [ "${1:-}" != "--keep-watch" ]; then
    _stop_agent_pod_watch "${artifact_dir}"
  fi

  "${kctl[@]}" get pods -n "${ns}" -o jsonpath="${AGENT_DIAG_POD_STATUS_JSONPATH}" \
    > "${artifact_dir}/${prefix}agent-pod-status.txt" 2>&1 || true
  "${kctl[@]}" get events -n "${ns}" --sort-by=.lastTimestamp -o wide 2>&1 \
    | tail -n "${AGENT_DIAG_EVENTS_TAIL_LINES}" > "${artifact_dir}/${prefix}k8s-events.txt" || true
  "${kctl[@]}" top pods -n "${ns}" --containers > "${artifact_dir}/${prefix}agent-pod-top.txt" 2>&1 || true

  "${kctl[@]}" logs "${AGENT_DIAG_DEPLOYMENT}" -c "${AGENT_DIAG_AGENT_CONTAINER}" -n "${ns}" \
    --previous --tail="${AGENT_DIAG_LOG_TAIL_LINES}" 2>&1 \
    | last_bytes "${AGENT_DIAG_LOG_MAX_BYTES}" "${artifact_dir}/${prefix}platform-agent-previous.log" || true

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
        | last_bytes "${AGENT_DIAG_LOG_MAX_BYTES}" "${artifact_dir}/${prefix}hermes-bridge.log" || true
      "${kctl[@]}" logs "${AGENT_DIAG_DEPLOYMENT}" -c "${AGENT_DIAG_BRIDGE_CONTAINER}" -n "${ns}" \
        --previous --tail="${AGENT_DIAG_LOG_TAIL_LINES}" 2>&1 \
        | last_bytes "${AGENT_DIAG_LOG_MAX_BYTES}" "${artifact_dir}/${prefix}hermes-bridge-previous.log" || true
      ;;
  esac

  collect_a2a_component_logs "${artifact_dir}" "${prefix}" "${ns}" "${kctl[@]}"
}

# ─── A2A Component Logs ──────────────────────────────────────────────────────
# The A2A_DIAG_ constants above say what and why. Called by
# collect_agent_pod_diagnostics, so it inherits that function's once-per-run
# guard, its artifact prefix and its context pin; `$4...` is that function's
# kubectl command line. Same contract: every command ends in `|| true`. A
# today-mode install has no pods under either selector and gains no log files.
# The pod list goes to a2a-component-pods.txt, so a list that failed (an
# unreachable cluster) is on record rather than looking like no A2A stack.
#
# `--previous` only for a pod with a restarted container. `--all-containers`
# with `--ignore-errors`: without it kubectl stops at the first container of a
# multi-container pod whose read fails (no previous instance, or not started)
# and never reads the rest; with it, outside --follow, kubectl prints the
# error and goes on to the next container. `--prefix` says which container
# each line came from.
collect_a2a_component_logs() {
  local artifact_dir="$1" prefix="$2" ns="$3"
  shift 3
  local kctl=("$@")
  local listing="${artifact_dir}/${prefix}a2a-component-pods.txt"
  {
    "${kctl[@]}" get pods -n "${ns}" -l "${A2A_DIAG_COMPONENT_LABEL}" \
      -o jsonpath="${A2A_DIAG_PODS_JSONPATH}" 2>&1 || true
    "${kctl[@]}" get pods -n "${ns}" -l "${A2A_DIAG_SESSION_SELECTOR}" \
      -o jsonpath="${A2A_DIAG_PODS_JSONPATH}" 2>&1 || true
  } > "${listing}" || true

  local line rest pod component restarts base read_pods=0
  while IFS= read -r line; do
    # Only pod lines: an error kubectl wrote into the listing has no tab.
    [[ "${line}" == *$'\t'* ]] || continue
    # Split by hand: tab is IFS whitespace to `read`, which would merge the
    # empty component field of a session pod into its neighbour.
    pod="${line%%$'\t'*}"
    rest="${line#*$'\t'}"
    component="${rest%%$'\t'*}"
    restarts="${rest#*$'\t'}"
    [ -n "${pod}" ] || continue
    if (( read_pods >= A2A_DIAG_MAX_PODS )); then
      echo "truncated: read the first ${A2A_DIAG_MAX_PODS} pods" >> "${listing}" || true
      break
    fi
    read_pods=$((read_pods + 1))
    component="${component:-${A2A_DIAG_SESSION_COMPONENT}}"
    # Names come from the API, which already restricts them; this keeps a
    # path separator out of a file name regardless.
    base="a2a-${component//[^A-Za-z0-9._-]/_}-${pod//[^A-Za-z0-9._-]/_}"
    "${kctl[@]}" logs "pod/${pod}" -n "${ns}" --all-containers --prefix --ignore-errors \
      --tail="${A2A_DIAG_LOG_TAIL_LINES}" 2>&1 \
      | last_bytes "${A2A_DIAG_LOG_MAX_BYTES}" "${artifact_dir}/${prefix}${base}.log" || true
    case "${restarts}" in
      *[1-9]*)
        "${kctl[@]}" logs "pod/${pod}" -n "${ns}" --all-containers --prefix --ignore-errors \
          --previous --tail="${A2A_DIAG_LOG_TAIL_LINES}" 2>&1 \
          | last_bytes "${A2A_DIAG_LOG_MAX_BYTES}" "${artifact_dir}/${prefix}${base}-previous.log" || true
        ;;
    esac
  done < <(cat "${listing}" 2>/dev/null || true)
  return 0
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
    #    The running pod's log is the every-run capture above, taken here for a
    #    caller without a green-path collector (ci-deploy.sh); in a process that
    #    already took it (ci-eval-pr.sh) the collector keeps that first capture.
    #    The previous agent container (platform-agent-previous.log), the
    #    bridge sidecar, pod restarts and events come from the every-run
    #    collector, called here for the same reason, with its bounds.
    collect_gateway_log
    collect_agent_pod_diagnostics
    kubectl logs deployment/kube-agents-controller-manager -n "${ns}" --tail=1000 > "${artifact_dir}/controller-manager.log" 2>&1 || true
    # The model path runs through LiteLLM, and with vertex_ai its failure
    # domain (Workload Identity token fetch, aiplatform 403s, model 404s)
    # is visible only in its pods' log -- the gateway just relays the text.
    # The every-run collector, called here for the same reason as the two
    # above; a process that already took it keeps that capture.
    collect_litellm_log
    # The gateway capture above reads the pod's default container (platform-agent);
    # a dropped port-forward stream is only visible from the auth sidecar's side.
    kubectl logs deployment/platform-agent-gateway -c agent-api-auth -n "${ns}" --tail=2000 > "${artifact_dir}/agent-api-auth.log" 2>&1 || true
    # The broker's log and one metrics scrape: the every-run collector, called
    # here for the same reason as the three above; a process that already
    # took them keeps that capture.
    collect_credential_proxy_evidence
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
