#!/usr/bin/env bash
# Run scripts/pool_pressure.py on a schedule, keep the history, and hand a
# breach to whatever sends the notification.
#
# This script does not send mail. It decides *whether* to notify and *what to
# say*, then execs $POOL_PRESSURE_NOTIFY_COMMAND with the report on stdin.
# Delivery is deliberately somebody else's problem: it is the part that needs a
# credential, and the part most likely to be replaced.
#
# Install (crontab, hourly):
#     0 * * * * /path/to/kube-agents/hack/pool_pressure_cron.sh
#
# Run `pool_pressure_cron.sh --help` for the systemd timer version and the full
# list of environment variables.
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly CHECK="${REPO_ROOT}/scripts/pool_pressure.py"

readonly PYTHON="${POOL_PRESSURE_PYTHON:-python3}"
readonly STATE_DIR="${POOL_PRESSURE_STATE_DIR:-${XDG_STATE_HOME:-${HOME}/.local/state}/pool-pressure}"
readonly WINDOW_DAYS="${POOL_PRESSURE_WINDOW_DAYS:-7}"
readonly COOLDOWN_HOURS="${POOL_PRESSURE_COOLDOWN_HOURS:-6}"
readonly HISTORY_DAYS="${POOL_PRESSURE_HISTORY_DAYS:-90}"
readonly NOTIFY_COMMAND="${POOL_PRESSURE_NOTIFY_COMMAND:-}"

# Mirrors scripts/pool_pressure.py. Kept as names because the branching below
# reads better for it, and because 2 meaning "could not measure" is the whole
# point of the exit-code taxonomy.
readonly EXIT_OK=0
readonly EXIT_BREACH=1
readonly EXIT_UNMEASURED=2

readonly SECONDS_PER_HOUR=3600

# The state file records nothing sensitive, but it is this user's alert state
# and nobody else's business.
readonly STATE_MODE_OWNER_ONLY=600

usage() {
  cat <<'USAGE'
pool_pressure_cron.sh -- run the pool-pressure check on a schedule and notify.

Environment:
  POOL_PRESSURE_NOTIFY_COMMAND  Shell command run when a notification is due.
                                The rendered report arrives on stdin. Unset
                                means log only -- useful for the first few days.
  POOL_PRESSURE_STATE_DIR       Where history and state live.
                                Default: ${XDG_STATE_HOME:-~/.local/state}/pool-pressure
  POOL_PRESSURE_WINDOW_DAYS     Days of history the check measures. Default: 7
  POOL_PRESSURE_COOLDOWN_HOURS  Minimum gap between two notifications about the
                                same ongoing condition. 0 notifies every run.
                                Default: 6
  POOL_PRESSURE_HISTORY_DAYS    Days of past reports to keep. Default: 90
  POOL_PRESSURE_PYTHON          Python interpreter. Default: python3

Passed to the notify command in its environment:
  POOL_PRESSURE_SUBJECT     One line, ready to use as an email subject.
  POOL_PRESSURE_VERDICT     OK | BREACH | UNMEASURED
  POOL_PRESSURE_CAUSE       CAPACITY | CONCURRENCY_CAP | CONTROL_PLANE | UNKNOWN
                            Only CAPACITY justifies onboarding a project.
                            NONE when the verdict is OK -- nothing is waiting,
                            so there is nothing to diagnose.
  POOL_PRESSURE_REASON      new | reminder | recovered
  POOL_PRESSURE_EXIT_CODE   The check's exit code.
  POOL_PRESSURE_JSON        Path to the full JSON payload for this run.
  POOL_PRESSURE_REPORT      Path to the rendered report for this run.

Any further arguments are passed through to pool_pressure.py, so
`--boskos-via none` or `--p95-threshold-minutes 30` work here too.

Exit status:
  0   The run completed and any notification was delivered.
  1   The check could not be run, or the notify command failed. Either one
      hides a breach, so it is reported as a failure of this script rather
      than logged and forgotten.

systemd (~/.config/systemd/user/), then
`systemctl --user enable --now pool-pressure.timer`:

  # pool-pressure.service
  [Unit]
  Description=Measure how long CI runs wait for an evals project

  [Service]
  Type=oneshot
  Environment=POOL_PRESSURE_NOTIFY_COMMAND=/path/to/your/sender
  ExecStart=/path/to/kube-agents/hack/pool_pressure_cron.sh

  # pool-pressure.timer
  [Unit]
  Description=Hourly pool-pressure check

  [Timer]
  OnCalendar=hourly
  Persistent=true
  RandomizedDelaySec=5m

  [Install]
  WantedBy=timers.target
USAGE
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ ! -f "${CHECK}" ]]; then
  echo "pool_pressure_cron.sh: cannot find the check at ${CHECK}" >&2
  exit 1
fi

mkdir -p "${STATE_DIR}/history"
STATE_FILE="${STATE_DIR}/state"
LOG_FILE="${STATE_DIR}/pool-pressure.log"

NOW_EPOCH="$(date -u +%s)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
JSON_FILE="${STATE_DIR}/history/${STAMP}.json"
REPORT_FILE="${STATE_DIR}/history/${STAMP}.txt"

log() {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >>"${LOG_FILE}"
}

# The check exits non-zero to report a breach, which is a result and not an
# error, so `set -e` must not see it.
CHECK_STATUS=0
"${PYTHON}" "${CHECK}" --window-days "${WINDOW_DAYS}" --json "$@" >"${JSON_FILE}" 2>>"${LOG_FILE}" \
  || CHECK_STATUS=$?

