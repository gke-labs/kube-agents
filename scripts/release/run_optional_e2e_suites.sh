#!/usr/bin/env bash
# Runs the optional E2E suites of a pipeline's E2E step, one after another.
#
# "Optional" is the caller's tolerance rather than this script's: e2e-run.yml
# marks the step continue-on-error, so a non-zero exit here names which suites
# failed without stopping the pipeline. Every suite runs regardless of what the
# ones before it did — a suite that fails must not swallow the coverage of the
# rest, which is the reason the input is a list and not a single name.
#
# The list arrives comma-separated because `workflow_call` has no list input
# type; arrays exist only for `workflow_dispatch` choices.
set -uo pipefail

OPTIONAL_SUITES="${OPTIONAL_SUITES:-}"
# Relative, and overridable, for the same reason teardown_common.sh calls
# `./uninstall.sh`: the unit tests run this from a temp directory holding a mock
# at the same path, so nothing has to reach a cluster to pin the loop's control
# flow.
E2E_RUNNER="${E2E_RUNNER:-./scripts/release/execute_e2e_tests.sh}"
# pytest's summary line for a run that skipped and passed nothing: a suite that
# skips while its credentials are unprovisioned exits 0, and reading that as
# "passed" would report coverage that never ran.
readonly PYTEST_SKIPPED_PATTERN='[0-9]+ skipped'
readonly PYTEST_PASSED_PATTERN='[0-9]+ passed'

# Strip separators as well as whitespace, so "", " " and ",," all read as empty.
if [ -z "${OPTIONAL_SUITES//[[:space:],]/}" ]; then
  echo "==> No optional E2E suites requested; nothing to run."
  exit 0
fi

passed=()
skipped=()
failed=()

IFS=',' read -r -a requested_suites <<<"${OPTIONAL_SUITES}"
for raw_suite in "${requested_suites[@]}"; do
  suite="${raw_suite//[[:space:]]/}"
  [ -n "${suite}" ] || continue

  echo "::group::Optional E2E suite: ${suite}"
  suite_log="$(mktemp)"
  "${E2E_RUNNER}" --suite "${suite}" 2>&1 | tee "${suite_log}"
  suite_status=${PIPESTATUS[0]}
  if [ "${suite_status}" -eq 0 ] && grep -Eq "${PYTEST_SKIPPED_PATTERN}" "${suite_log}" && ! grep -Eq "${PYTEST_PASSED_PATTERN}" "${suite_log}"; then
    skipped+=("${suite}")
    echo "==> Optional suite '${suite}' skipped: it ran no test."
  elif [ "${suite_status}" -eq 0 ]; then
    passed+=("${suite}")
    echo "==> Optional suite '${suite}' passed."
  else
    failed+=("${suite}")
    echo "::warning title=Optional E2E suite failed::'${suite}' exited ${suite_status}. It is optional, so the pipeline continues and nothing it gates is blocked — but the coverage it was meant to provide is missing from this run." >&2
  fi
  rm -f "${suite_log}"
  echo "::endgroup::"
done

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### Optional E2E suites"
    echo ""
    echo "| Suite | Result |"
    echo "| --- | --- |"
    for suite in "${passed[@]:-}"; do
      [ -n "${suite}" ] && echo "| \`${suite}\` | ✅ passed |"
    done
    for suite in "${skipped[@]:-}"; do
      [ -n "${suite}" ] && echo "| \`${suite}\` | ⏭️ skipped |"
    done
    for suite in "${failed[@]:-}"; do
      [ -n "${suite}" ] && echo "| \`${suite}\` | ❌ failed (tolerated) |"
    done
  } >>"${GITHUB_STEP_SUMMARY}"
fi

if [ "${#failed[@]}" -gt 0 ]; then
  echo "==> ${#failed[@]} optional suite(s) failed: ${failed[*]}" >&2
  exit 1
fi

echo "==> ${#passed[@]} optional suite(s) passed, ${#skipped[@]} skipped."
