#!/usr/bin/env bash
# Resolves the candidate commit SHA and deployment parameters for long-lived environments (autopush, staging).
# Used by .github/workflows/autopush-deploy.yml and .github/workflows/staging-deploy.yml.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

env_target="${TARGET_ENVIRONMENT:-${1:-}}"
if [ -z "${env_target}" ]; then
  echo "❌ ERROR: Target environment (autopush or staging) must be specified via TARGET_ENVIRONMENT or as first argument." >&2
  exit 1
fi

case "${env_target}" in
  autopush|staging) ;;
  *)
    echo "❌ ERROR: Invalid target environment: '${env_target}'. Must be 'autopush' or 'staging'." >&2
    exit 1
    ;;
esac

target="${INPUT_TAG:-}"
event_name="${EVENT_NAME:-${GITHUB_EVENT_NAME:-}}"
run_head_sha="${RUN_HEAD_SHA:-}"
github_sha="${GITHUB_SHA:-}"

if [ -z "${target}" ]; then
  if [ "${env_target}" = "autopush" ]; then
    if [ "${event_name}" = "workflow_run" ]; then
      target="${run_head_sha}"
    elif [ -n "${github_sha}" ]; then
      target="${github_sha}"
    fi
  elif [ "${env_target}" = "staging" ]; then
    if [ "${event_name}" = "push" ]; then
      target="${github_sha}"
    else
      command -v git >/dev/null 2>&1 || {
        echo "❌ ERROR: git is required but not installed." >&2
        exit 1
      }
      release_fetch_tags
      target="$(get_latest_staging_tag)"
      if [ -z "${target}" ]; then
        echo "❌ ERROR: No staging tag found and no image_tag provided." >&2
        exit 1
      fi
    fi
  fi
fi

if [ -z "${target}" ]; then
  echo "❌ ERROR: Unable to determine commit SHA or target ref for ${env_target} deploy." >&2
  exit 1
fi

# If target resolves via git (e.g. an annotated tag or valid commit object), peel it cleanly.
if command -v git >/dev/null 2>&1 && git rev-parse --verify --quiet "${target}^{commit}" >/dev/null 2>&1; then
  "${SCRIPT_DIR}/peel_tag_commit.sh" "${target}"
elif [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "commit_sha=${target}" >> "${GITHUB_OUTPUT}"
fi

lease_policy="${INPUT_LEASE_POLICY:-}"
if [ -z "${lease_policy}" ]; then
  # Automated deployment triggers:
  # - staging is driven by a one-shot `staging_*` tag push that does not auto-retry.
  # - autopush is triggered on every GHCR publish from pushes to main; while frequent,
  #   automated release pipelines must fail loudly when lease contention blocks an apply
  #   rather than silently dropping or reporting success on an unapplied candidate.
  # Manual workflow_dispatch invocations can explicitly pass 'defer' or 'ignore' when needed.
  lease_policy="fail"
fi

case "${lease_policy}" in
  defer|fail|ignore) ;;
  *)
    echo "❌ ERROR: Invalid lease_policy: '${lease_policy}'. Must be defer, fail, or ignore." >&2
    exit 1
    ;;
esac

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "lease_policy=${lease_policy}" >> "${GITHUB_OUTPUT}"
fi

echo "==> ${env_target} deploy resolved: target=${target}, lease_policy=${lease_policy}"
