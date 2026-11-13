#!/usr/bin/env bash
# The Slack lane: run hack/slack-lane/cases.txt through real Slack, advisory.
#
# Each case runs once with AGENT_TRANSPORT=slack: a test user mentions the
# install's bot in a CI channel and bench/kube_agents_bench/slack_transport.py
# reads the thread back, timing post -> first reaction -> ack -> answer. A
# "then:" line adds a second turn in the same thread (BENCH_SLACK_THEN), timed
# as slack_then_* and not graded. The
# lane gates nothing and always exits 0; a case's result and timing land under
# ${SLACK_LANE_ARTIFACTS}, with the run's Slack overhead (delivery lag, a
# post's time, the first ack's quantiles: what the gateway's metrics gained
# between a scrape before the cases and one after) in slack-overhead.json
# (hack/slack-lane/overhead.py).
#
# The test user's token, the channel and the bot's member id are read from
# Secret Manager in ${SLACK_LANE_SECRETS_PROJECT}, the project the nightly
# already reads its shared GitLab tokens from (hack/ci-eval-pr.sh,
# read_gitlab_token). Absent any of them, the lane prints one skip line and
# exits 0. Nothing runs it on a schedule yet: it runs by hand against an
# install, and a nightly part is to call it once the Slack test account exists.
#
# Beside the three Slack values it needs what any devops-bench run against the
# install needs: PROJECT_ID, CLUSTER_NAME, AGENT_CLUSTER_CONTEXT (the harness
# still reads the agent pod, and a case's prompt may name the context),
# BENCH_TF_ROOT, the judge's GCP_PROJECT_ID, JUDGE_PROVIDER and JUDGE_MODEL, and
# BENCH_FLEET_KUBECONFIG_DIR for a case that reads the seeded fleet.
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
# The run's Slack overhead (hack/slack-lane/overhead.py), from the A2A
# gateway's metrics listener scraped before the cases and after them: its pods
# by name, its metrics port (a2aGatewayMetricsPort in the operator), and the
# namespace and context the eval exports.
readonly OVERHEAD_FILE_NAME="slack-overhead.json"
readonly GATEWAY_POD_PREFIX="${SLACK_LANE_GATEWAY_POD_PREFIX:-platform-agent-a2a-gateway-}"
readonly GATEWAY_METRICS_PORT="${SLACK_LANE_GATEWAY_METRICS_PORT:-9096}"
readonly AGENT_NAMESPACE_DEFAULT="kubeagents-system"
readonly GATEWAY_LOCAL_PORT_BASE=19096
readonly PORT_FORWARD_TRIES=10
readonly METRICS_TIMEOUT_SECONDS=5
readonly POD_MARKER="# pod: "

# Writes every gateway pod's metrics, each under a "# pod: <name>" line, to
# <out file>. Through kubectl port-forward, as the eval runner reaches the
# gateway: the gateway's NetworkPolicy admits port 9096 only from the
# monitoring namespace and the operator, and an API-server pod proxy is pod
# network ingress the policy denies, while a port-forward is not.
scrape_gateway() { # <out file>
  local out="$1" ns="${TARGET_NAMESPACE:-${NAMESPACE:-${AGENT_NAMESPACE_DEFAULT}}}" pod pf i=0 local_port
  local -a kubectl_cmd=(kubectl)
  [ -n "${AGENT_CLUSTER_CONTEXT:-}" ] && kubectl_cmd+=(--context "${AGENT_CLUSTER_CONTEXT}")
  : >"${out}"
  for pod in $("${kubectl_cmd[@]}" -n "${ns}" get pods -o name 2>/dev/null | sed -n "s|^pod/\\(${GATEWAY_POD_PREFIX}.*\\)|\\1|p"); do
    i=$((i + 1))
    local_port=$((GATEWAY_LOCAL_PORT_BASE + i))
    "${kubectl_cmd[@]}" -n "${ns}" port-forward "pod/${pod}" "${local_port}:${GATEWAY_METRICS_PORT}" >/dev/null 2>&1 &
    pf=$!
    echo "${POD_MARKER}${pod}" >>"${out}"
    for _ in $(seq 1 "${PORT_FORWARD_TRIES}"); do
      curl -fsS --max-time "${METRICS_TIMEOUT_SECONDS}" "http://127.0.0.1:${local_port}/metrics" >>"${out}" 2>/dev/null && break
      sleep 1
    done
    kill "${pf}" 2>/dev/null || true
  done
}

# Writes the run's overhead (overhead.py: after less before, per pod), or why it could not.
record_overhead() { # <before file> <after file> <out file>
  if grep -q "_count" "$2" 2>/dev/null; then
    python3 "${SCRIPT_DIR}/overhead.py" "$2" "$1" >"$3" || echo '{"error": "overhead.py failed"}' >"$3"
  else
    echo '{"error": "no gateway metrics could be read (see kubectl port-forward to the gateway)"}' >"$3"
  fi
  echo "slack lane: overhead $(cat "$3")"
}

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
  local before="${SLACK_LANE_ARTIFACTS}/gateway-metrics-before.txt" after="${SLACK_LANE_ARTIFACTS}/gateway-metrics-after.txt"
  scrape_gateway "${before}"
  local summary="${SLACK_LANE_ARTIFACTS}/${SUMMARY_FILE_NAME}"
  : >"${summary}"
  local line case_id reply dir log timing rc
  while IFS= read -r line; do
    # "<case id>", or "<case id> then: <reply>" for a second turn in the thread;
    # split before the comment strip, so a reply may hold a "#", but only when no
    # "#" comes before " then: " (a commented-out reply is a comment).
    reply=""
    case "${line%% then: *}" in
      "${line}" | *#*) ;;
      *) reply="${line#* then: }"; line="${line%% then: *}" ;;
    esac
    line="${line%%#*}"
    case_id="$(echo "${line}" | tr -d '[:space:]')"
    [ -n "${case_id}" ] || continue
    dir="${SLACK_LANE_ARTIFACTS}/${case_id}"
    mkdir -p "${dir}"
    log="${dir}/run.log"
    timing="${dir}/${SLACK_TIMING_FILE_NAME}"
    rc=0
    # A run that fails before writing its timing must not report the last run's.
    rm -f -- "${dir}/${TIMING_FILE_NAME}" "${timing}"
    # The token reaches the child's environment only, never a file or argv.
    (cd "${BENCH_DIR}" && AGENT_TRANSPORT=slack BENCH_SLACK_THEN="${reply}" \
      BENCH_SLACK_USER_TOKEN="${token}" BENCH_SLACK_CHANNEL_ID="${channel}" BENCH_SLACK_BOT_USER_ID="${bot}" \
      KUBE_AGENTS_TIMING_FILE="${dir}/${TIMING_FILE_NAME}" BENCH_SLACK_TIMING_FILE="${timing}" \
      uv run devops-bench "./tasks/${case_id}" --agent-type kubeagents >"${log}" 2>&1) || rc=$?
    printf '{"case": "%s", "exit": %d, "timing": %s}\n' "${case_id}" "${rc}" "$(cat "${timing}" 2>/dev/null || echo null)" >>"${summary}"
    echo "slack lane: ${case_id} exit=${rc} timing=$(cat "${timing}" 2>/dev/null || echo none)"
  done <"${CASES_FILE}"
  scrape_gateway "${after}"
  record_overhead "${before}" "${after}" "${SLACK_LANE_ARTIFACTS}/${OVERHEAD_FILE_NAME}"
  return 0
}

main "$@"
exit 0
