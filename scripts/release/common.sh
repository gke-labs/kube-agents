#!/usr/bin/env bash
# Common helper functions for Release Candidate CI/CD automation scripts.
set -euo pipefail

export REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# gke_dns_endpoint_flag, so release automation reaches a cluster over the same
# endpoint the installer would.
# shellcheck source=k8s-operator/scripts/gke_dns_endpoint.sh
source "${REPO_ROOT}/k8s-operator/scripts/gke_dns_endpoint.sh"

# Centralized definition of required container images and registry defaults
export DEFAULT_REGISTRY_PREFIX="ghcr.io/gke-labs/kube-agents"
export DEFAULT_RELEASE_REPO="gke-labs/kube-agents"

# Declarative registry of all 4 required container images
export REQUIRED_RELEASE_IMAGES=(
  "k8s-operator"
  "platform-agent"
  "credential-proxy"
  "replay-proxy"
)

# ─── Boolean Parsing ──────────────────────────────────────────────────────────
# Interpret a value as a boolean toggle. Returns 0 (success) for common
# affirmative spellings and 1 otherwise. Matching is case-insensitive and
# surrounding whitespace is ignored, so all of the following are truthy:
#   true, yes, y, 1, on  (in any letter case, e.g. "True", "YES", "On")
# Everything else — including false, no, n, 0, off, and empty/unset — is falsy.
is_truthy() {
  local val="${1:-}"
  val="${val//[[:space:]]/}"
  case "$val" in
    [Tt][Rr][Uu][Ee] | [Yy][Ee][Ss] | [Yy] | 1 | [Oo][Nn]) return 0 ;;
    *) return 1 ;;
  esac
}

is_ci_pipeline() {
  is_truthy "${CI:-}"
}

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

# Finds an existing rc_* tag for a commit SHA (excluding *_validated tags)
get_existing_rc_tag() {
  local sha="$1"
  git tag --points-at "${sha}" "rc_*" 2>/dev/null | grep -v '_validated$' | head -n 1 || echo ""
}

# Checks if a commit SHA has already been attempted in a previous RC run (rc_* tag exists)
is_commit_already_attempted() {
  local sha="$1"
  local rc_tag
  rc_tag=$(get_existing_rc_tag "${sha}")
  [ -n "${rc_tag}" ]
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

  local is_shallow
  is_shallow="$(git rev-parse --is-shallow-repository 2>/dev/null || echo "false")"
  local depth_arg=()
  if [ "${is_shallow}" = "true" ]; then
    depth_arg=("--depth=30")
  fi

  local fetch_ok="false"
  if [ -n "${target_repo}" ]; then
    # bash 3.2 compatibility: guard empty array expansion under set -u
    if git fetch "https://github.com/${target_repo}.git" main --tags ${depth_arg[@]+"${depth_arg[@]}"} >/dev/null 2>&1; then
      fetch_ok="true"
    else
      echo "⚠️ Warning: Failed to fetch from target_repo (${target_repo}), falling back to origin..." >&2
    fi
  fi

  if [ "${fetch_ok}" != "true" ]; then
    # bash 3.2 compatibility: guard empty array expansion under set -u
    if git fetch origin main --tags ${depth_arg[@]+"${depth_arg[@]}"} >/dev/null 2>&1; then
      fetch_ok="true"
    else
      echo "⚠️ Warning: Failed to fetch from origin remote, checking available local refs..." >&2
    fi
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
  if is_ci_pipeline; then
    git config user.name "github-actions[bot]"
    git config user.email "github-actions[bot]@users.noreply.github.com"
  fi
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
    git fetch "https://github.com/${target_repo}.git" --tags >/dev/null 2>&1 || git fetch origin --tags >/dev/null 2>&1 || true
  else
    git fetch origin --tags >/dev/null 2>&1 || true
  fi

  # Canonicalize commit SHA to full 40-character hash before comparison
  local target_full_sha
  target_full_sha="$(git rev-parse --verify "${commit_sha}^{commit}" 2>/dev/null || echo "${commit_sha}")"

  # Check if tag already exists in Git
  if git rev-parse "${rc_tag}" >/dev/null 2>&1; then
    local existing_sha
    existing_sha=$(git rev-parse "${rc_tag}^{commit}")
    if [ "${existing_sha}" = "${target_full_sha}" ]; then
      echo "✅ Git tag '${rc_tag}' already exists and points to target commit ${target_full_sha}. Idempotent skip."
      return 0
    else
      echo "❌ ERROR: Tag '${rc_tag}' already exists but points to commit ${existing_sha}, not target SHA ${target_full_sha}!" >&2
      return 1
    fi
  fi

  setup_git_bot_user
  git tag -a "${rc_tag}" "${target_full_sha}" -m "${tag_message}"

  # Safety Guard: Remote push executes exclusively inside CI
  if ! is_ci_pipeline; then
    echo "⚠️ [Local Execution] Dry-run: Git tag '${rc_tag}' created locally. Remote push skipped (runs only in CI)."
    return 0
  fi

  local push_err
  if push_err=$(git push "https://github.com/${target_repo}.git" "${rc_tag}" 2>&1); then
    echo "✅ Git tag '${rc_tag}' successfully pushed to remote repository (${target_repo})!"
  else
    echo "❌ ERROR: Could not push git tag '${rc_tag}' to remote repository (${target_repo}): ${push_err}" >&2
    return 1
  fi
}

# Clean Promotion: Tags verified container images in GHCR without rebuilding
promote_release_images() {
  local commit_sha="${1:-}"
  local target_tag="${2:-}"

  if [ -z "${commit_sha}" ] || [ -z "${target_tag}" ]; then
    echo "❌ ERROR: commit_sha and target_tag are required for promote_release_images." >&2
    return 1
  fi

  if [[ ! "${target_tag}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "❌ ERROR: Target release tag '${target_tag}' is not a valid pure numeric SemVer (e.g. 0.1.0, 0.2.0)." >&2
    return 1
  fi

  local registry_prefix
  registry_prefix="$(get_registry_prefix)"

  echo "🚀 Promoting verified container images (${commit_sha:0:7}) -> (${target_tag})..."

  for img in "${REQUIRED_RELEASE_IMAGES[@]}"; do
    local source_image="${registry_prefix}/${img}:${commit_sha}"
    local target_image="${registry_prefix}/${img}:${target_tag}"
    echo "  • Promoting ${img}..."

    if ! command -v docker >/dev/null 2>&1; then
      echo "❌ ERROR: 'docker buildx' CLI is required for image promotion!" >&2
      return 1
    fi

    # Safety Guard: Check if target tag already exists in registry to prevent accidental tag overwriting
    if docker manifest inspect "${target_image}" >/dev/null 2>&1; then
      echo "    ℹ️ Target image '${target_image}' already exists in registry. Skipping duplicate promotion."
      continue
    fi

    docker buildx imagetools create --tag "${target_image}" "${source_image}"
    echo "    ✅ Promoted ${img} to ${target_tag}"
  done
}
