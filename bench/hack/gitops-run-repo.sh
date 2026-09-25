#!/usr/bin/env bash
#
# One GitOps repository per benchmark run (gke-labs/kube-agents#1773). Merged
# pull requests cannot be deleted on GitHub, so a run that must not see an
# earlier run's fix needs a repository no earlier run wrote to, not a fresh
# branch. This script creates that repository with one neutral root commit
# (the files under bench/tf/prebuilt/gitops-fix-cycle/repo: a README and the
# merge-on-green workflow; no task directory, no history), wires it into the
# install so the agent can mint a token for it, proves the mint, and archives
# it when the campaign is done.
#
# Actions:
#   minter-mount      one-time: mount the minter's whole ConfigMap at
#                     /etc/minty/<org>/ (keys <repo>.yaml) instead of one file
#                     by subPath, so adding a repository is a ConfigMap key.
#   create <name>     create <org>/<name>, push the root commit, add the
#                     repository to the GitHub App installation and to the
#                     minter's config, restart the minter. Prints the root
#                     commit SHA on stdout; progress goes to stderr.
#   check <name>      from the shell sandbox pod, mint a token for the
#                     repository through the credential proxy (what the agent
#                     does before its first push). Exit 0 means the wiring
#                     holds end to end. The proxy refuses repositories the
#                     install does not manage, so this passes only once the
#                     PlatformAgent's gitRepo names <name> (the wrapper's
#                     AGENT_STATE_RESET does that before calling it).
#   archive <name>    mark the repository read-only and drop its minter entry.
#
# Env. Required, with no defaults, because each names an install or an
# organisation of yours:
#   AGENT_HOST_CONTEXT          kubectl context of the cluster running the
#                               platform agent (every action)
#   GITOPS_ORG                  GitHub organisation the repositories live in
#                               (minter-mount, create, archive; `check` takes
#                               an owner/name and needs it only for a bare name)
#   GITOPS_APP_INSTALLATION_ID  installation id, on that organisation, of the
#                               GitHub App the install's minter signs for; an
#                               installation on selected repositories needs
#                               every new repository added to it (create)
# Optional:
#   GITOPS_TOKEN_FILE (~/.config/gitops-pilot/github-token; must be an org
#     admin's token: repository creation and the installation edit need it)
#   AGENT_NAMESPACE (kubeagents-system)
#   GITOPS_RUN_REPO_PREFIX (kube-agents-eval): the prefix every repository this
#     script creates or archives must carry, the guard against writing to any
#     other repository of the organisation
set -euo pipefail

: "${AGENT_HOST_CONTEXT:?set AGENT_HOST_CONTEXT to the kubectl context of the cluster running the platform agent}"
: "${GITOPS_TOKEN_FILE:=${HOME}/.config/gitops-pilot/github-token}"
: "${AGENT_NAMESPACE:=kubeagents-system}"
: "${GITOPS_RUN_REPO_PREFIX:=kube-agents-eval}"

TEMPLATE_DIR="$(cd "$(dirname "$0")/.." && pwd)/tf/prebuilt/gitops-fix-cycle/repo"
readonly TEMPLATE_DIR
readonly ROOT_COMMIT_MESSAGE="Initial import of the platform manifests"
readonly ROOT_AUTHOR_NAME="platform-team"
readonly ROOT_AUTHOR_EMAIL="platform-team@users.noreply.github.com"
# The root commit predates the staged history's oldest commit (ten days), so
# the log reads oldest-first by date as well as by parentage.
readonly ROOT_AGE_DAYS=30
readonly MINTER_CONFIGMAP="github-token-minter-config"
readonly MINTER_DEPLOYMENT="github-token-minter"
readonly MINTER_VOLUME="config-volume"
readonly MINTER_CONFIGS_DIR="/etc/minty"
readonly MINTER_ROLLOUT_TIMEOUT="180s"
readonly MINTER_SCOPE_NAME="platform-agent-scope"
readonly PLATFORM_AGENT_CR="platformagents.kubeagents.x-k8s.io/platform-agent"
readonly GSA_ANNOTATION="iam.gke.io/gcp-service-account"
readonly SHELL_POD="platform-agent-shell-0"
readonly REFRESH_SCRIPT="/opt/data/scripts/github_token_refresh.py"
readonly REPO_NAME_PATTERN="^${GITOPS_RUN_REPO_PREFIX}-[a-z0-9-]+\$"

ACTION="${1:?usage: $0 minter-mount | create <name> | check <name> | archive <name>}"
NAME="${2:-}"
K=(kubectl --context "${AGENT_HOST_CONTEXT}" -n "${AGENT_NAMESPACE}")

log() { echo "$*" >&2; }
die() { log "gitops-run-repo: $*"; exit 1; }

token() { tr -d '\r\n' < "${GITOPS_TOKEN_FILE/#\~/$HOME}"; }
gh_api() { GH_TOKEN="$(token)" gh api -H "Accept: application/vnd.github+json" "$@"; }

