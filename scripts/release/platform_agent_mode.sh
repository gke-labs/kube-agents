#!/usr/bin/env bash
# The PlatformAgent's spec.mode on the release path: the one value the release
# scripts carry for it, the patch that applies `next` after an install, and the
# gate that waits for what `next` renders.
#
# PLATFORM_AGENT_MODE is the install.env key the install's planned --mode
# flag (#2524) will read and record, so the release path spells it the same
# way. Unset or `today` is the default and changes nothing:
# platform_agent_mode_resolve unsets the variable, so a release script and
# everything it runs see exactly the environment they saw before the mode
# existed, and render_install_env.sh writes no key for it (an absent spec.mode
# is `today` to the CRD). Only `next` adds anything.
#
# `next` is for the ephemeral rc and nightly clusters only.
# platform_agent_mode_refuse_long_lived says why.
#
# Sourced, not executed: this file defines constants and functions and runs
# nothing.

# The PlatformAgent every release install creates (the chart's
# platformAgent.name default, which install.sh does not override), and the
# names the operator derives from it for what `next` renders
# (platformagent_a2a_manifests.go: a2aNATSName, a2aCalloutName,
# a2aVerifierName, a2aGatewayName) plus the agent Deployment itself.
readonly PLATFORM_AGENT_MODE_CR_NAME="platform-agent"
readonly PLATFORM_AGENT_MODE_NATS="statefulset/${PLATFORM_AGENT_MODE_CR_NAME}-a2a-nats"
readonly PLATFORM_AGENT_MODE_CALLOUT="deployment/${PLATFORM_AGENT_MODE_CR_NAME}-a2a-callout"
readonly PLATFORM_AGENT_MODE_VERIFIER="deployment/${PLATFORM_AGENT_MODE_CR_NAME}-a2a-verifier"
readonly PLATFORM_AGENT_MODE_A2A_GATEWAY="deployment/${PLATFORM_AGENT_MODE_CR_NAME}-a2a-gateway"
readonly PLATFORM_AGENT_MODE_AGENT="deployment/${PLATFORM_AGENT_MODE_CR_NAME}-gateway"

# The two values the CRD's spec.mode enum takes, and the merge patch that sets
# the second. The chart renders no spec.mode, so a later helm upgrade leaves a
# patched value alone.
readonly PLATFORM_AGENT_MODE_TODAY="today"
readonly PLATFORM_AGENT_MODE_NEXT="next"
readonly PLATFORM_AGENT_MODE_NEXT_PATCH='{"spec":{"mode":"next"}}'

# The conditions the operator writes under `next`
# (k8s-operator/internal/controller): BusProvisioned once the provisioning Job
# has completed, BusCredentialsReady while the auth callout serves the current
# identity map, and A2AGateway, present only while the gateway is withheld for
# want of a chat backend, with this reason.
readonly PLATFORM_AGENT_MODE_BUS_PROVISIONED="BusProvisioned"
readonly PLATFORM_AGENT_MODE_BUS_CREDENTIALS="BusCredentialsReady"
readonly PLATFORM_AGENT_MODE_GATEWAY_CONDITION="A2AGateway"
readonly PLATFORM_AGENT_MODE_GATEWAY_DARK_REASON="NoChatBackend"

# One budget for the whole gate, every wait inside it taking what is left.
# Ready under `next` counts NATS, the callout, the provisioning Job and the A2A
# gateway as well as the agent, and the switch rolls the agent; the A2A
# workloads come up alongside that roll rather than after it, so the longest
# single thing in the gate is the agent's rollout, and the budget is the
# agent Deployment's own progress deadline (gatewayProgressDeadlineSeconds,
# 1800s), past which the rollout fails whatever anyone waits. One figure also
# keeps the gate's worst case a single number for the jobs that budget for it
# (deploy-environment.yml's timeout under `next`, SiblingGatewayRolloutGatesTest).
# It and the poll interval take an override from the environment, which is how
# the tests run the failure paths without waiting them out.
readonly PLATFORM_AGENT_MODE_GATE_TIMEOUT_SECONDS="${PLATFORM_AGENT_MODE_GATE_TIMEOUT_SECONDS:-1800}"
readonly PLATFORM_AGENT_MODE_POLL_SECONDS="${PLATFORM_AGENT_MODE_POLL_SECONDS:-10}"
# The label on every object the operator renders for `next`, for the dump a
# failed gate leaves in the log.
readonly PLATFORM_AGENT_MODE_A2A_SELECTOR="app.kubernetes.io/part-of=a2a-next"

