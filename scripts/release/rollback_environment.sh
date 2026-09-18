#!/usr/bin/env bash
#
# Rolls an installed environment back from the candidate to the previous GA
# release, checks the result, and rolls forward to the candidate again.
#
# This is the rollback runbook (docs/site/src/content/docs/deploy/rollback.md)
# run by a machine: from a clean checkout of the GA tag, `upgrade.sh
# --upgrade-mode=operator` then `--upgrade-mode=harness`, both `--image-tag`
# that GA. The roll-forward is the same pair from the candidate checkout, so
# one run exercises both directions between N-1 and N and leaves the
# environment where it found it.
#
# Inputs (environment):
#   CANDIDATE_SHA            The commit the environment runs now (N). Required.
#   ROLLBACK_TAG             The GA to roll back to (N-1). Default: the newest
#                            pure-numeric tag in this checkout.
#   ROLL_FORWARD             `false` to stop after the rollback. Default true.
#   KUBE_AGENTS_INSTALL_ENV  The install's install.env. When unset, one is
#                            rendered from the environment with
#                            render_install_env.sh (not --strict; see below).
#   ROLLBACK_MODE            `resolve` prints the tag it would roll back to and
#                            exits without touching anything.
#
# Outputs: a Markdown table in GITHUB_STEP_SUMMARY and `diagnostics_dir` in
# GITHUB_OUTPUT. Exit 0 only when every step and every check passed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

readonly WORK_DIR="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/rollback-leg"
readonly ROLLBACK_CHECKOUT="${WORK_DIR}/kube-agents-rollback"
readonly CANDIDATE_CHECKOUT="${WORK_DIR}/kube-agents-candidate"
readonly DIAGNOSTICS_DIR="${WORK_DIR}/diagnostics"
readonly RENDERED_INSTALL_ENV="${WORK_DIR}/install.env"
readonly CONFIRM_IMAGE_SCRIPT="${REPO_ROOT}/scripts/confirm_agent_image.sh"
readonly DEFAULT_NAMESPACE="kubeagents-system"
readonly HELM_RELEASE="kube-agents"
readonly PLATFORM_AGENT_RESOURCE="platform-agent"
readonly GATEWAY_DEPLOYMENT="platform-agent-gateway"
readonly OPERATOR_DEPLOYMENT="kube-agents-controller-manager"
readonly OPERATOR_CONTAINER="manager"
readonly OPERATOR_POD_SELECTOR="app.kubernetes.io/name=kube-agents-operator"
readonly LITELLM_POLICY="litellm-policy"
readonly OPERATOR_MANAGED_BY_LABEL="platformagent-controller"
readonly HELM_MANAGED_BY_LABEL="Helm"
readonly HELM_KEEP_ANNOTATION="helm.sh/resource-policy=keep"
readonly OPERATOR_SCALE_TIMEOUT_SECONDS=120
# The last GA whose chart renders litellm-policy itself; from the next one on
# the operator creates and owns it (#1195, #1488). The chart text is no guide:
# the current template still names the object under a condition that is false
# on a default install, so a grep would say "renders it" for every release.
readonly LAST_GA_WITH_STATIC_LITELLM_POLICY="0.5.0"
# What a release's upgrade.sh re-tags, for a target checkout without an
# images.json to say which first-party images it publishes.
readonly RETAGGED_IMAGE_NAMES="k8s-operator
platform-agent
agent-sandbox"
# confirm_agent_image.sh polls; this is how long a re-tagged Deployment gets to
# show the new tag in its template, which is immediate once Helm has returned.
readonly IMAGE_CONFIRM_TIMEOUT_SECONDS=120
# The operator's Ready condition follows the agent rollout, which upgrade.sh
# has already waited for; this covers the reconcile that writes the status.
readonly READY_TIMEOUT_SECONDS=600
readonly READY_POLL_SECONDS=15
readonly OPERATOR_LOG_TAIL_LINES=200

