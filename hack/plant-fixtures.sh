#!/usr/bin/env bash
# ==============================================================================
# Per-case fixture planting, at run time, in the leased project
# ==============================================================================
# Some fixtures cannot be pre-planted into the standing seeded fleet
# (bench/tf/fleet/) and cannot justify a whole GKE cluster either. Cloud Logging
# entries are the motivating shape: gpu-stress-test-diagnosis's entire planted
# incident is two `gcloud logging write` calls, the entries expire on a
# retention window, and nothing in the fleet's kubectl-shaped probe syntax can
# confirm they are still there -- so pre-planting them into a fleet that is
# never torn down would go quietly wrong about a month later, and the checks
# would blame the agent.
#
# Before this hook there were exactly two options and neither fits:
#
#   deployer: tofu  provisions a per-run GKE cluster. evalharness/default.py
#                   calls deployer.get_cluster_info() unconditionally for any
#                   non-noop deployer and TFDeployer errors without
#                   cluster_name/cluster_location outputs, so "OpenTofu but no
#                   cluster" is not a reachable state. A case that needs two
#                   log lines pays for a cluster and its teardown.
#   deployer: noop  skips OpenTofu entirely, which is the right answer for a
#                   case that reads the standing fleet -- and plants nothing.
#
# This is the third: a shell script beside the task, run in the leased project,
# before that case's eval, whatever its deployer says.
#
# THE CONVENTION
#
#   bench/tasks/<case>/plant.sh
#
# Derived from the task's own directory, so there is no registry to forget.
# `TASKS` in ci-eval-pr.sh already has to name every case; a second list saying
# which of them plant would be a second thing to get wrong, and the failure
# mode of forgetting it is the silent one -- an eval graded against a fixture
# that was never created. The name is fixed rather than declared in task.yaml
# for the same reason: a `plant:` key is a key someone can typo into a case
# that then plants nothing and still runs.
#
# The file is executed with `bash`, not by its shebang, so a lost executable
# bit cannot turn a plant into a no-op. It must therefore be bash.
# bench/tests/test_plant_hook.py fails the build on a task directory holding
# any other `plant*` file -- `plant.bash`, `setup.sh`, `plant.py` -- because a
# misnamed plant script is discovered by nothing and reads as green.
#
# WHAT THE SCRIPT RECEIVES
#
# The job's environment, plus:
#
#   PROJECT_ID         the Boskos-leased project. Everything the plant creates
#                      must be scoped to it; the hook refuses to run at all if
#                      it is empty, rather than let a plant land wherever
#                      gcloud's default project happens to point.
#   TASK_NAME          the case's directory name
#   TASK_DIR           absolute path to the case's directory; also the cwd
#   PLANT_SCRATCH_DIR  a private writable directory, removed afterwards
#   KUBECONFIG         an EMPTY file (see below)
#
# and minus:
#
#   BENCH_FLEET_KUBECONFIG_DIR   the seeded fleet's credentials
#   PLATFORM_AGENT_TOKEN         the agent's bearer token
#   JUDGE_API_KEY / GEMINI_API_KEY   the judge's model key
#
# A plant needs a project and a cloud credential. It has no business holding
# the key that grades the run.
#
# IT MUST NOT WRITE TO THE SEEDED FLEET
#
# The fleet is standing and never torn down, so one plant that mutates a shared
# cluster poisons every later run in that project -- and the run that broke it
# is long gone by the time a check notices. Two mechanisms, because the runtime
# one cannot be complete:
#
#   Enforced. KUBECONFIG points at an empty file inside a private scratch
#   directory, so `kubectl` in a plant script reaches no cluster at all --
#   neither a fleet cluster nor platform-agent-host, whose credentials the
#   ambient ~/.kube/config holds. BENCH_FLEET_KUBECONFIG_DIR is removed from
#   the environment, so the fleet's per-role credentials are not addressable
#   either.
#
#   Linted. A plant script could still call `gcloud container clusters
#   get-credentials` and write itself a kubeconfig, and nothing at run time can
#   stop that -- the runner's identity holds roles/container.admin on the
#   project (bench/tasks/DRAFTS.md, A5). So `kubectl` and `get-credentials` are
#   both rejected in a plant.sh by bench/tests/test_plant_hook.py, at pull
#   request time, with the reason. A case that genuinely needs to plant INTO a
#   cluster needs a cluster of its own, which is `deployer: tofu` and its
#   stack -- and note this hook runs BEFORE devops-bench, so a per-run cluster
#   does not exist yet when a plant runs. Plant project-scoped things here:
#   Cloud Logging, GCS, Pub/Sub, Monitoring.
#
# IDEMPOTENCY IS THE SCRIPT'S JOB
#
# Boskos re-leases projects, so a plant runs in projects earlier runs already
# planted, and a retried Prow job replants within minutes. The hook
# de-duplicates nothing and keeps no state between runs: a plant script must
# create-if-absent, tolerate "already exists" on every create, and never assume
# a clean project. Append-only sinks are the easy case -- a second
# `gcloud logging write` adds a second entry, and a check that matches on
# content rather than on count does not care. Anything named and singular
# (a bucket, a topic, a sink) must be written as create-or-adopt.
#
# Usage:
#   source hack/plant-fixtures.sh
#   plant_task_fixtures <task-dir> <task-name>
#
# Sets PLANT_STATUS (absent|planted|failed|timeout|unusable) and PLANT_LOG.
# Returns 0 when there was nothing to plant or the plant succeeded, 1 when the
# plant failed, 124 when it ran out of budget, 2 when the hook itself could not
# run it. Everything it says goes to stderr; the plant's own output goes to the
# log.
# ==============================================================================

