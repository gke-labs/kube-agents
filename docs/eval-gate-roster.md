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

The roster is a transition bridge, not a destination. `bench/baselines/` ships empty, so
no case is admitted by measured evidence and nothing can reach the collapse rung — which
would mean the presubmit blocks on nothing for as long as screening takes. Cases named in
`BOOTSTRAP_ADMITTED` keep their old blocking behaviour meanwhile: a bootstrap-admitted
case arms rung 4 but leaves rung 6 quiet and contributes nothing to main's side of the
aggregate, because it has no measured evidence to contribute. Screening replaces it — see
[`docs/designs/eval-scorer.md`](designs/eval-scorer.md) for computed admission and
[`docs/designs/testing-strategy.md`](designs/testing-strategy.md) §4.2 for the verdict
ladder the rungs below refer to.

The variable is comma- or whitespace-separated task ids; `_bootstrap_admitted()` in
`bench/kube_agents_bench/gate.py` accepts either.

## The admission bar, and who clears it

Eleven of the twenty active cases are admitted (recount the uncommented entries in the
script's `TASKS` array rather than trusting this sentence — an earlier copy of it
miscounted twice): the ones whose recent record shows failures only on their own
regressions or on infra classes the harness already excludes from the verdict.

The rest still run and report on every pull request, and they cannot red one on a GRADED
failure. Five are held out with a filed issue naming the exit condition:

- **capacity-pinned-pool-probe** —
  [#1010](https://github.com/gke-labs/kube-agents/issues/1010): worker completes its card
  at fan-out ("Awaiting synthesis" as the final answer). The failure is correlated across
  repetitions when the agent chooses to fan out, so the collapse rule does not absorb it.
  Enters when the fix merges.
- **cluster-agent-healthy-workload-no-finding** —
  [#1100](https://github.com/gke-labs/kube-agents/issues/1100): the agent invents a
  finding on a healthy workload ~1 run in 8. Main's own trait, so a collapse would tax an
  innocent PR. Enters when the false-positive rate drops or when rung-6 screening can
  compare against main.
- **autoops-warning-event-triage** —
  [#1101](https://github.com/gke-labs/kube-agents/issues/1101): 0/5 graded repetitions on
  record; admitting it reds every pull request today. Enters when the lettered-options bar
  is settled and it has a clean record.
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

A demoted case keeps running and reporting; give it a hold-out entry above with the issue
that names its re-admission condition.
