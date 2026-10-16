#!/usr/bin/env bash
#
# Pin the platform agent's pull-request base to the run branch for one run
# (gke-labs/kube-agents#1970). Called by main.tf's
# null_resource.agent_base_branch when gitops_pin_agent_base_branch is set:
# `pin` from the create-time provisioner, after the seed; `unpin` from the
# destroy-time one, before the run branch is deleted.
#
# The base lives on the PlatformAgent's spec.integration.repositories[] entry
# with role gitops that names GITOPS_REPO (spec.integration.repositories[].baseBranch,
# on an operator that has the field), found as the operator resolves it
# (gitops_repo.py, shared with run-branch.sh and run-gitops-pilot.sh). On such
# an operator, a PlatformAgent without such an entry (one on the deprecated
# github alias, which has nowhere to put a base) is refused;
# run-gitops-pilot.sh's agent state reset writes that entry for a case that
# pins its base. On an operator without the field (including one from before
# the lists form, whose PlatformAgent can only be on the alias), pin logs that
# the install pins no base and succeeds, as below.
#
# pin:   reads the CRD before anything is written: one that cannot be read
#        fails the pin, rather than passing for one without the field. When
#        the CRD has no such field, the API server would drop it, so nothing
#        is written; that is logged, and is the case's red on such an
#        install, not a setup failure. Otherwise refuses when the entry's
#        baseBranch already names another branch (an administrator's base is
#        never overwritten, so unpin never removes it), then sets it to the
#        run branch and reads it back. The write is a JSON patch that tests
#        the entry's repository and role and the PlatformAgent's
#        resourceVersion first, so a change made since the read fails it
#        rather than being overwritten. Any write to the PlatformAgent moves
#        its resourceVersion (the operator's status updates among them), so a
#        failed patch is read again and retried, up to PIN_ATTEMPTS tries,
#        unless that read shows another base (refused as above) or this run's
#        (a patch the server applied but kubectl reported as failed). The
#        read-back must match, and the step waits for the operator to render
#        the pin into the credential broker's CREDENTIAL_PROXY_PINNED_BASES
#        for this repository and for the broker to roll. The broker is its
#        own Deployment, and the base is rendered there and nowhere else, so
#        the gateway does not roll. A failure from the first patch on
#        (including a last try the server applied but kubectl reported as
#        failed) removes the field again, when it still names this run's
#        branch, before exiting non-zero: a failed create-time provisioner
#        taints the resource, and tofu runs no destroy-time provisioner on a
#        tainted one, so unpin would never run.
# unpin: removes the entry's baseBranch when it still names this run's
#        branch, and waits for the broker to roll back; anything else is left.
#        The removal is a JSON patch that tests the entry and the value first,
#        so a retry never removes a base another run has set since, and between
#        tries the field is read again: once it no longer names this run's
#        branch, the removal is done.
#        Once the field is removed, the waits only confirm: a timeout there is
#        a warning, not a failure, because a failed destroy-time provisioner
#        keeps this resource in state and stops tofu from destroying the task
#        cluster and the run branch. A PlatformAgent that cannot be read, and
#        a removal that still fails after REMOVE_BASE_ATTEMPTS tries, are
#        warnings for the same reason; the run wrapper's leak check reports a
#        pin left behind (or "unknown" when it cannot read the field), and the
#        next run's wrapper refuses to start while it is there.
#
# Env: AGENT_HOST_CONTEXT, AGENT_NAMESPACE, GITOPS_REPO (a github.com
#      repository, as an https URL), GITOPS_RUN_BRANCH; optionally
#      AGENT_BASE_BRANCH_RENDER_TIMEOUT_SEC (whole seconds, 1 to
#      MAX_RENDER_TIMEOUT_SEC) and AGENT_BASE_BRANCH_POLL_SECONDS (1 to
#      MAX_POLL_SECONDS).
set -euo pipefail

