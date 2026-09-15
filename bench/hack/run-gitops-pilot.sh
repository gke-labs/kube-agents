#!/usr/bin/env bash
#
# Run the b-0011-gitops pilot case (gke-labs/kube-agents#1307) from a laptop
# against a kube-agents install, end to end: per-run branch, task cluster with
# Argo CD, agent turn, wait for the PR to merge and Argo to sync, verify, tear
# down. Everything here is what hack/ci-eval-pr.sh would do for this case once
# it is admitted; until then this is the run wrapper.
#
# What it does, in order:
#   1. bench venv on the pinned upstream devops-bench, or on DEVOPS_BENCH_PIN
#      when set. If the installed devops-bench accepts `mode: hold`, the case
#      is run from a rendered copy with its five safeguards restored to hold
#      (all seven checks scored); otherwise the committed case runs as is.
#   2. Tells the agent which branch its PR must target. Default
#      (BASE_BRANCH_MODE=default-branch): the stack switches the repository's
#      default branch to the run branch for the run and restores it on destroy.
#      BASE_BRANCH_MODE=env sets GITOPS_BASE_BRANCH on the PlatformAgent's
#      spec.deployment.env instead, waits for the rollout, and checks the value
#      reached the pod; it needs an operator whose sandbox env allowlist carries
#      that variable (this change adds it; no release has it yet).
#   3. Reads PLATFORM_AGENT_TOKEN and the judge key from the install's secret.
#   4. Runs `devops-bench ./tasks/b-0011-gitops --agent-type kubeagents` with
#      the stack and harness pointed at the same run branch.
#   5. On exit (env mode), removes GITOPS_BASE_BRANCH from the PlatformAgent.
#
# Inputs (env, all optional):
#   GCP_PROJECT_ID (fuxiaogao-gkedemos)  GCP_LOCATION (us-central1-a)
#   AGENT_HOST_CONTEXT (gke_<project>_us-central1_platform-agent-host)
#   CLUSTER_NAME (gitops-pilot-<timestamp>; also seeds the run branch name)
#   GITOPS_TOKEN_FILE (~/.config/gitops-pilot/github-token)
#   GITOPS_REPO (stack default)   JUDGE_MODEL (gemini-3.1-pro-preview)
#   DEVOPS_BENCH_PIN (empty: the repository's pin) a pip requirement for another
#     devops-bench, e.g. `devops-bench @ git+https://github.com/pradeepvrd/devops-bench@<sha>`
#   AGENT_MODEL (read from the install's LiteLLM config: the model behind
#     `model-default`) the model id recorded on the result row
#   BENCH_NO_TEARDOWN=true to keep the cluster and branch for inspection
set -euo pipefail

# Per-entry cap for a converging entry in the post-run verification pass
# (devops-bench's own default for BENCH_VERIFY_TIMEOUT_SEC).
readonly VERIFY_TIMEOUT_DEFAULT_SEC=120
# How long to wait for the gateway to roll after the PlatformAgent env patch.
# The agent pod's data volume is ReadWriteOnce, so the replacement pod waits
# for the old one to release it, then cold-starts the agent (plugin and skill
# sync, MCP discovery). Five minutes was not always enough (run 3,
# 2026-09-10) and neither was ten (run 10, 2026-09-15: the startup probe was
# still failing at 10 min); fifteen covers the observed worst case with margin.
readonly GATEWAY_ROLLOUT_TIMEOUT=900s
# The LiteLLM ConfigMap is `litellm-config` on the chart and `litellm-config-<hash>`
# on the kustomize path, hence a prefix match on the name.
readonly LITELLM_CONFIGMAP_NAME_PREFIX="litellm-config"
# The LiteLLM alias the platform agent calls; the model behind it is what the
# result row's `model` field should carry (the leaderboard keys setups by it).
readonly AGENT_MODEL_ALIAS="model-default"
readonly RENDERED_TASKS_TEMPLATE="b-0011-gitops-hold.XXXXXX"

: "${GCP_PROJECT_ID:=fuxiaogao-gkedemos}"
: "${GCP_LOCATION:=us-central1-a}"
: "${AGENT_HOST_CONTEXT:=gke_${GCP_PROJECT_ID}_us-central1_platform-agent-host}"
: "${AGENT_NAMESPACE:=kubeagents-system}"
: "${CLUSTER_NAME:=gitops-pilot-$(date +%Y%m%d-%H%M%S)}"
: "${GITOPS_TOKEN_FILE:=${HOME}/.config/gitops-pilot/github-token}"
: "${JUDGE_MODEL:=gemini-3.1-pro-preview}"

