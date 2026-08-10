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
  (
    delay="${WATCHER_RETRY_MIN_SECONDS}"
    consecutive=0
    while true; do
      started=$SECONDS
      /usr/local/bin/k8s-event-watcher \
        --cluster-name="${EVENT_WATCHER_CLUSTER_NAME:-}" \
        --profiles-dir="${CREDENTIAL_PROXY_WORKSPACE_ROOT:-/opt/data}/profiles" \
        --in-cluster \
        --daemon-url=http://127.0.0.1:8699 \
        --token-env=API_SERVER_KEY \
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
