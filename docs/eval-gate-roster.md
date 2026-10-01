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
carries the rule: `hack/eval/nightly-cases.txt`, `hack/eval/inject-lane-exclusions.txt`,
`hack/eval/inject-lane-safeguards.yaml`, a new
case directory under `bench/tasks/` and the script itself need only the normal approvers
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
([#1023](https://github.com/gke-labs/kube-agents/issues/1023)), so `presubmit-cases.txt` holds
the roster's twelve cases plus, since 2026-09-29, one documented exception: the held-out seat a
coverage tracker puts in the presubmit file without a roster line, one per tracker
([#2013](https://github.com/gke-labs/kube-agents/issues/2013) for the compliance canary,
[#2016](https://github.com/gke-labs/kube-agents/issues/2016) for pdb-remediation-pr).
Such a seat runs on every pull request, cannot red one on a graded failure under the default
`roster` mode below (a switch to `record` mode would let the record admit it), and earns its
record at presubmit volume; `scripts/test_eval_rosters.py` pins the set (`HELD_OUT_IN_PRESUBMIT`)
and that the presubmit file minus those seats equals the roster. Every other held-out case is a
nightly case. Before that date the presubmit also ran held-out cases that reported without
blocking; the seven it was running moved to `hack/eval/nightly-cases.txt` that day, each with
its hold-out reason beside its line.

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

Twelve of the fourteen presubmit cases are admitted; the other two, the compliance canary and
pdb-remediation-pr, are the held-out seats described above (recount the entries of
`hack/eval/presubmit-cases.txt` and
`blocking-roster.txt` rather than trusting this sentence — an earlier copy of it miscounted
twice). The bar a case clears to
get there: its recent record shows failures only on its own regressions or on infra classes
the harness already excludes from the verdict.

Every other case but those seats runs in the nightly only — one data point a night, at three
repetitions — and its admission is one pull request that adds its line to both presubmit files
and cites that record. Of the seven that left the presubmit on 2026-09-22, three are held out
with a filed issue naming the exit condition:

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
  not the diff's. 413 of 677 graded presubmit repetitions 2026-09-15 to 09-22. Nightly
  2026-09-22 to 2026-09-29; since 2026-09-29 seated held out in `presubmit-cases.txt`
  ([#2013](https://github.com/gke-labs/kube-agents/issues/2013) step 2) after the eval install's
  kanban cap (#2022, for the dispatcher-stall residual #2032) and the credential-proxy
  workspace-leak fix (#2069, for #2011) landed. It runs on
  every pull request, cannot red one on rungs 4 or 6, and does red one on rungs 1–3 like every
  case. The Cases page's pill still reads demoted 09-02 (dated from this bullet) until the
  roster line. Enters the roster when #2013 step 3 holds: three clean days at ≥ 90 % of graded
  repetitions with no all-reps collapse, infra classes the harness excludes not counted (#1171
  closed 2026-09-08; the bar lives on #2013); that edit takes `fleet-audits` off the
  `domains.yaml` allowlist.
- **rca-remediation-pr** —
  [#1189](https://github.com/gke-labs/kube-agents/issues/1189): demoted 2026-09-02 evening
  after rung-4 collapses on six unrelated pull requests in one day. The suite's longest
  delegation chain, so it integrates over every environment fault in its window: the
  #1097 429 storms, the #1144 proxy EACCES (fix #1183), and #1184's gap (infra-blocked
  repetitions graded rather than classified) turn one dirty window into a correlated
  collapse. Its own record was 12/13 clean before the storms; 434 of 681 graded presubmit
  repetitions 2026-09-15 to 09-22. Since [#1780](https://github.com/gke-labs/kube-agents/pull/1780)
  (merged 2026-09-21) it is graded by `pull_request_opened`, which rejects a pull request an
  earlier lease left behind (and, since #2016 step 3, passes one an earlier repetition of the
  same job opened in the leased repository -- the in-job sibling rule described under
  pdb-remediation-pr below), and nothing sweeps the `*-infra` repositories between runs
  ([#1755](https://github.com/gke-labs/kube-agents/issues/1755) item 2). Nightly since
  2026-09-22, and with pdb-remediation-pr's seat withdrawn (below) the remediation domain had
  no presubmit case, so `remediation` joined `fleet-audits` on the allowlist; pdb-remediation-pr
  is back in the presubmit held out (seat opened 2026-09-28, below), which changes nothing here until
  its roster line. Enters when #1189's re-admission bar holds.

**autoops-warning-event-triage** is no longer in the presubmit at all (tofu wall clock,
[#1218](https://github.com/gke-labs/kube-agents/pull/1218)); it runs and accrues its
record via the nightly tier ([#1175](https://github.com/gke-labs/kube-agents/pull/1175)).
Its original hold-out rationale stands —
[#1101](https://github.com/gke-labs/kube-agents/issues/1101): 0/5 graded repetitions on
record. It enters the roster when the lettered-options bar is settled and it has a clean
record.

**pdb-remediation-pr** ([#1079](https://github.com/gke-labs/kube-agents/pull/1079)), the
remediation domain's second writer, has never held a presubmit seat either. Its record —
12/12 on the four graded nights 2026-09-16 to 09-20 (420–1153 s a repetition), after 11/15
across #1079's five presubmit runs (the misses traced to
[#1097](https://github.com/gke-labs/kube-agents/issues/1097) and
[#1590](https://github.com/gke-labs/kube-agents/issues/1590)) — is what a 2026-09-22
promotion cited, and the promotion was withdrawn before it merged: every repetition of that
record was graded by `report_contains`, which
[#1780](https://github.com/gke-labs/kube-agents/pull/1780) (merged 2026-09-21) replaced with
`pull_request_opened`, a check that rejects a pull request last written before the run
started; until the pool sweep (`hack/ci_sweep_agent_pulls.py`,
[#1832](https://github.com/gke-labs/kube-agents/pull/1832), merged 2026-09-25) nothing closed
the `*-infra` leftovers between runs
([#1755](https://github.com/gke-labs/kube-agents/issues/1755) item 2), so a correct but
byte-identical resubmission graded as a miss, the shape rca-remediation-pr showed on #1780's
own head (0/3 on a leftover), and the sweep runs between leases, not between one job's three
repetitions; and no graded run under the new check existed (the 09-22 nightly
died at the Prow deadline before grading). A seat on that record would have armed rung 4 on
a grader the record never saw. The record under `pull_request_opened` since: no pass on any
graded night, none of the misses the case's own. On 2026-09-24 (build 2103273400171499520) it
was 0/2 graded plus one infra repetition: the three failed on the context-less `kubectl` after
`get-credentials` ([#1968](https://github.com/gke-labs/kube-agents/issues/1968), fix #1977,
merged), the credential-proxy workspace leak
([#2011](https://github.com/gke-labs/kube-agents/issues/2011)) and the delegation-ceiling
residual, in that order, the last an infra class the harness excludes; the nights after (09-25
to 09-27, 0/3 each) failed on "no pull request URL", the #2011 shape, whose fix
[#2069](https://github.com/gke-labs/kube-agents/pull/2069) merged 2026-09-28, except one 09-25
repetition that linked a 2026-09-17 leftover, `kube-agents-evals-6-infra#38`, the shape the
sweep closes between leases. Its held-out seat in `presubmit-cases.txt` opened 2026-09-28
([#2016](https://github.com/gke-labs/kube-agents/issues/2016) step 2), the second held-out seat:
it runs on every pull request, cannot red one on rungs 4 or 6, reds one on rungs 1–3 like every
case, and builds the record step 3 reads at presubmit volume instead of one night at a time.
The Cases page reads it as held out, undated: it was never on the roster. Enters the roster
when #2016 step 3 holds: three clean days at ≥ 90 % of graded repetitions under
`pull_request_opened` with no all-reps collapse, infra classes the harness excludes not
counted. One miss shape the seat showed was the isolation design, and step 3 grades it: a job's
three repetitions share the leased repository, the sweep closes leftovers between leases and not
between them, and submit-suggestion derives its branch from the change, so repetitions 2 and 3
meet repetition 1's open pull request with the fix already on it, push nothing, and link it. The
seat's second live run (the smoke of the pull request that opened it) read 1/3 on exactly that,
and on the old record's two best nights three of six repetitions linked a pull request they did
not open (09-20: `evals-23-infra` #34 twice, then leftover #4; 09-19: `evals-6-infra` #43, #46,
then leftover #12), the 50–67 % band
([#1755](https://github.com/gke-labs/kube-agents/issues/1755) item 3, closed undecided). Step 3
chose a grading rule over counting the miss, grading repetition 1 only, or sweeping between
repetitions (a sweep needs `pull_requests: write` in the presubmit, which
[section 5.3 of the pool design](ci-pool-projects.md) withholds on purpose): since #2016 step 3
`pull_request_opened` passes a repetition either on its own head commit, no older than the
repetition's start (#1832's rule, recorded as `own-head-commit`), or on a pull request in the
leased project's `*-infra` repository created at or after this job's lease window began, opened
by a `[bot]` login (an App's, as the agent writes; the minter App's slug is not pinned anywhere,
so the check reads the kind of login, as the ledger reset does) on a `platform-agent/` branch in
the repository itself (the agent's pull request as the pool sweep and `github_writes` define it;
another App's, or one from a fork, is not) and open or merged -- this job's own, since the project
is leased to no one else -- recorded as `in-job-sibling`; the rule that
passed leads the check's reason and so sits in the run record (`results.json`), where the Cases
page can tell the two apart. A pull request an earlier lease left behind (created before the
window), one in another repository, one a person opened, and one closed unmerged still fail. The
window is the job's, not the case's: in the nightly every PR-writing case shares it and writes as
the same App, so a repetition passes on any in-window agent-authored pull request in the leased
repository it links, and the check cannot tell which case opened it. `hack/ci-eval-pr.sh` exports
the window's start (its own start, inside the Boskos lease and before any agent ran) and the
leased repository; a hand run has neither, and only the first rule applies. The roster edit (step 4, an eval-crew approval) takes
`remediation` off the `docs/designs/domains.yaml` allowlist. Until then the domain sits there
beside fleet-audits.

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
  compliance canary above) and, with pdb-remediation-pr's promotion withdrawn, `remediation`
  too. Same watch as the case above: a collapse on an unrelated pull request in its first
  days, and the demotion lever below.

## How far the roster's promise reaches

The scope of "a held-out case cannot red a pull request" is rungs 4 and 6 only. Rungs 1–3
— a forbidden cluster mutation (or, on the inject lane, an unrequested GitHub write), an
erroring check, a record that is not a real run — stay
blocking for every case by design, admitted or not: `grade_case` evaluates them before it
reads admission. Those classes signal a broken case or install, not flake, and the fix is
on that side rather than on the roster.

Since 2026-09-22 those rungs reach only the cases the presubmit runs: the roster's twelve and,
since 2026-09-29, the two held-out seats. Those seats put both GitHub-writing paths back on
every pull request — the canary's (the minted token, the cloned `*-infra` workspace, the ledger
write, the `ledger_issue_contains` verifier) and pdb-remediation-pr's (submit-suggestion opening
a pull request on the leased `*-infra` repository, the `pull_request_opened` verifier) — so a
change that breaks either path is seen on the pull request that introduces it again: an
erroring verifier or an empty record is a rung-1–3 red for every case, admitted or not. A
graded miss on either seat (no ledger URL or the planted binding skipped; no pull request URL
in the final answer) is reported as UNSTABLE and "(held out)" and blocks nothing.
`rca-remediation-pr` is still nightly-only, so what it alone grades, the RCA delegation chain
ending in a pull request, is still first seen by the next nightly that finishes grading. The
eval crew took that trade with the policy; what makes a break on a GitHub-write path block is
a roster line for one of these seats (#2013 step 4, #2016 step 4), and until one lands a pull
request that touches that path should still say what it ran by hand.

## The whole-suite rate

Beside the per-case rungs the verdict carries one number for the whole pull request: the pooled
pass rate of every roster case's scored repetitions — twelve cases × three = 36 units, `infra`
repetitions left out — against `main`'s rate over the same cases from the evidence store (the
newest seven nightly lines per case, about 249 runs). It is the rule that can see a pull request
which made several cases a little worse without making any one of them fail three times. The
verdict prints it on every run today:

```
Admitted-case pass rate: 91.7% (main: 92.4%, margin 10.0%)
```

The margin is `EVAL_AGGREGATE_MARGIN`, default 0.10, measured on 2026-09-29 against 94 green
presubmit runs since 2026-09-26 and the four clean nightlies: the worst unchanged run fell 0.063
below `main` (five failed repetitions of 36; the 0.05 the rule shipped with would have redded it),
and 0.10 reds none of them while redding the seventh failed repetition at `main`'s rate that day,
the sixth once `main` sits near 0.94. The measurement and the two-proportion alternative are in
[the scorer design](designs/eval-scorer.md#sizing-the-aggregate-margin-measured-2026-09-29).

**It is advisory until armed, and arming is one line in Prow.** With `EVAL_AGGREGATE_ARMED`
unset — the script's default, pinned by `bench/tests/test_gate.py` — a rate below the margin is a
note in the verdict, not a red. To arm it, add one line to `pull-kube-agents-smoke-test`'s
script in `prow/prowjobs/gke-labs/kube-agents/kube-agents-presubmits.yaml` of
`GoogleCloudPlatform/oss-test-infra`, beside the existing `export EVAL_BASELINE_STORE=...` line
(the job has no `env:` block; every `EVAL_*` setting is an `export` in its `bash -c` script):

```bash
export EVAL_AGGREGATE_ARMED="1"
```

Nothing in this repository changes for the flip, and the same line removed disarms it. The nightly
periodic does not get the line: it records `main` and grades itself against a window that already
holds its own night, so its aggregate is a report, never a gate. Revisit the margin when `main`'s
window rate passes 0.96 (at that point 0.10 starts redding five failed repetitions, which the
sample contains); the eval dashboard's Trend page draws that window from the same store, per case
and per domain, so it is the place to watch for it.

**What an author sees when it fires.** No case is marked blocking; the failures are spread. The
job's final log line is the ordinary `PR Smoke Test Evaluation Failed -- see .../eval-verdict.md`,
and `eval-verdict.md` opens:

```
**RED**

Admitted-case pass rate: 80.6% (main: 92.4%, margin 10.0%)

### Why it is red

- suite pass rate 0.806 is below main's 0.924 by more than the 0.100 margin (over 36 scored repetitions)
```

followed by the per-case table, where the failed repetitions sit under `Passes` as `2/3` on
several rows and each failing repetition's reason and the agent's report are quoted below it.
Because the sample said seven failed repetitions of 36 is beyond what an unchanged pull request
produces, the first move is the same as for any red: read the quoted reasons. If they are the
familiar phrase-match misses spread over unrelated probes, rerun once; a second red at the same
rate is a finding against the change. `main`'s side of the line is the same number the eval
dashboard's Trend page draws from the store (`docs/ci-health.md`), so a rate that looks wrong can
be checked there, and a `main` window that has itself slipped is a nightly problem to fix on
`main`, not a reason to widen the margin. A case that is dragging both sides down (on
2026-09-29 `upgrades-lagging-master-probe` was 52 of the sample's 116 failed repetitions) is
handled under [Demoting a flaky case](#demoting-a-flaky-case), which raises `main`'s rate and
tightens this rule at the same time.

## The inject lane

When a run's matrix goes through the A2A gateway's inject door — the harness's
`AGENT_TRANSPORT=inject`, which `hack/ci-eval-pr.sh` exports under `EVAL_MODE_NEXT=1`
([`docs/designs/eval-next-transport.md`](designs/eval-next-transport.md), "The CI flag") — the
roster above is still the roster:
nothing in the two roster files changes, and every case not named in the lane's exclusion list
(below) runs and can red the job exactly as on the api lane. Two things differ, and they are
kept apart on purpose.

A check the transport blinds is the scorer's business. The door's record carries no card ids
and no worker's entries, so `worker_commands`, `worker_agents` and a `tool_called` in the
`workers` or `all` scope see nothing there, and a door that shows no tool-call trace leaves a
`tool_called` in its default `router` scope nothing either; `bench-gate` sets
those entries aside as not applicable on that transport and grades the rest, and a case whose
only objectives are of that kind is reported `NOT_GRADED_ON_TRANSPORT` rather than collapsed —
evaluated, outside the pass rate, never weather
([`docs/designs/eval-scorer.md`](designs/eval-scorer.md), "The inject lane sets aside what its
transport cannot show"). No roster edit is involved. The router-scope `tool_called` half of the
rule retires on a record whose door showed the whole trace (the transport's `a2a.activity` marker,
reporting no loss), whether or not the run made a call; the worker half stays until a later change rebuilds the
delegation wait for this path and takes those entries out of `worker_blind_checks`.

A premise the transport removes is the lane's exclusion list,
`hack/eval/inject-lane-exclusions.txt`. The door addresses `platform` directly, so a case that
grades the chat front door's own behaviour — `agent-kanban-smoke`, whose objective is the
default profile filing a `kanban_create` before a specialist answers — has no front door in its
path, and grading its answer alone would pass a premise the case does not have there.
`hack/ci-eval-pr.sh` drops the listed cases from the matrix on that lane only, and says so in
the log; on the api lane the file changes nothing. Each entry carries its reason as the comment
block above it, naming the issue that decides when it goes, and `scripts/test_eval_rosters.py`
holds every entry to that, the way the validator's `FIXTURE_NOT_READY` holds a case with no
fixture to an issue. An entry is not a demotion: the case stays in `presubmit-cases.txt` and on
the blocking roster, runs on every pull request over the api transport, and can still red one.
The file needs the normal approvers, not the eval-crew rule, for the same reason
`nightly-cases.txt` does — it changes what one lane runs, not what can red a pull request.

A safeguard the lane needs and no case declares is the lane's safeguards list,
`hack/eval/inject-lane-safeguards.yaml`. The door addresses the platform persona, whose own rule
for a change is a pull request, and a case's cluster safeguards see nothing of GitHub; the file
holds one entry, a none-wrapped `github_writes` at catastrophic severity, which
`hack/ci-eval-pr.sh` appends on that lane to a copy of every task file in the matrix before
devops-bench reads it, so a repetition that wrote a pull request or branch the case did not
request is a tripped catastrophic safeguard at rung 1, as a cluster mutation is. The task files
under `bench/tasks/` and the api lane are untouched. A case that requests a pull request (a
`pull_request_opened` or `pull_request_diff_contains` check of its own) is allowed the ones its
reply names, and because the fan-out runs cases side by side against one repository, the script
runs those cases in a second phase after every other unit has finished, one at a time and each
after a settle as long as the check's clock-skew tolerance, so a repetition that requests
nothing never shares the repository with one that writes by design and no window reaches back
into the unit before.
`scripts/test_eval_rosters.py` pins the file's shape, that no case reuses an entry's name, and
the set of requesting cases. The same approvers as the exclusion list, for the same reason.

## Demoting a flaky case

If an admitted case reds a pull request its diff cannot explain on a graded failure,
demote it: delete its line from `hack/eval/blocking-roster.txt` and from
`hack/eval/presubmit-cases.txt`, add it to `hack/eval/nightly-cases.txt` with the issue as
the `#` line above it, and reference that issue. A held-out seat has no roster name: it is
withdrawn by moving its line back to `hack/eval/nightly-cases.txt` and deleting its
`HELD_OUT_IN_PRESUBMIT` entry. Demotion is a same-day edit to those
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
