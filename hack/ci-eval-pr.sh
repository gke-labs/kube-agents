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
export CLUSTER_NAME="${CLUSTER_NAME:-platform-agent-host}"
export GKE_CLUSTER_NAME="${CLUSTER_NAME}"

RAW_PULL_SHA="${PULL_PULL_SHA:-latest}"
PULL_SHA_SHORT="${RAW_PULL_SHA:0:7}"
export PR_ID="${PULL_NUMBER:-local}"
export NAMESPACE="kubeagents-system"

echo "=== Running PR Smoke Test Evaluation for PR #${PR_ID} in Namespace: ${NAMESPACE} ==="

# 2. Cluster Auth
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet

# 3. Agent & Harness Configuration
# Configures devops-bench runner to target deployed platform-agent service
export BENCH_AGENT_TYPE="cli"
export AGENT_TARGET="kubeagents"
export BENCH_PARALLEL="false"
export BENCH_NO_INFRA="false"
export BENCH_USE_MCP="false"
export AGENT_CLUSTER_CONTEXT="gke_${PROJECT_ID}_${REGION}_${CLUSTER_NAME}"
export AGENT_SERVICE_NAME="platform-agent"
export AGENT_NAMESPACE="${NAMESPACE}"

# 4. Token & Model Configuration
# Dynamically fetches API_SERVER_KEY from GKE secret and locks down Gemini 3.1
export PLATFORM_AGENT_TOKEN="$(kubectl get secret platform-agent-secrets -n "${NAMESPACE}" -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode)"
export JUDGE_API_KEY="${GEMINI_API_KEY}"
export JUDGE_PROVIDER="google"
export JUDGE_MODEL="gemini-3.1-pro-preview"
export AGENT_PROVIDER="google"
export AGENT_MODEL="gemini-3.1-pro-preview"

# 5. Task Matrix Execution Loop
TASKS=("/app/tasks/noop/create-deployment/task.yaml" "/app/tasks/gcp/gpu-stress-test-diagnosis/task.yaml")

FAILED_TASKS=()

for TASK in "${TASKS[@]}"; do
  TASK_NAME="$(basename "$(dirname "${TASK}")")"
  echo ">>> Running Task: ${TASK_NAME} (${TASK}) <<<"

  (cd /app && python3 /app/pkg/evaluator/evaluate.py "${TASK}") || true
  
  # Locate the timestamped results.json file created by evaluate.py
  LATEST_RESULT="$(ls -t /app/results/run_*/results.json 2>/dev/null | head -n 1)"
  SCORE=$(python3 -c "import json; data=json.load(open('${LATEST_RESULT}')) if '${LATEST_RESULT}' else [{}]; print(data[0].get('metrics', {}).get('OutcomeValidity', 0))" 2>/dev/null || echo "0")
  echo "Task ${TASK_NAME} OutcomeValidity Score: ${SCORE}"

  # Archive task-specific result JSON
  [ -n "${LATEST_RESULT}" ] && cp "${LATEST_RESULT}" "results_${TASK_NAME}.json" || true

  # 6. Validate Score Threshold & Dump Logs on Failure
  IS_PASS=$(python3 -c "print(1 if float('${SCORE}') >= 0.7 else 0)")
  if [ "${IS_PASS}" -eq 0 ]; then
    echo "=== ERROR: Task ${TASK_NAME} Failed (Score: ${SCORE})! Dumping Agent & LiteLLM Gateway Logs ==="
    echo "--- Agent Gateway Logs ---"
    kubectl logs deployment/platform-agent-gateway -n "${NAMESPACE}" -c platform-agent --tail=100 || true
    echo "--- LiteLLM Gateway Logs ---"
    kubectl logs deployment/litellm -n "${NAMESPACE}" --tail=100 || true
    FAILED_TASKS+=("${TASK_NAME}")
  fi
done

if [ "${#FAILED_TASKS[@]}" -gt 0 ]; then
  echo "❌ PR Smoke Test Evaluation Failed for tasks: ${FAILED_TASKS[*]}"
  exit 1
fi

echo "=== PR Smoke Test Evaluation Succeeded ==="