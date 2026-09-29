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
#   2. Makes the run branch the repository's default branch for the run (the
#      stack switches it and restores it on destroy); the agent's
#      submit-suggestion re-asks the remote for its default before each PR, so
#      that is the branch its PR targets.
#   3. Reads PLATFORM_AGENT_TOKEN and the judge key from the install's secret.
#   4. Runs `devops-bench ./tasks/b-0011-gitops --agent-type kubeagents` with
#      the stack and harness pointed at the same run branch.
#
# Inputs (env). Required, with no defaults, because each names an install
# or a repository of yours:
#   GCP_PROJECT_ID          project the run's task cluster is created in
#   AGENT_HOST_CONTEXT      kubectl context of the cluster running the
#                           platform agent
#   GITOPS_REPO             https URL of the GitOps repository; rendered into
#                           the task prompt in place of {{GITOPS_REPO}} and
#                           handed to the stack and the harness
#   GITOPS_BROKEN_BASE_SHA  commit in GITOPS_REPO that carries the task's
#                           broken base under tasks/b-0011 (the output of the
#                           stack's render-broken-base.sh, committed there)
# Optional:
#   GCP_LOCATION (us-central1-a)  AGENT_NAMESPACE (kubeagents-system)
#   CLUSTER_NAME (gitops-pilot-<timestamp>; also seeds the run branch name)
#   GITOPS_TOKEN_FILE (~/.config/gitops-pilot/github-token)
#   JUDGE_MODEL (gemini-3.1-pro-preview)
#   DEVOPS_BENCH_PIN (empty: the repository's pin) a pip requirement for another
#     devops-bench, e.g. `devops-bench @ git+https://github.com/pradeepvrd/devops-bench@<sha>`
#   AGENT_MODEL (read from the install's LiteLLM config: the model behind
#     `model-default`) the model id recorded on the result row
#   JUDGE_API_KEY (read from the install's secret when unset)
#   BENCH_VERIFY_TIMEOUT_SEC (120) per-entry cap of the post-run verification
#     pass; BENCH_VERIFY_TOTAL_BUDGET_SEC is derived from it and the entry count
#   BASE_BRANCH_MODE: only `default-branch` (env mode is gone; any other value
#     is refused)
#   BENCH_NO_TEARDOWN=true to keep the cluster and branch for inspection
set -euo pipefail

# Per-entry cap for a converging entry in the post-run verification pass
# (devops-bench's own default for BENCH_VERIFY_TIMEOUT_SEC).
readonly VERIFY_TIMEOUT_DEFAULT_SEC=120
# The LiteLLM ConfigMap is `litellm-config` on the chart and `litellm-config-<hash>`
# on the kustomize path, hence a prefix match on the name.
readonly LITELLM_CONFIGMAP_NAME_PREFIX="litellm-config"
# The LiteLLM alias the platform agent calls; the model behind it is what the
# result row's `model` field should carry (the leaderboard keys setups by it).
readonly AGENT_MODEL_ALIAS="model-default"
readonly RENDERED_TASKS_TEMPLATE="b-0011-gitops-hold.XXXXXX"
# What the committed prompt carries where the repository URL goes; the wrapper
# renders GITOPS_REPO over it (devops-bench renders only its own placeholders).
readonly PROMPT_REPO_PLACEHOLDER="{{GITOPS_REPO}}"

: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID to the project that hosts the task cluster of a run}"
: "${AGENT_HOST_CONTEXT:?set AGENT_HOST_CONTEXT to the kubectl context of the cluster running the platform agent}"
: "${GITOPS_REPO:?set GITOPS_REPO to the https URL of the GitOps repository}"
: "${GITOPS_BROKEN_BASE_SHA:?set GITOPS_BROKEN_BASE_SHA to the commit in GITOPS_REPO that carries the broken base of the task}"
: "${GCP_LOCATION:=us-central1-a}"
: "${AGENT_NAMESPACE:=kubeagents-system}"
: "${CLUSTER_NAME:=gitops-pilot-$(date +%Y%m%d-%H%M%S)}"
: "${GITOPS_TOKEN_FILE:=${HOME}/.config/gitops-pilot/github-token}"
: "${JUDGE_MODEL:=gemini-3.1-pro-preview}"

TASK="b-0011"
RUN_BRANCH="run/${CLUSTER_NAME}/${TASK}"   # must match the stack's locals.run_branch
K=(kubectl --context "${AGENT_HOST_CONTEXT}" -n "${AGENT_NAMESPACE}")

cd "$(dirname "$0")/.."
[ -r "${GITOPS_TOKEN_FILE}" ] || { echo "token file ${GITOPS_TOKEN_FILE} missing (contents read/write and administration on the GitOps repository: the run makes its branch the repository's default)" >&2; exit 1; }
[ "${#CLUSTER_NAME}" -le 40 ] || { echo "CLUSTER_NAME ${CLUSTER_NAME} exceeds GKE's 40 chars" >&2; exit 1; }

