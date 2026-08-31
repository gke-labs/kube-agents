#!/usr/bin/env bash
# Resolves the ref a push event fired on to the commit it points at, and writes
# it to GITHUB_OUTPUT as `commit_sha`.
#
# The staging promotion tags are annotated — common.sh's ensure_git_tag runs
# `git tag -a` — and a push event's github.sha is the new value of the ref, which
# for an annotated tag is the tag object's SHA rather than the commit's. The
# staging redeploys pass that value to `helm upgrade --set …image.tag`, and the
# GHCR images are published under the commit SHA, so the unpeeled value names an
# image that was never pushed: the `--wait` deploy times out on ImagePullBackOff
# and strands the shared kube-agents release in pending-upgrade.
#
# Peeling a lightweight tag or a branch head yields the same SHA back, so a
# caller does not need to know which kind of ref it was pushed.
#
# Usage: peel_tag_commit.sh [sha-or-ref]   (defaults to $GITHUB_SHA)
set -euo pipefail

REF="${1:-${GITHUB_SHA:-}}"

if [ -z "${REF}" ]; then
  echo "❌ ERROR: a ref or SHA is required (GITHUB_SHA is unset)." >&2
  exit 1
fi

if ! COMMIT_SHA="$(git rev-parse --verify --quiet "${REF}^{commit}")"; then
  echo "❌ ERROR: '${REF}' does not resolve to a commit in this checkout." >&2
  exit 1
fi

echo "==> ${REF} points at commit ${COMMIT_SHA}."

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "commit_sha=${COMMIT_SHA}" >>"${GITHUB_OUTPUT}"
fi

echo "${COMMIT_SHA}"
