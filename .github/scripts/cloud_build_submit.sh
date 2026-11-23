#!/usr/bin/env bash
# Runs `gcloud builds submit` with the given arguments, and submits it once
# more when the build died on BuildKit's "flightcontrol: exceeded retry
# timeout" (#3911). docker-publish-gcp.yml runs every image build through it.
#
# That error is Cloud Build's builder giving up, not the Dockerfile: the same
# commit builds on a second submit. Main's publish failed on it for 7 of 9
# pushes on 2026-11-23 under a busy push rate, against 13 of 13 green the night
# before, and nothing retried, so those commits' images were never published.
#
# Only that exact string earns the retry, and only one. Any other failure, and
# a second flightcontrol failure, exits with gcloud's status, so a build that
# is really broken still fails the job. The retry is announced as a workflow
# warning, so a run that needed it is visible in the run summary, not only in
# the log.
#
# A retry widens a race that already exists: main's publish runs are not
# serialised (a shared concurrency group would drop queued runs in a merge
# burst; .agents/rules/github_actions.md), so a retried older commit can land
# its `:latest` and `:buildcache` after a newer commit's. Images are also
# tagged by commit SHA, and nothing pulls these by SHA but this workflow; the
# smoke reads `:latest` and `:buildcache` only as a build cache.
#
# Usage: .github/scripts/cloud_build_submit.sh <gcloud builds submit arguments...>
set -euo pipefail

# The BuildKit message the retry is for. Matched as a fixed string.
readonly FLIGHTCONTROL_ERROR="flightcontrol: exceeded retry timeout"
# Submits allowed in total: the first, plus one retry.
readonly MAX_ATTEMPTS=2
# Exit status for a call with no arguments.
readonly USAGE_EXIT=2

if [[ $# -eq 0 ]]; then
  echo "usage: $0 <gcloud builds submit arguments...>" >&2
  exit "${USAGE_EXIT}"
fi

build_log="$(mktemp)"
trap 'rm -f "${build_log}"' EXIT

attempt=1
while true; do
  status=0
  # The log still streams to the job; the copy is only read for the error.
  gcloud builds submit "$@" 2>&1 | tee "${build_log}" || status=$?
  if [[ "${status}" -eq 0 ]]; then
    exit 0
  fi
  if [[ "${attempt}" -ge "${MAX_ATTEMPTS}" ]] || ! grep -qF "${FLIGHTCONTROL_ERROR}" "${build_log}"; then
    exit "${status}"
  fi
  echo "::warning title=Cloud Build retried (#3911)::gcloud builds submit died on '${FLIGHTCONTROL_ERROR}' (attempt ${attempt} of ${MAX_ATTEMPTS}); submitting it again."
  attempt=$((attempt + 1))
done
