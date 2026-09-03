#!/usr/bin/env bash
# Reconciles a long-lived environment against the composition in this checkout,
# or reports what a reconcile would change.
#
# `rc` and `nightly` are destroyed and rebuilt every run, so they always run
# today's `terraform/examples/full-install`. `autopush` and `staging` are not:
# they receive image tags and nothing else, so every infrastructure change that
# lands on main — IAM, Pub/Sub, node pools, and the chart values the composition
# renders — was invisible there until somebody re-applied by hand. #1117 found
# both of them a month behind while reporting themselves as "main".
#
# This is the in-place half of the answer (#1117's Option A). The other half,
# destroying and rebuilding from nothing, is deploy-environment.yml, which stays
# a dispatch-only button because it takes the cluster with it.
#
# Usage:
#   RECONCILE_MODE=plan|apply reconcile_environment.sh
#
# Inputs, all from the environment because the calling workflow is what resolves
# `vars.*` and `secrets.*`:
#
#   RECONCILE_MODE       plan (read-only) or apply
#   GITHUB_ENVIRONMENT   the environment's name, for messages and the lease
#   IMAGE_TAG            optional. Omitted, a plan reads the tag the install is
#                        already running and an apply keeps it where it is.
#   LEASE_POLICY         defer (default) | fail | ignore — what an apply does
#                        when somebody is live-testing against this install
#   plus every install setting render_install_env.sh maps.
set -euo pipefail

# How long to wait for an in-flight redeploy of this environment, and how often
# to look. A `terraform apply` running concurrently with the `helm upgrade` a
# redeploy performs contends for the same release, so the reconcile waits;
# past the deadline it gives up and runs again tomorrow rather than blocking a
# nightly on a stuck deploy.
readonly REDEPLOY_WAIT_SECONDS=900
readonly REDEPLOY_POLL_SECONDS=30
# `gh run list` page size. Only the in-flight states are counted, and a
# redeploy workflow with more than this many runs already queued is a problem
# no wait will fix.
readonly REDEPLOY_RUN_QUERY_LIMIT=20
# Long enough to cover a full apply, short enough that a crashed run does not
# lock the environment out for a working day.
readonly LEASE_TTL_MINUTES=90

MODE="${RECONCILE_MODE:-plan}"
ENV_NAME="${GITHUB_ENVIRONMENT:-unknown}"
LEASE_POLICY="${LEASE_POLICY:-defer}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

case "$MODE" in
  plan|apply) ;;
  *) echo "RECONCILE_MODE must be plan or apply, not '${MODE}'." >&2; exit 2 ;;
esac

summary() {
  [ -n "${GITHUB_STEP_SUMMARY:-}" ] || return 0
  printf '%s\n' "$*" >>"${GITHUB_STEP_SUMMARY}"
}

output() {
  [ -n "${GITHUB_OUTPUT:-}" ] || return 0
  printf '%s=%s\n' "$1" "$2" >>"${GITHUB_OUTPUT}"
}

# ---------------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------------
# An inbound KUBE_AGENTS_INSTALL_ENV wins. The renderer truncates whatever it
# writes to, and on a workstation the default path is a hand-authored file
# holding credentials that nothing backs up -- so the variable whose whole job
# is to point the installer somewhere else is honoured here rather than
# overwritten.
#
# A GitHub runner sets it nowhere and gets the repository root, which is where
# every front door looks for install.env and where scripts/live_test_lease.py
# discovers which install this checkout is pointed at. .gitignore already
# excludes it.
INSTALL_ENV="${KUBE_AGENTS_INSTALL_ENV:-${REPO_ROOT}/install.env}"
export KUBE_AGENTS_INSTALL_ENV="${INSTALL_ENV}"

echo "==> Rendering the install configuration for '${ENV_NAME}'."
# --strict: this environment already exists, so a setting that arrives empty
# would apply a default over a running install and plan the destruction of the
# difference. render_install_env.sh names every missing one at once and stops.
"${REPO_ROOT}/scripts/release/render_install_env.sh" "${INSTALL_ENV}" --strict

# The coordinates the steps below need, read back from the file that was just
# rendered so there is one answer rather than two.
# shellcheck disable=SC1090
set -a && . "${INSTALL_ENV}" && set +a

