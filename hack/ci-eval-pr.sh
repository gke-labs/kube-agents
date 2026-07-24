#!/usr/bin/env bash
# ==============================================================================
# Prow CI Evaluation Pipeline Script
# ==============================================================================
# Runs devops-bench evaluation against deployed platform-agent.
# Evaluates task 'gpu-stress-test-diagnosis' asserting OutcomeValidity score >= 0.7.
# ==============================================================================

set -euo pipefail

# 1. Target Cluster Context
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/ci-env.sh"
trap dump_prow_artifacts_on_failure EXIT

START_TIME=$SECONDS
echo "=== [$(date -u)] Running PR Smoke Test Evaluation for PR #${PR_ID} in Namespace: ${TARGET_NAMESPACE} ==="

# 2. Cluster Auth
STEP_START=$SECONDS
echo "=== [$(date -u)] Authenticating to GKE Cluster ==="
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

# For opentofu provider
export CLOUD_PROVIDER="gcp"
export TF_VAR_infra_provider="gcp"

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

# 5. Task Matrix Execution Loop
TASKS=("/app/tasks/gcp/gpu-stress-test-diagnosis/task.yaml")

FAILED_TASKS=()

for TASK in "${TASKS[@]}"; do
  TASK_NAME="$(basename "$(dirname "${TASK}")")"
  TASK_START=$SECONDS
  echo ">>> [$(date -u)] Running Task: ${TASK_NAME} (${TASK}) <<<"

  # Enable BENCH_NO_INFRA=true for noop tasks (skip OpenTofu); set false for real infra evaluation tasks
  if [[ "${TASK}" == *"noop"* ]]; then
    export BENCH_NO_INFRA="true"
  else
    export BENCH_NO_INFRA="false"
  fi
  echo "Executing with BENCH_NO_INFRA=${BENCH_NO_INFRA}"

  # Snapshot existing result directories before running evaluate.py to prevent stale score leakage
  PRE_RUNS="$(ls -d /app/results/run_* 2>/dev/null | sort || true)"

  (cd /app && python3 /app/pkg/evaluator/evaluate.py "${TASK}") || true

  # Use set difference (comm -13) to isolate the brand new directory created strictly by THIS task run.
  # If evaluate.py crashed before or during execution without completing results.json, NEW_RUN_DIR will be empty.
  POST_RUNS="$(ls -d /app/results/run_* 2>/dev/null | sort || true)"
  NEW_RUN_DIR="$(comm -13 <(echo "${PRE_RUNS}") <(echo "${POST_RUNS}") | head -n 1)"
  LATEST_RESULT=""
  [ -n "${NEW_RUN_DIR}" ] && LATEST_RESULT="${NEW_RUN_DIR}/results.json"

  # Assert that results.json exists for this specific run; if the task crashed or failed to output results,
  # fail closed with SCORE=0 instead of reading a previous task's results file.
  if [ -z "${LATEST_RESULT}" ] || [ ! -f "${LATEST_RESULT}" ]; then
    echo "ERROR: Evaluation task ${TASK_NAME} did not produce a results.json file!"
    SCORE="0"
  else
    SCORE=$(python3 -c "import json; d=json.load(open('${LATEST_RESULT}'))[0] if '${LATEST_RESULT}' else {}; s=d.get('scores', d.get('metrics', {})); v=s.get('OutcomeValidity [GEval]', s.get('OutcomeValidity', 0)); print(v.get('score', v) if isinstance(v, dict) else v)" 2>/dev/null || echo "0")
    cp "${LATEST_RESULT}" "results_${TASK_NAME}.json" || true
  fi

  TASK_DURATION=$((SECONDS - TASK_START))
  # 6. Validate Score Threshold
  IS_PASS=$(python3 -c "print(1 if float('${SCORE}') >= 0.7 else 0)" 2>/dev/null || echo "0")
  if [ "${IS_PASS}" -eq 1 ]; then
    echo "Task ${TASK_NAME} Result: [PASSED] OutcomeValidity Score: ${SCORE} (Threshold: >= 0.7) (Duration: ${TASK_DURATION}s)"
  else
    echo "Task ${TASK_NAME} Result: [FAILED] OutcomeValidity Score: ${SCORE} (Threshold: >= 0.7) (Duration: ${TASK_DURATION}s)"
    FAILED_TASKS+=("${TASK_NAME}")
  fi
done

TOTAL_DURATION=$((SECONDS - START_TIME))
if [ "${#FAILED_TASKS[@]}" -gt 0 ]; then
  echo "❌ [$(date -u)] PR Smoke Test Evaluation Failed for tasks: ${FAILED_TASKS[*]} (Total Duration: ${TOTAL_DURATION}s)"
  exit 1
fi

echo "=== [$(date -u)] PR Smoke Test Evaluation Succeeded (Total Duration: ${TOTAL_DURATION}s) ==="