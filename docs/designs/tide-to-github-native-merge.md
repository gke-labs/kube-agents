# Migrating merge automation from Tide to GitHub-native

**Status:** Proposed
**Author:** jayantid
**Date:** 2026-09-03

## Summary

We replace Tide with GitHub-native merge automation — branch protection
(rulesets), CODEOWNERS, and per-PR auto-merge — while keeping all Prow CI
(`pull-kube-agents-smoke-test`, `/test`, `/retest`, Boskos leasing) exactly as
it is. The one guarantee we give up is that every merge was smoke-tested
against the latest `main`; we compensate with a periodic smoke on `main` HEAD
and a fix-forward norm.

The trigger for this change: the smoke test became merge-blocking in 2026-09
and now takes ~3–4 hours (ten tasks × three repetitions, 240m timeout). Tide's
core contract is to re-run required presubmits against the current tip of
`main` before merging. At our merge volume (10–29 PRs/day this past week),
`main` moves faster than smoke results stay fresh, so most merges queue behind
a fresh 3–4 h run — PRs sit at "Waiting for status to be reported — Not
mergeable. Retesting: pull-kube-agents-smoke-test" for hours after every
approval.

## What Tide gives us (the case for staying)

This section exists so the decision is made with eyes open. Tide is better
than GitHub-native on exactly these axes:

1. **`main` is always green with respect to the gated checks.** Tide only
   merges a PR whose required presubmits passed against the *current* base
   SHA. Two individually-green PRs cannot land together and break `main`
   undetected — the second one re-tests on top of the first. GitHub
   auto-merge merges on checks that passed against whatever `main` looked
   like when the PR was last pushed; a PR approved a week after its last push
   merges on week-old results. This is the only structural loss, and it is
   real: at 20+ merges/day the staleness window is dozens of commits.

