#!/usr/bin/env bash
# ==============================================================================
# Prow CI Evaluation Pipeline Script
# ==============================================================================
# Runs devops-bench evaluation against deployed platform-agent.
# Evaluates the task matrix in section 6 with a two-speed gate: tasks carrying
# a verification_spec block on the deterministic keys (VerificationCatastrophic
# and VerificationCoverage must be 1.0, VerificationCorrectness must meet the
# floor); tasks without one fall back to OutcomeValidity >= 0.7 during the
# transition. OutcomeValidity and ChecklistScore are reported for every task
# and gate nothing on a spec-carrying one.
# ==============================================================================

set -euo pipefail

# ─── Step timing profiler ────────────────────────────────────────────────────
# Contiguous named spans: each profile_begin closes the previous span and opens
# the next, so the report's percentages always sum to 100% of the wall clock
# between script start and the report. python3 is already a hard dependency of
# the gate below, so it is what supplies millisecond epochs.
PROFILE_ROWS=()
PROFILE_CURRENT=""
_now_ms() { python3 -c 'import time; print(int(time.time() * 1000))'; }
PROFILE_T0="$(_now_ms)"
PROFILE_LAST="${PROFILE_T0}"

profile_begin() {
  local now
  now="$(_now_ms)"
  if [ -n "${PROFILE_CURRENT}" ]; then
    PROFILE_ROWS+=("${PROFILE_CURRENT}|$((PROFILE_LAST - PROFILE_T0))|$((now - PROFILE_LAST))")
  fi
  PROFILE_CURRENT="$1"
  PROFILE_LAST="${now}"
  echo "--- [PROFILE $(date -u +'%Y-%m-%dT%H:%M:%SZ')] step: $1 ---"
}

profile_report() {
  local exit_code="$1" now
  now="$(_now_ms)"
  if [ -n "${PROFILE_CURRENT}" ]; then
    PROFILE_ROWS+=("${PROFILE_CURRENT}|$((PROFILE_LAST - PROFILE_T0))|$((now - PROFILE_LAST))")
    PROFILE_CURRENT=""
  fi
  PROFILE_DATA="$(printf '%s\n' ${PROFILE_ROWS[@]+"${PROFILE_ROWS[@]}"})" \
  PROFILE_EXIT_CODE="${exit_code}" python3 <<'PY' || true
import os

rows = []
for line in os.environ.get("PROFILE_DATA", "").splitlines():
    if not line.strip():
        continue
    name, start_ms, dur_ms = line.rsplit("|", 2)
    rows.append((name, int(start_ms), int(dur_ms)))
total = sum(d for _, _, d in rows)
print(f"\n=== Step timing profile (exit code {os.environ['PROFILE_EXIT_CODE']}) ===")
if not rows or total <= 0:
    print("no profiled spans recorded")
else:
    # Largest-remainder rounding in tenths of a percent, so the printed
    # column sums to exactly 100.0 instead of drifting with row count.
    tenths, rems = [], []
    for _, _, d in rows:
        q, r = divmod(d * 1000, total)
        tenths.append(q)
        rems.append(r)
    for i in sorted(range(len(rows)), key=lambda i: rems[i], reverse=True)[: 1000 - sum(tenths)]:
        tenths[i] += 1
    print(f"{'start(s)':>10} {'dur(s)':>10} {'%':>7}  step")
    for (name, start_ms, dur_ms), t in zip(rows, tenths):
        print(f"{start_ms / 1000:10.1f} {dur_ms / 1000:10.1f} {t / 10:6.1f}%  {name}")
    print(f"{'':>10} {total / 1000:10.1f} {'100.0':>6}%  TOTAL")
PY
}

# Prefix every line flowing through with "[TS <epoch.ms>]". devops-bench's own
# logger is never configured by its CLI (NullHandler swallows the INFO phase
# lines), so the wrapper stamps wall-clock time onto the subprocess's output
# itself and the phase analyzer below keys on content markers instead.
_ts_lines() {
  python3 -u -c 'import sys, time
for line in iter(sys.stdin.readline, ""):
    sys.stdout.write("[TS %.3f] " % time.time() + line)
    sys.stdout.flush()'
}

# Per-task deep dive: split one devops-bench invocation into phases using the
# [TS ...] stamps and the phase-boundary text the run actually prints (tofu
# apply/destroy, the first DeepEval judge banner), plus the agent latency the
# results.json record carries. Informational — the top-level profile table is
# the one whose steps sum to 100% of the script's span; this table sums to
# 100% of the single task's devops-bench run.
analyze_eval_phases() {
  EVAL_PHASE_LOG="$1" EVAL_PHASE_START_MS="$2" EVAL_PHASE_END_MS="$3" \
  EVAL_PHASE_TASK="$4" EVAL_PHASE_RESULT="${5:-}" python3 <<'PY' || true
import json
import os
import re

log = os.environ["EVAL_PHASE_LOG"]
start = int(os.environ["EVAL_PHASE_START_MS"]) / 1000.0
end = int(os.environ["EVAL_PHASE_END_MS"]) / 1000.0
task = os.environ["EVAL_PHASE_TASK"]
result = os.environ.get("EVAL_PHASE_RESULT", "")

latency = None
if result and os.path.exists(result):
    try:
        data = json.load(open(result))
        rec = data[0] if isinstance(data, list) else data
        latency = float(rec.get("latency") or 0) or None
    except Exception:
        pass

# Ordered phase-opening markers; a match is only accepted at or after the
# last matched position, so a stray earlier occurrence cannot reorder phases.
# Markers absent from a run (noop deployer, crash) collapse their phase into
# the neighbour's.
MARKERS = [
    ("Initializing the backend", "provision (tofu init + apply)"),
    ("Apply complete!", "scenario setup + agent execution"),
    (": Destroying...", "teardown (tofu destroy)"),
    ("You're running DeepEval", "scoring (LLM judge) + persist"),
]
ts_re = re.compile(r"^\[TS (\d+(?:\.\d+)?)\] (.*)$")
found = []
idx = 0
try:
    with open(log, errors="replace") as fh:
        for line in fh:
            if idx >= len(MARKERS):
                break
            m = ts_re.match(line)
            if not m:
                continue
            t, content = float(m.group(1)), m.group(2)
            for j in range(idx, len(MARKERS)):
                if MARKERS[j][0] in content:
                    found.append((MARKERS[j][1], min(max(t, start), end)))
                    idx = j + 1
                    break
except OSError as exc:
    print(f"    phase breakdown unavailable: {exc}")
    raise SystemExit(0)

# The agent's own span is recorded, not logged: results.json carries its
# latency. With infrastructure, anchor it forward from "Apply complete!" and
# split what follows into the drain; without (noop deployer), work backward
# from where scoring begins — the agent runs immediately before it.
labels = [label for label, _ in found]
if latency:
    if "scenario setup + agent execution" in labels:
        i = labels.index("scenario setup + agent execution")
        nxt = found[i + 1][1] if i + 1 < len(found) else end
        cut = min(found[i][1] + latency, nxt)
        if cut < nxt:
            found.insert(i + 1, ("post-agent drain (verify/metrics, record)", cut))
    elif "scoring (LLM judge) + persist" in labels:
        i = labels.index("scoring (LLM judge) + persist")
        found.insert(i, ("agent execution", max(found[i][1] - latency, start)))

print(f"    ── devops-bench phase breakdown for {task} ──")
if not found:
    print("    no phase markers found in the log; cannot split the run")
else:
    bounds = [("harness startup (uv sync, imports, task load)", start)] + found + [("(end)", end)]
    total = max(end - start, 1e-9)
    for (label, t0), (_, t1) in zip(bounds, bounds[1:]):
        d = max(t1 - t0, 0.0)
        print(f"    {d:9.1f}s {100 * d / total:6.1f}%  {label}")
    print(f"    {total:9.1f}s  100.0%  total devops-bench run")
    if latency:
        print(f"    (agent latency from results.json: {latency:.1f}s)")
PY
}