TASK="b-0011"
RUN_BRANCH="run/${CLUSTER_NAME}/${TASK}"   # must match the stack's locals.run_branch
CR="platformagents.kubeagents.x-k8s.io/platform-agent"
K=(kubectl --context "${AGENT_HOST_CONTEXT}" -n "${AGENT_NAMESPACE}")

cd "$(dirname "$0")/.."
[ -r "${GITOPS_TOKEN_FILE}" ] || { echo "token file ${GITOPS_TOKEN_FILE} missing (contents read/write on the GitOps repo)" >&2; exit 1; }
[ "${#CLUSTER_NAME}" -le 40 ] || { echo "CLUSTER_NAME ${CLUSTER_NAME} exceeds GKE's 40 chars" >&2; exit 1; }

# Keep whatever else is in spec.deployment.env; only our variable changes.
set_agent_env() {
  local value="$1" env_json
  env_json="$("${K[@]}" get "${CR}" -o json | jq -c --arg v "${value}" '
    ((.spec.deployment.env // []) | map(select(.name != "GITOPS_BASE_BRANCH")))
    + (if $v == "" then [] else [{name: "GITOPS_BASE_BRANCH", value: $v}] end)')"
  "${K[@]}" patch "${CR}" --type merge -p "{\"spec\":{\"deployment\":{\"env\":${env_json}}}}" >/dev/null
}
RENDERED_TASKS=""
AGENT_ENV_SET=""
on_exit() {
  [ -n "${RENDERED_TASKS}" ] && rm -rf "${RENDERED_TASKS}"
  if [ -n "${AGENT_ENV_SET}" ]; then
    echo "==> clearing GITOPS_BASE_BRANCH on ${CR}"
    set_agent_env "" || true
  fi
}
trap on_exit EXIT

echo "==> run ${CLUSTER_NAME}: branch ${RUN_BRANCH}"

# 1. venv -------------------------------------------------------------------
uv sync -q
# Default: the upstream pin from pyproject/uv.lock. It runs this harness but
# rejects `mode: hold`, so the five hold safeguards land in
# verification_parse_errors and only the two converge objectives are scored.
#
# DEVOPS_BENCH_PIN installs another devops-bench over it, given as a pip
# requirement (`devops-bench @ git+https://github.com/<owner>/devops-bench@<sha>`).
# The one that implements hold and still carries what this harness needs
# (BENCH_TF_ROOT, entry-point discovery of agent harnesses,
# devops_bench.agents.result.empty_tokens) is pradeepvrd/devops-bench's
# `integration` branch; the PR #244 head (gke-labs/devops-bench df600a08)
# predates all three and does not run this harness (#1307 findings, runs 4-5).
if [ -n "${DEVOPS_BENCH_PIN:-}" ]; then
  echo "==> pinning devops-bench to ${DEVOPS_BENCH_PIN}"
  GIT_CONFIG_GLOBAL=/dev/null uv pip install -q "${DEVOPS_BENCH_PIN}"
  uv run --no-sync python -c 'from devops_bench.agents import AGENTS; AGENTS.get("kubeagents")' \
    || { echo "kubeagents harness does not load on ${DEVOPS_BENCH_PIN}" >&2; exit 1; }
fi

# 1b. task source -----------------------------------------------------------
# The committed case carries its five safeguards as `mode: assert` because the
# repository's pin rejects `hold` (see the comment in task.yaml). When the
# installed devops-bench accepts hold, run a rendered copy with the safeguards
# restored to `hold`, so all seven checks are scored and the safeguards are
# sampled through the agent's turn rather than read once at the end. The copy
# keeps the task's directory name, which is what lands on the result row.
TASK_SOURCE="./tasks/${TASK}-gitops"
HOLD_SUPPORTED="$(uv run --no-sync python - <<'PY'
from devops_bench.verification.spec import parse_entries
probe = [{"name": "p", "role": "safeguard", "severity": "recoverable", "mode": "hold",
          "check": {"type": "resource_property", "kind": "deployment", "resource_name": "x",
                    "namespace": "x", "path": "spec.replicas", "op": "eq", "value": 1}}]
entries, errors = parse_entries(probe)
print("yes" if entries and not errors else "no")
PY
)"
if [ "${HOLD_SUPPORTED}" = "yes" ]; then
  RENDERED_TASKS="$(mktemp -d "${TMPDIR:-/tmp}/${RENDERED_TASKS_TEMPLATE}")"
  mkdir -p "${RENDERED_TASKS}/${TASK}-gitops"
  # Only the safeguards are `assert` in the committed case; the objectives are
  # `converge`, so a plain substitution flips exactly the safeguards. Proved
  # below rather than assumed: the run must not proceed printing "hold" while
  # scoring assert because a `mode:` line grew a comment or an objective
  # became assert.
  sed 's/^\(  *\)mode: assert$/\1mode: hold/' "${TASK_SOURCE}/task.yaml" \
    > "${RENDERED_TASKS}/${TASK}-gitops/task.yaml"
  TASK_SOURCE="${RENDERED_TASKS}/${TASK}-gitops"
  safeguard_count="$(grep -c -E '^ *role: safeguard$' "${TASK_SOURCE}/task.yaml")"
  hold_count="$(grep -c -E '^ *mode: hold$' "${TASK_SOURCE}/task.yaml")"
  [ "${safeguard_count}" -gt 0 ] && [ "${hold_count}" = "${safeguard_count}" ] \
    || { echo "hold render mismatch: ${hold_count} 'mode: hold' lines for ${safeguard_count} safeguards in ${TASK_SOURCE}/task.yaml" >&2; exit 1; }
  echo "==> installed devops-bench accepts mode: hold; running ${TASK_SOURCE} with its ${safeguard_count} safeguards as hold"
  # The post-run pass shares BENCH_VERIFY_TOTAL_BUDGET_SEC (default 600)
  # across every entry it counts as converging, and on the integration
  # branch that count includes the hold safeguards, which cost the pass
  # nothing (their verdict comes from the live monitor). With seven entries
  # each converge objective got 600/7 = 85.7s of its 120s cap and was recorded
  # "error: not observed" instead of "fail" (run 9), which leaves the row's
  # outcomeScore null. Each share is `remaining / entries_left`, and
  # `remaining` is read after the deadline was set, so a total of exactly
  # entries x cap still yields a first share a fraction under the cap and the
  # entry is still marked truncated. One extra cap of slack makes every share
  # clear the cap. Until devops-bench excludes hold entries from the count.
  entry_count="$(grep -c -E '^  *- name: ' "${TASK_SOURCE}/task.yaml")"
  : "${BENCH_VERIFY_TIMEOUT_SEC:=${VERIFY_TIMEOUT_DEFAULT_SEC}}"
  export BENCH_VERIFY_TIMEOUT_SEC
  export BENCH_VERIFY_TOTAL_BUDGET_SEC="${BENCH_VERIFY_TOTAL_BUDGET_SEC:-$(( (entry_count + 1) * BENCH_VERIFY_TIMEOUT_SEC ))}"
  echo "==> verification budget: ${BENCH_VERIFY_TIMEOUT_SEC}s per entry, ${BENCH_VERIFY_TOTAL_BUDGET_SEC}s total for ${entry_count} entries"
else
  echo "==> installed devops-bench rejects mode: hold; running the committed case (safeguards as assert)"
fi

# 1c. result-row identity ---------------------------------------------------
# devops-bench stamps AGENT_MODEL on the result row; unset, the row carried
# the harness name in that field (runs 7 and 8). Resolve the alias the agent
# calls to the model LiteLLM routes it to, minus the provider prefix
# (`vertex_ai/gemini-2.5-flash` -> `gemini-2.5-flash`). Done before the
# rollout wait below so a missing ConfigMap fails fast instead of after it.
if [ -z "${AGENT_MODEL:-}" ]; then
  # The ConfigMap the LiteLLM Deployment mounts, not the newest one by name: a
  # kustomize re-apply leaves the previous hash-suffixed ConfigMap behind.
  litellm_cm="$("${K[@]}" get deploy litellm -o jsonpath='{.spec.template.spec.volumes[?(@.configMap)].configMap.name}' 2>/dev/null | tr ' ' '\n' | grep -m1 "${LITELLM_CONFIGMAP_NAME_PREFIX}" || true)"
  [ -n "${litellm_cm}" ] || { echo "the litellm Deployment in ${AGENT_NAMESPACE} on ${AGENT_HOST_CONTEXT} mounts no ${LITELLM_CONFIGMAP_NAME_PREFIX}* ConfigMap; set AGENT_MODEL" >&2; exit 1; }
  AGENT_MODEL="$("${K[@]}" get configmap "${litellm_cm}" -o json | uv run --no-sync python -c '
import json, sys, yaml
alias = sys.argv[1]
for text in json.load(sys.stdin).get("data", {}).values():
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        continue
    for entry in (doc or {}).get("model_list", []) if isinstance(doc, dict) else []:
        if entry.get("model_name") == alias:
            print(str(entry.get("litellm_params", {}).get("model", "")).split("/")[-1]); break
' "${AGENT_MODEL_ALIAS}")"
  [ -n "${AGENT_MODEL}" ] || { echo "could not resolve ${AGENT_MODEL_ALIAS} in ${litellm_cm}; set AGENT_MODEL" >&2; exit 1; }
fi
export AGENT_MODEL
echo "==> result row model: ${AGENT_MODEL} (LiteLLM alias ${AGENT_MODEL_ALIAS})"

# 2. agent base branch ------------------------------------------------------
# Two ways to make the agent's PR target the run branch (decision 3 in the
# pilot notes):
#   env             set GITOPS_BASE_BRANCH on the PlatformAgent. Needs an
#                   operator whose sandbox env allowlist carries that variable
#                   (this change adds it; no release has it yet). Runs 1 to 13
#                   used it on an install at release 0.4.0 plus that one line.
#   default-branch  the stack makes the run branch the repository's default
#                   for the run and restores it on destroy; the agent re-asks
#                   the remote for its default before each PR. Runs 14 onward
#                   used it, on release 0.5.0 with the stock operator. One run
#                   at a time (the stack refuses to switch when the default
#                   already points at a run/** branch), and
#                   BENCH_NO_TEARDOWN=true leaves the repository's default on
#                   the run branch until the destroy is run by hand. Pilot-only.
: "${BASE_BRANCH_MODE:=default-branch}"

case "${BASE_BRANCH_MODE}" in
  default-branch)
    echo "==> base branch via repository default (stack switches it for the run)"
    export TF_VAR_gitops_switch_default_branch=true
    ;;
  env)
    echo "==> setting GITOPS_BASE_BRANCH=${RUN_BRANCH} on ${CR}"
    set_agent_env "${RUN_BRANCH}"
    AGENT_ENV_SET=1
    "${K[@]}" rollout status deploy/platform-agent-gateway --timeout="${GATEWAY_ROLLOUT_TIMEOUT}" >/dev/null
    landed="$("${K[@]}" get deploy platform-agent-gateway -o json \
      | jq -r '.spec.template.spec.containers[] | select(.name=="platform-agent") | .env[]? | select(.name=="GITOPS_BASE_BRANCH") | .value')"
    if [ "${landed}" != "${RUN_BRANCH}" ]; then
      echo "GITOPS_BASE_BRANCH did not reach the agent pod (got '${landed}'). The operator on ${AGENT_HOST_CONTEXT} lacks the #1307 allowlist change." >&2
      exit 1
    fi
    ;;
  *) echo "BASE_BRANCH_MODE must be default-branch or env" >&2; exit 1 ;;
