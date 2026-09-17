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
`scripts/eval_dashboard/post_health.py` tells `#kube-agents-ci-health` on Google
Chat — only when the state changes, plus one digest a day at 9 AM Toronto time,
plus one line, once per episode, when the gate is slow without being broken
([below](#a-slow-gate)).
The digest also carries one line on last night's run of the nightly tier
(`--data`, the `data.json` the tick collected): the cases recorded, how many
passed all reps, partial and failed, what is newly failing against the night
before, and the wall clock, with a link to the dashboard's Nightly report
(`nightly.html`); a night Prow cut short, one still running, or no night
since the day before yesterday, says so instead of numbers.
`scripts/eval_dashboard/nightly.py` derives the line and the report from the
same nightly runs.
The same tick comments on each pull request whose run went red or whose
build node went away (`gate_comment.py`), files the tracking issue a new
OUTAGE lacks or the one a build-cluster node loss or a seeded-fixture drift
owes its owner (`gate_issue.py`), and appends `health.json` to a history
feed. A second job in the same workflow, on its own hourly cron, scans every
CI pool project's seeded fleet for fixtures out of their designed state and
publishes `fixture-state.json` beside `health.json` ([The seeded-fleet
scan](#the-seeded-fleet-scan)); the tick reads it for the `fixture_drift`
condition and the digest carries one line on the latest scan. A
`workflow_dispatch` of the same workflow is the on-demand refresh button
(its `fixture_state_scan` input also runs the scan).

Every message ends with a deep link into the dashboard:
`index.html#since=<ISO 8601 UTC>[&until=<ISO 8601 UTC>][&cases=<comma-separated case ids>]&view=gate`
for an incident (`until` on the recovery message), `view=agent` for the
digest, and the bare `index.html#view=agent` for the slow-gate note, whose
start is a GREEN tick that names no incident. The scope rides in the URL
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

**DEGRADED** — lost pods (the build cluster lost the node under the job:
3+ runs that concluded `FAILURE` with no tasks and either a `NodeNotReady` pod
event or no build log at all, finishing within 30 minutes of each other, among
the runs of the last 2 hours, #1478; 8+ is announced as a build-cluster event,
and the cluster owner's issue below is filed on any new `lost_pods`
condition), a quota storm (15+
repetitions lost to 429s or empty records across 3+ pull requests among the
runs that finished in the last 2 hours, #1225 / #1214), or setup deaths (3+ runs
that concluded `FAILURE` under 5 minutes with no tasks, on 2+ pull requests, in
2 hours, #1172; an aborted zero-task run is a superseded push), or seeded
fixture drift (the hourly scan found the same fixture role out of its designed
state on the same pool project on two consecutive scans, or on 3+ projects in
one scan; #1550, below). A zero-task run
is at most one of a lost pod, a conflicted merge (below) and a setup death, in
that order: a lost pod is never a setup death, whatever its duration. When more
than one condition fires, the order
above decides which one the message carries; the others stay in the evidence.
For a storm, retest after the time the message gives; for lost pods, once new
jobs are progressing; for fixture drift, once the fleet owner has re-applied
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
what keeps the replay fixtures cut before the field valid.

**GREEN** — none of the above. No message of its own beyond the recovery that
announces it; the daily digest carries the last 24 hours' runs, greens,
PR-caused reds and infra reds (setup deaths and lost pods are folded into the
infra count) and the typical run length. `health.json`'s `metrics` keeps the
rest — green rate, wall clock p50/p90, the infra-rep rate, `setup_deaths`,
`lost_pods`.

A case failing on exactly one pull request while passing elsewhere is that pull
request's problem and moves no state; the message lists it as "PR-caused".

Only presubmit runs reach these rules and the digest's numbers. `data.json`
also carries the nightly periodic's runs (`runs[].tier`, see
`scripts/eval_dashboard/SCHEMA.md`); a nightly has no pull request to count
towards a distinct-PR floor, and a nightly collapsing is a case's record on
`main`, not a gate incident.

## Hysteresis

A single bad tick does not change the state, and a single lucky green does not
end an incident. Entering OUTAGE or a storm DEGRADED needs the condition to be
current: one of the three newest completed runs carries it (setup deaths and
lost pods are not completed runs, so their count is the currency). Returning to GREEN needs 3
consecutive green runs on distinct pull requests, all finished after the
incident began and none carrying its signature — the runs that made the
incident cannot end it. Until then `health.json` reports `recovering`, its
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
clock of the last 5 full runs — a concluded run of 15+ cases, all five
finished in the last 6 hours — is at least 1.2× the median of the trailing 7
days' full runs (at least 20 of them), and stays set until that median is back
under 1.1×. Wall clock is a run's finish minus its start, the digest's
measure. The poster sends one line the first tick the note appears:

```text
🐢 Smoke gate: slow — the last 5 full runs took 152–213 min (median 183)
against a 7-day typical of 151 min (p90 198); 2 reps lost to 429s. Not a
break, and /retest won't make yours faster.
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

## The comment on a red pull request

Each tick, `scripts/eval_dashboard/gate_comment.py` finds the
`pull-kube-agents-smoke-test` runs in `data.json` that finished since its last
tick and concluded `FAILURE` with at least one graded repetition — not aborted
runs, not setup deaths, not a suite that lost every repetition to a storm — and
leaves one comment on each pull request (the newest red run per pull request
when there are several):

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

Which class a case gets — `shared`, `only-this-pr`, `storm`, unexplained — is
`scripts/eval_dashboard/classify.py`'s `classify_run`, the same rules the
dashboard's run page and the incident brief use; the comment only phrases it.

One zero-task run does get a comment: a lost pod (the build node went away
under the job, #1478). It is one line, same marker and dedupe:

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
`evidence`, `advice`, `recovering`, `stale`, `metrics`, `dashboard_url`,
`generated_at` — plus `tick`, the ISO 8601 UTC time the line was appended.
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
with the day's numbers and never over the 09-12/13 weekend.

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
`health.json` upload carry on. An incoming-webhook URL in Secret Manager
(`ci-health-chat-webhook`, `kube-agents-prow`) is the optional alternative.

`post_health.py --dry-run` prints the messages instead of posting them.