# ---------------------------------------------------------------------------
# 2. kubectl credentials
# ---------------------------------------------------------------------------
# Before the lease, not after. The lease is a ConfigMap read with `kubectl
# --context gke_<project>_<region>_<cluster>`, so without credentials that
# context does not exist, the read raises, and `acquire` exits non-zero --
# indistinguishable, to the caller, from somebody else holding it. A scheduled
# reconcile would then defer on every single run, exit 0, and report green
# having applied nothing: the same "green means deployed" failure this whole
# change exists to end.
#
# upgrade.sh fetches credentials too, but it does so long after the lease has
# been taken.
echo "==> Connecting kubectl to '${CLUSTER_NAME}' (${REGION})."
GKE_DNS_ENDPOINT_FLAG=""
if [ -f "${REPO_ROOT}/scripts/installer/gke_dns_endpoint.sh" ]; then
  # shellcheck source=scripts/installer/gke_dns_endpoint.sh
  # shellcheck disable=SC1091
  . "${REPO_ROOT}/scripts/installer/gke_dns_endpoint.sh"
  gke_dns_endpoint_flag "${CLUSTER_NAME}" "${REGION}" "${PROJECT_ID}"
fi
# Unquoted on purpose: empty must contribute no argument at all.
# shellcheck disable=SC2086
gcloud container clusters get-credentials "${CLUSTER_NAME}" \
  --location="${REGION}" --project="${PROJECT_ID}" $GKE_DNS_ENDPOINT_FLAG

# ---------------------------------------------------------------------------
# 3. Wait out any image redeploy already in flight
# ---------------------------------------------------------------------------
# The redeploy workflows run `helm upgrade` on the `kube-agents` release, and
# the composition's helm_release.kube_agents owns that same release. Both at
# once is either a failed apply or a lost deploy, depending on which one gets
# the release lock. They are not scheduled against each other — autopush's
# redeploys start from a GHCR publish, which is every push to main — so the
# overlap is real and this waits it out rather than racing it.
#
# Bounded, and a timeout is a deferral rather than a failure: the reconcile runs
# again tomorrow, and blocking a nightly on a stuck deploy helps nobody.
await_redeploys() {
  command -v gh >/dev/null 2>&1 || { echo "==> gh not available; skipping the redeploy check."; return 0; }
  [ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ] || { echo "==> No token for the redeploy check; skipping."; return 0; }

  local deadline=$((SECONDS + REDEPLOY_WAIT_SECONDS)) running
  while [ "$SECONDS" -lt "$deadline" ]; do
    running=""
    for component in agent controller integrations; do
      local wf="${ENV_NAME}-redeploy-${component}.yml"
      # `gh run list` on a workflow this repository does not have exits
      # non-zero; an environment with no redeploy workflows simply has nothing
      # to wait for.
      # queued and waiting count, not just in_progress. Each redeploy sits in
      # its own concurrency group, so a second one queued behind the first is
      # invisible to an in_progress-only query -- and autopush's redeploys
      # start on every push to main, so one dequeuing into a `helm upgrade`
      # halfway through the apply is the collision this loop exists to avoid.
      # `--status` takes one value, so the filtering is done in jq instead.
      local n
      n="$(gh run list --repo "${GITHUB_REPOSITORY:-gke-labs/kube-agents}" \
        --workflow "$wf" --limit "$REDEPLOY_RUN_QUERY_LIMIT" --json status \
        --jq '[.[] | select(.status == "in_progress" or .status == "queued" or .status == "waiting")] | length' \
        2>/dev/null || echo 0)"
      [ "$n" = "0" ] || running="${running} ${wf}(${n})"
    done
    [ -n "$running" ] || return 0
    echo "==> Waiting for in-flight redeploys:${running}"
    sleep "$REDEPLOY_POLL_SECONDS"
  done

  echo "::warning title=Redeploy still running::A redeploy of '${ENV_NAME}' was still in flight after $((REDEPLOY_WAIT_SECONDS / 60)) minutes; skipping this reconcile rather than running a terraform apply concurrently with a helm upgrade on the same release."
  return 1
}