ROLLBACK_MODE="${ROLLBACK_MODE:-run}"
ROLL_FORWARD="${ROLL_FORWARD:-true}"
CANDIDATE_SHA="${CANDIDATE_SHA:-}"
ROLLBACK_TAG="${ROLLBACK_TAG:-}"
# Set by the litellm-policy handoff. HANDOFF_DONE means the operator is down
# and is cleared by the restart; LITELLM_POLICY_HANDED_OFF stays set, for the
# roll-forward to know the object is now the release's.
HANDOFF_DONE="false"
LITELLM_POLICY_HANDED_OFF="false"
OPERATOR_REPLICAS_BEFORE_HANDOFF=""

summary() {
  [ -n "${GITHUB_STEP_SUMMARY:-}" ] || return 0
  printf '%s\n' "$*" >>"${GITHUB_STEP_SUMMARY}"
}

output() {
  [ -n "${GITHUB_OUTPUT:-}" ] || return 0
  printf '%s=%s\n' "$1" "$2" >>"${GITHUB_OUTPUT}"
}

fail() {
  echo "::error title=Rollback leg::$*"
  echo "❌ $*" >&2
  exit 1
}

# ---------------------------------------------------------------------------
# Resolve N and N-1.
# ---------------------------------------------------------------------------
[ -n "${CANDIDATE_SHA}" ] || fail "CANDIDATE_SHA is required: the commit the environment runs now."
CANDIDATE_SHA="$(git -C "${REPO_ROOT}" rev-parse --verify "${CANDIDATE_SHA}^{commit}" 2>/dev/null)" ||
  fail "CANDIDATE_SHA '${CANDIDATE_SHA}' is not a commit in this checkout."

if [ -z "${ROLLBACK_TAG}" ]; then
  ROLLBACK_TAG="$(cd "${REPO_ROOT}" && get_latest_ga_tag)"
  [ -n "${ROLLBACK_TAG}" ] || fail "No GA tag in this checkout to roll back to; pass ROLLBACK_TAG, or fetch tags first."
fi
validate_pure_numeric_semver "${ROLLBACK_TAG}" "ROLLBACK_TAG" || exit 1
ROLLBACK_COMMIT="$(git -C "${REPO_ROOT}" rev-parse --verify "refs/tags/${ROLLBACK_TAG}^{commit}" 2>/dev/null)" ||
  fail "Tag '${ROLLBACK_TAG}' is not in this checkout; fetch it first (git fetch --tags)."
# A GA tag lands on a stamped child of the candidate it was cut from, not on
# the candidate itself, so equality alone would let the night after a release
# move N to N under another tag and call it a rollback. Ancestry catches that
# shape, the direct-tag shape, and a hand-dispatched candidate older than the
# newest GA, where the two directions would run swapped.
if git -C "${REPO_ROOT}" merge-base --is-ancestor "${CANDIDATE_SHA}" "${ROLLBACK_COMMIT}"; then
  fail "The ${ROLLBACK_TAG} release was cut from the candidate ${CANDIDATE_SHA:0:7} or from a commit after it; there is no older release to roll back to from here."
fi

echo "======================================================================"
echo "🔁 ROLLBACK LEG"
echo "Candidate (N):    ${CANDIDATE_SHA}"
echo "Roll back to:     ${ROLLBACK_TAG} (${ROLLBACK_COMMIT:0:7})"
echo "Roll forward:     ${ROLL_FORWARD}"
echo "======================================================================"

if [ "${ROLLBACK_MODE}" = "resolve" ]; then
  echo "${ROLLBACK_TAG}"
  exit 0
fi

# ---------------------------------------------------------------------------
# The install's configuration, and a kubectl pointed at it.
# ---------------------------------------------------------------------------
mkdir -p "${WORK_DIR}" "${DIAGNOSTICS_DIR}"
output "diagnostics_dir" "${DIAGNOSTICS_DIR}"

