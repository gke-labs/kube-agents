#!/usr/bin/env bash
# ==============================================================================
# Prow CI Evaluation Pipeline Script
# ==============================================================================
# Runs devops-bench evaluation against deployed platform-agent.
# Evaluates the task matrix in section 6, asserting OutcomeValidity score >= 0.7
# per task. ChecklistScore is reported alongside but does not gate the build.
# ==============================================================================

set -euo pipefail

# 1. Target Cluster Context
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/ci-env.sh"
trap dump_prow_artifacts_on_failure EXIT

START_TIME=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Running PR Smoke Test Evaluation for PR #${PR_ID} in Namespace: ${TARGET_NAMESPACE} ==="

# 2. Cluster Auth
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Authenticating to GKE Cluster ==="
gcloud container clusters get-credentials "$HOST_CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet
echo "✓ Cluster authentication finished in $((SECONDS - STEP_START))s"

# 3. Agent & Harness Configuration
# Configures devops-bench runner to target deployed platform-agent service
export BENCH_AGENT_TYPE="cli"
export AGENT_TARGET="kubeagents"
export BENCH_PARALLEL="false"
export AGENT_CLUSTER_CONTEXT="gke_${PROJECT_ID}_${REGION}_${HOST_CLUSTER_NAME}"
export AGENT_SERVICE_NAME="platform-agent"
export AGENT_NAMESPACE="${TARGET_NAMESPACE}"
export BENCH_TF_ROOT="./tf"
# The harness default is 600s, which the multi-turn incident-triage apply tasks
# overrun: they replay the whole conversation each turn and land around 210k
# tokens, against ~28k for a single-turn task. A timed-out turn is not a soft
# failure — the agent's output becomes "Error: TimeoutError: timed out" and the
# judge then fails every criterion against it.
export AGENT_HTTP_TIMEOUT="${AGENT_HTTP_TIMEOUT:-1200}"

# For opentofu provider
export CLOUD_PROVIDER="gcp"
export TF_VAR_infra_provider="gcp"
export GKE_CLUSTER_NAME="test-cluster"
export CLUSTER_NAME="test-cluster"
export TF_VAR_cluster_name="test-cluster"
export GCP_LOCATION="us-west4-a" # set to different zone due to resource availability stockouts in us-central1

# Stamp the run onto every labelable GCP resource the stacks create, alongside
# the fixed managed-by label the cluster module applies. These say *which* run
# left an orphan behind; managed-by is what the sweep matches on. Both are set
# by Prow and empty when running locally, where the stacks fall back to "local".
export TF_VAR_prow_build_id="${BUILD_ID:-}"
export TF_VAR_prow_pull_number="${PULL_NUMBER:-}"

# 4. Token & Model Configuration
# Dynamically fetches API_SERVER_KEY from GKE secret and locks down Gemini 3.1
export PLATFORM_AGENT_TOKEN="$(kubectl get secret platform-agent-secrets -n "${TARGET_NAMESPACE}" -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode)"
export JUDGE_API_KEY="${GEMINI_API_KEY}"
export JUDGE_PROVIDER="google"
export JUDGE_MODEL="gemini-3.1-pro-preview"
export AGENT_PROVIDER="google"
export AGENT_MODEL="gemini-3.1-pro-preview"

# Unset NAMESPACE so devops-bench OpenTofu deployer does not pass -var namespace=... to stacks that don't declare it
unset NAMESPACE

# 5. Prerequisites Check
if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: 'uv' is not installed or not in PATH." >&2
  echo "The evaluation harness requires uv to run devops-bench." >&2
  echo "Please install uv (e.g. via 'curl -LsSf https://astral.sh/uv/install.sh | sh') or ensure the Prow runner image provides it." >&2
  exit 1
fi

# 6. Task Matrix Execution Loop
# Paths are relative to BENCH_DIR, which is where devops-bench runs. Tasks added
# under bench/tasks/ are NOT picked up automatically — list them here, and budget
# for it: each incident-triage task costs roughly 2.5 minutes of judge time.
BENCH_DIR="${SCRIPT_DIR}/../bench"
TASKS=(
  "./tasks/gpu-stress-test-diagnosis/task.yaml"
  # The incident-triage group ships fourteen tasks; these four are the smoke
  # subset. Each one covers a failure mode the others do not: the base report
  # contract, the single-option branch (which is what caught the agent padding
  # its option list), the workload-controlled warning message as an injection
  # surface, and one non-scheduling event class as an anti-boilerplate canary.
  # The remaining ten are a manual sweep -- see bench/tasks/incident-triage
  # /README.md. Two of them, no-live-mutation and apply-recommended, belong here
  # on severity and are held out only until they run green.
  "./tasks/incident-triage/report/task.yaml"
  "./tasks/incident-triage/single-option/task.yaml"
  "./tasks/incident-triage/prompt-injection/task.yaml"
  "./tasks/incident-triage/oomkilled/task.yaml"
)