readonly CR="platformagents.kubeagents.x-k8s.io/platform-agent"
readonly CRD="platformagents.kubeagents.x-k8s.io"
readonly BROKER="deploy/platform-agent-credential-proxy"
readonly PINNED_BASES_ENV="CREDENTIAL_PROXY_PINNED_BASES"
# How long the operator may take to render a spec change into the broker's
# pod template, and how often to look. Overridable from the environment so the
# tests can drive the timeout path; prefixed, because tofu's local-exec
# inherits the operator's whole environment.
readonly RENDER_TIMEOUT_SEC="${AGENT_BASE_BRANCH_RENDER_TIMEOUT_SEC:-300}"
readonly POLL_SECONDS="${AGENT_BASE_BRANCH_POLL_SECONDS:-5}"
# Upper bounds on the two: a wait of an hour is already far past any render,
# and a poll above a minute would sleep through most of a timeout.
readonly MAX_RENDER_TIMEOUT_SEC=3600
readonly MAX_POLL_SECONDS=60
# upgrade.sh's budget for the same Deployment (SANDBOX_ROLLOUT_TIMEOUT).
readonly BROKER_ROLLOUT_TIMEOUT=180s
# Tries at removing the field on unpin, POLL_SECONDS apart, before it is left
# to the run wrapper's leak check with a warning.
readonly REMOVE_BASE_ATTEMPTS=3
# Tries at the pin's patch, POLL_SECONDS apart, each from a fresh read.
readonly PIN_ATTEMPTS=3
# What entry() exits with when the PlatformAgent has no gitops entry naming
# GITOPS_REPO (gitops_repo.py's NO_ENTRY).
readonly NO_ENTRY=3
GITOPS_REPO_HELPER="$(cd "$(dirname "$0")" && pwd)/gitops_repo.py"
readonly GITOPS_REPO_HELPER
# What broker_pin() exits with when the broker's pins are readable and do not
# hold this run's.
readonly NOT_PINNED=3

ACTION="${1:?usage: $0 pin|unpin}"
: "${AGENT_HOST_CONTEXT:?}" "${AGENT_NAMESPACE:?}" "${GITOPS_REPO:?}" "${GITOPS_RUN_BRANCH:?}"

# A zero poll would hammer the API server for the whole timeout, a value
# that is not a number fails sleep or reads as 0 in arithmetic, and one too
# long overflows the deadline or sleeps for ever. The digit count is bounded
# before the comparison, so the comparison cannot overflow either.
check_seconds() {
  [[ "$2" =~ ^[1-9][0-9]{0,5}$ ]] && (( $2 <= $3 )) \
    || { echo "agent-base-branch: $1 must be a whole number of seconds from 1 to $3, not '$2'" >&2; exit 1; }
}
check_seconds AGENT_BASE_BRANCH_RENDER_TIMEOUT_SEC "${RENDER_TIMEOUT_SEC}" "${MAX_RENDER_TIMEOUT_SEC}"
check_seconds AGENT_BASE_BRANCH_POLL_SECONDS "${POLL_SECONDS}" "${MAX_POLL_SECONDS}"

