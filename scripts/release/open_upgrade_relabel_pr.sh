#!/usr/bin/env bash
# Opens the pull request that relabels the upgrade table's `pending` rows a release just shipped.
#
#   RELEASE_VERSION=0.30.0 GH_TOKEN=<release bot token> scripts/release/open_upgrade_relabel_pr.sh
#
# Run by release-publish.yml after it tags RELEASE_VERSION, from a full clone of main with the tags
# fetched. scripts/relabel_upgrade_table_pending.py rewrites each `pending` row a release contains to
# that release, by the rule `make docs-check-upgrade-releases` checks, and puts the table back in newest-first
# order, so the check on main is not left
# failing until someone relabels the rows by hand. With nothing to relabel it changes nothing. Otherwise
# it pushes the relabel to one fixed branch and opens a pull request against main, or updates the branch,
# title and description of the one already open (a re-run, or a later release while the first is unmerged). The pull request still needs a reviewer: this
# writes no commit to main itself.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

RELEASE_VERSION="${1:-${RELEASE_VERSION:-}}"
if [ -z "${RELEASE_VERSION}" ]; then
  echo "❌ ERROR: RELEASE_VERSION is required as an argument or environment variable." >&2
  exit 1
fi
validate_pure_numeric_semver "${RELEASE_VERSION}" "Release version" || exit 1

REPO_ROOT="$(git rev-parse --show-toplevel)"
readonly REPO_ROOT
readonly UPGRADE_PAGE="docs/site/src/content/docs/install/upgrade.md"
readonly BASE_BRANCH="main"
# One branch for every release, not one per release: a release cut while an earlier relabel pull request
# is still open finds it and updates it, rather than opening a second for the same rows. Not under
# release/: the kube-agents-release-lines ruleset guards refs/heads/release/** for the lines.
readonly RELABEL_BRANCH="relabel-upgrade-table/pending"
# The version CI's prettier check pins (.github/workflows/prettier.yml).
readonly PRETTIER_VERSION="3.9.6"
# Bot user ID 324943056 for kube-agents-release-bot[bot] so GitHub links commits to the App.
readonly GIT_USER_NAME="${GIT_USER_NAME:-kube-agents-release-bot[bot]}"
readonly GIT_USER_EMAIL="${GIT_USER_EMAIL:-324943056+kube-agents-release-bot[bot]@users.noreply.github.com}"

cd "${REPO_ROOT}"

# Neither the relabel nor prettier (fetched from npm) needs the token, so neither gets it.
relabelled="$(env -u GH_TOKEN python3 scripts/relabel_upgrade_table_pending.py)"
if [ -z "${relabelled}" ]; then
  echo "✅ No pending row of the upgrade table is in a release; nothing to relabel."
  exit 0
fi

# A label wider than the column changes its alignment; prettier puts the table back as CI checks it.
env -u GH_TOKEN npx --yes "prettier@${PRETTIER_VERSION}" --write "${UPGRADE_PAGE}" >/dev/null

if git diff --quiet -- "${UPGRADE_PAGE}"; then
  echo "✅ The relabel left the page unchanged; nothing to open."
  exit 0
fi