# Reads PLATFORM_AGENT_MODE and leaves the answer in RELEASE_PLATFORM_AGENT_MODE.
# Unset, empty and `today` are all today, and the variable is then unset, so
# nothing this script runs afterwards sees it. `next` stays exported. Anything
# else is refused: a typo must not read as today and validate a candidate on
# the stack nobody asked for.
# shellcheck disable=SC2034  # RELEASE_PLATFORM_AGENT_MODE is read by the scripts that source this file.
platform_agent_mode_resolve() {
  case "${PLATFORM_AGENT_MODE:-}" in
    "" | "${PLATFORM_AGENT_MODE_TODAY}")
      RELEASE_PLATFORM_AGENT_MODE="${PLATFORM_AGENT_MODE_TODAY}"
      unset PLATFORM_AGENT_MODE
      ;;
    "${PLATFORM_AGENT_MODE_NEXT}")
      RELEASE_PLATFORM_AGENT_MODE="${PLATFORM_AGENT_MODE_NEXT}"
      export PLATFORM_AGENT_MODE
      ;;
    *)
      echo "::error title=PLATFORM_AGENT_MODE is not a mode::'${PLATFORM_AGENT_MODE}' is neither ${PLATFORM_AGENT_MODE_TODAY} nor ${PLATFORM_AGENT_MODE_NEXT}, the two values the PlatformAgent's spec.mode takes. Pass one of them, or leave it unset for ${PLATFORM_AGENT_MODE_TODAY}."
      echo "==> PLATFORM_AGENT_MODE='${PLATFORM_AGENT_MODE}' is not one of ${PLATFORM_AGENT_MODE_TODAY}, ${PLATFORM_AGENT_MODE_NEXT}." >&2
      return 1
      ;;
  esac
}

# Refuses `next` on a long-lived environment (autopush, autopush-next,
# staging). The patch
# outlives the run there: the chart renders no spec.mode and the installer
# does not read the key, so no later upgrade or reconcile puts it back, drift
# detection plans nothing for it, and Google Chat stays moved off the legacy
# consumer onto the A2A gateway until somebody rebuilds the environment on
# `today`. rc and nightly are destroyed and rebuilt every run, so the mode
# goes with them. Call after platform_agent_mode_resolve.
platform_agent_mode_refuse_long_lived() {
  local long_lived="${1:-}" environment="${2:-this environment}"
  [ "${RELEASE_PLATFORM_AGENT_MODE}" = "${PLATFORM_AGENT_MODE_NEXT}" ] || return 0
  [ -n "${long_lived}" ] || return 0
  echo "::error title=spec.mode next is for the ephemeral environments::Refusing spec.mode ${PLATFORM_AGENT_MODE_NEXT} on '${environment}', a long-lived environment. Nothing that later moves it (an upgrade, a reconcile, drift detection) knows about the patched mode, so it would stay next, with Google Chat moved to the A2A gateway, until a ${PLATFORM_AGENT_MODE_TODAY} rebuild. Use the ephemeral rc or nightly environment for next."
  echo "==> spec.mode ${PLATFORM_AGENT_MODE_NEXT} refused on long-lived '${environment}'." >&2
  return 1
}

