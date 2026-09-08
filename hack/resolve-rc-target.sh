#!/usr/bin/env bash
# ==============================================================================
# Resolve the release candidate an eval run should be measured against
# ==============================================================================
# Prints the candidate's commit SHA on stdout. Everything else goes to stderr,
# so the caller can do:
#
#   main() {
#     local rc_commit_sha
#     rc_commit_sha="$(hack/resolve-rc-target.sh)"
#     git checkout --detach "${rc_commit_sha}"
#     RC_COMMIT_SHA="${rc_commit_sha}" hack/ci-deploy.sh
#     exit $?
#   }
#   main "$@"
#
# The checkout is the caller's job and not this script's, and not
# hack/ci-deploy.sh's either. .github/workflows/deploy-environment.yml already
# settles this the same way — a second actions/checkout step at the candidate's
# SHA, before anything that reads the tree runs.
#
# The main() wrapper is not decoration. Bash reads a script incrementally as it
# executes, keeping a byte offset into the file, so a script that rewrites its
# own file mid-run can resume at that offset in different content. A checkout
# is usually the benign case — it replaces a file by rename, leaving the
# descriptor bash holds pointed at the intact original — but a tool that
# rewrites in place instead truncates the live inode, and then the offset lands
# in whatever is there now. "Usually", for a failure whose signature is a green
# run with steps missing from it, is not a property to build on. Wrapping the
# body removes the question: bash parses main() into memory in full before
# running any of it, and the exit means control never returns to the file. Copy
# the shape above rather than the three bare statements it wraps.
# hack/ci-eval-rc.sh is the worked example, and tests/test_ci_eval_rc.py pins
# the property by emptying that file mid-run.
#
# Checking out matters because only the images come from the candidate. The
# chart, the CRDs, bench/tasks, and the seeded fleet in bench/tf/fleet all come
# from whatever tree the job happens to be sitting on, so without the checkout
# an "RC eval" grades the candidate's images against main's everything-else.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/../scripts/release/common.sh"

# The candidate tags rc-create-tag.yml writes. The `_validated` marker
# rc-tag-validated.yml adds is a sibling tag on the same commit rather than a
# replacement — rc_2609021231_fdba3a7 and rc_2609021231_fdba3a7_validated both
# exist once the pipeline passes — so the filter below drops markers from the
# list of candidates, and does not drop candidates that have been validated.
#
# The target is the newest candidate either way, and deliberately: an eval
# normally runs before the marker exists, and when it does not, re-measuring the
# newest candidate is right where walking back to an older unvalidated one would
# grade something already superseded. The marker is worth saying out loud, so
# there is a note below, but it is not a reason to pick a different commit.
readonly RC_TAG_GLOB="rc_*"

RC_TAG="${RC_TAG:-}"

if [ -z "${RC_TAG}" ]; then
  release_fetch_tags
  RC_TAG="$(git tag -l --sort=-v:refname "${RC_TAG_GLOB}" 2>/dev/null |
    grep -v '_validated$' | head -n 1 || echo "")"
  if [ -z "${RC_TAG}" ]; then
    echo "❌ ERROR: no ${RC_TAG_GLOB} tag found. Set RC_TAG explicitly, or wait for rc-scheduler.yml to cut a candidate." >&2
    exit 1
  fi
fi

# `refs/tags/<name>` first, so a branch that happens to share a candidate's name
# cannot answer ahead of the tag — the sibling lookups in common.sh are anchored
# the same way. The unqualified form stays as the fallback because RC_TAG is
# also how a caller names a target that is not a tag at all, a raw SHA being the
# usual one.
resolve_rc_commit() {
  git rev-parse --verify "refs/tags/${RC_TAG}^{commit}" 2>/dev/null ||
    git rev-parse --verify "${RC_TAG}^{commit}" 2>/dev/null
}

if ! RC_COMMIT_SHA="$(resolve_rc_commit)"; then
  release_fetch_tags
  if ! RC_COMMIT_SHA="$(resolve_rc_commit)"; then
    echo "❌ ERROR: cannot resolve a commit from release candidate tag '${RC_TAG}'." >&2
    exit 1
  fi
fi

# Through common.sh rather than a local `git tag --points-at`, so this reads the
# marker the same way the RC and promotion gates do — anchored on the
# `rc_*_validated` family, so no neighbouring tag family can answer for it.
if is_rc_candidate_commit_already_validated "${RC_COMMIT_SHA}"; then
  echo "ℹ️ Note: ${RC_TAG} (${RC_COMMIT_SHA:0:7}) already carries a validation marker; measuring it again." >&2
fi

# Fail here rather than 15 minutes into `helm --wait`. An eval run installs
# images it did not build, so "the publish workflow has not finished for this
# commit" is a real and recoverable state — docker-publish-ghcr.yml runs on
# every push to main with no paths filter, and a queued or failed run leaves a
# main commit with no images at all. The candidate is normally chosen because
# its images exist, so this firing means something moved underneath it.
#
# All six of REQUIRED_RELEASE_IMAGES, deliberately, though an eval install
# renders only four of them — both plugins default to enabled=false. The gate
# asks "is this commit published", and that is the release path's question with
# the release path's answer; a shorter list here would be a second definition of
# a published commit, disagreeing with verify_release_eligibility.sh about which
# candidates exist. The cost is that a publish run which drops only a plugin
# image blocks an eval that would not have installed it. Reach for the shorter
# list only after deciding the two definitions should differ.
registry_prefix="$(get_registry_prefix)"
if ! check_commit_images_exist "${RC_COMMIT_SHA}"; then
  echo "❌ ERROR: ${registry_prefix} is missing at least one of the required images at ${RC_COMMIT_SHA:0:7}:" >&2
  for img in "${REQUIRED_RELEASE_IMAGES[@]}"; do
    if registry_image_exists "${registry_prefix}/${img}:${RC_COMMIT_SHA}"; then
      echo "   ✓ ${img}" >&2
    else
      echo "   ✗ ${img}" >&2
    fi
  done
  exit 1
fi

if [ -n "${RC_TARGET_OUTPUT:-}" ]; then
  {
    echo "rc_tag=${RC_TAG}"
    echo "rc_commit_sha=${RC_COMMIT_SHA}"
  } >>"${RC_TARGET_OUTPUT}"
fi

echo "======================================================================" >&2
echo "🏷️ RELEASE CANDIDATE EVAL TARGET" >&2
echo "Release Tag:    ${RC_TAG}" >&2
echo "Commit SHA:     ${RC_COMMIT_SHA}" >&2
echo "Images:         ${registry_prefix}/<image>:${RC_COMMIT_SHA}" >&2
echo "======================================================================" >&2

echo "${RC_COMMIT_SHA}"
