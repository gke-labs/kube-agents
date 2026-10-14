#!/usr/bin/env bash
# Runs the A2A gateway's Slack live check as a Kubernetes Job on the cluster that
# --context names, prints the PASS/FAIL lines, and deletes the Job. A thin wrapper:
# the launcher is hack/slack-live-check/launch.py, and
# hack/slack-live-check/README.md has the setup, the flags and the run order.
# Run by hand only, never in CI.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly LAUNCHER="${SCRIPT_DIR}/slack-live-check/launch.py"

exec python3 "${LAUNCHER}" "$@"
