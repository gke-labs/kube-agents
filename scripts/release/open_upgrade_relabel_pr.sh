#!/usr/bin/env bash
# Opens the pull request that relabels the upgrade table's `pending` rows a release just shipped.
#
#   RELEASE_VERSION=0.30.0 GH_TOKEN=<release bot token> scripts/release/open_upgrade_relabel_pr.sh
#
# Run by release-publish.yml after it tags RELEASE_VERSION, from a full clone of main with the tags
# fetched. scripts/relabel_upgrade_table_pending.py rewrites each `pending` row a release contains to
# that release, by the rule `make docs-check-upgrade-releases` checks, so the check on main is not left
# failing until someone relabels the rows by hand. With nothing to relabel it changes nothing. Otherwise
# it pushes the relabel to one fixed branch and opens a pull request against main, or updates the branch
# of the one already open (a re-run, or a later release while the first is unmerged). The pull request still needs a reviewer: this
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
readonly GIT_USER_NAME="kube-agents release bot"
readonly GIT_USER_EMAIL="${GIT_USER_EMAIL:-kube-agents-release-bot@users.noreply.github.com}"

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
pr_title="docs(upgrade): relabel the pending rows ${releases} shipped, fixing main's Documentation Checks"

git switch -C "${RELABEL_BRANCH}"
git -c user.name="${GIT_USER_NAME}" -c user.email="${GIT_USER_EMAIL}" commit --quiet -m "${commit_message}" -- "${UPGRADE_PAGE}"
# The branch is this script's own, named for the release, so a re-run replaces it. The checkout keeps no
# credentials (persist-credentials: false); the token is handed to this one push as a header.
push_auth=()
if [ -n "${GH_TOKEN:-}" ]; then
  push_auth=(-c "http.https://github.com/.extraheader=AUTHORIZATION: basic $(printf 'x-access-token:%s' "${GH_TOKEN}" | base64 | tr -d '\n')")
fi
git ${push_auth[@]+"${push_auth[@]}"} push --force origin "HEAD:refs/heads/${RELABEL_BRANCH}"

# read, not $(cat <<EOF): bash 3.2 misparses an apostrophe in a here-document inside $(...).
IFS= read -r -d '' body <<EOF || true
These rows of the upgrade page's breaking-changes table still said \`pending\`, though a release now contains each. Each is relabelled with the first release that contains it:

${rows}

Until they're relabelled, \`make docs-check-upgrade-releases\` (Documentation Checks) fails on every push to main. This change only rewrites their Release cells, by the rule that check uses (\`scripts/relabel_upgrade_table_pending.py\`). Approving it turns main's docs check green again.

Opened by the release workflow after it tagged ${RELEASE_VERSION} (#5673). A later release updates this pull request, rather than opening another, while it is open.
EOF

existing="$(gh pr list --base "${BASE_BRANCH}" --head "${RELABEL_BRANCH}" --state open --json number --jq '.[0].number // empty')"
if [ -n "${existing}" ]; then
  echo "✅ Updated the branch of the open relabel pull request #${existing}."
  exit 0
fi
gh pr create --base "${BASE_BRANCH}" --head "${RELABEL_BRANCH}" --title "${pr_title}" --body "${body}"
