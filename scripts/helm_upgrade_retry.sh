#!/usr/bin/env bash
#
# Run `helm upgrade` with the arguments given, waiting out the release lock.
#
# The controller, agent, and integrations redeploys upgrade the same
# `kube-agents` release from separate workflows, so two can reach helm at once
# and the loser fails on the lock. Waiting costs a delay; the deploy it was
# about to do is still valid.
#
# Only the lock error retries. Any other helm failure exits immediately with
# helm's own status.
#
# The budget is wall-clock so it can be compared against the `--timeout 10m`
# every call site gives its own upgrade, and it has to outlast the longest hold
# a healthy deploy can take. That is an integrations run, which takes the lock
# three times in sequence -- provision-secrets, deploy-litellm, deploy-github
# -- for up to 30 minutes total.
#
# The default is sized against the callers' timeout-minutes: 60. Giving up
# costs the budget itself, ~35 minutes. Winning the lock on the last attempt
# costs more: that attempt starts at up to (budget - delay) and then runs its
# own --timeout 10m, so ~44.5 minutes before the job's other steps.

set -euo pipefail

LOCK_ERROR="another operation (install/upgrade/rollback) is in progress"

lock_timeout="${HELM_LOCK_TIMEOUT:-2100}"
delay="${HELM_LOCK_RETRY_DELAY:-30}"

deadline=$((SECONDS + lock_timeout))
attempt=0

while true; do
  attempt=$((attempt + 1))

  # The output is captured so the lock error can be matched, and echoed back on
  # both paths so a failed deploy still shows why. helm writes that error to
  # stderr, hence the 2>&1.
  status=0
  output="$(helm upgrade "$@" 2>&1)" || status=$?
  printf '%s\n' "$output"

  if [[ "$status" -eq 0 ]]; then
    exit 0
  fi

  if [[ "$output" != *"$LOCK_ERROR"* ]]; then
    exit "$status"
  fi

  # Stop before a sleep that would run past the deadline rather than after it,
  # so the budget is a ceiling on the wait instead of a floor.
  if ((SECONDS + delay >= deadline)); then
    break
  fi

  echo "Release lock held by a concurrent deploy; retrying in ${delay}s (attempt ${attempt}, $((deadline - SECONDS))s of budget left)"
  sleep "$delay"
done

# Deliberately not a bare "run helm rollback": from here a lock held by a live
# deploy looks exactly like a wedged release, and rolling back the former kills
# a deploy that was about to succeed.
echo "::error::Timed out after ${lock_timeout}s waiting for the kube-agents release lock. Check whether another deploy is still running in this environment first -- if one is, let it finish and re-run this job. Only if nothing is running is the release wedged: 'helm history kube-agents -n kubeagents-system' shows the pending revision, and 'helm rollback kube-agents <last-deployed-revision> -n kubeagents-system' clears it."
exit 1
