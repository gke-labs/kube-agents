#!/usr/bin/env bash
# tests/scale/kwok/baseline.sh -- measure the stock watcher and proxy against a KWOK cluster.
#
# Builds k8s-event-watcher and k8s-api-proxy from this checkout and runs each
# against an existing, seeded KWOK cluster (kwok-cluster.sh up NAME, then
# k8s-operator/cmd/kwok-seed). Every list runs twice, straight at the kwok API
# server with the admin kubeconfig and through the proxy, so the proxy's
# overhead is the difference. The result is one JSON record under
# BASELINE_OUT_DIR, next to the proxy and watcher logs, plus a Markdown summary
# on stdout. It measures and never grades: the run fails only when a step could
# not run (a binary did not build, the proxy never became healthy, a CLI read
# exited non-zero). A watcher that exits before its informer syncs is
# recorded, not fatal.
#
# Usage:
#   tests/scale/kwok/baseline.sh NAME PROFILE
#
#   NAME     the kwok cluster; kwok-cluster.sh kubeconfig NAME must resolve
#   PROFILE  a name under profiles/ (smoke, large-cluster) or the path of a profile JSON
#
# Environment:
#   BASELINE_OUT_DIR               default: tests/scale/kwok/results (gitignored)
#   BASELINE_BIN_DIR               default: $KWOK_STATE_DIR/bin
#   BASELINE_SKIP_BUILD            set to anything to reuse the binaries in BASELINE_BIN_DIR
#   BASELINE_WATCHER_TIMEOUT       default: 15m; how long to wait for the watcher's informer sync
#   BASELINE_READ_TIMEOUT          default: 30m; a CLI read that outlasts it is recorded as exit 124
#   BASELINE_PROXY_PORT            default: 18443
#   BASELINE_PROXY_METRICS_PORT    default: 18444
#   BASELINE_WATCHER_METRICS_PORT  default: 19095
#   BASELINE_SEED_SUMMARY          path of the seeder's --summary-out JSON to embed (optional)
#   KWOK_WORKDIR, KWOK_STATE_DIR, KWOK_TOOLS_DIR   as in kwok-cluster.sh
set -euo pipefail

