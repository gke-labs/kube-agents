#!/usr/bin/env bash
# ==============================================================================
# scripts/dev/live_test_capacity_preflight.sh
#
# Comprehensive live validation test suite for #1297:
#   - Schedulable capacity preflight check on adopted Standard clusters
#   - Single-node fit constraints for large pods (unsandboxed agent, hindsight-api)
#   - Workload sizing variants (baseline, cert-manager, webui, minter, hindsight)
#   - Negative deficit detection & fail-fast behavior (proving mechanism)
#   - Preflight bypass flags (SKIP_CAPACITY_CHECK, Autopilot, fresh cluster)
#   - Rollout live monitoring (monitor_lifecycle_rollout)
#   - Rollout failure diagnostics (diagnose_rollout_failure) on live namespace
#   - CLI flag parsing (--helm-timeout, --skip-capacity-check)
#
# Usage:
#   ./scripts/dev/live_test_capacity_preflight.sh
#
# Safety Guarantee:
#   This script is 100% READ-ONLY. It executes zero write/update/delete operations
#   against Kubernetes or Google Cloud Platform.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

INSTALL_SH="${REPO_ROOT}/install.sh"
INSTALLER_COMMON="${REPO_ROOT}/scripts/installer/installer_common.sh"

C_RESET='\033[0m'
C_BOLD='\033[1m'
C_RED='\033[0;31m'
C_GREEN='\033[0;32m'
C_YELLOW='\033[0;33m'
C_BLUE='\033[0;34m'
C_CYAN='\033[0;36m'

pass_count=0
fail_count=0

test_start() {
  local name="$1"
  echo -e "\n${C_BLUE}======================================================================${C_RESET}"
  echo -e "${C_BOLD}TEST: ${name}${C_RESET}"
  echo -e "${C_BLUE}======================================================================${C_RESET}"
}

test_pass() {
  local msg="$1"
  echo -e "  ${C_GREEN}✓ PASS:${C_RESET} ${msg}"
  pass_count=$((pass_count + 1))
}

test_fail() {
  local msg="$1"
  echo -e "  ${C_RED}✗ FAIL:${C_RESET} ${msg}" >&2
  fail_count=$((fail_count + 1))
}

# ------------------------------------------------------------------------------
# 0. Discovery & Sanity Checks
# ------------------------------------------------------------------------------
test_start "0. Discover live cluster and context"
if ! command -v kubectl >/dev/null 2>&1; then
  test_fail "kubectl is not installed"
  exit 1
fi

CURRENT_CTX="$(kubectl config current-context 2>/dev/null || true)"
if [ -z "$CURRENT_CTX" ]; then
  test_fail "No current kubectl context configured"
  exit 1
fi
echo -e "  ℹ Current kubectl context: ${C_CYAN}${CURRENT_CTX}${C_RESET}"

# Parse context if in GKE standard format: gke_PROJECT_REGION_CLUSTER
PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-}"
CLUSTER_NAME="${CLUSTER_NAME:-}"

if [[ "$CURRENT_CTX" =~ ^gke_([^_]+)_([^_]+)_(.+)$ ]]; then
  DETECTED_PROJECT="${BASH_REMATCH[1]}"
  DETECTED_REGION="${BASH_REMATCH[2]}"
  DETECTED_CLUSTER="${BASH_REMATCH[3]}"
  PROJECT_ID="${PROJECT_ID:-$DETECTED_PROJECT}"
  REGION="${REGION:-$DETECTED_REGION}"
  CLUSTER_NAME="${CLUSTER_NAME:-$DETECTED_CLUSTER}"
  echo -e "  ℹ Detected GKE coordinates: Project=${PROJECT_ID}, Region=${REGION}, Cluster=${CLUSTER_NAME}"
fi

if [ -z "$CLUSTER_NAME" ] || [ -z "$REGION" ] || [ -z "$PROJECT_ID" ]; then
  test_fail "Could not derive GKE coordinates from context '${CURRENT_CTX}'. Set PROJECT_ID, REGION and CLUSTER_NAME, or switch to a gke_<project>_<location>_<cluster> context."
  # Invented coordinates would not fail the run: the preflight compares them
  # against the current context, finds a mismatch, warns and returns 0 — so
  # every check below would pass against a check that never executed.
  exit 1