# Fixed by convention. See "THE CONVENTION" above before changing it: the value
# is a contract with every bench/tasks/<case>/ directory, not a setting.
_PLANT_SCRIPT_NAME="plant.sh"

# Seconds. The Prow job's whole budget is 85 minutes and it evaluates several
# cases inside it, so a hung plant must be bounded well below the point where
# it costs the job an eval.
#
# 300s is ~6% of that budget and roughly two orders of magnitude above the
# motivating plant, which is two `gcloud logging write` calls -- one API round
# trip each. It leaves room for a handful of gcloud calls that each retry
# through a slow control plane, and it does not leave room for provisioning.
# A plant that wants longer is not a plant: it is infrastructure, and
# infrastructure belongs in an OpenTofu stack the deployer owns and tears down.
#
# BENCH_PLANT_TIMEOUT_SECONDS overrides it for a local experiment. A value that
# is not a positive integer is a warning and the default, not a dead job.
_PLANT_DEFAULT_TIMEOUT=300

# How much of a failed plant's log is echoed into the job log. The artifact has
# all of it; this is so the reason is on screen in Prow next to the red line
# rather than one artifact download away.
_PLANT_LOG_TAIL_LINES=40

_plant_budget_seconds() {
  local raw="${BENCH_PLANT_TIMEOUT_SECONDS:-}"
  if [ -z "$raw" ]; then
    printf '%s' "$_PLANT_DEFAULT_TIMEOUT"
    return 0
  fi
  case "$raw" in
    '' | *[!0-9]*)
      echo "WARNING: BENCH_PLANT_TIMEOUT_SECONDS='${raw}' is not a positive integer; using ${_PLANT_DEFAULT_TIMEOUT}s" >&2
      printf '%s' "$_PLANT_DEFAULT_TIMEOUT"
      return 0
      ;;
  esac
  if [ "$raw" -le 0 ]; then
    echo "WARNING: BENCH_PLANT_TIMEOUT_SECONDS='${raw}' is not a positive integer; using ${_PLANT_DEFAULT_TIMEOUT}s" >&2
    printf '%s' "$_PLANT_DEFAULT_TIMEOUT"
    return 0
  fi
  printf '%s' "$raw"
}

# Run a command under a wall-clock bound, returning 124 when it ran out --
# coreutils' convention, kept on every path so the caller has one code to read.
#
# GNU `timeout` when the image has it, which the Prow runner does. The bash
# watchdog below is for a developer laptop without coreutils, and it is a real
# implementation rather than a warning-and-run-unbounded: "no timeout here"
# reintroduces exactly the hang this exists to bound, on the machine where a
# new plant script is being written and is most likely to hang.
#
# The watchdog's one imprecision: a plant that exits 143 (SIGTERM) or 137
# (SIGKILL) of its own accord is reported as a timeout. Both are failures
# either way, only the label differs, and only on the fallback path.
_plant_run_bounded() {
  local secs="$1"
  shift
  local rc=0

  if command -v timeout >/dev/null 2>&1; then
    timeout --kill-after=30s "${secs}s" "$@" || rc=$?
    return "$rc"
  fi
  if command -v gtimeout >/dev/null 2>&1; then
    gtimeout --kill-after=30s "${secs}s" "$@" || rc=$?
    return "$rc"
  fi

  "$@" &
  local child=$!
  (
    sleep "$secs"
    kill -TERM "$child" 2>/dev/null || exit 0
    sleep 30
    kill -KILL "$child" 2>/dev/null || exit 0
  ) >/dev/null 2>&1 &
  local watchdog=$!
  wait "$child" || rc=$?
  kill -TERM "$watchdog" 2>/dev/null || true
  wait "$watchdog" 2>/dev/null || true
  if [ "$rc" -eq 143 ] || [ "$rc" -eq 137 ]; then
    rc=124
  fi
  return "$rc"
}

# Out-parameters. The caller reads both after every call, and the return code
# alone cannot carry them: "failed" and "timed out" are different lines in the
# job log, and the log path is what a reader needs next.
# shellcheck disable=SC2034  # read by ci-eval-pr.sh, not by this file
PLANT_STATUS=""
# shellcheck disable=SC2034  # read by ci-eval-pr.sh, not by this file
PLANT_LOG=""