if [ -z "${KUBE_AGENTS_INSTALL_ENV:-}" ]; then
  # Not --strict: the strict render refuses a rebuilt environment such as
  # nightly (Chat enabled with an open allowlist by design, and settings the
  # composition needs that a re-tag does not), and the operator and harness
  # modes only re-tag the release with --reset-then-reuse-values; they apply
  # nothing from install.env to the cluster. The deploy job's lease check
  # renders the same file without --strict for the same reason.
  echo "==> Rendering the install configuration from the environment."
  "${SCRIPT_DIR}/render_install_env.sh" "${RENDERED_INSTALL_ENV}"
  export KUBE_AGENTS_INSTALL_ENV="${RENDERED_INSTALL_ENV}"
fi
[ -f "${KUBE_AGENTS_INSTALL_ENV}" ] || fail "KUBE_AGENTS_INSTALL_ENV points at '${KUBE_AGENTS_INSTALL_ENV}', which does not exist."
set -a
# shellcheck disable=SC1090
. "${KUBE_AGENTS_INSTALL_ENV}"
set +a
NAMESPACE="${NAMESPACE:-${DEFAULT_NAMESPACE}}"
export REGISTRY_PREFIX="${REGISTRY_PREFIX:-${DEFAULT_REGISTRY_PREFIX}}"

# release_connect_kubectl turns application-default credentials off for the
# auth plugin, which is right for a runner authenticated through Workload
# Identity Federation and wrong for a workstation whose gcloud user token the
# DNS endpoint rejects. Off the runner, upgrade.sh fetches credentials itself
# with whatever the operator's gcloud is configured to use; this only checks
# that kubectl already reaches the install before anything is recorded.
if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
  release_connect_kubectl
else
  kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 ||
    fail "kubectl does not reach namespace '${NAMESPACE}' on the current context; connect it to ${CLUSTER_NAME:-the cluster named in install.env} first (gcloud container clusters get-credentials)."
  echo "==> Using the current kubectl context: $(kubectl config current-context)"
fi

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
declare -a RESULT_ROWS=()
CURRENT_STEP=""

record() {
  RESULT_ROWS+=("| $1 | $2 | $3 |")
}

agent_image() {
  kubectl get deployment "${GATEWAY_DEPLOYMENT}" -n "${NAMESPACE}" \
    -o jsonpath='{.spec.template.spec.containers[?(@.name=="platform-agent")].image}' 2>/dev/null || true
}

operator_image() {
  kubectl get deployment "${OPERATOR_DEPLOYMENT}" -n "${NAMESPACE}" \
    -o jsonpath="{.spec.template.spec.containers[?(@.name==\"${OPERATOR_CONTAINER}\")].image}" 2>/dev/null || true
}

ready_condition() {
  kubectl get platformagent "${PLATFORM_AGENT_RESOURCE}" -n "${NAMESPACE}" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true
}

snapshot() {
  local file="${DIAGNOSTICS_DIR}/$1.txt"
  {
    echo "agent image:    $(agent_image)"
    echo "operator image: $(operator_image)"
    echo "Ready:          $(ready_condition)"
    echo
    kubectl get pods -n "${NAMESPACE}" -o wide 2>&1 || true
    echo
    helm history "${HELM_RELEASE}" -n "${NAMESPACE}" 2>&1 || true
  } | tee "${file}"
}

