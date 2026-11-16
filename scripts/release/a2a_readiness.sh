#!/usr/bin/env bash
# The readiness gate for the A2A stack the operator renders for every
# PlatformAgent: the NATS bus, the auth callout, the verifier, the agent and
# the A2A gateway. The release scripts run it after an install
# (provision_environment.sh) and before the E2E suites
# (wait_for_gke_readiness.sh), so a candidate is validated on a stack that is
# up rather than one still coming up.
#
# Sourced, not executed: this file defines constants and functions and runs
# nothing.

# The PlatformAgent every release install creates (the chart's
# platformAgent.name default, which install.sh does not override), and the
# names the operator derives from it for the A2A stack
# (platformagent_a2a_manifests.go: a2aNATSName, a2aCalloutName,
# a2aVerifierName, a2aGatewayName) plus the agent Deployment itself.
readonly A2A_READINESS_CR_NAME="platform-agent"
readonly A2A_READINESS_NATS="statefulset/${A2A_READINESS_CR_NAME}-a2a-nats"
readonly A2A_READINESS_CALLOUT="deployment/${A2A_READINESS_CR_NAME}-a2a-callout"
readonly A2A_READINESS_VERIFIER="deployment/${A2A_READINESS_CR_NAME}-a2a-verifier"
readonly A2A_READINESS_A2A_GATEWAY="deployment/${A2A_READINESS_CR_NAME}-a2a-gateway"
readonly A2A_READINESS_AGENT="deployment/${A2A_READINESS_CR_NAME}-gateway"

# The conditions the operator writes for the bus
# (k8s-operator/internal/controller): BusProvisioned once the provisioning Job
# has completed, and BusCredentialsReady while the auth callout serves the
# current identity map. Every install runs the A2A gateway (on the console
# door alone when no chat backend is configured), so the gate always waits on
# its rollout.
readonly A2A_READINESS_BUS_PROVISIONED="BusProvisioned"
readonly A2A_READINESS_BUS_CREDENTIALS="BusCredentialsReady"

# One budget for the whole gate, every wait inside it taking what is left.
# Ready counts NATS, the callout, the provisioning Job and the A2A gateway as
# well as the agent, and the A2A workloads come up alongside the agent's own
# rollout rather than after it, so the longest single thing in the gate is
# that rollout, and the budget is the agent Deployment's own progress deadline
# (gatewayProgressDeadlineSeconds, 1800s), past which the rollout fails
# whatever anyone waits. One figure also keeps the gate's worst case a single
# number for the jobs that budget for it (deploy-environment.yml's timeout,
# SiblingGatewayRolloutGatesTest). It and the poll interval take an override
# from the environment, which is how the tests run the failure paths without
# waiting them out.
readonly A2A_READINESS_GATE_TIMEOUT_SECONDS="${A2A_READINESS_GATE_TIMEOUT_SECONDS:-1800}"
readonly A2A_READINESS_POLL_SECONDS="${A2A_READINESS_POLL_SECONDS:-10}"
# The label on every object the operator renders for the A2A stack, for the
# dump a failed gate leaves in the log.
readonly A2A_READINESS_SELECTOR="app.kubernetes.io/part-of=a2a-next"

# What a failed gate leaves in the log: the CR's status and the A2A objects.
a2a_readiness_dump_state() {
  local namespace="$1" context="${2:-}"
  local -a kc=(kubectl)
  [ -z "${context}" ] || kc+=(--context "${context}")
  "${kc[@]}" get platformagent "${A2A_READINESS_CR_NAME}" -n "${namespace}" -o yaml | sed -n '/^status:/,$p' || true
  "${kc[@]}" get statefulsets,deployments,jobs,pods -n "${namespace}" -l "${A2A_READINESS_SELECTOR}" || true
}

# The seconds left before a deadline on bash's SECONDS clock, never less than
# one: kubectl reads --timeout=0s as "do not wait", which would turn a gate
# whose budget ran out on the previous step into one that passes unchecked.
a2a_readiness_remaining() {
  local left=$(($1 - SECONDS))
  [ "${left}" -ge 1 ] || left=1
  echo "${left}"
}

# Waits, until the deadline, for one workload's rollout. A container name,
# when given, is waited for in the pod template first: a sidecar the operator
# adds in a later roll of a workload that already exists would otherwise be
# answered for by the roll before it.
a2a_readiness_gate_rollout() {
  local namespace="$1" context="$2" workload="$3" deadline="$4" container="${5:-}"
  local -a kc=(kubectl)
  [ -z "${context}" ] || kc+=(--context "${context}")
  if [ -n "${container}" ]; then
    local names=""
    while :; do
      names="$("${kc[@]}" get "${workload}" -n "${namespace}" -o jsonpath='{.spec.template.spec.containers[*].name}' 2>/dev/null)" || names=""
      case " ${names} " in *" ${container} "*) break ;; esac
      if [ "${SECONDS}" -ge "${deadline}" ]; then
        echo "::error title=A2A stack: ${container} never appeared::${workload} carries no ${container} container within the gate's ${A2A_READINESS_GATE_TIMEOUT_SECONDS}s (containers: ${names:-none})."
        a2a_readiness_dump_state "${namespace}" "${context}"
        return 1
      fi
      sleep "${A2A_READINESS_POLL_SECONDS}"
    done
  fi
  echo "Waiting for ${workload} to roll out..."
  if ! "${kc[@]}" rollout status "${workload}" -n "${namespace}" --timeout="$(a2a_readiness_remaining "${deadline}")s"; then
    echo "::error title=A2A stack: ${workload} did not roll out::${workload} did not finish rolling out within the gate's ${A2A_READINESS_GATE_TIMEOUT_SECONDS}s."
    "${kc[@]}" describe "${workload}" -n "${namespace}" || true
    a2a_readiness_dump_state "${namespace}" "${context}"
    return 1
  fi
}

