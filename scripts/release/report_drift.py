#!/usr/bin/env python3
"""Turns a `terraform plan` for a long-lived environment into a tracked issue.

Drift itself is not the failure mode; drift nobody can see is. autopush ran a
month behind the composition on main and nothing anywhere said so: a green
redeploy reported that the images rolled, which reads as "main is deployed",
and the infrastructure half of that sentence was false the whole time. That is
#1117 §8.

So: one issue per environment, opened when a plan is non-empty, updated while it
stays non-empty, and closed the moment a plan comes back clean. One issue rather
than one per run, because a scheduled job that files a new issue every night
teaches everyone to filter it out.

Usage:
  report_drift.py --env autopush --plan-log path [--drift true|false|unknown]

Requires GH_TOKEN and the `gh` CLI, which every GitHub Actions runner has.
"""

import argparse
import json
import os
import re
import subprocess
import sys

# The issue is found again by this marker rather than by title, so retitling one
# by hand does not orphan it and start a second.
MARKER_TEMPLATE = "<!-- kube-agents-drift-report: {env} -->"
LABEL = "infra-drift"

# How many resource actions the issue body lists before truncating. A plan that
# replaces a cluster runs to hundreds, and an issue body has a hard size limit;
# the artifact carries the whole plan either way.
MAX_LISTED_ACTIONS = 100
# `gh issue list --limit`. One page is enough because the search is by label and
# this reporter opens at most one issue per environment.
ISSUE_LIST_LIMIT = 100

# `Plan: 1 to add, 3 to change, 0 to destroy.` and the per-resource action
# headers terraform prints above each block. Together these are the whole
# report a reader needs; the rest of a plan is attribute-level detail that
# belongs in the artifact.
PLAN_TOTALS_RE = re.compile(r"^Plan: .*$", re.MULTILINE)
# Three shapes, not one. A tainted resource is announced as
# `# addr is tainted, so must be replaced`, so an alternation anchored
# immediately after the address misses it — and a plan whose only changes are
# tainted replacements would then render an issue with no resource list and no
# destroy warning, which is the opposite of what it is reporting.
RESOURCE_ACTION_RE = re.compile(
    r"^\s*# (\S+) (?:is tainted, so )?(?:will be|must be) (.+?)\s*$",
    re.MULTILINE)

# A destroy in a plan against a long-lived environment is the dangerous case and
# the one #1060 was about, so it is called out rather than left for the reader
# to spot in a list.
DESTRUCTIVE = ("destroyed", "replaced")


def run(args, check=True):
    proc = subprocess.run(args, capture_output=True, text=True)
    if check and proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit("command failed: %s" % " ".join(args))
    return proc


def summarise(plan_text):
    """(totals line, [(address, action)], destructive?) from a plan's text."""
    totals = PLAN_TOTALS_RE.findall(plan_text)
    actions = RESOURCE_ACTION_RE.findall(plan_text)
    destructive = any(any(word in action for word in DESTRUCTIVE)
                      for _, action in actions)
    return (totals[-1] if totals else ""), actions, destructive


def body_for(env, run_url, totals, actions, destructive):
    marker = MARKER_TEMPLATE.format(env=env)
    lines = [
        marker,
        "",
        "`%s` has drifted from `terraform/examples/full-install` on `main`."
        % env,
        "",
        "Its images are current -- the redeploy workflows keep those moving --"
        " and its infrastructure is not. Everything below is a difference"
        " between what the composition says this environment should be and"
        " what it is.",
        "",
    ]
    if destructive:
        lines += [
            "> [!WARNING]",
            "> This plan would DESTROY or REPLACE resources. Read it before"
            " applying it. A plan that destroys what nobody meant to destroy is"
            " usually missing configuration rather than reporting real drift --"
            " check the environment's GitHub variables against"
            " `docs/site/src/content/docs/deploy/environment-reconcile.md`"
            " first.",
            "",
        ]
    if totals:
        lines += ["```", totals, "```", ""]
    if actions:
        lines += ["<details><summary>Resources the plan would change</summary>",
                  "", "```"]
        lines += ["%s: %s" % (addr, action)
                  for addr, action in actions[:MAX_LISTED_ACTIONS]]
        if len(actions) > MAX_LISTED_ACTIONS:
            lines.append("... and %d more" % (len(actions) - MAX_LISTED_ACTIONS))
        lines += ["```", "", "</details>", ""]
    lines += [
        "The full plan is attached to the run below as an artifact.",
        "",
        "**To close this:** reconcile the environment --"
        " `Shared: Reconcile Environment` with `mode: apply` --"
        " or fix whatever the plan is wrong about. This issue closes itself"
        " when a later plan comes back clean.",
        "",
        "Run: %s" % run_url,
        "",
        "Tracked by #1117.",
    ]
    return "\n".join(lines)