# Refuses a run whose mode is not the one the cluster was installed with.
# The deploy and E2E jobs each take the mode as an input, so nothing else ties
# the gate to what was built: `next` against a `today` install would spend the
# whole gate waiting on BusProvisioned, a condition `today` never carries, and
# fail naming the condition rather than the mismatch; `today` against a `next`
# install would run the suites without the gate. The CR's spec.mode is the
# answer, and an absent one is `today` to the CRD. A read that fails stops the
# run too: an unknown mode is not a match. Call after
# platform_agent_mode_resolve.
platform_agent_mode_check_installed() {
  local namespace="$1" context="${2:-}"
  local -a kc=(kubectl)
  [ -z "${context}" ] || kc+=(--context "${context}")
  local installed=""
  if ! installed="$("${kc[@]}" get platformagent "${PLATFORM_AGENT_MODE_CR_NAME}" -n "${namespace}" -o jsonpath='{.spec.mode}')"; then
    echo "::error title=Mode unreadable::Could not read PlatformAgent/${PLATFORM_AGENT_MODE_CR_NAME}'s spec.mode in ${namespace}, so whether this run's mode ${RELEASE_PLATFORM_AGENT_MODE} matches the install is unknown."
    return 1
  fi
  installed="${installed:-${PLATFORM_AGENT_MODE_TODAY}}"
  [ "${installed}" != "${RELEASE_PLATFORM_AGENT_MODE}" ] || return 0
  echo "::error title=Mode mismatch::This run was given mode ${RELEASE_PLATFORM_AGENT_MODE}, but PlatformAgent/${PLATFORM_AGENT_MODE_CR_NAME} in ${namespace} was installed with spec.mode ${installed}. Pass the same mode the deploy job installed with."
  echo "==> mode ${RELEASE_PLATFORM_AGENT_MODE} requested, spec.mode ${installed} installed." >&2
  return 1
}

# What a failed gate leaves in the log: the CR's status and the A2A objects.
platform_agent_mode_dump_state() {
  local namespace="$1" context="${2:-}"
  local -a kc=(kubectl)
  [ -z "${context}" ] || kc+=(--context "${context}")
  "${kc[@]}" get platformagent "${PLATFORM_AGENT_MODE_CR_NAME}" -n "${namespace}" -o yaml | sed -n '/^status:/,$p' || true
  "${kc[@]}" get statefulsets,deployments,jobs,pods -n "${namespace}" -l "${PLATFORM_AGENT_MODE_A2A_SELECTOR}" || true
}

# Sets spec.mode: next on the PlatformAgent.
platform_agent_mode_patch_next() {
  local namespace="$1" context="${2:-}"
  local -a kc=(kubectl)
  [ -z "${context}" ] || kc+=(--context "${context}")
  echo "==> Setting spec.mode: ${PLATFORM_AGENT_MODE_NEXT} on PlatformAgent/${PLATFORM_AGENT_MODE_CR_NAME} in ${namespace}..."
  "${kc[@]}" patch platformagent "${PLATFORM_AGENT_MODE_CR_NAME}" -n "${namespace}" \
    --type merge -p "${PLATFORM_AGENT_MODE_NEXT_PATCH}"
}

# The seconds left before a deadline on bash's SECONDS clock, never less than
# one: kubectl reads --timeout=0s as "do not wait", which would turn a gate
# whose budget ran out on the previous step into one that passes unchecked.
platform_agent_mode_remaining() {
  local left=$(($1 - SECONDS))
  [ "${left}" -ge 1 ] || left=1
  echo "${left}"
}

