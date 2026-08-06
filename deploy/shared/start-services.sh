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
    while true; do
      /usr/local/bin/k8s-event-watcher \
        --cluster-name="${EVENT_WATCHER_CLUSTER_NAME:-}" \
        --profiles-dir="${CREDENTIAL_PROXY_WORKSPACE_ROOT:-/opt/data}/profiles" \
        --in-cluster \
        --daemon-url=http://127.0.0.1:8699 \
        --token-env=API_SERVER_KEY \
        --owner=platform \
        --reason=Failed,FailedToDrainNode,CrashLoopBackOff,BackOff,ImagePullBackOff,ErrImagePull,OOMKilled || true
      echo "start-services: k8s-event-watcher exited, restarting in 10s" >&2
      sleep 10
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
