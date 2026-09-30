#!/usr/bin/env bash
#
# Create or delete the per-run branch in the GitOps repository through the
# GitHub REST API. Called by main.tf's null_resource.run_branch: `create` from
# the create-time provisioner, `delete` from the destroy-time one.
#
# create:  points refs/heads/$GITOPS_RUN_BRANCH at the broken base. If the
#          branch already exists (a rerun, or a previous run whose destroy never
#          ran) it is force-reset, so every run starts from the same state.
#          For a task with staged history (GITOPS_HISTORY_PARENT_SHA set) the
#          branch starts instead at a fresh `healthy` commit whose parent is
#          that SHA and whose tree is the broken base's tree with the task
#          directory re-rendered healthy (render-broken-base.sh stages). When
#          the base does not carry the task directory at all (a per-run
#          repository's root commit, gke-labs/kube-agents#1773) one commit
#          adds the broken render on top of it.
# advance: staged history only; appends the `inflated` and `broken` commits on
#          the branch head and fast-forwards the ref. The broken commit's tree
#          is byte-identical to the broken base's. Prints the new head on
#          stdout (setup.sh waits for Argo to reach it); progress goes to
#          stderr.
# delete:  removes the ref; an already-missing ref is not an error.
#
# Only branches under run/ are ever written. The default branch's content is
# never written; the pilot-only default-branch mode below moves the
# default-branch pointer to the run branch and back.
#
# Env: GITOPS_REPO (https URL), GITOPS_RUN_BRANCH, GITOPS_TOKEN_FILE,
#      GITOPS_BASE_SHA (create, advance),
#      GITOPS_HISTORY_PARENT_SHA, GITOPS_TASK, GITOPS_TASK_PATH,
#      GITOPS_MANIFESTS_DIR (staged history: create, advance),
#      GITOPS_SWITCH_DEFAULT_BRANCH=true and GITOPS_RESTORE_DEFAULT_BRANCH (see below).
set -euo pipefail

GITHUB_API="https://api.github.com"
GITHUB_API_VERSION="2022-11-28"
RENDER_SCRIPT="$(cd "$(dirname "$0")" && pwd)/render-broken-base.sh"
# Staged history for b-0011 (see render-broken-base.sh): the task's clue is
# that the memory request was inflated in an earlier change and this morning's
# build update came on top of it, so the commits are back-dated to read that
# way. Ages in days before the run; the build update is "this morning". The
# messages are the shape a build pipeline writes and say nothing about the
# change: the clue is the diff and the cluster's rollout history, not a title
# written for the task (gke-labs/kube-agents#1773).
HEALTHY_MESSAGE="Add the payments, edge and ledger manifests under tasks/b-0011"
HEALTHY_AGE_DAYS=10
INFLATED_MESSAGE="payments: update checkout deployment"
INFLATED_AGE_DAYS=3
BROKEN_MESSAGE="payments: update checkout deployment"
BROKEN_AGE_DAYS=0
# A task without staged history on a base that does not carry its directory
# yet (a per-run repository's root commit): one commit adds the broken render.
ADD_MESSAGE_PREFIX="Add manifests under"
ADD_AGE_DAYS=0
# Author when the token's user cannot be read (an App token): commits still
# need a name and an email to carry a date.
FALLBACK_AUTHOR_NAME="platform-team"
FALLBACK_AUTHOR_EMAIL="platform-team@users.noreply.github.com"

ACTION="${1:?usage: $0 create|advance|delete}"
: "${GITOPS_REPO:?}" "${GITOPS_RUN_BRANCH:?}" "${GITOPS_TOKEN_FILE:?}"

