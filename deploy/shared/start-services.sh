#!/usr/bin/env bash
# Entrypoint for the credential-proxy container.
#
# Three peer services live here, not one service with helpers. They share a
# container because they all need credentials, and credentials are deliberately
# kept out of the agent sandbox — not because any of them belongs to another:
#
#   credential_proxy.py   executes credentialed CLIs on behalf of the sandbox
#   envoy                 fronts the credential proxy on loopback
#   k8s-event-watcher     watches cluster API servers and reports events
#
# They differ in how their failure is treated. Envoy and the credential runtime
# are the container's reason to exist: if either dies the agent loses every
# credentialed command, so their exit ends the container and Kubernetes
# restarts it. The watcher is best-effort observability — losing it must not
# take the credential path down with it, so it is supervised and restarted in
# place instead.
set -euo pipefail

# Watcher restart policy. The watcher is retried in place rather than being
# allowed to end the container, so these bound how hard a permanently broken
# one is retried and how long it must survive to count as recovered.
WATCHER_RETRY_MIN_SECONDS="${WATCHER_RETRY_MIN_SECONDS:-10}"
WATCHER_RETRY_MAX_SECONDS="${WATCHER_RETRY_MAX_SECONDS:-120}"
WATCHER_HEALTHY_RUN_SECONDS="${WATCHER_HEALTHY_RUN_SECONDS:-120}"

# Where the watcher keeps its dedup snapshots. Without them the cache starts
# empty on every restart, and an empty cache is not a neutral state: the
# informer's initial LIST replays every event still inside the API server's TTL
# (an hour on GKE by default), so a restart re-reports incidents that were
# already triaged. The supervisor below restarts the watcher in place, which
# makes that a routine occurrence rather than a rare one.
#
# The data volume, not the container's own state directory: that one is a 16Mi
# in-memory emptyDir, so it would lose the cache on exactly the pod restarts
# that matter most. The watcher appends the profile name per cluster, since
# each cluster keeps its own cache and they cannot share a file.
WATCHER_DEDUP_DIR="${WATCHER_DEDUP_DIR:-${CREDENTIAL_PROXY_WORKSPACE_ROOT:-/opt/data}/event-watcher}"

# How long a failure stays suppressed after its last sighting. The window
# SLIDES: every fresh observation pushes the deadline out again, so a workload
# that keeps failing is reported once and then stays quiet. The window only
# expires after a genuine gap, and when it does the incident is rebuilt from
# scratch — new session, new chat thread, count back to 1.
#
# That is why the binary's own 5m default is the wrong value here rather than
# merely a conservative one. The kubelet's image-pull and crash-loop backoffs
# both cap at 300s, so a steadily-failing pod re-reports at almost exactly the
# threshold and clears it or misses it on delivery jitter alone. The customer-
# visible result is the same broken image arriving as an unrelated-looking new
# alert every few minutes, with nothing tying the copies together.
#
# 24h is chosen over anything shorter because a broken deploy is not a
# minutes-scale event. An unresolvable image reference, a missing Secret or a
# node that will not come back stays broken until a human acts, and the useful
# alert cadence for "still broken, nobody has fixed it" is daily, not hourly.
# The cost is the other side of the same coin, and it is real: a failure that
# genuinely clears and returns later the same day is folded into the original
# incident instead of opening a new one, and the agent is not woken for it.
# A fleet whose failures resolve and recur within a shift wants a smaller value.
#
# Overridable because the right value depends on the fleet's failure mix, and
# an operator should not have to rebuild the image to find out.
WATCHER_DEDUP_WINDOW="${WATCHER_DEDUP_WINDOW:-24h}"

runtime_pid=""
envoy_pid=""
watcher_pid=""

terminate() {
  trap - EXIT INT TERM
  # The supervisor first, so it does not restart the watcher on the way down.
  if [[ -n "${watcher_pid}" ]]; then
    kill "${watcher_pid}" 2>/dev/null || true
    pkill -P "${watcher_pid}" 2>/dev/null || true
  fi
  [[ -z "${runtime_pid}" ]] || kill "${runtime_pid}" 2>/dev/null || true
  [[ -z "${envoy_pid}" ]] || kill "${envoy_pid}" 2>/dev/null || true
}
trap terminate EXIT INT TERM

start_credential_runtime() {
  /opt/hermes/.venv/bin/python3 /opt/defaults/scripts/credential_proxy.py &
  runtime_pid=$!
}

start_envoy() {
  /usr/local/bin/envoy --config-path /etc/envoy/envoy-credential-proxy.yaml --log-level info &
  envoy_pid=$!
}

