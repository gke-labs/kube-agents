#!/usr/bin/env bash
# Shared helpers for the two calls an environment pipeline makes to uninstall.sh.
#
# provision_environment.sh tears the previous environment down before it
# installs; teardown_environment.sh removes the environment once a run has
# passed end to end. Both invoke uninstall.sh the same way and read the same
# three outcomes from its exit code, so both live here — what differs is only
# what each does about a failure, which stays in the caller.
#
# Nothing here names an environment. Which project, region and cluster get torn
# down comes from GCP_PROJECT_ID / GCP_REGION / GKE_CLUSTER_NAME, which the
# calling workflow reads from its GitHub environment — so the RC and nightly
# pipelines run this byte-identically against different infrastructure.
#
# Sourced, not executed: this file defines functions and runs nothing.

# A failed uninstall.sh is run again after a wait before either caller reads
# its status. Every teardown failure on record has been a Google API answering
# 5xx for seconds to minutes: a credentials 503 while lifecycle.sh read the
# state, a GKE API server 503 during the Helm uninstall, a cluster delete that
# GKE reported failed. uninstall.sh is safe to run again, because lifecycle.sh
# destroy resumes a destroy that stopped partway. Exit 0 and 3 return at once.
# Both knobs are environment overrides so a test runs with no wait; the job
# timeouts in deploy-environment.yml and teardown-environment.yml hold the
# extra attempt.
readonly TEARDOWN_DEFAULT_ATTEMPTS=2
readonly TEARDOWN_DEFAULT_RETRY_WAIT_SECONDS=60
# uninstall.sh's EXIT_NOTHING_TO_TEAR_DOWN: no state, so nothing to retry.
readonly TEARDOWN_EXIT_NOTHING_TO_TEAR_DOWN=3

# TEARDOWN_STRICT is the name, and both `rc` and `nightly` define it, so that is
# what deploy-environment.yml forwards and what CI reads. RC_TEARDOWN_STRICT is
# the pre-rename spelling, read as a fallback and losing to the new name where
# both are set.
#
# The fallback buys nothing in CI — the workflow forwards one name, and an
# `env:` key is defined even when its expression is empty, so an unmigrated
# setting would arrive as "" and read as "off" either way. It is for running
# these scripts by hand against an environment nobody has migrated. Drop it once
# the old variable is deleted from both settings pages.
#
# The value is hand-typed, so it accepts everything installer_common.sh's
# is_truthy does rather than the literal "true" alone — a maintainer who types
# `1` must not get a pipeline that installs over a surviving environment while
# logging that strict mode is off. Inlined because these scripts do not source
# installer_common.sh; tests/testing/common.py's TRUTHY_BOOLEAN_INPUTS pins the
# accepted set for both. Anything neither truthy nor obviously "off" is a typo,
# and a typo in a safety switch is worth a line of output.
teardown_strict_source() {
  if [ -n "${TEARDOWN_STRICT:-}" ]; then
    echo "TEARDOWN_STRICT"
  elif [ -n "${RC_TEARDOWN_STRICT:-}" ]; then
    echo "RC_TEARDOWN_STRICT"
  else
    echo "TEARDOWN_STRICT"
  fi
}

teardown_is_strict() {
  local name val
  name="$(teardown_strict_source)"
  val="${!name:-}"
  val="${val//[[:space:]]/}"
  case "$val" in
    [Tt][Rr][Uu][Ee] | [Yy][Ee][Ss] | [Yy] | 1 | [Oo][Nn]) return 0 ;;
    "" | [Ff][Aa][Ll][Ss][Ee] | [Nn][Oo] | [Nn] | 0 | [Oo][Ff][Ff]) return 1 ;;
    *)
      echo "::warning title=${name} not understood::'${val}' is neither truthy nor falsy; treating it as off." >&2
      return 1
      ;;
  esac
}

# Canonicalizes an affirmative or negative boolean string to literal "true" or
# "false". Preserves unrecognised values untouched (including typos and whitespace)
# so downstream or pre-flight validators can reject them instead of silently
# defaulting or disabling a feature.
canonical_bool() {
  local val="${1:-}"
  local stripped="${val//[[:space:]]/}"
  case "$stripped" in
    [Tt][Rr][Uu][Ee] | [Yy][Ee][Ss] | [Yy] | 1 | [Oo][Nn]) echo "true" ;;
    [Ff][Aa][Ll][Ss][Ee] | [Nn][Oo] | [Nn] | 0 | [Oo][Ff][Ff]) echo "false" ;;
    *) echo "$val" ;;
  esac
}


# Refuses, with a GitHub Actions error, any named variable that is not a plain
# decimal: a word makes a `[ -ge ]` comparison exit 2, which an if reads as
# false; a value past six digits is "not an integer" to `[`; and a leading
# zero is octal to shell arithmetic (09 is an error, 0600 reads as 384), which
# provision_environment.sh's stockout loop does on its knobs. The teardown
# knobs only reach `[` and `sleep`, which read a leading zero as decimal, and
# are held to the same shape so every knob of the pipeline reads one way.
teardown_require_plain_decimal() {
  local knob
  for knob in "$@"; do
    case "${!knob}" in
      "" | *[!0-9]* | 0?* | ???????*)
        echo "::error title=${knob} is not a plain decimal::${knob}='${!knob}' must be a whole number of at most six digits with no leading zero." >&2
        exit 1
        ;;
    esac
  done
}

