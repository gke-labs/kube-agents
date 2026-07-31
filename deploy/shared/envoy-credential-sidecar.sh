#!/usr/bin/env bash
set -euo pipefail

runtime_pid=""
envoy_pid=""
watcher_pid=""

terminate() {
  trap - EXIT INT TERM
  # Stop the supervisor before the watcher, so it does not restart it on the way down.
  if [[ -n "${watcher_pid}" ]]; then
    kill "${watcher_pid}" 2>/dev/null || true
    pkill -P "${watcher_pid}" 2>/dev/null || true
  fi
  [[ -z "${runtime_pid}" ]] || kill "${runtime_pid}" 2>/dev/null || true
  [[ -z "${envoy_pid}" ]] || kill "${envoy_pid}" 2>/dev/null || true
}
trap terminate EXIT INT TERM

/opt/hermes/.venv/bin/python3 /opt/defaults/scripts/credential_proxy.py &
runtime_pid=$!

/usr/local/bin/envoy --config-path /etc/envoy/envoy-credential-proxy.yaml --log-level info &
envoy_pid=$!

# Any arguments to this script are the k8s-event-watcher's. It lives here because
# it authenticates to cluster API servers and credentials belong in this container,
# not beside the agent sandbox. It is supervised rather than waited on: event
# watching is best-effort observability, so its failure must not take down Envoy
# or the credential server and cut the agent off from every credentialed command.
if [[ $# -gt 0 ]]; then
  (
    while true; do
      /usr/local/bin/k8s-event-watcher "$@" || true
      echo "envoy-credential-sidecar: k8s-event-watcher exited, restarting in 10s" >&2
      sleep 10
    done
  ) &
  watcher_pid=$!
fi

wait -n "${runtime_pid}" "${envoy_pid}"
