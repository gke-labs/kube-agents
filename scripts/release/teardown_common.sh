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
}

# Runs uninstall.sh against the resolved coordinates, teeing its output to $1, and
# returns uninstall.sh's own status.
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
    --project-id="${GCP_PROJECT_ID}"
    --region="${GCP_REGION}"
    --cluster-name="${GKE_CLUSTER_NAME}"
  )

  local status
  # errexit is lifted around the pipeline rather than the shorter `|| true`:
  # `||` runs `true` on failure and PIPESTATUS then describes `true` instead of
  # uninstall.sh. tee always succeeds, so a bare `$?` would report every
  # teardown as clean.
  set +e
  ./uninstall.sh "${args[@]}" 2>&1 | tee "${log_file}"
  status="${PIPESTATUS[0]}"
  set -e
  return "$status"
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