case "${GITOPS_RUN_BRANCH}" in
  run/*) ;;
  *) echo "run-branch: refusing to touch '${GITOPS_RUN_BRANCH}': only run/** branches are managed" >&2; exit 1 ;;
esac

slug="${GITOPS_REPO#https://github.com/}"
slug="${slug%.git}"
slug="${slug%/}"
token_path="${GITOPS_TOKEN_FILE/#\~/$HOME}"
[ -r "${token_path}" ] || { echo "run-branch: token file ${token_path} is missing or unreadable" >&2; exit 1; }

# The token travels to curl through a header file (mode 600), not on argv,
# where every process on the host could read it from the process table.
body="$(mktemp)"
auth_header="$(mktemp)"
chmod 600 "${auth_header}"
printf 'Authorization: Bearer %s\n' "$(tr -d '\r\n' < "${token_path}")" > "${auth_header}"
trap 'rm -f "${body}" "${auth_header}"' EXIT

api() {
  curl -sS -o "${body}" -w '%{http_code}' \
    -H "@${auth_header}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: ${GITHUB_API_VERSION}" \
    "$@"
}

# Pilot-only fallback: point the repository's default branch at the run branch
# for the duration of the run, and back at GITOPS_RESTORE_DEFAULT_BRANCH on
# delete. submit-suggestion resolves its PR base from the remote's advertised
# default (`git remote set-head origin --auto`, gitops_workspace.py) when
# GITOPS_BASE_BRANCH is not set, which is the case on an operator whose sandbox
# env allowlist predates that variable. The default branch is a repository-wide
# setting, so this is one run at a time: create refuses to switch when the
# default already points at a run/** branch (another run in flight, or a
# previous run whose destroy never ran), and a run kept alive with
# BENCH_NO_TEARDOWN=true leaves the switch in place until its destroy. Needs
# "administration" permission on the token. Wave 2 replaces both modes with a
# broker-enforced per-request base.
current_default_branch() {
  local code
  code="$(api "${GITHUB_API}/repos/${slug}")"
  [ "${code}" = "200" ] || { echo "run-branch: reading the repository failed, HTTP ${code}: $(cat "${body}")" >&2; return 1; }
  python3 -c 'import json,sys; print(json.load(sys.stdin)["default_branch"])' < "${body}"
}

set_default_branch() {
  local target="$1" code
  code="$(api -X PATCH "${GITHUB_API}/repos/${slug}" -d "{\"default_branch\":\"${target}\"}")"
  case "${code}" in
    200) echo "    default branch -> ${target}" ;;
    *) echo "run-branch: setting default branch to ${target} failed, HTTP ${code}: $(cat "${body}")" >&2; return 1 ;;
  esac
}

# --- staged history -------------------------------------------------------
json_field() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]] if len(sys.argv)==2 else json.load(sys.stdin)[sys.argv[1]][sys.argv[2]])' "$@" < "${body}"; }

days_ago() { python3 -c 'import datetime as d,sys; print((d.datetime.now(d.timezone.utc)-d.timedelta(days=int(sys.argv[1]))).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$1"; }

author_json() {
  local name email code
  code="$(api "${GITHUB_API}/user")"
  if [ "${code}" = "200" ]; then
    name="$(json_field login)"; email="${name}@users.noreply.github.com"
  else
    name="${FALLBACK_AUTHOR_NAME}"; email="${FALLBACK_AUTHOR_EMAIL}"
  fi
  python3 -c 'import json,sys; print(json.dumps({"name":sys.argv[1],"email":sys.argv[2],"date":sys.argv[3]}))' "${name}" "${email}" "$1"
}

# commit_stage <stage> <parent sha> <message> <age days> -> new commit sha on stdout.
# Tree = the broken base's tree with the task directory replaced by the stage's
# render, so every commit carries the rest of the repository (check workflow
# included) unchanged.
commit_stage() {
  local stage="$1" parent="$2" message="$3" age="$4"
  local render code base_tree entries="" f blob tree commit
  : "${GITOPS_BASE_SHA:?}" "${GITOPS_TASK:?}" "${GITOPS_TASK_PATH:?}" "${GITOPS_MANIFESTS_DIR:?}"
  render="$(mktemp -d)"
  "${RENDER_SCRIPT}" "${GITOPS_TASK}" "${GITOPS_MANIFESTS_DIR}" "${render}" "${stage}" >&2
  code="$(api "${GITHUB_API}/repos/${slug}/git/commits/${GITOPS_BASE_SHA}")"
  [ "${code}" = "200" ] || { echo "run-branch: reading base commit ${GITOPS_BASE_SHA} failed, HTTP ${code}: $(cat "${body}")" >&2; return 1; }
  base_tree="$(json_field tree sha)"
  for f in "${render}"/*; do
    code="$(api -X POST "${GITHUB_API}/repos/${slug}/git/blobs" \
      -d "{\"encoding\":\"base64\",\"content\":\"$(base64 < "${f}" | tr -d '\n')\"}")"
    [ "${code}" = "201" ] || { echo "run-branch: creating blob for $(basename "${f}") failed, HTTP ${code}: $(cat "${body}")" >&2; return 1; }
    blob="$(json_field sha)"
    entries="${entries}${entries:+,}{\"path\":\"${GITOPS_TASK_PATH}/$(basename "${f}")\",\"mode\":\"100644\",\"type\":\"blob\",\"sha\":\"${blob}\"}"
  done
  rm -rf "${render}"
  code="$(api -X POST "${GITHUB_API}/repos/${slug}/git/trees" -d "{\"base_tree\":\"${base_tree}\",\"tree\":[${entries}]}")"
  [ "${code}" = "201" ] || { echo "run-branch: creating tree failed, HTTP ${code}: $(cat "${body}")" >&2; return 1; }
  tree="$(json_field sha)"
  code="$(api -X POST "${GITHUB_API}/repos/${slug}/git/commits" \
    -d "{\"message\":\"${message}\",\"tree\":\"${tree}\",\"parents\":[\"${parent}\"],\"author\":$(author_json "$(days_ago "${age}")")}")"
  [ "${code}" = "201" ] || { echo "run-branch: creating commit failed, HTTP ${code}: $(cat "${body}")" >&2; return 1; }
  commit="$(json_field sha)"
  echo "    ${stage}: ${commit} (tree ${tree}) <- ${parent}" >&2
  echo "${commit}"
}

# Whether the base commit already carries the task directory (the shared
# repository that already holds the rendered base does; a per-run repository's root
# commit does not).
base_has_task_path() {
  local code
  code="$(api "${GITHUB_API}/repos/${slug}/contents/${GITOPS_TASK_PATH:?}?ref=${GITOPS_BASE_SHA}")"
  case "${code}" in
    200) return 0 ;;
    404) return 1 ;;
    *) echo "run-branch: reading ${GITOPS_TASK_PATH} at ${GITOPS_BASE_SHA} failed, HTTP ${code}: $(cat "${body}")" >&2; exit 1 ;;
  esac
}

branch_head() {
  local code
  code="$(api "${GITHUB_API}/repos/${slug}/git/refs/heads/${GITOPS_RUN_BRANCH}")"
  [ "${code}" = "200" ] || { echo "run-branch: reading ${GITOPS_RUN_BRANCH} failed, HTTP ${code}: $(cat "${body}")" >&2; return 1; }
  json_field object sha
}

case "${ACTION}" in
  create)
    : "${GITOPS_BASE_SHA:?}"
    if [ -n "${GITOPS_HISTORY_PARENT_SHA:-}" ]; then
      echo "==> run-branch: staged history for ${GITOPS_TASK:?}; healthy commit on ${GITOPS_HISTORY_PARENT_SHA}"
      target="$(commit_stage healthy "${GITOPS_HISTORY_PARENT_SHA}" "${HEALTHY_MESSAGE}" "${HEALTHY_AGE_DAYS}")"
    elif base_has_task_path; then
      target="${GITOPS_BASE_SHA}"
    else
      echo "==> run-branch: ${GITOPS_BASE_SHA} does not carry ${GITOPS_TASK_PATH:?}; committing the broken render on it"
      target="$(commit_stage broken "${GITOPS_BASE_SHA}" "${ADD_MESSAGE_PREFIX} ${GITOPS_TASK_PATH}" "${ADD_AGE_DAYS}")"
    fi
    echo "==> run-branch: ${slug} ${GITOPS_RUN_BRANCH} <- ${target}"
    code="$(api -X POST "${GITHUB_API}/repos/${slug}/git/refs" \
      -d "{\"ref\":\"refs/heads/${GITOPS_RUN_BRANCH}\",\"sha\":\"${target}\"}")"
    if [ "${code}" = "422" ]; then
      echo "    branch exists; force-resetting it"
      code="$(api -X PATCH "${GITHUB_API}/repos/${slug}/git/refs/heads/${GITOPS_RUN_BRANCH}" \
        -d "{\"sha\":\"${target}\",\"force\":true}")"
    fi
    case "${code}" in
      200|201) echo "    ok (HTTP ${code})" ;;
      *) echo "run-branch: create failed, HTTP ${code}: $(cat "${body}")" >&2; exit 1 ;;
    esac
    if [ "${GITOPS_SWITCH_DEFAULT_BRANCH:-false}" = "true" ]; then
      current="$(current_default_branch)"
      case "${current}" in
        run/*)
          echo "run-branch: the default branch is already '${current}' (another run in flight, or a run whose destroy never ran); refusing to switch it" >&2
          exit 1 ;;
      esac
      set_default_branch "${GITOPS_RUN_BRANCH}"
    fi
    ;;
  advance)
    [ -n "${GITOPS_HISTORY_PARENT_SHA:-}" ] || { echo "run-branch: advance is only for tasks with staged history" >&2; exit 1; }
    head="$(branch_head)"
    echo "==> run-branch: advancing ${GITOPS_RUN_BRANCH} from ${head}" >&2
    inflated="$(commit_stage inflated "${head}" "${INFLATED_MESSAGE}" "${INFLATED_AGE_DAYS}")"
    broken="$(commit_stage broken "${inflated}" "${BROKEN_MESSAGE}" "${BROKEN_AGE_DAYS}")"
    code="$(api -X PATCH "${GITHUB_API}/repos/${slug}/git/refs/heads/${GITOPS_RUN_BRANCH}" -d "{\"sha\":\"${broken}\",\"force\":false}")"
    [ "${code}" = "200" ] || { echo "run-branch: fast-forwarding ${GITOPS_RUN_BRANCH} failed, HTTP ${code}: $(cat "${body}")" >&2; exit 1; }
    echo "    ok: ${GITOPS_RUN_BRANCH} -> ${broken}" >&2
    echo "${broken}"
    ;;
  delete)
    if [ "${GITOPS_SWITCH_DEFAULT_BRANCH:-false}" = "true" ]; then
      # GitHub refuses to delete the default branch: restore it first, but
      # only when this run's branch is the default. A run that create refused
      # (the default already on another run's branch) still reaches this on
      # its destroy, and restoring then would pull the default out from under
      # the run in flight.
      current="$(current_default_branch || true)"
      if [ "${current}" = "${GITOPS_RUN_BRANCH}" ]; then
        set_default_branch "${GITOPS_RESTORE_DEFAULT_BRANCH:-main}"
      else
        echo "    default branch is '${current}', not this run's; leaving it"
      fi
    fi
    echo "==> run-branch: deleting ${slug} ${GITOPS_RUN_BRANCH}"
    code="$(api -X DELETE "${GITHUB_API}/repos/${slug}/git/refs/heads/${GITOPS_RUN_BRANCH}")"
    case "${code}" in
      204) echo "    ok" ;;
      422|404) echo "    already gone (HTTP ${code})" ;;
      *) echo "run-branch: delete failed, HTTP ${code}: $(cat "${body}")" >&2; exit 1 ;;
    esac
    ;;
  *)
    echo "usage: $0 create|advance|delete" >&2; exit 1 ;;
esac
