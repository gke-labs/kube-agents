---
# Claude Code loads a rule without `paths` unconditionally; Antigravity uses `trigger` and `description`.
trigger: model_decision
description: "What a public post may carry: pull request bodies, comments, review replies, issue bodies and commit messages name a run's outcome and where its record is, nothing from inside the record."
---

# Public surfaces carry the outcome, not the record

Everything on github.com is public: pull request bodies, comments, review replies, issue
bodies, commit messages, committed files. An eval run's record is internal: it lives in the run's
directory and on the CI health dashboard.

A public post about a run may carry:

- the case id and the run's outcome as a word (red, green, not evaluated);
- where the record is: the run's build id or timestamp, the link to its directory in the lanes'
  log bucket, or the dashboard page;
- the stage or check that failed, by name (provisioning, verification, a check id), without
  its reason text;
- the install, the commit, the run's wall clock, and infrastructure facts (the lease, the pool
  project, the Prow state).

It carries nothing from inside the record: what the run produced (per-repetition results, counts or
ratios, check reasons, scores, report and transcript text) and how the run was configured, the model
and its provider included. It names no internal group or access detail: who can open the record is
not for the post. A reader who needs the detail opens the record. A run on a dev install has no
record anyone else can open: the pull request cites the run directory by name, and the author shares
it with a reviewer who asks, off GitHub; a reviewer of an agent-behaviour change whose red is a dev
run asks before approving.

This holds for a person and for a bot alike, for a reply that quotes someone else's post, and for
a file committed to the repository. A pull request's **Live validation** section names the failing
check and cites the red and green runs ([`eval_driven_development.md`](eval_driven_development.md),
"What the pull request records").