readonly SCHEMA_VERSION=1
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
readonly REPO_ROOT
readonly CLUSTER_SCRIPT="$SCRIPT_DIR/kwok-cluster.sh"
readonly PROFILES_DIR="$SCRIPT_DIR/profiles"
readonly PROFILE_FIELDS="nodes pods namespaces services events"
readonly WATCHER_MODULE_DIR="$REPO_ROOT/k8s-operator"
readonly WATCHER_PACKAGE="./cmd/k8s-event-watcher/"
readonly WATCHER_BINARY="k8s-event-watcher"
readonly PROXY_MODULE_DIR="$REPO_ROOT/k8s-api-proxy"
readonly PROXY_PACKAGE="./cmd/k8s-api-proxy/"
readonly PROXY_BINARY="k8s-api-proxy"
readonly KWOK_WORKDIR="${KWOK_WORKDIR:-$HOME/.kwok}"
readonly KWOK_STATE_DIR="${KWOK_STATE_DIR:-$KWOK_WORKDIR/kube-agents}"
readonly KWOK_TOOLS_DIR="${KWOK_TOOLS_DIR:-$KWOK_STATE_DIR/bin}"
readonly BASELINE_OUT_DIR="${BASELINE_OUT_DIR:-$SCRIPT_DIR/results}"
readonly BASELINE_BIN_DIR="${BASELINE_BIN_DIR:-$KWOK_STATE_DIR/bin}"
readonly BASELINE_SKIP_BUILD="${BASELINE_SKIP_BUILD:-}"
readonly BASELINE_WATCHER_TIMEOUT="${BASELINE_WATCHER_TIMEOUT:-15m}"
# A stalled read (the proxy holding a stream open, say) is what the lane
# exists to see, so it ends as a recorded exit 124 rather than as the job's
# own timeout, which would take the direct-path numbers down with it.
readonly BASELINE_READ_TIMEOUT="${BASELINE_READ_TIMEOUT:-30m}"
# One unit only (15m, 900s, 2h); duration_seconds has no parser for 1h30m.
readonly DURATION_PATTERN='^[0-9]+[smh]?$'
readonly BASELINE_PROXY_PORT="${BASELINE_PROXY_PORT:-18443}"
readonly BASELINE_PROXY_METRICS_PORT="${BASELINE_PROXY_METRICS_PORT:-18444}"
readonly BASELINE_WATCHER_METRICS_PORT="${BASELINE_WATCHER_METRICS_PORT:-19095}"
readonly BASELINE_SEED_SUMMARY="${BASELINE_SEED_SUMMARY:-}"
readonly LOOPBACK="127.0.0.1"
# Where kwokctl's binary runtime keeps the API server's pid, under the cluster
# directory kwok-cluster.sh dir NAME prints.
readonly APISERVER_PID_FILE_SUFFIX="pids/kube-apiserver.pid"
readonly APISERVER_BINARY_SUFFIX="bin/kube-apiserver"
# The proxy's two identities, named as the install names them. The proxy reads
# with `view` plus a nodes role (`view` has no cluster-scoped resources; the
# install's role covers nodes) and authenticates callers through TokenReview,
# which needs system:auth-delegator.
readonly RBAC_NAMESPACE="default"
readonly PROXY_SA="kube-proxy"
readonly CALLER_SA="scale-caller"
readonly PROXY_VIEW_BINDING="kube-proxy-view"
readonly PROXY_TOKENREVIEW_BINDING="kube-proxy-tokenreview"
readonly PROXY_NODES_ROLE="kube-proxy-nodes-read"
readonly PROXY_NODES_BINDING="kube-proxy-nodes-read"
readonly PROXY_NODES_VERBS="get,list,watch"
readonly PROXY_NODES_RESOURCE="nodes"
readonly VIEW_CLUSTERROLE="view"
readonly TOKENREVIEW_CLUSTERROLE="system:auth-delegator"
readonly PROXY_AUDIENCE="kubeagents-credential-proxy"
readonly PROXY_DEFAULT_CLUSTER="in-cluster"
readonly TOKEN_DURATION="2h"
# client-go sends a bearer token over TLS only, so the proxy serves a
# self-signed loopback certificate, as the operator mints one for the install.
readonly PROXY_CERT_SUBJECT="/CN=kube-proxy-local"
readonly PROXY_CERT_SAN="subjectAltName=IP:127.0.0.1,DNS:localhost"
readonly PROXY_CERT_CURVE="prime256v1"
readonly PROXY_CERT_DAYS=2
readonly PROXY_CONTEXT="proxy"
readonly PROXY_HEALTHZ_PATH="/_kube_proxy/healthz"
readonly PROXY_START_TIMEOUT_SECONDS=60
readonly PROXY_REQUESTS_METRIC="kubeagents_kube_proxy_requests_total"
readonly PROXY_DURATION_METRIC="kubeagents_kube_proxy_request_duration_seconds"
readonly PROXY_AUDIT_MESSAGE="k8s-api-proxy request"
readonly METRICS_PATH="/metrics"
readonly LIST_CHUNK_SIZE=500
# The four reads, production shape first: paged pods, one-body pods (the proxy
# must stream it), paged events, nodes.
readonly LIST_NAMES="pods_paged pods_unpaged events_paged nodes"
declare -A LIST_ARGS=(
  [pods_paged]="get pods -A --chunk-size=$LIST_CHUNK_SIZE -o json"
  [pods_unpaged]="get pods -A --chunk-size=0 -o json"
  [events_paged]="get events -A --chunk-size=$LIST_CHUNK_SIZE -o json"
  [nodes]="get nodes -o json"
)
readonly LIST_ARGS
readonly WARMUP_ARGS="get namespaces -o name"
# The watcher runs as the pod does: one CPU, the pod's memory limit, dry-run so
# nothing is injected, no fleet-shape probing, a dedup window longer than a run.
readonly WATCHER_GOMAXPROCS=1
readonly WATCHER_MEMORY_LIMIT_BYTES=2147483648
readonly WATCHER_DEDUP_WINDOW="24h"
readonly WATCHER_FLEET_SHAPE_INTERVAL=0
# Sync is read from the cluster_up gauge, which the watcher sets to 1 at the
# same moment it logs this line (reload.go); the line is the fallback should
# the metrics listener fail to bind, which the watcher treats as non-fatal.
readonly WATCHER_SYNC_LINE="informer synced, now watching"
readonly WATCHER_CLUSTER_UP_VALUE=1
readonly WATCHER_STEADY_SECONDS=60
readonly WATCHER_EVENTS_SEEN_METRIC="k8s_event_watcher_events_seen_total"
readonly WATCHER_CLUSTER_UP_METRIC="k8s_event_watcher_cluster_up"
readonly APISERVER_REQUESTS_METRIC="apiserver_request_total"
readonly APISERVER_EVENTS_RESOURCE="events"
readonly POLL_SECONDS=1
readonly STOP_TIMEOUT_SECONDS=30
readonly STDERR_KEEP_BYTES=300
readonly SECONDS_PER_MINUTE=60
readonly SECONDS_PER_HOUR=3600
readonly KIB=1024
readonly MIB=1048576
export KWOK_WORKDIR KWOK_STATE_DIR KWOK_TOOLS_DIR
export PATH="$KWOK_TOOLS_DIR:$PATH"

usage() { sed -n '2,/^[^#]/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }
log() { echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
need() {
  local tool
  for tool in "$@"; do command -v "$tool" >/dev/null 2>&1 || die "$tool is not on PATH"; done
}
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
now_float() { date +%s.%N; }
elapsed() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.3f", b - a }'; }
duration_seconds() {
  case "$1" in
    *h) echo $(( ${1%h} * SECONDS_PER_HOUR )) ;;
    *m) echo $(( ${1%m} * SECONDS_PER_MINUTE )) ;;
    *s) echo "${1%s}" ;;
    *) echo "$1" ;;
  esac
}
kc_admin() { kubectl --kubeconfig "$KC" --context "$CTX" "$@"; }

