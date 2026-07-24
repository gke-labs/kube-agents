#!/usr/bin/env bash
# ==============================================================================
# Shared Prow CI Environment Configuration
# ==============================================================================
# Centralizes common variables sourced by ci-deploy.sh, ci-eval-pr.sh, and ci-teardown.sh.
# ==============================================================================

export PROJECT_ID="kube-agents-evals"
export GCP_PROJECT_ID="${PROJECT_ID}"
export REGION="${REGION:-us-central1}"

export HOST_CLUSTER_NAME="platform-agent-host"
export CLUSTER_NAME="${HOST_CLUSTER_NAME}"
export GKE_CLUSTER_NAME="test-cluster"

export TARGET_NAMESPACE="kubeagents-system"
export NAMESPACE="${TARGET_NAMESPACE}"
export PR_ID="${PULL_NUMBER:-local}"

# ─── Shared Artifact Collection Handler for Prow Job Failures ───────────────────
dump_prow_artifacts_on_failure() {
  local exit_code=$?
  if [ "$exit_code" -ne 0 ]; then
    local artifact_dir="${ARTIFACTS:-/tmp/artifacts}"
    mkdir -p "${artifact_dir}"
    echo "⚠️ Script failed (exit code ${exit_code}). Dumping diagnostics and logs to Prow artifacts (${artifact_dir})..."
    local ns="${TARGET_NAMESPACE:-${NAMESPACE:-kubeagents-system}}"
    
    # 1. Pipeline Summary & Cloud Build / Port-Forward Diagnostics (works even if kubectl fails)
    {
      echo "=== EXIT CODE: ${exit_code} ==="
      echo "=== TIMESTAMP: $(date -u) ==="
      echo "=== ACTIVE KUBECTL CONTEXT ==="
      kubectl config current-context 2>&1 || true
      echo "=== RECENT CLOUD BUILDS ==="
      gcloud builds list --project="${PROJECT_ID:-kube-agents-evals}" --limit=5 2>&1 || true
      echo "=== PORT FORWARD LOG (/tmp/pf-8642.log) ==="
      cat /tmp/pf-8642.log 2>&1 || true
    } > "${artifact_dir}/ci-failure-summary.txt" 2>&1 || true

    # 2. Current running & previous crashed pod logs (crucial for rollout deadline / CrashLoopBackOff failures)
    kubectl logs deployment/platform-agent-gateway -n "${ns}" --tail=2000 > "${artifact_dir}/platform-agent-gateway.log" 2>&1 || true
    kubectl logs deployment/platform-agent-gateway -n "${ns}" --previous --tail=1000 > "${artifact_dir}/platform-agent-gateway-previous-crash.log" 2>&1 || true
    kubectl logs deployment/kubeagents-controller-manager -n "${ns}" --tail=1000 > "${artifact_dir}/controller-manager.log" 2>&1 || true
    
    # 3. Detailed Pod Descriptions & K8s Events (explains image pull errors, scheduling blocks, OOMKilled, probe failures)
    kubectl describe pods -n "${ns}" > "${artifact_dir}/k8s-pod-descriptions.txt" 2>&1 || true
    kubectl get pods,svc,events -n "${ns}" -o wide > "${artifact_dir}/k8s-cluster-status.txt" 2>&1 || true
    
    # 4. Devops-bench Evaluation Results (if run in eval script)
    if [ -d "/app/results" ]; then
      cp -r /app/results/* "${artifact_dir}/" 2>/dev/null || true
    fi
    cp results_*.json "${artifact_dir}/" 2>/dev/null || true
  fi
}