def ensure_label(repo):
    """The label has to exist before an issue can carry it.

    `gh issue create --label` fails outright on a label the repository does not
    have, so without this the first morning that finds drift ends with a red
    job and no report -- and repeats that every night, since the issue this
    would have opened is also the thing `find_issue` looks for. Creating it is
    idempotent (`--force` updates an existing one rather than failing), so this
    is a no-op from the second run onwards.
    """
    run(["gh", "label", "create", LABEL, "--repo", repo, "--force",
         "--color", "B60205",
         "--description", "A long-lived environment has drifted from the "
                          "composition on main"], check=False)


def find_issue(repo, env):
    marker = MARKER_TEMPLATE.format(env=env)
    proc = run(["gh", "issue", "list", "--repo", repo, "--state", "open",
                "--label", LABEL, "--limit", str(ISSUE_LIST_LIMIT),
                "--json", "number,body"], check=False)
    if proc.returncode != 0:
        return None
    try:
        for issue in json.loads(proc.stdout or "[]"):
            if marker in (issue.get("body") or ""):
                return issue["number"]
    except json.JSONDecodeError:
        return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True)
    ap.add_argument("--plan-log", required=True)
    ap.add_argument("--drift", default="unknown",
                    choices=["true", "false", "unknown"])
    ap.add_argument("--repo", default=os.environ.get(
        "GITHUB_REPOSITORY", "gke-labs/kube-agents"))
    args = ap.parse_args()

    run_url = "%s/%s/actions/runs/%s" % (
        os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
        args.repo, os.environ.get("GITHUB_RUN_ID", ""))

    existing = find_issue(args.repo, args.env)

    # "unknown" is a plan that failed to run. It is not evidence of drift and it
    # is not evidence of the absence of drift, so an open issue stays open and a
    # closed one stays closed. The job itself is red; that is the signal.
    if args.drift == "unknown":
        print("Plan did not complete; leaving the drift report as it is.")
        return 0

    if args.drift == "false":
        if existing:
            run(["gh", "issue", "comment", str(existing), "--repo", args.repo,
                 "--body", "A plan at %s came back clean. Closing." % run_url])
            run(["gh", "issue", "close", str(existing), "--repo", args.repo])
            print("Closed #%d: %s is back in sync." % (existing, args.env))
        else:
            print("%s is in sync." % args.env)
        return 0

    try:
        with open(args.plan_log, encoding="utf-8", errors="replace") as fh:
            plan_text = fh.read()
    except OSError:
        plan_text = ""

    totals, actions, destructive = summarise(plan_text)
    body = body_for(args.env, run_url, totals, actions, destructive)
    title = "infra: %s has drifted from the composition on main" % args.env

    if existing:
        # Edited rather than commented on: the body is meant to describe the
        # drift as it stands now, and a month of nightly comments describing
        # yesterday's is what makes a tracking issue unreadable.
        run(["gh", "issue", "edit", str(existing), "--repo", args.repo,
             "--body", body])
        print("Updated #%d." % existing)
    else:
        ensure_label(args.repo)
        proc = run(["gh", "issue", "create", "--repo", args.repo,
                    "--title", title, "--label", LABEL, "--body", body])
        print(proc.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