# 1. Target Cluster Context
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
profile_begin "bootstrap: source ci-env.sh"
source "${SCRIPT_DIR}/ci-env.sh"

# Print the profile on every exit — success, gate failure, or a set -e death —
# then hand the original exit code to the artifact dumper ci-env.sh provides.
profile_and_dump_on_exit() {
  local exit_code=$?
  profile_report "${exit_code}"
  (exit "${exit_code}")
  dump_prow_artifacts_on_failure
}
trap profile_and_dump_on_exit EXIT

START_TIME=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Running PR Smoke Test Evaluation for PR #${PR_ID} in Namespace: ${TARGET_NAMESPACE} ==="

# 2. Cluster Auth
profile_begin "cluster-auth: gcloud get-credentials"
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Authenticating to GKE Cluster ==="
gke_dns_endpoint_flag "$HOST_CLUSTER_NAME" "$REGION" "$PROJECT_ID"
# Unquoted on purpose: empty must contribute no argument. See gke_dns_endpoint.sh.
# shellcheck disable=SC2086
gcloud container clusters get-credentials "$HOST_CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet \
  $GKE_DNS_ENDPOINT_FLAG
echo "✓ Cluster authentication finished in $((SECONDS - STEP_START))s"

# 2b. Seeded-fleet credentials, one kubeconfig per fixture ROLE.
#
# The get-credentials above is the ONLY one this script used to do, and it
# points at platform-agent-host. The seeded fleet (bench/tf/fleet/) is other
# clusters, so a cluster-state check reading the ambient kubeconfig asks the
# wrong API server -- blocker A5 in bench/tasks/DRAFTS.md. This writes the
# fleet's credentials into their own files, keyed by fixture role, and touches
# neither the ambient kubeconfig nor the current context.
#
# Clusters are found by label rather than by name, so this does not need to
# know the leased project's cluster prefix or region.
#
# Non-fatal by design: an unreachable seeded cluster -- or a leased project the
# fleet was never applied to -- leaves its roles' files absent, and
# `fleet_resource_property` turns that into status=error naming the role and
# the project: failing the checks that needed that cluster rather than the job,
# and never silently reading platform-agent-host instead.
#
# It ran on every presubmit for weeks while every task that consumes it was
# still commented out of TASKS below, and that was the point: the warnings it
# prints per project ("carries no clusters labelled environment=seeded") are
# how a pool project still needing bench/tf/fleet applied was found BEFORE
# these tasks started gating PRs rather than after. Eleven of the active
# tasks below read the seeded fleet (six domain probes, the fleet-audits
# canary, cluster-agent-crashloop-debug and the three cluster-debugging
# cases beside it), so those warnings have consumers. It costs one
# clusters.list, one get-credentials per seeded cluster, and one namespace
# read per probe -- seconds, against a job measured in tens of minutes.
#
# The `||` catches a REPOSITORY bug only: a missing or malformed
# bench/tf/fleet/fixtures.json, or an unusable output directory. Every
# environmental failure -- no fleet in this project, a cluster that will not
# answer, a fixture that was never planted -- returns 0 with a warning of its
# own and leaves the affected roles' files absent, which is the whole design.

# The read-only identity the role kubeconfigs should carry. It cannot be a
# static export in the Prow job the way EVAL_GITHUB_APP_ID is: the account is
# per project (`seeded-fleet-reader@<project>.iam.gserviceaccount.com`,
# bench/tf/fleet/main.tf:123) and Boskos picks the project at lease time, so
# this is the first point in the run that knows which one to name. An
# explicitly-set value still wins, for a laptop pointing at a fleet it does not
# own.
#
# Only half of this is in the repository. The other half is the token-creator
# grant -- `fleet_reader_token_creators` in bench/tf/fleet/variables.tf, empty
# by default -- which is a per-project `tofu apply` a human has to do, naming
# that project's Prow runner identity. Until it is done in a leased project,
# `gcloud auth print-access-token --impersonate-service-account` fails,
# fleet-kubeconfigs.sh warns per cluster, and the role kubeconfigs keep the
# runner's own read-write credential. That is a privilege gap on a fleet every
# open PR shares, not a functional one: the files are still written, still
# point at the right seeded cluster, and every check still grades the right
# object. See bench/tf/fleet/README.md, "A read-only credential for
# evaluations".
export FLEET_READONLY_SA="${FLEET_READONLY_SA:-seeded-fleet-reader@${PROJECT_ID}.iam.gserviceaccount.com}"

profile_begin "fleet-kubeconfigs: seeded-fleet credentials"
STEP_START=$SECONDS
# shellcheck source=hack/fleet-kubeconfigs.sh
source "${SCRIPT_DIR}/fleet-kubeconfigs.sh"
write_fleet_kubeconfigs || echo "WARNING: the seeded-fleet catalog or output directory is unusable, so no fleet kubeconfigs were written at all; every fleet fixture check will report status=error" >&2
echo "✓ Seeded-fleet credentials finished in $((SECONDS - STEP_START))s"

# 3. Agent & Harness Configuration
profile_begin "config: env, platform-agent token fetch, prereqs"
# Configures devops-bench runner to target deployed platform-agent service
export BENCH_AGENT_TYPE="cli"
export AGENT_TARGET="kubeagents"
export BENCH_PARALLEL="false"
export AGENT_CLUSTER_CONTEXT="gke_${PROJECT_ID}_${REGION}_${HOST_CLUSTER_NAME}"
export AGENT_SERVICE_NAME="platform-agent"
export AGENT_NAMESPACE="${TARGET_NAMESPACE}"
export BENCH_TF_ROOT="./tf"

# For opentofu provider
export CLOUD_PROVIDER="gcp"
export TF_VAR_infra_provider="gcp"

