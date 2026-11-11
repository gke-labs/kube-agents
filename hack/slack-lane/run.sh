#!/usr/bin/env bash
# The Slack lane: run hack/slack-lane/cases.txt through real Slack, advisory.
#
# Each case runs once with AGENT_TRANSPORT=slack: a test user mentions the
# install's bot in a CI channel and bench/kube_agents_bench/slack_transport.py
# reads the thread back, timing post -> first reaction -> ack -> answer. The
# lane gates nothing and always exits 0; a case's result and timing land under
# ${SLACK_LANE_ARTIFACTS}.
#
# The test user's token, the channel and the bot's member id are read from
# Secret Manager in ${SLACK_LANE_SECRETS_PROJECT}, the project the nightly
# already reads its shared GitLab tokens from (hack/ci-eval-pr.sh,
# read_gitlab_token). Absent any of them, the lane prints one skip line and
# exits 0, so a nightly part without the Slack test account is unchanged.
#
# The install it runs against must have Slack enabled with its own Slack app,
# the bot and the test user in the channel, the test user on
# integration.slack.allowedUsers and on operator.slackTestUsers (the gateway
# drops a user-token post as bot traffic otherwise). Socket Mode splits one
# app's events across every live connection, so the app must serve this
# install alone.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
readonly REPO_ROOT
readonly BENCH_DIR="${REPO_ROOT}/bench"
readonly CASES_FILE="${SCRIPT_DIR}/cases.txt"
readonly SLACK_LANE_SECRETS_PROJECT="${SLACK_LANE_SECRETS_PROJECT:-kube-agents-prow}"
readonly USER_TOKEN_SECRET="${SLACK_LANE_USER_TOKEN_SECRET:-slack-eval-user-token}"
readonly CHANNEL_SECRET="${SLACK_LANE_CHANNEL_SECRET:-slack-eval-channel-id}"
readonly BOT_USER_SECRET="${SLACK_LANE_BOT_USER_SECRET:-slack-eval-bot-user-id}"
readonly SLACK_LANE_ARTIFACTS="${SLACK_LANE_ARTIFACTS:-${ARTIFACTS:-/tmp}/slack-lane}"
readonly TIMING_FILE_NAME="timing.json"
readonly SLACK_TIMING_FILE_NAME="slack-timing.json"
readonly SUMMARY_FILE_NAME="summary.jsonl"

read_secret() { # <name> -> the value on stdout, newline stripped; empty when unreadable
  gcloud secrets versions access latest --secret="$1" --project="${SLACK_LANE_SECRETS_PROJECT}" 2>/dev/null | tr -d '\r\n'
}

main() {
  local token channel bot
  token="$(read_secret "${USER_TOKEN_SECRET}")" || token=""
  channel="$(read_secret "${CHANNEL_SECRET}")" || channel=""
  bot="$(read_secret "${BOT_USER_SECRET}")" || bot=""
  if [ -z "${token}" ] || [ -z "${channel}" ] || [ -z "${bot}" ]; then
    echo "slack lane: skipped: ${USER_TOKEN_SECRET}, ${CHANNEL_SECRET} or ${BOT_USER_SECRET} is not readable in ${SLACK_LANE_SECRETS_PROJECT}"
    return 0
  fi
  mkdir -p "${SLACK_LANE_ARTIFACTS}"
  local summary="${SLACK_LANE_ARTIFACTS}/${SUMMARY_FILE_NAME}"
  : >"${summary}"
  local case_id dir log timing rc
  while IFS= read -r case_id; do
    case_id="${case_id%%#*}"
    case_id="$(echo "${case_id}" | tr -d '[:space:]')"
    [ -n "${case_id}" ] || continue
    dir="${SLACK_LANE_ARTIFACTS}/${case_id}"
    mkdir -p "${dir}"
    log="${dir}/run.log"
    timing="${dir}/${SLACK_TIMING_FILE_NAME}"
    rc=0
    # The token reaches the child's environment only, never a file or argv.
    (cd "${BENCH_DIR}" && AGENT_TRANSPORT=slack \
      BENCH_SLACK_USER_TOKEN="${token}" BENCH_SLACK_CHANNEL_ID="${channel}" BENCH_SLACK_BOT_USER_ID="${bot}" \
      KUBE_AGENTS_TIMING_FILE="${dir}/${TIMING_FILE_NAME}" BENCH_SLACK_TIMING_FILE="${timing}" \
      uv run devops-bench "./tasks/${case_id}" --agent-type kubeagents >"${log}" 2>&1) || rc=$?
    printf '{"case": "%s", "exit": %d, "timing": %s}\n' "${case_id}" "${rc}" "$(cat "${timing}" 2>/dev/null || echo null)" >>"${summary}"
    echo "slack lane: ${case_id} exit=${rc} timing=$(cat "${timing}" 2>/dev/null || echo none)"
  done <"${CASES_FILE}"
  return 0
}

main "$@"
exit 0
