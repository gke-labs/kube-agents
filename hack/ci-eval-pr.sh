#!/usr/bin/env bash
# ==============================================================================
# Prow CI Evaluation Pipeline Script
# ==============================================================================
# Runs devops-bench evaluation against deployed platform-agent.
# Evaluates tasks 'create-deployment' and 'gpu-stress-test-diagnosis'
# asserting OutcomeValidity score >= 0.7.
# ==============================================================================

set -euo pipefail

# 1. Target Cluster Context
export PROJECT_ID="${PROJECT_ID:-kube-agents-evals}"
export GCP_PROJECT_ID="${PROJECT_ID}"
export REGION="${REGION:-us-central1}"
HOST_CLUSTER_NAME="platform-agent-host"
export GKE_CLUSTER_NAME="test-cluster"
export CLOUD_PROVIDER="gcp"
export TF_VAR_infra_provider="gcp"

RAW_PULL_SHA="${PULL_PULL_SHA:-latest}"
PULL_SHA_SHORT="${RAW_PULL_SHA:0:7}"
export PR_ID="${PULL_NUMBER:-local}"
TARGET_NAMESPACE="kubeagents-system"

echo "=== Running PR Smoke Test Evaluation for PR #${PR_ID} in Namespace: ${TARGET_NAMESPACE} ==="

# 2. Cluster Auth
gcloud container clusters get-credentials "$HOST_CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet

# 3. Agent & Harness Configuration
# Configures devops-bench runner to target deployed platform-agent service
export BENCH_AGENT_TYPE="cli"
export AGENT_TARGET="kubeagents"
export BENCH_PARALLEL="false"
export BENCH_NO_INFRA="false"
export AGENT_CLUSTER_CONTEXT="gke_${PROJECT_ID}_${REGION}_${HOST_CLUSTER_NAME}"
export AGENT_SERVICE_NAME="platform-agent"
export AGENT_NAMESPACE="${TARGET_NAMESPACE}"

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
TASKS=("/app/tasks/noop/create-deployment/task.yaml" "/app/tasks/gcp/gpu-stress-test-diagnosis/task.yaml")

FAILED_TASKS=()

for TASK in "${TASKS[@]}"; do
  TASK_NAME="$(basename "$(dirname "${TASK}")")"
  echo ">>> Running Task: ${TASK_NAME} (${TASK}) <<<"

  if [[ "${TASK}" == *"noop"* ]]; then
    (cd /app && BENCH_NO_INFRA=true python3 /app/pkg/evaluator/evaluate.py "${TASK}") || true
  else
    (cd /app && python3 /app/pkg/evaluator/evaluate.py "${TASK}") || true
  fi
  
  # Locate the timestamped results.json file created by evaluate.py
  LATEST_RESULT="$(ls -t /app/results/run_*/results.json 2>/dev/null | head -n 1)"
  SCORE=$(python3 -c "import json; d=json.load(open('${LATEST_RESULT}'))[0] if '${LATEST_RESULT}' else {}; s=d.get('scores', d.get('metrics', {})); v=s.get('OutcomeValidity [GEval]', s.get('OutcomeValidity', 0)); print(v.get('score', v) if isinstance(v, dict) else v)" 2>/dev/null || echo "0")

  # Archive task-specific result JSON
  [ -n "${LATEST_RESULT}" ] && cp "${LATEST_RESULT}" "results_${TASK_NAME}.json" || true

  # 6. Validate Score Threshold & Dump Logs on Failure
  IS_PASS=$(python3 -c "print(1 if float('${SCORE}') >= 0.7 else 0)" 2>/dev/null || echo "0")
  if [ "${IS_PASS}" -eq 1 ]; then
    echo "Task ${TASK_NAME} Result: [PASSED] OutcomeValidity Score: ${SCORE} (Threshold: >= 0.7)"
  else
    echo "Task ${TASK_NAME} Result: [FAILED] OutcomeValidity Score: ${SCORE} (Threshold: >= 0.7)"
    echo "=== ERROR: Task ${TASK_NAME} Failed! Dumping Agent & LiteLLM Gateway Logs ==="
    echo "--- Agent Gateway Logs ---"
    kubectl logs deployment/platform-agent-gateway -n "${TARGET_NAMESPACE}" -c platform-agent --tail=100 || true
    echo "--- LiteLLM Gateway Logs ---"
    kubectl logs deployment/litellm -n "${TARGET_NAMESPACE}" --tail=100 || true
    FAILED_TASKS+=("${TASK_NAME}")
  fi
done

if [ "${#FAILED_TASKS[@]}" -gt 0 ]; then
  echo "❌ PR Smoke Test Evaluation Failed for tasks: ${FAILED_TASKS[*]}"
  exit 1
fi

echo "=== PR Smoke Test Evaluation Succeeded ==="