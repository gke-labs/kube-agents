#!/usr/bin/env bash
#
# Run the b-0011-gitops pilot case (gke-labs/kube-agents#1307) from a laptop
# against a kube-agents install, end to end: per-run branch, task cluster with
# Argo CD, agent turn, wait for the PR to merge and Argo to sync, verify, tear
# down. Everything here is what hack/ci-eval-pr.sh would do for this case once
# it is admitted; until then this is the run wrapper.
#
# What it does, in order:
#   1. bench venv on the pinned upstream devops-bench (FORK_PIN=true tries the
#      PR #244 fork instead; see the comment at that step for why it does not
#      work yet).
#   2. Tells the agent which branch its PR must target. Default
#      (BASE_BRANCH_MODE=env): sets GITOPS_BASE_BRANCH on the PlatformAgent's
#      spec.deployment.env, waits for the rollout, and checks the value reached
#      the pod (it needs the operator's sandbox env allowlist to carry that
#      variable; this change adds it). BASE_BRANCH_MODE=default-branch is the
#      fallback for an operator without it: the stack switches the repository's
#      default branch for the run.
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
#   DEVOPS_BENCH_FORK_SHA (PR #244 head)   FORK_PIN=true to try the fork (see below; not working yet)
#   BENCH_NO_TEARDOWN=true to keep the cluster and branch for inspection
set -euo pipefail

: "${GCP_PROJECT_ID:=fuxiaogao-gkedemos}"
: "${GCP_LOCATION:=us-central1-a}"
: "${AGENT_HOST_CONTEXT:=gke_${GCP_PROJECT_ID}_us-central1_platform-agent-host}"
: "${AGENT_NAMESPACE:=kubeagents-system}"
: "${CLUSTER_NAME:=gitops-pilot-$(date +%Y%m%d-%H%M%S)}"
: "${GITOPS_TOKEN_FILE:=${HOME}/.config/gitops-pilot/github-token}"
: "${JUDGE_MODEL:=gemini-3.1-pro-preview}"
: "${DEVOPS_BENCH_FORK_SHA:=df600a08af4008d593628072a25370cf0d69280e}"

TASK="b-0011"
RUN_BRANCH="run/${CLUSTER_NAME}/${TASK}"   # must match the stack's locals.run_branch
CR="platformagents.kubeagents.x-k8s.io/platform-agent"
K=(kubectl --context "${AGENT_HOST_CONTEXT}" -n "${AGENT_NAMESPACE}")

cd "$(dirname "$0")/.."
[ -r "${GITOPS_TOKEN_FILE}" ] || { echo "token file ${GITOPS_TOKEN_FILE} missing (contents read/write on the GitOps repo)" >&2; exit 1; }
[ "${#CLUSTER_NAME}" -le 40 ] || { echo "CLUSTER_NAME ${CLUSTER_NAME} exceeds GKE's 40 chars" >&2; exit 1; }

echo "==> run ${CLUSTER_NAME}: branch ${RUN_BRANCH}"