start_event_watcher() {
  # Flags are set here rather than passed as container arguments: they describe
  # how processes inside this container reach each other over loopback, which is
  # implementation detail rather than deployment configuration. The one value
  # that varies per install — the cluster's name — comes from the operator via
  # EVENT_WATCHER_CLUSTER_NAME, which it always sets. No default is applied
  # here on purpose: guessing a name would mislabel every payload and metric,
  # so an unset value should fail loudly in the watcher's own validation.
  #
  # --token-env names SESSION_KV_API_KEY rather than API_SERVER_KEY: the latter
  # is the loopback sentinel `cluster-internal-trusted`, which authenticates
  # nothing. The watcher refuses to start when the named variable is empty,
  # which is the behaviour we want — the Session KV server fails closed too.

  # Said once, up front, and in terms of the consequence: the watcher's own
  # error ("bearer token env var ... is empty") names a variable, not what
  # stops working, and it only reaches the ALERT below after three short exits.
  # An install upgraded from before this key existed is exactly the case that
  # lands here — see the backfill in upgrade.sh.
  if [ -z "${SESSION_KV_API_KEY:-}" ]; then
    echo "start-services: ALERT SESSION_KV_API_KEY is empty, so k8s-event-watcher cannot authenticate to the Session KV server and will exit on every start — NO cluster events are being watched. Add the key to the agent Secret (upgrade.sh backfills it; provision_07_gcp_k8s_secrets.sh generates it on a fresh install) and restart the pod." >&2
  fi

  # An empty value disables persistence, which is what should happen if the
  # directory cannot be created: the watcher still dedups in memory, and losing
  # snapshots must not cost us the watcher itself. Creating it here rather than
  # in the watcher keeps the failure at startup, where it is logged once,
  # instead of on every snapshot tick.
  dedup_persist=""
  if mkdir -p "${WATCHER_DEDUP_DIR}" 2>/dev/null; then
    dedup_persist="${WATCHER_DEDUP_DIR}/dedup.json"
  else
    echo "start-services: cannot create ${WATCHER_DEDUP_DIR}; the dedup cache will not survive a watcher restart, so recent incidents may be reported twice" >&2
  fi

  (
    delay="${WATCHER_RETRY_MIN_SECONDS}"
    consecutive=0
    while true; do
      started=$SECONDS
      /usr/local/bin/k8s-event-watcher \
        --cluster-name="${EVENT_WATCHER_CLUSTER_NAME:-}" \
        --profiles-dir="${CREDENTIAL_PROXY_WORKSPACE_ROOT:-/opt/data}/profiles" \
        --dedup-persist="${dedup_persist}" \
        --dedup-window="${WATCHER_DEDUP_WINDOW}" \
        --in-cluster \
        --daemon-url=http://127.0.0.1:8699 \
        --token-env=SESSION_KV_API_KEY \
        --owner=platform \
        --reason=Failed,FailedToDrainNode,CrashLoopBackOff,BackOff,ImagePullBackOff,ErrImagePull,OOMKilled || true
      ran=$(( SECONDS - started ))

      # A run long enough to have synced and served is treated as a fresh
      # start, so an occasional crash after hours of work does not inherit a
      # backoff earned days earlier. Anything shorter is a failure to start.
      if [[ "${ran}" -ge "${WATCHER_HEALTHY_RUN_SECONDS}" ]]; then
        delay="${WATCHER_RETRY_MIN_SECONDS}"
        consecutive=0
      else
        consecutive=$(( consecutive + 1 ))
      fi

      echo "start-services: k8s-event-watcher exited after ${ran}s (consecutive short exits: ${consecutive}); retrying in ${delay}s" >&2
      if [[ "${consecutive}" -ge 3 ]]; then
        # Loud, greppable, and states the consequence rather than the symptom.
        # Nothing else reports this: the container stays Ready by design, so a
        # watcher that can never start is otherwise indistinguishable from a
        # fleet with no incidents.
        echo "start-services: ALERT k8s-event-watcher has failed to start ${consecutive} times in a row — NO cluster events are being watched" >&2
      fi

      sleep "${delay}"
      # Exponential, capped: a permanent failure (bad RBAC, missing profiles
      # directory) should not hammer the API server every 10s forever.
      delay=$(( delay * 2 ))
      [[ "${delay}" -le "${WATCHER_RETRY_MAX_SECONDS}" ]] || delay="${WATCHER_RETRY_MAX_SECONDS}"
    done
  ) &
  watcher_pid=$!
}

start_credential_runtime
start_envoy
start_event_watcher

# Only the two credential-path services are waited on. The watcher is absent
# from this list deliberately — see the header.
wait -n "${runtime_pid}" "${envoy_pid}"
