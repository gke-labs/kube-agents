## Summary

<!--
One paragraph, in plain prose, explaining what this PR does — what a reviewer would
want to know before reading the diff. Write it for a human, not as a bullet list or a
restatement of the commit message.
-->

## Why This Change

<!--
The problem, motivation, or user need this PR addresses, and what stays broken without
it. Impact belongs here too — say it once, in the place that already explains why the
problem is worth fixing.
-->

## What Changed

<!--
The important behaviour, interface, or workflow changes. GitHub's "Files changed" tab
already lists every file, so name one only where the reviewer needs to know why that
file matters.
-->

-

## Context

<!-- Include related issues, PRs, follow-up work, or other background. -->

## Testing

<!-- Automated checks: unit tests, builds, `make docs-check`, `prettier --check`. -->

-

### Live validation

<!--
Required: an empty section is not an answer. Describe how this change was exercised
against a real, running kube-agents installation — which install (cluster, image tag,
operator version), what you did, and what you observed at each layer the change claims to
touch: the CR `.status`, the Deployment env, the file or process inside the pod.

Prove the mechanism, not a coincidence: if the new behaviour happens to match the old
default, set something distinctly different, then revert and confirm it goes back.

Say plainly what you could NOT cover and why, and confirm any test artifacts were
cleaned up.

If the change cannot reach a running installation — docs-only, a CI workflow, a path
that needs infrastructure you do not have — write "Not live-tested" and say why.

Full contract: AGENTS.md, "Pull Request Hygiene".
-->

-

## Self-Review

<!--
Required: an empty section is not an answer. You are this change's first hostile
reader, and this is where you say so. Which pass you ran, what you looked for,
what it found, and what you did with each finding — fixed, or deliberately not,
with the reason.

"No findings" is a normal outcome and a complete answer only when you also say
what you looked for. A reason for not fixing something is an answer when it is an
argument about this change; "out of scope" and "will fix later" on their own are
not.

Full contract: AGENTS.md, "Pull Request Hygiene".
-->

-

## Risk & Rollout

<!--
Blast radius, any new failure mode, how to revert, and anything that has to happen at
merge time. A short paragraph. "Low risk, no runtime code paths touched" is a complete
answer when it is true.
-->
