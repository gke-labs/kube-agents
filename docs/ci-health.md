# CI health: the presubmit gate adjudicator

`scripts/eval_dashboard/health.py` reads the eval dashboard's `data.json` every
15 minutes (`.github/workflows/ci-health.yml`), decides whether
`pull-kube-agents-smoke-test` is **GREEN**, **DEGRADED** or **OUTAGE** and why,
and writes `health.json` next to the dashboard.
`scripts/eval_dashboard/post_health.py` tells `#kube-agents-ci-health` on Google
Chat — only when the state changes, plus one digest a day at 08:00 UTC.

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
repository's GitHub Actions deploy account, `github-actions@kube-agents-prow`,
the same identity the bucket steps run as; the space is
`#kube-agents-ci-health` (`spaces/AAQAlcuDUJI`). Both are defaults in the
workflow's `env`; the repository variables `CI_HEALTH_CHAT_SPACE`,
`CI_HEALTH_CHAT_SA` and `CI_HEALTH_CHAT_WIF_PROVIDER` override them, and an
empty space silences the poster: the job still runs, uploads `health.json`,
and logs "webhook not configured" without failing. An incoming-webhook URL in
Secret Manager (`ci-health-chat-webhook`, `kube-agents-prow`) is the optional
alternative.

`post_health.py --dry-run` prints the messages instead of posting them.
