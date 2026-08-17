# SOP: k8s-event-watcher Daily Activity Recap

**Purpose:** Reports the informational events the severity gate held back from chat, grouped by
workload and reason, and nothing else. Critical and Warning events are counted in the headline and
never listed — chat received them as they happened. That holds even for an alert that never reached
chat: a ceiling drop and a failed delivery are counted, so neither can be printed over with an
all-clear, but neither is named.
[What this recap does not report](#what-this-recap-does-not-report) is canonical for that contract
and for the gap it leaves.

---

## How it runs

`eod-event-watcher-daily-report` is a `no_agent` script entry on the Platform Agent's roster
(`cron/jobs.json`): a tick runs `eod_report_generator.py` as a plain subprocess and prompts no
model. The scheduler delivers a `no_agent` job's stdout verbatim, which is why the entry ships
`deliver: "all"` — the report _is_ the stdout, and `local` would resolve to no delivery target and
drop it. Nothing rewrites that output, so the script emits a bullet list rather than a Markdown
table, which wraps badly in chat viewports.

`profile-cron-tick` ticks the roster once a minute with `HERMES_HOME` set to this profile's home,
and restores the home-channel routing a `no_agent` child would otherwise lose. The script
itself is shared, not per-profile — the entrypoint links `profiles/platform/scripts` at the shared
scripts directory. It needs no cluster access.

**The schedule is `0 21 * * 1-5`, and that hour is UTC** — 17:00 US/Eastern in summer, 16:00 in
winter. Nothing in this repository sets `HERMES_TIMEZONE` or a `config.yaml` `timezone` key, so
every entry on every roster here runs on UTC whatever the local reading of its hour suggests;
[`cron-jobs.md`](../../../docs/site/src/content/docs/reference/cron-jobs.md) is canonical. A run
looks back 24 hours, except on Monday, when it looks back 72 — a fixed day-long window would leave
Friday 21:00 through Sunday 21:00 in no run's scope at all. `default_window_hours` reads the weekday
in UTC so it agrees with the clock the scheduler ticked on.

## Where the numbers come from

Every number comes from one table: `intercepted_events` in `session_kv.db`
(`/var/lib/kube-agents/session/session_kv.db`), written by the `session_kv_server.py` REST bridge on
port 8699. It holds one row per event the watcher forwarded, with `notified` recording whether that
event was announced in chat. [`../docs/session_management.md`](../docs/session_management.md) is
canonical for the schema and the ingestion flow it records.

Three things are deliberately not sources. The `incidents` table alongside it holds the triage
report each alerted incident produced, and the recap does not read it: nothing it lists was alerted,
so nothing it lists has a triage report. The watcher's `dedup.json` snapshot is keyed by
`(uid, reason)` with no namespace or workload to group by, is never pruned when a window expires,
and resets each entry's `count` when its window rolls over. And no deduplication ratio is derived
from the ledger: the watcher discards duplicates before the bridge hears about them
(`dispatcher.Dispatch` returns on `dedupDuplicate`) and hardcodes `count` to `1` on the payload it
does send, so a ratio over these rows would measure how many distinct incidents shared a key — 400
duplicate `CrashLoopBackOff` events collapsing into three injects would report zero. The real figure
is the `eventsDedupSuppress` Prometheus counter.

### When the ledger cannot be read

A missing database, or a volume old enough to have the database but not the table, yields zero rows
— indistinguishable from a quiet fleet. Per
["I found nothing" and "I could not look" must not arrive as the same silence](../../../docs/site/src/content/docs/concepts/autonomous-watchdogs.md),
the header turns 🔴, the body names each path and why it failed, and the telemetry counts, the
suppressed-events line and the ✅ all-clear are all withheld: those zeroes are the absence of a
measurement, not a measurement of zero. A warning on stderr is not enough — this job runs
`no_agent`, so its stdout is the whole chat message. A failure on one candidate path that a later
path made up for is not reported.

The candidate list is short on purpose: `SESSION_KV_DB_PATH` and the `/var/lib` literal, nothing
else. `/tmp` is writable by anything the agent runs and the search stops at the first path that
reads, so a stray database there would silently supply the entire recap. `--db` is the supported
override.

### What the header emoji grades

🔴 an unreadable ledger, 🟢 a day that was genuinely clean, 📊 everything else. The first line
carries the worst thing the ledger saw, not a summary of the report's length.

🟢 is the only assertion the header makes, so it is gated on the same condition as the ✅ all-clear
rather than on whether there was anything to list: a day with no informational events but thirty
`Critical` alerts the ceiling withheld grades 📊 — neutral, which is honest, where green would not
be.

The ✅ all-clear claims nothing was held back from chat. It does **not** claim the watcher is alive:
this script never contacts it, and both tables it reads are written by the daemon's own `/inject`
path, so zero rows means no event arrived — which a dead, crash-looping, RBAC-denied or deliberately
stopped watcher produces just as readily as a quiet fleet. `EVENT_WATCHER_ENABLED=false` is a
documented emergency stop for an event storm, and an all-clear asserting "daemon active and
streaming" would print a green light every weekday for as long as it stayed off.

## What it lists

Events whose Kubernetes `Event.Type` is not `Warning` are classified `Info` by
`get_severity_details` and suppressed at the notifier: no chat alert, no triage session. They are
still written to `intercepted_events` with `notified = 0`, and they are this report's subject —
listed by workload and reason under `🔕 Informational Events Held Back from Chat`, and totalled on
the closing line. That line prints even on a day with nothing to list, so "no alerts today" cannot
be confused with a watcher that has stopped forwarding.

`ALWAYS_ALERT_REASONS` never land in that count, whatever their `Event.Type`:
`get_severity_details` grades them on the reason, so they come back `Warning` or `Critical` and are
alerted rather than muted into a line here. Only one of the five reaches this profile as deployed —
the watcher's `--reason` list in deploy/shared/start-services.sh forwards `FailedToDrainNode` and
rejects `NodeNotReady`, `NetworkNotReady`, `FailedScheduling` and `Evicted` at the daemon, so do not
read their absence from a recap as their absence from the fleet. See the Severity Gate section of
[`../docs/session_management.md`](../docs/session_management.md).

The listing is fixed to `Info` in `LISTED_SEVERITIES` in `eod_report_generator.py`, and nothing
widens it. Widening it would buy a digest of the day's already-delivered alerts, and it still would
not reach the two classes chat never received, because those are an outcome and not a severity.

Three properties of the listing follow from what a suppressed event is:

- **No remediation advice.** Nobody was alerted to these events, so no troubleshooter session ran on
  them and there is nothing to quote. The recap says what happened and stops; the workload names are
  the handle for anyone who wants to look further.
- **Ten entries, not five.** Informational churn spreads across more workloads than alerts do —
  image-pull `BackOff` is high count and many pods — and the overflow line names how many groups the
  cut dropped.
- **Severity and delivery are part of the grouping key**, not just labels on the group. `BackOff`
  arrives both `Normal`-typed and `Warning`-typed, and keyed on cluster/namespace/workload/reason
  alone, whichever row the ledger returned first decided the grade for every event under it.
  `notified` is keyed for the same reason: a group is listed or dropped as a unit and its `count` is
  printed as a fact about every row in it. A session server predating the Info gate wrote
  `notified = 1` on informational events, so for a day or two after an upgrade a group can hold both
  outcomes; the key splits them rather than announcing a delivered event under a heading that says
  it never arrived.

## What this recap does not report

Two classes of event are graded `Critical` or `Warning`, never reached chat, and are **not reported
here at all** — not named, not counted, not alluded to:

- **Alerts the daily ceiling withheld.** `ALERT_DAILY_LIMITS` stopped them. The
  `k8s_event_watcher_events_quota_suppressed_total` counter and `GET /v1/alert-quota` both report
  these immediately and severity-accurately, so the signal exists outside this report.
- **Alerts whose delivery failed.** `notified = 1` is written before the chat post is attempted, so
  on its own it records an intention to deliver rather than a delivery. When the post fails the
  ingestion side clears the flag and records why in `delivery_error`. **No metric counts this**, and
  the alert is not in chat, so the ledger column is the only trace and nothing reads it. That is a
  real gap, accepted deliberately to keep this report to one subject; see the follow-up issue for
  the metric that would close it.

What the recap does instead is refuse to _deny_ them. Both counts are read, and either one blocks
the ✅ all-clear and the 🟢 header: "Nothing was held back from chat today" printed over a day the
ceiling ate thirty `Critical` alerts is not silence about the drops, it is a denial of them. Such a
day reports as 📊 with no all-clear line — the reader is told nothing about what went wrong, and is
not told that nothing did.

Neither filter reaches those counts. `min_event_count` applies to the listing only, and where it
does apply it measures the withheld rows rather than the group's total: a group is one
cluster/namespace/workload/reason/severity key and may hold rows with different outcomes, so a
workload with one withheld alert and nine delivered ones would otherwise clear a threshold of five
on the strength of nine events that were not withheld. `EOD_EXCLUDE_NAMESPACES` removes a
namespace from the workload breakdown, the headline counts and the closing informational total, but
not from the veto — `kube-system` ships in the exclusion list and the watcher forwards it anyway, so
applied to the veto a control-plane delivery failure would drop out of it and the recap would end
the day green over it. Excluding a namespace narrows what the report _says_ and must not widen what
it is willing to _claim_, so the row is flagged rather than skipped. Once the filter has removed
anything, the ✅ all-clear and the 📉 closing total both carry "namespaces in
`EOD_EXCLUDE_NAMESPACES` are outside this recap's scope".

## Running it by hand

```bash
python3 /opt/data/scripts/eod_report_generator.py
```

`--window-hours` widens or narrows the period (default 24, or 72 on a Monday), `--db` points at a
different session database, and `--cluster-name` overrides the name resolved from
`GKE_CLUSTER_NAME`. With no `--db`, the script reads `SESSION_KV_DB_PATH` — the same variable
`session_kv_server.py` and the operator use — before falling back to the packaged path.

The two filters are environment variables on the agent container, set the way the alert ceiling's
`ALERT_DAILY_LIMIT_*` are:

| Variable                 | Default                                   | Effect                                                      |
| ------------------------ | ----------------------------------------- | ----------------------------------------------------------- |
| `EOD_EXCLUDE_NAMESPACES` | `kube-system,kube-public,kube-node-lease` | Comma-separated; an empty value excludes nothing.           |
| `EOD_MIN_EVENT_COUNT`    | `1`                                       | Groups below it are dropped from the listing, not the veto. |

Both are read on every run, so `kubectl set env` takes effect on the next tick with no restart. A
value that will not parse warns on stderr and falls back to the default rather than failing the run.
