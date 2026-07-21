#!/usr/bin/env bash
set -euo pipefail

export PROJECT_ID="${PROJECT_ID:-kube-agents-evals}"
export REGION="${REGION:-us-central1}"
export CLUSTER_NAME="${CLUSTER_NAME:-platform-agent-host}"

PULL_SHA_SHORT="${PULL_PULL_SHA:0:7}"
export PR_ID="${PULL_NUMBER:-local}"
export NAMESPACE="kubeagents-system"

echo "=== Running PR Smoke Test Evaluation for PR #${PR_ID} in Namespace: ${NAMESPACE} ==="

gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet

export BENCH_AGENT_TYPE="cli"
export AGENT_TARGET="kubeagents"
export BENCH_PARALLEL="false"
export BENCH_NO_INFRA="false"
export AGENT_CLUSTER_CONTEXT="gke_${PROJECT_ID}_${REGION}_${CLUSTER_NAME}"
export AGENT_SERVICE_NAME="platform-agent"
export AGENT_NAMESPACE="${NAMESPACE}"
export PLATFORM_AGENT_TOKEN="$(kubectl get secret platform-agent-secrets -n "${NAMESPACE}" -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode)"
export JUDGE_API_KEY="${GEMINI_API_KEY:-}"
export JUDGE_PROVIDER="google"
export JUDGE_MODEL="gemini-3.1-pro-preview"
export AGENT_PROVIDER="google"
export AGENT_MODEL="gemini-3.1-pro-preview"

TASKS=("/app/tasks/noop/create-deployment/task.yaml" "/app/tasks/gcp/gpu-stress-test-diagnosis/task.yaml")

for TASK in "${TASKS[@]}"; do
  echo ">>> Running Task: ${TASK} <<<"
  if ! (cd /app && python3 /app/pkg/evaluator/evaluate.py "${TASK}"); then
    echo "=== ERROR: Evaluation Failed! Dumping Agent & LiteLLM Gateway Logs ==="
    echo "--- Agent Gateway Logs ---"
    kubectl logs deployment/platform-agent-gateway -n "${NAMESPACE}" --tail=100 || true
    echo "--- LiteLLM Logs ---"
    kubectl logs deployment/litellm -n "${NAMESPACE}" --tail=100 || true
    exit 1
  fi
  
  SCORE=$(python3 -c "import json, os; p='results.json' if os.path.exists('results.json') else '/app/results.json'; data=json.load(open(p)); print(data.get('metrics', {}).get('OutcomeValidity', 0))")
  echo "Task ${TASK} OutcomeValidity Score: ${SCORE}"
  python3 -c "import sys; sys.exit(0 if ${SCORE} >= 0.7 else 1)"
done

echo "=== PR Smoke Test Evaluation Succeeded ==="