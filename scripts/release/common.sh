#!/usr/bin/env bash
# Common helper functions for Release Candidate CI/CD automation scripts.
set -euo pipefail

# Configures Git bot user identity for automated tagging
setup_git_bot_user() {
  git config user.name "github-actions[bot]"
  git config user.email "github-actions[bot]@users.noreply.github.com"
}

# Ensures a Git tag exists for a given commit SHA idempotently and pushes to origin.
# Arguments: $1 = rc_tag, $2 = commit_sha, $3 = tag_message
ensure_git_tag() {
  local rc_tag="${1:-${RC_TAG:-}}"
  local commit_sha="${2:-${COMMIT_SHA:-}}"
  local tag_message="${3:-Release Candidate ${rc_tag}}"

  if [ -z "${rc_tag}" ] || [ -z "${commit_sha}" ]; then
    echo "❌ ERROR: RC_TAG and COMMIT_SHA are required for ensure_git_tag." >&2
    return 1
  fi

  # Fetch remote tags to ensure local view is updated
  if ! git fetch origin --tags >/dev/null 2>&1; then
    echo "⚠️ Warning: Could not fetch tags from origin repository." >&2
  fi

  # Check if tag already exists in Git
  if git rev-parse "${rc_tag}" >/dev/null 2>&1; then
    local existing_sha
    existing_sha=$(git rev-parse "${rc_tag}^{commit}")
    if [ "${existing_sha}" = "${commit_sha}" ]; then
      echo "✅ Git tag '${rc_tag}' already exists and points to target commit ${commit_sha}. Idempotent skip."
      return 0
    else
      echo "❌ ERROR: Tag '${rc_tag}' already exists but points to commit ${existing_sha}, not target SHA ${commit_sha}!" >&2
      return 1
    fi
  fi

  setup_git_bot_user
  git tag -a "${rc_tag}" "${commit_sha}" -m "${tag_message}"

  local target_repo
  if [ -n "${GH_ORG:-}" ] && [ -n "${GH_REPO:-}" ]; then
    target_repo="${GH_ORG}/${GH_REPO}"
  else
    target_repo="${GITHUB_REPOSITORY:-}"
  fi

  local push_err
  if push_err=$(git push "https://github.com/${target_repo}.git" "${rc_tag}" 2>&1); then
    echo "✅ Git tag '${rc_tag}' successfully pushed to remote repository (${target_repo})!"
  else
    echo "❌ ERROR: Could not push git tag '${rc_tag}' to remote repository (${target_repo}): ${push_err}" >&2
    return 1
  fi
}
