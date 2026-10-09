#!/usr/bin/env bash
# ==============================================================================
# Boskos lease heartbeat daemon
# ==============================================================================
# POSTs the Boskos client's /update call every
# BOSKOS_HEARTBEAT_INTERVAL_SECONDS so the lease's LastUpdate stays fresh and
# the reaper (ranch.Reset, ~5m window) does not reclaim the project while a
# CI phase still runs. Covers what the Prow wrapper's own heartbeat does not
# — teardown, where a slow sweep outliving the lease turned the final
# release into a 401 (2026-09-01, kube-agents-evals-4).
#
# Liveness travels on its own channel: per-beat lines go to
# BOSKOS_HEARTBEAT_LOG, stdout carries only start, ok<->fail transitions,
# and a stop summary. At the ~5m window a 30s cadence tolerates 9 missed
# beats, so a 3-minute hang (6 beats) keeps the lease; a pod frozen past the
# window loses it, which is the reclaim working as intended.
#
# Usage (backgrounded, killed by the caller's EXIT trap):
#   ./hack/boskos_heartbeat.sh & HEARTBEAT_PID=$!
#   trap 'kill "${HEARTBEAT_PID}" 2>/dev/null || true' EXIT
# If the caller dies without running that trap, the daemon notices at its
# next beat and stops itself (see the loop), so it cannot outlive the job.
#
# Disabled (single notice, exit 0) unless BOSKOS_HOST, BOSKOS_RESOURCE_NAME,
# and BOSKOS_OWNER_NAME are all set. Pool resource names are DNS-safe, so
# the query string needs no URL encoding.
#
# No command substitution anywhere in this file. bash 5.2 (every patch
# level; 5.1 and 5.3 are clean, and the Prow image and ubuntu-latest both
# run 5.2) runs a pending trap from inside the parser when a signal lands
# while it re-parses a $(...) or <(...) for expansion, and the parser's
# state leaks into the trap: either the trap string fails to parse
# ("trap: line 2: unexpected EOF while looking for matching `)'") and the
# signal is lost, or the enclosing command fails to parse and the shell
# exits 1 without running summary -- no stop line, no WARNING. The window
# is microseconds per beat. So curl writes its status code to CAPTURE_FILE
# and `read` takes it back, date writes the detail line itself, and the
# parent pid is `read` out of /proc. tests/test_boskos_heartbeat.py rejects
# the construct. No `break` either: on every bash version a trapped signal
# that lands on a `break` out of a loop is dropped rather than deferred
# (5 of 1000 TERMs in a tight loop), so a loop here runs to its end. All of
# it also runs on bash 3.2, the macOS /bin/bash the unit tests can land in
# on a developer machine.

set -uo pipefail

BOSKOS_HEARTBEAT_INTERVAL_SECONDS="${BOSKOS_HEARTBEAT_INTERVAL_SECONDS:-30}"
# State every kube-agents lease is held in while a job owns it; /update 401s
# on an owner mismatch and 409s on a state mismatch, so both must match the
# acquire call the Prow wrapper made.
BOSKOS_RESOURCE_STATE="${BOSKOS_RESOURCE_STATE:-busy}"
# Per-beat detail lands here, off the job log. ARTIFACTS is set by Prow.
BOSKOS_HEARTBEAT_LOG="${BOSKOS_HEARTBEAT_LOG:-${ARTIFACTS:-/tmp}/boskos-heartbeat.log}"
# Where an external command's output lands to be read back (see the header
# on why not a $(...)). Removed by the stop summary; a SIGKILL leaves it.
readonly CAPTURE_FILE="${BOSKOS_HEARTBEAT_LOG}.capture"
# A beat must never wedge the loop behind a slow server: cap each call well
# under the interval so a timed-out beat still leaves room for the next one.
CURL_MAX_TIME_SECONDS=10
LOG_PREFIX="boskos-heartbeat:"
# Timestamp opening each detail-log line, UTC; date -u writes the line.
readonly BEAT_TIME_FORMAT='%Y-%m-%dT%H:%M:%SZ'

if [ -z "${BOSKOS_HOST:-}" ] || [ -z "${BOSKOS_RESOURCE_NAME:-}" ] || [ -z "${BOSKOS_OWNER_NAME:-}" ]; then
  echo "${LOG_PREFIX} disabled (BOSKOS_HOST/BOSKOS_RESOURCE_NAME/BOSKOS_OWNER_NAME not all set)"
  exit 0
fi

UPDATE_URL="${BOSKOS_HOST}/update?name=${BOSKOS_RESOURCE_NAME}&owner=${BOSKOS_OWNER_NAME}&state=${BOSKOS_RESOURCE_STATE}"

beats_sent=0
beats_failed=0
# Beats Boskos answered 401: ranch's OwnerNotMatch, i.e. the lease is no
# longer this job's. Counted apart from other failures because it is the one
# outcome the stop summary must shout about (see summary below).
beats_401=0
# "" until the first beat resolves, then ok|fail; transitions are the only
# per-beat events worth a line on the job log.
last_status=""

