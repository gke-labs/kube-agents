#!/usr/bin/env bash
# Print the revision stamp that deploy/docker/Dockerfile bakes into
# /opt/build-info.json and org.opencontainers.image.revision.
#
# One definition, because three call sites had drifted apart and the drift was
# the bug. The Makefile used `git describe --always --dirty`, which is wrong in
# two separate ways, and the self-improvement runner accepted both silently:
#
#   * `describe --always` yields an abbreviated hash. The runner's stamp check
#     accepts 7 characters and up, so it reports the image as stamped and, in
#     fork or upstream mode, runs `git fetch --depth 1 origin <abbrev>`. GitHub
#     refuses that ("couldn't find remote ref"). The fetch then falls back to
#     the codeload tarball, which does accept abbreviations, so nothing fails
#     loudly -- and nothing announces that the investigation is now reading a
#     tree with no `.git`, which is the provenance the fork and upstream modes
#     fetch a real checkout for in the first place. It used to be worse: a
#     tarball took every filing turn down with it on "not a git repository",
#     until `fetch_base_checkout` began fetching the filing tree separately.
#     Emitting the full SHA costs nothing and keeps the fast path.
#
#   * `describe --dirty` reports only tracked modifications. A tree whose only
#     change is a new untracked file -- which is exactly what adding a skill or
#     a module looks like -- stamps clean, and the runner then fetches a commit
#     that does not contain the change and reviews code the pod is not running.
#     `git status --porcelain` counts untracked files, so it is the one used
#     here.
#
# Output is the full 40-character SHA, plus a `-dirty` suffix when the working
# tree has any modification at all. Empty outside a git checkout, which the
# runner treats as unknown and refuses to investigate rather than guessing at
# `main`. Always exits 0: an unstamped image is a supported outcome, and a
# caller running under `set -e` should not die because it was handed a tarball
# instead of a clone.
#
# Usage: git_revision_stamp.sh [repo-root]   (default: this script's parent)
set -euo pipefail

repo_root="${1:-}"
if [ -z "$repo_root" ]; then
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

sha="$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || true)"
if [ -n "$sha" ] && [ -n "$(git -C "$repo_root" status --porcelain 2>/dev/null)" ]; then
  sha="${sha}-dirty"
fi

printf '%s\n' "$sha"
