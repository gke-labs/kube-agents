#!/usr/bin/env bash
#
# Run a GitOps fix-cycle pilot case (gke-labs/kube-agents#1307; TASK=b-0011 or
# b-0022b, case ./tasks/<TASK>-gitops, or CASE=<case> for a variant such as
# b-0022b-gitops-pinned-base) from a laptop
# against a kube-agents install, end to end: per-run branch, task cluster with
# Argo CD, agent turn, wait for the PR to merge and Argo to sync, verify, tear
# down. Everything here is what hack/ci-eval-pr.sh would do for this case once
# it is admitted; until then this is the run wrapper.
#
# What it does, in order:
#   1. bench venv on the pinned upstream devops-bench, or on DEVOPS_BENCH_PIN
#      when set. If the installed devops-bench accepts `mode: hold`, the case
#      is run from a rendered copy with its safeguards restored to hold (every
#      check scored); otherwise the committed case runs as is.
#   2. Makes the run branch the repository's default branch for the run (the
#      stack switches it and restores it on destroy); the agent's
#      submit-suggestion re-asks the remote for its default before each PR, so
#      that is the branch its PR targets. A case that sets
#      gitops_pin_agent_base_branch is left to its stack instead, which pins
#      the base on the PlatformAgent and leaves the repository default alone.
#      Before any of that (right after the case is resolved), a run/** branch
#      other than this run's in any of the PlatformAgent's
#      spec.integration.repositories[].baseBranch is refused as a pin leaked by
#      an earlier run; and, for a case that pins its base on a CRD that
#      declares that field, so is a base the install already sets on the
#      GitOps repository's entry, and a PlatformAgent with no such entry when
#      AGENT_STATE_RESET is not set to write one (the stack would refuse both
#      only after the task cluster is built).
#   3. Reads PLATFORM_AGENT_TOKEN and the judge key from the install's secret.
#   4. Runs `devops-bench ./tasks/<CASE> --agent-type kubeagents` with
#      the stack and harness pointed at the same run branch.
#   5. Logs the pull requests opened since the run started with their bases
#      (how many went onto the default branch), writes the version stamp with
#      them, and checks for leaks.
#
# Inputs (env). Required, with no defaults, because each names an install
# or a repository of yours:
#   GCP_PROJECT_ID          project the run's task cluster is created in
#   AGENT_HOST_CONTEXT      kubectl context of the cluster running the
#                           platform agent
#   GITOPS_REPO             https URL of the GitOps repository, on github.com;
#                           rendered into the task prompt in place of
#                           {{GITOPS_REPO}} and handed to the stack and the
#                           harness
# Optional:
#   GITOPS_BROKEN_BASE_SHA  commit in GITOPS_REPO that already carries the
#     task's broken base under tasks/<TASK> (render-broken-base.sh output).
#     Unset: the repository's default-branch head is the base and the broken
#     render is committed on it, which is how a per-run repository starts
#   GITOPS_HISTORY_PARENT_SHA  parent of b-0011's staged history (unset: the
#     base above)
#   GCP_LOCATION (us-central1-a)  AGENT_NAMESPACE (kubeagents-system)
#   TASK (b-0011, or the case's gitops_task when CASE is set) the stack's
#     gitops_task; picks the case ./tasks/<TASK>-gitops when CASE is unset
#   CASE (<TASK>-gitops) the case directory under ./tasks, for a variant of a
#     task's case (CASE=b-0022b-gitops-pinned-base); a TASK that disagrees
#     with the case's gitops_task is refused
#   CLUSTER_NAME (gitops-pilot-<timestamp>; also seeds the run branch name)
#   GITOPS_TOKEN_FILE (~/.config/gitops-pilot/github-token)
#   JUDGE_MODEL (gemini-3.1-pro-preview)
#   GITOPS_REPO_ROOT_SHA (read from GITOPS_REPO when unset) the default-branch
#     head of the repository, used as the base and b-0011's history parent
#     only while GITOPS_BROKEN_BASE_SHA is unset (a per-run repository)
#   AGENT_STATE_RESET=true re-create the PlatformAgent on fresh volumes (with
#     GITOPS_REPO as its managed repository, the event watcher off unless
#     AGENT_EVENT_WATCHER=true and, on a CRD that declares it, the drift
#     detector off unless AGENT_DRIFT_DETECTOR=true) before the run, then
#     refuse to run unless its stores hold nothing but the first-boot
#     discovery card (#1773)
#   DEVOPS_BENCH_PIN (empty: the repository's pin) a pip requirement for another
#     devops-bench, e.g. `devops-bench @ git+https://github.com/pradeepvrd/devops-bench@<sha>`
#   AGENT_MODEL (read from the install's LiteLLM config: the model behind
#     `model-default`) the model id recorded on the result row
#   JUDGE_API_KEY (read from the install's secret when unset)
#   BENCH_VERIFY_TIMEOUT_SEC (120) per-entry cap of the post-run verification
#     pass; BENCH_VERIFY_TOTAL_BUDGET_SEC is derived from it and the entry count
#   BASE_BRANCH_MODE: accepted only as `default-branch`, for old invocations;
#     the case's infrastructure.variables decide how the agent gets its base
#     (step 2)
#   INTEGRITY_SWEEP_SCRIPT (empty: skipped) path to devops-bench's
#     integrity-sweep `sweep.py` (kubernetes-sigs/devops-bench#195); run over
#     the finished record alone, writing integrity-sweep.json and .md beside
#     it for adjudication (#1773)
#   BENCH_NO_TEARDOWN=true to keep the cluster and branch for inspection; a
#     case that pins the base also keeps the baseBranch of the PlatformAgent's
#     GitOps repository entry on the run branch until the destroy is run by
#     hand, which pins every proposal onto the GitOps repository from that
#     install to it
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
readonly RENDERED_TASKS_TEMPLATE="gitops-hold.XXXXXX"
# What the committed prompt carries where the repository URL goes; the wrapper
# renders GITOPS_REPO over it (devops-bench renders only its own placeholders).
readonly PROMPT_REPO_PLACEHOLDER="{{GITOPS_REPO}}"
readonly STAGED_HISTORY_TASK="b-0011"
# The task run when neither TASK nor CASE is set.
readonly DEFAULT_TASK="b-0011"
# Agent state reset (#1773): everything the agent remembers lives on these
# claims; the first two are owned by the PlatformAgent and go with it, the
# shell StatefulSet's two are not and are removed by name.
readonly SHELL_STATEFULSET="platform-agent-shell"
readonly SHELL_PVCS="data-platform-agent-shell-0 sshd-platform-agent-shell-0"
readonly OWNED_PVCS="platform-agent-data system-metadata"
readonly CR_REMOVE_TIMEOUT=600s
readonly CR_READY_TIMEOUT=1500s
readonly PVC_GONE_TIMEOUT_SEC=300
readonly PVC_POLL_INTERVAL_SEC=5
# The branch a repository is left on after a run (the stack restores it).
readonly REPO_DEFAULT_BRANCH="main"
# The card a fresh install files itself on first boot (host inventory); the
# freshness check reports it apart from foreign work instead of refusing it.
readonly ONBOARDING_CARD_PREFIX="First-time environment discovery"
readonly STAMP_FILE="campaign.json"
readonly SWEEP_JSON="integrity-sweep.json"
readonly SWEEP_MD="integrity-sweep.md"
readonly RESULTS_DIR="./results"
# Rollout wait after the reset re-creates the agent: the data volume is
# ReadWriteOnce, so the new pod waits for the old one to release it, then
# cold-starts (plugin and skill sync, MCP discovery). Twenty-five minutes
# matches the gateway rollout gate over the 905s startupProbe budget (#2087;
# pilot run 10 on 2026-09-15 saw the startup probe still failing at ten).
readonly GATEWAY_ROLLOUT_TIMEOUT=1500s
# The repository and PlatformAgent reader the stack's scripts share, relative
# to the bench directory (the cd below), and what its `entry` exits with when
# the PlatformAgent has no gitops entry for the repository.
readonly GITOPS_REPO_HELPER="./tf/prebuilt/gitops-fix-cycle/scripts/gitops_repo.py"
readonly NO_GITOPS_ENTRY=3
readonly GITHUB_URL_PREFIX="https://github.com/"

