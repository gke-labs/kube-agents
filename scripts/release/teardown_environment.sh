#!/usr/bin/env bash
# Destroys an ephemeral environment after a pipeline run that passed end to end.
#
# Both the RC pipeline and the nightly pipeline install a full GKE cluster per
# run and would otherwise leave it idling between runs. This runs as the last
# step of each install (the RC pipeline installs twice, today then next) so the
# cluster exists only for the length of a run. Which environment it
# destroys is whatever GCP_PROJECT_ID / GCP_REGION / GKE_CLUSTER_NAME name —
# nothing here is RC-specific.
#
# On the RC pipeline it is reached ONLY when every earlier step succeeded —
# the job's `needs` carry the implicit success() that gives that guarantee —
# and a run that failed anywhere leaves its environment standing on purpose,
# so the failure can be examined on the live cluster. The staging promotion
# pipeline keeps that rule for a failed matrix and adds one way in: a deploy
# that did not succeed, which leaves a half-built environment with nothing to
# examine beyond the deploy log, is torn down in the same run.
#
# What clears a standing environment afterwards: on the RC the next scheduled
# run's pre-install teardown in provision_environment.sh removes it within
# three hours; on the nightly, the next daily promotion run's does, or
# uninstall.sh by hand sooner.
set -euo pipefail

export CLOUDSDK_CORE_DISABLE_PROMPTS="${CLOUDSDK_CORE_DISABLE_PROMPTS:-1}"

# shellcheck source=scripts/release/teardown_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/teardown_common.sh"

# Before the temp file, so a missing coordinate aborts without leaving one
# behind — this script deliberately carries no EXIT trap to clean it up. A trap
# that ends on a successful command hands ITS status to the shell, which would
# turn a `set -u` abort on a missing input into a green step.
teardown_require_inputs
TEARDOWN_LOG="$(mktemp)"

echo "==> Tearing down the environment (${TEARDOWN_TARGET}) via canonical uninstall.sh..."
TEARDOWN_STATUS=0
teardown_run "${TEARDOWN_LOG}" || TEARDOWN_STATUS=$?

case "${TEARDOWN_STATUS}" in
  0)
    echo "==> Teardown complete (uninstall.sh exit 0); the cluster is gone."
    ;;
  3)
    # No Terraform state to destroy. Unexpected here, because the deploy step
    # installed against this same target minutes ago — but it means there is no
    # environment left running, which is what this job exists to guarantee, so
    # it is not a failure.
    echo "==> Nothing to tear down: no Terraform state for '${GKE_CLUSTER_NAME}' (uninstall.sh exit 3)."
    ;;
  *)
    # Always fatal, with no TEARDOWN_STRICT escape. That variable governs
    # whether provision_environment.sh will install ON TOP of an environment
    # it failed to remove, which is a question about correctness of the next
    # install. Here the environment is simply still running and still being
    # billed, and nothing later in the pipeline will remove it, so the only
    # useful outcome is a red job somebody looks at.
    UNINSTALL_BY_HAND="./uninstall.sh --non-interactive -y --gcp-project-id=${GCP_PROJECT_ID} --gcp-region=${GCP_REGION} --gke-cluster-name=${GKE_CLUSTER_NAME}"
    if [ -n "${NAMESPACE:-}" ]; then
      UNINSTALL_BY_HAND+=" --agent-namespace=${NAMESPACE}"
    fi
    teardown_report_failure \
      "${TEARDOWN_STATUS}" "${TEARDOWN_LOG}" \
      "uninstall.sh exited ${TEARDOWN_STATUS}; the environment is STILL RUNNING and no later step will remove it." \
      "⚠️ Environment teardown failed — the environment is still running" \
      "The run reached its teardown, but \`uninstall.sh\` did not remove its environment." \
      "The GKE cluster, its node pools and the rest of the project's resources are" \
      "still there and still billing." \
      "" \
      "Remove it by hand — one line, so it can be copied out of here whole:" \
      "" \
      "\`${UNINSTALL_BY_HAND}\`" \
      "" \
      "The alternative is the next scheduled run's pre-install teardown -- three" \
      "hours away on the RC pipeline, the next day on the nightly -- which will fail" \
      "the same way if the cause is not fixed."
    rm -f "${TEARDOWN_LOG}"
    exit "${TEARDOWN_STATUS}"
    ;;
esac

rm -f "${TEARDOWN_LOG}"