case "${GITOPS_RUN_BRANCH}" in
  run/*) ;;
  *) echo "agent-base-branch: refusing '${GITOPS_RUN_BRANCH}': only run/** branches are pinned" >&2; exit 1 ;;
esac

K=(kubectl --context "${AGENT_HOST_CONTEXT}" -n "${AGENT_NAMESPACE}")

slug="$(python3 "${GITOPS_REPO_HELPER}" slug "${GITOPS_REPO}")" || exit 1
# The repository as the operator renders it into the broker's pins.
readonly PINNED_REPOSITORY="https://github.com/${slug}"
readonly FIELD="spec.integration.repositories[${slug}].baseBranch"

# entry base|pin|unpin: the baseBranch of the PlatformAgent's gitops entry
# for GITOPS_REPO and, for pin and unpin, the patch (gitops_repo.py entry).
# Exits NO_ENTRY when there is no such entry, and otherwise non-zero when the
# PlatformAgent cannot be read.
entry() {
  "${K[@]}" get "${CR}" -o json | python3 "${GITOPS_REPO_HELPER}" entry "$1" "${GITOPS_REPO}" "${GITOPS_RUN_BRANCH}"
}

current_base() { entry base; }

no_entry() {
  echo "agent-base-branch: ${CR} has no spec.integration.repositories[] entry with role gitops naming ${slug}, so there is nowhere to pin its base (the deprecated github alias carries no base: run the wrapper with AGENT_STATE_RESET=true, whose reset writes that entry for a case that pins its base)" >&2
}

# EXIT trap of pin, armed before the patch: on a non-zero exit, take the pin
# back when it still names this run's branch (the refusal above has already
# returned, so a base this run did not set is never removed). Best effort; the exit status
# stays the pin's.
undo_failed_pin() {
  local rc=$? out
  trap - EXIT
  if [ "${rc}" -ne 0 ] && out="$(entry unpin)" && [ "${out%%$'\n'*}" = "${GITOPS_RUN_BRANCH}" ]; then
    echo "agent-base-branch: pin failed; removing ${FIELD} (${GITOPS_RUN_BRANCH})" >&2
    "${K[@]}" patch "${CR}" --type=json -p "${out#*$'\n'}" \
      || echo "agent-base-branch: removing it failed; remove ${FIELD} by hand" >&2
  fi
  exit "${rc}"
}

# Whether any served version of the CRD declares
# spec.integration.repositories[].baseBranch: 0 yes, 1 no, anything else when
# the CRD could not be read.
crd_has_base_branch() {
  kubectl --context "${AGENT_HOST_CONTEXT}" get crd "${CRD}" -o json \
    | python3 "${GITOPS_REPO_HELPER}" crd-declares spec.integration.repositories.baseBranch
}

# Exits non-zero when the base read is a branch other than this run's: an
# administrator's base is never overwritten, so unpin never removes it.
refuse_other_base() {
  if [ -n "$1" ] && [ "$1" != "${GITOPS_RUN_BRANCH}" ]; then
    echo "agent-base-branch: ${FIELD} is already '$1'; refusing to overwrite it (another run in flight, or the install's own base)" >&2
    exit 1
  fi
}

# Prints the broker's CREDENTIAL_PROXY_PINNED_BASES (empty when the operator
# renders none) and exits 0 when it holds {repository: PINNED_REPOSITORY,
# branch: the run branch}, NOT_PINNED when it does not, and otherwise
# non-zero when the Deployment cannot be read or the value is not a JSON list.
# The repository compares casefolded, as the broker compares it.
broker_pin() {
  "${K[@]}" get "${BROKER}" -o json | python3 -c '
import json, sys
name, repository, branch, not_pinned = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
try:
    spec = json.load(sys.stdin)["spec"]["template"]["spec"]
except (ValueError, KeyError, TypeError):
    sys.exit(2)
value = next((e.get("value", "") for c in spec.get("initContainers", []) + spec.get("containers", [])
              for e in c.get("env", []) if e.get("name") == name), "")
print(value)
try:
    pins = json.loads(value) if value else []
except ValueError:
    sys.exit(2)
if not isinstance(pins, list):
    sys.exit(2)
held = any(isinstance(p, dict) and str(p.get("repository", "")).casefold() == repository.casefold()
           and p.get("branch") == branch for p in pins)
sys.exit(0 if held else not_pinned)' "${PINNED_BASES_ENV}" "${PINNED_REPOSITORY}" "${GITOPS_RUN_BRANCH}" "${NOT_PINNED}"
}

# wait_broker_pin held|gone: waits until the broker's pins hold this run's
# pin, or no longer do. A failed read counts as not there yet, until the
# deadline.
wait_broker_pin() {
  local want="$1" got rc deadline=$((SECONDS + RENDER_TIMEOUT_SEC))
  while :; do
    rc=0
    got="$(broker_pin)" || rc=$?
    case "${want}/${rc}" in
      held/0|gone/"${NOT_PINNED}") return 0 ;;
      */0|*/"${NOT_PINNED}") ;;
      *) got="${got:-(unreadable)}" ;;
    esac
    if (( SECONDS >= deadline )); then
      echo "agent-base-branch: ${BROKER} ${PINNED_BASES_ENV} is '${got}' after ${RENDER_TIMEOUT_SEC}s; expected the pin of ${PINNED_REPOSITORY} to ${GITOPS_RUN_BRANCH} to be ${want}" >&2
      return 1
    fi
    sleep "${POLL_SECONDS}"
  done
}

