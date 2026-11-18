#!/usr/bin/env python3
"""Whether a held live-test lease belongs to an earlier, finished reconcile run.

A reconcile takes the live-test lease under its workflow run's session
(gha-<run id>) and releases it when it ends. When that release fails -- a
kubectl timeout against the cluster is the case seen (#6381) -- the lease stays
held for its whole TTL, and every later reconcile finds it and refuses, because
a later run is a different session with no token for it.

This decides, fail-closed, whether the holder is such a run, so the caller can
take the lease over with `live_test_lease.py takeover --if-session`, a
compare-and-swap on that session. Every condition must hold:

  * the lease's session is exactly gha-<digits>;
  * its note is exactly "scheduled reconcile of <env>";
  * GitHub reports that run, in this repository, as completed (any conclusion:
    a cancelled or failed run holds no one's live test either, and the apply it
    was making is idempotent);
  * that run's workflow is one of the workflows that run a reconcile.

Anything else -- a person's or an agent's lease, a run still in progress, a
workflow outside the list, or a GitHub API call that fails or answers in an
unexpected shape -- is "no", and the caller keeps today's behaviour.

Reads `live_test_lease.py status --json` on stdin. Prints one JSON object:
  {"takeover": true, "session": "gha-123", "run_id": "123", "conclusion": "success", "path": "..."}
  {"takeover": false, "reason": "..."}
Exit status is always 0 unless the arguments are wrong; the caller branches on
"takeover".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

SESSION_RE = re.compile(r"^gha-([0-9]+)$")

# Workflows that call reconcile-environment.yml with mode apply, plus the
# reusable workflow itself. A run of anything else is not a reconcile, whatever
# its lease note says.
RECONCILE_WORKFLOWS = frozenset({
    ".github/workflows/autopush-deploy.yml",
    ".github/workflows/deploy-environment.yml",
    ".github/workflows/reconcile-environment.yml",
    ".github/workflows/staging-deploy.yml",
    ".github/workflows/staging-promotion-pipeline.yml",
})


def gh_run(repo: str, run_id: str) -> dict:
    """GET /repos/{repo}/actions/runs/{id} through the gh CLI (GH_TOKEN)."""
    proc = subprocess.run(
        ["gh", "api", "repos/%s/actions/runs/%s" % (repo, run_id)],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or "gh api rc=%d" % proc.returncode)
    return json.loads(proc.stdout)


def decide(status: list, env: str, repo: str, fetch_run=gh_run) -> dict:
    held = [e for e in status if isinstance(e, dict) and e.get("state") == "held"]
    if len(held) != 1:
        return {"takeover": False, "reason": "expected one held lease, found %d" % len(held)}
    entry = held[0]
    session = entry.get("session") or ""
    match = SESSION_RE.match(session)
    if not match:
        return {"takeover": False, "reason": "the holder is not a workflow run (session %r)" % session}
    want_note = "scheduled reconcile of %s" % env
    if entry.get("note") != want_note:
        return {"takeover": False, "reason": "the lease's note is not %r" % want_note}
    run_id = match.group(1)
    try:
        run = fetch_run(repo, run_id)
    except Exception as exc:  # fail closed on anything the API does
        return {"takeover": False, "reason": "could not read run %s: %s" % (run_id, exc)}
    if not isinstance(run, dict) or str(run.get("id")) != run_id:
        return {"takeover": False, "reason": "run %s: unexpected API answer" % run_id}
    path = run.get("path") or ""
    # A reusable workflow's run reports the caller's path; strip any @ref suffix.
    path = path.split("@", 1)[0]
    if path not in RECONCILE_WORKFLOWS:
        return {"takeover": False, "reason": "run %s is %r, not a reconcile workflow" % (run_id, path)}
    if run.get("status") != "completed":
        return {"takeover": False, "reason": "run %s is still %s" % (run_id, run.get("status"))}
    return {"takeover": True, "session": session, "run_id": run_id,
            "conclusion": run.get("conclusion") or "", "path": path}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", required=True, help="the environment being reconciled")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = ap.parse_args(argv)
    if not args.repo:
        print(json.dumps({"takeover": False, "reason": "no repository (GITHUB_REPOSITORY unset)"}))
        return 0
    try:
        status = json.load(sys.stdin)
    except ValueError as exc:
        print(json.dumps({"takeover": False, "reason": "unreadable lease status: %s" % exc}))
        return 0
    if not isinstance(status, list):
        status = []
    print(json.dumps(decide(status, args.env, args.repo)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