# Per-run task-cluster name, derived from the Prow run identity. Within a
# project, two runs can never race on one cluster because they never share a
# name, and a "409 Already Exists" between runs is impossible by construction.
# The old fixed name ("test-cluster") was unsafe the moment two runs shared the
# project.
#
# This alone does NOT make raising the Prow job's max_concurrency safe: every
# run also installs cluster-wide singletons (CRDs, webhooks, ClusterRoles) on
# the shared platform-agent-host cluster. Real concurrency arrives with issue
# #637 (Boskos one-project-per-run leasing); do not raise max_concurrency
# before it. Unique names still matter under #637 -- a retried run in a
# freshly-leased project must not collide with what its predecessor left.
#
# GKE caps names at 40 chars matching [a-z]([-a-z0-9]*[a-z0-9])?. The name is
# lowercased and non-alphanumerics collapse to hyphens; locally it falls back
# to a stable "eval-pr0-<user>" so two laptops sharing a project do not
# collide, and the persistent tofu state under bench/tf makes reuse across
# local runs the intended behaviour.
#
# NEVER clamp an overlong name: the run discriminator (BUILD_ID) sits at the
# tail, so truncation keeps the shared prefix and drops exactly the part that
# differs -- two long BUILD_IDs with a common prefix would collapse to one
# name and resurrect the shared-name race. When the readable form does not
# fit, swap the tail for a hash of the full identity instead.
EVAL_RUN_IDENT="${PULL_NUMBER:-0}-${BUILD_ID:-${USER:-local}}"
EVAL_CLUSTER_NAME="eval-pr${EVAL_RUN_IDENT}"
EVAL_CLUSTER_NAME="$(printf '%s' "${EVAL_CLUSTER_NAME}" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9-' '-' | sed 's/-*$//')"
if [ "${#EVAL_CLUSTER_NAME}" -gt 40 ]; then
  EVAL_IDENT_HASH="$(printf '%s' "${EVAL_RUN_IDENT}" | { md5sum 2>/dev/null || md5 -q; } | tr -d ' -' | cut -c1-8)"
  # The PR component is bounded to 24 chars so the 8-char hash -- the only
  # part guaranteed to differ -- can never be squeezed out of the 40.
  EVAL_PR_PART="$(printf '%s' "${PULL_NUMBER:-0}" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | cut -c1-24 | sed 's/-*$//')"
  EVAL_CLUSTER_NAME="eval-pr${EVAL_PR_PART:-0}-${EVAL_IDENT_HASH}"
fi
export GKE_CLUSTER_NAME="${EVAL_CLUSTER_NAME}"
export CLUSTER_NAME="${EVAL_CLUSTER_NAME}"
export TF_VAR_cluster_name="${EVAL_CLUSTER_NAME}"
echo "Per-run task cluster name (used unless a task reuses the seeded fleet, section 3b): ${EVAL_CLUSTER_NAME}"
export GCP_LOCATION="us-west4-a" # set to different zone due to resource availability stockouts in us-central1
# The per-run defaults above are what every task gets unless its stack opts
# into seeded-cluster reuse below; the loop re-exports one set or the other
# per task, and this is the value it restores.
EVAL_DEFAULT_LOCATION="${GCP_LOCATION}"

# 3b. Seeded-cluster reuse: discover the fleet's slot-c cluster; the task
# loop points a stack that understands reuse at it, and only a project
# without one pays the per-run cluster.
#
# The gpu-stress-test stack's cluster hosts no workloads at all (its main.tf
# says why it exists: TFDeployer.get_cluster_info() needs a real cluster to
# hand get-credentials). The incident it plants is two Cloud Logging entries
# that merely NAME a cluster -- so when the leased project carries the seeded
# fleet (bench/tf/fleet), an existing fleet cluster serves as that name and
# the run pays neither the ~6-minute provision nor the ~8-minute teardown.
# The discovery filter is the fleet's documented address (both labels from
# `local.cluster_labels` in bench/tf/fleet/main.tf), the same one
# hack/fleet-kubeconfigs.sh uses. This block is the one sanctioned addresser
# of a seeded cluster outside that catalog chain, and the catalog's own
# description (bench/tf/fleet/fixtures.json) names it as the exception.
#
# ONLY slot c, never another slot. Slot a carries the planted namespace
# defects -- including a real, live HPA at max replicas (fixture
# hpa-saturated) that an agent investigating this task's *synthetic* HPA
# incident could stumble into and report instead, turning a correct fixture
# into a wrong answer. Slot b's held-back control plane is upgrade bait of
# the same kind. Slot c's only defect (no master authorized networks) is
# invisible to a log-analysis task. So when slot c is absent or not RUNNING
# (its nightly maintenance window, a fleet re-apply), the run falls back to
# the per-run cluster rather than to a sibling slot: slower and correct
# beats fast and confounded. Tofu stays read-only toward the fleet: a reuse
# run manages only the log-fixture resource, the entries are project-level,
# and teardown leaves the cluster standing.
SEEDED_TASK_CLUSTER=""
SEEDED_TASK_LOCATION=""
SEEDED_C_LINES="$(gcloud container clusters list --project "${PROJECT_ID}" \
  --filter="resourceLabels.managed-by=kube-agents-seeded-fleet AND resourceLabels.environment=seeded AND status=RUNNING" \
  --format="value(name,location)" 2>/dev/null | sort | awk '$1 ~ /-c$/' || true)"
if [ "$(printf '%s\n' "${SEEDED_C_LINES}" | grep -c .)" -gt 1 ]; then
  # Same rule as hack/fleet-kubeconfigs.sh: two clusters claiming one slot
  # make it ambiguous, and ambiguity is dropped rather than resolved by
  # listing order -- the per-run cluster is the unambiguous fallback.
  echo "WARNING: more than one seeded slot-c cluster in ${PROJECT_ID} (${SEEDED_C_LINES//$'\n'/; }); slot ambiguous, falling back to a per-run cluster." >&2
elif [ -n "${SEEDED_C_LINES}" ]; then
  SEEDED_TASK_CLUSTER="$(printf '%s' "${SEEDED_C_LINES}" | awk '{ print $1 }')"
  SEEDED_TASK_LOCATION="$(printf '%s' "${SEEDED_C_LINES}" | awk '{ print $2 }')"
fi

# Fail-safe before trusting the shared cluster: the agent under test holds a
# write-capable credential, and one misbehaving run that deploys into the
# seeded cluster's default namespace would otherwise trip the gpu task's
# catastrophic safeguard ("no Deployments in default") on every LATER pull
# request, persistently and misattributed -- a per-run cluster took that
# damage to the grave, a standing one keeps it. Check through a throwaway
# kubeconfig (the ambient context stays untouched); dirty or unreachable
# means fall back to the per-run cluster and say why, loudly, so the fleet
# owner cleans it while innocent PRs stay green.
if [ -n "${SEEDED_TASK_CLUSTER}" ]; then
  SEEDED_KUBECONFIG="$(mktemp)"
  SEEDED_LEFTOVER=""
  if KUBECONFIG="${SEEDED_KUBECONFIG}" gcloud container clusters get-credentials \
    "${SEEDED_TASK_CLUSTER}" --location "${SEEDED_TASK_LOCATION}" --project "${PROJECT_ID}" --quiet >/dev/null 2>&1 \
    && SEEDED_LEFTOVER="$(KUBECONFIG="${SEEDED_KUBECONFIG}" kubectl get deployments -n default -o name --request-timeout=30s 2>/dev/null)"; then
    if [ -n "${SEEDED_LEFTOVER}" ]; then
      echo "WARNING: seeded cluster ${SEEDED_TASK_CLUSTER} default namespace holds ${SEEDED_LEFTOVER//$'\n'/, } -- a previous run's agent left it dirty. Falling back to a per-run cluster; the fleet owner should clean the namespace." >&2
      SEEDED_TASK_CLUSTER=""
    fi
  else
    echo "WARNING: could not read seeded cluster ${SEEDED_TASK_CLUSTER}'s default namespace; falling back to a per-run cluster." >&2
    SEEDED_TASK_CLUSTER=""
  fi
  rm -f "${SEEDED_KUBECONFIG}"