# A change cell can hold its own code span, so a cell with a backtick is fenced with two.
rows="$(printf '%s\n' "${relabelled}" | awk -F '\t' '{f = index($2, "`") ? "``" : "`"; s = index($2, "`") ? " " : ""; printf "- %s%s%s%s%s → %s\n", f, s, $2, s, f, $1}')"
# The releases the rows are labelled with, which are not always RELEASE_VERSION: a row an earlier release
# shipped, still pending because its relabel has not merged, takes that release.
releases="$(printf '%s\n' "${relabelled}" | cut -f1 | sort -u -t. -k1,1n -k2,2n -k3,3n | paste -sd, - | sed 's/,/, /g')"
commit_message="docs(upgrade): relabel the pending rows ${releases} shipped"
pr_title="docs(upgrade): relabel the pending rows ${releases} shipped"

git switch -C "${RELABEL_BRANCH}"
# Set GIT_AUTHOR_*/GIT_COMMITTER_* inline so caller env and author.*/committer.* git config cannot override.
GIT_AUTHOR_NAME="${GIT_USER_NAME}" \
  GIT_AUTHOR_EMAIL="${GIT_USER_EMAIL}" \
  GIT_COMMITTER_NAME="${GIT_USER_NAME}" \
  GIT_COMMITTER_EMAIL="${GIT_USER_EMAIL}" \
  git commit --quiet -m "${commit_message}" -- "${UPGRADE_PAGE}"
# The branch is this script's own, one for every release, so a re-run replaces it. The checkout keeps no
# credentials (persist-credentials: false); the token is handed to this one push as a header.
push_auth=()
if [ -n "${GH_TOKEN:-}" ]; then
  push_auth=(-c "http.https://github.com/.extraheader=AUTHORIZATION: basic $(printf 'x-access-token:%s' "${GH_TOKEN}" | base64 | tr -d '\n')")
fi
git ${push_auth[@]+"${push_auth[@]}"} push --force origin "HEAD:refs/heads/${RELABEL_BRANCH}"

# The body answers every section of .github/PULL_REQUEST_TEMPLATE.md: the review bot holds a
# description to each heading, and a section left out opens a thread that keeps the pull request out
# of the merge queue. tests/test_open_upgrade_relabel_pr.py reads this body against the template.
# read, not $(cat <<EOF): bash 3.2 misparses an apostrophe in a here-document inside $(...).
IFS= read -r -d '' body <<EOF || true
## Summary

These rows of the upgrade page's breaking-changes table still said \`pending\`, though a release now contains each. This change relabels each with the first release that contains it. Merging it relabels the rows before main's Documentation Checks starts failing, 48 hours after the release.

## Why This Change

Until the rows are relabelled, \`make docs-check-upgrade-releases\` (Documentation Checks) fails on every push to main once the release is 48 hours old.

## What Changed

\`${UPGRADE_PAGE}\`: the Release cells of these rows change, by the rule that check uses (\`scripts/relabel_upgrade_table_pending.py\`), and the table's rows go back in newest-first order, which the check also holds it to. The change stays inside the breaking-changes table; \`prettier\` then restores the table's formatting, which can touch other rows of it.

${rows}

## Context

Written by the release workflow after it tagged ${RELEASE_VERSION} (#5673). A later release updates this pull request, rather than opening another, while it is open. \`scripts/release/open_upgrade_relabel_pr.sh\` wrote the change and this description; no person and no AI agent did. The script rewrites this description on each run, so an edit made here by hand does not survive the next release.

## Bug Fix: Preventing Recurrence

Not a bug fix. The relabel is the routine step after a release.

## Testing

The release job ran \`scripts/relabel_upgrade_table_pending.py\` and then \`prettier@${PRETTIER_VERSION}\` on the page. Documentation Checks on this pull request run \`make docs-check-upgrade-releases\` on the result.

### Live validation

Not live-tested: a documentation table, with no runtime effect.

### UX change

- [x] No user-visible change

The page keeps its structure. In the breaking-changes table, the Release cells of the rows above change from \`pending\` to a release number, and rows can move to keep the table newest first.

## Self-Review

No review pass ran: a script wrote this change, so no author exists to review it. The script that wrote the cells applies the same rule as the check that reads them. The reviewer is the first reader: confirm that the diff stays inside the breaking-changes table and that Documentation Checks pass.

## Roster Admission

Not a roster admission.

## Risk & Rollout

Documentation only. Revert by reverting the commit.
EOF

existing="$(gh pr list --base "${BASE_BRANCH}" --head "${RELABEL_BRANCH}" --state open --json number --jq '.[0].number // empty')"
if [ -n "${existing}" ]; then
  # The rows and the releases can differ from the run that opened it, so its title and description follow.
  gh pr edit "${existing}" --title "${pr_title}" --body "${body}"
  echo "✅ Updated the branch, title and description of the open relabel pull request #${existing}."
  exit 0
fi
gh pr create --base "${BASE_BRANCH}" --head "${RELABEL_BRANCH}" --title "${pr_title}" --body "${body}"