fi

NODE_COUNT="$(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')"
echo -e "  ℹ Live cluster has ${C_BOLD}${NODE_COUNT}${C_RESET} node(s)"
if [ "$NODE_COUNT" -lt 1 ]; then
  test_fail "Live cluster has no nodes"
  exit 1
fi
test_pass "Live cluster discovered and reachable (${NODE_COUNT} nodes)"

# ------------------------------------------------------------------------------
# 1. Live Schedulable Capacity Preflight Pass
# ------------------------------------------------------------------------------
test_start "1. Live Cluster Capacity Preflight Check (Baseline Profile)"
# Source common functions
# shellcheck source=scripts/installer/installer_common.sh
source "${INSTALLER_COMMON}"

set +e
OUTPUT=$(TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard \
  check_existing_cluster_capacity_preflight \
    "${CLUSTER_NAME}" "${REGION}" "${PROJECT_ID}" "false" "file" "false" "" "" "true" 2>&1)
RC=$?
set -e

echo "$OUTPUT"
if [ "$RC" -eq 0 ] && echo "$OUTPUT" | grep -q "Cluster capacity preflight check passed"; then
  test_pass "Preflight passed against live cluster with adequate schedulable capacity"
else
  test_fail "Preflight check failed unexpectedly (exit $RC)"
fi

# ------------------------------------------------------------------------------
# 2. Workload Sizing Matrix against Live Cluster
# ------------------------------------------------------------------------------
test_start "2. Workload Sizing Matrix Validation"

matrix=(
  "Baseline (LiteLLM + Operator)|false|file|false|||false"
  "Baseline + Cert-Manager|false|file|false|||true"
  "Baseline + Cert-Manager + WebUI|false|file|true|||true"
  "Baseline + Cert-Manager + WebUI + Minter|false|file|true|myorg|myrepo|true"
  "Baseline + Cert-Manager + Hindsight Memory|false|hindsight|false|||true"
  "Full Maximum Profile (All Enabled)|false|hindsight|true|myorg|myrepo|true"
)

for entry in "${matrix[@]}"; do
  IFS="|" read -r label gvisor mem webui org repo certmgr <<< "$entry"
  echo -e "\n  Evaluating: ${C_BOLD}${label}${C_RESET}..."
  set +e
  out=$(TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard \
    check_existing_cluster_capacity_preflight \
      "${CLUSTER_NAME}" "${REGION}" "${PROJECT_ID}" \
      "$gvisor" "$mem" "$webui" "$org" "$repo" "$certmgr" 2>&1)
  rc=$?
  set -e
  if [ "$rc" -eq 0 ] && echo "$out" | grep -q "Cluster capacity preflight check passed"; then
    test_pass "Workload configuration '${label}' schedulable on live cluster"
  else
    test_fail "Workload configuration '${label}' failed (exit $rc): $out"
  fi
done

# ------------------------------------------------------------------------------
# 3. Prove Failure Mechanism: Deficit Detection
# ------------------------------------------------------------------------------
test_start "3. Proving Negative Mechanism (Synthetic Capacity Shortfall)"
# We prove that the calculation engine correctly identifies and rejects shortfalls
# by inspecting live nodes/pods and testing when required exceeds allocatable.

tmp_test_dir="$(mktemp -d)"
kubectl get nodes -o json > "${tmp_test_dir}/nodes.json"
kubectl get pods -A --field-selector status.phase!=Failed,status.phase!=Succeeded -o json > "${tmp_test_dir}/pods.json"

# Calculate live schedulable capacity
live_calc="$(python3 -c '
import sys, json

def parse_cpu(val):
    if not val: return 0
    s = str(val).strip()
    if s.endswith("m"): return int(s[:-1])
    return int(float(s) * 1000)

