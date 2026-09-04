#!/usr/bin/env bash
# ==============================================================================
# Eval dashboard hourly refresh (the periodic job's entrypoint)
# ==============================================================================
# collect -> render -> publish for the live eval dashboard, INCREMENTALLY:
# download the currently published data.json, hand it to collect.py's
# --merge-with so the GCS scan only reads builds newer than the newest one on
# record, then re-render and republish. A cold sweep is ~3 serial gsutil calls
# per archived build -- 30-45 minutes over two weeks of history -- so the
# watermark is what makes an hourly cadence possible at all; only the first
# armed run (no data.json published yet) pays a sweep, and even that one is
# bounded to EVAL_DASHBOARD_SINCE_DAYS (default 14).
#
# This is a companion to publish_eval_dashboard in hack/ci-eval-pr.sh, with
# the OPPOSITE failure posture. The hook rides the eval job's EXIT trap, so it
# must never change that job's exit code and every failure collapses to one
# skip line. This script IS the job: a red periodic is the freshness alert,
# so past the dormancy/trust gates below every failure is `set -euo pipefail`
# loud, with the stage log preserved to $ARTIFACTS.
#
# What it shares with the hook, deliberately:
#   * DORMANCY: EVAL_DASHBOARD_TARGET unset -> one skip line, exit 0. The job
#     config arms the script; until the companion oss-test-infra PR exports
#     the target this file is inert wherever it runs.
#   * TRUST BOUNDARY: a pull request never writes the dashboard everyone
#     reads (a presubmit runs branch-authored code, and collect.py derives
#     `active` and coverage from THIS checkout). A gs:// target additionally
#     requires the main-branch job shape (JOB_TYPE periodic/postsubmit, no
#     PULL_NUMBER). A local-directory target has no trust boundary to cross,
#     which is what lets the unit tests and a laptop run the whole pipeline.
#   * ZERO-RUNS FLOOR between collect and render: collect.py warns and
#     continues when a listing fails, so a total source outage still yields a
#     well-formed document -- and in merge mode an empty result additionally
#     means the prior file was unusable AND the sweep found nothing.
#     Publishing that would overwrite a good dashboard with an empty one.
#   * One EVAL_DASHBOARD_TIMEOUT (default 900s) over the whole pipeline, with
#     the no-`timeout`-binary degrade-to-unbounded idiom, and the stage log
#     copied to $ARTIFACTS on success AND failure.
#
# Environment:
#   EVAL_DASHBOARD_TARGET      gs://bucket/path or a local directory. Unset =
#                              dormant. (Live: gs://kube-agents-dashboards/evals/)
#   EVAL_DASHBOARD_PR_GLOB     Prow build-dir glob(s) for collect.py; default
#                              below is the smoke-test presubmit's archive.
#   EVAL_DASHBOARD_SINCE_DAYS  sweep bound when no usable prior data exists
#   EVAL_DASHBOARD_STALE_AFTER_S  freshness-badge threshold written into
#                              data.json (default 2400 = 15m cadence x ~2.5)
#                              (default 14).
#   EVAL_DASHBOARD_TIMEOUT     whole-pipeline budget in seconds (default 900).
#   EVAL_DASHBOARD_FROM_DIR    local build-dir source instead of the GCS glob
#                              -- the offline path the unit tests use.
#   JOB_TYPE / PULL_NUMBER     Prow's; gate bucket writes as above.
#   ARTIFACTS                  when set, receives eval-dashboard-refresh.log.
#
# The path of this file is a CONTRACT: the periodic job in oss-test-infra
# invokes hack/ci-dashboard-refresh.sh by name. Do not rename it.
# ==============================================================================

set -euo pipefail

# The artifact-log filename cleanup() copies to $ARTIFACTS and the failure
# message points readers at; named once so the two mentions cannot drift.
readonly REFRESH_LOG_NAME="eval-dashboard-refresh.log"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASH_SRC="${SCRIPT_DIR}/../scripts/eval_dashboard"

# ─── Dormancy and trust gates (exit 0: nothing to do, or must not do it) ────
if [ -z "${EVAL_DASHBOARD_TARGET:-}" ]; then
  echo "eval-dashboard refresh skipped: EVAL_DASHBOARD_TARGET is not set (the Prow job config arms this later)"
  exit 0
fi
if [ -n "${PULL_NUMBER:-}" ]; then
  echo "eval-dashboard refresh skipped: PULL_NUMBER=${PULL_NUMBER} is set: a pull request never writes the dashboard"
  exit 0
fi
case "${EVAL_DASHBOARD_TARGET}" in
  gs://*)
    case "${JOB_TYPE:-}" in
      periodic | postsubmit) ;;
      *)
        echo "eval-dashboard refresh skipped: JOB_TYPE=${JOB_TYPE:-unset} may not write a bucket dashboard (only a main-branch periodic/postsubmit publishes)"
        exit 0
        ;;
    esac
    ;;
esac

WORK="$(mktemp -d)"
REFRESH_LOG="${WORK}/refresh.log"
: >"${REFRESH_LOG}"

# The stage log rides to Prow on success AND failure: collect.py's per-build
# fetch errors are warnings, and they are the only after-the-fact evidence
# that a published dashboard came from a partial sweep. `set +e` first --
# errexit stays live inside an EXIT trap and would truncate it (see the trap
# discussion in hack/ci-eval-pr.sh); no explicit exit, so the shell keeps the
# status it was already exiting with.
cleanup() {
  set +e
  if [ -n "${ARTIFACTS:-}" ] && [ -d "${ARTIFACTS}" ]; then
    cp "${REFRESH_LOG}" "${ARTIFACTS}/${REFRESH_LOG_NAME}" 2>/dev/null || true
  fi
  rm -rf "${WORK}" || true
}
trap cleanup EXIT
# A Prow deadline delivers SIGTERM, which does not run the EXIT trap on its
# own; converting it to an exit is what preserves the log above.
trap 'exit 143' TERM INT

