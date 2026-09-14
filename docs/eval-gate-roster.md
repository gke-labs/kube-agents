# The eval gate roster

`BOOTSTRAP_ADMITTED` in [`hack/ci-eval-pr.sh`](../hack/ci-eval-pr.sh) names the eval cases
that can red `pull-kube-agents-smoke-test`. This page is the prose that used to sit above
that export: what admits a case, which cases are held out and on which issue, how far the
roster's promise reaches, and how a flaky case is demoted. The list itself stays in the
script — edit it there, and keep this page in step. It lives under `docs/` on purpose:
the script's step-0 revalidation treats `docs/` as inert (and the Prow path filter in
`oss-test-infra` does today too), so a review finding against this prose costs no eval run
([#1179](https://github.com/gke-labs/kube-agents/issues/1179)), which is exactly what
roster-comment edits used to cost.

## What the roster is

The roster is the authority on what blocks. A case named in `BOOTSTRAP_ADMITTED` arms
rung 4: it reds a pull request by failing every repetition. An unlisted case runs, reports,
and cannot red one on a graded failure. That is so by decision
([#1493](https://github.com/gke-labs/kube-agents/issues/1493), 2026-09-14): nobody should be
able to move a case into or out of the blocking set without the eval crew knowing, and a
roster edit reviewed in a pull request is that knowledge.

**The record recommends; it does not decide.** The evidence store
(`bench/baselines/README.md`) pools the nightly's runs per case at the current version key,
and once it holds a full window — `EVAL_ADMISSION_MIN_RUNS` runs, default 20 — it has a
verdict: the case clears the bar or it does not. Under `EVAL_ADMISSION_MODE=roster`, the
default, `BaselineStore.admission()` in `bench/kube_agents_bench/baselines.py` computes that
verdict and reports it beside the roster's decision, per case, on every presubmit and
nightly: in the case's admission reason, in the per-case JSON as `record_verdict`, and in the
verdict markdown's **Admitted by · record says** column, which reads `bootstrap · record:
would demote (17/21, below 95% over 20)` for a listed case that has stopped working on
`main`, `none · record: would admit (21/21 at key …)` for an unlisted case that has earned a
seat, and `bootstrap · record: collecting 9/20`, `record: stale (key …)` or `record: none`
short of a full window. The column appears once a store is configured or the record has
anything to say about any case; with `EVAL_BASELINE_STORE` unset and the checked-in
directory empty it is absent and the presubmit's output is what it was before. That
recommendation is what a roster pull request cites, in either direction — see
[Demoting a flaky case](#demoting-a-flaky-case) below.

`EVAL_ADMISSION_MODE=record` is the other mode, kept for a later decision: the record decides
a case once it holds a full window, either way, and the list is the fallback while it cannot
(`stale`, `collecting`, nothing). No job sets it; `hack/ci-eval-pr.sh` exports the variable
with `roster` as its default. While the store holds nothing for
a listed case at the current key, the case leaves rung 6 quiet and contributes nothing to
main's side of the aggregate in either mode; once the nightly has appended a partial window
(`collecting`), that evidence feeds both. See
[`docs/designs/eval-scorer.md`](designs/eval-scorer.md) for computed admission and
[`docs/designs/testing-strategy.md`](designs/testing-strategy.md) §4.2 for the verdict
ladder the rungs below refer to.

The variable is comma- or whitespace-separated task ids; `_bootstrap_admitted()` in
`bench/kube_agents_bench/gate.py` accepts either.

## The admission bar, and who clears it

Ten of the eighteen active cases are admitted (recount the uncommented entries in the
script's `TASKS` array rather than trusting this sentence — an earlier copy of it
miscounted twice): the ones whose recent record shows failures only on their own
regressions or on infra classes the harness already excludes from the verdict.

The rest still run and report on every pull request, and they cannot red one on a GRADED
failure. Four are held out with a filed issue naming the exit condition:

- **capacity-pinned-pool-probe** —
  [#1010](https://github.com/gke-labs/kube-agents/issues/1010): worker completes its card
  at fan-out ("Awaiting synthesis" as the final answer). The failure is correlated across
  repetitions when the agent chooses to fan out, so the collapse rule does not absorb it.
  Enters when the fix merges.
- **cluster-agent-healthy-workload-no-finding** —
  [#1010](https://github.com/gke-labs/kube-agents/issues/1010): the delegation receipt is
  graded as the answer (51 of 156 recorded repetitions).
  [#1100](https://github.com/gke-labs/kube-agents/issues/1100) held this seat until its own
  sweep closed it — the agent invents nothing here, so the false-positive premise is gone
  but the reason for the hold is not. Still main's own trait, so a collapse would tax an
  innocent PR. Enters when #1010's fix merges or when rung-6 screening can compare against
  main.
- **compliance-rbac-overgrant** —
  [#1171](https://github.com/gke-labs/kube-agents/issues/1171): demoted 2026-09-02 after
  rung-4 collapses on unrelated pull requests (#1153 was red on this case alone). The
  fleet-audit delegation chain is degraded: audits go partial on what the agent reports as
  "access limitations", skipping check 2.4 (the cluster-admin-binding check this case
  grades), and some runs publish no ledger at all — so the collapse is the environment's,
  not the diff's. Enters when #1171's re-admission bar holds: delegation fixed and a clean
  3-day graded record.
- **rca-remediation-pr** —
  [#1189](https://github.com/gke-labs/kube-agents/issues/1189): demoted 2026-09-02 evening
  after rung-4 collapses on six unrelated pull requests in one day. The suite's longest
  delegation chain, so it integrates over every environment fault in its window: the
  #1097 429 storms, the #1144 proxy EACCES (fix #1183), and #1184's gap (infra-blocked
  repetitions graded rather than classified) turn one dirty window into a correlated
  collapse. Its own record was 12/13 clean before the storms. Enters when #1189's
  re-admission bar holds.

**autoops-warning-event-triage** is no longer in the presubmit `TASKS` array at all
(tofu wall clock, [#1218](https://github.com/gke-labs/kube-agents/pull/1218)); it runs
and accrues its record via the nightly tier
([#1175](https://github.com/gke-labs/kube-agents/pull/1175)) once that runs. Its
original hold-out rationale stands —
[#1101](https://github.com/gke-labs/kube-agents/issues/1101): 0/5 graded repetitions on
record. It enters `BOOTSTRAP_ADMITTED` when the lettered-options bar is settled and it
has a clean record.

The eval dashboard's Cases page (`cases.html`, "How reliable is each test?") is
the readable view of that record: per case, the presubmit and the nightly pass
rate over repetitions at 7 and 30 days, kept apart — the nightly tier is the only
place a case outside `TASKS` runs at all — beside the case's roster status, which
it reads from the script and from this page. The admission evidence itself is the
baseline store ([`bench/baselines/README.md`](../bench/baselines/README.md)); the
page shows the same nightly runs, it does not replace the store.

The others are simply new and earn their record like any case, then enter:
**security-overgrant-remediation-proposal**
([#1066](https://github.com/gke-labs/kube-agents/issues/1066)) and the three
obtainability activations from
[#1049](https://github.com/gke-labs/kube-agents/issues/1049)
(**obtainability-pdb-semantics**, **obtainability-fleet-exposure-sweep**,
**obtainability-healthy-namespace-silence**).

One re-admission on record: **agent-kanban-smoke** earned its seat back after the
2026-08-27 redesign (a real SRE question graded on `kanban_create` plus cluster names);
the reds that once argued for un-arming it belonged to the old vocabulary check.

## How far the roster's promise reaches

The scope of "a held-out case cannot red a pull request" is rungs 4 and 6 only. Rungs 1–3
— a forbidden cluster mutation, an erroring check, a record that is not a real run — stay
blocking for every case by design, admitted or not: `grade_case` evaluates them before it
reads admission. Those classes signal a broken case or install, not flake, and the fix is
on that side rather than on the roster.

## Demoting a flaky case

If an admitted case reds a pull request its diff cannot explain on a graded failure,
demote it: delete its name from `BOOTSTRAP_ADMITTED` and reference its issue. Demotion is
a one-line same-day edit to the script — that file, not the Prow config, is deliberately
the fast lever. It is the lever for rung-4 reds ONLY: a rung-1–3 red (a mutation, an
erroring verifier, an empty record on a task that provisions nothing — a record whose
deployer died before any agent ran grades INFRA and reds nobody) does not stop when its
case leaves the list.

The record is the evidence the edit cites. Once the store holds a full window for a case at
the current key, its **record says** cell is the demotion's one-line justification: one
night of three failures on `main` against a 21/21 window reads `record: would demote (18/21,
below 95% over 20)` on the next morning's presubmit, and the pull request that deletes the
name quotes it. The same cell is the admission's: an unlisted case reading `record: would
admit (21/21 at key …)` has earned its seat, and the pull request that adds the name quotes
that. Nothing automatic moves a case in either direction — the edit is the mechanism, and
that is the point of the default mode.

A demoted case keeps running and reporting; give it a hold-out entry above with the issue
that names its re-admission condition, and date it as `demoted YYYY-MM-DD` inside its
`- **case-name** —` bullet, the shape the entries above use — the dashboard's Cases page
reads that phrase from those bullets for the case's "demoted" pill. That issue goes to the case's `owner:` in its
`task.yaml` — a GitHub login, or `maintainers` for the approvers in the root `OWNERS` file —
who investigates and either fixes the case or proposes retiring it. A case whose owner does
not answer stays demoted. The bar is the same for a contributed case and an in-house one;
[`bench/CONTRIBUTING.md`](../bench/CONTRIBUTING.md) is what a contributor signs up to.

## The roster and the record

The list is not scheduled for deletion. The decision on
[#1493](https://github.com/gke-labs/kube-agents/issues/1493) (2026-09-14) is that it stays
hand-edited, and the record's job is to make each edit a one-line citation rather than an
argument. The demotion protocol above is unchanged and stays the same-day path.

What the record can say about a case, and when:

1. In the Prow jobs it says something only once the evidence store is armed on both — the
   nightly appends to it and the presubmit reads it (`EVAL_BASELINE_STORE` exported in both;
   the two-export contract is the comment above that variable in the script). Until then
   every case reads `record: none` and the column is absent, unless evidence has been landed
   by hand in `bench/baselines/`, which the reader treats the same way.
2. It says `would admit` or `would demote` only for a case with a full window at the
   current version key. At `EVAL_REPETITIONS=3`, the script's default the nightly inherits,
   that is seven nights from an empty store, and seven nights again after any version-key
   bump (a new agent or judge model, a `fleet` or `verifiers` bump); in between it reads
   `collecting n/20`, and after a bump, `stale (key …)`.
3. A nightly killed at its deadline records only the units that finished, so a case queued
   late can fall behind the count the calendar suggests; read the column rather than
   counting nights.
4. The BigQuery `admission_state` view over the store (`bench/dashboard/dashboard.sql`, not
   the HTML dashboard under `scripts/eval_dashboard/`) computes the same verdict from the
   same evidence and knows nothing of the list. Where it and the column disagree, one of
   them is reading a different key, and that is worth a look before citing either.

A roster pull request in either direction quotes the cell. A disagreement between the list
and the record that nobody has acted on — a listed case reading `would demote` for a week,
an unlisted one reading `would admit` — is a roster edit somebody owes, and the Cases page
shows the same evidence for anyone to raise it. Switching `EVAL_ADMISSION_MODE` to `record`
would make those edits automatic; that is a separate decision, and this page will say so
when it is taken.