collect_diagnostics() {
  local status=$?
  if [ "${status}" -ne 0 ]; then
    echo "==> Collecting diagnostics for the failed step '${CURRENT_STEP}' into ${DIAGNOSTICS_DIR}."
    # A failure between the handoff and the operator step's return would
    # otherwise leave the operator at zero replicas: on the nightly the
    # teardown erases that, on a workstation it is an outage.
    restart_operator_after_handoff || true
    # Pod listings and events rather than `describe` or the CR's spec: those
    # carry the rendered environment (allowlists, channel ids, the project),
    # and the artifact is downloadable from a public repository.
    kubectl get pods -n "${NAMESPACE}" -o wide >"${DIAGNOSTICS_DIR}/pods.txt" 2>&1 || true
    kubectl get events -n "${NAMESPACE}" --sort-by=.lastTimestamp >"${DIAGNOSTICS_DIR}/events.txt" 2>&1 || true
    kubectl get platformagent "${PLATFORM_AGENT_RESOURCE}" -n "${NAMESPACE}" -o jsonpath='{.status}' \
      >"${DIAGNOSTICS_DIR}/platformagent-status.json" 2>&1 || true
    kubectl logs "deployment/${OPERATOR_DEPLOYMENT}" -c "${OPERATOR_CONTAINER}" -n "${NAMESPACE}" \
      --tail="${OPERATOR_LOG_TAIL_LINES}" >"${DIAGNOSTICS_DIR}/operator.log" 2>&1 || true
    helm history "${HELM_RELEASE}" -n "${NAMESPACE}" >"${DIAGNOSTICS_DIR}/helm-history.txt" 2>&1 || true
    [ -n "${CURRENT_STEP}" ] && record "${CURRENT_STEP}" "❌ failed" "see the diagnostics artifact"
  fi
  summary "### Rollback leg: ${CANDIDATE_SHA:0:7} → ${ROLLBACK_TAG}$([ "${ROLL_FORWARD}" = "true" ] && echo " → ${CANDIDATE_SHA:0:7}")"
  summary ""
  summary "| Step | Result | Detail |"
  summary "| --- | --- | --- |"
  local row
  for row in "${RESULT_ROWS[@]}"; do summary "${row}"; done
  summary ""
  if [ "${status}" -eq 0 ]; then
    summary "Every step passed. The environment is back on ${CANDIDATE_SHA:0:7}."
  else
    summary "Failed at **${CURRENT_STEP}**. The diagnostics artifact holds the upgrade logs, the pod list, the events, the operator log and the Helm history at the time of the failure."
  fi
  exit "${status}"
}
trap collect_diagnostics EXIT

# Clones from this checkout, not from GitHub: the tag and the candidate commit
# are both here already, and upgrade.sh's source check only needs a clean tree
# whose HEAD is the ref it is given.
checkout_at() {
  local ref="$1" dir="$2"
  rm -rf "${dir}"
  git clone --quiet --no-checkout "${REPO_ROOT}" "${dir}"
  git -C "${dir}" fetch --quiet origin "+refs/tags/*:refs/tags/*"
  git -C "${dir}" checkout --quiet --detach "${ref}"
}

run_upgrade() {
  local dir="$1" tag="$2" mode="$3"
  local log="${DIAGNOSTICS_DIR}/upgrade-${mode}-${tag}.log"
  echo "==> ${dir##*/}: ./upgrade.sh --non-interactive --upgrade-mode=${mode} --image-tag ${tag}"
  (cd "${dir}" && ./upgrade.sh --non-interactive --upgrade-mode="${mode}" --image-tag "${tag}") 2>&1 | tee "${log}"
  return "${PIPESTATUS[0]}"
}

# The images the re-tag will pull: every first-party image the install's
# workloads reference now that the target release knows about, at the target
# tag, in the registry each one is pulled from. The registry the install uses
# is the one Helm keeps across the re-tag, whatever install.env says, so that
# is where the tag has to exist. The names come from the target checkout's
# images.json, not this one's: an image added after the target release was
# never published at its tag, and the target's re-tag does not pull it (its
# chart either deletes the object or leaves it on the current image), so it
# is not a reason to refuse; nor is a release image the install does not run.
# Refusing here is what keeps a missing tag from being discovered as an
# ImagePullBackOff after the CRDs and the operator have already moved.
release_image_names() {
  local checkout="$1"
  if [ -f "${checkout}/images.json" ]; then
    jq -r '.images[] | select(.origin == "first-party" and .tagPolicy == "release") | .name' "${checkout}/images.json"
  else
    echo "${RETAGGED_IMAGE_NAMES}"
  fi
}

