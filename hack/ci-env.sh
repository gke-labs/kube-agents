#!/usr/bin/env bash
# ==============================================================================
# Shared Prow CI Environment Configuration
# ==============================================================================
# Centralizes common variables sourced by ci-deploy.sh, ci-eval-pr.sh, and ci-teardown.sh.
# ==============================================================================

# gke_dns_endpoint_flag, so every CI get-credentials picks the same endpoint the
# installer would. Only this helper is pulled in, not k8s-operator/scripts/common.sh,
# whose state file and print_* helpers CI has no use for.
# shellcheck source=k8s-operator/scripts/gke_dns_endpoint.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../k8s-operator/scripts" && pwd)/gke_dns_endpoint.sh"

# TODO(boskos): Once oss-test-infra#2655 merges and deploys Boskos project leasing,
# consider failing closed if JOB_NAME is set and PROJECT_ID is unset.
export PROJECT_ID="${PROJECT_ID:-kube-agents-evals}"
export GCP_PROJECT_ID="${PROJECT_ID}"
export REGION="${REGION:-us-central1}"

export HOST_CLUSTER_NAME="platform-agent-host"
export CLUSTER_NAME="${HOST_CLUSTER_NAME}"
# GKE_CLUSTER_NAME (the per-run task cluster devops-bench provisions) is set by
# ci-eval-pr.sh, derived from the Prow BUILD_ID so concurrent runs never share
# a name. Deploy and teardown never touch a task cluster, so it is not set here.

export TARGET_NAMESPACE="kubeagents-system"
export NAMESPACE="${TARGET_NAMESPACE}"
export PR_ID="${PULL_NUMBER:-local}"

# ─── Helm Bootstrap ──────────────────────────────────────────────────────────
# The Prow job image carries gcloud, kubectl, and go, but no helm — and
# ci-deploy.sh / ci-teardown.sh drive the kube-agents chart with it. Install a
# checksum-pinned binary when the image has none; a machine that already has
# helm on PATH (a developer laptop, a GitHub runner) is left alone.
HELM_VERSION="v3.21.4"
HELM_SHA256_LINUX_AMD64="61f88ab166748cb19604d7884cb100ae9ccb13804ddeb98e08af167eacbb6a14"
HELM_SHA256_LINUX_ARM64="b54c04b4e0b2540bbdc08c17a121dab70e9a2ed0de5705528fec68a5fd3b85a7"

ensure_helm() {
  if command -v helm >/dev/null 2>&1; then
    return 0
  fi
  local arch sha dir
  if [ "$(uname -s)" != "Linux" ]; then
    echo "ERROR: no helm on PATH and the pinned download covers Linux only. Install helm and re-run." >&2
    return 1
  fi
  case "$(uname -m)" in
    x86_64) arch="amd64"; sha="$HELM_SHA256_LINUX_AMD64" ;;
    aarch64 | arm64) arch="arm64"; sha="$HELM_SHA256_LINUX_ARM64" ;;
    *)
      echo "ERROR: no helm on PATH and no pinned download for architecture '$(uname -m)'. Install helm and re-run." >&2
      return 1
      ;;
  esac
  dir="/tmp/kube-agents-helm-${HELM_VERSION}-${arch}"
  if [ ! -x "${dir}/helm" ]; then
    mkdir -p "$dir"
    curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz" -o "${dir}/helm.tar.gz"
    echo "${sha}  ${dir}/helm.tar.gz" | sha256sum -c - >/dev/null
    tar -xzf "${dir}/helm.tar.gz" -C "$dir" --strip-components=1 "linux-${arch}/helm"
    rm -f "${dir}/helm.tar.gz"
  fi
  export PATH="${dir}:${PATH}"
  echo "Installed helm ${HELM_VERSION} (${arch}) to ${dir}"
}

# ─── Bench Result Collection (runs on PASS as well as on failure) ──────────────
# Split out of dump_prow_artifacts_on_failure, which wraps its whole body in
# `if [ "$exit_code" -ne 0 ]`. That meant the eval job had NEVER kept a record
# from a passing run -- exactly backwards for a rate-based gate, whose baseline
# store is built from green runs on main. The copy is one `cp`, so there is no
# reason to condition it; the expensive diagnostics (kubectl logs, describe
# pods, gcloud builds list) stay failure-only below.
#
# Callers must invoke this BEFORE dump_prow_artifacts_on_failure: that function
# reads `$?` on its first line, so anything running ahead of it must leave the
# status alone. Every command here ends in `|| true` for that reason.
collect_bench_results() {
  local artifact_dir="${ARTIFACTS:-/tmp/artifacts}"
  mkdir -p "${artifact_dir}" || true

  # Devops-bench Evaluation Results (if run in eval script)
  if [ -d "${SCRIPT_DIR}/../bench/results" ]; then
    cp -r "${SCRIPT_DIR}/../bench/results/"* "${artifact_dir}/" 2>/dev/null || true
  elif [ -d "/app/results" ]; then
    cp -r /app/results/* "${artifact_dir}/" 2>/dev/null || true
  fi
  cp results_*.json "${artifact_dir}/" 2>/dev/null || true
}

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
      echo "=== TIMESTAMP: $(date -u +'%Y-%m-%dT%H:%M:%SZ') ==="
      echo "=== ACTIVE KUBECTL CONTEXT ==="
      kubectl config current-context 2>&1 || true
      echo "=== RECENT CLOUD BUILDS ==="
      gcloud builds list --project="${PROJECT_ID}" --limit=5 2>&1 || true
      echo "=== PORT FORWARD LOG (/tmp/pf-8642.log) ==="
      cat /tmp/pf-8642.log 2>&1 || true
    } > "${artifact_dir}/ci-failure-summary.txt" 2>&1 || true

    # 2. Current running & previous crashed pod logs (crucial for rollout deadline / CrashLoopBackOff failures)
    kubectl logs deployment/platform-agent-gateway -n "${ns}" --tail=2000 > "${artifact_dir}/platform-agent-gateway.log" 2>&1 || true
    kubectl logs deployment/platform-agent-gateway -n "${ns}" --previous --tail=1000 > "${artifact_dir}/platform-agent-gateway-previous-crash.log" 2>&1 || true
    kubectl logs deployment/kube-agents-controller-manager -n "${ns}" --tail=1000 > "${artifact_dir}/controller-manager.log" 2>&1 || true
    # The gateway capture above reads the pod's default container (platform-agent);
    # a dropped port-forward stream is only visible from the envoy sidecar's side.
    kubectl logs deployment/platform-agent-gateway -c envoy-credential-proxy -n "${ns}" --tail=2000 > "${artifact_dir}/envoy-credential-proxy.log" 2>&1 || true
    # Konnectivity/tunnel churn shows up as kube-system events, not in "${ns}".
    kubectl get events -n kube-system --sort-by=.lastTimestamp 2>&1 | tail -100 > "${artifact_dir}/kube-system-events.txt" 2>&1 || true

    # 3. Detailed Pod Descriptions & K8s Events (explains image pull errors, scheduling blocks, OOMKilled, probe failures)
    kubectl describe pods -n "${ns}" > "${artifact_dir}/k8s-pod-descriptions.txt" 2>&1 || true
    kubectl get pods,svc,events -n "${ns}" -o wide > "${artifact_dir}/k8s-cluster-status.txt" 2>&1 || true

    # 4. Devops-bench Evaluation Results used to be copied here. They now come
    #    from collect_bench_results() above, which runs on green too. Callers
    #    that want both must call it FIRST -- see the note on that function.
    collect_bench_results
  fi
}