# Waits, until the deadline, for one workload's rollout. A container name,
# when given, is waited for in the pod template first: a sidecar the operator
# adds in a later roll of a workload that already exists would otherwise be
# answered for by the roll before it.
platform_agent_mode_gate_rollout() {
  local namespace="$1" context="$2" workload="$3" deadline="$4" container="${5:-}"
  local -a kc=(kubectl)
  [ -z "${context}" ] || kc+=(--context "${context}")
  if [ -n "${container}" ]; then
    local names=""
    while :; do
      names="$("${kc[@]}" get "${workload}" -n "${namespace}" -o jsonpath='{.spec.template.spec.containers[*].name}' 2>/dev/null)" || names=""
      case " ${names} " in *" ${container} "*) break ;; esac
      if [ "${SECONDS}" -ge "${deadline}" ]; then
        echo "::error title=mode next: ${container} never appeared::${workload} carries no ${container} container within the gate's ${PLATFORM_AGENT_MODE_GATE_TIMEOUT_SECONDS}s (containers: ${names:-none})."
        platform_agent_mode_dump_state "${namespace}" "${context}"
        return 1
      fi
      sleep "${PLATFORM_AGENT_MODE_POLL_SECONDS}"
    done
  fi
  echo "Waiting for ${workload} to roll out..."
  if ! "${kc[@]}" rollout status "${workload}" -n "${namespace}" --timeout="$(platform_agent_mode_remaining "${deadline}")s"; then
    echo "::error title=mode next: ${workload} did not roll out::${workload} did not finish rolling out within the gate's ${PLATFORM_AGENT_MODE_GATE_TIMEOUT_SECONDS}s under spec.mode: ${PLATFORM_AGENT_MODE_NEXT}."
    "${kc[@]}" describe "${workload}" -n "${namespace}" || true
    platform_agent_mode_dump_state "${namespace}" "${context}"
    return 1
  fi
}

