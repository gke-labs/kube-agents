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
alias with `no_parent_owners`, so a root approver does not count for either, and the inject
lane's `hack/eval/inject-lane-exclusions.txt` and `hack/eval/inject-lane-safeguards.yaml` with
them, since the merge-blocking presubmit runs on that lane (below). Nothing else carries the rule:
`hack/eval/nightly-cases.txt`, `hack/eval/api-lane-exclusions.txt`, a new case directory under `bench/tasks/` and the script itself need only the normal approvers
([#1546](https://github.com/gke-labs/kube-agents/issues/1546), decided 2026-09-15). This
page lives under `docs/` on purpose: the presubmit's step 0 (`hack/ci-revalidate.sh`) treats
`docs/` as inert (and the Prow path filter in `oss-test-infra` does today too), so a review finding
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
the roster's cases plus, since 2026-09-29, one documented exception: the held-out seat a
coverage tracker puts in the presubmit file without a roster line, one per tracker
([#2013](https://github.com/gke-labs/kube-agents/issues/2013) for the compliance canary;
[#2016](https://github.com/gke-labs/kube-agents/issues/2016)'s seat for pdb-remediation-pr took
its roster line on 2026-10-14).
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
  `EVAL_ADMISSION_RATE` bar, 0.90 since 2026-09-29 so that it is the same ≥ 90 % bar the
  held-out entries below hold a case to; the measurement it was set from is
  [on #1493](https://github.com/gke-labs/kube-agents/issues/1493#issuecomment-5892741229)),
  `collecting` for a partial one, `stale` for evidence only at a
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

Every presubmit case without a roster line is a held-out seat as described above, and the rest
are admitted (`python3 scripts/eval_rosters.py --summary` counts
`hack/eval/presubmit-cases.txt` and `blocking-roster.txt`; this page writes no count, so a seat
change doesn't edit it). The bar a case clears to
get there: its recent record shows failures only on its own regressions or on infra classes
the harness already excludes from the verdict. Every seat can red any pull request in the
repository, so five more rules sit on top of that record, under "The five rules a seat
meets" at the end of this section.

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
  (merged 2026-09-21) it is graded by `pull_request_opened`, which rejects a pull request last
  written before the run started; since #2260 the job empties the `*-infra` repository before
  every repetition ([#1755](https://github.com/gke-labs/kube-agents/issues/1755) item 2). Nightly since
  2026-09-22, and with pdb-remediation-pr's seat withdrawn (below) the remediation domain had
  no presubmit case, so `remediation` joined `fleet-audits` on the allowlist until
  pdb-remediation-pr's roster line on 2026-10-14 (below) took it off again; that case's
  demotion on 2026-10-17 (#3120) put it back until its return on 2026-10-26 (below). Enters
  when #1189's re-admission bar holds.
- **consistency-authorized-networks-probe** —
  [#3598](https://github.com/gke-labs/kube-agents/issues/3598): demoted 2026-10-21 after it
  failed about 30% of repetitions on the Gemini setup, on unrelated pull requests (#2997,
  #3255, #3321, #3392, #3395 and others) and on `main`'s eval-next, red on this case alone on
  2026-10-20: 37 of 126 repetitions across 42 runs, the store's record at 18/21,
  `would-demote`. 15 of the 37 were Vertex 429s
  ([#3399](https://github.com/gke-labs/kube-agents/issues/3399), fix open in
  [#3448](https://github.com/gke-labs/kube-agents/pull/3448)). The other 22 were the agent
  being wrong, not a narrow reference: seeded-c, which never enabled authorized networks,
  still returns `masterAuthorizedNetworksConfig: {gcpPublicCidrsAccessEnabled: true}` with no
  `enabled`, and the agent read that leftover block as configured or open and said seeded-c
  matches its peers, or named the wrong facet (NetworkPolicies, Pod Security, database
  encryption), or once, a tool error (a grep that missed seeded-c in its own saved cluster
  list). The rung-4 collapses include #2997 at 0/3; `main`'s red was rung 6. The Claude nightlies passed every scored repetition. Its removal leaves
  `consistency` with no presubmit case, so the domain is on the `domains.yaml` allowlist until
  it returns. Enters again when #3448 has merged, the agent-side fix (#3605) has landed, and the
  case passes the roster's bar, 90% over 20 runs, on the Gemini nightly (#3598).

**autoops-warning-event-triage** is no longer in the presubmit at all (tofu wall clock,
[#1218](https://github.com/gke-labs/kube-agents/pull/1218)); it runs and accrues its
record via the nightly tier ([#1175](https://github.com/gke-labs/kube-agents/pull/1175)).
Its original hold-out rationale stands —
[#1101](https://github.com/gke-labs/kube-agents/issues/1101): 0/5 graded repetitions on
record. It enters the roster when the lettered-options bar is settled and it has a clean
record.

**pdb-remediation-pr** ([#1079](https://github.com/gke-labs/kube-agents/pull/1079)), the
remediation domain's second writer, was on the roster from 2026-10-14
([#2016](https://github.com/gke-labs/kube-agents/issues/2016) step 4; the entry under
"Admitted on the record since the split" below has the record) until its demotion to the
nightly on 2026-10-17 ([#3120](https://github.com/gke-labs/kube-agents/issues/3120)), and is on
it again since 2026-10-26 (the first entry under that heading). How it got there, kept for
the measurements it cites: it held no presubmit seat before 2026-09-28. Its record —
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
it ran on every pull request, could not red one on rungs 4 or 6, redded one on rungs 1–3 like
every case, and built the record step 3 read at presubmit volume instead of one night at a time.
The Cases page read it as held out, undated, until the roster line. The bar step 3 set: three
clean days at ≥ 90 % of graded repetitions under `pull_request_opened` with no all-reps
collapse, infra classes the harness excludes not counted, read on the Cases page on either
tier it shows; the eval crew took the roster line on the writers nightlies of 2026-10-12 to
10-14 with the presubmit at 90.2 % in aggregate, and the dominant remaining miss (the worker
declining to open a pull request into a repository that holds no manifest for the workload) as
a known cost rather than a reason to wait. One miss shape the seat showed before #2260 was graded and counted against the case: a
job's three repetitions shared the leased repository, the sweep closed leftovers between leases
and not between them, and submit-suggestion derives its branch from the change, so repetitions 2
and 3 met repetition 1's open pull request. A repetition that pushed its own commit onto that pull
request passed (#1832 grades the head commit, which must be no older than the repetition's
start); one that only linked the sibling's pull request failed, and that miss was the case's own,
not infra. On the old record's two best nights three of six repetitions linked a pull request
they did not open (09-20: `evals-23-infra` #34 twice, then leftover #4; 09-19: `evals-6-infra`
#43, #46, then leftover #12), so a reading in the 50–67 % band is the isolation design
([#1755](https://github.com/gke-labs/kube-agents/issues/1755) item 3, closed undecided) before
it is agent regression. Step 3's choice was the third of counting it, grading repetition 1 only,
or sweeping between repetitions: since [#2260](https://github.com/gke-labs/kube-agents/issues/2260)
the job empties the repository before every repetition of a case that requests a pull request,
so the shape no longer arises and a reading in that band is regression.
`pull_request_opened`'s `accepts_stream_pull_request` is not a
fourth answer here: it widens only a fleet audit's stream, and this case has no ledger and
opens submit-suggestion branches. The roster edit of 2026-10-14 (step 4) took `remediation` off
the `docs/designs/domains.yaml` allowlist, where it had sat beside fleet-audits since 2026-09-22;
the demotion of 2026-10-17 (#3120) put it back, and the return of 2026-10-26 took it off again.

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

Three re-admissions on record: **agent-kanban-smoke** earned its seat back after the
2026-08-27 redesign (a real SRE question graded on `kanban_create` plus cluster names);
the reds that once argued for un-arming it belonged to the old vocabulary check. The second is
**upgrades-lagging-master-probe**, 2026-10-23, and the third **pdb-remediation-pr**,
2026-10-26, both below.

Admitted on the record since the split:

- **pdb-remediation-pr**, 2026-10-26
  ([#3120](https://github.com/gke-labs/kube-agents/issues/3120)), back from its demotion on
  2026-10-17 (rung-4 collapses on #2971, #3017 and #3329: the worker found checkout-gateway with
  no PodDisruptionBudget, then declined to open a pull request because the pool's `*-infra`
  repository held no manifest for it, or named an older pull request it had not opened).
  [#3145](https://github.com/gke-labs/kube-agents/pull/3145), merged 2026-10-23 00:36 UTC, seeds
  that manifest into every pool repository. The record is the writers nightly
  (`ci-kube-agents-eval-nightly-writers`, where the case runs because it opens a pull request),
  read from each build's `case-pdb-remediation-pr.json` and dated, as the dashboard's Nightly page
  dates it, by the build's UTC start (8 PM ET the evening before): 3/3 on each of the three nights
  since #3145, 2026-10-24, 10-25 and 10-26 (builds 2113782591684349952, 2114144983022833664 and
  2114507375523139584), all three under `spec.mode next`, the presubmit's mode and inject lane,
  since [#3783](https://github.com/gke-labs/kube-agents/pull/3783); 21 of 21 over the seven nights
  2026-10-20 to 10-26, every one since the agent model became `claude-opus-5-5-kubeagents-mcp`;
  and 37 of 41 graded repetitions over the fourteen nights 2026-10-13 to 10-26, every miss before
  #3145 and on the Gemini setup: 0/3 on 2026-10-18 and one repetition on 10-16, all the "names no
  pull request URL" shape #3145 addresses (10-16 also had one infra repetition). Median repetition
  172 s (142–265 s) over the nine since #3145. The four Claude nights before #3145 merged passed
  too (12/12), so the record does not separate #3145's effect from the model change's; either way
  the miss has not recurred in 21 repetitions. The store reads
  `collecting: 9/9 runs recorded at this key across 3 recorded run(s), 11 more needed` at the
  next-mode key, which the model change
  and then the move to next mode each reset (the last Gemini key reads `would-demote`, 16/20, on
  the pre-fix misses). So the citation is the nightly record, as for vcs-spent-branch-reuse below,
  and the eval crew's approval of this line is the call that it suffices before the window fills.
  The GitLab nightly passed it 21 of 21 on the seven nights that graded it, 2026-10-19 to 10-26.
  What it costs: like vcs-spent-branch-reuse it requests a pull request, so its three repetitions
  join that case's in the fan-out's second phase, one at a time after every other unit, each after
  the repository reset and on the inject lane the 120-second GitHub-write settle ([The inject
  lane](#the-inject-lane)): about 15 minutes more serial tail at the measured median (3 × (120 s +
  172 s)), plus three more resets. The two cases' units alternate there, so a late write from one
  would be charged to the other by the lane's write safeguard; the writers nightly has run them
  interleaved that way on the inject lane, with six other requesting cases, on all three next-mode
  nights, and neither tripped it (every repetition's catastrophic score 1.0). Taking the line took
  `remediation` off the `docs/designs/domains.yaml` allowlist.
- **vcs-spent-branch-reuse**, 2026-10-23, the source-control domain's first roster seat, moved
  from `hack/eval/nightly-cases.txt` into the presubmit file and onto the roster in one edit, on
  the record the writers nightly built: 42 of 42 graded repetitions over the fourteen nights
  2026-10-09 to 10-22, every night 3/3, no infra repetition, median repetition 173 s (113–656 s).
  The agent model changed from `gemini-3-1-pro-preview-kubeagents-mcp` to
  `claude-opus-5-5-kubeagents-mcp` on 2026-10-19, which reset the version key: the store reads
  `collecting: 12/12 runs recorded at this key across 4 recorded run(s), 8 more needed`, and the
  last Gemini key read `would-admit` (21/21 over the newest seven nights) on every night from
  2026-10-09 to 10-18. So the citation is the
  nightly record, 12/12 of it on the four Claude nights, and the eval crew's approval of this
  line is the call that it suffices before the window fills (expected with the 2026-10-25
  writers nightly). The same case under `spec.mode next`, the presubmit's lane, passed 18 of 18
  on `ci-kube-agents-eval-nightly-next-claude` 2026-10-16 to 10-21, with
  `the-forge-was-reached-without-a-cli` (a `worker_commands` check) set aside as blind on that
  transport, as it will be on the presubmit; and 15 of 15 on the five GitLab nightlies that
  graded, 2026-10-17 to 10-22. `deployer: noop`, and it seeds its own spent branch inside the
  run, so it reads no seeded cluster and no fixture: none of the fleet-drift exposure that
  demoted the upgrades and consistency probes. What it costs: it was the presubmit's one case
  that requests a pull request (until pdb-remediation-pr's return on 2026-10-26), so its three
  repetitions run in the fan-out's second phase, one at a time after every other unit, each
  after the repository reset and on the inject lane the 120-second GitHub-write settle
  ([The inject lane](#the-inject-lane)): about 15 minutes of serial tail at the measured median
  (3 × (120 s + 173 s)), plus the resets and the drain at the phase boundary. Its seat took
  `source-control` off the `docs/designs/domains.yaml` allowlist.
- **upgrades-lagging-master-probe**, 2026-10-23
  ([#3269](https://github.com/gke-labs/kube-agents/issues/3269)), back from its demotion of
  2026-10-16 (rung-4 collapses on #2997, #2959, #3282, #3271 and more). The fixture was as
  planted on every pool; the prompt asked for "what its release channel offers", and during the
  1.36 roll-out REGULAR's `upgradeTargetVersion` (1.35.8-gke.1439001) sat below the planted
  version while `defaultVersion` (1.36.4) sat above it, so an agent reading the target called the
  lagging cluster current: 66 of 141 presubmit repetitions passed on 2026-10-16 (34 of the 75
  misses read the target; the rest were other wrong verdicts, wording the check lacks, and rate
  limits), then nightlies of 2/3, 2/3 and 0/3 on 10-17 to 10-19.
  [#3338](https://github.com/gke-labs/kube-agents/pull/3338) (merged 2026-10-19) makes the
  prompt name the channel's default version. The three clean nights the bar asks for are
  `ci-kube-agents-eval-nightly` 2026-10-20 (build 2112333062724390912, kube-agents-evals-28),
  10-22 (2113057845506215936, evals-10) and 10-23 (2113420200605913088, evals-16), 3/3 each,
  every repetition comparing against `defaultVersion`; 10-21's build (2112695452917829632)
  uploaded nothing when the build cluster's GKE upgrade replaced its node (#3616) and counts
  neither way. The demotion's exit condition named an agent-side fix; the eval crew's approval of
  this line accepts the prompt fix in its place, with the agent's own wrong-verdict defect
  ([#2124](https://github.com/gke-labs/kube-agents/issues/2124)) still open. The three nights
  ran over the api transport: the main nightly moved to the inject door with
  [#3783](https://github.com/gke-labs/kube-agents/pull/3783) (its first such run is 2026-10-24),
  and the inject lane became the blocking one on 2026-10-21 (#3156), after the demotion. So under
  the #3338 prompt the case's inject-lane record is this line's own pull request,
  [#3899](https://github.com/gke-labs/kube-agents/pull/3899): `pull-kube-agents-smoke-test` build
  2113733214391504896 (2026-10-23, kube-agents-evals-10), 3/3, each repetition reading
  `defaultVersion`, in a GREEN run at 100 % admitted-case pass rate; its pre-fix record on the
  optional `-next` job was mixed (2/3 on 2026-10-15, build 2110588743994839040). Taking the line
  took `upgrades` off the `domains.yaml` allowlist.
- **pdb-remediation-pr**, 2026-10-14
  ([#2016](https://github.com/gke-labs/kube-agents/issues/2016) step 4), from the held-out
  seat it took on 2026-09-28. The three clean days the bar asks for are the writers nightlies
  of 2026-10-12, 10-13 and 10-14, 3/3 each (16 of 18 over the six nights before that), and the
  evidence store's newest-20 window stood at 19/21, reported as `would-admit` since 2026-10-13.
  The presubmit record behind it, since
  [#2263](https://github.com/gke-labs/kube-agents/pull/2263) landed, which empties the leased
  repository before each repetition: 1,882 of 2,086 graded repetitions passed (90.2 %); by UTC
  day, 10-09 298/322, 10-10 69/75, 10-11 12/15 with one all-reps collapse, 10-12 53/60,
  10-13 245/270, 10-14 101/106 to 14:53Z. Read on the presubmit alone the days do not run three
  clean in a row, 10-11 and 10-12 being under on 15 and 60 repetitions, and the eval crew's
  approval of this line is the call that the nightly reading and the aggregate suffice. Named
  so a reader can reproduce it, the cut: presubmit tier, UTC days, runs started at or after #2263's merge, 2026-10-03 02:32Z, `data.json` generated 2026-10-14 14:53Z, the 13 `MISSING` repetitions of the 2026-10-07 GitHub outage left out, which read 89.6 % with them in. What
  the remaining misses are: the worker finding checkout-gateway and declining to open a pull
  request because the `*-infra` repository holds no manifest for it; a 429 on the model; the
  worker citing an earlier repetition's pull request; two all-reps collapses in the window
  (PRs #2763, #2874), both the first shape. The terminal exit-255 class
  ([#2174](https://github.com/gke-labs/kube-agents/issues/2174), fixed by
  [#2738](https://github.com/gke-labs/kube-agents/pull/2738) on 2026-10-08) is absent from every
  artifact read after that merge. In the nightly from 2026-10-17, when that first miss shape
  collapsed it on unrelated pull requests (#3120), until its return on 2026-10-26 (above).
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
  too, until its roster line on 2026-10-14 (and again from its demotion on 2026-10-17, #3120,
  to its return on 2026-10-26). Same watch as the case above: a collapse on an unrelated pull
  request in its first days, and the demotion lever below.

### The five rules a seat meets

Decided on [#3968](https://github.com/gke-labs/kube-agents/issues/3968), section 5. A case
holds a line in `hack/eval/blocking-roster.txt` only when all five hold. Rules (a) and (b) are
judgments on the record, so the admission pull request shows them, in the roster-admission
block of [`.github/PULL_REQUEST_TEMPLATE.md`](../.github/PULL_REQUEST_TEMPLATE.md). Rules (c),
(d) and (e) can be read from the files, so `scripts/test_eval_rosters.py` checks them on every
pull request.

- **(a) A recorded red.** The case has failed for the right reason before: against `main`
  without the fix it guards, the red half of the loop in
  [`.agents/rules/eval_driven_development.md`](../.agents/rules/eval_driven_development.md). A
  case nobody has seen fail may be unable to fail. The pull request links that red run: the
  build or artifact, the check that failed and why, and one line of the agent's report. For an
  `expected_fail` case, the run that recorded it red counts.
- **(b) Every miss explained.** In the case's last 20 runs at the current version key, every
  failed repetition is classified. Either it is an agent regression with an issue, or it is an
  infra class the harness already sets aside (`rep_kind` in
  [`scripts/eval_dashboard/classify.py`](../scripts/eval_dashboard/classify.py)). This is on
  top of the `would-admit` reading at 90 % or better. The pull request lists each miss with its
  class and, for a regression, its issue. "None" is a complete answer when the window has no
  miss.
- **(c) A guarded fixture.** The seeded fleet stays, because it saves the presubmit from
  building clusters. But a case that reads it qualifies only when the precondition it grades is
  asserted by the hourly seeded-fleet scan (`scripts/eval_dashboard/fixture_state.py` running
  [`hack/fleet-fixture-state.py`](../hack/fleet-fixture-state.py)). The same goes for a case
  that writes to a pool `*-infra` repository. The test reads the case's `task.yaml` and fails
  when:
  - a `fixtures:` role has no `state` assertion in `bench/tf/fleet/fixtures.json`. A
    project-scoped role (`slot: null` in
    [`docs/designs/fleet-fixtures.yaml`](designs/fleet-fixtures.yaml)) is not on a cluster, so
    the scan never reads it;
  - a check names a seeded cluster (`seeded-b`, or `{cluster:b}`) and the case declares no
    fixture role on that slot;
  - the case requests a pull request, which writes to the pool's GitOps repository, and the scan
    asserts nothing there that the case depends on. A repository assertion is a `state` entry
    on the subject that `hack/fleet-fixture-state.py` names in its `REPOSITORY_SUBJECT`
    constant, so it counts only once the scan really reads pool repositories. It must sit on
    one of the case's roles, or anywhere in the catalog for a case that lists no role
    (`repository_asserted_roles` in the test).

  The admission pull request names the assertion that guards the case. If none exists, it adds
  one to the scan first.

- **(d) Deterministic grading.** At least one `objective` in the case's `verification_spec`
  is an exact check, not under a negating `none`. A case graded only by the judge does not
  qualify. The test catches the judge-only case. Whether the exact checks name the planted
  defect (the object the fleet's Terraform wrote, as
  [`docs/designs/bench-case-format.md`](designs/bench-case-format.md) "Exact check or judged
  score" asks) is for the reviewer.
- **(e) A wall-time budget.** `python3 scripts/eval_rosters.py --summary` prints an estimated
  presubmit wall time, priced from `unit_cost_hint` in `hack/ci-eval-pr.sh`. Phase one runs the
  cases that request nothing, four lanes at a time (`EVAL_TASK_PARALLELISM`), and one case's
  repetitions run one after another on its task lock. Phase two runs the cases that request a
  pull request, and the stopped asks, one unit at a time, after phase one is done, each after
  the 120 s write settle, so their time adds straight onto the job. Fifteen minutes of build and deploy go on top.
  Held-out seats count, because they run. Locks shared between cases are not counted, so a
  seat with its own Terraform stack would cost more than the estimate says. The test fails
  when the estimate passes
  `PRESUBMIT_WALL_BUDGET_MINUTES` in `scripts/eval_rosters.py`, which is 200. The hints are
  launch-order prices and run long: on 2026-10-26 the roster estimated 122 minutes, while
  recent green presubmits took 88 to 108. So 200 leaves room for one more seat that requests a
  pull request (a 1250 s hint costs about 69 minutes in phase two), and it stays well inside
  the 360-minute Prow deadline. That seat went to pdb-remediation-pr the same day
  ([#3964](https://github.com/gke-labs/kube-agents/pull/3964), hinted at 1250 s), which took the
  estimate to 190 minutes. An admission that goes over the budget either re-measures the hints
  it relies on or raises the budget in the same pull request, with the reason.

A seat that breaks (c) or (d) today is listed in `SEAT_BAR_EXEMPT` in
`scripts/test_eval_rosters.py`, each with an issue that says what would clear it. An entry names
the one part of the rule it covers (`c-role`, `c-slot`, `c-repository` or `d`), so a second
problem on the same seat still fails the test. That list is kept the way `HELD_OUT_IN_PRESUBMIT`
is: it does not grow to seat a new case, and the test fails an entry whose case now meets that
part, so the edit that fixes the case deletes the entry.

## How far the roster's promise reaches

The scope of "a held-out case cannot red a pull request" is rungs 4 and 6 only. Rungs 1–3
— a forbidden cluster mutation (or, on the inject lane, an unrequested GitHub write), an
erroring check, a record that is not a real run — stay
blocking for every case by design, admitted or not: `grade_case` evaluates them before it
reads admission. Those classes signal a broken case or install, not flake, and the fix is
on that side rather than on the roster.

Since 2026-09-22 those rungs reach only the cases the presubmit runs: the roster's cases and,
since 2026-09-29, the held-out seat. The seats of 2026-09-29 put both GitHub-writing paths back on
every pull request — the canary's (the minted token, the cloned `*-infra` workspace, the ledger
write, the `ledger_issue_contains` verifier) and pdb-remediation-pr's (submit-suggestion opening
a pull request on the leased `*-infra` repository, the `pull_request_opened` verifier) — so a
change that breaks either path is seen on the pull request that introduces it again: an
erroring verifier or an empty record is a rung-1–3 red for every case, admitted or not. A
graded miss on the canary's seat (no ledger URL or the planted binding skipped) is reported as
UNSTABLE and "(held out)" and blocks nothing; pdb-remediation-pr's (no pull request URL in the
final answer) arms rung 4 like any roster case, as it did from 2026-10-14 until its demotion to
the nightly on 2026-10-17 (#3120) and again since 2026-10-26. From 2026-10-17 until 2026-10-23
the submit-suggestion path was not in the presubmit at all; since then `vcs-spent-branch-reuse`,
on the roster, opens two pull requests a repetition through it on the leased `*-infra`
repository, so a change that breaks that path is a rung-4 red on the pull request that introduces
it, or a rung-1–3 red when the verifier errors. Since 2026-10-26 a remediation is graded there
too: a pdb-remediation-pr fix that never becomes a pull request is a rung-4 red. What
`rca-remediation-pr` alone grades, the RCA delegation chain ending in a pull request, is still
nightly-only, so it is first seen by the next nightly that finishes grading. The eval crew took
that trade with the policy; what makes a break on the ledger path block is the canary's roster
line (#2013 step 4), and until it holds a pull request that touches that path should still say
what it ran by hand.

## The whole-suite rate

Beside the per-case rungs the verdict carries one number for the whole pull request: the pooled
pass rate of every roster case's scored repetitions — on the presubmit, the roster less
`agent-kanban-smoke`, which is off the inject lane ([below](#the-inject-lane)), and on the today
lane the whole roster, each case at three repetitions (`python3 scripts/eval_rosters.py --summary`
has the roster's count), `infra` repetitions left out — against `main`'s rate over the same cases from the evidence store (the
newest seven nightly lines per case). It is the rule that can see a pull request
which made several cases a little worse without making any one of them fail three times. The
version key carries the mode ([`bench/baselines/README.md`](../bench/baselines/README.md)), so the
presubmit's `next` is compared only with lines a next-mode run on `main` recorded at its key, and
the today lane's `today` only with today's; a case with no line at its key contributes nothing to
`main`'s side, with one exception below. The main nightly (`ci-kube-agents-eval-nightly`, its
`-main-b` half and its `-writers` part) is the next-mode run on `main` that records, on the presubmit's agent model. A case
with no line at its next key (one the inject lane excludes, or whose window has not filled) falls
back, for this number only, to its today-mode lines at the same key otherwise, which the store
already holds (no job appends them now), and the verdict names those cases in a note; rung 6
never falls back. The verdict prints it on every
run:

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
sample contains); the eval dashboard's Trend page draws today's window from the same store, per case
and per domain, and since no job appends that window now, the view empties as its lines age out of
the page's window. It does not draw the `next` series yet, which is the window the presubmit is compared
with since the nightly grades `next`; the `next` block of the published `trend.json` holds it.

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

`pull-kube-agents-smoke-test` grades a `spec.mode: next` install, the mode a new install gets, so
its matrix goes through the A2A gateway's inject door — the harness's `AGENT_TRANSPORT=inject`,
which `hack/ci-eval-pr.sh` exports under `EVAL_MODE_NEXT=1`
([`docs/designs/eval-next-transport.md`](designs/eval-next-transport.md), "The CI flag"). The
roster above is still the roster there: nothing in the two roster files changes, and every case
not named in the lane's exclusion list (below) runs and can red the job. Three things differ from
the api transport, and they are kept apart on purpose.

`today`, the mode a rollback lands on, is graded over the api transport by the today lane: the
optional `pull-kube-agents-smoke-test-today` (on demand with `/test`, and on its own when a pull
request touches `hack/rollback-roundtrip.sh` or the operator's A2A reconciliation) and the
six-hourly periodic `ci-kube-agents-eval-today` on `main`. Both run with `EVAL_ROLLBACK_LANE=1`:
the deploy brings the install up under `next`, `hack/rollback-roundtrip.sh` takes it to `today`,
back to `next` and to `today` again before the matrix, and a rollback that fails reds the run.
The matrix is the presubmit's, roster included, so a case graded only over the api transport is
graded there, but neither job blocks a merge.

A check the transport blinds is the scorer's business. The door's trace carries no card ids,
no worker's entries and no results. The delegation wait now reads the cards and the workers from the pod
when the turn ran under the bridge's `api` executor, but the scorer still treats
`worker_commands`, `worker_agents`, `worker_budget`, `report_times_in_window`, a `tool_called` in the `workers` or `all` scope, and a
`tool_called` with a `result` pattern as blind on
every inject record, and a door that shows no tool-call trace leaves a `tool_called` in its
default `router` scope nothing to read; `bench-gate` sets
those entries aside as not applicable on that transport and grades the rest, and a case whose
only objectives are of that kind is reported `NOT_GRADED_ON_TRANSPORT` rather than collapsed —
evaluated, outside the pass rate, never weather
([`docs/designs/eval-scorer.md`](designs/eval-scorer.md), "The inject lane sets aside what its
transport cannot show"). No roster edit is involved. The half of the rule for a router-scope
`tool_called` without a `result` pattern retires on a record whose door showed the whole trace (the transport's `a2a.activity` marker,
reporting no loss), whether or not the run made a call. The delegation wait for this path is
rebuilt, but the worker half stays until `scoring.py` takes those entries out of
`worker_blind_checks` for a record that carries the workers.

A premise the transport removes is the lane's exclusion list,
`hack/eval/inject-lane-exclusions.txt`. `agent-kanban-smoke`'s objective is the default profile
filing a `kanban_create` and the specialist's answer coming back to the thread. Under the bridge's
default `api` executor the default profile answers the door's task and can file the card, but the
card's completion never reaches the A2A thread. The lane's delegation wait reads the card's
result from the pod and appends it to the graded answer, but that is the harness's view, not the
thread's, so grading it would pass a premise the case does not have there. The card-wake
cases replay a wake the harness sends on the api transport only, and error on this lane.
`hack/ci-eval-pr.sh` drops the listed cases from the matrix on that lane only, and says so in
the log; on the api lane the file changes nothing. Each entry carries its reason as the comment
block above it, naming the issue that decides when it goes, and `scripts/test_eval_rosters.py`
holds every entry to that, the way the validator's `FIXTURE_NOT_READY` holds a case with no
fixture to an issue. The case keeps its seat in `presubmit-cases.txt` or `nightly-cases.txt` and
on the blocking roster, but it stops running wherever the inject lane runs, the nightlies
included. The only api-transport job on `main` is the today lane, which runs the presubmit
matrix: a listed presubmit case still runs there, and a listed nightly case runs nowhere in CI.
An entry also leaves what can red a pull request, because the merge-blocking presubmit is on the inject lane: `hack/ci-eval-pr.sh` drops it
from the matrix and from `BOOTSTRAP_ADMITTED` there. `agent-kanban-smoke` is the one roster case
listed, so until the lane's delegation wait reads card ids on this path, or the case is rewritten
for a premise both transports have, no merge-blocking run grades delegation through the chat
front door; the today lane does. `scripts/test_eval_rosters.py` pins
that set, so listing a second roster case is a decision made in that test and on this page, not a
side effect of an exclusion entry.
The file needs an `eval-crew` approval, as `presubmit-cases.txt` does: it changes what every
pull request runs.

Its mirror, `hack/eval/api-lane-exclusions.txt`, names the cases only the inject door can carry,
today the stopped asks (a `stop` sent on the conversation after the opening turn,
[`bench/kube_agents_bench/stop_hook.py`](../bench/kube_agents_bench/stop_hook.py)), and
`hack/ci-eval-pr.sh` drops them on every other transport. None is on the roster, so the file
needs only the normal approvers. On the inject lane a stopped ask runs in the second phase with
the cases that request a pull request, so a stop that fails and lets its card write reds only
that case.

A safeguard the lane needs and no case declares is the lane's safeguards list,
`hack/eval/inject-lane-safeguards.yaml`. The door's task can reach the platform persona, whose
own rule for a change is a pull request (through a card the Planning Agent files, under the
bridge's default `api` executor the lane runs), and a case's cluster safeguards see nothing of
GitHub; the file
holds one entry, a none-wrapped `github_writes` at catastrophic severity, which
`hack/ci-eval-pr.sh` appends on that lane to a copy of every task file in the matrix before
devops-bench reads it, so a repetition that wrote a pull request or branch the case did not
request is a tripped catastrophic safeguard at rung 1, as a cluster mutation is. The task files
under `bench/tasks/` and the api lane are untouched. A case that requests a pull request (a
`pull_request_opened` or `pull_request_diff_contains` check of its own) is allowed the ones its
reply names, and because the fan-out runs cases side by side against one repository, the script
runs those cases in a second phase after every other unit has finished (on both lanes since
#2260, for the repository reset's sake), one at a time and on the inject lane each
after a settle as long as the check's clock-skew tolerance, so a repetition that requests
nothing never shares the repository with one that writes by design and no window reaches back
into the unit before.
`scripts/test_eval_rosters.py` pins the file's shape, that no case reuses an entry's name, and
the set of requesting cases. The same approvers as the exclusion list: an entry is a rung 1 check
on every pull request.

Rung 6 compares an inject run only with next-mode history. The version key carries the mode
([`bench/baselines/README.md`](../bench/baselines/README.md)), and `bench-gate` reads
`AGENT_TRANSPORT=inject` as `next`, so an inject run's judged mean is set against the means a
next-mode run on `main` recorded at the same key, never against today-mode history: that
comparison measured the lane rather than the pull request, and redded four admitted cases at once
on 2026-10-17 with nothing changed on their side. Where no next-mode line exists at the key, rung 6
is quiet; a partial window is compared, as it is for `today`. The today lane reads today's history
only, which no job appends to now, so rung 6 there is quiet once the key moves past the stored
lines. The key includes the agent model, and the main nightly runs the presubmit's, so its
next-mode lines are the presubmit's rung 6 history.

One thing about the lane's install differs too. That write safeguard
(`no-github-writes-the-case-did-not-request`) charges every pull request opened in a repetition's
window to the case in flight, and the fleet audits' automatic remediation sweep opens one on its
own wherever a pool repository declares a workload with a `major` finding on the sweep's list, the
fresh install's first-run chain included. So `hack/ci-deploy.sh` installs the lane's agent with
`KUBEAGENTS_AUDIT_AUTO_PROMOTE=false` in `spec.deployment.env`: the audits still run and file
their ledgers, `finish` opens nothing nobody asked for, and a `/remediate` still works. The api
lane keeps the sweep on, where the job's repository reset closes what it opens ([#3439](https://github.com/gke-labs/kube-agents/issues/3439)).

## The GitLab lane

The GitLab eval job (`pull-kube-agents-smoke-test-gitlab`, `EVAL_FORGE=gitlab`,
[#2394](https://github.com/gke-labs/kube-agents/issues/2394)) deploys the agent against the leased
project's GitLab repository instead of its GitHub one, in a run of its own beside the GitHub
presubmit. On a pull request it runs `hack/eval/gitlab-cases.txt`, not
`presubmit-cases.txt`: registered cases that grade the forge, a `pull_request_opened` or
`pull_request_diff_contains` check (a merge request) or a `ledger_issue_contains` check (a ledger
issue), since a chat probe passes the same on either forge and the GitHub run already grades it.
Every entry is a registered case, from `presubmit-cases.txt` or `nightly-cases.txt`; the lane picks
from the catalogue and registers nothing. `hack/ci-eval-pr.sh` refuses an entry in neither file,
and `scripts/test_eval_rosters.py` pins the set and that each entry requires a positive forge check
(not negated, not one alternative under an `any`). Today that is three merge-request cases; no
ledger case passes three repetitions anywhere, so the ledger path is the harness's to prove: the
lease-time probe in [`docs/ci-pool-projects.md`](ci-pool-projects.md) 5.6, which says the forge
takes a ledger, not that the agent publishes one; that stays a case's job once one earns a line. The GitLab
nightly runs the same list once a night on its own leased project
(`ci-kube-agents-eval-nightly-gitlab`, #2832), and never appends to the evidence store, since a
case's record is per case, not per forge; nothing runs the cases this file leaves out against
GitLab. `ci-kube-agents-eval-nightly-gitlab-next` runs the same list under `spec.mode: next`
(`EVAL_MODE_NEXT=1`, through the inject door), so the forge is graded on both stacks; its runs
show in Prow only, as the dashboard collects the GitLab lane's today-mode nightly alone and the GitLab
lane never records.

The roster above is still the roster, intersected with the lane's list: a roster case in the lane
arms rung 4 there, and a held-out seat runs and reports without arming it, exactly as on the GitHub
presubmit. pdb-remediation-pr was on the roster from 2026-10-14
([#2016](https://github.com/gke-labs/kube-agents/issues/2016)) until its demotion to the nightly on
2026-10-17 ([#3120](https://github.com/gke-labs/kube-agents/issues/3120)), and while it was, its
collapse redded the lane's verdict. From then until 2026-10-23 no lane case was on the roster and
rung 4 was disarmed on the lane; `vcs-spent-branch-reuse` took its roster line that day and
pdb-remediation-pr took its own back on 2026-10-26, so both arm rung 4 there,
`vcs-review-feedback-read-back` is held out, and rungs 1 to 3 red the lane like every case. GitLab
runs on demand (`/test pull-kube-agents-smoke-test-gitlab`) and nightly, not as a blocking
presubmit: the agent goes through the same forge abstraction on both forges, so the GitHub
presubmit already grades what a pull request can break in the agent; what GitLab can break on its
own is the provider and the forge, which the nightly catches within a day and the on-demand run
checks for a pull request that touches the provider. The file needs the normal approvers, like
`nightly-cases.txt`: it changes what one on-demand lane and the two nightlies run, not what can red
a pull request.

## Demoting a flaky case

If an admitted case reds a pull request its diff cannot explain on a graded failure,
demote it: delete its line from `hack/eval/blocking-roster.txt` and from
`hack/eval/presubmit-cases.txt`, add it to the sorted section of `hack/eval/nightly-cases.txt`
with the issue as the `#` line above it (and its `DEMOTED_AFTER_THE_SPLIT` entry with `None`
as its predecessor in `scripts/test_eval_rosters.py`), and reference that issue. A held-out
seat has no roster name: it is withdrawn by moving its line back to that section, deleting
its `HELD_OUT_IN_PRESUBMIT` entry and listing it in `WITHDRAWN_TO_SORTED`. Demotion is a same-day edit to those
files — the files, not the Prow config, are deliberately the fast lever. It is the lever for rung-4 reds ONLY: a rung-1–3 red (a mutation, an
erroring verifier, an empty record on a task that provisions nothing — a record whose
deployer died before any agent ran grades INFRA and reds nobody) does not stop when its
case leaves the list.

A broken seeded-fleet fixture is not a reason to demote. After it writes the fleet kubeconfigs,
`hack/ci-eval-pr.sh` runs `hack/fleet-fixture-state.py` against the leased project. A case
whose grade depends on a role that scan read and found out of its designed state, after the pool
verifier's convergence wait and on the hourly scan's last two passes too (a role a
`fixture_role:` check reads, or the case's only `fixtures:` role) is not run. Each of its repetitions is recorded as infrastructure under `fixture-drift:<role>` and
the scan's own words, and the suite leaves the case out of its coverage floor, so it cannot
red or hold a pull request ([the scorer](designs/eval-scorer.md#the-verdict-ladder)). The
dashboard classes the case `fixture-drift`, the nightly files no `nightly-red` issue for it
(three such nights in a row file the infra owner's kept-from-running issue, one per case), and the fixture itself is the [seeded-fleet scan](ci-health.md#the-seeded-fleet-scan)'s to
report. A role the scan could not read, a scan that failed, a pull request that changes the
fleet catalog, the scan or the case itself: each leaves the case graded as usual.

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
reads that phrase from those bullets for the case's "demoted" pill. That issue goes to the case's effective owner — its `owner:`, or
its domain's owner ([`bench/CONTRIBUTING.md`](../bench/CONTRIBUTING.md#ownership)) —
who investigates and either fixes the case or proposes retiring it. A case whose owner does
not answer stays demoted. The bar is the same for a contributed case and an in-house one;
[`bench/CONTRIBUTING.md`](../bench/CONTRIBUTING.md) is what a contributor signs up to.

A demotion that leaves a domain with no case on the roster must, in the same pull request, do one
of three things: seat a stand-in case from that domain on the roster; keep the demoted case on the
presubmit as a held-out seat (its line in the last section of `hack/eval/presubmit-cases.txt` and
its `HELD_OUT_IN_PRESUBMIT` entry), which keeps the domain exercised but does not cover it; or add
a record to the `allowlist` in [`docs/designs/domains.yaml`](designs/domains.yaml) with `since`,
`issue`, `owner` and `expires`, the expiry at most 14 days after `since` unless a `why:` says why.
The issue is the one that says how the domain gets a roster case back. A held-out seat still needs
the record, with the case named in its `held_out:` field. `scripts/test_domain_coverage.py`
enforces this: it fails on an uncovered domain with no record, and on a record that is missing a
field or runs past the 14-day cap without a `why:`.

Once a record's `expires` date has passed (by the UTC date), pull requests are not blocked: a date
passing would red contributors who do not own the gap. Instead the CI-health bot escalates to the
record's owner on its issue, and the daily digest says how many days ago the exception expired.
`scripts/domain_allowlist.py` is the expiry rule it reads. The owner either covers the domain or
moves the date in a pull request that says why.

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
   bar below the noise floor demotes cases for weather. Read once so far, on 2026-09-29
   ([#1493](https://github.com/gke-labs/kube-agents/issues/1493)): over the four full-matrix
   nights since oss-test-infra#2707, 20 of the 45 cases with all four nights moved 0 points
   and 31 moved at most one repetition, so the bar moved from 0.95 (19 of 20, a number nobody
   had chosen from data) to 0.90, the bar the entries above already hold a case to. At 0.95
   the record would have demoted 6 of the 12 roster cases (17/21, 19/21, 18/21, 18/21, 18/20,
   16/20); at 0.90 it demotes 4, and the two that flip are the ones one miss short of the
   old bar. This item is not "holds" yet: one reading, and the four that remain below the
   bar are not one story — the two crashloop cases miss this week only, capacity misses in
   both weeks, and upgrades-lagging-master-probe's misses are all from 09-20 to 09-25 with
   12 of 12 since.

Switching before 2 holds leaves a listed case with no full window with the list (the list
is the fallback in `record` mode) but hands every full-window case to the record the same
day, with no column read first. Switching after 2 holds changes only the cases whose
**Record says** and **Admitted by** columns disagree, and the point of the columns is that
the crew has already seen every one of those before the switch.
