#!/usr/bin/env bash
# Destroys the RC environment after a pipeline run that passed end to end.
#
# The pipeline installs a full GKE cluster for every release candidate and, on
# the 3-hourly schedule, would otherwise leave it idling between runs. This
# runs as the last step so the cluster exists only for the length of a run.
#
# It is reached ONLY when steps 1-4 all succeeded — the job's `needs` carry the
# implicit success() that gives that guarantee. A run that failed anywhere
# leaves its environment standing on purpose, so the failure can be examined on
# the live cluster; the next run's pre-install teardown in
# provision_rc_environment.sh is what eventually clears it.
set -euo pipefail

export CLOUDSDK_CORE_DISABLE_PROMPTS="${CLOUDSDK_CORE_DISABLE_PROMPTS:-1}"

# shellcheck source=scripts/release/rc_teardown_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/rc_teardown_common.sh"

# Before the temp file, so a missing coordinate aborts without leaving one
# behind — this script deliberately carries no EXIT trap to clean it up. A trap
# that ends on a successful command hands ITS status to the shell, which would
# turn a `set -u` abort on a missing input into a green step.
rc_teardown_require_inputs
TEARDOWN_LOG="$(mktemp)"

echo "==> Tearing down the RC environment (${RC_TEARDOWN_TARGET}) via canonical uninstall.sh..."
TEARDOWN_STATUS=0
rc_teardown_run "${TEARDOWN_LOG}" || TEARDOWN_STATUS=$?

case "${TEARDOWN_STATUS}" in
  0)
    echo "==> Teardown complete (uninstall.sh exit 0); the RC cluster is gone."
    ;;
  3)
    # No Terraform state to destroy. Unexpected here, because step 2 installed
    # against this same target minutes ago — but it means there is no
    # environment left running, which is what this job exists to guarantee, so
    # it is not a failure.
    echo "==> Nothing to tear down: no Terraform state for '${GKE_CLUSTER_NAME}' (uninstall.sh exit 3)."
    ;;
  *)
    # Always fatal, with no RC_TEARDOWN_STRICT escape. That variable governs
    # whether provision_rc_environment.sh will install ON TOP of an environment
    # it failed to remove, which is a question about correctness of the next
    # install. Here the environment is simply still running and still being
    # billed, and nothing later in the pipeline will remove it, so the only
    # useful outcome is a red job somebody looks at.
    rc_teardown_report_failure \
      "${TEARDOWN_STATUS}" "${TEARDOWN_LOG}" \
      "uninstall.sh exited ${TEARDOWN_STATUS}; the RC environment is STILL RUNNING and no later step will remove it." \
      "⚠️ RC teardown failed — the environment is still running" \
      "The release candidate passed, but \`uninstall.sh\` did not remove its environment." \
      "The GKE cluster, its node pools and the rest of the RC project's resources are" \
      "still there and still billing." \
      "" \
      "Remove it by hand — one line, so it can be copied out of here whole:" \
      "" \
      "\`./uninstall.sh --non-interactive -y --project-id=${GCP_PROJECT_ID} --region=${GCP_REGION} --cluster-name=${GKE_CLUSTER_NAME}\`" \
      "" \
      "The alternative is the next run's pre-install teardown, which is up to three hours" \
      "away on the schedule and will fail the same way if the cause is not fixed."
    rm -f "${TEARDOWN_LOG}"
    exit "${TEARDOWN_STATUS}"
    ;;
esac

rm -f "${TEARDOWN_LOG}"