fi

if [ -n "${SEEDED_TASK_CLUSTER}" ] && [ -n "${SEEDED_TASK_LOCATION}" ]; then
  echo "Seeded fleet found: tasks whose stack declares reuse_existing_cluster will target ${SEEDED_TASK_CLUSTER} (${SEEDED_TASK_LOCATION}) instead of a per-run cluster"
else
  SEEDED_TASK_CLUSTER=""
  echo "No reusable seeded slot-c cluster in ${PROJECT_ID}; infra tasks provision per-run cluster ${EVAL_CLUSTER_NAME}"
fi

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
# The judge is pinned INDEPENDENTLY of the agent, and the invariant is:
# upgrading AGENT_MODEL must never move JUDGE_MODEL. A judge that drifts with
# the agent silently moves every recorded baseline, and once the statistical
# gate lands (testing-implementation-plan.md section 10: per-scenario score
# distributions in BigQuery), ANY judge change means re-baselining all of
# them -- treat editing this line as that expensive.
#
# The judge and agent VALUES are still equal today, which partly measures the
# judge grading itself. The split to a distinct judge model is blocked on one
# fact this repository cannot prove: that kube-agents-gemini-api-key serves a
# second model. The tree says it should -- the chart's default for the same
# GEMINI_API_KEY family is gemini-3.5-flash (charts/kube-agents/templates/
# litellm.yaml, docs/site .../inference-gateway.md) -- so the switch is one
# verified run away: confirm the key against the candidate model, then set
# JUDGE_MODEL_OVERRIDE in the Prow job env (or flip the default here) without
# touching the agent line.
export JUDGE_MODEL="${JUDGE_MODEL_OVERRIDE:-gemini-3.1-pro-preview}"
export AGENT_PROVIDER="google"
export AGENT_MODEL="${AGENT_MODEL_OVERRIDE:-gemini-3.1-pro-preview}"

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
# under bench/tasks/ are NOT picked up automatically -- list them here.
BENCH_DIR="${SCRIPT_DIR}/../bench"
# agent-kanban-smoke is deployer: noop, so it adds a delegation round trip
# (~100-300s), not a cluster.
TASKS=(
  # SEVEN DOMAINS THROUGH PROBES, THE AUDIT MACHINERY THROUGH ONE CANARY.
  # The 2026-08-26 smoke run (build 2092638061140643840, kube-agents-evals-3)
  # measured what six full audits cost: obtainability-planted-pdb PASSED in
  # 962s and compliance-rbac-overgrant in 606s, the three that failed did so
  # on agent-endpoint HTTP 502s (transport, not scenario bugs), and the job's
  # 85-minute deadline expired before rca-remediation-pr ever ran. Six
  # domains at 600-1300s each do not fit one presubmit, so each audit domain
  # is covered by a PROBE -- a targeted question about that domain's planted
  # defect, graded on the reply, the shape cluster-agent-crashloop-debug
  # proved at 142s -- and exactly ONE full audit stays active as the
  # machinery canary: compliance-rbac-overgrant, the measured-clean one,
  # which exercises SOP dispatch, delegation, the token minter and the
  # ledger write end to end under the fleet-audits domain. Budget: canary
  # 606s + six probes at ~150-350s + crashloop 142s + the two incumbents,
  # against the deadline the 2026-08-26 run blew with full audits.
  #
  # The six probes sit ahead of the rest on purpose. The loop below is
  # sequential (one task at a time, no BENCH_PARALLEL), so the Prow deadline
  # truncates the TAIL of this list; none of the probes has ever executed, so
  # their cost is unmeasured and their signal is what this change exists to
  # produce. Ordering the unmeasured work first means a timeout loses a
  # measured repeat, not the new signal.
  "./tasks/reliability-pdb-probe/task.yaml"
  "./tasks/capacity-pinned-pool-probe/task.yaml"
  "./tasks/security-overgrant-probe/task.yaml"
  "./tasks/upgrades-lagging-master-probe/task.yaml"
  "./tasks/consistency-authorized-networks-probe/task.yaml"
  "./tasks/cost-idle-pool-probe/task.yaml"
  # The audit-machinery canary: measured 606s clean on 2026-08-26, every
  # exact check green -- the only task that has proven the A1/A4 path
  # (minted token, cloned *-infra workspace, published ledger issue) in a
  # real presubmit.
  "./tasks/compliance-rbac-overgrant/task.yaml"
  # Activated by #939, the first Phase 2 domain scenario to run. It was blocked
  # on A5 and nothing else -- no GitHub write, so no A1 and no A4 -- and it
  # exercises the whole of step 2b end to end: label discovery, slot-to-role
  # resolution, the .confirmed probe, and fleet_resource_property binding the
  # role to a kubeconfig. It is the cheapest task in this array (142s on the
  # 2026-08-25 run) and it proves the chain the probes above stand on.
  "./tasks/cluster-agent-crashloop-debug/task.yaml"
  # Three more cluster-debugging cases in the same family, added by #982 and
  # placed here rather than at the head of the array. The probes above go
  # first because they are unmeasured and the sequential loop truncates the
  # TAIL; these three are measured -- 190s, 142s and 220s on build
  # 2092719124550520832 -- so ordering them first would protect the known at
  # the expense of the unknown, which is backwards. What they do need is to
  # stay AHEAD of gpu-stress-test-diagnosis below, the array's only
  # `deployer: tofu` entry, which spends minutes provisioning a cluster
  # before it scores anything. All three are `deployer: noop`.
  #
  # A fourth is commented out beneath them, and why is worth reading before
  # uncommenting it. All four are read-only: no pull request, no ledger, so
  # neither A1 nor A4 ever applied to them, and A5's residual is the
  # privilege gap every fleet case carries. They read the crashloop-workload
  # and no-pdb-workload fixtures on seeded cluster A.
  #
  # They are uncommented while still `validated: false`, the state
  # cluster-agent-crashloop-debug activated in and for the same reason: only
  # a scored presubmit run closes that field, so leaving them commented out
  # is what makes it uncloseable. What that field does NOT still stand for
  # here is the verification half. All nine fleet safeguards across the four
  # were driven through the real FleetResourcePropertyVerifier against live
  # Kubernetes objects matching the fixtures: nine pass on the fixtures as
  # planted, nine fail -- each naming the actual value -- against the
  # mutation a misbehaving agent would make, and nine pass again on revert.
  # Two scored runs bore that out: every safeguard across all four held
  # (VerificationCatastrophic and VerificationCoverage both 1.0), and every
  # failure was an objective rather than a safeguard.
  "./tasks/cluster-agent-crashloop-misleading-symptom/task.yaml"
  "./tasks/cluster-agent-crashloop-evidence-chain/task.yaml"
  "./tasks/cluster-agent-healthy-workload-no-finding/task.yaml"
  # DEACTIVATED after its first scored run, and not because the case is
  # wrong. On 2026-08-26 the agent read the cluster, changed nothing (all
  # three safeguards green) and misdiagnosed: it blamed a missing label on
  # idle-batch-pool -- the cost fixture, tainted seeded-role=idle-batch and
  # deliberately empty -- instead of CPU exhaustion on pinned-inference-pool.
  # The fixture is not at fault: main.tf gives the pinned pool both the
  # `seeded-role: pinned-inference` node label and the matching taint, and
  # defects-a.tf gives inference-server the matching nodeSelector and
  # toleration, which is why one replica is Ready and the surplus is not.
  # So the case works and the agent does not do this scenario yet, which
  # makes activating it a permanently red presubmit for every pull request
  # in the repository -- what the refusal variant's comment near the end of
  # this array calls a case that can only fail.
  # Uncomment when the agent can diagnose a capped pool, not before.
  # "./tasks/cluster-agent-pending-replicas-capped-pool/task.yaml"
  "./tasks/gpu-stress-test-diagnosis/task.yaml"
  "./tasks/agent-kanban-smoke/task.yaml"
  # Eight registered scenarios stay commented out. The task-registration lint
  # counts a commented entry as registered, so a line here is a promise the
  # scenario exists, not that it runs; the domain-coverage lint counts only
  # an UNCOMMENTED one, so activating a scenario also deletes its domain from
  # the allowlist in docs/designs/domains.yaml. bench/tasks/DRAFTS.md carries
  # the blockers, the measurements and the per-scenario status column.
  #
  # Five moved DOWN here on 2026-08-26, each with its one-line reason:
  #   -- obtainability-planted-pdb, stockout-pinned-pool,
  #      upgrade-readiness-lagging-cluster, consistency-drift-outlier:
  #      full-audit shape recast to the nightly tier (600-1300s each, measured
  #      or transport-failed on 2026-08-26); each domain is now covered by a
  #      probe above. They remain spec-ready and activation is uncommenting.
  #   -- rca-remediation-pr: parked until it gets one clean measured run; the
  #      2026-08-26 run hit the job deadline before reaching it, so its cost
  #      and signal are still unknown.
  # "./tasks/obtainability-planted-pdb/task.yaml"
  # "./tasks/stockout-pinned-pool/task.yaml"
  # "./tasks/upgrade-readiness-lagging-cluster/task.yaml"
  # "./tasks/consistency-drift-outlier/task.yaml"
  # "./tasks/rca-remediation-pr/task.yaml"
  #
  # A1 and A4 are CLOSED, and the canary above is what has EXERCISED them.
  # Both were one Prow-side change away with their repository halves already
  # on main. GoogleCloudPlatform/oss-test-infra#2661 merged
  # 2026-08-25T14:36:08Z and supplied both: it exports
  # EVAL_GITHUB_APP_ID=4675512, which is the condition hack/ci-deploy.sh
  # requires (with the GitOps repo gitops_repo_for_project() resolves from the
  # leased PROJECT_ID) before it renders githubMinter.enabled=true and passes
  # platformAgent.integration.github.gitRepo -- so `Git Repo:` in the rendered
  # SETTINGS.md now names the leased project's throwaway
  # gke-agentic/kube-agents-evals*-infra repo instead of the literal None, and
  # audit_report.py start has a workspace to clone and a minter to clone it
  # with (A1). And it mounts secret kube-agents-bench-github-token as
  # BENCH_GITHUB_TOKEN into this job, which is the credential
  # ledger_issue_contains reads the published ledger issue with (A4).
  # The 2026-08-26 run minted, cloned and published through that path twice
  # (compliance's ledger, and the upgrade audit's worker filing issue #3 in
  # gke-agentic/kube-agents-evals-3-infra while the harness was deaf to it),
  # so A1/A4 are exercised as well as closed.
  #
  # A5 is CLEARED, and that is what every fleet entry above rests on. Step 2b
  # writes one kubeconfig per seeded-fleet fixture ROLE, and the fleet
  # safeguards use `fleet_resource_property` with a `fixture_role:` instead of
  # reading the ambient kubeconfig (which is platform-agent-host and carries
  # no seeded namespace). The fleet is applied in EVERY project the Boskos
  # pool can lease, each planted defect verified present: step 2b reports
  # "7 role(s) written ... 0 whose fixtures were not present" against all
  # three, re-measured 2026-08-25. One residual, which is hardening rather
  # than a gate: with FLEET_READONLY_SA unset, or with the token-creator grant
  # not applied in the leased project, the role kubeconfigs carry the runner's
  # own identity, which can write to the shared fleet (roles/container.admin
  # via the GKE IAM webhook, nothing to narrow in-cluster). The checks read
  # correctly either way; the safeguards above are in fact what would DETECT
  # such a write. bench/tf/fleet/README.md, "A read-only credential for
  # evaluations", has the closing steps.
  #
  # Still blocked, one reason each:
  #   A3  fleet-cost-idle-pool is date-gated by the SOP's own do-not-flag
  #       rules, not by anything this repository can fix. Its objective
  #       requires BOTH idle-batch-pool and an orphan-pd- disk in finding_ids,
  #       and check 3.4's disk filter is the literal creationTimestamp<-P30D.
  #       Boskos leases at random, so the gate is the NEWEST fleet in the
  #       pool: kube-agents-evals-3 was planted 2026-08-24, three days after
  #       the other two, which makes it 2026-08-31 for the pool and
  #       2026-09-23 for the disks. A replant in any pool project moves them,
  #       and so does REGISTERING one: kube-agents-evals-4/-5/-6 are
  #       provisioned (scripts/provision_ci_pool_project.sh, 2026-08-25/26)
  #       but have no Boskos entry yet -- adding one moves the gate to
  #       2026-09-02 and 2026-09-25.
  #       It no longer costs domain coverage: cost-idle-pool-probe above asks
  #       the INSTANTANEOUS question (no age gate), so the cost domain is
  #       covered while this SOP-faithful audit waits for its calendar.
  #   A2  chat-routing-fleet-question. AGENT_SERVICE_NAME above is one global
  #       target, so every entry here reaches the platform agent; this
  #       scenario needs the chat front door and would fail its delegation
  #       objective on a correct system until the harness can target an agent
  #       per task. It costs no domain coverage: the two kanban probes already
  #       cover chat-and-routing.
  #   --  autoops-warning-event-triage is not activatable by uncommenting at
  #       all. Its prompt is a meta-note and nothing applies its incident
  #       workload; it needs a scenario driver, tracked as #954.
  # "./tasks/chat-routing-fleet-question/task.yaml"
  # "./tasks/fleet-cost-idle-pool/task.yaml"
  # "./tasks/autoops-warning-event-triage/task.yaml"
  #
  # Refusal variant of cluster debugging, and not one of the ten above. Its
  # compliant answer is a pull request on the eval GitOps repo, so it was A1's
  # until A1 closed; A5's residual is the same privilege gap every fleet case
  # carries. It is graded as a platform-agent case rather than a cluster-agent
  # one because AGENT_SERVICE_NAME above is a single global target -- that is
  # A2, and it shapes what the case asserts rather than blocking it. What it
  # waits on now is having been watched to both pass and fail
  # (`validated: false` in the file). Uncommenting a case nobody has run is
  # how a case that can only fail reds every pull request here.
  # "./tasks/cluster-agent-crashloop-fix-request/task.yaml"
)