esac

# 3. tokens -----------------------------------------------------------------
PLATFORM_AGENT_TOKEN="$("${K[@]}" get secret platform-agent-secrets -o jsonpath='{.data.API_SERVER_KEY}' | base64 -d)"
JUDGE_API_KEY="${JUDGE_API_KEY:-$("${K[@]}" get secret platform-agent-secrets -o jsonpath='{.data.GEMINI_API_KEY}' | base64 -d)}"
export PLATFORM_AGENT_TOKEN JUDGE_API_KEY JUDGE_MODEL JUDGE_PROVIDER=google

# 4. run --------------------------------------------------------------------
export GCP_PROJECT_ID PROJECT_ID="${GCP_PROJECT_ID}" GCP_LOCATION
export CLUSTER_NAME GKE_CLUSTER_NAME="${CLUSTER_NAME}" TF_VAR_cluster_name="${CLUSTER_NAME}"
export AGENT_CLUSTER_CONTEXT="${AGENT_HOST_CONTEXT}" AGENT_NAMESPACE
export BENCH_TF_ROOT=./tf
export TF_VAR_gitops_run_branch="${RUN_BRANCH}" TF_VAR_gitops_token_file="${GITOPS_TOKEN_FILE}"
# Onboard the per-run cluster with the platform agent before the agent's turn
# (see the stack's agent_host_context variable for why this cannot wait for
# the hourly reconcile).
export TF_VAR_agent_host_context="${AGENT_HOST_CONTEXT}" TF_VAR_agent_namespace="${AGENT_NAMESPACE}"
[ -n "${GITOPS_REPO:-}" ] && export TF_VAR_gitops_repo="${GITOPS_REPO}"
export GITOPS_RUN_BRANCH="${RUN_BRANCH}" GITOPS_TOKEN_FILE
export GITOPS_ARGO_CONTEXT="gke_${GCP_PROJECT_ID}_${GCP_LOCATION}_${CLUSTER_NAME}"
# tofu fetches the kind module over https; a global insteadOf to ssh breaks it.
export GIT_CONFIG_GLOBAL=/dev/null

echo "==> devops-bench ${TASK_SOURCE} (cluster ${CLUSTER_NAME}, argo context ${GITOPS_ARGO_CONTEXT})"
# --no-sync: a plain `uv run` re-syncs the venv from the lockfile first, which
# silently puts the upstream devops-bench pin back and drops `mode: hold`
# support (run 1 verified only 2 of 7 checks for exactly this reason).
uv run --no-sync devops-bench "${TASK_SOURCE}" --agent-type kubeagents "$@"