: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID to the project that hosts the task cluster of a run}"
: "${AGENT_HOST_CONTEXT:?set AGENT_HOST_CONTEXT to the kubectl context of the cluster running the platform agent}"
: "${GITOPS_REPO:?set GITOPS_REPO to the https URL of the GitOps repository}"
: "${GCP_LOCATION:=us-central1-a}"
: "${AGENT_NAMESPACE:=kubeagents-system}"
: "${CLUSTER_NAME:=gitops-pilot-$(date +%Y%m%d-%H%M%S)}"
: "${GITOPS_TOKEN_FILE:=${HOME}/.config/gitops-pilot/github-token}"
: "${JUDGE_MODEL:=gemini-3.1-pro-preview}"

K=(kubectl --context "${AGENT_HOST_CONTEXT}" -n "${AGENT_NAMESPACE}")
CR="platformagents.kubeagents.x-k8s.io/platform-agent"

cd "$(dirname "$0")/.."
[ -r "${GITOPS_TOKEN_FILE}" ] || { echo "token file ${GITOPS_TOKEN_FILE} missing (contents read/write on the GitOps repository, and administration for a case whose run makes its branch the repository's default)" >&2; exit 1; }
[ "${#CLUSTER_NAME}" -le 40 ] || { echo "CLUSTER_NAME ${CLUSTER_NAME} exceeds GKE's 40 chars" >&2; exit 1; }
# owner/name of GITOPS_REPO, read as the operator and the stack read it, and
# GITOPS_REPO as its https URL from here on: the prompt, the stack, the reset
# and the harness (whose repo_slug reads no other spelling) all get that one.
slug="$(python3 "${GITOPS_REPO_HELPER}" slug "${GITOPS_REPO}")" || exit 1
GITOPS_REPO="${GITHUB_URL_PREFIX}${slug}"

RENDERED_TASKS=""
on_exit() {
  if [ -n "${RENDERED_TASKS}" ]; then rm -rf "${RENDERED_TASKS}"; fi
}
trap on_exit EXIT
# Pull requests opened from here on are this run's (see 5).
RUN_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# 1. venv -------------------------------------------------------------------
uv sync -q
# Default: the upstream pin from pyproject/uv.lock. It runs this harness but
# rejects `mode: hold`, so the hold safeguards land in
# verification_parse_errors and only the converge objectives are scored.
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
# The committed case carries its safeguards as `mode: assert` because the
# repository's pin rejects `hold` (see the comment in task.yaml). When the
# installed devops-bench accepts hold, run a rendered copy with the safeguards
# restored to `hold`, so every check is scored and the safeguards are
# sampled through the agent's turn rather than read once at the end. The copy
# keeps the task's directory name, which is what lands on the result row.
#
# The case's infrastructure.variables name the stack's task and decide how the
# agent gets its base (2), so TASK defaults to the case's gitops_task, and a
# TASK that disagrees with it is refused: the run branch, the staged history
# and the Argo Application below all key on TASK.
if [ -z "${CASE:-}" ]; then
  : "${TASK:=${DEFAULT_TASK}}"
  CASE="${TASK}-gitops"