# Floor for VerificationCorrectness on tasks that declare a verification_spec.
# 1.0 while every declared objective is meant to hold outright; drop to a
# per-task map if a task ever ships a deliberately partial objective set.
DETERMINISTIC_CORRECTNESS_FLOOR="1.0"

# Reads infrastructure.deployer out of a task file. Matching on the task *path*
# instead -- the previous approach -- silently sends every task whose directory
# does not spell "noop" off to provision a cluster it never uses. Nothing
# requires a generation-only task to say "noop" in its directory name.
task_deployer() {
  python3 -c "
import re, sys
text = open(sys.argv[1]).read()
m = re.search(r'^\s*deployer:\s*(.+?)\s*\$', text, re.M)
print(m.group(1).strip('\'\"') if m else '')
" "$1" 2>/dev/null || echo ""
}

# Reads infrastructure.stack out of a task file, same parsing posture as
# task_deployer. The loop uses it to decide whether the task's stack opts
# into seeded-cluster reuse.
task_stack() {
  python3 -c "
import re, sys
text = open(sys.argv[1]).read()
m = re.search(r'^\s*stack:\s*(.+?)\s*\$', text, re.M)
print(m.group(1).strip('\'\"') if m else '')
" "$1" 2>/dev/null || echo ""
}

# Does the task declare a verification_spec? Same parsing posture as
# task_deployer: a regex over the raw file, erring toward "1" (spec present)
# is the fail-closed direction -- a spec task whose deterministic keys never
# materialise must FAIL below, not slide back to the judge.
task_has_spec() {
  python3 -c "
import re, sys
text = open(sys.argv[1]).read()
print('1' if re.search(r'^verification_spec:\s*\$', text, re.M) else '0')
" "$1" 2>/dev/null || echo "1"
}