check_images_exist() {
  local tag="$1" checkout="$2" names repos repo name missing=""
  names="$(release_image_names "${checkout}")"
  repos="$( {
    kubectl get deployment,statefulset -n "${NAMESPACE}" \
      -o jsonpath='{range .items[*]}{range .spec.template.spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.template.spec.containers[*]}{.image}{"\n"}{end}{end}' 2>/dev/null
  } | sed -e 's/@.*$//' -e 's/:[^/]*$//' | sort -u)"
  while IFS= read -r repo; do
    [ -n "${repo}" ] || continue
    name="${repo##*/}"
    grep -qxF "${name}" <<<"${names}" || continue
    if registry_image_exists "${repo}:${tag}"; then
      echo "  ✓ ${repo}:${tag}"
    else
      echo "  ✗ ${repo}:${tag} is not in the registry, or could not be probed"
      missing="${missing} ${name}"
    fi
  done <<<"${repos}"
  if [ -n "${missing}" ]; then
    echo "::error title=Images missing for ${tag}::The install pulls${missing} from a registry that has no :${tag} for them, or that could not be probed from here. The re-tag would leave those pods in ImagePullBackOff; nothing was changed."
    return 1
  fi
}

# The runbook's "N's operator owns an object that N-1's chart renders" case.
# Every chart through 0.5.0 renders litellm-policy; from the first release
# after it the operator creates and labels the object, and Helm refuses to
# adopt an object carrying another manager's ownership labels. The way
# through is the chart README's handoff, done here only when both halves
# hold: the live object is the operator's, and the target GA is one whose
# chart renders it (LAST_GA_WITH_STATIC_LITELLM_POLICY or earlier). The
# operator is stopped first so its watch does not re-stamp the label between
# the relabel and the upgrade, and started again after the operator step,
# because Helm's three-way merge leaves a replica count it did not change
# alone.
target_ga_renders_litellm_policy() {
  [ "$(compare_semver "$1" "${LAST_GA_WITH_STATIC_LITELLM_POLICY}")" != "1" ]
}

handoff_litellm_policy_if_needed() {
  local target_tag="$1" managed_by renders="no"
  managed_by="$(kubectl get networkpolicy "${LITELLM_POLICY}" -n "${NAMESPACE}" \
    -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}' 2>/dev/null || true)"
  target_ga_renders_litellm_policy "${target_tag}" && renders="yes"
  if [ "${managed_by}" != "${OPERATOR_MANAGED_BY_LABEL}" ] || [ "${renders}" != "yes" ]; then
    echo "==> ${LITELLM_POLICY} needs no handoff (managed-by '${managed_by:-<absent>}', ${target_tag}'s chart renders it: ${renders})."
    return 0
  fi
  echo "==> ${LITELLM_POLICY} is the operator's and ${target_tag}'s chart renders it: handing it to Helm first."
  OPERATOR_REPLICAS_BEFORE_HANDOFF="$(kubectl get deployment "${OPERATOR_DEPLOYMENT}" -n "${NAMESPACE}" -o jsonpath='{.spec.replicas}')"
  kubectl scale deployment "${OPERATOR_DEPLOYMENT}" -n "${NAMESPACE}" --replicas=0
  # From here the operator is down, so from here a failure has to restore it.
  HANDOFF_DONE="true"
  LITELLM_POLICY_HANDED_OFF="true"
  kubectl wait --for=delete pod -l "${OPERATOR_POD_SELECTOR}" -n "${NAMESPACE}" \
    --timeout="${OPERATOR_SCALE_TIMEOUT_SECONDS}s" || true
  kubectl label networkpolicy "${LITELLM_POLICY}" -n "${NAMESPACE}" \
    "app.kubernetes.io/managed-by=${HELM_MANAGED_BY_LABEL}" --overwrite
  kubectl annotate networkpolicy "${LITELLM_POLICY}" -n "${NAMESPACE}" \
    "meta.helm.sh/release-name=${HELM_RELEASE}" "meta.helm.sh/release-namespace=${NAMESPACE}" --overwrite
}

