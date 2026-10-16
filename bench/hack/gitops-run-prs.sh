#!/usr/bin/env bash
#
# The pull requests opened in a GitOps repository since a run started, with
# their bases, for run-gitops-pilot.sh's step 5 (gke-labs/kube-agents#1970).
# The harness waits only for a pull request onto the run branch, so a run whose
# agent opened its pull request onto the default branch and a run whose agent
# opened none both record no_pr; this list tells the two apart.
#
# Usage: gitops-run-prs.sh <owner/repo> <run start, %Y-%m-%dT%H:%M:%SZ> <default branch>
# Env: GH_TOKEN (read access to the repository's pull requests).
# stdout: a JSON array of {number, base, head, url, created_at}, or null when
#   the listing failed (never [] for a failure, so a campaign record does not
#   read a failed listing as "no pull requests").
# stderr: the count, how many went onto the default branch, and one line per
#   pull request.
set -euo pipefail

# One page of pull requests covers a per-run repository.
# Not GitHubClient.pulls_updated_since: that windows by updated_at and raises on
# failure, while this record wants pull requests created since the run started and
# null when the listing fails.
readonly PR_LIST_PAGE_SIZE=100

slug="${1:?usage: $0 <owner/repo> <run start> <default branch>}"
started="${2:?usage: $0 <owner/repo> <run start> <default branch>}"
default_branch="${3:?usage: $0 <owner/repo> <run start> <default branch>}"

# ISO 8601 UTC timestamps in one format compare correctly as strings.
if ! listing="$(gh api "repos/${slug}/pulls?state=all&sort=created&direction=desc&per_page=${PR_LIST_PAGE_SIZE}" 2>/dev/null)" \
  || ! prs="$(python3 -c '
import json, sys
started = sys.argv[1]
print(json.dumps([{"number": p["number"], "base": p["base"]["ref"], "head": p["head"]["ref"], "url": p["html_url"], "created_at": p["created_at"]}
                  for p in json.loads(sys.stdin.read()) if p["created_at"] >= started]))' "${started}" <<< "${listing}" 2>/dev/null)"; then
  echo "WARN could not list the pull requests in ${slug}" >&2
  echo null
  exit 0
fi

python3 -c '
import json, sys
prs = json.loads(sys.argv[1])
onto_default = sum(1 for p in prs if p["base"] == sys.argv[2])
print("==> pull requests opened since %s: %d, %d onto %s" % (sys.argv[3], len(prs), onto_default, sys.argv[2]))
for p in prs:
    print("    #%s base %s head %s %s" % (p["number"], p["base"], p["head"], p["url"]))' "${prs}" "${default_branch}" "${started}" >&2
echo "${prs}"