RENDERED_TASKS=""
on_exit() {
  if [ -n "${RENDERED_TASKS}" ]; then rm -rf "${RENDERED_TASKS}"; fi
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
render_task_copy() {
  [ -n "${RENDERED_TASKS}" ] && return 0
  RENDERED_TASKS="$(mktemp -d "${TMPDIR:-/tmp}/${RENDERED_TASKS_TEMPLATE}")"
  mkdir -p "${RENDERED_TASKS}/${TASK}-gitops"
  cp "${TASK_SOURCE}/task.yaml" "${RENDERED_TASKS}/${TASK}-gitops/task.yaml"
  TASK_SOURCE="${RENDERED_TASKS}/${TASK}-gitops"
}
# 1a. repository ------------------------------------------------------------
# The prompt is the one place the agent learns the repository from, and the
# committed case names none: it carries the placeholder, and the wrapper
# renders GITOPS_REPO into the task copy, so the agent, the stack and the
# harness all see the same repository.
render_task_copy
sed -i.bak "s|${PROMPT_REPO_PLACEHOLDER}|${GITOPS_REPO}|" "${TASK_SOURCE}/task.yaml" && rm -f "${TASK_SOURCE}/task.yaml.bak"
grep -q "${GITOPS_REPO} under" "${TASK_SOURCE}/task.yaml" || { echo "prompt render failed: ${GITOPS_REPO} not in ${TASK_SOURCE}/task.yaml" >&2; exit 1; }
echo "==> repository ${GITOPS_REPO}; prompt rendered"

if [ "${HOLD_SUPPORTED}" = "yes" ]; then
  render_task_copy
  # Only the safeguards are `assert` in the committed case; the objectives are
  # `converge`, so a plain substitution flips exactly the safeguards. Proved
  # below rather than assumed: the run must not proceed printing "hold" while
  # scoring assert because a `mode:` line grew a comment or an objective
  # became assert.
  sed -i.bak 's/^\(  *\)mode: assert$/\1mode: hold/' "${TASK_SOURCE}/task.yaml" && rm -f "${TASK_SOURCE}/task.yaml.bak"
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
# The stack makes the run branch the repository's default branch for the run
# and restores it on destroy; the agent's submit-suggestion re-asks the remote
# for its default before each PR (decision 3 in the pilot notes). One run at a
# time (the stack refuses to switch when the default already points at a
# run/** branch), and BENCH_NO_TEARDOWN=true leaves the repository's default on
# the run branch until the destroy is run by hand. Pilot-only: the per-run
# base is the credential broker's to enforce (#1498; its direct-push half
# landed as #1669, the base-branch half is #1848). Runs 1 to 13 set
# GITOPS_BASE_BRANCH on the PlatformAgent instead, on a 0.4.0 install whose
# operator copied it into the agent container; on the shell-sandbox layout
# every command runs in platform-agent-shell-0, whose environment does not
# take spec.deployment.env, so the variable never reaches the process that
# opens the PR, and that mode is gone.
case "${BASE_BRANCH_MODE:-default-branch}" in
  default-branch) ;;
  *) echo "BASE_BRANCH_MODE=${BASE_BRANCH_MODE} is not offered: the run branch becomes the repository's default for the run (see the comment above)" >&2; exit 1 ;;
esac
echo "==> base branch via repository default (stack switches it for the run)"
export TF_VAR_gitops_switch_default_branch=true

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
export TF_VAR_gitops_repo="${GITOPS_REPO}" TF_VAR_gitops_broken_base_sha="${GITOPS_BROKEN_BASE_SHA}"
export GITOPS_REPO GITOPS_RUN_BRANCH="${RUN_BRANCH}" GITOPS_TOKEN_FILE
# The harness prefers BENCH_GITHUB_TOKEN or GITHUB_TOKEN over the file; hand it
# the token the stack uses, so an ambient GITHUB_TOKEN for another account
# cannot make it poll the private repository as a stranger.
BENCH_GITHUB_TOKEN="$(tr -d '\r\n' < "${GITOPS_TOKEN_FILE}")"; export BENCH_GITHUB_TOKEN
export GITOPS_ARGO_CONTEXT="gke_${GCP_PROJECT_ID}_${GCP_LOCATION}_${CLUSTER_NAME}"
# tofu fetches the kind module over https; a global insteadOf to ssh breaks it.
export GIT_CONFIG_GLOBAL=/dev/null

echo "==> devops-bench ${TASK_SOURCE} (cluster ${CLUSTER_NAME}, argo context ${GITOPS_ARGO_CONTEXT})"
# --no-sync: a plain `uv run` re-syncs the venv from the lockfile first, which
# silently puts the upstream devops-bench pin back and drops `mode: hold`
# support (run 1 verified only 2 of 7 checks for exactly this reason).
uv run --no-sync devops-bench "${TASK_SOURCE}" --agent-type kubeagents "$@"