# Expands the three coordinates into TEARDOWN_TARGET so that a `set -u`
# abort on a missing one happens at the top of a script, before it has created
# a temp file or reached GCP.
#
# Assigns to a global rather than echoing, because a caller would have to write
# `x="$(...)"` to read an echo and command substitution is a subshell: the
# abort would kill the subshell, `x` would be empty, and the script would carry
# on to tear down a target it could not name.
teardown_require_inputs() {
  # shellcheck disable=SC2034  # read by the sourcing script, not by this file
  TEARDOWN_TARGET="${GCP_PROJECT_ID}/${GKE_CLUSTER_NAME} (${GCP_REGION})"

  # The retry knobs, checked here for the same reason: before anything is
  # destroyed. A count of 0 is refused because the loop below runs
  # uninstall.sh once whatever the count says, and a setting that reads as
  # "never" while the script runs anyway would lie.
  TEARDOWN_ATTEMPTS="${TEARDOWN_ATTEMPTS:-$TEARDOWN_DEFAULT_ATTEMPTS}"
  TEARDOWN_RETRY_WAIT_SECONDS="${TEARDOWN_RETRY_WAIT_SECONDS:-$TEARDOWN_DEFAULT_RETRY_WAIT_SECONDS}"
  teardown_require_plain_decimal TEARDOWN_ATTEMPTS TEARDOWN_RETRY_WAIT_SECONDS
  if [ "${TEARDOWN_ATTEMPTS}" -lt 1 ]; then
    echo "::error title=TEARDOWN_ATTEMPTS is not at least 1::TEARDOWN_ATTEMPTS='${TEARDOWN_ATTEMPTS}' is refused; the first run of uninstall.sh always happens." >&2
    exit 1
  fi
}

# Runs uninstall.sh against the resolved coordinates, teeing its output to $1, and
# returns uninstall.sh's own status. A failure is run again, up to
# TEARDOWN_ATTEMPTS runs in all with TEARDOWN_RETRY_WAIT_SECONDS between them
# (see the top of this file); $1 then holds every run's output, each after the
# first behind a marker line.
#
# Call it as `teardown_run "$log" || status=$?`. The arguments are expanded
# in this function body rather than inside the pipeline because a function runs
# in the calling shell: a `set -u` abort on a missing GCP_PROJECT_ID kills the
# caller from here — including under `||`, which was measured rather than
# assumed — where the same expansion inside a pipeline would kill only that
# stage's subshell and leave the script running against an empty target.
teardown_run() {
  local log_file="$1"
  local args=(
    --non-interactive -y
    --gcp-project-id="${GCP_PROJECT_ID}"
    --gcp-region="${GCP_REGION}"
    --gke-cluster-name="${GKE_CLUSTER_NAME}"
  )
  if [ -n "${NAMESPACE:-}" ]; then
    args+=(--agent-namespace="${NAMESPACE}")
  fi

  local status attempt=1
  while :; do
    # errexit is lifted around the pipeline rather than the shorter `|| true`:
    # `||` runs `true` on failure and PIPESTATUS then describes `true` instead
    # of uninstall.sh. tee always succeeds, so a bare `$?` would report every
    # teardown as clean.
    # The first run truncates the log and a later one appends behind a
    # marker line, so the log holds every run and a first run that ended
    # without a newline does not join the second. The job summary renders
    # the log's last 40 lines, so after two long failures it shows the
    # second run's tail; the first run's exit status is in the warning
    # annotation below, and its output in the job log.
    set +e
    if [ "$attempt" -eq 1 ]; then
      ./uninstall.sh "${args[@]}" 2>&1 | tee "${log_file}"
    else
      printf '\n--- uninstall.sh run %s of %s ---\n' "$attempt" "${TEARDOWN_ATTEMPTS}" | tee -a "${log_file}"
      ./uninstall.sh "${args[@]}" 2>&1 | tee -a "${log_file}"
    fi
    status="${PIPESTATUS[0]}"
    set -e
    if [ "$status" -eq 0 ] || [ "$status" -eq "$TEARDOWN_EXIT_NOTHING_TO_TEAR_DOWN" ] ||
      [ "$attempt" -ge "${TEARDOWN_ATTEMPTS}" ]; then
      return "$status"
    fi
    attempt=$((attempt + 1))
    echo "::warning title=Environment teardown failed; running it again::uninstall.sh exited ${status}; waiting ${TEARDOWN_RETRY_WAIT_SECONDS}s, then running it again (attempt ${attempt} of ${TEARDOWN_ATTEMPTS})." >&2
    sleep "${TEARDOWN_RETRY_WAIT_SECONDS}"
  done
}

# Surfaces a failed teardown on the run's annotations and in the job summary,
# not just in the scrolled-past middle of a step log.
#
# $1 exit status, $2 the teed log, $3 the annotation message, $4 the summary
# heading, and every remaining argument one line of summary prose.
teardown_report_failure() {
  local status="$1" log_file="$2" message="$3" heading="$4"
  shift 4

  echo "::error title=Environment teardown failed::${message}" >&2
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
      echo "### ${heading} (exit ${status})"
      echo ""
      local line
      for line in "$@"; do
        echo "${line}"
      done
      echo ""
      echo "<details><summary>uninstall.sh output</summary>"
      echo ""
      echo '```'
      # Backticks stripped so a triple-backtick in the teardown output cannot
      # close this fence and render the rest as markdown and HTML; nothing in a
      # log excerpt depends on them. `awk 1` guarantees the trailing newline the
      # closing fence needs — without it a final line with no newline swallows
      # the ``` and the block never closes.
      tail -n 40 "${log_file}" | tr -d '`' | awk 1
      echo '```'
      echo ""
      echo "</details>"
    } >> "${GITHUB_STEP_SUMMARY}"
  fi
}
