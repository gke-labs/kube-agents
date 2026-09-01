#!/usr/bin/env bash
# Chooses which of the two ways into release-publish.yml a run is taking, and
# produces the `should_release` verdict the publish job is gated on.
#
# A scheduled run has no human behind it, so the verdict comes from
# resolve_scheduled_release.sh. A manual dispatch does, and that human clicking
# "Run workflow" *is* the gate — so by default it short-circuits to `true`,
# emergency path included.
#
# The two remaining modes make the unattended verdict reachable on demand, which
# is the only way to see what the cron will decide without waiting a week for it
# to decide it:
#
#   bypass    (default) A human decided. Publishes.
#   dry-run   Run the resolver, report its verdict, publish nothing. What the
#             cron would do, with the consequences left off.
#   evaluate  Run the resolver and honour it. Exactly a cron tick, on demand.
#
# On a `schedule` event the mode is `evaluate` whatever the input says — inputs
# are empty there anyway, and pinning it means a default edited later cannot
# quietly turn the cron into a dry run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

EVENT_NAME="${EVENT_NAME:-workflow_dispatch}"
MODE="${SCHEDULE_GATE:-bypass}"
TARGET_COMMIT="${TARGET_COMMIT:-}"

if [ "${EVENT_NAME}" = "schedule" ]; then
  MODE="evaluate"
fi

# The two resolver-consulting modes answer "what would the cron do", and the cron
# has no commit to name: the resolver picks its own from the tag graph. Naming one
# alongside them asks two different questions and acts on the answers to both —
# under `evaluate` the publish job's TARGET_COMMIT prefers the input, so the run
# would publish a commit whose range condition 3 never scanned, breaking change and
# all. Refuse rather than pick a winner. An emergency dispatch naming a commit is
# `bypass`, which is the default and unaffected.
if [ -n "${TARGET_COMMIT}" ] && { [ "${MODE}" = "evaluate" ] || [ "${MODE}" = "dry-run" ]; }; then
  echo "❌ ERROR: schedule_gate '${MODE}' decides which commit to release from the tag graph;" >&2
  echo "   target_commit '${TARGET_COMMIT}' cannot be set alongside it. Use schedule_gate" >&2
  echo "   'bypass' to release a named commit, or clear target_commit to run the gate." >&2
  exit 1
fi

emit() {
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    printf '%s\n' "$@" >> "${GITHUB_OUTPUT}"
  fi
}

case "${MODE}" in
  bypass)
    echo "Manual dispatch — the decision to release has already been made."
    # release_commit is deliberately empty: on this path the publish job falls
    # back to `inputs.target_commit`, or to the scripts' own resolution. A
    # commit named here would override the one the dispatcher asked for.
    emit "should_release=true" "release_commit=" "gate_tag=" "skip_reason="
    if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
      {
        echo "### Publishing on a manual dispatch"
        echo ""
        echo "The scheduled gate was bypassed; a human is the gate on this path."
      } >> "${GITHUB_STEP_SUMMARY}"
    fi
    ;;

  evaluate)
    # The resolver writes should_release, release_commit, gate_tag and
    # skip_reason straight to GITHUB_OUTPUT, and exits non-zero only to halt.
    "${SCRIPT_DIR}/resolve_scheduled_release.sh"
    ;;

  dry-run)
    # Same resolver, same inputs, but its outputs land in a scratch file so the
    # verdict is reported without reaching the publish job. The exit code is
    # preserved: a dry run that halts goes red, because that is what the cron
    # would do and seeing it is the point.
    scratch="$(mktemp)"
    trap 'rm -f "${scratch}"' EXIT

    # Ahead of the resolver rather than after it: the resolver's own summary
    # opens with "Releasing <sha>", and a reader who meets that first has to get
    # to the end before learning it did not happen.
    if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
      {
        echo "## Dry run — nothing will be published"
        echo ""
        echo "The verdict below is what a scheduled run would decide. Re-run with"
        echo "\`schedule_gate: evaluate\` to act on it."
        echo ""
      } >> "${GITHUB_STEP_SUMMARY}"
    fi

    rc=0
    GITHUB_OUTPUT="${scratch}" "${SCRIPT_DIR}/resolve_scheduled_release.sh" || rc=$?

    echo "----------------------------------------------------------------------"
    echo "DRY RUN — the verdict above is reported, not acted on."
    echo "Resolver outputs:"
    cat "${scratch}"
    echo "----------------------------------------------------------------------"

    # should_release is forced false rather than passed through: a dry run must
    # publish nothing even when the resolver says it would.
    emit "should_release=false"
    grep -E '^(release_commit|gate_tag|skip_reason)=' "${scratch}" >> "${GITHUB_OUTPUT:-/dev/null}" || true

    exit "${rc}"
    ;;

  *)
    echo "❌ ERROR: Unknown schedule_gate mode '${MODE}'. Expected bypass, dry-run or evaluate." >&2
    exit 1
    ;;
esac
