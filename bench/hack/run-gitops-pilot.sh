#!/usr/bin/env bash
#
# Run a GitOps fix-cycle pilot case (gke-labs/kube-agents#1307; TASK=b-0011 or
# b-0022b, case ./tasks/<TASK>-gitops) from a laptop
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
#      that is the branch its PR targets.
#   3. Reads PLATFORM_AGENT_TOKEN and the judge key from the install's secret.
#   4. Runs `devops-bench ./tasks/<TASK>-gitops --agent-type kubeagents` with
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
# Optional:
#   GITOPS_BROKEN_BASE_SHA  commit in GITOPS_REPO that already carries the
#     task's broken base under tasks/<TASK> (render-broken-base.sh output).
#     Unset: the repository's default-branch head is the base and the broken
#     render is committed on it, which is how a per-run repository starts
#   GITOPS_HISTORY_PARENT_SHA  parent of b-0011's staged history (unset: the
#     base above)
#   GCP_LOCATION (us-central1-a)  AGENT_NAMESPACE (kubeagents-system)
#   TASK (b-0011) which case to run: ./tasks/<TASK>-gitops, stack gitops_task <TASK>
#   CLUSTER_NAME (gitops-pilot-<timestamp>; also seeds the run branch name)
#   GITOPS_TOKEN_FILE (~/.config/gitops-pilot/github-token)
#   JUDGE_MODEL (gemini-3.1-pro-preview)
#   GITOPS_REPO_ROOT_SHA (read from GITOPS_REPO when unset) the default-branch
#     head of the repository, used as the base and b-0011's history parent
#     only while GITOPS_BROKEN_BASE_SHA is unset (a per-run repository)
#   AGENT_STATE_RESET=true re-create the PlatformAgent on fresh volumes (with
#     GITOPS_REPO as its managed repository and the event watcher off unless
#     AGENT_EVENT_WATCHER=true) before the run, then refuse to run unless its
#     stores hold nothing but the first-boot discovery card (#1773)
#   DEVOPS_BENCH_PIN (empty: the repository's pin) a pip requirement for another
#     devops-bench, e.g. `devops-bench @ git+https://github.com/pradeepvrd/devops-bench@<sha>`
#   AGENT_MODEL (read from the install's LiteLLM config: the model behind
#     `model-default`) the model id recorded on the result row
#   JUDGE_API_KEY (read from the install's secret when unset)
#   BENCH_VERIFY_TIMEOUT_SEC (120) per-entry cap of the post-run verification
#     pass; BENCH_VERIFY_TOTAL_BUDGET_SEC is derived from it and the entry count
#   BASE_BRANCH_MODE: only `default-branch` (env mode is gone; any other value
#     is refused)
#   INTEGRITY_SWEEP_SCRIPT (empty: skipped) path to devops-bench's
#     integrity-sweep `sweep.py` (kubernetes-sigs/devops-bench#195); run over
#     the finished record alone, writing integrity-sweep.json and .md beside
#     it for adjudication (#1773)
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
readonly RENDERED_TASKS_TEMPLATE="gitops-hold.XXXXXX"
# What the committed prompt carries where the repository URL goes; the wrapper
# renders GITOPS_REPO over it (devops-bench renders only its own placeholders).
readonly PROMPT_REPO_PLACEHOLDER="{{GITOPS_REPO}}"
readonly STAGED_HISTORY_TASK="b-0011"
# Agent state reset (#1773): everything the agent remembers lives on these
# claims; the first two are owned by the PlatformAgent and go with it, the
# shell StatefulSet's two are not and are removed by name.
readonly SHELL_STATEFULSET="platform-agent-shell"
readonly SHELL_PVCS="data-platform-agent-shell-0 sshd-platform-agent-shell-0"
readonly OWNED_PVCS="platform-agent-data system-metadata"
readonly CR_REMOVE_TIMEOUT=600s
readonly CR_READY_TIMEOUT=900s
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
# cold-starts (plugin and skill sync, MCP discovery). Fifteen minutes covers
# the worst case the pilot saw (run 10, 2026-09-15: the startup probe was still
# failing at ten).
readonly GATEWAY_ROLLOUT_TIMEOUT=900s

: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID to the project that hosts the task cluster of a run}"
: "${AGENT_HOST_CONTEXT:?set AGENT_HOST_CONTEXT to the kubectl context of the cluster running the platform agent}"
: "${GITOPS_REPO:?set GITOPS_REPO to the https URL of the GitOps repository}"
: "${GCP_LOCATION:=us-central1-a}"
: "${AGENT_NAMESPACE:=kubeagents-system}"
: "${CLUSTER_NAME:=gitops-pilot-$(date +%Y%m%d-%H%M%S)}"
: "${GITOPS_TOKEN_FILE:=${HOME}/.config/gitops-pilot/github-token}"
: "${JUDGE_MODEL:=gemini-3.1-pro-preview}"