fi
TASK_SOURCE="./tasks/${CASE}"
[ -f "${TASK_SOURCE}/task.yaml" ] || { echo "no case at ${TASK_SOURCE} (TASK=${TASK:-unset}, CASE=${CASE})" >&2; exit 1; }
# case_var <name>: the case's infrastructure.variables.<name> (a boolean as
# true or false), empty when the case does not set it.
case_var() {
  uv run --no-sync python - "${TASK_SOURCE}/task.yaml" "$1" <<'CASEVAR'
import sys, yaml
variables = (yaml.safe_load(open(sys.argv[1])).get("infrastructure") or {}).get("variables") or {}
value = variables.get(sys.argv[2], "")
print(str(value).lower() if isinstance(value, bool) else value)
CASEVAR
}
case_task="$(case_var gitops_task)"
[ -n "${case_task}" ] || { echo "${TASK_SOURCE}/task.yaml sets no infrastructure.variables.gitops_task" >&2; exit 1; }
: "${TASK:=${case_task}}"
[ "${TASK}" = "${case_task}" ] || { echo "TASK=${TASK} disagrees with ${CASE}'s gitops_task ${case_task}; unset TASK or pick the matching case" >&2; exit 1; }
RUN_BRANCH="run/${CLUSTER_NAME}/${TASK}"   # must match the stack's locals.run_branch
case_pin="$(case_var gitops_pin_agent_base_branch)"
echo "==> run ${CLUSTER_NAME}: case ${CASE}, branch ${RUN_BRANCH}"
# agent_bases: one line per spec.integration.repositories[] entry of the
# PlatformAgent that sets baseBranch (an operator without the field has none):
# its index, repository and base, tab-separated. Fails when the PlatformAgent
# cannot be read.
agent_bases() {
  "${K[@]}" get "${CR}" -o json | python3 -c '
import json, sys
for i, r in enumerate((json.load(sys.stdin)["spec"].get("integration") or {}).get("repositories") or []):
    if r.get("baseBranch"):
        print("%d\t%s\t%s" % (i, r.get("repository", ""), r["baseBranch"]))'
}
# A run/** base on the PlatformAgent that is not this run's was left by an
# earlier run (agent-base-branch.sh's unpin only warns when its removal fails),
# unless that run is still in flight. Refused here, before the agent state reset
# copies the spec forward and before the stack applies: it would point this
# run's broker at a branch that is gone, and the row would read no_pr. A base
# outside run/** is the install's own and is left to the stack.
if ! agent_base_lines="$(agent_bases)"; then
  echo "cannot read the PlatformAgent's spec.integration.repositories[].baseBranch on ${AGENT_HOST_CONTEXT}, so whether an earlier run left a pin there is unknown" >&2
  exit 1