2. **Batching.** When several PRs are green and approved, Tide tests them as
   one batch — one 3–4 h smoke run amortized over N merges. GitHub's native
   equivalent, the merge queue, cannot drive Prow jobs (it triggers checks on
   `merge_group` refs, which Prow's trigger plugin does not respond to), so
   going native forecloses the queue unless the smoke itself moves to GitHub
   Actions. This is a loss of *optionality*: once native, the only road back
   to fresh-base testing is re-adopting Tide or migrating the eval to
   Actions.

3. **Label-driven merge control.** `/hold` and `do-not-merge/*` labels block
   merging for anyone; GitHub has no native label gate. Substitutes (draft
   PRs, dismissing a review, not clicking auto-merge) are softer.

4. **Nuanced review freshness.** Prow resets `lgtm` on new pushes (for
   non-Googler authors) while `approved` survives. GitHub's stale-review
   dismissal is a single all-or-nothing toggle. Note our current config
   (`trusted_team_for_sticky_lgtm: 'Googlers'`) already makes nothing reset
   on push for Googler-authored PRs, so toggle-off matches today's de facto
   behavior for almost all our PRs.

We accept losing all four. The deciding argument: Tide's freshness guarantee
is delivered *by* the retest that is throttling us, and the failure mode it
prevents (a semantic conflict between two green PRs) is one we can detect
within hours with a periodic smoke on `main` and repair by fixing forward.
The smoke has per-PR value either way; the merge-time re-run is the part
whose cost now exceeds its benefit.

## What does not change

- All Prow CI keeps running: `pull-kube-agents-smoke-test` on PRs (with its
  `skip_if_only_changed` filter), `/test`, `/retest`, `/override`-era
  chat-ops that belong to `trigger`, Boskos project leasing, the
  `build-kube-agents` cluster. Tide is severable from job execution.
- The `BOOTSTRAP_ADMITTED` roster in `hack/ci-eval-pr.sh` still decides which
  eval cases can red the job.
- Squash-only merging (becomes a repo setting instead of Tide config).
- Eval-pool load goes *down*: merge-time retest and batch runs disappear;
  only per-PR pushes consume Boskos leases.

## Target state

| Concern | Today (Tide) | Target (GitHub-native) |
|---|---|---|
| Merge actor | Tide bot, label-driven | Author clicks auto-merge; GitHub merges on green |
| Required checks | All non-optional contexts, implicitly (Tide default) | Explicit required-checks list in a ruleset |
| Approval | OWNERS + `approved`/`lgtm` labels | CODEOWNERS team + required review |
| Fresh-base testing | Tide retest/batch before merge | None at merge time; periodic smoke on `main` |
| Hold | `/hold` label | Draft PR / withhold approval / don't enable auto-merge |
| Merge method | `tide/merge-method-squash` config | Repo setting: squash only |

## Migration plan

Ordering principle: build the native gate *alongside* Tide first, watch it
agree with Tide for a few days, then remove Tide. At no point is `main`
unprotected.

### Phase 0 — Preconditions (repo + org admin)

1. **Create a GitHub team** `gke-labs/kube-agents-approvers` mirroring the
   union of `OWNERS` + `OWNERS_ALIASES` (bradhoekstra, dshnayder, haoxuw,
   jayantid, toshiowang, AntonTyb, fatoshoti, mateuszklinowski, mplakhtiy).
   Our OWNERS structure is effectively flat (the `waw-leads` alias puts the
   `k8s-operator/` approvers in the root file), so one team suffices.
2. **Verify every team member has write access** to the repo. CODEOWNERS
   silently ignores users without write permission — this is the classic
   silent-failure bug of this migration.
3. **Decide the stale-review toggle.** Recommendation: leave "dismiss stale
   approvals on push" **off**, matching current sticky-lgtm behavior for
   Googler PRs.
4. **Curate the required-checks list.** Tide today implicitly requires every
   reporting context. Enumerate contexts from the last ~50 merged PRs
   (`gh api repos/gke-labs/kube-agents/commits/<sha>/check-runs`) and pick
   the deliberate gate set — likely: `validate`, `build`, the unit/controller
   test jobs, `Validate Conventional Commit PR Title`, and the security scans
   we actually want blocking. Advisory jobs (AI Review, milestone assignment)
   stay unrequired. GitHub Actions jobs that skip on path filters are safe to
   require (GitHub treats a skipped Actions job as passing).

### Phase 1 — The smoke gate (the one hard problem)

`pull-kube-agents-smoke-test` reports as an external commit status, not an
Actions job, and **empirically posts no status at all on PRs where
`skip_if_only_changed` skips it** (verified on #1209: the only status on the
merged head SHA was `tide`). A plain required status check would therefore
deadlock docs-only PRs, and *not* requiring it would let auto-merge land code
PRs before the 3–4 h smoke finishes. Neither is acceptable.

Add a thin required GitHub Actions job, `smoke-gate`, that:

1. Computes changed files and applies the **same** path filter as the Prow
   job's `skip_if_only_changed` (extract the regex to one shared place —
   `hack/ci-env.sh` — so the two cannot drift; this repo already documents
   the Prow-name-drift hazard in `testing-implementation-plan.md`).
2. If all changed files match the skip filter: exit success immediately.
3. Otherwise: poll the commit status API for `pull-kube-agents-smoke-test`
   on the head SHA and mirror its result. Timeout 5 h (job timeout is 240m).
   Re-runs of the gate (free, seconds) re-read the status, so a `/retest`
   of the smoke just needs a gate re-run afterwards — or the gate is
   triggered on `status` events for the smoke context.

`smoke-gate` goes in the required-checks list; the raw Prow context does not.

### Phase 2 — Native protection alongside Tide

1. Add `CODEOWNERS` (`.github/CODEOWNERS`):
   `* @gke-labs/kube-agents-approvers`. Keep `OWNERS` files for now (Prow
   plugins still read them until Phase 4).
2. Create a new ruleset on `~DEFAULT_BRANCH`: require a PR, require 1
   approval, require code-owner review, required status checks = Phase 0
   list + `smoke-gate`, block force pushes. Leave the existing
   `kube-agents-tide-only-merge` ruleset (id 21010669) untouched for now —
   Tide is still the merger.
