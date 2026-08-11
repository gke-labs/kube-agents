#!/usr/bin/env bash
# Common helper functions for Release Candidate CI/CD automation scripts.
set -euo pipefail

# Centralized definition of required container images and registry defaults
DEFAULT_REGISTRY_PREFIX="ghcr.io/gke-labs/kube-agents"
DEFAULT_RELEASE_REPO="gke-labs/kube-agents"
REQUIRED_RELEASE_IMAGES=("k8s-operator" "platform-agent")

# Resolves target GitHub repository (e.g. gke-labs/kube-agents)
get_target_repo() {
  if [ -n "${GH_ORG:-}" ] && [ -n "${GH_REPO:-}" ]; then
    echo "${GH_ORG}/${GH_REPO}"
  elif [ -n "${GITHUB_REPOSITORY:-}" ]; then
    echo "${GITHUB_REPOSITORY}"
  else
    echo "${DEFAULT_RELEASE_REPO}"
  fi
}

# Resolves registry prefix (e.g. ghcr.io/gke-labs/kube-agents)
get_registry_prefix() {
  if [ -n "${REGISTRY_PREFIX:-}" ]; then
    echo "${REGISTRY_PREFIX}"
  else
    local target_repo
    target_repo="$(get_target_repo)"
    if [ "$target_repo" = "$DEFAULT_RELEASE_REPO" ]; then
      echo "$DEFAULT_REGISTRY_PREFIX"
    else
      local repo_downcased
      repo_downcased="$(echo "$target_repo" | tr '[:upper:]' '[:lower:]')"
      echo "ghcr.io/${repo_downcased}"
    fi
  fi
}

# Checks if all required candidate container images exist in GHCR for a specific commit SHA
check_commit_images_exist() {
  local sha="$1"
  local registry_prefix
  registry_prefix="$(get_registry_prefix)"

  for img in "${REQUIRED_RELEASE_IMAGES[@]}"; do
    local target_img="${registry_prefix}/${img}:${sha}"
    if ! docker manifest inspect "${target_img}" >/dev/null 2>&1; then
      return 1
    fi
  done
  return 0
}

# Checks if a commit SHA has already been validated in a previous RC run (*_validated tag)
is_commit_already_validated() {
  local sha="$1"
  local validated_tags
  validated_tags=$(git tag --points-at "${sha}" "*_validated" 2>/dev/null || echo "")
  [ -n "${validated_tags}" ]
}

# Finds the latest commit on main whose required container images are already built in the registry
find_latest_built_commit() {
  local target_repo
  target_repo="$(get_target_repo)"
  local registry_prefix
  registry_prefix="$(get_registry_prefix)"

  echo "🔍 [Schedule / Auto-resolve] Scanning recent commits on main for prebuilt container images (${registry_prefix})..." >&2

  if [ -n "${target_repo}" ]; then
    git fetch "https://github.com/${target_repo}.git" main +refs/tags/*:refs/tags/* --depth=30 >/dev/null 2>&1 || git fetch origin main +refs/tags/*:refs/tags/* --depth=30 >/dev/null 2>&1 || true
  else
    git fetch origin main +refs/tags/*:refs/tags/* --depth=30 >/dev/null 2>&1 || true
  fi

  local candidate_commits
  candidate_commits=$(git log -n 30 --format="%H" FETCH_HEAD 2>/dev/null || git log -n 30 --format="%H" origin/main 2>/dev/null || git log -n 30 --format="%H" HEAD 2>/dev/null || echo "")

  if [ -z "${candidate_commits}" ]; then
    echo "❌ ERROR: Cannot retrieve commit history from git repository!" >&2
    return 1
  fi

  for sha in $candidate_commits; do
    if check_commit_images_exist "${sha}"; then
      echo "✅ Found latest commit with verified container images: ${sha}" >&2
      echo "$sha"
      return 0
    else
      echo "  ⏳ Images not ready yet in GHCR for commit ${sha:0:7}, checking previous commit..." >&2
    fi
  done

  echo "❌ ERROR: Could not find any commit in the last 30 commits on main with published images in GHCR (${registry_prefix})!" >&2
  return 1
}

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

  local target_repo
  target_repo="$(get_target_repo)"

  # Fetch remote tags to ensure local view is updated
  if [ -n "${target_repo}" ]; then
    git fetch "https://github.com/${target_repo}.git" +refs/tags/*:refs/tags/* >/dev/null 2>&1 || git fetch origin --tags >/dev/null 2>&1 || true
  else
    git fetch origin --tags >/dev/null 2>&1 || true
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

  local push_err
  if push_err=$(git push "https://github.com/${target_repo}.git" "${rc_tag}" 2>&1); then
    echo "✅ Git tag '${rc_tag}' successfully pushed to remote repository (${target_repo})!"
  else
    echo "❌ ERROR: Could not push git tag '${rc_tag}' to remote repository (${target_repo}): ${push_err}" >&2
    return 1
  fi
}