# The hook. Called once per case, from the task loop in ci-eval-pr.sh, before
# that case's eval.
plant_task_fixtures() {
  local task_dir="$1" task_name="$2"
  PLANT_STATUS="absent"
  PLANT_LOG=""

  local script="${task_dir}/${_PLANT_SCRIPT_NAME}"
  # The no-plant path, which is almost every case: one stat, no subprocess, no
  # directory created, nothing exported. A case without a plant script must be
  # indistinguishable from one running before this hook existed.
  [ -f "$script" ] || return 0

  # A plant with no project would create its fixture wherever gcloud's default
  # project points -- someone's personal project, or the last one a developer
  # configured. Refuse instead, and red the case: "scoped to the leased
  # project" is not a comment, it is the only thing making a plant safe to run.
  if [ -z "${PROJECT_ID:-}" ]; then
    PLANT_STATUS="unusable"
    echo "ERROR: ${task_name} carries ${_PLANT_SCRIPT_NAME} but PROJECT_ID is empty; refusing to plant into an unnamed project" >&2
    return 2
  fi

  local artifact_dir="${ARTIFACTS:-/tmp/artifacts}"
  if ! mkdir -p "$artifact_dir"; then
    PLANT_STATUS="unusable"
    echo "ERROR: could not create ${artifact_dir} to log ${task_name}'s plant" >&2
    return 2
  fi
  PLANT_LOG="${artifact_dir}/plant_${task_name}.log"

  local scratch
  if ! scratch="$(mktemp -d "${TMPDIR:-/tmp}/kube-agents-plant-XXXXXX")"; then
    PLANT_STATUS="unusable"
    echo "ERROR: could not create a scratch directory for ${task_name}'s plant" >&2
    return 2
  fi
  chmod 700 "$scratch"
  mkdir -p "${scratch}/work"
  # Empty, and present: kubectl reads KUBECONFIG in preference to
  # ~/.kube/config, so an empty file is what makes the host cluster's ambient
  # credential unreachable. Absent would fall back.
  : >"${scratch}/kubeconfig"

  local budget
  budget="$(_plant_budget_seconds)"
  local start=$SECONDS

  {
    echo "=== plant ${task_name}"
    echo "=== script  ${script}"
    echo "=== project ${PROJECT_ID}"
    echo "=== budget  ${budget}s"
    echo "=== started $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    echo "==="
  } >"$PLANT_LOG"

  # `env -u` removes; `env NAME=value` adds. Everything else -- PATH, HOME,
  # gcloud's application default credentials, CLOUDSDK_* -- is inherited,
  # because a plant is a gcloud script and stripping the environment to a
  # whitelist would break it in ways nobody could debug from a Prow log.
  #
  # The trailing `bash -c` exists only to chdir into the case's directory
  # before exec'ing the plant, so a relative path beside the script resolves
  # the way its author expects; the cd happens in the child, and the hook's own
  # cwd is untouched. Its `$1`/`$2` are the CHILD's positional parameters,
  # supplied after the -c string, so the single quotes are the point.
  local rc=0
  # shellcheck disable=SC2016
  _plant_run_bounded "$budget" \
    env -u BENCH_FLEET_KUBECONFIG_DIR \
    -u PLATFORM_AGENT_TOKEN \
    -u JUDGE_API_KEY \
    -u GEMINI_API_KEY \
    KUBECONFIG="${scratch}/kubeconfig" \
    PLANT_SCRATCH_DIR="${scratch}/work" \
    TASK_NAME="${task_name}" \
    TASK_DIR="${task_dir}" \
    bash -c 'cd "$1" || exit 2; exec bash "$2"' plant-hook "$task_dir" "$_PLANT_SCRIPT_NAME" \
    >>"$PLANT_LOG" 2>&1 || rc=$?

  local elapsed=$((SECONDS - start))
  rm -rf "$scratch"

  if [ "$rc" -eq 0 ]; then
    PLANT_STATUS="planted"
    echo "=== finished ok in ${elapsed}s" >>"$PLANT_LOG"
    echo "Fixture plant for ${task_name}: planted in ${PROJECT_ID} (${elapsed}s, log ${PLANT_LOG})" >&2
    return 0
  fi

  if [ "$rc" -eq 124 ]; then
    PLANT_STATUS="timeout"
    echo "=== KILLED after ${elapsed}s: exceeded the ${budget}s plant budget" >>"$PLANT_LOG"
    echo "ERROR: fixture plant for ${task_name} exceeded its ${budget}s budget and was killed after ${elapsed}s (project ${PROJECT_ID})" >&2
  else
    # shellcheck disable=SC2034  # read by ci-eval-pr.sh, not by this file
    PLANT_STATUS="failed"
    echo "=== FAILED after ${elapsed}s: exit ${rc}" >>"$PLANT_LOG"
    echo "ERROR: fixture plant for ${task_name} exited ${rc} after ${elapsed}s (project ${PROJECT_ID})" >&2
  fi
  echo "--- last ${_PLANT_LOG_TAIL_LINES} lines of ${PLANT_LOG} ---" >&2
  tail -n "$_PLANT_LOG_TAIL_LINES" "$PLANT_LOG" >&2 || true
  echo "--- end of plant log ---" >&2
  return "$rc"
}