fi
while IFS=$'\t' read -r base_index base_repo agent_base; do
  case "${agent_base}" in
    run/*)
      if [ "${agent_base}" != "${RUN_BRANCH}" ]; then
        echo "the PlatformAgent's spec.integration.repositories[${base_index}].baseBranch (${base_repo}) is '${agent_base}', not this run's ${RUN_BRANCH}: a leaked pin from an earlier run (its destroy could not remove it), unless a run on that branch is still in flight. When none is, remove it and rerun:" >&2
        echo "  kubectl --context ${AGENT_HOST_CONTEXT} -n ${AGENT_NAMESPACE} patch ${CR} --type=json -p '[{\"op\":\"test\",\"path\":\"/spec/integration/repositories/${base_index}/baseBranch\",\"value\":\"${agent_base}\"},{\"op\":\"remove\",\"path\":\"/spec/integration/repositories/${base_index}/baseBranch\"}]'" >&2
        exit 1
      fi ;;
  esac
done <<<"${agent_base_lines}"
# crd_declares <path>...: whether a served version of the PlatformAgent CRD
# declares every dotted <path> (spec.integration.repositories.baseBranch): 0
# yes, 1 no, anything else when the CRD could not be read.
crd_declares() {
  kubectl --context "${AGENT_HOST_CONTEXT}" get crd platformagents.kubeagents.x-k8s.io -o json \
    | python3 "${GITOPS_REPO_HELPER}" crd-declares "$@"
}
# A case that pins its base, on a CRD that declares
# spec.integration.repositories[].baseBranch, needs the PlatformAgent's gitops
# entry for this run's repository with no base of its own: the stack refuses
# to overwrite an install's base, and has nowhere to pin without the entry.
# Both are refused here, before the agent state reset (which keeps that base)
# and the task cluster, rather than by the stack after the cluster is built.
# A missing entry passes with AGENT_STATE_RESET=true, whose reset writes it.
# On a CRD without the field there is nothing to check: the stack logs that
# the install pins no base.
if [ "${case_pin}" = "true" ]; then
  crd_rc=0
  crd_declares spec.integration.repositories.baseBranch || crd_rc=$?
  case "${crd_rc}" in
    0)
      entry_rc=0
      gitops_base="$("${K[@]}" get "${CR}" -o json | python3 "${GITOPS_REPO_HELPER}" entry base "${GITOPS_REPO}")" || entry_rc=$?
      case "${entry_rc}" in
        0)
          if [ -n "${gitops_base}" ] && [ "${gitops_base}" != "${RUN_BRANCH}" ]; then
            echo "the PlatformAgent's spec.integration.repositories[] entry with role gitops for ${slug} has baseBranch '${gitops_base}', the install's own base: the stack refuses to overwrite it, so ${CASE}, which pins the base to ${RUN_BRANCH}, cannot run on this install" >&2
            exit 1
          fi ;;
        "${NO_GITOPS_ENTRY}")
          if [ "${AGENT_STATE_RESET:-false}" != "true" ]; then
            echo "the PlatformAgent has no spec.integration.repositories[] entry with role gitops for ${slug}, so ${CASE} has nowhere to pin its base (the deprecated github alias carries none): rerun with AGENT_STATE_RESET=true, whose reset writes that entry" >&2
            exit 1
          fi ;;
        *)
          echo "cannot read the PlatformAgent on ${AGENT_HOST_CONTEXT}, so whether ${CASE} can pin its base is unknown" >&2
          exit 1 ;;
      esac ;;
    1) ;;
    *)
      echo "cannot read the PlatformAgent CRD on ${AGENT_HOST_CONTEXT}, so whether ${CASE} can pin its base is unknown; nothing was changed" >&2
      exit 1 ;;
  esac
fi
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
  mkdir -p "${RENDERED_TASKS}/${CASE}"
  cp "${TASK_SOURCE}/task.yaml" "${RENDERED_TASKS}/${CASE}/task.yaml"
  TASK_SOURCE="${RENDERED_TASKS}/${CASE}"
}
# 1a. repository ------------------------------------------------------------
# 1a. repository ------------------------------------------------------------
# The prompt is the one place the agent learns the repository from, and the
# committed cases name none: they carry the placeholder, and the wrapper
# renders GITOPS_REPO into the task copy, so the agent, the stack and the
# harness all see the same repository. The run branch is built on
# GITOPS_BROKEN_BASE_SHA when set (a repository that already carries the task's
# broken base); otherwise on the repository's default-branch head, the root of
# a per-run repository (#1773), and run-branch.sh commits the broken render on
# it (b-0011's staged history hangs off that same commit unless
# GITOPS_HISTORY_PARENT_SHA says otherwise).
if [ -z "${GITOPS_BROKEN_BASE_SHA:-}" ]; then
  : "${GITOPS_REPO_ROOT_SHA:=$(GH_TOKEN="$(tr -d '\r\n' < "${GITOPS_TOKEN_FILE}")" gh api "repos/${slug}/commits/${REPO_DEFAULT_BRANCH}" --jq .sha)}"
  [ -n "${GITOPS_REPO_ROOT_SHA}" ] || { echo "could not read ${REPO_DEFAULT_BRANCH}'s head in ${GITOPS_REPO}" >&2; exit 1; }
  GITOPS_BROKEN_BASE_SHA="${GITOPS_REPO_ROOT_SHA}"
  if [ "${TASK}" = "${STAGED_HISTORY_TASK}" ]; then : "${GITOPS_HISTORY_PARENT_SHA:=${GITOPS_REPO_ROOT_SHA}}"; fi
fi
# A task with staged history has one seeding, the staged one (its seed asserts
# the state only that history produces, and its task_version names it), so a
# broken base given without the history's parent is refused here rather than
# at SEED FAIL forty minutes in.
if [ "${TASK}" = "${STAGED_HISTORY_TASK}" ] && [ -z "${GITOPS_HISTORY_PARENT_SHA:-}" ]; then
  echo "${TASK} runs on its staged history: set GITOPS_HISTORY_PARENT_SHA (the commit the healthy commit is built on) alongside GITOPS_BROKEN_BASE_SHA" >&2
  exit 1
fi
export TF_VAR_gitops_broken_base_sha="${GITOPS_BROKEN_BASE_SHA}"
if [ -n "${GITOPS_HISTORY_PARENT_SHA:-}" ]; then export TF_VAR_gitops_history_parent_sha="${GITOPS_HISTORY_PARENT_SHA}"; fi
render_task_copy
sed -i.bak "s|${PROMPT_REPO_PLACEHOLDER}|${GITOPS_REPO}|" "${TASK_SOURCE}/task.yaml" && rm -f "${TASK_SOURCE}/task.yaml.bak"
grep -q "${GITOPS_REPO} under" "${TASK_SOURCE}/task.yaml" || { echo "prompt render failed: ${GITOPS_REPO} not in ${TASK_SOURCE}/task.yaml" >&2; exit 1; }
echo "==> repository ${GITOPS_REPO} (broken base ${GITOPS_BROKEN_BASE_SHA}); prompt rendered"

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

# 1d. agent state reset (#1773) --------------------------------------------
# Remove the PlatformAgent (the operator garbage-collects everything it owns,
# the data and session-metadata claims included), remove the shell
# StatefulSet's claims, re-apply the same spec with the run's repository as
# its managed repository, and wait for Ready. For a case that pins its base
# (and for a spec already in that form), the repository goes into the lists
# form (spec.integration.forges and a repositories[] entry with role gitops)
# instead of the deprecated github alias, which carries no base, so
# agent-base-branch.sh has an entry to pin; on a CRD from before the lists form
# the alias stays, and that install pins no base. An existing gitops entry for
# the run's repository keeps its baseBranch. Then prove the stores are empty
# before the card is created; a non-empty store is a refusal, not a warning.
agent_stores_report() {
  "${K[@]}" exec -i deploy/platform-agent-gateway -c platform-agent -- env ONBOARDING_CARD_PREFIX="${ONBOARDING_CARD_PREFIX}" python3 - <<'STORES'
import json, os, sqlite3
def rows(db, table):
    if not os.path.exists(db): return 0
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try: return c.execute(f"select count(*) from {table}").fetchone()[0]
    except sqlite3.Error: return 0
def entries(d): return sorted(os.listdir(d)) if os.path.isdir(d) else []
# A fresh install files itself one card, the first-time environment discovery
# of the host cluster; it and its worker session are part of what "fresh"
# means and are reported separately rather than counted as foreign work.
onboarding_prefix = os.environ.get("ONBOARDING_CARD_PREFIX", "")
cards, onboarding, sessions = [], [], set()
if os.path.exists("/opt/data/kanban.db"):
    kb = sqlite3.connect("file:/opt/data/kanban.db?mode=ro", uri=True)
    for tid, title in kb.execute("select id, title from tasks"):
        (onboarding if onboarding_prefix and str(title).startswith(onboarding_prefix) else cards).append(tid)
    for tid, md in kb.execute("select task_id, metadata from task_runs"):
        if tid in onboarding and md:
            try: sessions.add(json.loads(md).get("worker_session_id"))
            except ValueError: pass
platform_outside = 0
if os.path.exists("/opt/data/profiles/platform/state.db"):
    pf = sqlite3.connect("file:/opt/data/profiles/platform/state.db?mode=ro", uri=True)
    platform_outside = sum(1 for (sid,) in pf.execute("select session_id from messages") if sid not in sessions)
print(json.dumps({
    "kanban_cards": cards, "onboarding_cards": onboarding,
    "front_messages": rows("/opt/data/state.db", "messages"),
    "platform_messages": rows("/opt/data/profiles/platform/state.db", "messages"),
    "platform_messages_outside_onboarding": platform_outside,
    "scratch": entries("/opt/data/scratch"), "gitops": entries("/opt/data/gitops"),
    "workspaces": entries("/opt/data/kanban/workspaces"),
    "profiles": entries("/opt/data/profiles")}))
STORES
}
assert_fresh_agent() {
  AGENT_STORES="$(ONBOARDING_CARD_PREFIX="${ONBOARDING_CARD_PREFIX}" agent_stores_report)"
  export AGENT_STORES
  echo "==> agent stores: ${AGENT_STORES}"
  python3 -c '
import json, os, sys
r = json.loads(os.environ["AGENT_STORES"])
bad = [k for k in ("kanban_cards", "front_messages", "platform_messages_outside_onboarding", "scratch", "gitops", "workspaces") if r[k]]
sys.exit(1 if bad else 0)' || { echo "the agent is not fresh (see the stores above); rerun with AGENT_STATE_RESET=true" >&2; exit 1; }
}
reset_agent_state() {
  local backup reapply pvc waited lists="${case_pin:-false}" drift_field=true crd_rc
  # The CRD is read before anything is deleted, because kubectl apply's field
  # validation is strict by default: a field the CRD does not declare fails
  # the re-apply after the PlatformAgent is gone. On a CRD without the lists
  # form (#2070), a case that pins its base keeps the alias, and
  # agent-base-branch.sh then logs that the install pins no base (the case's
  # red). On one without spec.harness.driftDetector (an operator from before
  # it, which runs no drift detector) nothing is written there.
  # spec.harness.eventWatcher is declared by every release.
  if [ "${lists}" = "true" ]; then
    crd_rc=0
    crd_declares spec.integration.forges spec.integration.repositories || crd_rc=$?
    case "${crd_rc}" in
      0) ;;
      1)
        echo "==> the installed PlatformAgent CRD has no spec.integration.forges and repositories, so the reset keeps the github alias, which carries no base: this install pins none"
        lists=false ;;
      *)
        echo "cannot read the PlatformAgent CRD on ${AGENT_HOST_CONTEXT}, so whether the reset can write the lists form is unknown; nothing was reset" >&2
        exit 1 ;;
    esac
  fi
  crd_rc=0
  crd_declares spec.harness.driftDetector || crd_rc=$?
  case "${crd_rc}" in
    0) ;;
    1)
      echo "==> the installed PlatformAgent CRD has no spec.harness.driftDetector, so the reset writes none (the install runs no drift detector)"
      drift_field=false ;;
    *)
      echo "cannot read the PlatformAgent CRD on ${AGENT_HOST_CONTEXT}, so whether the reset can write spec.harness.driftDetector is unknown; nothing was reset" >&2
      exit 1 ;;
  esac
  backup="${TMPDIR:-/tmp}/platformagent-$(date +%Y%m%d-%H%M%S).json"
  reapply="${backup%.json}.reapply.json"
  "${K[@]}" get "${CR}" -o json > "${backup}"
  echo "==> resetting agent state: PlatformAgent saved to ${backup}"
  # The PlatformAgent to re-apply is built from the backup before anything is
  # deleted, so a failure building it leaves the PlatformAgent in place.
  # The event watcher turns Warning events from every watched cluster into
  # autonomous triage cards; on a benchmark run the prompt must be the only
  # stimulus (a reset alone made it file four cards about the host), so the
  # re-applied agent has it off. AGENT_EVENT_WATCHER=true keeps it on. The
  # drift detector is the same kind of stimulus: it turns the reset's own
  # deletions on the host into out-of-band-change triage cards (four of them
  # before the freshness check on a 2026-10-05 run), so it is off too, keeping
  # its other settings; AGENT_DRIFT_DETECTOR=true keeps it on.
  python3 -c '
import json, sys
d = json.load(open(sys.argv[1])); repo = sys.argv[2]; watcher = sys.argv[3] == "true"; lists = sys.argv[4] == "true"; drift = sys.argv[5] == "true"; drift_field = sys.argv[6] == "true"
sys.path.insert(0, sys.argv[7])
import gitops_repo
d.pop("status", None)
for k in ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields", "finalizers", "deletionTimestamp"): d["metadata"].pop(k, None)
d["metadata"].get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
integration = d["spec"].setdefault("integration", {})
if repo and (lists or integration.get("forges") or integration.get("repositories")):
    # Also for a spec already in the lists form (a pinned run leaves it so),
    # beside which the alias would be refused. The alias is one forge named
    # github with namespace org, and its gitRepo the gitops repository on it;
    # other forges and repositories are kept.
    alias = integration.pop("github", None) or {}
    forges = integration.setdefault("forges", [])
    forge = next(iter(gitops_repo.github_forges(integration)), None)
    if forge is None:
        # Under a name no forge has: the schema keys forges on name, and one
        # named github may be on another provider, or refused.
        names = {f.get("name") for f in forges}
        forge = next(n for n in ["github"] + ["github-%d" % i for i in range(2, len(forges) + 2)] if n not in names)
        forges.append({"name": forge, "provider": "github", **({"namespace": alias["org"]} if alias.get("org") else {})})
    # The existing gitops entry for the same repository (resolved as the
    # operator and agent-base-branch.sh resolve it) keeps its forge, namespace
    # and baseBranch, so an administrator'"'"'s base survives the reset; one for
    # another repository is replaced, and a base it held is logged.
    same = gitops_repo.gitops_entry(integration, repo)
    gitops = {"forge": forge, "repository": repo, "role": "gitops"}
    for i, r in enumerate(integration.get("repositories") or []):
        if r.get("role") != "gitops":
            continue
        if same and i == same[0]:
            gitops.update({k: r[k] for k in ("forge", "namespace", "baseBranch") if k in r})
        elif r.get("baseBranch"):
            print("==> the reset replaces the gitops entry %s, and with it its baseBranch %s" % (r.get("repository"), r["baseBranch"]), file=sys.stderr)
    integration["repositories"] = [gitops] + [
        r for r in integration.get("repositories") or [] if r.get("role") != "gitops"]
elif repo:
    integration.setdefault("github", {})["gitRepo"] = repo
d["spec"].setdefault("harness", {})["eventWatcher"] = {"enabled": watcher}
if drift_field:
    d["spec"]["harness"]["driftDetector"] = {**(d["spec"]["harness"].get("driftDetector") or {}), "enabled": drift}
print(json.dumps(d))' "${backup}" "${GITOPS_REPO:-}" "${AGENT_EVENT_WATCHER:-false}" "${lists}" "${AGENT_DRIFT_DETECTOR:-false}" "${drift_field}" "${GITOPS_REPO_HELPER%/*}" > "${reapply}"
  "${K[@]}" delete "${CR}" --wait --timeout="${CR_REMOVE_TIMEOUT}"
  for pvc in ${SHELL_PVCS}; do "${K[@]}" delete pvc "${pvc}" --ignore-not-found --wait=false; done
  waited=0
  # shellcheck disable=SC2086
  while "${K[@]}" get pvc ${OWNED_PVCS} ${SHELL_PVCS} >/dev/null 2>&1; do
    [ "${waited}" -lt "${PVC_GONE_TIMEOUT_SEC}" ] || { echo "agent volumes still present after ${PVC_GONE_TIMEOUT_SEC}s" >&2; "${K[@]}" get pvc >&2; exit 1; }
    sleep "${PVC_POLL_INTERVAL_SEC}"; waited=$((waited + PVC_POLL_INTERVAL_SEC))
  done
  "${K[@]}" apply -f "${reapply}"
  "${K[@]}" wait "${CR}" --for=condition=Ready --timeout="${CR_READY_TIMEOUT}"
  "${K[@]}" rollout status deploy/platform-agent-gateway --timeout="${GATEWAY_ROLLOUT_TIMEOUT}" >/dev/null
  "${K[@]}" rollout status sts/"${SHELL_STATEFULSET}" --timeout="${GATEWAY_ROLLOUT_TIMEOUT}" >/dev/null
  echo "==> PlatformAgent Ready on fresh volumes (managed repository: ${GITOPS_REPO:-unchanged})"
}
AGENT_STORES=""
if [ "${AGENT_STATE_RESET:-false}" = "true" ]; then
  reset_agent_state
  assert_fresh_agent
fi
# The credential proxy refuses repositories the install does not manage, so
# the run proves the mint for GITOPS_REPO before the cluster exists: after the
# reset, once the re-applied PlatformAgent names the run's repository, or right
# away when there was no reset (a PlatformAgent naming another repository fails
# here, not at the agent's first push an hour in). Relative to the bench
# directory (the cd above), not to this file: a run executes a frozen copy of
# this script from results/ so that an edit during the run cannot reach it.
./hack/gitops-run-repo.sh check "${slug}"

# 2. agent base branch ------------------------------------------------------
# Unless the case pins the base (below), the stack makes the run branch the
# repository's default branch for the run
# and restores it on destroy; the agent's submit-suggestion re-asks the remote
# for its default before each PR (decision 3 in the pilot notes). One run at a
# time (the stack refuses to switch when the default already points at a
# run/** branch), and BENCH_NO_TEARDOWN=true leaves the repository's default on
# the run branch until the destroy is run by hand. A case that pins the base
# has the GitOps repository's baseBranch in spec.integration.repositories set
# for the run instead, which the operator renders into the credential broker
# and the broker enforces (#1970). Runs 1 to 13 set
# GITOPS_BASE_BRANCH on the PlatformAgent instead, on a 0.4.0 install whose
# operator copied it into the agent container; on the shell-sandbox layout
# every command runs in platform-agent-shell-0, whose environment does not
# take spec.deployment.env, so the variable never reaches the process that
# opens the PR, and that mode is gone.
# The case's own variables decide how the agent gets its base; the wrapper
# reads them and logs what the case does. A case that sets
# gitops_pin_agent_base_branch has its stack set the baseBranch of the
# PlatformAgent's GitOps repository entry (spec.integration.repositories[]) to
# the run branch and leave the repository default alone, so the
# default-branch switch is not exported for it.
# BENCH_NO_TEARDOWN=true leaves that pin in place until the destroy is run by
# hand (see 5).
case "${BASE_BRANCH_MODE:-default-branch}" in
  default-branch) ;;
  *) echo "BASE_BRANCH_MODE=${BASE_BRANCH_MODE} is not offered: the case's variables decide how the agent gets its base (see the comment above)" >&2; exit 1 ;;
esac
case_switch="$(case_var gitops_switch_default_branch)"
if [ "${case_pin}" = "true" ]; then
  echo "==> base branch via the PlatformAgent's spec.integration.repositories[].baseBranch (${CASE} sets gitops_pin_agent_base_branch: its stack pins the base for the run, and the repository default stays ${REPO_DEFAULT_BRANCH})"
elif [ "${case_switch}" = "false" ]; then
  echo "${CASE} sets gitops_switch_default_branch false and pins no base: nothing would make the run branch the agent's base" >&2
  exit 1
else
  echo "==> base branch via repository default (stack switches it for the run)"
  export TF_VAR_gitops_switch_default_branch=true
fi

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
# The stack names the Argo Application after the task; the harness looks it up.
export GITOPS_ARGO_APP="${TASK}"
export GITOPS_ARGO_CONTEXT="gke_${GCP_PROJECT_ID}_${GCP_LOCATION}_${CLUSTER_NAME}"
# tofu fetches the kind module over https; a global insteadOf to ssh breaks it.
export GIT_CONFIG_GLOBAL=/dev/null

[ -n "${GITOPS_REPO:-}" ] && export GITOPS_REPO
echo "==> devops-bench ${TASK_SOURCE} (cluster ${CLUSTER_NAME}, argo context ${GITOPS_ARGO_CONTEXT})"
# --no-sync: a plain `uv run` re-syncs the venv from the lockfile first, which
# silently puts the upstream devops-bench pin back and drops `mode: hold`
# support (run 1 verified only 2 of 7 checks for exactly this reason).
rc=0
uv run --no-sync devops-bench "${TASK_SOURCE}" --agent-type kubeagents "$@" || rc=$?

# 5. pull requests, stamp and leak check (#1773) ----------------------------
# The harness waits only for a pull request onto the run branch, so a run whose
# agent opened its pull request onto the default branch and a run whose agent
# opened none both record no_pr. The pull requests opened in the repository
# since the run started, with their bases (hack/gitops-run-prs.sh; null when
# the listing failed), tell the two apart, and a pull request onto the run
# branch on an install that pins no base shows an agent that found the branch
# on its own. Then what the row was produced with,
# beside the record, so a campaign's rows can be shown to share one setup;
# then the checks that catch a leaked run (a cluster or branch left behind,
# the default branch still switched, a baseBranch on the PlatformAgent still
# pinned).
gh_token="$(tr -d '\r\n' < "${GITOPS_TOKEN_FILE}")"
RUN_PRS="$(GH_TOKEN="${gh_token}" ./hack/gitops-run-prs.sh "${slug}" "${RUN_STARTED_AT}" "${REPO_DEFAULT_BRANCH}")" \
  || RUN_PRS=null
export RUN_PRS
run_dir="$(ls -td "${RESULTS_DIR}"/run_* 2>/dev/null | head -1 || true)"
if [ -n "${run_dir}" ] && [ -n "$(find "${run_dir}" -newer "${TASK_SOURCE}/task.yaml" -name results.json | head -1)" ]; then
  export STAMP_CASE="${CASE}" STAMP_STARTED="${RUN_STARTED_AT}" STAMP_DEFAULT_BRANCH="${REPO_DEFAULT_BRANCH}"
  export STAMP_TASK="${TASK}" STAMP_CLUSTER="${CLUSTER_NAME}" STAMP_BRANCH="${RUN_BRANCH}" \
    STAMP_REPO="${GITOPS_REPO}" STAMP_ROOT="${GITOPS_BROKEN_BASE_SHA}" \
    STAMP_MODEL="${AGENT_MODEL}" STAMP_JUDGE="${JUDGE_MODEL}" STAMP_PIN="${DEVOPS_BENCH_PIN:-repository pin}" \
    STAMP_RESET="${AGENT_STATE_RESET:-false}" STAMP_CONTEXT="${AGENT_HOST_CONTEXT}" STAMP_NAMESPACE="${AGENT_NAMESPACE}"
  python3 - "${run_dir}/${STAMP_FILE}" <<'STAMP'
import json, os, subprocess, sys
e = os.environ
K = ["kubectl", "--context", e["STAMP_CONTEXT"], "-n", e["STAMP_NAMESPACE"]]
def sh(*a): return subprocess.run(a, capture_output=True, text=True).stdout.strip()
stamp = {
  "task": e["STAMP_TASK"], "cluster": e["STAMP_CLUSTER"], "run_branch": e["STAMP_BRANCH"],
  "gitops_repo": e["STAMP_REPO"], "gitops_repo_root_sha": e["STAMP_ROOT"],
  "agent_model": e["STAMP_MODEL"], "judge_model": e["STAMP_JUDGE"], "devops_bench_pin": e["STAMP_PIN"],
  "agent_image": sh(*K, "get", "deploy", "platform-agent-gateway", "-o", "jsonpath={.spec.template.spec.containers[?(@.name=='platform-agent')].image}"),
  "operator_image": sh(*K, "get", "deploy", "kubeagents-controller-manager", "-o", "jsonpath={.spec.template.spec.containers[*].image}"),
  "kube_agents_commit": sh("git", "rev-parse", "HEAD"),
  "agent_state_reset": e["STAMP_RESET"],
  "agent_stores_before_run": json.loads(e.get("AGENT_STORES") or "null"),
  "case": e["STAMP_CASE"], "run_started_at": e["STAMP_STARTED"],
  # Every pull request opened since run_started_at, with its base; null when
  # the listing failed.
  "default_branch": e["STAMP_DEFAULT_BRANCH"],
  "pull_requests": json.loads(e.get("RUN_PRS") or "null"),
}
json.dump(stamp, open(sys.argv[1], "w"), indent=2)
print("==> stamp written to", sys.argv[1])
STAMP
fi
# The sweep walks roots for run_*/results.json (real directories, not
# links), so it gets a scratch root holding a copy of this run alone: the
# corpus-level checks (several runs in one cell) are for the campaign's own
# summary, not for a per-run record.
if [ -n "${run_dir}" ] && [ -n "${INTEGRITY_SWEEP_SCRIPT:-}" ] && [ -f "${run_dir}/results.json" ]; then
  sweep_root="$(mktemp -d "${TMPDIR:-/tmp}/integrity-sweep.XXXXXX")"
  cp -R "${run_dir}" "${sweep_root}/"
  python3 "${INTEGRITY_SWEEP_SCRIPT}" "${sweep_root}" --json "${run_dir}/${SWEEP_JSON}" --md "${run_dir}/${SWEEP_MD}" \
    && echo "==> integrity sweep written to ${run_dir}/${SWEEP_MD}" \
    || echo "WARN integrity sweep failed (see above); record kept" >&2
  rm -rf "${sweep_root}"
fi
if [ "${BENCH_NO_TEARDOWN:-false}" != "true" ]; then
  repo_slug="${slug}"
  if GH_TOKEN="${gh_token}" gh api "repos/${repo_slug}/git/refs/heads/${RUN_BRANCH}" >/dev/null 2>&1; then
    echo "WARN leak: run branch ${RUN_BRANCH} still exists in ${repo_slug}" >&2
  fi
  default_branch="$(GH_TOKEN="${gh_token}" gh api "repos/${repo_slug}" --jq .default_branch 2>/dev/null || true)"
  [ "${default_branch}" = "${REPO_DEFAULT_BRANCH}" ] || echo "WARN leak: default branch of ${repo_slug} is '${default_branch}', not ${REPO_DEFAULT_BRANCH}" >&2
  if ! agent_base_lines="$(agent_bases 2>/dev/null)"; then
    echo "WARN leak: whether a PlatformAgent spec.integration.repositories[].baseBranch still names ${RUN_BRANCH} is unknown (it could not be read); check it by hand" >&2
  elif cut -f3 <<<"${agent_base_lines}" | grep -qxF "${RUN_BRANCH}"; then
    echo "WARN leak: a PlatformAgent spec.integration.repositories[].baseBranch still names ${RUN_BRANCH}" >&2
  fi
  if gcloud container clusters list --project "${GCP_PROJECT_ID}" --filter="name=${CLUSTER_NAME}" --format="value(name)" 2>/dev/null | grep -q .; then
    echo "WARN leak: task cluster ${CLUSTER_NAME} still exists" >&2
  fi
elif [ "${case_pin}" = "true" ] \
  && { agent_bases 2>/dev/null || true; } | cut -f3 | grep -qxF "${RUN_BRANCH}"; then
  echo "WARN BENCH_NO_TEARDOWN=true: the PlatformAgent's spec.integration.repositories[].baseBranch still names ${RUN_BRANCH}, so every proposal onto ${slug} from this install targets it until the stack's destroy is run by hand" >&2
fi
exit "${rc}"