EVAL_DASHBOARD_PR_GLOB="${EVAL_DASHBOARD_PR_GLOB:-gs://kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/*/pull-kube-agents-smoke-test/*}"
EVAL_DASHBOARD_SINCE_DAYS="${EVAL_DASHBOARD_SINCE_DAYS:-14}"
# Freshness contract with the rendered page: the badge turns amber this many
# seconds after generated_at. Sized to the periodic's 15m cadence with slack
# for ~2 missed ticks, so amber means "the refresh job is missing ticks",
# not ordinary jitter.
EVAL_DASHBOARD_STALE_AFTER_S="${EVAL_DASHBOARD_STALE_AFTER_S:-2400}"

# ─── Step 1: fetch the currently published data.json (missing is fine) ──────
# Downloaded here rather than left to collect.py's gs:// --merge-with support
# so the artifact log shows exactly which prior document this run merged
# into. A failed or partial download is removed and collect.py degrades to
# the bounded fresh sweep -- slower, still correct, self-heals next hour.
PRIOR="${WORK}/prior-data.json"
PRIOR_SRC="${EVAL_DASHBOARD_TARGET%/}/data.json"
case "${EVAL_DASHBOARD_TARGET}" in
  gs://*)
    if ! gsutil cp "${PRIOR_SRC}" "${PRIOR}" >>"${REFRESH_LOG}" 2>&1; then
      rm -f "${PRIOR}"
      echo "no existing data.json at ${PRIOR_SRC} (first armed run, or a transient read failure): falling back to a fresh sweep bounded to ${EVAL_DASHBOARD_SINCE_DAYS} days"
    fi
    ;;
  *)
    if [ -f "${PRIOR_SRC}" ]; then
      cp "${PRIOR_SRC}" "${PRIOR}"
    else
      echo "no existing data.json at ${PRIOR_SRC} (first run against this directory)"
    fi
    ;;
esac

# ─── Steps 2-5: collect (incremental) -> floor -> render -> publish ─────────
# One timeout over the whole pipeline so a hung gsutil cannot eat the job.
# The budget must stay LARGER than the 300s collect.py grants each individual
# gsutil call (same arithmetic as the hook); 900s covers the incremental path
# with room, and EVAL_DASHBOARD_TIMEOUT raises it from the job config for the
# first-run sweep if that ever needs it. No `timeout` binary (a laptop)
# degrades to running unbounded.
BUDGET="${EVAL_DASHBOARD_TIMEOUT:-900}"
TIMEOUT_CMD=(timeout "${BUDGET}")
command -v timeout >/dev/null 2>&1 || TIMEOUT_CMD=()

# Single quotes on purpose: $1..$7 are the child bash's own positionals, so
# no value ever meets an outer expansion. --merge-with always points at the
# prior path; when the download above left nothing there, collect.py treats
# it as a first run and bounds the sweep itself.
rc=0
# shellcheck disable=SC2016
${TIMEOUT_CMD[@]+"${TIMEOUT_CMD[@]}"} bash -c '
  set -euo pipefail
  if [ -n "$6" ]; then
    src_args=(--from-dir "$6")
  else
    src_args=(--pr-glob "$4")
  fi
  python3 "$1/collect.py" "${src_args[@]}" \
    --merge-with "$2/prior-data.json" \
    --since-days "$5" \
    --stale-after-s "$7" \
    --out "$2/data.json"
  python3 -c "
import json, sys
if not json.load(open(sys.argv[1], encoding=\"utf-8\")).get(\"runs\"):
    sys.exit(\"collected zero runs: source unreadable or empty; refusing to publish an empty dashboard over a good one\")
" "$2/data.json"
  python3 "$1/render.py" --data "$2/data.json" --out-dir "$2/site"
  python3 "$1/publish.py" --out-dir "$2/site" --target "$3"
' _ "${DASH_SRC}" "${WORK}" "${EVAL_DASHBOARD_TARGET}" "${EVAL_DASHBOARD_PR_GLOB}" \
  "${EVAL_DASHBOARD_SINCE_DAYS}" "${EVAL_DASHBOARD_FROM_DIR:-}" \
  "${EVAL_DASHBOARD_STALE_AFTER_S}" \
  >>"${REFRESH_LOG}" 2>&1 || rc=$?

# The full stage log always goes to stdout too: on a periodic, the build log
# is where a red run gets read first, and $ARTIFACTS may not be set locally.
cat "${REFRESH_LOG}"

if [ "${rc}" -ne 0 ]; then
  if [ "${rc}" -eq 124 ]; then
    echo "ERROR: eval-dashboard refresh timed out after ${BUDGET}s (EVAL_DASHBOARD_TIMEOUT raises it; a first run with no prior data.json can need a full sweep)" >&2
  else
    echo "ERROR: eval-dashboard refresh pipeline exited ${rc}; see the stage log above (also ${ARTIFACTS:+${ARTIFACTS}/}${REFRESH_LOG_NAME})" >&2
  fi
  exit "${rc}"
fi
echo "eval-dashboard: refreshed ${EVAL_DASHBOARD_TARGET}"