need_name() {
  [ -n "${NAME}" ] || die "${ACTION} needs a repository name"
}
need_org() {
  [ -n "${GITOPS_ORG:-}" ] || die "${ACTION} needs GITOPS_ORG, the GitHub organisation the repositories live in"
}
# create and archive write to the org; only repositories carrying the prefix.
need_campaign_name() {
  need_name
  need_org
  [[ "${NAME}" =~ ${REPO_NAME_PATTERN} ]] || die "refusing '${NAME}': repositories this script creates or archives are named ${GITOPS_RUN_REPO_PREFIX}-<...> (GITOPS_RUN_REPO_PREFIX)"
}
# check takes owner/name, or a bare name under GITOPS_ORG.
repo_slug() {
  case "${NAME}" in
    */*) echo "${NAME}" ;;
    *) need_org; echo "${GITOPS_ORG}/${NAME}" ;;
  esac
}

# --- minter ----------------------------------------------------------------
minter_mounts_dir() {
  "${K[@]}" get deploy "${MINTER_DEPLOYMENT}" -o json | python3 -c '
import json, sys
org, d = sys.argv[1], json.load(sys.stdin)
for c in d["spec"]["template"]["spec"]["containers"]:
    for m in c.get("volumeMounts", []):
        if m["mountPath"].rstrip("/") == f"/etc/minty/{org}" and not m.get("subPath"):
            sys.exit(0)
sys.exit(1)' "${GITOPS_ORG}"
}

minter_scope_yaml() {
  local gsa
  gsa="$("${K[@]}" get "${PLATFORM_AGENT_CR}" -o jsonpath="{.spec.security.serviceAccountAnnotations.${GSA_ANNOTATION//./\\.}}")"
  [ -n "${gsa}" ] || die "could not read the agent's GSA from ${PLATFORM_AGENT_CR}"
  cat <<YAML
version: 'minty.abcxyz.dev/v2'
rule:
  if: "assertion.iss == 'https://accounts.google.com'"
scope:
  ${MINTER_SCOPE_NAME}:
    rule:
      if: "assertion.email in ['${gsa}']"
    repositories:
      - '${1}'
    permissions:
      contents: 'write'
      pull_requests: 'write'
      issues: 'write'
YAML
}

# minter_set_key <key> <file|-> ; minter_drop_key <key>
minter_set_key() {
  "${K[@]}" get cm "${MINTER_CONFIGMAP}" -o json | python3 -c '
import json, sys
key, path, d = sys.argv[1], sys.argv[2], json.load(sys.stdin)
d["data"][key] = open(path).read()
for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"): d["metadata"].pop(k, None)
d["metadata"].get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
json.dump(d, sys.stdout)' "$1" "$2" | "${K[@]}" apply -f - >&2
}
minter_drop_key() {
  "${K[@]}" get cm "${MINTER_CONFIGMAP}" -o json | python3 -c '
import json, sys
key, d = sys.argv[1], json.load(sys.stdin)
d["data"].pop(key, None)
for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"): d["metadata"].pop(k, None)
d["metadata"].get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
json.dump(d, sys.stdout)' "$1" | "${K[@]}" apply -f - >&2
}
minter_restart() {
  "${K[@]}" rollout restart deploy/"${MINTER_DEPLOYMENT}" >&2
  "${K[@]}" rollout status deploy/"${MINTER_DEPLOYMENT}" --timeout="${MINTER_ROLLOUT_TIMEOUT}" >&2
}

case "${ACTION}" in
  minter-mount)
    need_org
    # Converges the minter to one directory mount of the ConfigMap at
    # /etc/minty/<org>/ (keys <repo>.yaml). Re-keys <org>-<repo>.yaml entries,
    # replaces the whole mount list through a JSON patch (a strategic merge
    # keys mounts by mountPath and would keep a subPath mount beside the
    # directory one, which no pod can start with), waits for the rollout, then
    # drops the old keys. Safe to rerun.
    "${K[@]}" get cm "${MINTER_CONFIGMAP}" -o json | python3 -c '
import json, sys
org, d = sys.argv[1], json.load(sys.stdin)
for k in list(d["data"]):
    if k.startswith(org + "-"): d["data"][k[len(org) + 1:]] = d["data"][k]
for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"): d["metadata"].pop(k, None)
d["metadata"].get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
json.dump(d, sys.stdout)' "${GITOPS_ORG}" | "${K[@]}" apply -f - >&2
    patch="$("${K[@]}" get deploy "${MINTER_DEPLOYMENT}" -o json | python3 -c '
import json, sys
org, vol, d = sys.argv[1], sys.argv[2], json.load(sys.stdin)
c = d["spec"]["template"]["spec"]["containers"][0]
want = [m for m in c.get("volumeMounts", []) if m["name"] != vol] + [{"name": vol, "mountPath": f"/etc/minty/{org}", "readOnly": True}]
if c.get("volumeMounts") == want: sys.exit(0)
print(json.dumps([{"op": "replace", "path": "/spec/template/spec/containers/0/volumeMounts", "value": want}]))' "${GITOPS_ORG}" "${MINTER_VOLUME}")"
    if [ -n "${patch}" ]; then
      "${K[@]}" patch deploy "${MINTER_DEPLOYMENT}" --type json -p "${patch}" >&2
    else
      log "mount list already converged"
    fi
    "${K[@]}" rollout status deploy/"${MINTER_DEPLOYMENT}" --timeout="${MINTER_ROLLOUT_TIMEOUT}" >&2
    for key in $("${K[@]}" get cm "${MINTER_CONFIGMAP}" -o json | python3 -c 'import json,sys; print(" ".join(k for k in json.load(sys.stdin)["data"] if k.startswith(sys.argv[1]+"-")))' "${GITOPS_ORG}"); do
      minter_drop_key "${key}"
    done
    log "minter reads ${MINTER_CONFIGS_DIR}/${GITOPS_ORG}/<repo>.yaml from the ConfigMap"
    ;;

  create)
    : "${GITOPS_APP_INSTALLATION_ID:?set GITOPS_APP_INSTALLATION_ID to the installation id of the GitHub App the minter signs for}"
    need_campaign_name
    minter_mounts_dir || die "the minter mounts one file by subPath; run '$0 minter-mount' once first"
    [ -d "${TEMPLATE_DIR}/.github/workflows" ] || die "template ${TEMPLATE_DIR} missing"
    log "==> creating ${GITOPS_ORG}/${NAME}"
    repo_id="$(gh_api -X POST "orgs/${GITOPS_ORG}/repos" -f name="${NAME}" -F private=true -F has_wiki=false -F has_projects=false \
      -f description="kube-agents benchmark run repository (gke-labs/kube-agents#1773); archived after the run" --jq .id)"
    work="$(mktemp -d)"; trap 'rm -rf "${work}"' EXIT
    cp -R "${TEMPLATE_DIR}/." "${work}/"
    # A global url.insteadOf that rewrites https to ssh would send the push
    # down a key path this token cannot use; the global config stays out.
    export GIT_CONFIG_GLOBAL=/dev/null
    git -C "${work}" init -q -b main
    git -C "${work}" add -A
    root_date="$(python3 -c 'import datetime as d,sys; print((d.datetime.now(d.timezone.utc)-d.timedelta(days=int(sys.argv[1]))).strftime("%Y-%m-%dT%H:%M:%SZ"))' "${ROOT_AGE_DAYS}")"
    GIT_AUTHOR_DATE="${root_date}" GIT_COMMITTER_DATE="${root_date}" \
      git -C "${work}" -c user.name="${ROOT_AUTHOR_NAME}" -c user.email="${ROOT_AUTHOR_EMAIL}" commit -q -m "${ROOT_COMMIT_MESSAGE}"
    root="$(git -C "${work}" rev-parse HEAD)"
    # The token reaches git through a helper that reads the file when git
    # asks for credentials: single-quoted, so the shell does not expand it
    # onto git's argv (where the process table and a set -x trace would show
    # it for the whole push), and the path travels in the environment, as
    # GH_TOKEN does for gh above.
    GITOPS_TOKEN_PATH="${GITOPS_TOKEN_FILE/#\~/$HOME}" \
      git -C "${work}" -c credential.helper='!f() { echo username=x-access-token; echo "password=$(tr -d "\r\n" < "${GITOPS_TOKEN_PATH}")"; }; f' \
      push -q "https://github.com/${GITOPS_ORG}/${NAME}.git" main:main >&2
    log "    root commit ${root}"
    # GitHub answers this with 403 for an OAuth token (tested 2026-09-18), so
    # it is attempted, not required: `check` is what proves the installation
    # reaches the repository, and the manual path is one click per run.
    log "==> adding to App installation ${GITOPS_APP_INSTALLATION_ID}"
    if ! gh_api -X PUT "user/installations/${GITOPS_APP_INSTALLATION_ID}/repositories/${repo_id}" >&2 2>/dev/null; then
      log "    not added through the API; add ${NAME} at https://github.com/organizations/${GITOPS_ORG}/settings/installations/${GITOPS_APP_INSTALLATION_ID} (Repository access), then run: $0 check ${NAME}"
    fi
    log "==> minter entry ${NAME}.yaml"
    scope="$(mktemp)"; minter_scope_yaml "${NAME}" > "${scope}"
    minter_set_key "${NAME}.yaml" "${scope}"; rm -f "${scope}"
    minter_restart
    echo "${root}"
    ;;

  check)
    need_name
    slug="$(repo_slug)"
    log "==> minting a token for ${slug} from ${SHELL_POD} (credential proxy -> minter -> GitHub)"
    "${K[@]}" exec "${SHELL_POD}" -- python3 "${REFRESH_SCRIPT}" "${slug}" >&2
    log "    ok"
    ;;

  archive)
    need_campaign_name
    log "==> archiving ${GITOPS_ORG}/${NAME}"
    gh_api -X PATCH "repos/${GITOPS_ORG}/${NAME}" -F archived=true --jq '.archived' >&2
    minter_drop_key "${NAME}.yaml"
    minter_restart
    ;;

  *) die "unknown action ${ACTION}" ;;
esac