# Also called from the EXIT trap, which can fire before the variables above
# are declared, hence the defaults. Clears the flag so a restart done here
# is not repeated by the trap.
restart_operator_after_handoff() {
  [ "${HANDOFF_DONE:-false}" = "true" ] || return 0
  local replicas="${OPERATOR_REPLICAS_BEFORE_HANDOFF:-1}"
  echo "==> Scaling ${OPERATOR_DEPLOYMENT} back to ${replicas} after the handoff."
  kubectl scale deployment "${OPERATOR_DEPLOYMENT}" -n "${NAMESPACE}" --replicas="${replicas}"
  # Cleared only once the scale has succeeded, so a failed scale leaves the
  # trap armed to try again.
  HANDOFF_DONE="false"
  kubectl rollout status "deployment/${OPERATOR_DEPLOYMENT}" -n "${NAMESPACE}" --timeout="${OPERATOR_SCALE_TIMEOUT_SECONDS}s"
}

# Every first-party image in both Deployments carries the tag, the operator
# reports the PlatformAgent Ready, and the gateway rollout is complete.
assert_running() {
  local tag="$1"
  AGENT_IMAGE_CONFIRM_TIMEOUT="${IMAGE_CONFIRM_TIMEOUT_SECONDS}" \
    "${CONFIRM_IMAGE_SCRIPT}" "${NAMESPACE}" "${GATEWAY_DEPLOYMENT}" "${tag}"
  AGENT_IMAGE_CONFIRM_TIMEOUT="${IMAGE_CONFIRM_TIMEOUT_SECONDS}" \
    "${CONFIRM_IMAGE_SCRIPT}" "${NAMESPACE}" "${OPERATOR_DEPLOYMENT}" "${tag}"
  kubectl rollout status "deployment/${GATEWAY_DEPLOYMENT}" -n "${NAMESPACE}" --timeout="${READY_TIMEOUT_SECONDS}s"

  local deadline=$((SECONDS + READY_TIMEOUT_SECONDS)) ready=""
  while [ "${SECONDS}" -lt "${deadline}" ]; do
    ready="$(ready_condition)"
    [ "${ready}" = "True" ] && break
    sleep "${READY_POLL_SECONDS}"
  done
  if [ "${ready}" != "True" ]; then
    echo "::error title=PlatformAgent not Ready::${PLATFORM_AGENT_RESOURCE} in ${NAMESPACE} reports Ready='${ready:-<unset>}' ${READY_TIMEOUT_SECONDS}s after the rollout completed."
    return 1
  fi
  echo "✅ ${GATEWAY_DEPLOYMENT} and ${OPERATOR_DEPLOYMENT} run :${tag}; ${PLATFORM_AGENT_RESOURCE} is Ready."
}

# ---------------------------------------------------------------------------
# Rollback: N → N-1, the runbook's two commands from N-1's checkout.
# ---------------------------------------------------------------------------
CURRENT_STEP="before"
echo "==> State before the rollback:"
snapshot "00-before"
before_agent="$(agent_image)"
before_operator="$(operator_image)"
record "before" "recorded" "agent :${before_agent##*:}, operator :${before_operator##*:}"

CURRENT_STEP="checkout ${ROLLBACK_TAG}"
checkout_at "${ROLLBACK_TAG}" "${ROLLBACK_CHECKOUT}"
check_images_exist "${ROLLBACK_TAG}" "${ROLLBACK_CHECKOUT}"
record "checkout ${ROLLBACK_TAG}" "✅" "${ROLLBACK_COMMIT:0:7}"

