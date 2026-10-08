#!/usr/bin/env bash
# Verifies that an automated deployment to a long-lived environment (autopush,
# autopush-next, staging) applied successfully and was not deferred or dropped
# due to lease contention.
# Used by .github/workflows/autopush-deploy.yml and .github/workflows/staging-deploy.yml.
#
# ALLOW_SKIPPED=true also accepts `skipped`, which reconcile-environment.yml
# reports only for a caller that passed skip_unconfigured: the environment is
# not provisioned yet. Unset, `skipped` fails like any unexpected result.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

env_target="${TARGET_ENVIRONMENT:-${1:-}}"
if [ -z "${env_target}" ]; then
  echo "❌ ERROR: Target environment (autopush, autopush-next or staging) must be specified via TARGET_ENVIRONMENT or as first argument." >&2
  exit 1
fi

case "${env_target}" in
  autopush|autopush-next|staging) ;;
  *)
    echo "❌ ERROR: Invalid target environment: '${env_target}'. Must be 'autopush', 'autopush-next' or 'staging'." >&2
    exit 1
    ;;
esac

deploy_result="${DEPLOY_RESULT:-${2:-}}"
if [ -z "${deploy_result}" ]; then
  echo "❌ ERROR: DEPLOY_RESULT is required via environment variable or second argument." >&2
  exit 1
fi

echo "🔍 Verifying deployment outcome for '${env_target}' (result: '${deploy_result}')..."

case "${deploy_result}" in
  applied)
    echo "✅ ${env_target} deployment completed successfully (applied)."
    ;;
  deferred)
    echo "::error title=Deployment deferred::${env_target} deployment was deferred because the live-test lease was held. Deferrals are not permitted for automated release deployments." >&2
    echo "❌ ERROR: ${env_target} deployment deferred due to held live-test lease. Refusing silent drop." >&2
    exit 1
    ;;
  skipped)
    if [ "${ALLOW_SKIPPED:-}" != "true" ]; then
      echo "::error title=Deployment skipped::${env_target} deployment was skipped, and this caller does not allow a skip." >&2
      echo "❌ ERROR: ${env_target} deployment skipped without ALLOW_SKIPPED=true." >&2
      exit 1
    fi
    echo "::notice title=Deployment skipped::${env_target} is not provisioned yet (no GitHub environment, or no GCP_PROJECT_ID on it); nothing was deployed."
    echo "⏭️ ${env_target} deployment skipped: environment not provisioned."
    ;;
  failed)
    echo "::error title=Deployment failed::${env_target} deployment failed." >&2
    echo "❌ ERROR: ${env_target} deployment failed." >&2
    exit 1
    ;;
  *)
    echo "::error title=Unexpected deployment result::${env_target} deployment returned unexpected result: '${deploy_result}'." >&2
    echo "❌ ERROR: Unexpected deployment result '${deploy_result}' for ${env_target}." >&2
    exit 1
    ;;
esac