# proc_mem PID FIELD: VmHWM or VmRSS of a live process in bytes, null once it is gone.
proc_mem() {
  local kb=""
  if [ -r "/proc/$1/status" ]; then
    kb="$(awk -v key="$2:" '$1 == key { print $2 }' "/proc/$1/status" 2>/dev/null || true)"
  fi
  if [ -n "$kb" ]; then echo $(( kb * KIB )); else echo null; fi
}
mem_json() {
  jq -n --argjson hwm "$(proc_mem "$1" VmHWM)" --argjson rss "$(proc_mem "$1" VmRSS)" \
    '{vm_hwm_bytes: $hwm, vm_rss_bytes: $rss}'
}

# prom_samples FILE FAMILY: the samples of one metric family (its _sum and
# _count series included, buckets left out) as a JSON array of {metric, labels, value}.
prom_samples() {
  { grep -E "^$2(_sum|_count)?\{" "$1" || true; } | awk '
    {
      open = index($0, "{")
      name = substr($0, 1, open - 1)
      rest = substr($0, open + 1)
      close_at = match(rest, /\} [^ ]+$/)
      labels = substr(rest, 1, close_at - 1)
      value = substr(rest, close_at + 2)
      gsub(/=/, "\":", labels)
      gsub(/,/, ",\"", labels)
      if (labels != "") labels = "\"" labels
      printf "{\"metric\":\"%s\",\"labels\":{%s},\"value\":\"%s\"}\n", name, labels, value
    }' | jq -s 'map(.value as $v | .value = (($v | tonumber?) // $v))'
}

# watcher_cluster_up: the watcher's cluster_up gauge for this cluster from its
# metrics endpoint, empty while the endpoint does not answer.
watcher_cluster_up() {
  { curl -fsS "http://$LOOPBACK:$BASELINE_WATCHER_METRICS_PORT$METRICS_PATH" 2>/dev/null || true; } \
    | awk -v prefix="$WATCHER_CLUSTER_UP_METRIC{" 'index($0, prefix) == 1 { print $NF; exit }'
}

# apiserver_events_requests FILE VERB: the API server's request count for Events
# with that verb, every API group summed.
apiserver_events_requests() {
  awk -v prefix="$APISERVER_REQUESTS_METRIC{" -v res="resource=\"$APISERVER_EVENTS_RESOURCE\"" -v verb="verb=\"$2\"" '
    index($0, prefix) == 1 && index($0, res) > 0 && index($0, verb) > 0 { sum += $NF }
    END { printf "%d", sum }' "$1"
}

# measure LABEL KUBECONFIG CONTEXT ARGS...: one timed CLI read whose output is
# counted, not kept. Prints {seconds, exit_code, bytes, stderr}; a read that
# outlasts BASELINE_READ_TIMEOUT exits 124.
measure() {
  local label="$1" kubeconfig="$2" context="$3"
  shift 3
  local start end rc bytes
  start="$(now_float)"
  set +e
  timeout "$BASELINE_READ_TIMEOUT" kubectl --kubeconfig "$kubeconfig" --context "$context" "$@" 2>"$TMP_DIR/$label.err" | wc -c >"$TMP_DIR/$label.bytes"
  rc="${PIPESTATUS[0]}"
  set -e
  end="$(now_float)"
  bytes="$(tr -d ' ' <"$TMP_DIR/$label.bytes")"
  jq -n --arg seconds "$(elapsed "$start" "$end")" --argjson exit_code "$rc" --argjson bytes "$bytes" \
    --arg stderr "$(head -c "$STDERR_KEEP_BYTES" "$TMP_DIR/$label.err")" \
    '{seconds: ($seconds | tonumber), exit_code: $exit_code, bytes: $bytes, stderr: $stderr}'
}

# stop_process PID: SIGTERM, wait up to STOP_TIMEOUT_SECONDS, then SIGKILL.
# Sets STOPPED_EXIT; it must run in this shell, since only the parent can wait.
STOPPED_EXIT=""
stop_process() {
  local pid="$1"
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 "$STOP_TIMEOUT_SECONDS"); do
      kill -0 "$pid" 2>/dev/null || break
      sleep "$POLL_SECONDS"
    done
    if kill -0 "$pid" 2>/dev/null; then kill -KILL "$pid" 2>/dev/null || true; fi
  fi
  set +e
  wait "$pid"
  STOPPED_EXIT=$?
  set -e
}

apiserver_pid() {
  local pid_file="$CLUSTER_DIR/$APISERVER_PID_FILE_SUFFIX" pid=""
  if [ -f "$pid_file" ]; then pid="$(tr -d '[:space:]' <"$pid_file")"; fi
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    pid="$(pgrep -f "$CLUSTER_DIR/$APISERVER_BINARY_SUFFIX" 2>/dev/null | head -n 1 || true)"
  fi
  echo "$pid"
}

PROXY_PID=""
WATCHER_PID=""
TMP_DIR=""
cleanup() {
  local pid
  for pid in "$PROXY_PID" "$WATCHER_PID"; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  if [ -n "$TMP_DIR" ]; then rm -rf "$TMP_DIR"; fi
  return 0
}
trap cleanup EXIT

