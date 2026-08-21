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

# 1. Target Cluster Context
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/ci-env.sh"
trap dump_prow_artifacts_on_failure EXIT

START_TIME=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Running PR Smoke Test Evaluation for PR #${PR_ID} in Namespace: ${TARGET_NAMESPACE} ==="

# 2. Cluster Auth
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Authenticating to GKE Cluster ==="
gke_dns_endpoint_flag "$HOST_CLUSTER_NAME" "$REGION" "$PROJECT_ID"
# Unquoted on purpose: empty must contribute no argument. See gke_dns_endpoint.sh.
# shellcheck disable=SC2086
gcloud container clusters get-credentials "$HOST_CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet \
  $GKE_DNS_ENDPOINT_FLAG
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
echo "Task cluster for this run: ${EVAL_CLUSTER_NAME}"
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
# agent-kanban-smoke is deployer: noop, so it adds seconds, not a cluster.
TASKS=(
  "./tasks/gpu-stress-test-diagnosis/task.yaml"
  "./tasks/agent-kanban-smoke/task.yaml"
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

  # Snapshot existing result directories before running to prevent stale score leakage
  PRE_RUNS="$(ls -d "${BENCH_DIR}/results/run_"* 2>/dev/null | sort || true)"
  EVAL_LOG="/tmp/eval_${TASK_NAME}.log"

  (cd "${BENCH_DIR}" && uv run devops-bench "${TASK}" --agent-type kubeagents 2>&1 | tee "${EVAL_LOG}") || true

  # Use set difference (comm -13) to isolate the brand new directory created strictly by THIS task run.
  # If devops-bench crashed before or during execution without completing results.json, NEW_RUN_DIR will be empty.
  POST_RUNS="$(ls -d "${BENCH_DIR}/results/run_"* 2>/dev/null | sort || true)"
  NEW_RUN_DIR="$(comm -13 <(echo "${PRE_RUNS}") <(echo "${POST_RUNS}") | head -n 1)"
  LATEST_RESULT=""
  [ -n "${NEW_RUN_DIR}" ] && LATEST_RESULT="${NEW_RUN_DIR}/results.json"

  # Classify the run. Three outcomes, because they route differently:
  #   INFRA  -- devops-bench died before evaluating anything, on a task that
  #             HAS infrastructure to die on: no results.json, or the
  #             documented empty-list record. Non-blocking for the PR, loud
  #             for the infra owner. A noop-deployer task prepares nothing,
  #             so its pre-record death is never weather -- it is a harness
  #             or agent crash and classifies BROKEN instead.
  #   BROKEN -- the run died somewhere no infrastructure excuse exists: a
  #             record with no scores (the scoring pass crashed), or any
  #             pre-record death on a noop task. BLOCKS -- treating these as
  #             infra would let a crash turn the whole gate green.
  #   OK     -- a record with scores; the gate below decides.
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
            print('OK' if rec and isinstance(rec, dict) and rec.get('scores') else 'BROKEN')
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
    echo "⚠️ [RESOURCE_PREPARATION_FAILED] Evaluation task ${TASK_NAME} resource creation or teardown failed! (The evaluation is skipped)"
    ARTIFACT_DIR="${ARTIFACTS:-/tmp/artifacts}"
    mkdir -p "${ARTIFACT_DIR}"
    cp "${EVAL_LOG}" "${ARTIFACT_DIR}/resource_prep_failure_${TASK_NAME}.log" 2>/dev/null || true
    [ -n "${NEW_RUN_DIR}" ] && cp "${EVAL_LOG}" "${NEW_RUN_DIR}/resource_prep_failure.log" 2>/dev/null || true
    echo "Saved resource preparation log to artifact: ${ARTIFACT_DIR}/resource_prep_failure_${TASK_NAME}.log"
    echo "Task ${TASK_NAME} Result: [RESOURCE_PREPARATION_FAILED] Infrastructure setup/teardown error (Duration: ${TASK_DURATION}s)"
    # Deliberately NOT appended to FAILED_TASKS: an OpenTofu stockout or a
    # teardown race says nothing about the pull request under test, and
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
    print('PASS' if not problems else 'FAIL: ' + '; '.join(problems))
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
      echo "Task ${TASK_NAME} Result: [FAILED] ${VERDICT#FAIL: } | OutcomeValidity recorded: ${SCORE} (Duration: ${TASK_DURATION}s)"
      FAILED_TASKS+=("${TASK_NAME}")
    fi
  fi
done

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