# Reads infrastructure.deployer out of a task file. Matching on the task *path*
# instead — the previous approach — silently sends every task whose directory
# does not spell "noop" off to provision a cluster it never uses.
task_deployer() {
  python3 -c "
import re, sys
text = open(sys.argv[1]).read()
m = re.search(r'^\s*deployer:\s*(.+?)\s*\$', text, re.M)
print(m.group(1).strip('\'\"') if m else '')
" "$1" 2>/dev/null || echo ""
}

# Runs one task and sets LATEST_RESULT to the results.json the run produced, or
# to "" when devops-bench died before writing one. It sets a global rather than
# echoing the path because the run's own output goes to stdout — a command
# substitution would capture the whole evaluation log along with the path.
# The set difference (comm -13) isolates the directory created strictly by THIS
# run, so a crashed run cannot inherit an earlier task's scores.
run_bench_task() {
  local task="$1" pre post new
  pre="$(ls -d "${BENCH_DIR}/results/run_"* 2>/dev/null | sort || true)"
  (cd "${BENCH_DIR}" && uv run devops-bench "${task}" --agent-type kubeagents 2>&1 | tee -a "${EVAL_LOG}") || true
  post="$(ls -d "${BENCH_DIR}/results/run_"* 2>/dev/null | sort || true)"
  new="$(comm -13 <(echo "${pre}") <(echo "${post}") | head -n 1)"
  LATEST_RESULT=""
  [ -n "${new}" ] && LATEST_RESULT="${new}/results.json"
}

# True when the agent never answered and the harness recorded its transport
# error as the output. Scoring that is meaningless — every criterion fails
# against "Error: TimeoutError: timed out" — so the task is worth one retry.
is_transport_timeout() {
  python3 -c "
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print('0'); raise SystemExit
rec = data[0] if isinstance(data, list) else data
out = (rec.get('actual_output') or '') if isinstance(rec, dict) else ''
errs = ' '.join(rec.get('errors') or []) if isinstance(rec, dict) else ''
print('1' if 'TimeoutError' in out or 'TimeoutError' in errs else '0')
" "$1" 2>/dev/null || echo "0"
}

FAILED_TASKS=()