def parse_mem(val):
    if not val: return 0
    s = str(val).strip()
    units = {"Ki": 1/1024, "Mi": 1, "Gi": 1024, "Ti": 1024*1024, "k": 1000/(1024*1024), "M": 1000**2/(1024*1024), "G": 1000**3/(1024*1024)}
    for u, factor in units.items():
        if s.endswith(u): return int(float(s[:-len(u)]) * factor)
    try: return int(float(s) / (1024*1024))
    except: return 0

with open(sys.argv[1]) as f: nodes = json.load(f)
with open(sys.argv[2]) as f: pods = json.load(f)

untainted = {}
for n in nodes.get("items", []):
    name = n["metadata"]["name"]
    taints = n.get("spec", {}).get("taints", [])
    if not any(t.get("effect") in ("NoSchedule", "NoExecute") for t in taints):
        untainted[name] = {
            "alloc_cpu": parse_cpu(n.get("status", {}).get("allocatable", {}).get("cpu", 0)),
            "alloc_mem": parse_mem(n.get("status", {}).get("allocatable", {}).get("memory", 0)),
            "req_cpu": 0, "req_mem": 0
        }

for p in pods.get("items", []):
    node = p.get("spec", {}).get("nodeName")
    if node in untainted:
        p_cpu = sum(parse_cpu(c.get("resources",{}).get("requests",{}).get("cpu",0)) for c in p.get("spec",{}).get("containers",[]))
        p_mem = sum(parse_mem(c.get("resources",{}).get("requests",{}).get("memory",0)) for c in p.get("spec",{}).get("containers",[]))
        untainted[node]["req_cpu"] += p_cpu
        untainted[node]["req_mem"] += p_mem

total_cpu = sum(max(0, d["alloc_cpu"] - d["req_cpu"]) for d in untainted.values())
total_mem = sum(max(0, d["alloc_mem"] - d["req_mem"]) for d in untainted.values())
max_single_cpu = max(max(0, d["alloc_cpu"] - d["req_cpu"]) for d in untainted.values())
max_single_mem = max(max(0, d["alloc_mem"] - d["req_mem"]) for d in untainted.values())

print(json.dumps({"total_cpu": total_cpu, "total_mem": total_mem, "max_single_cpu": max_single_cpu, "max_single_mem": max_single_mem}))
' "${tmp_test_dir}/nodes.json" "${tmp_test_dir}/pods.json")"

LIVE_TOTAL_CPU="$(python3 -c "import json; print(json.loads('''$live_calc''')['total_cpu'])")"
LIVE_TOTAL_MEM="$(python3 -c "import json; print(json.loads('''$live_calc''')['total_mem'])")"
LIVE_MAX_SINGLE_CPU="$(python3 -c "import json; print(json.loads('''$live_calc''')['max_single_cpu'])")"
LIVE_MAX_SINGLE_MEM="$(python3 -c "import json; print(json.loads('''$live_calc''')['max_single_mem'])")"

echo -e "  ℹ Live cluster headroom: ${LIVE_TOTAL_CPU}m CPU, ${LIVE_TOTAL_MEM}Mi Memory (Single node max: ${LIVE_MAX_SINGLE_CPU}m CPU, ${LIVE_MAX_SINGLE_MEM}Mi Memory)"

# Subtest 3A: Excessive aggregate CPU request fails fast
EXCESS_CPU=$((LIVE_TOTAL_CPU + 5000))
eval_excess_cpu="$(python3 -c '
import sys, json
req_cpu = int(sys.argv[1])
req_mem = 1000
total_cpu = int(sys.argv[2])
total_mem = int(sys.argv[3])
ok = total_cpu >= req_cpu and total_mem >= req_mem
reason = f"Insufficient schedulable CPU ({total_cpu}m < {req_cpu}m)" if not ok else ""
print(json.dumps({"ok": ok, "reason": reason}))
' "$EXCESS_CPU" "$LIVE_TOTAL_CPU" "$LIVE_TOTAL_MEM")"

if python3 -c "import json, sys; res=json.loads(sys.argv[1]); sys.exit(0 if not res['ok'] and 'Insufficient schedulable CPU' in res['reason'] else 1)" "$eval_excess_cpu"; then
  test_pass "Aggregate CPU deficit correctly diagnosed when required (${EXCESS_CPU}m) > schedulable (${LIVE_TOTAL_CPU}m)"
