#!/usr/bin/env bash
# ==============================================================================
# Port-forward a gateway door to the replica that serves it
# ==============================================================================
# The A2A gateway's inject and A2A doors bind the pod's loopback, so a
# port-forward is the only way in. With more than one gateway replica only the
# holder of the gateway's Lease serves them, and a standby answers every request
# with 503 (docs/designs/gateway-replicas.md, "Reaching the active replica over
# HTTP"). This forwards to the holder's pod and, when the port-forward drops (a
# handover, a rollout, the pod deleted), reads the Lease again and forwards to
# whoever holds it now. With no Lease, an unelected gateway, it forwards the
# door's Service as before.
#
# Usage: hack/gateway-port-forward.sh [door-port] [local-port]
#   door-port   the door's port in the pod (default 8099, the inject door)
#   local-port  the local side (default: the door's port)
#
# Environment:
#   NAMESPACE     the install's namespace (default kubeagents-system)
#   CR_NAME       the PlatformAgent's name (default platform-agent)
#   KUBE_CONTEXT  a kubectl context to pin (default: the current one)
#   DOOR_SERVICE  the Service used with no Lease (default <CR_NAME>-a2a-inject)
#
# Ctrl-C stops it.
set -euo pipefail

readonly DEFAULT_NAMESPACE="kubeagents-system"
readonly DEFAULT_CR_NAME="platform-agent"
readonly DEFAULT_DOOR_PORT="8099"
readonly LEASE_SUFFIX="-a2a-gateway"
readonly DOOR_SERVICE_SUFFIX="-a2a-inject"
# A pause between a dropped port-forward and the next, about the election's
# retry period: a released lease is taken within it.
readonly RESPAWN_DELAY_SECONDS=2

readonly NAMESPACE="${NAMESPACE:-${DEFAULT_NAMESPACE}}"
readonly CR_NAME="${CR_NAME:-${DEFAULT_CR_NAME}}"
readonly DOOR_PORT="${1:-${DEFAULT_DOOR_PORT}}"
readonly LOCAL_PORT="${2:-${DOOR_PORT}}"
readonly LEASE="${CR_NAME}${LEASE_SUFFIX}"
readonly DOOR_SERVICE="${DOOR_SERVICE:-${CR_NAME}${DOOR_SERVICE_SUFFIX}}"

k() {
  if [ -n "${KUBE_CONTEXT:-}" ]; then
    kubectl --context "${KUBE_CONTEXT}" -n "${NAMESPACE}" "$@"
  else
    kubectl -n "${NAMESPACE}" "$@"
  fi
}

# The pod holding the gateway's Lease, or the door's Service when no Lease
# names one.
door_target() {
  local holder
  holder="$(k get lease "${LEASE}" -o jsonpath='{.spec.holderIdentity}' 2>/dev/null || true)"
  if [ -n "${holder}" ]; then
    echo "pod/${holder}"
  else
    echo "svc/${DOOR_SERVICE}"
  fi
}

trap 'exit 0' INT TERM
while true; do
  target="$(door_target)"
  echo "forwarding 127.0.0.1:${LOCAL_PORT} to ${target}:${DOOR_PORT} in ${NAMESPACE}" >&2
  k port-forward "${target}" "${LOCAL_PORT}:${DOOR_PORT}" || true
  echo "the port-forward to ${target} ended; reading the lease again" >&2
  sleep "${RESPAWN_DELAY_SECONDS}"
done