summary() {
  # One loud line when the lease was lost under this daemon. The Prow
  # wrapper's release that follows will get the same 401 and its `|| true`
  # swallows it, so this is the end-of-run signal that the project was not
  # handed back: the first full nightly (build 2100374258805903360,
  # 2026-09-17) lost kube-agents-evals-6 that way when the wrapper's
  # boskosctl heartbeat hit its default 5h --timeout (#1491).
  if [ "${beats_401}" -gt 0 ]; then
    echo "${LOG_PREFIX} WARNING: lease on ${BOSKOS_RESOURCE_NAME} is lost (Boskos answered 401 owner-mismatch on ${beats_401} of ${beats_sent} beats); the release will fail the same way and ${BOSKOS_RESOURCE_NAME} stays leased until Boskos's reaper frees it"
  fi
  echo "${LOG_PREFIX} stopping for ${BOSKOS_RESOURCE_NAME}: ${beats_sent} beats sent, ${beats_failed} failed (detail: ${BOSKOS_HEARTBEAT_LOG})"
  rm -f "${CAPTURE_FILE}"
  exit 0
}
trap summary TERM INT

# True while the process that started this daemon still exists. Compares the
# kernel's current parent pid with $PPID (fixed at startup): the kernel
# reparents a child the moment its parent dies, before anyone reaps it, so
# this also catches a caller that sits as a zombie -- where `kill -0 $PPID`
# would still say alive. Reads /proc where it exists (the Prow image) and
# falls back to ps elsewhere; if neither answers, the caller is assumed
# alive, so a missing tool can only keep the lease beating, never drop it.
caller_alive() {
  local parent_now="" key value
  if [ -r "/proc/$$/status" ]; then
    while read -r key value _; do
      [ "${key}" = "PPid:" ] && parent_now="${value}"
    done <"/proc/$$/status"
  else
    ps -o ppid= -p "$$" >"${CAPTURE_FILE}" 2>/dev/null && read -r parent_now <"${CAPTURE_FILE}"
  fi
  [ -z "${parent_now}" ] || [ "${parent_now}" = "${PPID}" ]
}

case "${BOSKOS_HEARTBEAT_LOG}" in
  */*) mkdir -p "${BOSKOS_HEARTBEAT_LOG%/*}" 2>/dev/null || true ;;
esac
echo "${LOG_PREFIX} started for ${BOSKOS_RESOURCE_NAME} (owner ${BOSKOS_OWNER_NAME}, every ${BOSKOS_HEARTBEAT_INTERVAL_SECONDS}s, detail: ${BOSKOS_HEARTBEAT_LOG})"

while true; do
  # The caller's EXIT trap is the normal stop. If the caller died without
  # running it (SIGKILL), stop anyway: this process inherits the job's log
  # pipe, and Prow's entrypoint waits in command.Wait() for every holder of
  # that pipe to exit, so an orphan that never exits holds the job open
  # until the decoration timeout and turns a finished run into a "timed
  # out" one.
  caller_alive || summary
  # curl -w emits a code even on failure ("000", or "200000" when the
  # connection dies after headers), so normalise to the LAST three digits
  # rather than appending a fallback that doubles it up.
  http_code=""
  curl -sS -o /dev/null -w '%{http_code}' --max-time "${CURL_MAX_TIME_SECONDS}" \
    -X POST "${UPDATE_URL}" >"${CAPTURE_FILE}" 2>>"${BOSKOS_HEARTBEAT_LOG}" || true
  read -r http_code <"${CAPTURE_FILE}" 2>/dev/null || true
  http_code="${http_code:(-3)}"
  [ -n "${http_code}" ] || http_code="000"
  beats_sent=$((beats_sent + 1))
  if [ "${http_code}" = "200" ]; then
    status="ok"
  else
    status="fail"
    beats_failed=$((beats_failed + 1))
    [ "${http_code}" = "401" ] && beats_401=$((beats_401 + 1))
  fi
  date -u +"${BEAT_TIME_FORMAT} ${status} http=${http_code}" >>"${BOSKOS_HEARTBEAT_LOG}"
  if [ "${status}" != "${last_status}" ]; then
    if [ "${status}" = "fail" ]; then
      # 401 here means the lease is already lost (owner mismatch) — the exact
      # signal that used to surface only as a failed release at job end.
      echo "${LOG_PREFIX} beat FAILED for ${BOSKOS_RESOURCE_NAME} (http=${http_code}); continuing"
    elif [ -n "${last_status}" ]; then
      echo "${LOG_PREFIX} recovered for ${BOSKOS_RESOURCE_NAME}"
    fi
    last_status="${status}"
  fi
  sleep "${BOSKOS_HEARTBEAT_INTERVAL_SECONDS}"
done