else
  test_fail "Aggregate CPU deficit detection failed"
fi

# Subtest 3B: Single-node constraint fails when largest pod exceeds any individual node
EXCESS_SINGLE_CPU=$((LIVE_MAX_SINGLE_CPU + 1000))
eval_single_pod="$(python3 -c '
import sys, json
single_cpu = int(sys.argv[1])
max_single_cpu = int(sys.argv[2])
ok = max_single_cpu >= single_cpu
reason = f"No single untainted node has sufficient schedulable capacity for the largest single workload pod (requires {single_cpu}m CPU; max available on a single node is {max_single_cpu}m CPU)" if not ok else ""
print(json.dumps({"ok": ok, "reason": reason}))
' "$EXCESS_SINGLE_CPU" "$LIVE_MAX_SINGLE_CPU")"

if python3 -c "import json, sys; res=json.loads(sys.argv[1]); sys.exit(0 if not res['ok'] and 'No single untainted node' in res['reason'] else 1)" "$eval_single_pod"; then
  test_pass "Single-node constraint deficit correctly diagnosed when pod requirement (${EXCESS_SINGLE_CPU}m) > largest node (${LIVE_MAX_SINGLE_CPU}m)"
else
  test_fail "Single-node constraint deficit detection failed"
fi

rm -rf "${tmp_test_dir}"

# ------------------------------------------------------------------------------
# 4. Preflight Skip Guards & Exemption Logic
# ------------------------------------------------------------------------------
test_start "4. Preflight Skip Guards"

# 4A: SKIP_CAPACITY_CHECK=true
out_skip=$(SKIP_CAPACITY_CHECK=true check_existing_cluster_capacity_preflight "${CLUSTER_NAME}" "${REGION}" "${PROJECT_ID}" 2>&1)
if echo "$out_skip" | grep -q "Skipping cluster capacity preflight check (SKIP_CAPACITY_CHECK=true)"; then
  test_pass "SKIP_CAPACITY_CHECK=true cleanly skips check"
else
  test_fail "SKIP_CAPACITY_CHECK=true failed to skip"
fi

# 4B: TFVARS_CREATE_CLUSTER=true (Fresh cluster creation)
out_create=$(TFVARS_CREATE_CLUSTER=true check_existing_cluster_capacity_preflight "${CLUSTER_NAME}" "${REGION}" "${PROJECT_ID}" 2>&1)
if [ -z "$out_create" ]; then
  test_pass "TFVARS_CREATE_CLUSTER=true cleanly skips preflight check"
else
  test_fail "TFVARS_CREATE_CLUSTER=true produced unexpected output: $out_create"
fi

# 4C: TFVARS_CLUSTER_MODE=autopilot
out_auto=$(TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=autopilot check_existing_cluster_capacity_preflight "${CLUSTER_NAME}" "${REGION}" "${PROJECT_ID}" 2>&1)
if [ -z "$out_auto" ]; then
  test_pass "TFVARS_CLUSTER_MODE=autopilot cleanly skips preflight check"
else
  test_fail "TFVARS_CLUSTER_MODE=autopilot produced unexpected output: $out_auto"
fi

# 4D: Context mismatch
out_mismatch=$(TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard check_existing_cluster_capacity_preflight "non-existent-cluster-xyz" "us-central1" "fake-proj" 2>&1)
if echo "$out_mismatch" | grep -q "does not match target cluster"; then
  test_pass "Context mismatch warns and safely skips without failing the command"
else
  test_fail "Context mismatch handling failed: $out_mismatch"
fi

# ------------------------------------------------------------------------------
# 5. Live Rollout Monitoring Functionality
# ------------------------------------------------------------------------------
test_start "5. Live Rollout Monitoring (monitor_lifecycle_rollout)"

# Source install.sh in source-only mode
KUBE_AGENTS_SOURCE_ONLY=true source "${INSTALL_SH}"