# Waits, until the deadline, for one CR condition to read True. Polled off
# the status rather than left to `kubectl wait --for=condition`, which skips a
# condition whose observedGeneration trails the CR's generation: the operator
# writes BusProvisioned once, when the provisioning Job first completes, and
# deliberately never rewrites it, so on any CR edited since (the mode patch
# included, once the bus is up) that wait times out on a condition that is True.
platform_agent_mode_wait_condition() {
  local namespace="$1" context="$2" condition="$3" deadline="$4"
  local -a kc=(kubectl)
  [ -z "${context}" ] || kc+=(--context "${context}")
  local status=""
  echo "Waiting for ${condition}=True..."
  while :; do
    status="$("${kc[@]}" get platformagent "${PLATFORM_AGENT_MODE_CR_NAME}" -n "${namespace}" \
      -o jsonpath="{.status.conditions[?(@.type==\"${condition}\")].status}" 2>/dev/null)" || status=""
    [ "${status}" != "True" ] || return 0
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      echo "::error title=mode next: ${condition} not True::PlatformAgent/${PLATFORM_AGENT_MODE_CR_NAME} did not report ${condition}=True within the gate's ${PLATFORM_AGENT_MODE_GATE_TIMEOUT_SECONDS}s (last read: ${status:-absent})."
      platform_agent_mode_dump_state "${namespace}" "${context}"
      return 1
    fi
    sleep "${PLATFORM_AGENT_MODE_POLL_SECONDS}"
  done
}

# Waits for the operator to report what `next` renders as ready, in the
# order the operator brings it up, inside one budget.
#
# Ready first, and Ready for the CR's current generation: right after the
# patch the CR still carries the Ready the operator wrote for today, and the
# Ready condition's observedGeneration is what tells the two apart. Under
# `next` Ready counts the NATS StatefulSet, the auth callout, the provisioning
# Job (until BusProvisioned latches) and the A2A gateway unless the install
# has no chat backend. Then the two conditions that say the bus is usable, and
# each workload's rollout, so the tests do not start in the middle of one. The
# verifier is gated by rollout alone because it is deliberately not a Ready
# row. The A2A gateway is skipped only when the operator says it withheld it.
platform_agent_mode_wait_next() {
  local namespace="$1" context="${2:-}"
  local -a kc=(kubectl)
  [ -z "${context}" ] || kc+=(--context "${context}")
  local deadline=$((SECONDS + PLATFORM_AGENT_MODE_GATE_TIMEOUT_SECONDS))

  echo "==> spec.mode ${PLATFORM_AGENT_MODE_NEXT}: waiting up to ${PLATFORM_AGENT_MODE_GATE_TIMEOUT_SECONDS}s for PlatformAgent/${PLATFORM_AGENT_MODE_CR_NAME} and what it renders..."
  local state="" generation="" ready="" observed=""
  while :; do
    # One read, so the generation and the condition cannot straddle a write.
    state="$("${kc[@]}" get platformagent "${PLATFORM_AGENT_MODE_CR_NAME}" -n "${namespace}" \
      -o jsonpath='{.metadata.generation}{" "}{range .status.conditions[?(@.type=="Ready")]}{.status}{" "}{.observedGeneration}{end}' 2>/dev/null)" || state=""
    read -r generation ready observed <<<"${state}" || true
    if [ -n "${generation}" ] && [ "${ready}" = "True" ] && [ "${observed}" = "${generation}" ]; then
      break
    fi
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      echo "::error title=mode next: PlatformAgent not Ready::PlatformAgent/${PLATFORM_AGENT_MODE_CR_NAME} did not report Ready for generation ${generation:-unknown} within ${PLATFORM_AGENT_MODE_GATE_TIMEOUT_SECONDS}s (last read: Ready=${ready:-absent} at generation ${observed:-none})."
      platform_agent_mode_dump_state "${namespace}" "${context}"
      return 1
    fi
    sleep "${PLATFORM_AGENT_MODE_POLL_SECONDS}"
  done
  echo "PlatformAgent/${PLATFORM_AGENT_MODE_CR_NAME} is Ready at generation ${generation}."

  local condition
  for condition in "${PLATFORM_AGENT_MODE_BUS_PROVISIONED}" "${PLATFORM_AGENT_MODE_BUS_CREDENTIALS}"; do
    platform_agent_mode_wait_condition "${namespace}" "${context}" "${condition}" "${deadline}" || return 1
  done

  local workload
  for workload in "${PLATFORM_AGENT_MODE_NATS}" "${PLATFORM_AGENT_MODE_CALLOUT}" \
    "${PLATFORM_AGENT_MODE_VERIFIER}" "${PLATFORM_AGENT_MODE_AGENT}"; do
    platform_agent_mode_gate_rollout "${namespace}" "${context}" "${workload}" "${deadline}" || return 1
  done

  # Empty is an answer here (the condition is absent while the gateway is
  # rendered), so a dropped read is told from it by the exit status and
  # retried inside the budget like every other read in the gate.
  local gateway_reason=""
  until gateway_reason="$("${kc[@]}" get platformagent "${PLATFORM_AGENT_MODE_CR_NAME}" -n "${namespace}" \
    -o jsonpath="{.status.conditions[?(@.type==\"${PLATFORM_AGENT_MODE_GATEWAY_CONDITION}\")].reason}" 2>/dev/null)"; do
    gateway_reason=""
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      echo "::error title=mode next: ${PLATFORM_AGENT_MODE_GATEWAY_CONDITION} unreadable::PlatformAgent/${PLATFORM_AGENT_MODE_CR_NAME}'s ${PLATFORM_AGENT_MODE_GATEWAY_CONDITION} condition could not be read within the gate's ${PLATFORM_AGENT_MODE_GATE_TIMEOUT_SECONDS}s, so whether to wait for the A2A gateway is unknown."
      platform_agent_mode_dump_state "${namespace}" "${context}"
      return 1
    fi
    sleep "${PLATFORM_AGENT_MODE_POLL_SECONDS}"
  done
  if [ "${gateway_reason}" = "${PLATFORM_AGENT_MODE_GATEWAY_DARK_REASON}" ]; then
    echo "${PLATFORM_AGENT_MODE_A2A_GATEWAY} is withheld (${PLATFORM_AGENT_MODE_GATEWAY_CONDITION}=False ${PLATFORM_AGENT_MODE_GATEWAY_DARK_REASON}): this install configures no chat backend, so there is no A2A gateway to wait for."
  else
    platform_agent_mode_gate_rollout "${namespace}" "${context}" "${PLATFORM_AGENT_MODE_A2A_GATEWAY}" "${deadline}" || return 1
  fi

  echo "✅ spec.mode ${PLATFORM_AGENT_MODE_NEXT}: everything the operator renders for it is ready."
}