if [ "$MODE" = "apply" ]; then
  if ! await_redeploys; then
    summary "### Reconcile deferred — \`${ENV_NAME}\`"
    summary ""
    summary "An image redeploy was still running. Nothing was applied."
    output "result" "deferred"
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
# 4. The live-test lease
# ---------------------------------------------------------------------------
# AGENTS.md requires every pull request to be validated against a running
# install, and autopush is that install for most of this repository's agents.
# An unattended `terraform apply` landing in the middle of somebody's live
# validation rewrites what they are in the process of observing, and they have
# no way to tell that is what happened -- their evidence simply stops matching
# the cluster.
#
# So the reconcile takes the same lease an agent would. `acquire` fails when
# somebody else holds it, which is the check and the claim in one step: a
# separate "is it free?" read would leave a window between the answer and the
# apply. A plan takes nothing, because it changes nothing.
LEASE_HELD="false"
release_lease() {
  [ "$LEASE_HELD" = "true" ] || return 0
  python3 "${REPO_ROOT}/scripts/live_test_lease.py" release >/dev/null 2>&1 || true
  LEASE_HELD="false"
}
trap release_lease EXIT

if [ "$MODE" = "apply" ] && [ "$LEASE_POLICY" != "ignore" ]; then
  # A run id rather than a pid: the lease keys holder identity on the session,
  # and every step of a workflow run is a different shell.
  export KUBE_AGENTS_LEASE_SESSION="gha-${GITHUB_RUN_ID:-manual}"
  if python3 "${REPO_ROOT}/scripts/live_test_lease.py" acquire \
    --note "scheduled reconcile of ${ENV_NAME}" --ttl "$LEASE_TTL_MINUTES"; then
    LEASE_HELD="true"
  else
    holder="$(python3 "${REPO_ROOT}/scripts/live_test_lease.py" status --json 2>/dev/null || echo '[]')"
    # A cluster that could not be asked has not answered "no". `acquire` exits
    # non-zero for both, so deferring on the strength of that alone would turn
    # every kind of breakage -- a bad kubeconfig, an API server mid-upgrade, a
    # renamed namespace -- into a green run that applied nothing. Only a lease
    # somebody genuinely holds is a deferral; anything else is a failure.
    if ! printf '%s' "${holder}" | grep -q '"state": "held"'; then
      echo "::error title=Live-test lease could not be read::Could not determine whether '${ENV_NAME}' is in use, so this reconcile stopped rather than assuming either answer. ${holder}"
      summary "### Reconcile failed — \`${ENV_NAME}\`"
      summary ""
      summary "The live-test lease could not be read, so nothing was applied."
      output "result" "failed"
      exit 1
    fi
    if [ "$LEASE_POLICY" = "fail" ]; then
      echo "::error title=Live-test lease is held::Somebody is live-testing against '${ENV_NAME}'. Refusing to apply. ${holder}"
      exit 1
    fi
    echo "::warning title=Live-test lease is held::Somebody is live-testing against '${ENV_NAME}'; skipping this reconcile. It will run again on the next schedule. ${holder}"
    summary "### Reconcile deferred — \`${ENV_NAME}\`"
    summary ""
    summary "The live-test lease was held, so nothing was applied."
    summary ""
    summary '```json'
    summary "${holder}"
    summary '```'
    output "result" "deferred"
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
# 5. Refuse to apply a configuration that would un-provision the minter
# ---------------------------------------------------------------------------
# render_install_env.sh --strict catches a setting that arrives empty. This
# catches the one that does not go through a variable at all: the minter is
# enabled only when its KMS signing key has an ENABLED version OR a readable
# App private key PEM is at hand, and an unattended reconcile has no PEM. So a
# key that has been rotated, disabled, or scheduled for destruction silently
# flips `enable_github_minter` to false, and the apply destroys the minter with
# a warning in a log nobody reads.
#
# Same destroy-by-omission class as the strict list, so it gets the same
# treatment: stop, and say which of the two ways out to take.
if [ "$MODE" = "apply" ] && [ -n "${GITHUB_APP_ID:-}" ]; then
  # The key names and the location rule come from the installer's own homes --
  # install.defaults.env and derive_kms_location -- rather than being restated
  # here. A second copy of either is how this guard ends up querying a keyring
  # that does not exist, finding no enabled version, and refusing every apply
  # for the environment with a message about a rotated key.
  # installer_common.sh loads install.defaults.env on the way in, so one source
  # brings both DEFAULT_KMS_* and derive_kms_location.
  # shellcheck source=scripts/installer/installer_common.sh
  # shellcheck disable=SC1091
  . "${REPO_ROOT}/scripts/installer/installer_common.sh"
  kms_location="$(derive_kms_location "${REGION}")"
  minter_key_version="$({ gcloud kms keys versions list \
    --key "${KMS_KEY:-$DEFAULT_KMS_KEY}" \
    --keyring "${KMS_KEYRING:-$DEFAULT_KMS_KEYRING}" \
    --location "${kms_location}" --project "${PROJECT_ID}" \
    --filter='state=ENABLED' --format='value(name)' 2>/dev/null || true; } | head -1)"
  if [ -z "${minter_key_version}" ]; then
    echo "::error title=The minter's signing key has no ENABLED version::GITHUB_APP_ID is set on '${ENV_NAME}', but the KMS key the token minter signs with has no enabled version — so this apply would render enable_github_minter = false and DESTROY the minter. Re-import the App key (k8s-operator/config/integrations/github/README.md), or unset the GH_APP_ID secret on this environment to reconcile without a minter."
    summary "### Reconcile refused — \`${ENV_NAME}\`"
    summary ""
    summary "The token minter's KMS signing key has no enabled version, so applying would destroy the minter."
    output "result" "failed"
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# 6. Plan, or apply
# ---------------------------------------------------------------------------
UPGRADE_ARGS=(--non-interactive)
if [ -n "${IMAGE_TAG:-}" ]; then
  UPGRADE_ARGS+=(--image-tag="${IMAGE_TAG}")