if [[ "${CHECK_STATUS}" -ne "${EXIT_OK}" && "${CHECK_STATUS}" -ne "${EXIT_BREACH}" \
   && "${CHECK_STATUS}" -ne "${EXIT_UNMEASURED}" ]]; then
  log "the check itself failed with exit ${CHECK_STATUS}; see above"
  echo "pool_pressure_cron.sh: the check failed with exit ${CHECK_STATUS}" >&2
  tail -n 20 "${LOG_FILE}" >&2
  rm -f "${JSON_FILE}"
  exit 1
fi

# One python call rather than four: reading the payload once and printing the
# fields as shell assignments keeps them consistent with each other even if the
# file is replaced mid-run.
FIELDS="$("${PYTHON}" - "${JSON_FILE}" "${REPORT_FILE}" <<'PY'
import json, shlex, sys

payload = json.load(open(sys.argv[1]))
open(sys.argv[2], "w").write(payload["report"] + "\n")

trend = payload["trend"]
queue = payload["queue"]
verdict = payload["verdict"]
# Absent on a green run: nothing is waiting, so nothing is diagnosed.
cause = payload["cause"] or "NONE"

if verdict == "BREACH":
    detail = f"p95 {trend['p95_minutes']}m over {trend['runs']} run(s)"
    waiting = queue.get("over_threshold") or 0
    if waiting:
        detail += f", {waiting} waiting now"
    subject = f"[pool-pressure] BREACH ({cause}) -- {detail}"
elif verdict == "UNMEASURED":
    subject = "[pool-pressure] COULD NOT MEASURE -- the queue wait was not read"
else:
    subject = f"[pool-pressure] OK -- p95 {trend['p95_minutes']}m over {trend['runs']} run(s)"

for key, value in (
    ("VERDICT", verdict),
    ("CAUSE", cause),
    ("SUBJECT", subject),
    ("SUMMARY", f"{verdict} {cause} p95={trend['p95_minutes']} runs={trend['runs']}"),
):
    print(f"{key}={shlex.quote(str(value))}")
PY
)"
eval "${FIELDS}"

log "${SUMMARY}"

PREVIOUS_VERDICT=""
LAST_NOTIFIED=0
if [[ -f "${STATE_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${STATE_FILE}"
  PREVIOUS_VERDICT="${STATE_VERDICT:-}"
  LAST_NOTIFIED="${STATE_LAST_NOTIFIED:-0}"
fi

# Notify on a change of state, and while a bad state persists, no more often
# than the cooldown. Without the cooldown an hourly timer mails hourly for as
# long as the queue is long, which is exactly when the mail stops being read.
REASON=""
if [[ "${VERDICT}" != "OK" ]]; then
  if [[ "${VERDICT}" != "${PREVIOUS_VERDICT}" ]]; then
    REASON="new"
  elif [[ $(( (NOW_EPOCH - LAST_NOTIFIED) / SECONDS_PER_HOUR )) -ge "${COOLDOWN_HOURS}" ]]; then
    REASON="reminder"
  fi
elif [[ -n "${PREVIOUS_VERDICT}" && "${PREVIOUS_VERDICT}" != "OK" ]]; then
  REASON="recovered"
  SUBJECT="[pool-pressure] recovered -- queue wait is back within threshold"
fi

NOTIFY_STATUS=0
if [[ -n "${REASON}" ]]; then
  if [[ -z "${NOTIFY_COMMAND}" ]]; then
    log "would notify (${REASON}): ${SUBJECT} -- POOL_PRESSURE_NOTIFY_COMMAND is unset"
  else
    log "notifying (${REASON}): ${SUBJECT}"
    POOL_PRESSURE_SUBJECT="${SUBJECT}" \
    POOL_PRESSURE_VERDICT="${VERDICT}" \
    POOL_PRESSURE_CAUSE="${CAUSE}" \
    POOL_PRESSURE_REASON="${REASON}" \
    POOL_PRESSURE_EXIT_CODE="${CHECK_STATUS}" \
    POOL_PRESSURE_JSON="${JSON_FILE}" \
    POOL_PRESSURE_REPORT="${REPORT_FILE}" \
      bash -c "${NOTIFY_COMMAND}" <"${REPORT_FILE}" >>"${LOG_FILE}" 2>&1 || NOTIFY_STATUS=$?
    if [[ "${NOTIFY_STATUS}" -ne 0 ]]; then
      log "the notify command failed with exit ${NOTIFY_STATUS}"
    else
      LAST_NOTIFIED="${NOW_EPOCH}"
    fi
  fi
  if [[ -z "${NOTIFY_COMMAND}" ]]; then
    LAST_NOTIFIED="${NOW_EPOCH}"
  fi
fi

cat >"${STATE_FILE}" <<STATE
STATE_VERDICT=${VERDICT}
STATE_LAST_NOTIFIED=${LAST_NOTIFIED}
STATE
chmod "${STATE_MODE_OWNER_ONLY}" "${STATE_FILE}"

find "${STATE_DIR}/history" -type f -mtime "+${HISTORY_DAYS}" -delete 2>/dev/null || true

if [[ "${NOTIFY_STATUS}" -ne 0 ]]; then
  echo "pool_pressure_cron.sh: ${SUBJECT}" >&2
  echo "pool_pressure_cron.sh: the notification could NOT be delivered." >&2
  cat "${REPORT_FILE}" >&2
  exit 1
fi

# The report goes to stdout so that a cron or journald record of the run holds
# it, whether or not a notification was due.
cat "${REPORT_FILE}"
exit 0