for TASK in "${TASKS[@]}"; do
  TASK_NAME="$(basename "$(dirname "${TASK}")")"
  TASK_START=$SECONDS
  echo ">>> [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Running Task: ${TASK_NAME} (${TASK}) <<<"

  # Skip OpenTofu for generation-only tasks; provision for everything else.
  DEPLOYER="$(task_deployer "${BENCH_DIR}/${TASK}")"
  if [[ "${DEPLOYER}" == "noop" ]]; then
    export BENCH_NO_INFRA="true"
  else
    export BENCH_NO_INFRA="false"
  fi

  # The incident-triage payload is self-contained, so granting MCP there only
  # buys ~3x the tokens in tool_search probes and a ToolInvocation score against
  # a trajectory the task never asked for. The two exceptions assert on tool
  # calls themselves and need the tools present to have anything to assert on.
  case "${TASK}" in
    ./tasks/incident-triage/no-live-mutation/*|./tasks/incident-triage/session-routing/*)
      export BENCH_USE_MCP="true" ;;
    ./tasks/incident-triage/*)
      export BENCH_USE_MCP="false" ;;
    *)
      export BENCH_USE_MCP="true" ;;
  esac
  echo "Executing with deployer=${DEPLOYER:-unknown} BENCH_NO_INFRA=${BENCH_NO_INFRA} BENCH_USE_MCP=${BENCH_USE_MCP}"

  EVAL_LOG="/tmp/eval_${TASK_NAME}.log"
  : > "${EVAL_LOG}"

  run_bench_task "${TASK}"
  if [ -n "${LATEST_RESULT}" ] && [ "$(is_transport_timeout "${LATEST_RESULT}")" -eq 1 ]; then
    echo "⚠️ Task ${TASK_NAME}: agent request timed out (AGENT_HTTP_TIMEOUT=${AGENT_HTTP_TIMEOUT}s). Retrying once."
    run_bench_task "${TASK}"
  fi
  NEW_RUN_DIR=""
  [ -n "${LATEST_RESULT}" ] && NEW_RUN_DIR="$(dirname "${LATEST_RESULT}")"

  # Check if results.json is missing or empty [] due to OpenTofu / resource creation or deletion failure
  IS_RESOURCE_PREP_FAILURE=$(python3 -c "
import json, os
path = '${LATEST_RESULT}'
if not path or not os.path.exists(path):
    print('1')
else:
    try:
        data = json.load(open(path))
        rec = data[0] if isinstance(data, list) else data
        print('0' if rec and isinstance(rec, dict) and rec.get('scores') else '1')
    except Exception:
        print('1')
" 2>/dev/null || echo "1")

  TASK_DURATION=$((SECONDS - TASK_START))

  if [ "${IS_RESOURCE_PREP_FAILURE}" -eq 1 ]; then
    echo "⚠️ [RESOURCE_PREPARATION_FAILED] Evaluation task ${TASK_NAME} resource creation or teardown failed! (The evaluation is skipped)"
    ARTIFACT_DIR="${ARTIFACTS:-/tmp/artifacts}"
    mkdir -p "${ARTIFACT_DIR}"
    cp "${EVAL_LOG}" "${ARTIFACT_DIR}/resource_prep_failure_${TASK_NAME}.log" 2>/dev/null || true
    [ -n "${NEW_RUN_DIR}" ] && cp "${EVAL_LOG}" "${NEW_RUN_DIR}/resource_prep_failure.log" 2>/dev/null || true
    echo "Saved resource preparation log to artifact: ${ARTIFACT_DIR}/resource_prep_failure_${TASK_NAME}.log"
    echo "Task ${TASK_NAME} Result: [RESOURCE_PREPARATION_FAILED] Infrastructure setup/teardown error (Duration: ${TASK_DURATION}s)"
    FAILED_TASKS+=("${TASK_NAME} (Resource Preparation Failed)")
  else
    SCORE=$(python3 -c "
import json
data = json.load(open('${LATEST_RESULT}'))
rec = data[0] if isinstance(data, list) else data
scores = rec.get('scores', rec.get('metrics', {}))
ov = scores.get('OutcomeValidity [GEval]', scores.get('OutcomeValidity', 0))
score_val = ov.get('score', ov) if isinstance(ov, dict) else ov
print(score_val if score_val is not None else 0)
" 2>/dev/null || echo "0")
    # Reported, not gated: the per-requirement checklist is where the incident
    # triage report contract lives, so a drop there is worth seeing in the log
    # even when OutcomeValidity still clears its threshold.
    CHECKLIST=$(python3 -c "
import json
data = json.load(open('${LATEST_RESULT}'))
rec = data[0] if isinstance(data, list) else data
scores = rec.get('scores', rec.get('metrics', {}))
cs = scores.get('ChecklistScore')
if isinstance(cs, dict):
    print(f\"{cs.get('score')} ({cs.get('reason', '').strip()})\")
elif cs is not None:
    print(cs)
else:
    print('n/a')
" 2>/dev/null || echo "n/a")
    echo "Task ${TASK_NAME} ChecklistScore: ${CHECKLIST}"
    cp "${LATEST_RESULT}" "results_${TASK_NAME}.json" || true

    # 6. Validate Score Threshold
    IS_PASS=$(python3 -c "print(1 if float('${SCORE}') >= 0.7 else 0)" 2>/dev/null || echo "0")
    if [ "${IS_PASS}" -eq 1 ]; then
      echo "Task ${TASK_NAME} Result: [PASSED] OutcomeValidity Score: ${SCORE} (Threshold: >= 0.7) (Duration: ${TASK_DURATION}s)"
    else
      echo "Task ${TASK_NAME} Result: [FAILED] OutcomeValidity Score: ${SCORE} (Threshold: >= 0.7) (Duration: ${TASK_DURATION}s)"
      FAILED_TASKS+=("${TASK_NAME}")
    fi
  fi
done

TOTAL_DURATION=$((SECONDS - START_TIME))
if [ "${#FAILED_TASKS[@]}" -gt 0 ]; then
  echo "❌ [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] PR Smoke Test Evaluation Failed for tasks: ${FAILED_TASKS[*]} (Total Duration: ${TOTAL_DURATION}s)"
  exit 1
fi

echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] PR Smoke Test Evaluation Succeeded (Total Duration: ${TOTAL_DURATION}s) ==="