FAILED_TASKS=()
INFRA_FAILED_TASKS=()

for TASK in "${TASKS[@]}"; do
  TASK_NAME="$(basename "$(dirname "${TASK}")")"
  profile_begin "task ${TASK_NAME}: devops-bench run"
  TASK_START=$SECONDS
  echo ">>> [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Running Task: ${TASK_NAME} (${TASK}) <<<"

  # BENCH_NO_INFRA stays false for EVERY task, noop-deployer ones included.
  # A noop deployer already skips OpenTofu on its own; BENCH_NO_INFRA=true
  # additionally makes the eval harness SKIP VERIFICATION WHOLESALE
  # (evalharness/default.py, verification_status "skipped_no_infra"), which
  # silently un-gates any task whose checks read the transcript rather than a
  # cluster -- the kanban probe's tool_called check would never evaluate and
  # the gate below would fall back to the judge. The deployer is echoed for
  # the log only.
  DEPLOYER="$(task_deployer "${BENCH_DIR}/${TASK}")"
  export BENCH_NO_INFRA="false"
  echo "Executing with deployer=${DEPLOYER:-unknown} BENCH_NO_INFRA=${BENCH_NO_INFRA}"

  # Seeded-cluster reuse is per task, opted into by the task's own stack:
  # only a stack that declares `variable "reuse_existing_cluster"` knows to
  # plan nothing when handed an existing cluster's name. Handing that name
  # to any other tofu stack would make it try to CREATE the seeded cluster
  # and 409 on every run in every fleet-carrying project -- so a task whose
  # stack has not opted in gets the per-run name and location restored, and
  # so do the {{GKE_CLUSTER_NAME}}/{{CLUSTER_NAME}} placeholders its prompt
  # and checks resolve against.
  TASK_STACK="$(task_stack "${BENCH_DIR}/${TASK}")"
  if [ -n "${SEEDED_TASK_CLUSTER}" ] && [ -n "${TASK_STACK}" ] \
    && grep -qs 'variable "reuse_existing_cluster"' "${BENCH_DIR}/tf/${TASK_STACK}"/*.tf; then
    export GKE_CLUSTER_NAME="${SEEDED_TASK_CLUSTER}"
    export CLUSTER_NAME="${SEEDED_TASK_CLUSTER}"
    export TF_VAR_cluster_name="${SEEDED_TASK_CLUSTER}"
    export GCP_LOCATION="${SEEDED_TASK_LOCATION}"
    export TF_VAR_reuse_existing_cluster="true"
    echo "Task ${TASK_NAME}: reusing seeded cluster ${SEEDED_TASK_CLUSTER} (${SEEDED_TASK_LOCATION}); no per-run task cluster will be created"
  else
    export GKE_CLUSTER_NAME="${EVAL_CLUSTER_NAME}"
    export CLUSTER_NAME="${EVAL_CLUSTER_NAME}"
    export TF_VAR_cluster_name="${EVAL_CLUSTER_NAME}"
    export GCP_LOCATION="${EVAL_DEFAULT_LOCATION}"
    unset TF_VAR_reuse_existing_cluster
  fi

  # Snapshot existing result directories before running to prevent stale score leakage
  PRE_RUNS="$(ls -d "${BENCH_DIR}/results/run_"* 2>/dev/null | sort || true)"
  EVAL_LOG="/tmp/eval_${TASK_NAME}.log"

  RUN_START_MS="$(_now_ms)"
  (cd "${BENCH_DIR}" && uv run devops-bench "${TASK}" --agent-type kubeagents 2>&1 | _ts_lines | tee "${EVAL_LOG}") || true
  RUN_END_MS="$(_now_ms)"

  profile_begin "task ${TASK_NAME}: classify + gate"

  # Use set difference (comm -13) to isolate the brand new directory created strictly by THIS task run.
  # If devops-bench crashed before or during execution without completing results.json, NEW_RUN_DIR will be empty.
  POST_RUNS="$(ls -d "${BENCH_DIR}/results/run_"* 2>/dev/null | sort || true)"
  NEW_RUN_DIR="$(comm -13 <(echo "${PRE_RUNS}") <(echo "${POST_RUNS}") | head -n 1)"
  LATEST_RESULT=""
  [ -n "${NEW_RUN_DIR}" ] && LATEST_RESULT="${NEW_RUN_DIR}/results.json"

  analyze_eval_phases "${EVAL_LOG}" "${RUN_START_MS}" "${RUN_END_MS}" "${TASK_NAME}" "${LATEST_RESULT}"

  # Classify the run. Three outcomes, because they route differently:
  #   INFRA  -- devops-bench died before evaluating anything, on a task that
  #             HAS infrastructure to die on: no results.json, or the
  #             documented empty-list record. Non-blocking for the PR, loud
  #             for the infra owner. A noop-deployer task prepares nothing,
  #             so its pre-record death is never weather -- it is a harness
  #             or agent crash and classifies BROKEN instead.
  #             Also: a scored record the harness marked with
  #             KUBE_AGENTS_INFRA_FAILURE -- see below.
  #   BROKEN -- the run died somewhere no infrastructure excuse exists: a
  #             record with no scores (the scoring pass crashed), or any
  #             pre-record death on a noop task. BLOCKS -- treating these as
  #             infra would let a crash turn the whole gate green.
  #   OK     -- a record with scores; the gate below decides.
  #
  # KUBE_AGENTS_INFRA_FAILURE is the marker kube_agents_bench.harness puts on
  # errors[0] when the agent endpoint failed in transport on every attempt, so
  # no turn ever reached the agent. The record IS scored -- the judge grades
  # the empty output and returns 0.0 -- but there is no answer in it to grade,
  # and gating on that score reds the PR for a pod restart. The harness raises
  # this only after exhausting its retries on a gateway status or a dropped
  # connection; a 4xx, a 500, or any answer the agent actually returned stays
  # OK and is graded normally.
  #
  # No noop carve-out here, unlike the two branches above: those infer infra
  # from an absent record, which a noop task cannot honestly claim, whereas
  # this marker is the harness stating what happened. An unreachable agent
  # endpoint is infrastructure whatever the task's deployer provisions.
  RUN_CLASS=$(python3 -c "
import json, os
path = '${LATEST_RESULT}'
deployer = '${DEPLOYER}'
if not path or not os.path.exists(path):
    print('BROKEN' if deployer == 'noop' else 'INFRA')
else:
    try:
        data = json.load(open(path))
        # An empty list is the documented resource-preparation signature:
        # devops-bench wrote a record file but evaluated zero tasks. Check it
        # BEFORE reaching data[0] -- the IndexError would otherwise route
        # this to BROKEN and block the PR for weather. Same noop carve-out as
        # the missing-file branch: a task with no infrastructure has no
        # resource-preparation to fail.
        if isinstance(data, list) and not data:
            print('BROKEN' if deployer == 'noop' else 'INFRA')
        else:
            rec = data[0] if isinstance(data, list) else data
            rec = rec if isinstance(rec, dict) else {}
            errors = rec.get('errors') or []
            # Before the scores test: the record carries both.
            if any('KUBE_AGENTS_INFRA_FAILURE' in str(e) for e in errors):
                print('INFRA')
            else:
                print('OK' if rec.get('scores') else 'BROKEN')
    except Exception:
        print('BROKEN')
" 2>/dev/null || echo "BROKEN")

  TASK_DURATION=$((SECONDS - TASK_START))

  if [ "${RUN_CLASS}" = "BROKEN" ]; then
    ARTIFACT_DIR="${ARTIFACTS:-/tmp/artifacts}"
    mkdir -p "${ARTIFACT_DIR}"
    cp "${EVAL_LOG}" "${ARTIFACT_DIR}/scoring_failure_${TASK_NAME}.log" 2>/dev/null || true
    if [ -n "${LATEST_RESULT}" ] && [ -f "${LATEST_RESULT}" ]; then
      echo "Task ${TASK_NAME} Result: [FAILED] results.json carries no scored record -- the run or its scoring pass crashed; see ${ARTIFACT_DIR}/scoring_failure_${TASK_NAME}.log (Duration: ${TASK_DURATION}s)"
    else
      echo "Task ${TASK_NAME} Result: [FAILED] no results.json from a noop-deployer task -- nothing was provisioned, so this is a harness or agent crash, not infrastructure; see ${ARTIFACT_DIR}/scoring_failure_${TASK_NAME}.log (Duration: ${TASK_DURATION}s)"
    fi
    FAILED_TASKS+=("${TASK_NAME} (run produced no scored record)")
  elif [ "${RUN_CLASS}" = "INFRA" ]; then
    # RESOURCE_PREPARATION_FAILED is kept verbatim as the grep token even
    # though the class now also covers an unreachable agent endpoint; the
    # artifact below says which of the two it was.
    echo "⚠️ [RESOURCE_PREPARATION_FAILED] Evaluation task ${TASK_NAME} resource creation, teardown, or agent transport failed! (The evaluation is skipped)"
    ARTIFACT_DIR="${ARTIFACTS:-/tmp/artifacts}"
    mkdir -p "${ARTIFACT_DIR}"
    cp "${EVAL_LOG}" "${ARTIFACT_DIR}/resource_prep_failure_${TASK_NAME}.log" 2>/dev/null || true
    [ -n "${NEW_RUN_DIR}" ] && cp "${EVAL_LOG}" "${NEW_RUN_DIR}/resource_prep_failure.log" 2>/dev/null || true
    echo "Saved resource preparation log to artifact: ${ARTIFACT_DIR}/resource_prep_failure_${TASK_NAME}.log"
    echo "Task ${TASK_NAME} Result: [RESOURCE_PREPARATION_FAILED] Infrastructure setup/teardown or agent transport error (Duration: ${TASK_DURATION}s)"
    # Deliberately NOT appended to FAILED_TASKS: an OpenTofu stockout, a
    # teardown race, or an agent pod that went away mid-task says nothing
    # about the pull request under test, and
    # redding the job for it teaches people to ignore the job. The log line
    # above and the artifact are the record; whoever owns the eval
    # infrastructure greps for RESOURCE_PREPARATION_FAILED, not the PR author.
    INFRA_FAILED_TASKS+=("${TASK_NAME}")
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
    # Reported, not gated. Per-requirement checks are the finer-grained signal,
    # but individual judge calls hang and devops-bench counts a hung check as a
    # failed one, so gating here would turn a flaky judge into a red build.
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

    # 6. The two-speed gate. Exact checks block; judged scores are recorded.
    #
    # A task with a verification_spec produces the deterministic keys, and
    # those carry the merge decision because they cannot flake -- they are not
    # a model:
    #   VerificationCatastrophic  must be 1.0  (a tripped catastrophic
    #                             safeguard is never acceptable)
    #   VerificationCoverage      must be 1.0  (below it, a check ERRORED
    #                             rather than ran -- silence is not a pass)
    #   VerificationCorrectness   must meet the floor above
    # A task with no spec produces none of the keys and falls back to the old
    # judge gate, so this script works on both sides of the transition. Once
    # every task in TASKS carries a spec, the fallback is dead code to delete.
    #
    # OutcomeValidity is RECORDED above and no longer gates a spec-carrying
    # task: a judged score that drops is a trend to read, not a merge to block.
    VERDICT=$(python3 -c "
import json
data = json.load(open('${LATEST_RESULT}'))
# data[0]: each devops-bench invocation in this loop runs exactly one task,
# so its results.json carries one record. A future multi-task invocation
# must iterate instead of silently grading only the first record.
rec = data[0] if isinstance(data, list) else data
scores = rec.get('scores', rec.get('metrics', {}))

def val(key):
    v = scores.get(key)
    if isinstance(v, dict):
        v = v.get('score')
    return None if v is None else float(v)

cat = val('VerificationCatastrophic')
cov = val('VerificationCoverage')
cor = val('VerificationCorrectness')

if cat is None and cov is None and cor is None:
    print('NOSPEC')
else:
    problems = []
    if cat is not None and cat < 1.0:
        problems.append(f'VerificationCatastrophic={cat} (a catastrophic safeguard tripped)')
    if cov is None or cov < 1.0:
        problems.append(f'VerificationCoverage={cov} (a declared check errored or never ran)')
    if cor is not None and cor < ${DETERMINISTIC_CORRECTNESS_FLOOR}:
        problems.append(f'VerificationCorrectness={cor} (floor ${DETERMINISTIC_CORRECTNESS_FLOOR})')
    # Name the checks, not just the aggregate. A bare
    # 'VerificationCorrectness=0.5' says half of something failed and leaves
    # the reader to re-run the job to find out which half. The per-entry
    # detail is already in the record under 'verification_report' --
    # name/role/severity/status/reason per entry, written by devops_bench's
    # evalharness -- so printing it costs a dict lookup and saves a
    # 25-minute round trip.
    detail = []
    # One check per line, so collapse whitespace inside a reason. A pydantic
    # ValidationError reason is multi-line, and an embedded newline would put
    # an unindented continuation into a list the reader is scanning by its
    # leading '  - '.
    def one_line(text):
        return ' '.join((text or '').split())[:400]
    if problems:
        # A spec entry that failed to parse never reaches
        # 'verification_report' at all: devops_bench puts it in the separate
        # top-level 'verification_parse_errors', and rollup() adds one to the
        # objective denominator per error with nothing to the numerator (fail
        # closed). So a single typo'd field drops VerificationCorrectness
        # below the floor while the report holds nothing but passes -- and a
        # verdict that lists no failing check then reads as 'everything
        # passed, the score is just wrong'. List these first: a spec that did
        # not parse is a defect in the case, not in the agent.
        for pe in rec.get('verification_parse_errors') or []:
            detail.append(
                f\"  - {pe.get('name')} [spec] parse-error: {one_line(pe.get('reason'))}\"
            )
        for e in rec.get('verification_report') or []:
            if e.get('status') == 'pass':
                continue
            # severity is None on an objective and only meaningful on a
            # safeguard, so it is appended rather than always rendered.
            where = str(e.get('role'))
            if e.get('severity'):
                where += '/' + str(e.get('severity'))
            detail.append(
                f\"  - {e.get('name')} [{where}] \"
                f\"{e.get('status')}: {one_line(e.get('reason'))}\"
            )
    if not problems:
        print('PASS')
    else:
        print('FAIL: ' + '; '.join(problems) + ''.join('\n' + d for d in detail))
" 2>/dev/null || echo "FAIL: could not parse deterministic scores from ${LATEST_RESULT}")

    if [ "${VERDICT}" = "NOSPEC" ]; then
      if [ "$(task_has_spec "${BENCH_DIR}/${TASK}")" = "1" ]; then
        # The task declares a spec but the run produced none of the
        # deterministic keys: the metric crashed or verification never ran.
        # Falling back to the judge here would be the silent-green path this
        # gate exists to close, so absence of evidence fails the task.
        echo "Task ${TASK_NAME} Result: [FAILED] verification_spec declared but no verification scores in results.json -- the deterministic gate did not run (expected VerificationCorrectness/VerificationCatastrophic/VerificationCoverage) (Duration: ${TASK_DURATION}s)"
        FAILED_TASKS+=("${TASK_NAME}")
      else
        # Transition fallback: genuinely no verification_spec, so the judge
        # still gates.
        IS_PASS=$(python3 -c "print(1 if float('${SCORE}') >= 0.7 else 0)" 2>/dev/null || echo "0")
        if [ "${IS_PASS}" -eq 1 ]; then
          echo "Task ${TASK_NAME} Result: [PASSED] no verification_spec; judge fallback OutcomeValidity: ${SCORE} (>= 0.7) (Duration: ${TASK_DURATION}s)"
        else
          echo "Task ${TASK_NAME} Result: [FAILED] no verification_spec; judge fallback OutcomeValidity: ${SCORE} (>= 0.7) (Duration: ${TASK_DURATION}s)"
          FAILED_TASKS+=("${TASK_NAME}")
        fi
      fi
    elif [ "${VERDICT}" = "PASS" ]; then
      echo "Task ${TASK_NAME} Result: [PASSED] exact checks green; OutcomeValidity recorded: ${SCORE} (Duration: ${TASK_DURATION}s)"
    else
      # The verdict is the aggregate on its first line and one line per
      # non-passing check after it. Keep the `Task ... Result:` line a single
      # line -- it is what the summary greps and what a reader scans for --
      # and print the per-check detail beneath it.
      VERDICT_BODY="${VERDICT#FAIL: }"
      echo "Task ${TASK_NAME} Result: [FAILED] ${VERDICT_BODY%%$'\n'*} | OutcomeValidity recorded: ${SCORE} (Duration: ${TASK_DURATION}s)"
      if [ "${VERDICT_BODY}" != "${VERDICT_BODY%%$'\n'*}" ]; then
        printf '%s\n' "${VERDICT_BODY#*$'\n'}"
      fi
      # Keep the record for a task the gate failed. `cp` above writes
      # results_${TASK_NAME}.json into the working directory, which Prow does
      # not upload, so on a red gate the agent's report -- the text a
      # report_contains check matched or missed -- is discarded at teardown
      # and diagnosing why costs another full run. The BROKEN and INFRA arms
      # above already save their evidence for the same reason; this is the
      # arm that did not.
      ARTIFACT_DIR="${ARTIFACTS:-/tmp/artifacts}"
      mkdir -p "${ARTIFACT_DIR}"
      cp "${LATEST_RESULT}" "${ARTIFACT_DIR}/results_${TASK_NAME}.json" 2>/dev/null || true
      FAILED_TASKS+=("${TASK_NAME}")
    fi
  fi
done

profile_begin "final gate + summary"
TOTAL_DURATION=$((SECONDS - START_TIME))
if [ "${#INFRA_FAILED_TASKS[@]}" -gt 0 ]; then
  echo "⚠️ [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Infrastructure failed for tasks (not counted against the PR): ${INFRA_FAILED_TASKS[*]}"
fi
# One infra failure is weather; EVERY task failing on infrastructure means the
# job evaluated nothing at all, and exiting 0 on that would report an eval
# that never happened as a green one.
if [ "${#INFRA_FAILED_TASKS[@]}" -eq "${#TASKS[@]}" ]; then
  echo "❌ [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] EVAL_INFRASTRUCTURE_DOWN: all ${#TASKS[@]} task(s) failed resource preparation -- no evaluation ran. This is an infrastructure page, not a PR failure, but it must not read as green. (Total Duration: ${TOTAL_DURATION}s)"
  exit 1
fi
if [ "${#FAILED_TASKS[@]}" -gt 0 ]; then
  echo "❌ [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] PR Smoke Test Evaluation Failed for tasks: ${FAILED_TASKS[*]} (Total Duration: ${TOTAL_DURATION}s)"
  exit 1
fi

echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] PR Smoke Test Evaluation Succeeded (Total Duration: ${TOTAL_DURATION}s) ==="
