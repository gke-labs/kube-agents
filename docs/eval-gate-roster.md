# The eval gate roster

[`hack/eval/blocking-roster.txt`](../hack/eval/blocking-roster.txt) names the eval cases
that can red `pull-kube-agents-smoke-test`; [`hack/ci-eval-pr.sh`](../hack/ci-eval-pr.sh)
reads it at startup as the default of `BOOTSTRAP_ADMITTED`. This page is the prose that
used to sit above that export: what admits a case, which cases are held out and on which
issue, how far the roster's promise reaches, and how a flaky case is demoted. The list
itself stays in the file — edit it there, and keep this page in step. A roster edit merges
only with an `approved` from the `eval-crew` alias in [`OWNERS_ALIASES`](../OWNERS_ALIASES):
[`hack/OWNERS`](../hack/OWNERS) scopes `blocking-roster.txt` and
`hack/eval/presubmit-cases.txt` — what blocks and what runs on every pull request — to that
alias with `no_parent_owners`, so a root approver does not count for either. Nothing else
carries the rule: `hack/eval/nightly-cases.txt`, a new case directory under `bench/tasks/`
and the script itself need only the normal approvers
([#1546](https://github.com/gke-labs/kube-agents/issues/1546), decided 2026-09-15). This
page lives under `docs/` on purpose: the script's step-0 revalidation treats `docs/` as
inert (and the Prow path filter in `oss-test-infra` does today too), so a review finding
against this prose costs no eval run
([#1179](https://github.com/gke-labs/kube-agents/issues/1179)), which is exactly what
roster-comment edits used to cost.

## What the roster is

The roster is the blocking set, and it is hand-edited on purpose. A case named in
`BOOTSTRAP_ADMITTED` arms rung 4 — three failed repetitions red the job — and, while the
evidence store holds nothing for it at the current version key, leaves rung 6 quiet and
contributes nothing to main's side of the aggregate. Once the nightly has appended a
partial window for it (`collecting`), that evidence feeds both. A case not named here cannot
red a pull request on a graded failure, whatever its record says — and since 2026-09-22 it does
not run on one either: the eval crew decided that the presubmit runs the blocking roster only
([#1023](https://github.com/gke-labs/kube-agents/issues/1023)), so `presubmit-cases.txt` and
`blocking-roster.txt` hold the same thirteen cases, a held-out case is a nightly case, and
`scripts/test_eval_rosters.py` pins the equality. Before that date the presubmit also ran
held-out cases that reported without blocking; the seven it was running moved to
`hack/eval/nightly-cases.txt` that day, each with its hold-out reason beside its line.

**The record informs; it does not decide.** Decided 2026-09-14
([#1493](https://github.com/gke-labs/kube-agents/issues/1493)): nobody should be able to
move a case into or out of the blocking set without the eval crew knowing, and a roster
edit reviewed in a pull request is that knowledge. `EVAL_ADMISSION_MODE` in the script
selects who decides, and it defaults to `roster`:

- **`roster`** (default): the list decides, outright. `BaselineStore.admission()` in
  `bench/kube_agents_bench/baselines.py` still computes what the store would do and
  reports it per case — `would-admit` and `would-demote` for a full window
  (`EVAL_ADMISSION_MIN_RUNS` runs, default 20, at the current key, above or below the
  `EVAL_ADMISSION_RATE` bar), `collecting` for a partial one, `stale` for evidence only at a
  superseded key, `none` for nothing — and a roster edit cites that sentence. Nothing the
  nightly appends changes which cases block.
- **`record`**: the store decides once it holds a full window for a case, either way — a
  case at 21/21 is admitted whether or not it is named here, and a case at 12/21 is turned
  away even if it is — and the list is the fallback for a case the record cannot judge yet.
  Kept for a later decision; [Switching to `record`](#switching-to-record) below says what
  the record has to show first.

Once a store is configured, or the record holds a full window for any case (evidence landed
by hand in `bench/baselines/` counts), the verdict markdown carries two columns per case:
**Admitted by** — `bootstrap`, `none`, or in `record` mode `record` and
`record: not admitted` — and **Record says** — the five states above. The per-case JSON
hand-off carries the same as `admission_source`, `record_verdict` and `admission_mode`.
With `EVAL_BASELINE_STORE` unset and the checked-in directory empty, the columns are absent
and the presubmit's output is what it was before. See
[`docs/designs/eval-scorer.md`](designs/eval-scorer.md) for computed admission and
[`docs/designs/testing-strategy.md`](designs/testing-strategy.md) §4.2 for the verdict
ladder the rungs below refer to.

The variable is comma- or whitespace-separated task ids; `_bootstrap_admitted()` in
`bench/kube_agents_bench/gate.py` accepts either.

## The admission bar, and who clears it

All thirteen presubmit cases are admitted, because the presubmit file is the roster
(recount the entries of `hack/eval/presubmit-cases.txt` and `blocking-roster.txt` rather than
trusting this sentence — an earlier copy of it miscounted twice). The bar a case clears to
get there: its recent record shows failures only on its own regressions or on infra classes
the harness already excludes from the verdict.

Every other case runs in the nightly only — one data point a night, at three repetitions —
and its admission is one pull request that adds its line to both presubmit files and cites
that record. Of the seven that left the presubmit on 2026-09-22, three are held out with a
filed issue naming the exit condition:

- **cluster-agent-healthy-workload-no-finding** —
  [#1010](https://github.com/gke-labs/kube-agents/issues/1010): the delegation receipt is
  graded as the answer (51 of 156 recorded repetitions).
  [#1100](https://github.com/gke-labs/kube-agents/issues/1100) held this seat until its own
  sweep closed it — the agent invents nothing here, so the false-positive premise is gone
  but the reason for the hold is not. Still main's own trait, so a collapse would tax an
  innocent PR. #1010's fix ([#1174](https://github.com/gke-labs/kube-agents/pull/1174))
  merged 2026-09-03, but this case's record after it was never re-derived (566 of 674 graded
  presubmit repetitions 2026-09-15 to 09-22). Nightly since 2026-09-22; enters when a
  re-derived record clears the bar above.
- **compliance-rbac-overgrant** —
  [#1171](https://github.com/gke-labs/kube-agents/issues/1171): demoted 2026-09-02 after
  rung-4 collapses on unrelated pull requests (#1153 was red on this case alone). The
  fleet-audit delegation chain is degraded: audits go partial on what the agent reports as
  "access limitations", skipping check 2.4 (the cluster-admin-binding check this case
  grades), and some runs publish no ledger at all — so the collapse is the environment's,
  not the diff's. 413 of 677 graded presubmit repetitions 2026-09-15 to 09-22. Nightly since
  2026-09-22, and the fleet-audits domain's only presubmit case, so its move put
  `fleet-audits` on the `docs/designs/domains.yaml` allowlist
  ([#1876](https://github.com/gke-labs/kube-agents/issues/1876)). Enters when #1171's
  re-admission bar holds: delegation fixed and a clean 3-day graded record.
- **rca-remediation-pr** —
  [#1189](https://github.com/gke-labs/kube-agents/issues/1189): demoted 2026-09-02 evening
  after rung-4 collapses on six unrelated pull requests in one day. The suite's longest
  delegation chain, so it integrates over every environment fault in its window: the
  #1097 429 storms, the #1144 proxy EACCES (fix #1183), and #1184's gap (infra-blocked
  repetitions graded rather than classified) turn one dirty window into a correlated
  collapse. Its own record was 12/13 clean before the storms; 434 of 681 graded presubmit
  repetitions 2026-09-15 to 09-22. Since [#1780](https://github.com/gke-labs/kube-agents/pull/1780)
  (merged 2026-09-21) it is graded by `pull_request_opened`, which rejects a pull request last
  written before the run started, and nothing sweeps the `*-infra` repositories between runs
  ([#1755](https://github.com/gke-labs/kube-agents/issues/1755) item 2). Nightly since
  2026-09-22. Enters when #1189's re-admission bar holds.

**autoops-warning-event-triage** is no longer in the presubmit at all (tofu wall clock,
[#1218](https://github.com/gke-labs/kube-agents/pull/1218)); it runs and accrues its
record via the nightly tier ([#1175](https://github.com/gke-labs/kube-agents/pull/1175)).
Its original hold-out rationale stands —
[#1101](https://github.com/gke-labs/kube-agents/issues/1101): 0/5 graded repetitions on
record. It enters the roster when the lettered-options bar is settled and it has a clean
record.

Every case that is not in the presubmit runs in the nightly, since 2026-09-15 including
the nine that used to wait commented out in the script (the reasons each cannot take a
presubmit seat yet are beside its line in `hack/eval/nightly-cases.txt`). A new case lands
there by default and earns its presubmit seat — which is its roster seat — on the record
the nightly builds ([`docs/designs/bench-case-format.md`](designs/bench-case-format.md),
"Registration").

The eval dashboard's Cases page (`cases.html`, "How reliable is each test?") is
the readable view of that record: per case, the presubmit and the nightly pass
rate over repetitions at 7 and 30 days, kept apart — the nightly tier is the only
place a case outside the presubmit file runs at all — beside the case's roster status,
which it reads from `hack/eval/blocking-roster.txt` and from this page. The admission evidence itself is the
baseline store ([`bench/baselines/README.md`](../bench/baselines/README.md)); the
page shows the same nightly runs, it does not replace the store.

The other four of the seven are simply new and earn their record in the nightly like any
case, then enter: **security-overgrant-remediation-proposal**
([#1066](https://github.com/gke-labs/kube-agents/issues/1066)) and the three
obtainability activations from
[#1049](https://github.com/gke-labs/kube-agents/issues/1049)
(**obtainability-pdb-semantics**, **obtainability-fleet-exposure-sweep**,
**obtainability-healthy-namespace-silence**). Their presubmit records to the move (graded
repetitions 2026-09-15 to 09-22): 680/688, 600/685, 491/679 and 69/684 — the silence case
fails on a correct agent today, and the nightly record is what will show a fix landing.

One re-admission on record: **agent-kanban-smoke** earned its seat back after the
2026-08-27 redesign (a real SRE question graded on `kanban_create` plus cluster names);
the reds that once argued for un-arming it belonged to the old vocabulary check.

Admitted on the record since the split:

- **capacity-pinned-pool-probe**, 2026-09-22
  ([#1023](https://github.com/gke-labs/kube-agents/issues/1023)). Held out on
  [#1010](https://github.com/gke-labs/kube-agents/issues/1010) (the delegation receipt
  graded as the answer; fixed by
  [#1174](https://github.com/gke-labs/kube-agents/pull/1174), 2026-09-03), then kept out
  while its ceiling check redded correct answers on wording
  ([#1626](https://github.com/gke-labs/kube-agents/pull/1626), merged 2026-09-15 10:44 PM
  ET). Presubmit record from that merge to 2026-09-22 (`data.json`, presubmit tier): 196
  runs on 104 pull requests, 570 graded repetitions, 529 passed (92.8%), 41 failed, 18
  infra-excluded, 147 runs at 3/3 and no run with every graded repetition failed. Per UTC
  day: 09-16 105/111, 09-17 164/177, 09-18 95/104, 09-19 33/36, 09-20 8/9, 09-21 118/127,
  09-22 6/6. Nightly: 3/3, 2/3, 2/3, 3/3 on the four graded nights 09-16 to 09-20. What the
  41 misses are: 31 carry an excerpt and every one is the delegation acknowledgement or a
  blocked delegation delivered as the answer
  ([#1840](https://github.com/gke-labs/kube-agents/issues/1840),
  [#1874](https://github.com/gke-labs/kube-agents/issues/1874)); 30 of the 41 miss both the
  planted-pool name and the ceiling, 10 only the ceiling, 1 only the pool. The residual is
  mostly the platform's shape, not the case's own regression; the roster's operative metric
  is the collapse, and there were none in 196 runs.
- **pdb-remediation-pr**, 2026-09-22
  ([#1023](https://github.com/gke-labs/kube-agents/issues/1023)), the remediation domain's
  second case ([#1079](https://github.com/gke-labs/kube-agents/pull/1079)), moved from
  `hack/eval/nightly-cases.txt` into the presubmit file and onto the roster in one edit, on
  the record the nightly built: 12/12 on the four graded nights 2026-09-16 to 09-20
  (420–1153 s a repetition), after 11/15 across its five presubmit runs on #1079 (3/3, 2/3,
  1/3, 3/3, 2/3; the misses #1079 traced to
  [#1097](https://github.com/gke-labs/kube-agents/issues/1097) and
  [#1590](https://github.com/gke-labs/kube-agents/issues/1590)). It is the first case to
  take a presubmit seat straight from the nightly under the 2026-09-15 rule, and its
  presubmit record starts with this edit; a collapse on an unrelated pull request in its
  first days is the thing to watch, and the demotion lever below is the answer. One caveat
  the eval crew accepted with the seat: every repetition of that record was graded by
  `report_contains`, and since [#1780](https://github.com/gke-labs/kube-agents/pull/1780)
  (merged 2026-09-21) the case is graded by `pull_request_opened`, which rejects a pull request
  last written before the run started; the first presubmit runs under the new grader are the
  record to read.
- **incident-triage-oom-event-probe**, 2026-09-22
  ([#1023](https://github.com/gke-labs/kube-agents/issues/1023)), the incident-triage
  domain's presubmit-eligible probe
  ([#1625](https://github.com/gke-labs/kube-agents/pull/1625)), moved from
  `hack/eval/nightly-cases.txt` into the presubmit file and onto the roster in one edit, on
  the record the nightly built: 10/12 on the four graded nights 2026-09-16 to 09-20 (2/3, 3/3,
  2/3, 3/3; 529–2808 s a repetition at the nightly's parallelism 6), after 3/3 in its measured
  presubmit run (build 2099969322708373504, 737/599/1357 s). Both misses are platform bugs the
  case surfaced, not the case: on the first night the worker blocked on
  `cluster_agent_profile.py` in the sandbox
  ([#1840](https://github.com/gke-labs/kube-agents/issues/1840)); on the fourth the delegation
  acknowledgement was delivered as the final answer
  ([#1874](https://github.com/gke-labs/kube-agents/issues/1874), filed 2026-09-22, the
  [#1254](https://github.com/gke-labs/kube-agents/issues/1254)/[#1010](https://github.com/gke-labs/kube-agents/issues/1010)
  shape). Its seat took incident-triage off the `docs/designs/domains.yaml` allowlist; the
  same day's decision that the presubmit runs the roster only put `fleet-audits` on it (the
  compliance canary above). Same watch as the case above: a collapse on an unrelated pull
  request in its first days, and the demotion lever below.

## How far the roster's promise reaches

The scope of "a held-out case cannot red a pull request" is rungs 4 and 6 only. Rungs 1–3
— a forbidden cluster mutation, an erroring check, a record that is not a real run — stay
blocking for every case by design, admitted or not: `grade_case` evaluates them before it
reads admission. Those classes signal a broken case or install, not flake, and the fix is
on that side rather than on the roster.

## Demoting a flaky case

If an admitted case reds a pull request its diff cannot explain on a graded failure,
demote it: delete its line from `hack/eval/blocking-roster.txt` and from
`hack/eval/presubmit-cases.txt`, add it to `hack/eval/nightly-cases.txt` with the issue as
the `#` line above it, and reference that issue. Demotion is a same-day edit to those
files — the files, not the Prow config, are deliberately the fast lever. It is the lever for rung-4 reds ONLY: a rung-1–3 red (a mutation, an
erroring verifier, an empty record on a task that provisions nothing — a record whose
deployer died before any agent ran grades INFRA and reds nobody) does not stop when its
case leaves the list.

Nothing automatic demotes a listed case under the default `roster` mode, which is why the
manual edit is the lever. The record is the evidence for it: when the store holds a full
window for the case, the verdict's **Record says** column reads `would-demote` and the
case's `admission_reason` carries the numbers (`screened at 12/21 …, below the bar`) — cite
them in the demotion pull request. A case whose record still says `would-admit` when it
redded an unrelated pull request is the other thing to look at before demoting: three
correlated failures against a 21/21 window point at the environment, not the case. (Under
`EVAL_ADMISSION_MODE=record` the record decides once it holds a full window and the edit
changes nothing for that case: one night of three failures on `main` against a 21/21 window
reads 18/21, below the bar, and the case is turned away on the next presubmit — a one-night
de-admission window, with nobody in the loop.)

A demoted case keeps running and reporting in the nightly; give it a hold-out entry above
with the issue that names its re-admission condition, and date it as `demoted YYYY-MM-DD` inside its
`- **case-name** —` bullet, the shape the entries above use — the dashboard's Cases page
reads that phrase from those bullets for the case's "demoted" pill. That issue goes to the case's `owner:` in its
`task.yaml` — a GitHub login, or `maintainers` for the approvers in the root `OWNERS` file —
who investigates and either fixes the case or proposes retiring it. A case whose owner does
not answer stays demoted. The bar is the same for a contributed case and an in-house one;
[`bench/CONTRIBUTING.md`](../bench/CONTRIBUTING.md) is what a contributor signs up to.

## Switching to `record`

The list is not scheduled for deletion. `EVAL_ADMISSION_MODE=record` exists so that handing
the decision to the store stays a one-variable change if the eval crew ever decides to make
it; it would be set in the Prow job config, never as the script's default. The record has
to have shown all of the following first, and the decision is still a team one afterwards:

1. The evidence store is armed on both jobs: the nightly appends to it and the presubmit
   reads it (`EVAL_BASELINE_STORE` exported in both Prow jobs — the two-export contract is
   the comment above that variable in the script).
2. Every case on the list has a full window at the current version key: each reads
   `would-admit` or `would-demote` in the nightly verdict's **Record says** column. At
   `EVAL_REPETITIONS=3`, the script's default the nightly inherits, that is seven nights from
   an empty store, and seven nights again after any version-key bump (a new agent or judge
   model, a `fleet` or `verifiers` bump).
3. Those seven nights completed for every listed case. A nightly killed at its deadline
   records only the units that finished, so a case queued late can fall behind the count
   the calendar suggests; read the column rather than counting nights.
4. The BigQuery `admission_state` view over the store (`bench/dashboard/dashboard.sql`, not
   the HTML dashboard under `scripts/eval_dashboard/`) and the verdict's **Record says**
   column agree on which cases have a full window and which way it points. The view knows
   nothing of the list, so it can only agree once 2 holds — which is the point of checking
   it.
5. The night-to-night movement per case has been read off the store and
   `EVAL_ADMISSION_RATE` sits above it: two consecutive nights on the same `main` commit are
   the "run it twice, see how much it moves" calibration
   [`docs/designs/testing-strategy.md`](designs/testing-strategy.md) §4.2 asks for, and a
   bar below the noise floor demotes cases for weather.

Switching before 2 holds leaves a listed case with no full window with the list (the list
is the fallback in `record` mode) but hands every full-window case to the record the same
day, with no column read first. Switching after 2 holds changes only the cases whose
**Record says** and **Admitted by** columns disagree, and the point of the columns is that
the crew has already seen every one of those before the switch.
