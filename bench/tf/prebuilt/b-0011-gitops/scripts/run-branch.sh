#!/usr/bin/env bash
#
# Create or delete the per-run branch in the GitOps repository through the
# GitHub REST API. Called by main.tf's null_resource.run_branch: `create` from
# the create-time provisioner, `delete` from the destroy-time one.
#
# create: points refs/heads/$GITOPS_RUN_BRANCH at $GITOPS_BASE_SHA. If the
#         branch already exists (a rerun, or a previous run whose destroy never
#         ran) it is force-reset to the base, so every run starts from the same
#         broken state.
# delete: removes the ref; an already-missing ref is not an error.
#
# Only branches under run/ are ever written. The default branch's content is
# never written; the pilot-only default-branch mode below moves the
# default-branch pointer to the run branch and back.
#
# Env: GITOPS_REPO (https URL), GITOPS_RUN_BRANCH, GITOPS_TOKEN_FILE,
#      GITOPS_BASE_SHA (create only),
#      GITOPS_SWITCH_DEFAULT_BRANCH=true and GITOPS_RESTORE_DEFAULT_BRANCH (see below).
set -euo pipefail

GITHUB_API="https://api.github.com"
GITHUB_API_VERSION="2022-11-28"

ACTION="${1:?usage: $0 create|delete}"
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

case "${ACTION}" in
  create)
    : "${GITOPS_BASE_SHA:?}"
    echo "==> run-branch: ${slug} ${GITOPS_RUN_BRANCH} <- ${GITOPS_BASE_SHA}"
    code="$(api -X POST "${GITHUB_API}/repos/${slug}/git/refs" \
      -d "{\"ref\":\"refs/heads/${GITOPS_RUN_BRANCH}\",\"sha\":\"${GITOPS_BASE_SHA}\"}")"
    if [ "${code}" = "422" ]; then
      echo "    branch exists; force-resetting it to the broken base"
      code="$(api -X PATCH "${GITHUB_API}/repos/${slug}/git/refs/heads/${GITOPS_RUN_BRANCH}" \
        -d "{\"sha\":\"${GITOPS_BASE_SHA}\",\"force\":true}")"
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
  delete)
    if [ "${GITOPS_SWITCH_DEFAULT_BRANCH:-false}" = "true" ]; then
      # GitHub refuses to delete the default branch: restore it first.
      set_default_branch "${GITOPS_RESTORE_DEFAULT_BRANCH:-main}"
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
    echo "usage: $0 create|delete" >&2; exit 1 ;;
esac
