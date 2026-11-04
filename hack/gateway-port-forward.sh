#!/usr/bin/env bash
# ==============================================================================
# Port-forward a gateway door to the replica that serves it
# ==============================================================================
# The A2A gateway's inject and A2A doors bind the pod's loopback, so a
# port-forward is the only way in. With more than one gateway replica only the
# holder of the gateway's Lease serves them, and a standby answers every request
# with 503 (docs/designs/gateway-replicas.md, "Reaching the active replica over
# HTTP"). This forwards to the holder's pod, reads the Lease every few seconds
# while it runs, and restarts the port-forward on the new holder when the lease
# moves or the port-forward drops. With no Lease, an unelected gateway, it
# forwards the door's Service as before. The holder is used only if it is one
# of the pods the door's Service selects: the door's token travels over the
# tunnel, and a Lease's holder is a string anyone who can write the Lease sets.
#
# Usage: hack/gateway-port-forward.sh [door-port] [local-port]
#   door-port   the door's port in the pod (default 8099, the inject door)
#   local-port  the local side (default: the door's port)
#
# Environment:
#   NAMESPACE     the install's namespace (default kubeagents-system)
#   CR_NAME       the PlatformAgent's name (default platform-agent)
#   KUBE_CONTEXT  a kubectl context to pin (default: the current one)
#   DOOR_SERVICE  the door's Service (default <CR_NAME>-a2a-inject); set it for
#                 the A2A door, whose Service has another name
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
# How often the Lease is read while a port-forward runs. A handover where the
# old leader's pod survives (its container restarts as a standby) leaves the
# port-forward up, so only a read notices.
readonly HOLDER_POLL_SECONDS=5
# A Service's selector as a label selector, with a trailing comma to trim.
# shellcheck disable=SC2016 # a go-template, not a shell expansion
readonly SELECTOR_TEMPLATE='{{range $k, $v := .spec.selector}}{{$k}}={{$v}},{{end}}'

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

# The Lease's holder: "" when there's no Lease or nobody holds it. Fails,
# saying why on stderr, when the read itself fails (a timeout, a Forbidden),
# so the caller can tell that from a lease nobody holds.
read_holder() {
  local out err rc=0
  err="$(mktemp)"
  out="$(k get lease "${LEASE}" -o jsonpath='{.spec.holderIdentity}' 2>"${err}")" || rc=$?
  if [ "${rc}" -ne 0 ] && ! grep -q NotFound "${err}"; then
    echo "reading lease ${LEASE} failed: $(tr '\n' ' ' <"${err}")" >&2
    rm -f "${err}"
    return 1
  fi
  rm -f "${err}"
  [ "${rc}" -eq 0 ] && printf '%s' "${out}"
  return 0
}

# The target for a holder: its pod, if the door's Service selects it, or the
# Service.
target_for() {
  local holder="$1" selector
  if [ -n "${holder}" ]; then
    selector="$(k get svc "${DOOR_SERVICE}" -o "go-template=${SELECTOR_TEMPLATE}" 2>/dev/null || true)"
    selector="${selector%,}"
    if [ -n "${selector}" ] && k get pods -l "${selector}" -o name 2>/dev/null | grep -qxF "pod/${holder}"; then
      echo "pod/${holder}"
      return 0
    fi
    echo "lease ${LEASE} names ${holder}, which is not a pod svc/${DOOR_SERVICE} selects; using the service" >&2
  fi
  echo "svc/${DOOR_SERVICE}"
}

pf_pid=""
trap '[ -z "${pf_pid}" ] || kill "${pf_pid}" 2>/dev/null; exit 0' INT TERM
while true; do
  holder="$(read_holder)" || holder=""
  target="$(target_for "${holder}")"
  echo "forwarding 127.0.0.1:${LOCAL_PORT} to ${target}:${DOOR_PORT} in ${NAMESPACE}" >&2
  # kubectl itself is the background job, not the k function: $! must be
  # the process holding the port, or a kill leaves the forward running.
  pf=(kubectl)
  if [ -n "${KUBE_CONTEXT:-}" ]; then
    pf+=(--context "${KUBE_CONTEXT}")
  fi
  pf+=(-n "${NAMESPACE}" port-forward "${target}" "${LOCAL_PORT}:${DOOR_PORT}")
  "${pf[@]}" &
  pf_pid=$!
  while kill -0 "${pf_pid}" 2>/dev/null; do
    sleep "${HOLDER_POLL_SECONDS}"
    # A read that fails moves nothing: keep the forward that's up.
    now_holder="$(read_holder)" || continue
    # The Lease read alone shows a move. The Service check runs again only
    # when the holder changed, or when the last answer was the Service (a
    # holder it didn't select then may be selected now).
    if [ "${now_holder}" = "${holder}" ] && [ "${target}" = "pod/${holder}" ]; then
      continue
    fi
    holder="${now_holder}"
    now="$(target_for "${holder}")"
    if [ "${now}" != "${target}" ]; then
      echo "the lease moved to ${now}; restarting the port-forward" >&2
      kill "${pf_pid}" 2>/dev/null || true
      break
    fi
  done
  wait "${pf_pid}" 2>/dev/null || true
  pf_pid=""
  sleep "${RESPAWN_DELAY_SECONDS}"
done
