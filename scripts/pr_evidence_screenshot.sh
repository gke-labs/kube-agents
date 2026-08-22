#!/usr/bin/env bash
# ==============================================================================
# Publish a screenshot as linkable evidence for a pull request's Live
# validation section.
# ==============================================================================
# PRs here are opened with `gh`, and no GitHub API accepts an image the way
# browser drag-and-drop does — that upload path is session-authenticated and
# browser-only. So a screenshot that proves a verification step has nowhere to
# live unless something hosts it. This script hosts it on an orphan
# `pr-evidence` branch of the author's fork: forks of this repository are
# public, so `raw.githubusercontent.com` URLs from that branch render inline
# in a PR body.
#
# The file name carries the provenance — the commit under test and a UTC
# timestamp — and files are only ever added to the branch, never overwritten,
# so a published URL keeps meaning what it meant when it was pasted.
#
# Usage:
#   scripts/pr_evidence_screenshot.sh [--remote <name>] <url> <slug>
#   scripts/pr_evidence_screenshot.sh [--remote <name>] --file <path> <slug>
#
# The first form captures <url> with a headless Chromium: the first of
# $PR_EVIDENCE_BROWSER, $AGENT_BROWSER_EXECUTABLE_PATH (the agent runtime
# exports this for its bundled Playwright Chromium — deploy/shared/
# docker-entrypoint.sh), or an installed Chrome/Chromium. Driving the binary
# directly avoids `npx playwright`, which needs a working npm and fails
# behind authenticated mirrors (the same failure AGENTS.md documents for
# `npx prettier`). The second form publishes an image you already have — a
# macOS `screencapture` of a surface headless Chromium cannot reach, for
# example.
#
# <slug> is a short kebab-case description that becomes part of the file name
# and the image's alt text (e.g. `kanban-task-done`). --remote names the git
# remote pointing at your fork; without it the script uses the one remote
# that is not gke-labs/kube-agents and refuses if that is ambiguous.
#
# Output is the ready-to-paste Markdown: the inline image plus a caption line
# recording when, from what, and at which commit it was captured.
set -euo pipefail

usage() {
  sed -n '/^# Usage:/,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
  exit 1
}

remote=""
file=""
url=""
viewport="1280,800"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --remote)
      remote="${2:?--remote needs a value}"
      shift 2
      ;;
    --file)
      file="${2:?--file needs a path}"
      shift 2
      ;;
    -h | --help) usage ;;
    *) break ;;
  esac
done

if [[ -n "$file" ]]; then
  [[ $# -eq 1 ]] || usage
  [[ -f "$file" ]] || {
    echo "error: no such file: $file" >&2
    exit 1
  }
else
  [[ $# -eq 2 ]] || usage
  url="$1"
  shift
fi
slug="$1"
[[ "$slug" =~ ^[a-z0-9][a-z0-9-]*$ ]] || {
  echo "error: slug must be kebab-case (got: $slug)" >&2
  exit 1
}

# Provenance comes from the repository the script is run in, so run it from
# the worktree whose code the screenshot is evidence for.
sha="$(git rev-parse --short=12 HEAD)"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
name="${slug}-${sha}-${ts}.png"

# Find the fork. PR branches live on forks (AGENTS.md, Pull Request Hygiene),
# so the fork is any remote that is not the upstream repository.
if [[ -z "$remote" ]]; then
  candidates=()
  while read -r r; do
    git remote get-url "$r" | grep -q 'gke-labs/kube-agents' || candidates+=("$r")
  done < <(git remote)
  if [[ ${#candidates[@]} -ne 1 ]]; then
    echo "error: cannot pick a fork remote from: ${candidates[*]:-none}." >&2
    echo "Pass --remote <name>." >&2
    exit 1
  fi
  remote="${candidates[0]}"
fi
fork_url="$(git remote get-url "$remote")"
# git@github.com:owner/repo.git and https://github.com/owner/repo(.git) both
# reduce to owner/repo.
fork_slug="$(echo "$fork_url" | sed -E 's#^(git@github\.com:|https://github\.com/)##; s#\.git$##')"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

find_browser() {
  local candidate
  for candidate in \
    "${PR_EVIDENCE_BROWSER:-}" \
    "${AGENT_BROWSER_EXECUTABLE_PATH:-}" \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "$(command -v chromium || true)" \
    "$(command -v google-chrome || true)"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      echo "$candidate"
      return 0
    fi
  done
  echo "error: no Chromium found; set PR_EVIDENCE_BROWSER to a browser binary" >&2
  return 1
}

if [[ -n "$url" ]]; then
  browser="$(find_browser)"
  file="$workdir/$name"
  "$browser" --headless --disable-gpu --hide-scrollbars \
    --window-size="$viewport" --screenshot="$file" "$url" >&2
  [[ -s "$file" ]] || {
    echo "error: capture produced no image" >&2
    exit 1
  }
  source_line="headless-Chromium screenshot of $url (window ${viewport/,/x})"
else
  source_line="pre-captured image ($(basename "$file"))"
fi

# Publish on the orphan branch: clone it if the fork already has one,
# otherwise start it from scratch. Only ever add files.
clone="$workdir/pr-evidence"
if git clone --quiet --depth 1 --branch pr-evidence "$fork_url" "$clone" 2>/dev/null; then
  :
else
  git init --quiet "$clone"
  git -C "$clone" checkout --quiet --orphan pr-evidence
fi
cp "$file" "$clone/$name"
git -C "$clone" add "$name"
git -C "$clone" commit --quiet -m "evidence: $name"
git -C "$clone" push --quiet "$fork_url" HEAD:refs/heads/pr-evidence

raw_url="https://raw.githubusercontent.com/${fork_slug}/pr-evidence/${name}"
cat <<EOF

![${slug}](${raw_url})

_Captured ${ts} at commit ${sha} — ${source_line}._
EOF
