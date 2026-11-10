# Post-merge verification: did the merged fix work?

**Status:** proposed, 2026-11-06. Part of #2000 and the autonomy epic #4250.

## The problem

A merged fix is not a working fix. Today nothing checks, after a merge, that the GitOps tool applied
the change, that the workload stayed healthy, or that the problem went away. On 2026-11-05 a dev
install told a user that Argo CD had deleted a merged PodDisruptionBudget; Argo had not yet synced
the merge.

Auto-merge (a later milestone) depends on this check, so its answer must be the same every time and
hard to fake.

## What it does

For each fix pull request kube-agents opens:

1. **Record it.** When the PR opens, save what it fixes and on which cluster.
2. **Notice the merge.** Reuse the existing sweep that reads every agent PR's state every 4 minutes.
3. **Wait until it is applied.** Ask Argo CD, Flux or Config Sync whether the merge commit is live
   (#5041).
4. **Watch health.** Wait a short window: rollout finished, pods Ready, no new restarts.
5. **Re-check the problem.** Run the audit's own check on that one workload only (#5353).
6. **Report once** on the PR, and in the chat thread that asked for the fix.

## Outcomes

| Outcome                 | When                                                   |
| ----------------------- | ------------------------------------------------------ |
| Verified fixed          | Applied, healthy, problem gone                         |
| Still broken            | Applied, but the problem is still there or pods failed |
| Applied, not re-checked | Applied and healthy, but the re-check could not run    |
| Not applied             | Not live within the deadline                           |

## Settings per fix

| Fix                        | Deadline to apply | Health window                        |
| -------------------------- | ----------------- | ------------------------------------ |
| Missing PDB                | 60 min            | 5 min                                |
| ComputeClass node selector | 60 min            | Rollout, then 15 min with no restart |

## How it runs

A scheduled job every 5 minutes, with no AI model: each step has a fixed answer, and the Nov 5
mistake was a model misreading which commit was live. Records live in Session KV, which the sandbox
cannot write. The job only reads clusters and posts comments; it never merges, approves or reverts.

## Safety

- It accepts a PR only if re-generating the fix from the live workload produces the same files, so a
  forged PR cannot claim a fix the audit would not write.
- Comments contain fixed text, object names and commit IDs, never logs or event messages.
- A restart loses nothing; a hidden marker stops it posting twice.

## Testing

Unit tests for each step and outcome. An eval case that runs the check after a GitOps fix in three
variants: fixed, still broken, not applied. Live: a planted PDB fix under Argo CD and under Config
Sync each gets one "verified fixed".

## Not in this design

Reverting a failed fix (next milestone), other kinds of fix, Flux HelmRelease, and Terraform or
Config Connector fixes.

## Open questions

1. Should fixes asked for in chat get this check too? They come from a different path today.
2. Should reverting ship in the first milestone?
3. If a fix applies after "not applied" was posted, stay silent or post a late note?
4. Are the deadlines and windows right? The first pilot will tell.
5. Step 2 needs the PR-state sweep to share what it already reads. Will its owner agree?
