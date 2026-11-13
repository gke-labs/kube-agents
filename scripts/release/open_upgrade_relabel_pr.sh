#!/usr/bin/env bash
# Opens the pull request that relabels the upgrade table's `pending` rows a release just shipped.
#
#   RELEASE_VERSION=0.30.0 GH_TOKEN=<release bot token> scripts/release/open_upgrade_relabel_pr.sh
#
# Run by release-publish.yml after it tags RELEASE_VERSION, from a full clone of main with the tags
# fetched. scripts/relabel_upgrade_table_pending.py rewrites each `pending` row a release contains to
# that release, by the rule `make docs-check-upgrade-releases` checks, so the check on main is not left
# failing until someone relabels the rows by hand. With nothing to relabel it changes nothing. Otherwise
# it pushes the relabel to a branch named for the release and opens a pull request against main, or
# updates the branch of one already open (a re-run). The pull request still needs a reviewer: this
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
UPGRADE_PAGE="docs/site/src/content/docs/install/upgrade.md"
BASE_BRANCH="main"
# Not under release/: the kube-agents-release-lines ruleset guards refs/heads/release/** for the lines.
RELABEL_BRANCH="relabel-upgrade-table/${RELEASE_VERSION}"
# The version CI's prettier check pins (.github/workflows/prettier.yml).
PRETTIER_VERSION="3.9.6"
COMMIT_MESSAGE="docs(upgrade): relabel the rows ${RELEASE_VERSION} shipped from pending"
PR_TITLE="docs(upgrade): relabel the rows ${RELEASE_VERSION} shipped, fixing main's Documentation Checks"
GIT_USER_NAME="kube-agents release bot"
GIT_USER_EMAIL="${GIT_USER_EMAIL:-kube-agents-release-bot@users.noreply.github.com}"

cd "${REPO_ROOT}"

relabelled="$(python3 scripts/relabel_upgrade_table_pending.py)"
if [ -z "${relabelled}" ]; then
  echo "✅ No pending row of the upgrade table is in a release; nothing to relabel."
  exit 0
fi

# A label wider than the column changes its alignment; prettier puts the table back as CI checks it.
npx --yes "prettier@${PRETTIER_VERSION}" --write "${UPGRADE_PAGE}" >/dev/null

if git diff --quiet -- "${UPGRADE_PAGE}"; then
  echo "✅ The relabel left the page unchanged; nothing to open."
  exit 0
fi

rows="$(printf '%s\n' "${relabelled}" | awk -F '\t' '{printf "- `%s` → %s\n", $2, $1}')"

git switch --create "${RELABEL_BRANCH}"
git -c user.name="${GIT_USER_NAME}" -c user.email="${GIT_USER_EMAIL}" commit --quiet -m "${COMMIT_MESSAGE}" -- "${UPGRADE_PAGE}"
# The branch is this script's own, named for the release, so a re-run replaces it.
git push --force origin "HEAD:refs/heads/${RELABEL_BRANCH}"

# read, not $(cat <<EOF): bash 3.2 misparses an apostrophe in a here-document inside $(...).
IFS= read -r -d '' body <<EOF || true
Release ${RELEASE_VERSION} contains these rows of the upgrade page's breaking-changes table, which still said \`pending\`:

${rows}

Until they're relabelled, \`make docs-check-upgrade-releases\` (Documentation Checks) fails on every push to main. This change only rewrites their Release cells, by the rule that check uses (\`scripts/relabel_upgrade_table_pending.py\`). Approving it turns main's docs check green again.

Opened by the release workflow after it tagged ${RELEASE_VERSION} (#5673).
EOF

existing="$(gh pr list --base "${BASE_BRANCH}" --head "${RELABEL_BRANCH}" --state open --json number --jq '.[0].number // empty')"
if [ -n "${existing}" ]; then
  echo "✅ Updated the branch of the open relabel pull request #${existing}."
  exit 0
fi
gh pr create --base "${BASE_BRANCH}" --head "${RELABEL_BRANCH}" --title "${PR_TITLE}" --body "${body}"