# 1. venv -------------------------------------------------------------------
uv sync -q
# Default: the upstream pin from pyproject/uv.lock. It runs this harness but
# rejects `mode: hold`, so the five hold safeguards land in
# verification_parse_errors and only the two converge objectives are scored.
#
# FORK_PIN=true installs the PR #244 fork commit instead, which implements
# hold mode but predates three upstream changes this harness relies on:
# BENCH_TF_ROOT (run 4: "TF stack not found in repo"), entry-point discovery
# of agent harnesses (run 5: "'kubeagents' is not registered"), and
# devops_bench.agents.result.empty_tokens (import error in this package's
# parsing module). The first two are papered over below; the third is not,
# so this mode does not work until the fork rebases onto upstream. Kept as
# the record of what a rebase has to carry (#1307 findings).
if [ "${FORK_PIN:-}" = "true" ]; then
  echo "==> pinning devops-bench to gke-labs fork ${DEVOPS_BENCH_FORK_SHA:0:8} (mode: hold)"
  GIT_CONFIG_GLOBAL=/dev/null uv pip install -q \
    "devops-bench @ git+https://github.com/gke-labs/devops-bench@${DEVOPS_BENCH_FORK_SHA}"
  site_packages="$(uv run --no-sync python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  if [ ! -e "${site_packages}/tf" ] || [ -L "${site_packages}/tf" ]; then
    ln -sfn "$(pwd)/tf" "${site_packages}/tf"
  else
    echo "${site_packages}/tf exists and is not a symlink; refusing to replace it" >&2; exit 1
  fi
  agents_base="${site_packages}/devops_bench/agents/base.py"
  if grep -q 'Registry("agents")' "${agents_base}"; then
    sed -i '' 's/Registry("agents")/Registry("agents", entry_point_group="devops_bench.agents")/' "${agents_base}"
  fi
  uv run --no-sync python -c 'from devops_bench.agents import AGENTS; AGENTS.get("kubeagents")' \
    || { echo "kubeagents harness does not load on the fork pin (see comment above)" >&2; exit 1; }
fi

# 2. agent base branch ------------------------------------------------------
# Two ways to make the agent's PR target the run branch (decision 3 in the
# pilot notes):
#   env             set GITOPS_BASE_BRANCH on the PlatformAgent. Needs an
#                   operator whose sandbox env allowlist carries that variable
#                   (this change adds it); the measured runs used it on an
#                   install at release 0.4.0 plus that one line.
#   default-branch  fallback for an operator without it: the stack makes the
#                   run branch the repository's default for the run and
#                   restores it on destroy; the agent re-asks the remote for
#                   its default before each PR. One run at a time (the stack
#                   refuses to switch when the default already points at a
#                   run/** branch), and BENCH_NO_TEARDOWN=true leaves the
#                   repository's default on the run branch until the destroy
#                   is run by hand. Pilot-only.
: "${BASE_BRANCH_MODE:=env}"

case "${BASE_BRANCH_MODE}" in
  default-branch)
    echo "==> base branch via repository default (stack switches it for the run)"
    export TF_VAR_gitops_switch_default_branch=true
    ;;
  env)
    # Keep whatever else is in spec.deployment.env; only our variable changes.
    set_agent_env() {
      local value="$1" env_json
      env_json="$("${K[@]}" get "${CR}" -o json | jq -c --arg v "${value}" '
        ((.spec.deployment.env // []) | map(select(.name != "GITOPS_BASE_BRANCH")))
        + (if $v == "" then [] else [{name: "GITOPS_BASE_BRANCH", value: $v}] end)')"
      "${K[@]}" patch "${CR}" --type merge -p "{\"spec\":{\"deployment\":{\"env\":${env_json}}}}" >/dev/null
    }
    cleanup() {
      echo "==> clearing GITOPS_BASE_BRANCH on ${CR}"
      set_agent_env "" || true
    }
    trap cleanup EXIT

    echo "==> setting GITOPS_BASE_BRANCH=${RUN_BRANCH} on ${CR}"
    set_agent_env "${RUN_BRANCH}"
    # The agent pod's data volume is ReadWriteOnce, so the replacement pod
    # waits for the old one to release it, then cold-starts the agent (plugin
    # and skill sync, MCP discovery). Five minutes was not always enough
    # (run 3, 2026-09-10); ten covers the observed worst case with margin.
    "${K[@]}" rollout status deploy/platform-agent-gateway --timeout=600s >/dev/null
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

echo "==> devops-bench ./tasks/b-0011-gitops (cluster ${CLUSTER_NAME}, argo context ${GITOPS_ARGO_CONTEXT})"
# --no-sync: a plain `uv run` re-syncs the venv from the lockfile first, which
# silently puts the upstream devops-bench pin back and drops `mode: hold`
# support (run 1 verified only 2 of 7 checks for exactly this reason).
uv run --no-sync devops-bench ./tasks/b-0011-gitops --agent-type kubeagents "$@"
