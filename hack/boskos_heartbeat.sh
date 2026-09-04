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
#
# Disabled (single notice, exit 0) unless BOSKOS_HOST, BOSKOS_RESOURCE_NAME,
# and BOSKOS_OWNER_NAME are all set. Pool resource names are DNS-safe, so
# the query string needs no URL encoding.

set -uo pipefail

BOSKOS_HEARTBEAT_INTERVAL_SECONDS="${BOSKOS_HEARTBEAT_INTERVAL_SECONDS:-30}"
# State every kube-agents lease is held in while a job owns it; /update 401s
# on an owner mismatch and 409s on a state mismatch, so both must match the
# acquire call the Prow wrapper made.
BOSKOS_RESOURCE_STATE="${BOSKOS_RESOURCE_STATE:-busy}"
# Per-beat detail lands here, off the job log. ARTIFACTS is set by Prow.
BOSKOS_HEARTBEAT_LOG="${BOSKOS_HEARTBEAT_LOG:-${ARTIFACTS:-/tmp}/boskos-heartbeat.log}"
# A beat must never wedge the loop behind a slow server: cap each call well
# under the interval so a timed-out beat still leaves room for the next one.
CURL_MAX_TIME_SECONDS=10
LOG_PREFIX="boskos-heartbeat:"

if [ -z "${BOSKOS_HOST:-}" ] || [ -z "${BOSKOS_RESOURCE_NAME:-}" ] || [ -z "${BOSKOS_OWNER_NAME:-}" ]; then
  echo "${LOG_PREFIX} disabled (BOSKOS_HOST/BOSKOS_RESOURCE_NAME/BOSKOS_OWNER_NAME not all set)"
  exit 0
fi

UPDATE_URL="${BOSKOS_HOST}/update?name=${BOSKOS_RESOURCE_NAME}&owner=${BOSKOS_OWNER_NAME}&state=${BOSKOS_RESOURCE_STATE}"

beats_sent=0
beats_failed=0
# "" until the first beat resolves, then ok|fail; transitions are the only
# per-beat events worth a line on the job log.
last_status=""

summary() {
  echo "${LOG_PREFIX} stopping for ${BOSKOS_RESOURCE_NAME}: ${beats_sent} beats sent, ${beats_failed} failed (detail: ${BOSKOS_HEARTBEAT_LOG})"
  exit 0
}
trap summary TERM INT

mkdir -p "$(dirname "${BOSKOS_HEARTBEAT_LOG}")" 2>/dev/null || true
echo "${LOG_PREFIX} started for ${BOSKOS_RESOURCE_NAME} (owner ${BOSKOS_OWNER_NAME}, every ${BOSKOS_HEARTBEAT_INTERVAL_SECONDS}s, detail: ${BOSKOS_HEARTBEAT_LOG})"

while true; do
  # curl -w emits a code even on failure ("000", or "200000" when the
  # connection dies after headers), so normalise to the LAST three digits
  # rather than appending a fallback that doubles it up.
  http_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time "${CURL_MAX_TIME_SECONDS}" \
    -X POST "${UPDATE_URL}" 2>>"${BOSKOS_HEARTBEAT_LOG}")" || true
  http_code="${http_code:(-3)}"
  [ -n "${http_code}" ] || http_code="000"
  beats_sent=$((beats_sent + 1))
  if [ "${http_code}" = "200" ]; then
    status="ok"
  else
    status="fail"
    beats_failed=$((beats_failed + 1))
  fi
  echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') ${status} http=${http_code}" >>"${BOSKOS_HEARTBEAT_LOG}"
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
