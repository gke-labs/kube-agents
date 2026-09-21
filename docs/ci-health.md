# CI health: the presubmit gate adjudicator

Every 15 minutes `.github/workflows/ci-health.yml` refreshes the eval dashboard
(the incremental collect → render → publish that `hack/ci-dashboard-refresh.sh`
runs, split across two identities: `github-actions@kube-agents-prow` reads the
Prow archive, `eval-dashboard-publisher@kube-agents-prow` writes the bucket),
then `scripts/eval_dashboard/health.py` reads the `data.json` just collected,
decides whether `pull-kube-agents-smoke-test` is **GREEN**, **DEGRADED** or
**OUTAGE** and why, and writes `health.json` next to it — before the render, so
the dashboard's Brief bakes that verdict and its history (`render.py --health`,
`--health-history`; the checkout is fetched with full history for the Brief's
"what changed right before" block).
The Brief's "What the agent saw" also quotes the agent's own words:
`bench-gate case` prints the first 300 characters of each failing
repetition's final report under its grading line (`rep N report:`), the
collector keeps it as `reps[].excerpt`, and the Brief and the run page show
it beside the grader's reason (builds graded before 2026-09-15 carry none).
`scripts/eval_dashboard/post_health.py` tells `#kube-agents-ci-health` on Google
Chat — only when the state changes, plus one digest a day at 9 AM Toronto time,
plus one line, once per episode, when the gate is slow without being broken
([below](#a-slow-gate)), plus one when runs start waiting to be scheduled and
one when they stop ([below](#a-backed-up-pool)).
The digest also carries one line on last night's run of the nightly tier
(`--data`, the `data.json` the tick collected): the cases recorded, how many
passed all reps, partial and failed, what is newly failing against the night
before, and the wall clock, with a link to the dashboard's Nightly report
(`nightly.html`); a night Prow cut short, one still running, or no night
since the day before yesterday, says so instead of numbers.
`scripts/eval_dashboard/nightly.py` derives the line and the report from the
same nightly runs.
The same tick reads the eval evidence store (`gs://kube-agents-evals-bench/evidence`,
the records the nightly appends; `scripts/eval_dashboard/store.py`, best-effort
and incremental) and renders the dashboard's Trend page (`trend.html`,
`scripts/eval_dashboard/trend.py`): per case and per domain, the pass rate by
night with the admission window beside it, the judged quality by night with the
spread the store can support, and a marker on every night the version key
changed. The Cases page links each case to its trend and the Brief's last
incident links to the record around the night it started. What "score" means
there — the deterministic pass rate is the gate's number, judged quality is
advisory and never a single point — is defined once, in
[`docs/designs/eval-scorer.md`, "What a score is"](designs/eval-scorer.md#what-a-score-is).
A store read that would not fit its time budget stops early, keeps what it
fetched with a note on the page, and finishes over the next ticks. A store read
that fails, times out or finds no wall clock left after the collect
leaves the Trend page on its last good read, marked stale with the reason, and
every other page unaffected; a tick that cannot download that last read at all
(anything but a NotFound, retried three times) reads nothing, the page says the
store was not read for that tick, and the next tick recovers. The read reaches two weeks past the page's 90-day
window so the first drawn night's admission window is as whole as the gate's.
The same tick comments on each pull request whose run went red, whose
build node went away, whose run Prow killed at the deadline, or whose run
the suite could not evaluate (`gate_comment.py`), files the tracking issue a new
OUTAGE lacks or the one a build-cluster node loss or a seeded-fixture drift
owes its owner (`gate_issue.py`), and appends `health.json` to a history
feed. A second job in the same workflow, on its own hourly cron, scans every
CI pool project's seeded fleet for fixtures out of their designed state and
publishes `fixture-state.json` beside `health.json` ([The seeded-fleet
scan](#the-seeded-fleet-scan)); the tick reads it for the `fixture_drift`
condition and the digest carries one line on the latest scan. A
`workflow_dispatch` of the same workflow is the on-demand refresh button
(its `fixture_state_scan` input also runs the scan).

Most messages end with a deep link into the dashboard:
`index.html#since=<ISO 8601 UTC>[&until=<ISO 8601 UTC>][&cases=<comma-separated case ids>]&view=gate`
for an incident (`until` on the recovery message), `view=agent` for the
digest, and the bare `index.html#view=agent` for the slow-gate and pool notes,
which report no incident and so have no window to scope a link to. The two
that say the pool check itself is not reporting — `wait unknown` and `pool
check stopped` — link to the periodic's job history instead, the one place
that shows whether it has started running again. `queue clear` and the two
data-freshness messages carry no link: what they report is the absence of
something to show. The scope rides in the URL
fragment because the host's login redirect drops a query string and a browser
carries the fragment through the redirect.
The contract, and the older `?cases=…#gate` form the pages still read (it
opens the same page wherever its query survives), are in
[`scripts/eval_dashboard/SCHEMA.md`](../scripts/eval_dashboard/SCHEMA.md).

## Times

Every time a person reads — in a Chat message, the gate comment, the issue
title — is America/Toronto, written `7:30 AM ET` (`Sun 7:30 AM ET` where the
day matters), DST included. URL parameters, `health.json`, the state files and
the history feed stay ISO 8601 UTC. The digest hour is a Toronto hour
(`--digest-hour 9`, `--digest-tz America/Toronto` by default) and "once a day"
is a Toronto day: the state file's `last_digest_date` is the local date.
`--digest-tz` moves only the digest's clock; the times written into messages
and the `ET` label are fixed to Toronto.

The rules are the procedure the eval crew ran by hand through the week of
2026-09-01, written down as constants in `health.py`; each one cites the
incident it was tuned on. This page is what a reader of the Chat message needs;
the constants and their reasoning are in the script.

## States and what to do

**OUTAGE** — the same admitted case (or cases) failed every graded repetition on
3+ runs from 3+ pull requests inside 6 hours, those cases explain at least half
of the reds in that window, and the reds are at least half of the window's
concluded runs (#1278, #1171). Don't retest: the reds share a cause. The message
names the cases, the pull requests, and the tracking issue when
`case-notes.yaml` has one.

**OUTAGE** also when runs are being killed at the job deadline (#1894): 3+
runs on 2+ pull requests among those finishing in the last 2 hours concluded
`FAILURE` with no eval verdict after running to within 15 minutes of the
presubmit's 360-minute Prow timeout (the job's `timeout` in `oss-test-infra`'s
`kube-agents-presubmits.yaml`; `health.py` owns the copy, `post_health.py`
imports it, and the copies in `classify.py` and `gate_issue.py` are pinned to
it by tests), and were neither
lost pods nor conflicted merges. Nothing was graded, so nothing can pass; a
killed run that recorded some cases before Prow stopped it still counts, and
its run page and comment read as the kill's, with the finished cases listed.
The message says how many runs on how many pull requests, an issue is filed
for whoever owns the gate (below, "The tracking issue"), and recovery is 3
runs with a verdict — green or red — on distinct pull requests after the last
kill, because a red that graded proves the gate grades again (a NOT EVALUATED
red, which records `eval_verdict: RED` with no graded repetition, is not a
verdict here). It ranks below a
shared break (which names cases) and above every DEGRADED condition.

**DEGRADED** — lost pods (the build cluster lost the node under the job:
3+ runs that concluded `FAILURE` with no tasks and either a `NodeNotReady` pod
event or no build log at all, finishing within 30 minutes of each other, among
the runs of the last 2 hours, #1478; 8+ is announced as a build-cluster event,
and the cluster owner's issue below is filed on any new `lost_pods`
condition), a quota storm (15+
repetitions lost to 429s or empty records across 3+ pull requests among the
runs that finished in the last 2 hours, #1225 / #1214), a delegation-ceiling
wave (15+ repetitions across 3+ pull requests, among the runs that finished in
the last 2 hours, ended at the harness's delegation wait with the worker still
running, #1874; the dispatcher stall of #1879 is its usual cause, and it ranks
below a storm because a 429-starved worker hits the same wait), or setup
deaths (3+ runs that concluded `FAILURE` under 5 minutes with no tasks, on 2+
pull requests, in 2 hours, #1172; an aborted zero-task run is a superseded
push), or seeded
fixture drift (the hourly scan found the same fixture role out of its designed
state on the same pool project on two consecutive scans, or on 3+ projects in
one scan; #1550, below). A zero-task run
is at most one of a lost pod, a conflicted merge (below), a deadline kill
(above) and a setup death, in that order: a lost pod is never a setup death,
whatever its duration. When more
than one condition fires, the order
above decides which one the message carries; the others stay in the evidence.
For a storm, retest after the time the message gives; for lost pods, once new
jobs are progressing; for a delegation-ceiling wave, once workers are
finishing again (the gateway log in a run's artifacts says whether the
dispatcher stalled); for fixture drift, once the fleet owner has re-applied
the stack — a red on a case that depends on the drifted fixture, from a run
that leased one of those projects, is the fixture's, not the change's.

A pull request that will not merge into `main` dies in the same seconds with
no tasks and is not a setup death either (`merge_conflict` in SCHEMA.md,
#1608): the fix is the author's rebase, so it is neither an outage nor a
reason to retest. It is counted nowhere — not green, not red, not infra — and
the run page says to rebase.

A run collected before the collector recorded how a build ended (SCHEMA.md,
`has_build_log`, `pod_*`) is unknown and is never a lost pod. An unknown
`merge_conflict` defaults the other way and reads as a setup death, which is
what keeps the replay fixtures cut before the field valid. A run without the
`eval_verdict` key is unknown too, and is never a deadline kill: only a
recorded `null` is "no verdict".

**GREEN** — none of the above. No message of its own beyond the recovery that
announces it; the daily digest carries the last 24 hours' runs, greens,
PR-caused reds and infra reds (setup deaths, lost pods and deadline kills that
graded nothing are folded into the infra count), the typical run length and the typical wait before a run starts,
and the delegation-ceiling repetitions on a day that had any. `health.json`'s
`metrics` keeps the rest — green rate, wall clock p50/p90, `queue_wait_p50_s`
and whether it was read at all, the infra-rep rate, `setup_deaths`,
`lost_pods`, `deadline_kills`, `ceiling_reps`.

A case failing on exactly one pull request while passing elsewhere is that pull
request's problem and moves no state; the message lists it as "PR-caused".

Only presubmit runs reach these rules and the digest's numbers. `data.json`
also carries the nightly periodic's runs (`runs[].tier`, see
`scripts/eval_dashboard/SCHEMA.md`); a nightly has no pull request to count
towards a distinct-PR floor, and a nightly collapsing is a case's record on
`main`, not a gate incident.

## Hysteresis

A single bad tick does not change the state, and a single lucky green does not
end an incident. Entering a shared-break OUTAGE or a storm or delegation-ceiling DEGRADED needs the condition to be
current: one of the three newest completed runs carries it. Setup deaths,
lost pods, deadline kills and fixture drift have no such signature on a
completed run; their count is the currency. Returning to GREEN needs 3
consecutive green runs on distinct pull requests, all finished after the
incident began and none carrying its signature — the runs that made the
incident cannot end it. The one exception is a deadline-kill OUTAGE, left
after 3 runs with a verdict, green or red, on distinct pull requests after
the last kill. Until then `health.json` reports `recovering`, its
advice says a retest is reasonable, and nothing is posted. The adjudicator's
only state between ticks is the previous `health.json`; the poster keeps what
it last told the space in `health-state.json` beside it.

Inside an OUTAGE the message is repeated when a new case joins the set, at most
every 2 hours. A case dropping off is not news until the state changes. A
change of condition inside DEGRADED (a storm giving way to setup deaths) is
posted, because the advice differs.

If `data.json` itself stops refreshing — the bucket copy sat unrefreshed for
four days in the week of 2026-09-04 — `health.json` keeps the last state,
flags it `stale` once the data is older than its own `stale_after_s` (2 hours
by default), and the poster says so once, and once more when the data is fresh
again. The digest carries the same note while it lasts.

## A slow gate

A day when every run is green but takes twice as long matches none of the
conditions above — nothing is lost, nothing is shared — and on 2026-09-14 the
bot stayed GREEN while every open pull request waited three hours on Vertex
latency (#1586). The wall clock is therefore a note beside the state, never a
state, and only beside a GREEN one: inside a storm or an outage the long runs
are the incident's symptom, and the incident's advice stands alone.
`health.json`'s `slow` is set, while the state is GREEN, when the median wall
clock of the last 5 full runs — a concluded run of at least one case fewer than `hack/eval/presubmit-cases.txt` lists (11+ today), all five
finished in the last 6 hours — is at least 1.2× the median of the trailing 7
days' full runs (at least 20 of them), and stays set until that median is back
under 1.1×. Wall clock is a run's finish minus its start, the digest's
measure. The poster sends one line the first tick the note appears:

```text
🐢 Smoke gate: slow — the last 5 full runs took 152–213 min (median 183)
against a 7-day typical of 151 min (p90 198); 2 reps lost to 429s or empty
records. Not a break, and /retest won't make yours faster.
```

and not again until the note has cleared and come back. The digest repeats
the line while it lasts, the Brief's healthy headline carries the same
sentence, and the state, the advice and the gate comment do not move. The
note reads finished runs, so it trails the slowdown by about one run's
length: on 2026-09-14 the pod count rose from 11:30 AM ET and the note
would have gone out at 2:00 PM ET. The ages of the running pods would show
it sooner and are not read: the adjudicate step runs as the dashboard
publisher, which holds nothing on the Prow build cluster, and the tick
carries no `kubectl`. The rule the issue proposed — three consecutive full
runs above the trailing seven-day p90 — is not the one used: replayed over
the published `data.json` (`health.py --replay` prints the note's edges
beside the state changes) it never fired that day (the p90 stood at 198
minutes because 09-08 to 09-11 had been slow too), while the median rule
fired from 2:00 PM ET and stayed quiet over 09-06 to 09-09 and the 09-12/13
weekend. Model latency is not sampled: the per-repetition eval logs carry
it, and reading them is not something a tick does.

## A backed-up pool

Every number above is measured from a run's start, so nothing here sees a run
that sat in the queue first. The `ci-kube-agents-pool-pressure` periodic
measures that hourly and grades it against the runbook's thresholds; this job
reads its `pool-pressure.json` and never re-derives the verdict, so the two
cannot disagree. A missing artifact is not an alert — that is "not wired up",
not "the pool is fine".

Like the slow note it rides beside the state and never becomes one: the runs
still pass, they just start late, and DEGRADED would tell people to retest,
which lengthens the queue being reported. Unlike the slow note it is **not**
held back outside GREEN — a different job reading different data cannot be
this incident's own symptom.

The poster sends one line when the note appears, and again if its verdict
changes inside the episode. The header names the cause, because the four
causes have four different remedies and one of them spends money:

```text
⏳ Smoke gate: pool full — all 30 projects are leased and runs are queuing.
Consider onboarding a project.
Last 3h: median wait 24 min against a 15 min limit; p95 157 min against 45.
2 runs waiting right now, past the 45 min p95 limit.
Runs still pass; /retest makes the queue longer.
```

Those are the numbers the verdict was reached on. The periodic breaches on a
day's row or on runs queued past p95 right now, never on the seven-day window,
which one bad day leaves inside its own limit. A breach on only one of the two
carries only that line.

The stretch quoted is the last three hours, not the worst day, which a
week-long verdict leaves up to six days older than the incident. The three
hours have to be over a limit themselves to be quoted, and hold five runs, as a
day's row needs; otherwise the worst day is what is left to show, and the label
says which it was.

A ⏳ also needs a run that has been waiting past the p50 limit at the moment of
the reading. Not the p95 limit, because a pool full all afternoon with every
run waiting half an hour is the case this message is for; and not any queued
run at all, because one triggered seconds ago is not a backlog. The remedy is
recomputed hourly from a live count of leased projects while the verdict stands
for a week, so a pool that filled on Monday and drained by Tuesday would
otherwise post Tuesday's remedy under Monday's numbers with nothing wrong. The
two ⚪ messages below are exempt; neither advises anything.

Once nothing has waited past the p50 limit the dashboard and the digest keep
reporting the episode, in the past tense, and say there is no backlog — not
that the queue is empty, which they do not measure. `runs not starting` needs the
queue read too: it means the pool looked fine so Prow must be at fault, which
holds only while something is queued. Unread, the message gives the free count
and apportions no blame. `pool full` keeps its remedy either way and drops "and
runs are queuing" whenever Deck did not see a backlog — the leased count is this
hour's, the queue is Deck's.

A drained queue also ends what the jam said. The verdict holds for a week, so a
pool that fills every afternoon would otherwise be announced on Monday and
silent for the rest of it; the causes already named are forgotten on a reading
that shows nothing waiting, and the next jam is news again — a next jam that has
to be measured, since an hour Deck could not be read has seen no queue at all.
The dashboard dates
a jam from its own oldest queued run rather than from the episode, for the same
reason: the episode can have opened days before the backlog being described.

`concurrency cap` (raise it), `runs not starting` (projects were free, so the
delay is Prow's; the message names the build cluster) and `queue backed up`
(the job could not read how many projects were in use) carry the same lines
under a different first one.

Unlike the slow note, this one also says when it is over. The window is a
rolling seven days, so an episode outlives the bad day by up to a week:

```text
✅ Smoke gate: queue clear — runs are starting on time again, typical wait 24s.
```

It fires only on a reading that says so. The note also disappears when the
artifact does, and that is the bot going blind, not the queue clearing. It is
owed to an episode that breached, not to a ⚪ one, which never claimed the
queue was bad -- and a breach that goes ⚪ before it drains still gets it.

Two ⚪ messages are about the monitoring, not the pool. `wait unknown` is the
check running and failing to read how long recent runs waited -- its sweep
over the window, not the live pool. `pool check stopped` is
`window_end` more than 3 hours old, or a build that published no artifact at
all. Both link to the periodic's job history, which tells the two apart. The
stopped message carries **no numbers**: `latest-build.txt` keeps resolving
after the periodic dies, so a stopped job reads as an unchanging healthy
artifact.

This note files no `presubmit-gate` issue, unlike an OUTAGE, lost pods and
fixture drift. A full pool is a capacity fact, not a defect a code change
closes; its remedies are onboarding and raising the cap, which are planned
work. Chat, the Brief and the digest carry it, and nothing opens.

The digest carries the median wait every morning whether or not anything is
wrong (`typical wait`, beside `typical run`), and repeats a one-line version
of the note while it lasts. The Brief's lede carries the same numbers. There
is no tile for it: the tiles recompute for the reader's date range, and the
wait is a fixed 24-hour figure that is not in the run data.

## The comment on a red pull request

Each tick, `scripts/eval_dashboard/gate_comment.py` finds the
`pull-kube-agents-smoke-test` runs in `data.json` that finished since its last
tick and concluded `FAILURE` with at least one graded repetition — not aborted
runs, not setup deaths, not a suite that lost every repetition to a storm, and
not a run the suite itself marked not evaluated (`runs[].eval_outcome`) — and
leaves one comment on each pull request (the newest red run per pull request
when there are several). Three shapes outside that filter also get one, below:
a not-evaluated run, a lost pod and a deadline kill.

- a heading, `❌ Smoke gate: failed · 3 of 14 cases`, or `· hard failure` when
  the run failed with no gate case failing all of its repetitions (an absolute
  check, or a truncated log);
- a health box: during an OUTAGE or DEGRADED state whose signature the run
  carries, that the red is not the author's code and not to retest yet; when
  the gate is healthy and the failed case passes on other pull requests' recent
  runs, that it looks specific to this pull request; a mix says both, with
  counts;
- a table of the failed cases — result as `passed / total reps`, and how many
  other pull requests the case is failing on right now;
- the check's reason for a case that looks like the pull request's;
- how many cases passed, the run's wall clock and pool project, and links: the
  build log, `run.html#build=<build id>` on the dashboard, and the incident
  brief when there is an incident.

An ordinary red that reaches a verdict during a deadline-kill OUTAGE gets the
outage box with the kills' sentence ("N runs on M PRs were killed at the
360-minute deadline …") whatever its failures are classed — the shared break's
"fail on every PR" needs failing cases, and this condition names none — dated
from the outage's first kill. During the hold the box says the gate is
recovering and runs are reaching verdicts again, and a red whose failures are
all the gate's is told a retest is reasonable rather than "don't retest yet".

Which class a case gets — `shared`, `only-this-pr`, `storm`, unexplained — is
`scripts/eval_dashboard/classify.py`'s `classify_run`, the same rules the
dashboard's run page and the incident brief use; the comment only phrases it.

A repetition the scorer graded `infra` under a reason that leads with
`KUBE_AGENTS_DELEGATION_CEILING` is a delegation-ceiling repetition: the
harness's wait for the delegated worker (`AGENT_DELEGATION_TIMEOUT`) ran out
with the card still running and nothing delivered, so what the judge saw was
the front door's acknowledgement. It is not a storm repetition — the agent ran
and nothing was lost to 429s — so the storm rule above does not count it, no
pass rate has it in the denominator, and a case whose ungraded repetitions are
all of this kind is classed `delegation-ceiling` on the run page (its Do is a
retest). `health.json`'s `metrics.ceiling_reps` counts them apart from
`infra_reps`, and the daily digest carries that count on any day it is not
zero. Fifteen of them across three pull requests in two hours are the
delegation-ceiling condition above: DEGRADED under its own name, its own
message and advice, so a fleet-wide worker stall (#1879) is named here rather
than read only as "not evaluated" on every pull request.

Three other shapes get a comment, each one line with the same marker and
dedupe. A run the suite marked **not evaluated** — an admitted case, or every
case, lost every repetition to infrastructure, so `hack/ci-eval-pr.sh` exited
2 and Prow recorded a `FAILURE` — is not a red to the tick: the collector
recorded the suite's `outcome` on the run (`runs[].eval_outcome`,
`SCHEMA.md`), so the comment names the cases the suite listed in
`runs[].not_evaluated` and says nothing about the change is implied:

```text
### ⚪ Smoke gate: run not evaluated

> `security-overgrant-probe` lost every repetition to infrastructure before
> the agent could be graded. The suite could certify nothing, so Prow reports
> the run red; nothing was graded for it and nothing about your change is
> implied. Retest once the environment is healthy. [Details →](run.html#build=<build id>)

Ran 27 min on evals-11 · build log
```

When every recorded case was lost the box says so instead of naming them.
While `health.json`'s condition is `storm` it adds "a quota storm is declared
on the gate right now" and links the brief. A gate case that failed every
graded repetition on the same run (the dashboard's roster can be newer than
the branch's) is named in a second sentence, with its transcript to read
before retesting; the heading stays ⚪, because the suite is the verdict's
owner.

The second is a lost pod (the build node went away under the job, #1478), a
zero-task run:

```text
### ⚪ Smoke gate: run lost

> The Prow build node running this job went away at 10:19 AM ET (<node>).
> Nothing was graded and nothing about your change is implied. `/retest` once
> new jobs are progressing. [Details →](run.html#build=<build id>)

Ran 128 min before the node went away · build log
```

While `health.json`'s condition is `lost_pods` the box adds "part of a
build-cluster event: N runs on M PRs" (below the 8-run event bar, "one of N
runs on M PRs that lost their build node") and the incident brief link. Prow's
build-log page shows the pod's events. Setup deaths and conflicted merges stay
silent; the build log says which it was.

The third is a run Prow killed at the job deadline with no verdict (#1894),
whether or not some cases finished first: the same one-line shape under `⚪
Smoke gate: run killed at the deadline`, saying when it was killed and that no
verdict was reached. While `health.json`'s condition is `deadline_kill` the box
says the gate is down — "N runs on M PRs have been killed at the deadline since
⟨time⟩; your run's failure is not your diff" (the time is the outage's first
kill, which `health.json`'s `incident.first_kill` keeps as the rule's 2-hour
window slides) — with the brief link, and asks
the author not to retest yet. During the hold that follows the outage
(`recovering`) it says instead that the outage is recovering and this kill
holds it back, and that with other pull requests' runs finishing it may be the
branch; the run page reads the same way, from the same `recovering` flag, and
does not count that kill as the incident's. With no deadline-kill outage declared it does not
clear the branch: one pull request looping to the deadline is that pull request's
problem (a change that hangs the eval ends the same way), so the box says it
may be the branch and points at the build log.

The comment starts with a hidden marker (`<!-- smoke-gate-comment -->`); a
later red on the same pull request edits it in place, and a build already
commented on is never commented on twice. The watermark and the comment ids
live in `gate-comment-state.json` beside `health.json`; a first tick with no
state looks back one hour. Posting uses the workflow's own `GITHUB_TOKEN`
through `gh api` (the job holds `pull-requests: write` and `issues: write` for
this and the tracking issue). A failure to post is a warning; the run is
retried next tick and the job never reds for it. `--dry-run` prints the
comments instead.

## The tracking issue

When the state becomes OUTAGE and no issue tracks it — `case-notes.yaml` names
none for the failing cases, and no open issue labelled `presubmit-gate` names
every failing case in its title or body — the poster files one, labelled
`presubmit-gate`: `Smoke gate outage: 3 cases failing on every PR since Sun
7:30 AM ET`, with the cases, the window, the class, the incident brief link,
and the line "Filed automatically by the smoke health bot; edit freely. Fix
PRs: reference this issue." A human's issue that already names the cases is
adopted instead. The Chat message then reads `Tracking #NNN`, the issue rides
in `health-state.json` and in `health.json`'s `issue` field (`{number, url, condition}`,
`null` outside an incident; `health.py` reads it back through
`--posted-state`), and the recovery comments on it: "Healthy again after Xh;
bot will not close it." `issue` is the current condition's; every issue the
incident filed or adopted stays in `health-state.json`'s `issues` list until
GREEN, so an outage that gives way to a storm or to lost pods before it clears
still gets its recovery comment, and the recovery message names them all. The
bot never closes an issue. A GitHub failure leaves
the message at "no issue yet — file one with the presubmit-gate label" and the
next change asks again.

A new `lost_pods` condition files one the same way, for the cluster owner:
`Build cluster lost node(s) gke-kube-agents-prow-default-pool-eb220b2a-{er33,pe72,sgnk} at Fri 10:05
AM ET: 12 smoke runs on 12 PRs died mid-run` (several node names are compacted
to their shared prefix; past GitHub's 256-character title limit they become a
count), with the nodes and how many runs each lost, the window, the affected
pull requests, the evidence, the advice for authors, and the line "Filed
automatically by the smoke health bot; the cluster owner should check the node
events and autorepair; the bot will not close it." An open `presubmit-gate`
issue that already names every lost node is adopted instead. The Chat message
reads `Tracking #NNN`; the issue rides in the state the same way and is filed
once per event. Each issue records the condition it was filed for (`{number,
url, condition}`): an outage's issue is never cited as the lost pods' tracking,
nor the reverse, so a break followed by a node loss files both, and both are
commented on when the gate recovers.

A new `deadline_kill` OUTAGE files one for whoever owns the gate: `Smoke gate
outage: 3 runs on 3 PRs killed at the 360-minute deadline with no verdict
since Tue 3:40 PM ET`, with the window of the kills, the affected pull
requests, the deadline evidence lines (not the per-case ones: a body naming
cases would be adopted as a later break's tracker), the advice for authors
(don't retest until the space reports the gate healthy), where to look (each killed run's `build-log.txt`
and the eval project's Cloud Logging), and the recovery bar of 3 runs with a
verdict. An open `presubmit-gate` issue whose **title** carries "deadline" and
"smoke" is adopted instead — the title only, because every bot-filed body
names the job and quotes the evidence, which mentions deadline kills whenever
one sits in the window. The Chat message reads `Tracking #NNN`, the issue
rides in the state the same way, and the recovery comments on it.

## The seeded-fleet scan

Presence probes passed on 2026-09-07 while every slot-a fixture sat Pending on
all 30 pool projects (#1278). `hack/fleet-fixture-state.py` is the check that
would have failed (#1544: each role's `state` assertions in
`bench/tf/fleet/fixtures.json`), and the `fixture-state-scan` job in
`.github/workflows/ci-health.yml` runs it on a clock rather than per lease: at
the top of every hour (`0 * * * *`, a second cron in the same workflow; the
`github.event.schedule` guards send each run to one job) it runs
`scripts/eval_dashboard/fixture_state.py`, which, per pool project and in a
temporary directory of its own, runs `hack/fleet-kubeconfigs.sh` and then
`hack/fleet-fixture-state.py --wait 0 --report`, six projects at a time, and
publishes `gs://kube-agents-dashboards/evals/fixture-state.json`. It is its
own job rather than a step on the top-of-hour tick because it needs `kubectl`
and `gke-gcloud-auth-plugin`, runs thirty projects for a few minutes (a
healthy project takes about 20 s), and must never hold the 15-minute verdict:
the tick reads whatever scan is published. The project list is
`gitops_repo_for_project()` in `hack/ci-deploy.sh`, the one list of pool
projects this repository holds; the leasable roster is Boskos's, and every
leasable project is mapped there first. A mapped project that is not
provisioned or not visible scans as "not checked".

**The document.** `fixture-state.json` is `{schema_version, scanned_at,
duration_s, projects{}, summary, previous}`. `projects` has one entry per
pool project, `{roles{}, summary, duration_s, reader, error?}`, and `roles`
one entry per catalog role: `{"state": "healthy" | "drifted" | "not_checked",
"detail": [...]}` — for a drifted role, the assertion and what the scan
observed, as `hack/fleet-fixture-state.py` writes it (`deployment/checkout-gateway
status.readyReplicas eq 2: observed 0`); for one not checked, why (the reader
could not be impersonated, the runner published no kubeconfig for it, its
read failed). `summary` counts projects, projects checked (at least one role
read), projects with drift, and roles by state. `previous` is the prior
document's `scanned_at` and its `{project: [drifted roles]}` map, carried so
the adjudicator can ask "drifted last scan too?" from one file.

**The identity and the one grant.** Every read runs as that project's
read-only account, `seeded-fleet-reader@<project>.iam.gserviceaccount.com`
(`bench/tf/fleet`): `CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT` makes gcloud
impersonate it for the cluster listing, the credentials and the control-plane
describes, and `FLEET_READONLY_SA` makes the runner rewrite each kubeconfig so
`kubectl`'s token is minted as it too. The bot,
`eval-dashboard-publisher@kube-agents-prow`, therefore needs exactly one grant
per pool project — `roles/iam.serviceAccountTokenCreator` on that account, the
grant #1238 gave the presubmit's identity — and nothing on the project itself.
The grant lives on the service account resource, so it is per project by
nature (the pool projects sit directly under the organisation, with no folder
to grant on). `bench/tf/fleet`'s `fleet_reader_token_creators` defaults to the bot
beside both runners, so an apply of the fleet stack in a project grants it; for projects
applied before that default, the repair is one command per project:

```bash
BOT=eval-dashboard-publisher@kube-agents-prow.iam.gserviceaccount.com
for p in $(sed -n '/^gitops_repo_for_project() {/,/^}/p' hack/ci-deploy.sh \
          | sed -n 's/^[[:space:]]*\(kube-agents-evals[-0-9]*\)).*/\1/p'); do
  gcloud iam service-accounts add-iam-policy-binding \
    "seeded-fleet-reader@${p}.iam.gserviceaccount.com" --project "$p" \
    --member "serviceAccount:${BOT}" --role roles/iam.serviceAccountTokenCreator --quiet
done
```

Until the grant is in place the scan pre-flights one token mint per project,
fails it, and records every role as "not checked" with gcloud's own words.

**The condition.** `health.py`'s `fixture_drift` fires when the same role is
drifted on the same project in two consecutive scans, or on 3 or more projects
in one scan. One scan on one project is not enough: in #1278's retest sweep the
crashloop fixture lagged the node repair by about 40 minutes on one project (it
needs its first restart before OOMKilled evidence exists), and one hourly scan
can land inside that window. Three projects at once is the fleet-wide shape
(#1278 was all 30) and waits for nothing. It is DEGRADED, ranked below every
run-based condition (nothing in the presubmit runs this check and nothing acts on a
drift, so a drifted fixture reds only the cases that depend on it, on the runs that
lease those projects; the run-based conditions see that red as it happens, and
this one names the cause and its owner), and it ends the hour a scan that could
read the incident's roles on the incident's projects no longer shows it — three
green runs could all have leased healthy projects and say nothing about the
fixture, and a scan that is missing, stale, blind, or that could not read one of
those projects holds the condition with a note in the evidence rather than
posting a recovery nothing observed. A scan older than 3 hours is ignored with a
note in the evidence; a scan that could check no project at all (the grant missing,
`kubectl` missing) is `fixture_state.unknown` in `health.json`: the poster says
so once, and once more when the scan sees the fleet again, and it is never a
drift. `health.json`'s `fixture_state` block carries the latest scan's time,
how many projects it could read, every project's drifted roles, and the
`unknown` and `stale` flags with the commonest reason.

**What it posts.** A new `fixture_drift` condition is a state change like any
other: one Chat message naming the roles and how many projects, that a red on
a case depending on them from a run in those projects is the fixture and not
the code, that a retest waits for the re-apply, and `Tracking
#NNN`; the gate comment's health box carries the same sentence on a red run
while the condition lasts; the 9 AM digest always carries one line on the
latest scan (`🧭 Seeded fleet: 30 of 30 pool projects checked at 8:00 AM ET,
every fixture in its designed state`, or the drifted projects and roles, or
that the scan is stale or could see nothing). The tracking issue is filed for
the fleet owner, labelled `presubmit-gate`: `Seeded fleet drift:
crashloop-workload out of designed state on 3 pool projects since Mon 9:00 AM
ET`, with the roles, per project the assertion and what was observed, the
window, the evidence, and the reconcile — re-apply `bench/tf/fleet` in each
project named (`bench/tf/fleet/README.md`, "State and reconcile") — and the
line "Filed automatically by the smoke health bot; the fleet owner should
re-apply the stack in the projects named; the bot will not close it." An open
`presubmit-gate` issue that already names every drifted role is adopted
instead. The recovery comments on it as on any other.

**What never fails the bot.** A missing `kubectl` or `gcloud`, a project the
publisher cannot read, a missing grant, a runner or a state check that hangs
past its ceiling (300 s per project, 1500 s for the scan): each is "not
checked" with its reason, the scan exits 0 and publishes, and the tick reads
it as such. Only a repository bug — no mapping in `hack/ci-deploy.sh`, no
catalog — reds the scan job. `fixture_state.py --projects <id> --no-impersonate`
runs the same scan from a laptop with direct access to one project.

## The history feed

After `health.json` is uploaded, the same object is appended as one line to
`gs://kube-agents-dashboards/evals/health-history.jsonl` (JSON Lines, one
record per tick, oldest first, nothing trimmed). Each record is the
`health.json` document verbatim — `schema_version`, `state`, `condition`,
`since`, `cause`, `failing_cases`, `tracking_issues`, `issue`, `incident`,
`evidence`, `advice`, `recovering`, `stale`, `slow`, `pool`, `metrics`,
`dashboard_url`, `generated_at` — plus `tick`, the ISO 8601 UTC time the line
was appended.
`generated_at` is the data's horizon and `tick` the wall clock, so a stalled
refresh shows as many ticks sharing one `generated_at`. GCS has no append: the
workflow downloads the object (a missing one is the first tick), appends with
`scripts/eval_dashboard/health_history.py`, and uploads; a failure there is a
warning, not a failed tick. The incident brief reads this feed.

## Replaying history

```bash
python3 scripts/eval_dashboard/health.py --replay --data data.json --step 30m \
  --roster-history scripts/eval_dashboard/testdata_health/roster-history.json
```

walks a `data.json` as if the job had run every 30 minutes and prints the state
timeline. `scripts/test_eval_dashboard_health.py` asserts that timeline for
2026-09-01 → 2026-09-08 against the incidents filed that week (#1171, #1189,
#1214, #1269, #1278). The roster history matters: `compliance-rbac-overgrant`
and `rca-remediation-pr` were admitted when they collapsed and were demoted
afterwards, so a replay with today's roster would not see the 09-02 outage.
A second fixture, `testdata_health/lost-pods-2026-09-11.json.gz`, is the day
the build cluster lost five nodes (#1478); the same test file asserts it reads
as `lost_pods` with 12 runs on 12 pull requests, and that the setup-death rule
no longer claims them. A third, `testdata_health/slow-gate-2026-09-14.json.gz`,
is the week ending 2026-09-14 18:20Z (#1586), the seven days the slow-gate
baseline needs; the test asserts the `slow` note appears at 18:00Z that day
with the day's numbers and never over the 09-12/13 weekend. A fourth,
`testdata_health/deadline-kills-2026-09-22.json.gz`, is 2026-09-22 12:00Z →
09-23 20:00Z, the day the Hermes bump wedged the workers and 33 runs were
killed at the deadline (#1880, #1894); the test asserts GREEN until 20:00Z on
the 22nd, OUTAGE `deadline_kill` from the third kill, and that a lull in kills
reads as recovering rather than green.

## The Chat space

Incoming webhooks are disabled org-wide, so the poster calls the Chat API as a
Chat app: `POST https://chat.googleapis.com/v1/{space}/messages` with a token
bearing the `chat.bot` scope, minted in the workflow with
`gcloud auth print-access-token --scopes=…` for the service account bound to
the app. The app ("Smoke Health", project `kube-agents-prow`) is bound to the
dashboard publisher, `eval-dashboard-publisher@kube-agents-prow`, the identity
the publish, adjudicate and post steps run as; the space is
`#kube-agents-ci-health` (`spaces/AAQAlcuDUJI`). Both are defaults in the
workflow's `env`; the repository variables `CI_HEALTH_CHAT_SPACE` and
`CI_HEALTH_SA` override them. The off switch is the repository variable
`CI_HEALTH_MUTE=true`: no token is minted, the poster logs "webhook not
configured" and exits 0 before it would file a tracking issue, the comment
step on pull requests is skipped, and the refresh, the verdict and the
`health.json` upload carry on. It exits before writing `health-state.json`
too, so what the poster remembers stands still while muted: an alert already
sent stays sent. The dashboard is unaffected — the pool episode's start rides
in `health.json`, which is written every tick, not in the state file.
An incoming-webhook URL in Secret Manager
(`ci-health-chat-webhook`, `kube-agents-prow`) is the optional alternative.

`post_health.py --dry-run` prints the messages instead of posting them.