# ─── Arguments ───────────────────────────────────────────────────────────────
[ $# -ge 1 ] || { usage >&2; exit 2; }
case "$1" in -h|--help) usage; exit 0 ;; esac
[ $# -eq 2 ] || { usage >&2; exit 2; }
NAME="$1"
if [ -f "$2" ]; then PROFILE_FILE="$2"; else PROFILE_FILE="$PROFILES_DIR/$2.json"; fi
[ -f "$PROFILE_FILE" ] || die "profile $2 is neither a file nor $PROFILES_DIR/$2.json"
PROFILE_NAME="$(basename "$PROFILE_FILE" .json)"

need kubectl jq curl openssl awk date mktemp timeout
[ -n "$BASELINE_SKIP_BUILD" ] || need go
for duration_var in BASELINE_WATCHER_TIMEOUT BASELINE_READ_TIMEOUT; do
  [[ "${!duration_var}" =~ $DURATION_PATTERN ]] \
    || die "$duration_var=${!duration_var} must be a number with at most one unit (s, m or h)"
done
for field in $PROFILE_FIELDS; do
  jq -e --arg f "$field" '.[$f] | type == "number" and . > 0' "$PROFILE_FILE" >/dev/null \
    || die "profile $PROFILE_FILE: $field must be a positive number"
done
PROFILE_JSON="$(jq --arg name "$PROFILE_NAME" '{name: $name} + .' "$PROFILE_FILE")"
SEED_JSON=null
if [ -n "$BASELINE_SEED_SUMMARY" ]; then
  [ -f "$BASELINE_SEED_SUMMARY" ] || die "BASELINE_SEED_SUMMARY=$BASELINE_SEED_SUMMARY does not exist"
  SEED_JSON="$(jq . "$BASELINE_SEED_SUMMARY")"
fi

KC="$("$CLUSTER_SCRIPT" kubeconfig "$NAME")"
CA="$("$CLUSTER_SCRIPT" ca "$NAME")"
SERVER="$("$CLUSTER_SCRIPT" server "$NAME")"
CLUSTER_DIR="$("$CLUSTER_SCRIPT" dir "$NAME")"
# The context is whatever kwokctl wrote into the kubeconfig, asked rather than assumed.
CTX="$(kubectl config current-context --kubeconfig "$KC")"
# The per-cluster files go beside the kubeconfig kwok-cluster.sh resolved, so
# `down NAME` finds them whatever either script's state directory default is.
STATE_DIR="$(dirname "$KC")"

mkdir -p "$BASELINE_OUT_DIR" "$BASELINE_BIN_DIR" "$STATE_DIR"
TMP_DIR="$(mktemp -d)"
STARTED_AT="$(now_iso)"
RUN_STEM="$BASELINE_OUT_DIR/$PROFILE_NAME-$(date -u +%Y%m%dT%H%M%SZ)"
RESULT_FILE="$RUN_STEM.json"
PROXY_LOG="$RUN_STEM-proxy.log"
WATCHER_LOG="$RUN_STEM-watcher.log"
WATCHER_BIN="$BASELINE_BIN_DIR/$WATCHER_BINARY"
PROXY_BIN="$BASELINE_BIN_DIR/$PROXY_BINARY"
# NAME.<what>: kwok-cluster.sh down removes NAME.* and cluster names carry no dot.
PROXY_TOKEN_FILE="$STATE_DIR/$NAME.proxy.token"
CALLER_TOKEN_FILE="$STATE_DIR/$NAME.caller.token"
PROXY_CERT="$STATE_DIR/$NAME.proxy.crt"
PROXY_KEY="$STATE_DIR/$NAME.proxy.key"
CALLER_KUBECONFIG="$STATE_DIR/$NAME.caller.kubeconfig"

# ─── 1. Build ────────────────────────────────────────────────────────────────
if [ -n "$BASELINE_SKIP_BUILD" ]; then
  { [ -x "$WATCHER_BIN" ] && [ -x "$PROXY_BIN" ]; } || die "BASELINE_SKIP_BUILD is set but $WATCHER_BIN or $PROXY_BIN is missing"
  log "reusing $WATCHER_BIN and $PROXY_BIN"
else
  log "building $WATCHER_BINARY into $BASELINE_BIN_DIR"
  (cd "$WATCHER_MODULE_DIR" && CGO_ENABLED=0 go build -o "$WATCHER_BIN" "$WATCHER_PACKAGE") || die "$WATCHER_BINARY did not build"
  log "building $PROXY_BINARY into $BASELINE_BIN_DIR"
  (cd "$PROXY_MODULE_DIR" && CGO_ENABLED=0 go build -o "$PROXY_BIN" "$PROXY_PACKAGE") || die "$PROXY_BINARY did not build"
fi

# ─── 2. Cluster facts ────────────────────────────────────────────────────────
log "cluster $NAME at $SERVER: counting objects"
count_objects() { kc_admin get "$@" --chunk-size="$LIST_CHUNK_SIZE" -o name | wc -l | tr -d ' '; }
NODE_COUNT="$(count_objects nodes)"
POD_COUNT="$(count_objects pods -A)"
SERVICE_COUNT="$(count_objects services -A)"
EVENT_COUNT="$(count_objects events -A)"
NAMESPACE_COUNT="$(count_objects namespaces)"
APISERVER_PID="$(apiserver_pid)"
if [ -n "$APISERVER_PID" ]; then
  APISERVER_JSON="$(jq -n --argjson pid "$APISERVER_PID" --argjson mem "$(mem_json "$APISERVER_PID")" '{pid: $pid} + $mem')"
else
  APISERVER_JSON='{"pid": null, "vm_hwm_bytes": null, "vm_rss_bytes": null}'
fi
CLI_VERSION="$(kubectl version --client -o json | jq -r '.clientVersion.gitVersion')"
KUBE_VERSION="$(kc_admin version -o json | jq -r '.serverVersion.gitVersion')"
KWOK_VERSION_SEEN="$({ kwokctl --version 2>/dev/null || true; } | awk '{ print $3 }')"
GO_VERSION="$({ go version 2>/dev/null || true; } | awk '{ print $3 }')"
HOST_JSON="$(jq -n --argjson nproc "$(nproc)" --argjson mem "$(( $(awk '/^MemTotal:/ { print $2 }' /proc/meminfo) * KIB ))" \
  '{nproc: $nproc, mem_total_bytes: $mem}')"
log "$NODE_COUNT nodes, $POD_COUNT pods, $SERVICE_COUNT services, $EVENT_COUNT events, $NAMESPACE_COUNT namespaces; API server pid ${APISERVER_PID:-unknown}"

# ─── 3. Proxy baseline ───────────────────────────────────────────────────────
log "proxy identities in namespace $RBAC_NAMESPACE"
kc_admin create serviceaccount "$PROXY_SA" -n "$RBAC_NAMESPACE" --dry-run=client -o yaml | kc_admin apply -f -
kc_admin create serviceaccount "$CALLER_SA" -n "$RBAC_NAMESPACE" --dry-run=client -o yaml | kc_admin apply -f -
kc_admin create clusterrolebinding "$PROXY_VIEW_BINDING" --clusterrole="$VIEW_CLUSTERROLE" \
  --serviceaccount="$RBAC_NAMESPACE:$PROXY_SA" --dry-run=client -o yaml | kc_admin apply -f -
kc_admin create clusterrolebinding "$PROXY_TOKENREVIEW_BINDING" --clusterrole="$TOKENREVIEW_CLUSTERROLE" \
  --serviceaccount="$RBAC_NAMESPACE:$PROXY_SA" --dry-run=client -o yaml | kc_admin apply -f -
kc_admin create clusterrole "$PROXY_NODES_ROLE" --verb="$PROXY_NODES_VERBS" --resource="$PROXY_NODES_RESOURCE" \
  --dry-run=client -o yaml | kc_admin apply -f -
kc_admin create clusterrolebinding "$PROXY_NODES_BINDING" --clusterrole="$PROXY_NODES_ROLE" \
  --serviceaccount="$RBAC_NAMESPACE:$PROXY_SA" --dry-run=client -o yaml | kc_admin apply -f -
(
  umask 077
  kc_admin create token "$PROXY_SA" -n "$RBAC_NAMESPACE" --duration "$TOKEN_DURATION" >"$PROXY_TOKEN_FILE"
  kc_admin create token "$CALLER_SA" -n "$RBAC_NAMESPACE" --audience "$PROXY_AUDIENCE" --duration "$TOKEN_DURATION" >"$CALLER_TOKEN_FILE"
  openssl req -x509 -newkey ec -pkeyopt "ec_paramgen_curve:$PROXY_CERT_CURVE" -nodes -days "$PROXY_CERT_DAYS" \
    -subj "$PROXY_CERT_SUBJECT" -addext "$PROXY_CERT_SAN" -keyout "$PROXY_KEY" -out "$PROXY_CERT" 2>/dev/null
)
cat >"$CALLER_KUBECONFIG" <<EOF
apiVersion: v1
kind: Config
clusters:
- name: $PROXY_CONTEXT
  cluster:
    server: https://$LOOPBACK:$BASELINE_PROXY_PORT
    certificate-authority: $PROXY_CERT
contexts:
- name: $PROXY_CONTEXT
  context: {cluster: $PROXY_CONTEXT, user: $CALLER_SA}
current-context: $PROXY_CONTEXT
users:
- name: $CALLER_SA
  user:
    tokenFile: $CALLER_TOKEN_FILE
EOF

log "starting $PROXY_BINARY on $LOOPBACK:$BASELINE_PROXY_PORT (metrics port $BASELINE_PROXY_METRICS_PORT, log $PROXY_LOG)"
OTEL_SDK_DISABLED=true "$PROXY_BIN" \
  --listen-addr="$LOOPBACK:$BASELINE_PROXY_PORT" --metrics-port="$BASELINE_PROXY_METRICS_PORT" \
  --tls-cert-file="$PROXY_CERT" --tls-key-file="$PROXY_KEY" \
  --default-cluster="$PROXY_DEFAULT_CLUSTER" \
  --kube-api-server="$SERVER" --kube-ca-file="$CA" --kube-token-file="$PROXY_TOKEN_FILE" \
  --audience="$PROXY_AUDIENCE" --allowed-callers="system:serviceaccount:$RBAC_NAMESPACE:$CALLER_SA" \
  >"$PROXY_LOG" 2>&1 &
PROXY_PID=$!
PROXY_HEALTHZ_URL="https://$LOOPBACK:$BASELINE_PROXY_PORT$PROXY_HEALTHZ_PATH"
for _ in $(seq 1 "$PROXY_START_TIMEOUT_SECONDS"); do
  curl -fsS --cacert "$PROXY_CERT" -o /dev/null "$PROXY_HEALTHZ_URL" 2>/dev/null && break
  kill -0 "$PROXY_PID" 2>/dev/null || break
  sleep "$POLL_SECONDS"
done
curl -fsS --cacert "$PROXY_CERT" -o /dev/null "$PROXY_HEALTHZ_URL" \
  || die "$PROXY_BINARY did not answer $PROXY_HEALTHZ_URL within ${PROXY_START_TIMEOUT_SECONDS}s; see $PROXY_LOG"

# Warm the CLI's discovery cache on both paths so the first list does not pay for it.
read -ra warmup_args <<<"$WARMUP_ARGS"
kc_admin "${warmup_args[@]}" >/dev/null
kubectl --kubeconfig "$CALLER_KUBECONFIG" --context "$PROXY_CONTEXT" "${warmup_args[@]}" >/dev/null \
  || die "the CLI cannot read through the proxy as $CALLER_SA; see $PROXY_LOG"

LISTS_JSON="[]"
for list_name in $LIST_NAMES; do
  read -ra list_args <<<"${LIST_ARGS[$list_name]}"
  log "measuring '${list_args[*]}' direct, then via the proxy"
  direct_json="$(measure "$list_name-direct" "$KC" "$CTX" "${list_args[@]}")"
  proxy_json="$(measure "$list_name-proxy" "$CALLER_KUBECONFIG" "$PROXY_CONTEXT" "${list_args[@]}")"
  LISTS_JSON="$(jq -n --argjson acc "$LISTS_JSON" --arg name "$list_name" --arg args "${list_args[*]}" \
    --argjson direct "$direct_json" --argjson proxy "$proxy_json" \
    '$acc + [{name: $name, args: $args, direct: $direct, proxy: $proxy}]')"
done
PROXY_MEM_JSON="$(mem_json "$PROXY_PID")"
curl -fsS "http://$LOOPBACK:$BASELINE_PROXY_METRICS_PORT$METRICS_PATH" >"$TMP_DIR/proxy.metrics" 2>/dev/null || : >"$TMP_DIR/proxy.metrics"
PROXY_REQUESTS_JSON="$(prom_samples "$TMP_DIR/proxy.metrics" "$PROXY_REQUESTS_METRIC")"
PROXY_DURATION_JSON="$(prom_samples "$TMP_DIR/proxy.metrics" "$PROXY_DURATION_METRIC")"
# The metric names are copied from the proxy's Go source. Reads went through
# the proxy a moment ago, so an empty family is a renamed metric (or a dead
# listener), which is a step that could not run, not a measurement.
[ "$(jq 'length' <<<"$PROXY_REQUESTS_JSON")" -gt 0 ] \
  || die "the proxy's metrics carry no $PROXY_REQUESTS_METRIC series after the reads; the name in this script no longer matches the proxy's"
stop_process "$PROXY_PID"
PROXY_PID=""
PROXY_EXIT="$STOPPED_EXIT"
PROXY_AUDIT_JSON="$(jq -c -R -n --arg msg "$PROXY_AUDIT_MESSAGE" \
  '[inputs | fromjson? | select(.msg == $msg) | (.outcome // "unknown")] | group_by(.) | map({key: .[0], value: length}) | from_entries' \
  "$PROXY_LOG")"
log "proxy stopped (exit $PROXY_EXIT); audit outcomes $PROXY_AUDIT_JSON"

# ─── 4. Watcher baseline ─────────────────────────────────────────────────────
kc_admin get --raw "$METRICS_PATH" >"$TMP_DIR/apiserver-before.metrics"
EVENTS_LIST_BEFORE="$(apiserver_events_requests "$TMP_DIR/apiserver-before.metrics" LIST)"
EVENTS_WATCH_BEFORE="$(apiserver_events_requests "$TMP_DIR/apiserver-before.metrics" WATCH)"
WATCHER_TIMEOUT_SECONDS="$(duration_seconds "$BASELINE_WATCHER_TIMEOUT")"
log "starting $WATCHER_BINARY (metrics $LOOPBACK:$BASELINE_WATCHER_METRICS_PORT, log $WATCHER_LOG); waiting up to ${WATCHER_TIMEOUT_SECONDS}s for $WATCHER_CLUSTER_UP_METRIC = $WATCHER_CLUSTER_UP_VALUE"
watcher_start="$(now_float)"
GOMAXPROCS="$WATCHER_GOMAXPROCS" EVENT_WATCHER_MEMORY_LIMIT_BYTES="$WATCHER_MEMORY_LIMIT_BYTES" "$WATCHER_BIN" \
  --kubeconfig "$KC" --cluster-name "$NAME" --dry-run \
  --metrics-addr "$LOOPBACK:$BASELINE_WATCHER_METRICS_PORT" \
  --fleet-shape-interval "$WATCHER_FLEET_SHAPE_INTERVAL" --dedup-window "$WATCHER_DEDUP_WINDOW" \
  >"$WATCHER_LOG" 2>&1 &
WATCHER_PID=$!
watcher_deadline=$(( $(date +%s) + WATCHER_TIMEOUT_SECONDS ))
WATCHER_SYNCED=false
WATCHER_EXITED_EARLY=false
WATCHER_TIMED_OUT=false
while :; do
  if [ "$(watcher_cluster_up)" = "$WATCHER_CLUSTER_UP_VALUE" ] || grep -q "$WATCHER_SYNC_LINE" "$WATCHER_LOG"; then
    WATCHER_SYNCED=true
    break
  fi
  if ! kill -0 "$WATCHER_PID" 2>/dev/null; then WATCHER_EXITED_EARLY=true; break; fi
  if [ "$(date +%s)" -ge "$watcher_deadline" ]; then WATCHER_TIMED_OUT=true; break; fi
  sleep "$POLL_SECONDS"
done
WATCHER_SYNC_SECONDS="$(elapsed "$watcher_start" "$(now_float)")"
WATCHER_AT_SYNC_JSON="$(mem_json "$WATCHER_PID")"
if [ "$WATCHER_SYNCED" = true ]; then
  log "watcher synced after ${WATCHER_SYNC_SECONDS}s; holding ${WATCHER_STEADY_SECONDS}s of steady state"
  sleep "$WATCHER_STEADY_SECONDS"
else
  log "watcher did not sync (exited early: $WATCHER_EXITED_EARLY, timed out: $WATCHER_TIMED_OUT); recorded, not fatal"
fi
WATCHER_STEADY_JSON="$(mem_json "$WATCHER_PID")"
curl -fsS "http://$LOOPBACK:$BASELINE_WATCHER_METRICS_PORT$METRICS_PATH" >"$TMP_DIR/watcher.metrics" 2>/dev/null || : >"$TMP_DIR/watcher.metrics"
WATCHER_SEEN_JSON="$(prom_samples "$TMP_DIR/watcher.metrics" "$WATCHER_EVENTS_SEEN_METRIC" \
  | jq 'map({key: .labels.reason, value: .value}) | from_entries')"
WATCHER_CLUSTER_UP="$(prom_samples "$TMP_DIR/watcher.metrics" "$WATCHER_CLUSTER_UP_METRIC" | jq '.[0].value // null')"
# Same guard as the proxy's: a watcher that synced and answered on its metrics
# port exposes the gauge under the name this script carries, or the name moved.
if [ "$WATCHER_SYNCED" = true ] && [ -s "$TMP_DIR/watcher.metrics" ] && [ "$WATCHER_CLUSTER_UP" = null ]; then
  die "the watcher's metrics carry no $WATCHER_CLUSTER_UP_METRIC series after its sync; the name in this script no longer matches the watcher's"
fi
stop_process "$WATCHER_PID"
WATCHER_PID=""
WATCHER_EXIT="$STOPPED_EXIT"
kc_admin get --raw "$METRICS_PATH" >"$TMP_DIR/apiserver-after.metrics"
EVENTS_LIST_AFTER="$(apiserver_events_requests "$TMP_DIR/apiserver-after.metrics" LIST)"
EVENTS_WATCH_AFTER="$(apiserver_events_requests "$TMP_DIR/apiserver-after.metrics" WATCH)"

# ─── 5. Record ───────────────────────────────────────────────────────────────
FINISHED_AT="$(now_iso)"
GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
jq -n \
  --argjson schema_version "$SCHEMA_VERSION" --arg git_commit "$GIT_COMMIT" --argjson profile "$PROFILE_JSON" \
  --arg kwok "${KWOK_VERSION_SEEN:-unknown}" --arg kube "$KUBE_VERSION" --arg cli "$CLI_VERSION" --arg go "${GO_VERSION:-unknown}" \
  --argjson host "$HOST_JSON" \
  --arg cluster_name "$NAME" --argjson nodes "$NODE_COUNT" --argjson pods "$POD_COUNT" --argjson services "$SERVICE_COUNT" \
  --argjson events "$EVENT_COUNT" --argjson namespaces "$NAMESPACE_COUNT" --argjson apiserver "$APISERVER_JSON" \
  --argjson seed "$SEED_JSON" \
  --argjson proxy_exit "$PROXY_EXIT" --argjson proxy_mem "$PROXY_MEM_JSON" --argjson lists "$LISTS_JSON" \
  --argjson requests "$PROXY_REQUESTS_JSON" --argjson durations "$PROXY_DURATION_JSON" --argjson audit "$PROXY_AUDIT_JSON" \
  --argjson w_synced "$WATCHER_SYNCED" --arg w_sync_seconds "$WATCHER_SYNC_SECONDS" \
  --argjson w_exited_early "$WATCHER_EXITED_EARLY" --argjson w_timed_out "$WATCHER_TIMED_OUT" --argjson w_exit "$WATCHER_EXIT" \
  --argjson w_at_sync "$WATCHER_AT_SYNC_JSON" --argjson w_steady_seconds "$WATCHER_STEADY_SECONDS" --argjson w_steady "$WATCHER_STEADY_JSON" \
  --argjson w_seen "$WATCHER_SEEN_JSON" --argjson w_up "$WATCHER_CLUSTER_UP" \
  --argjson list_before "$EVENTS_LIST_BEFORE" --argjson list_after "$EVENTS_LIST_AFTER" \
  --argjson watch_before "$EVENTS_WATCH_BEFORE" --argjson watch_after "$EVENTS_WATCH_AFTER" \
  --arg started_at "$STARTED_AT" --arg finished_at "$FINISHED_AT" \
  '{
    schema_version: $schema_version,
    git_commit: $git_commit,
    profile: $profile,
    versions: {kwok: $kwok, kube: $kube, cli: $cli, go: $go},
    host: $host,
    cluster: {name: $cluster_name, nodes: $nodes, pods: $pods, services: $services, events: $events, namespaces: $namespaces, apiserver: $apiserver},
    seed: $seed,
    proxy: ({exit_code: $proxy_exit} + $proxy_mem + {lists: $lists, requests_total: $requests, request_duration_seconds: $durations, audit_outcomes: $audit}),
    watcher: {
      synced: $w_synced, sync_seconds: ($w_sync_seconds | tonumber), exited_early: $w_exited_early, timed_out: $w_timed_out, exit_code: $w_exit,
      at_sync: $w_at_sync, steady: ({seconds_after_sync: $w_steady_seconds} + $w_steady),
      events_seen_total: ($w_seen | add // 0), events_seen_by_reason: $w_seen, cluster_up: $w_up,
      apiserver_events_requests: {
        list: {before: $list_before, after: $list_after, delta: ($list_after - $list_before)},
        watch: {before: $watch_before, after: $watch_after, delta: ($watch_after - $watch_before)}
      }
    },
    started_at: $started_at,
    finished_at: $finished_at
  }' >"$RESULT_FILE"

SUMMARY="$(jq -r --arg name "$NAME" --arg file "$RESULT_FILE" --argjson mib "$MIB" '
  def mb: if . == null then "n/a" else (((. / $mib * 10) | round) / 10 | tostring) + " MB" end;
  "## Scale baseline: \(.profile.name) on kwok cluster \($name) (\(.git_commit[0:12]))",
  "",
  "Cluster: \(.cluster.nodes) nodes, \(.cluster.pods) pods, \(.cluster.services) services, \(.cluster.events) events, \(.cluster.namespaces) namespaces; API server RSS \(.cluster.apiserver.vm_rss_bytes | mb), VmHWM \(.cluster.apiserver.vm_hwm_bytes | mb).",
  "",
  "| List | Direct | Via proxy |",
  "| --- | ---: | ---: |",
  (.proxy.lists[] | "| `\(.args)` | \(.direct.seconds) s, \(.direct.bytes | mb), exit \(.direct.exit_code) | \(.proxy.seconds) s, \(.proxy.bytes | mb), exit \(.proxy.exit_code) |"),
  "",
  "Proxy: VmHWM \(.proxy.vm_hwm_bytes | mb), VmRSS \(.proxy.vm_rss_bytes | mb) after the lists; exit \(.proxy.exit_code); audit outcomes \(.proxy.audit_outcomes | tojson).",
  "",
  "Watcher: synced \(.watcher.synced) after \(.watcher.sync_seconds) s (exited early \(.watcher.exited_early), timed out \(.watcher.timed_out), exit \(.watcher.exit_code)); VmHWM \(.watcher.at_sync.vm_hwm_bytes | mb) at sync, \(.watcher.steady.vm_hwm_bytes | mb) after \(.watcher.steady.seconds_after_sync) s; events seen \(.watcher.events_seen_total); cluster_up \(.watcher.cluster_up); API server Events requests LIST +\(.watcher.apiserver_events_requests.list.delta), WATCH +\(.watcher.apiserver_events_requests.watch.delta).",
  "",
  "Record: `\($file)`"
' "$RESULT_FILE")"
echo "$SUMMARY"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then printf '%s\n' "$SUMMARY" >>"$GITHUB_STEP_SUMMARY"; fi

READ_FAILURES="$(jq '[.proxy.lists[] | .direct.exit_code, .proxy.exit_code] | map(select(. != 0)) | length' "$RESULT_FILE")"
[ "$READ_FAILURES" -eq 0 ] || die "$READ_FAILURES list read(s) exited non-zero; the record is $RESULT_FILE"
log "wrote $RESULT_FILE"
