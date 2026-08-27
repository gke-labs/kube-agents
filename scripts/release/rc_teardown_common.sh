#!/usr/bin/env bash
# Shared helpers for the RC pipeline's two calls to uninstall.sh.
#
# provision_rc_environment.sh tears the previous environment down before it
# installs; teardown_rc_environment.sh removes the environment once a run has
# passed end to end. Both invoke uninstall.sh the same way and read the same
# three outcomes from its exit code, so both live here — what differs is only
# what each does about a failure, which stays in the caller.
#
# Sourced, not executed: this file defines functions and runs nothing.

# RC_TEARDOWN_STRICT is typed into a GitHub web form, so it accepts what
# installer_common.sh's is_truthy accepts rather than the literal "true" alone —
# a maintainer who types `1` must not get a pipeline that keeps installing over
# a surviving environment while logging that strict mode is off. Inlined
# because these scripts do not source installer_common.sh; keep the two in step
# (the accepted set is pinned by tests/testing/common.py's TRUTHY_BOOLEAN_INPUTS).
# A value that is neither truthy nor an obvious "off" is a typo, and a typo in a
# safety switch is worth a line of output.
rc_teardown_is_strict() {
  local val="${RC_TEARDOWN_STRICT:-}"
  val="${val//[[:space:]]/}"
  case "$val" in
    [Tt][Rr][Uu][Ee] | [Yy][Ee][Ss] | [Yy] | 1 | [Oo][Nn]) return 0 ;;
    "" | [Ff][Aa][Ll][Ss][Ee] | [Nn][Oo] | [Nn] | 0 | [Oo][Ff][Ff]) return 1 ;;
    *)
      echo "::warning title=RC_TEARDOWN_STRICT not understood::'${RC_TEARDOWN_STRICT}' is neither truthy nor falsy; treating it as off." >&2
      return 1
      ;;
  esac
}

# Expands the three coordinates into RC_TEARDOWN_TARGET so that a `set -u`
# abort on a missing one happens at the top of a script, before it has created
# a temp file or reached GCP.
#
# Assigns to a global rather than echoing, because a caller would have to write
# `x="$(...)"` to read an echo and command substitution is a subshell: the
# abort would kill the subshell, `x` would be empty, and the script would carry
# on to tear down a target it could not name.
rc_teardown_require_inputs() {
  # shellcheck disable=SC2034  # read by the sourcing script, not by this file
  RC_TEARDOWN_TARGET="${GCP_PROJECT_ID}/${GKE_CLUSTER_NAME} (${GCP_REGION})"
}

# Runs uninstall.sh against the RC coordinates, teeing its output to $1, and
# returns uninstall.sh's own status.
#
# Call it as `rc_teardown_run "$log" || status=$?`. The arguments are expanded
# in this function body rather than inside the pipeline because a function runs
# in the calling shell: a `set -u` abort on a missing GCP_PROJECT_ID kills the
# caller from here — including under `||`, which was measured rather than
# assumed — where the same expansion inside a pipeline would kill only that
# stage's subshell and leave the script running against an empty target.
rc_teardown_run() {
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
rc_teardown_report_failure() {
  local status="$1" log_file="$2" message="$3" heading="$4"
  shift 4

  echo "::error title=RC teardown failed::${message}" >&2
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