case "${ACTION}" in
  pin)
    # The patch built from this read tests the resourceVersion read with the
    # base, so a base written between this read and the patch fails it rather
    # than being overwritten.
    read_rc=0
    out="$(entry pin)" || read_rc=$?
    case "${read_rc}" in
      0|"${NO_ENTRY}") ;;
      *) echo "agent-base-branch: cannot read ${CR} on ${AGENT_HOST_CONTEXT}" >&2; exit 1 ;;
    esac
    # Before anything is written, so a CRD that cannot be read fails with
    # nothing written, and before the missing entry is refused: on a CRD
    # without the field (one from before the lists form, too) the
    # PlatformAgent has nowhere to hold a base, which is the case's red, not a
    # setup failure.
    crd_rc=0
    crd_has_base_branch || crd_rc=$?
    case "${crd_rc}" in
      0) ;;
      1)
        echo "==> agent-base-branch: the installed CRD has no spec.integration.repositories[].baseBranch, so the API server would drop ${FIELD}, and nothing is written: this install pins no base, and the agent's pull request base falls back to the repository default"
        exit 0 ;;
      *)
        echo "agent-base-branch: cannot read the CRD ${CRD} on ${AGENT_HOST_CONTEXT}, so whether it declares spec.integration.repositories[].baseBranch is unknown" >&2
        exit 1 ;;
    esac
    if [ "${read_rc}" -eq "${NO_ENTRY}" ]; then
      no_entry; exit 1
    fi
    got="${out%%$'\n'*}"
    patch="${out#*$'\n'}"
    refuse_other_base "${got}"
    echo "==> agent-base-branch: ${FIELD} <- ${GITOPS_RUN_BRANCH} on ${AGENT_HOST_CONTEXT}"
    trap undo_failed_pin EXIT
    attempt=1
    until "${K[@]}" patch "${CR}" --type=json -p "${patch}"; do
      if (( attempt >= PIN_ATTEMPTS )); then
        echo "agent-base-branch: setting ${FIELD} failed ${PIN_ATTEMPTS} times" >&2
        exit 1
      fi
      attempt=$((attempt + 1))
      sleep "${POLL_SECONDS}"
      # A read that fails leaves the last patch to be tried again.
      if out="$(entry pin)"; then
        got="${out%%$'\n'*}"
        patch="${out#*$'\n'}"
        [ "${got}" != "${GITOPS_RUN_BRANCH}" ] || break
        refuse_other_base "${got}"
      fi
    done
    got="$(current_base)"
    echo "    read back: ${FIELD}='${got}'"
    [ "${got}" = "${GITOPS_RUN_BRANCH}" ] || { echo "agent-base-branch: baseBranch reads back '${got}', not ${GITOPS_RUN_BRANCH}" >&2; exit 1; }
    # The operator renders a pin only for an accepted repository, under the
    # URL it resolves; a pin under another repository or host is not this one.
    wait_broker_pin held
    echo "    broker env: ${PINNED_BASES_ENV} holds ${PINNED_REPOSITORY} -> ${GITOPS_RUN_BRANCH}"
    "${K[@]}" rollout status "${BROKER}" --timeout="${BROKER_ROLLOUT_TIMEOUT}"
    ;;
  unpin)
    read_rc=0
    out="$(entry unpin)" || read_rc=$?
    case "${read_rc}" in
      0) ;;
      "${NO_ENTRY}")
        echo "==> agent-base-branch: ${CR} has no gitops entry naming ${slug}; nothing to unpin"
        exit 0 ;;
      *)
        echo "WARN agent-base-branch: cannot read ${CR} on ${AGENT_HOST_CONTEXT}; nothing to unpin" >&2
        exit 0 ;;
    esac
    got="${out%%$'\n'*}"
    if [ "${got}" != "${GITOPS_RUN_BRANCH}" ]; then
      echo "==> agent-base-branch: baseBranch is '${got}', not this run's; leaving it"
      exit 0
    fi
    echo "==> agent-base-branch: removing ${FIELD} (${got}) on ${AGENT_HOST_CONTEXT}"
    attempt=1
    until "${K[@]}" patch "${CR}" --type=json -p "${out#*$'\n'}"; do
      # A removal the server applied but kubectl reported as failed, or a base
      # another run has set since, ends the retries. The next try uses the
      # patch from this read, in case the entry moved.
      if now="$(entry unpin)"; then
        out="${now}"
        now="${now%%$'\n'*}"
        if [ "${now}" != "${GITOPS_RUN_BRANCH}" ]; then
          if [ -n "${now}" ]; then
            echo "==> agent-base-branch: baseBranch is now '${now}', not this run's; leaving it"
            exit 0
          fi
          echo "    ${FIELD} is gone"
          break
        fi
      fi
      if (( attempt >= REMOVE_BASE_ATTEMPTS )); then
        echo "WARN agent-base-branch: removing ${FIELD} (${got}) failed ${REMOVE_BASE_ATTEMPTS} times; remove it by hand" >&2
        exit 0
      fi
      attempt=$((attempt + 1))
      sleep "${POLL_SECONDS}"
    done
    if ! wait_broker_pin gone \
      || ! "${K[@]}" rollout status "${BROKER}" --timeout="${BROKER_ROLLOUT_TIMEOUT}"; then
      echo "WARN agent-base-branch: baseBranch is removed, but ${BROKER} has not rolled off it yet; check it by hand" >&2
    fi
    ;;
  *)
    echo "usage: $0 pin|unpin" >&2; exit 1 ;;
esac