elif [ "$MODE" = "apply" ]; then
  # An apply with no tag has to say so. Empty otherwise means "the caller's
  # IMAGE_TAG did not resolve", which upgrade.sh refuses on purpose.
  UPGRADE_ARGS+=(--keep-image-tag)
fi

# Somewhere the calling workflow can read afterwards, so the drift report can
# quote the plan rather than telling a reader to open the job log. Named as an
# output rather than assumed, because a caller running two environments in one
# job would otherwise have them overwrite each other.
PLAN_LOG="${RUNNER_TEMP:-/tmp}/reconcile-${ENV_NAME}.log"
output "plan_log" "${PLAN_LOG}"
status=0

if [ "$MODE" = "plan" ]; then
  echo "==> Planning '${ENV_NAME}' (read-only)."
  # `tee`, so the plan is in the job log and in a file at once. `|| status=` is
  # what keeps `set -e` from killing the script on the exit code this whole
  # path exists to read: with pipefail the pipeline reports upgrade.sh's 2, and
  # PIPESTATUS[0] is where the unambiguous copy of it lives (tee's own 0 is
  # PIPESTATUS[1]).
  "${REPO_ROOT}/upgrade.sh" --plan "${UPGRADE_ARGS[@]}" 2>&1 | tee "${PLAN_LOG}" \
    || status="${PIPESTATUS[0]}"

  case "$status" in
    0)
      summary "### \`${ENV_NAME}\` is in sync"
      summary ""
      summary "A full upgrade would change nothing."
      output "drift" "false"
      ;;
    2)
      summary "### \`${ENV_NAME}\` has drifted from the composition"
      summary ""
      summary "See the plan in the job log."
      output "drift" "true"
      # 2 is this script's report, not its failure. The caller decides what a
      # drifted environment means; here it means the plan succeeded.
      status=0
      ;;
    *)
      summary "### The plan for \`${ENV_NAME}\` failed"
      output "drift" "unknown"
      # `failed`, not `planned`. A caller branching on `result` has to be able
      # to tell a plan that ran from one that broke without also having to read
      # `drift` to find out.
      output "result" "failed"
      ;;
  esac
  [ "$status" = "0" ] || exit "$status"
  output "result" "planned"
else
  echo "==> Reconciling '${ENV_NAME}' in place."
  "${REPO_ROOT}/upgrade.sh" --upgrade-mode=full "${UPGRADE_ARGS[@]}" 2>&1 | tee "${PLAN_LOG}" \
    || status="${PIPESTATUS[0]}"
  if [ "$status" = "0" ]; then
    summary "### Reconciled \`${ENV_NAME}\`"
    summary ""
    summary "The composition in this checkout is now applied to the environment."
    output "result" "applied"
  else
    summary "### Reconciling \`${ENV_NAME}\` failed"
    output "result" "failed"
  fi
fi

release_lease
exit "$status"