: "${TASK:=b-0011}"   # devops-bench task id; the case is ./tasks/${TASK}-gitops
RUN_BRANCH="run/${CLUSTER_NAME}/${TASK}"   # must match the stack's locals.run_branch
K=(kubectl --context "${AGENT_HOST_CONTEXT}" -n "${AGENT_NAMESPACE}")
CR="platformagents.kubeagents.x-k8s.io/platform-agent"

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
TASK_SOURCE="./tasks/${TASK}-gitops"
[ -f "${TASK_SOURCE}/task.yaml" ] || { echo "no case at ${TASK_SOURCE} (TASK=${TASK})" >&2; exit 1; }
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
slug="${GITOPS_REPO#https://github.com/}"; slug="${slug%.git}"
if [ -z "${GITOPS_BROKEN_BASE_SHA:-}" ]; then
  : "${GITOPS_REPO_ROOT_SHA:=$(GH_TOKEN="$(tr -d '\r\n' < "${GITOPS_TOKEN_FILE}")" gh api "repos/${slug}/commits/${REPO_DEFAULT_BRANCH}" --jq .sha)}"
  [ -n "${GITOPS_REPO_ROOT_SHA}" ] || { echo "could not read ${REPO_DEFAULT_BRANCH}'s head in ${GITOPS_REPO}" >&2; exit 1; }
  GITOPS_BROKEN_BASE_SHA="${GITOPS_REPO_ROOT_SHA}"
  if [ "${TASK}" = "${STAGED_HISTORY_TASK}" ]; then : "${GITOPS_HISTORY_PARENT_SHA:=${GITOPS_REPO_ROOT_SHA}}"; fi
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
# its managed repository, and wait for Ready. Then prove the stores are empty
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
  local backup pvc waited
  backup="${TMPDIR:-/tmp}/platformagent-$(date +%Y%m%d-%H%M%S).json"
  "${K[@]}" get "${CR}" -o json > "${backup}"
  echo "==> resetting agent state: PlatformAgent saved to ${backup}"
  "${K[@]}" delete "${CR}" --wait --timeout="${CR_REMOVE_TIMEOUT}"
  for pvc in ${SHELL_PVCS}; do "${K[@]}" delete pvc "${pvc}" --ignore-not-found --wait=false; done
  waited=0
  # shellcheck disable=SC2086
  while "${K[@]}" get pvc ${OWNED_PVCS} ${SHELL_PVCS} >/dev/null 2>&1; do
    [ "${waited}" -lt "${PVC_GONE_TIMEOUT_SEC}" ] || { echo "agent volumes still present after ${PVC_GONE_TIMEOUT_SEC}s" >&2; "${K[@]}" get pvc >&2; exit 1; }
    sleep "${PVC_POLL_INTERVAL_SEC}"; waited=$((waited + PVC_POLL_INTERVAL_SEC))
  done
  # The event watcher turns Warning events from every watched cluster into
  # autonomous triage cards; on a benchmark run the prompt must be the only
  # stimulus (a reset alone made it file four cards about the host), so the
  # re-applied agent has it off. AGENT_EVENT_WATCHER=true keeps it on.
  python3 -c '
import json, sys
d = json.load(open(sys.argv[1])); repo = sys.argv[2]; watcher = sys.argv[3] == "true"
d.pop("status", None)
for k in ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields", "finalizers", "deletionTimestamp"): d["metadata"].pop(k, None)
d["metadata"].get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
if repo: d["spec"].setdefault("integration", {}).setdefault("github", {})["gitRepo"] = repo
d["spec"].setdefault("harness", {})["eventWatcher"] = {"enabled": watcher}
print(json.dumps(d))' "${backup}" "${GITOPS_REPO:-}" "${AGENT_EVENT_WATCHER:-false}" | "${K[@]}" apply -f -
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

# 5. stamp and leak check (#1773) -------------------------------------------
# What the row was produced with, beside the record, so a campaign's rows can
# be shown to share one setup; then the checks that catch a leaked run (a
# cluster or branch left behind, the default branch still switched).
run_dir="$(ls -td "${RESULTS_DIR}"/run_* 2>/dev/null | head -1 || true)"
if [ -n "${run_dir}" ] && [ -n "$(find "${run_dir}" -newer "${TASK_SOURCE}/task.yaml" -name results.json | head -1)" ]; then
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
  gh_token="$(tr -d '\r\n' < "${GITOPS_TOKEN_FILE}")"
  repo_slug="${slug}"
  if GH_TOKEN="${gh_token}" gh api "repos/${repo_slug}/git/refs/heads/${RUN_BRANCH}" >/dev/null 2>&1; then
    echo "WARN leak: run branch ${RUN_BRANCH} still exists in ${repo_slug}" >&2
  fi
  default_branch="$(GH_TOKEN="${gh_token}" gh api "repos/${repo_slug}" --jq .default_branch 2>/dev/null || true)"
  [ "${default_branch}" = "${REPO_DEFAULT_BRANCH}" ] || echo "WARN leak: default branch of ${repo_slug} is '${default_branch}', not ${REPO_DEFAULT_BRANCH}" >&2
  if gcloud container clusters list --project "${GCP_PROJECT_ID}" --filter="name=${CLUSTER_NAME}" --format="value(name)" 2>/dev/null | grep -q .; then
    echo "WARN leak: task cluster ${CLUSTER_NAME} still exists" >&2
  fi
fi
exit "${rc}"