3. Repo settings: allow **squash merging only**; enable **auto-merge**.
4. **Soak for ~1 week.** Tide still merges; the new ruleset just has to not
   block anything Tide would have merged. Any PR Tide merges that the
   ruleset would have blocked (or vice versa) is a config bug to fix now,
   while both systems agree on the outcome.

### Phase 3 — Cut over

1. PR to `GoogleCloudPlatform/oss-test-infra`:
   - Remove `gke-labs/kube-agents` from the Tide `queries` and
     `merge_method` entries in `prow/oss/config.yaml`.
   - **Keep** the presubmit and the `trigger` plugin.
   - In the same PR, add a **periodic** job running the smoke against `main`
     HEAD every 6 h (reuse `hack/ci-eval-pr.sh`; it consumes one Boskos
     lease per run, 4 runs/day — well within the 30-project pool now that
     merge-time runs are gone). Report failures to the team channel.
2. Update or delete the `kube-agents-tide-only-merge` ruleset so humans can
   merge via PR again (its purpose — "only Tide merges" — is obsolete). The
   new Phase 2 ruleset is now the sole gate.
3. Announce the new flow: approve → author clicks **auto-merge** →
   GitHub merges on green. `/hold` is replaced by draft/un-approve.

### Phase 4 — Cleanup (after 2 quiet weeks)

1. Trim the plugins list for `gke-labs/kube-agents` in
   `prow/oss/plugins.yaml`: drop `approve`, `lgtm`, `hold`, `override`,
   `owners-label`, `verify-owners` (and the fun ones if desired); keep
   `trigger`. Bypass on the ruleset replaces `/override` — restrict the
   bypass list to the same people who could override.
2. Delete `OWNERS`, `OWNERS_ALIASES`, `k8s-operator/OWNERS` (CODEOWNERS is
   now the source of truth). Optionally add a `size`-label Action if anyone
   misses it.
3. Update `docs/pull-request-workflow.md` and
   `docs/designs/testing-implementation-plan.md` (which documents the Tide
   merge mechanics) to describe the native flow.
4. Remove `pull-kube-agents-smoke-test` from any lingering Tide
   `context_options` (none exist today; this is a check, not an edit).

## Fix-forward policy (replaces the freshness guarantee)

When the periodic smoke on `main` goes red and per-PR smokes were green, the
cause is by construction an interaction between merges (or an infra/flake
issue the harness misclassified). Policy:

- The periodic-smoke failure notification names a rotation owner (reuse the
  eval-dashboard ownership).
- Default response is **fix forward within one working day**; revert only if
  a fix isn't identified by then.
- No merge freeze: PRs keep landing on their own green smokes. A freeze
  would recreate the serialization we are removing.

## Rollback

Every step is reversible independently. Full rollback = one PR to
oss-test-infra re-adding the repo to the Tide queries + re-enabling the
tide-only-merge ruleset. The Phase 2 ruleset can stay enabled under Tide
(Tide respects branch protection), so rollback does not require undoing the
native gate.

## Risks

- **`smoke-gate` filter drift** from the Prow `skip_if_only_changed` regex.
  Mitigated by single-sourcing the regex; residual risk is a job that runs
  (wasteful) or skips (gate gap) on the wrong PRs. The Prow config comment
  already forbids adding `bench/`, `agents/`, `deploy/`, `charts/`,
  `k8s-operator/` to the filter — the shared source must carry that comment.
- **Auto-merge landing a PR the author forgot to re-check.** With dismissal
  off, an approval given before a large force-push still counts. This is
  today's behavior for Googler PRs (sticky lgtm + sticky approved), so it is
  not a regression, but it becomes the *only* behavior.
- **Semantic conflicts on `main`** now surface up to ~6 h post-merge instead
  of never landing. Bounded by the periodic cadence; tighten to 3 h if the
  first month shows real breakage.
