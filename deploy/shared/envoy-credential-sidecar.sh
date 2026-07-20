#!/usr/bin/env bash
set -euo pipefail

runtime_pid=""
envoy_pid=""

terminate() {
  [[ -z "${runtime_pid}" ]] || kill "${runtime_pid}" 2>/dev/null || true
  [[ -z "${envoy_pid}" ]] || kill "${envoy_pid}" 2>/dev/null || true
}
trap terminate EXIT INT TERM

/opt/hermes/.venv/bin/python3 /opt/defaults/scripts/credential_proxy.py &
runtime_pid=$!

/usr/local/bin/envoy --config-path /etc/envoy/envoy-credential-proxy.yaml --log-level info &
envoy_pid=$!

wait -n "${runtime_pid}" "${envoy_pid}"