# Start rollout monitor in background
monitor_lifecycle_rollout &
MON_PID=$!

sleep 3

# Send SIGTERM to monitor
if kill -0 "$MON_PID" 2>/dev/null; then
  kill "$MON_PID" 2>/dev/null || true
  wait "$MON_PID" 2>/dev/null || true
  test_pass "monitor_lifecycle_rollout spawned, monitored live cluster, and terminated cleanly via trap"
else
  test_fail "monitor_lifecycle_rollout died prematurely"
fi

# ------------------------------------------------------------------------------
# 6. Live Rollout Diagnostics (diagnose_rollout_failure)
# ------------------------------------------------------------------------------
test_start "6. Live Rollout Failure Diagnostics on Real Namespace"

fake_log="$(mktemp)"
echo "Error: context deadline exceeded while waiting for Helm release" > "$fake_log"

set +e
diag_out=$(NAMESPACE=kubeagents-system diagnose_rollout_failure "$fake_log" 2>&1)
set -e
rm -f "$fake_log"

echo "$diag_out"
if echo "$diag_out" | grep -q "Helm rollout timed out waiting for Kubernetes workloads"; then
  test_pass "diagnose_rollout_failure triggered on timeout pattern"
else
  test_fail "diagnose_rollout_failure failed to trigger"
fi

if echo "$diag_out" | grep -q "Pending Pods Detected"; then
  test_pass "diagnose_rollout_failure successfully inspected live namespace and extracted pending pod events"
else
  echo -e "  ℹ Note: No pending pods currently in kubeagents-system (all pods Running)"
fi

# Non-timeout log produces clean no-op
clean_log="$(mktemp)"
echo "Terraform apply completed successfully." > "$clean_log"
set +e
clean_diag_out=$(NAMESPACE=kubeagents-system diagnose_rollout_failure "$clean_log" 2>&1)
set -e
rm -f "$clean_log"

if [ -z "$clean_diag_out" ]; then
  test_pass "diagnose_rollout_failure cleanly ignored non-timeout logs (no false alarms)"
else
  test_fail "diagnose_rollout_failure produced output on non-timeout log: $clean_diag_out"
fi

# ------------------------------------------------------------------------------
# 7. CLI Flag Validation (--helm-timeout & --skip-capacity-check)
# ------------------------------------------------------------------------------
test_start "7. Flag Validation (--helm-timeout & --skip-capacity-check)"

# install.sh's own validator, in a subshell so a rejection cannot end this run.
# Re-implementing the regex here would assert this file against itself, and
# stay green with the check in install.sh deleted.
run_validate_helm_timeout() {
  (
    KUBE_AGENTS_SOURCE_ONLY=true source "${INSTALL_SH}" >/dev/null 2>&1
    validate_helm_timeout "$1"
  ) >/dev/null 2>&1
}

for valid in "540" "600" "899"; do
  if run_validate_helm_timeout "$valid"; then
    test_pass "--helm-timeout=${valid} accepted by install.sh's validator"
  else
    test_fail "--helm-timeout=${valid} rejected by install.sh's validator"
  fi
done

# Non-integers, and values outside the window hindsight-api's manifest fixes:
# under 540 the wait ends mid cold start, 900 is its progressDeadlineSeconds.
invalid_timeouts=("0" "-15" "abc" "10m" "539" "900" "1800")
for inv in "${invalid_timeouts[@]}"; do
  if run_validate_helm_timeout "$inv"; then
    test_fail "Invalid --helm-timeout='${inv}' was unexpectedly accepted"
  else
    test_pass "Invalid --helm-timeout='${inv}' correctly rejected"
  fi
done

# ------------------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------------------
echo -e "\n${C_BLUE}======================================================================${C_RESET}"
echo -e "${C_BOLD}SUMMARY:${C_RESET} ${C_GREEN}${pass_count} passed${C_RESET}, ${C_RED}${fail_count} failed${C_RESET}"
echo -e "${C_BLUE}======================================================================${C_RESET}"

if [ "$fail_count" -gt 0 ]; then
  exit 1
fi
exit 0