CURRENT_STEP="dry run ${ROLLBACK_TAG}"
(cd "${ROLLBACK_CHECKOUT}" && ./upgrade.sh --non-interactive --dry-run --upgrade-mode=operator --image-tag "${ROLLBACK_TAG}")
record "dry run ${ROLLBACK_TAG}" "✅" "source check and configuration"

CURRENT_STEP="handoff ${LITELLM_POLICY}"
handoff_litellm_policy_if_needed "${ROLLBACK_TAG}"
if [ "${HANDOFF_DONE}" = "true" ]; then
  record "handoff ${LITELLM_POLICY}" "✅" "relabelled for Helm before the operator step"
else
  record "handoff ${LITELLM_POLICY}" "✅" "not needed"
fi

CURRENT_STEP="rollback operator ${ROLLBACK_TAG}"
run_upgrade "${ROLLBACK_CHECKOUT}" "${ROLLBACK_TAG}" operator
restart_operator_after_handoff
record "rollback operator" "✅" "helm revision $(helm history "${HELM_RELEASE}" -n "${NAMESPACE}" -o json 2>/dev/null | jq -r '.[-1].revision // "?"')"

CURRENT_STEP="rollback harness ${ROLLBACK_TAG}"
run_upgrade "${ROLLBACK_CHECKOUT}" "${ROLLBACK_TAG}" harness
record "rollback harness" "✅" "helm revision $(helm history "${HELM_RELEASE}" -n "${NAMESPACE}" -o json 2>/dev/null | jq -r '.[-1].revision // "?"')"

CURRENT_STEP="check ${ROLLBACK_TAG} is running"
assert_running "${ROLLBACK_TAG}"
snapshot "01-after-rollback" >/dev/null
record "check ${ROLLBACK_TAG}" "✅" "both images :${ROLLBACK_TAG}, Ready=True"

# ---------------------------------------------------------------------------
# Roll forward: N-1 → N, the same pair from the candidate's checkout.
# ---------------------------------------------------------------------------
if [ "${ROLL_FORWARD}" = "true" ]; then
  # After a handoff the GA's release owns litellm-policy, and the candidate's
  # chart does not render it, so the roll-forward's helm upgrade would prune
  # it and LiteLLM would run unselected until the candidate's operator
  # recreates it. The chart README's answer for that upgrade is the keep
  # annotation: Helm leaves the object, the operator adopts it.
  if [ "${LITELLM_POLICY_HANDED_OFF}" = "true" ]; then
    CURRENT_STEP="keep ${LITELLM_POLICY} across the roll-forward"
    kubectl annotate networkpolicy "${LITELLM_POLICY}" -n "${NAMESPACE}" "${HELM_KEEP_ANNOTATION}" --overwrite
    record "keep ${LITELLM_POLICY}" "✅" "helm.sh/resource-policy=keep, so the roll-forward does not prune it"
  fi

  CURRENT_STEP="checkout candidate"
  checkout_at "${CANDIDATE_SHA}" "${CANDIDATE_CHECKOUT}"
  check_images_exist "${CANDIDATE_SHA}" "${CANDIDATE_CHECKOUT}"
  record "checkout candidate" "✅" "${CANDIDATE_SHA:0:7}"

  CURRENT_STEP="roll forward operator"
  run_upgrade "${CANDIDATE_CHECKOUT}" "${CANDIDATE_SHA}" operator
  record "roll forward operator" "✅" ""

  CURRENT_STEP="roll forward harness"
  run_upgrade "${CANDIDATE_CHECKOUT}" "${CANDIDATE_SHA}" harness
  record "roll forward harness" "✅" ""

  CURRENT_STEP="check candidate is running"
  assert_running "${CANDIDATE_SHA}"
  snapshot "02-after-roll-forward" >/dev/null
  record "check candidate" "✅" "both images :${CANDIDATE_SHA:0:7}, Ready=True"
fi

CURRENT_STEP=""
echo "✅ Rollback leg passed."
