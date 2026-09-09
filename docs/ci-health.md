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
Chat — only when the state changes, plus one digest a day at 9 AM Toronto time.
The same tick comments on each pull request whose run went red
(`gate_comment.py`), files the tracking issue a new OUTAGE lacks
(`gate_issue.py`), and appends `health.json` to a history feed. A
`workflow_dispatch` of the same workflow is the on-demand refresh button.

Every message ends with a deep link into the dashboard:
`index.html?cases=<comma-separated case ids>&since=<ISO 8601 UTC>[&until=<ISO 8601 UTC>]#gate`
for an incident (`until` on the recovery message), `#agent` for the digest.

## Times

Every time a person reads — in a Chat message, the gate comment, the issue
title — is America/Toronto, written `7:30 AM ET` (`Sun 7:30 AM ET` where the
day matters), DST included. URL parameters, `health.json`, the state files and
the history feed stay ISO 8601 UTC. The digest hour is a Toronto hour
(`--digest-hour 9`, `--digest-tz America/Toronto` by default) and "once a day"
is a Toronto day: the state file's `last_digest_date` is the local date.

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

**DEGRADED** — a quota storm (15+ repetitions lost to 429s or empty records
across 3+ pull requests among the runs that finished in the last 2 hours,
#1225 / #1214), or setup deaths (3+ runs that concluded `FAILURE` under 5
minutes with no tasks, on 2+ pull requests, in 2 hours, #1172; an aborted
zero-task run is a superseded push). Retest after the time the message gives; a
run started inside a storm loses repetitions to it.

**GREEN** — none of the above. No message of its own beyond the recovery that
announces it; the daily digest carries the green rate, wall clock p50/p90, the
infra-rep rate and the setup-death count over the last 24 hours.

A case failing on exactly one pull request while passing elsewhere is that pull
request's problem and moves no state; the message lists it as "PR-caused".

## Hysteresis

A single bad tick does not change the state, and a single lucky green does not
end an incident. Entering OUTAGE or a storm DEGRADED needs the condition to be
current: one of the three newest completed runs carries it (setup deaths are
not completed runs, so their count is the currency). Returning to GREEN needs 3
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
  build log, `run.html?build=<build id>` on the dashboard, and the incident
  brief when there is an incident.

Which class a case gets — `shared`, `only-this-pr`, `storm`, unexplained — is
`scripts/eval_dashboard/classify.py`'s `classify_run`, the same rules the
dashboard's run page and the incident brief use; the comment only phrases it.
When that module is not on the checkout the script's built-in fallback applies
the same shape from `health.json`'s failing cases and the other pull requests'
runs in the window.

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
in `health-state.json` and in `health.json`'s `issue` field (`{number, url}`,
`null` outside an incident; `health.py` reads it back through
`--posted-state`), and the recovery comments on it: "Healthy again after Xh;
bot will not close it." The bot never closes an issue. A GitHub failure leaves
the message at "no issue yet — file one with the presubmit-gate label" and the
next change asks again.

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
configured" and exits 0, and the refresh, the verdict and the `health.json`
upload carry on. An incoming-webhook URL in Secret Manager
(`ci-health-chat-webhook`, `kube-agents-prow`) is the optional alternative.

`post_health.py --dry-run` prints the messages instead of posting them.