# Waits, until the deadline, for one CR condition to read True. Polled off
# the status rather than left to `kubectl wait --for=condition`, which skips a
# condition whose observedGeneration trails the CR's generation: the operator
# writes BusProvisioned once, when the provisioning Job first completes, and
# deliberately never rewrites it, so on any CR edited since, once the bus is
# up, that wait times out on a condition that is True.
a2a_readiness_wait_condition() {
  local namespace="$1" context="$2" condition="$3" deadline="$4"
  local -a kc=(kubectl)
  [ -z "${context}" ] || kc+=(--context "${context}")
  local status=""
  echo "Waiting for ${condition}=True..."
  while :; do
    status="$("${kc[@]}" get platformagent "${A2A_READINESS_CR_NAME}" -n "${namespace}" \
      -o jsonpath="{.status.conditions[?(@.type==\"${condition}\")].status}" 2>/dev/null)" || status=""
    [ "${status}" != "True" ] || return 0
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      echo "::error title=A2A stack: ${condition} not True::PlatformAgent/${A2A_READINESS_CR_NAME} did not report ${condition}=True within the gate's ${A2A_READINESS_GATE_TIMEOUT_SECONDS}s (last read: ${status:-absent})."
      a2a_readiness_dump_state "${namespace}" "${context}"
      return 1
    fi
    sleep "${A2A_READINESS_POLL_SECONDS}"
  done
}

# Waits for the operator to report the A2A stack ready, in the order the
# operator brings it up, inside one budget.
#
# Ready first, and Ready for the CR's current generation: right after an
# edit the CR still carries the Ready the operator wrote for the generation
# before, and the Ready condition's observedGeneration is what tells the two
# apart. Ready counts the NATS StatefulSet, the auth callout, the provisioning
# Job (until BusProvisioned latches) and the A2A gateway. Then the two
# conditions that say the bus is usable, and each workload's rollout, so the
# tests do not start in the middle of one. The verifier is gated by rollout
# alone because it is deliberately not a Ready row.
a2a_readiness_wait() {
  local namespace="$1" context="${2:-}"
  local -a kc=(kubectl)
  [ -z "${context}" ] || kc+=(--context "${context}")
  local deadline=$((SECONDS + A2A_READINESS_GATE_TIMEOUT_SECONDS))

  echo "==> A2A stack: waiting up to ${A2A_READINESS_GATE_TIMEOUT_SECONDS}s for PlatformAgent/${A2A_READINESS_CR_NAME} and what it renders..."
  local state="" generation="" ready="" observed=""
  while :; do
    # One read, so the generation and the condition cannot straddle a write.
    state="$("${kc[@]}" get platformagent "${A2A_READINESS_CR_NAME}" -n "${namespace}" \
      -o jsonpath='{.metadata.generation}{" "}{range .status.conditions[?(@.type=="Ready")]}{.status}{" "}{.observedGeneration}{end}' 2>/dev/null)" || state=""
    read -r generation ready observed <<<"${state}" || true
    if [ -n "${generation}" ] && [ "${ready}" = "True" ] && [ "${observed}" = "${generation}" ]; then
      break
    fi
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      echo "::error title=A2A stack: PlatformAgent not Ready::PlatformAgent/${A2A_READINESS_CR_NAME} did not report Ready for generation ${generation:-unknown} within ${A2A_READINESS_GATE_TIMEOUT_SECONDS}s (last read: Ready=${ready:-absent} at generation ${observed:-none})."
      a2a_readiness_dump_state "${namespace}" "${context}"
      return 1
    fi
    sleep "${A2A_READINESS_POLL_SECONDS}"
  done
  echo "PlatformAgent/${A2A_READINESS_CR_NAME} is Ready at generation ${generation}."

  local condition
  for condition in "${A2A_READINESS_BUS_PROVISIONED}" "${A2A_READINESS_BUS_CREDENTIALS}"; do
    a2a_readiness_wait_condition "${namespace}" "${context}" "${condition}" "${deadline}" || return 1
  done

  local workload
  for workload in "${A2A_READINESS_NATS}" "${A2A_READINESS_CALLOUT}" \
    "${A2A_READINESS_VERIFIER}" "${A2A_READINESS_AGENT}"; do
    a2a_readiness_gate_rollout "${namespace}" "${context}" "${workload}" "${deadline}" || return 1
  done

  a2a_readiness_gate_rollout "${namespace}" "${context}" "${A2A_READINESS_A2A_GATEWAY}" "${deadline}" || return 1

  echo "✅ A2A stack: everything the operator renders for it is ready."
}
