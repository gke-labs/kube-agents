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
model. The script's stdout _is_ the report: the scheduler hands it to `_deliver_result` the same way
it hands over a model's final turn, so `deliver` behaves here as it does on the watchdogs.

The entry ships `deliver: "chat"`, as every enabled entry on the roster does bar
`gcp-networking-fabric-audit`. That routes the report through the
Chat Agent, which presents it in the channel and can then answer a follow-up about it — the
[relay design](../../../docs/designs/cron-report-relay.md) is canonical. `"all"` is the wrong value
even though it is audible: it expands to every platform with a home channel, and the relay now has
one, so a job left on `"all"` reports twice — once flat and once through the Chat Agent. `"local"`
resolves to no target and drops the report. The script still emits a bullet list rather than a
Markdown table, which wraps badly in chat viewports.

`profile-cron-tick` ticks the roster once a minute with `HERMES_HOME` set to this profile's home,
and restores the home-channel routing a `no_agent` child would otherwise lose. The script
itself is shared, not per-profile — the entrypoint links `profiles/platform/scripts` at the shared
scripts directory. It needs no cluster access.

**The install needs a home channel, or the recap is composed and delivered to nobody.**
The `deliver: "chat"` route does not go through `home_target_env`: `profile_cron_tick.spawn_tick`
sets `CHAT_HOME_CHANNEL` on every cron child unconditionally, so the target always resolves and the
relay is always attempted. The home channel is needed one hop later, where the Session KV server
posts what the Chat Agent composed: `_send_to_chat` runs `hermes send --to <platform>` with no
chat id, and that bare form is the one `hermes send` documents as "platform (home channel)". Where
there is none the send fails and the roster entry records
`last_delivery_error: "composed but not delivered to google_chat"`. The value is set either by the
CR — `spec.integration.googleChat.homeChannel`, which the operator renders into the pod as
`GOOGLE_CHAT_HOME_CHANNEL` — or by running `/sethome` in the channel you want it in. Every entry
on the roster shipping `deliver: "chat"` takes this route; nothing about it is specific to
`no_agent`. It matters here because the recap is the only place a filtered event is ever reported,
so an install without a home channel loses those events entirely rather than seeing them late.
Check before relying on the recap:

```bash
kubectl -n kubeagents-system exec deployment/platform-agent-gateway -c platform-agent -- \
  sh -c 'echo "${GOOGLE_CHAT_HOME_CHANNEL:-<unset>}"'
```

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

### Which clusters a recap covers

**Every count is fleet-wide, and the header says so when it is.** `start-services.sh` starts the
watcher with `--profiles-dir` as well as `--cluster-name`, which turns on multi-cluster fan-in:
every Cluster Agent profile in the pod becomes a watched cluster, and all of them forward into the
one `intercepted_events` table on `127.0.0.1:8699`. The reader does not scope its query by cluster
— that is deliberate, since filtering to the host would discard most of the fleet's events — so on
a fan-in install the headline counts, the listing and the closing total all span every watched
cluster.

What that leaves is a header naming one cluster over numbers that are not its own. So when the
window's rows carry a cluster other than the one the job runs on, the first line reads
``— `host` +2 clusters`` and a line under it names them: "one ledger serves every watched cluster,
so every count below covers `cluster-b`, `cluster-c` as well as `host`". Five names, then a count of
the rest. The scope line reports where the recap **looked**, not where it found noise, so a cluster
whose only traffic was in an excluded namespace is still named. A single-cluster install, and a
ledger whose rows carry no cluster at all, print the header exactly as before — an empty `cluster`
is read as the host's, the same way the workload listing reads it.

Within the listing, a foreign cluster is prefixed onto the workload label
(`cluster-b:prod-api/payment-api`) and the local one is left bare. `cluster` is also part of the
grouping key, so `prod/api` on two clusters is two lines rather than one line with the counts added.

### When the ledger cannot be read

A missing database, a volume old enough to have the database but not the table, or a table old
enough to be missing its `cluster` column, all yield zero rows — indistinguishable from a quiet
fleet. The third is the nastiest, and is not a stale-reader problem: `record_intercepted_event`
names `cluster` in its INSERT, so on that shape every write fails and the ledger stays permanently
empty. See ["A pre-release table, and no migration"](../docs/session_management.md) for the
`DROP TABLE` that fixes it. Per
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
be. The gate is exactly the ✅'s, no wider: a namespace exclusion does not bar green. It reads as
though it should, since the recap did not look there, but `kube-system` ships excluded and the
watcher forwards it anyway, so vetoing on it would hold the header at 📊 every day and leave 🟢
unreachable. The scope caveat rides the qualifier line under the counts instead. See
[What this recap does not report](#what-this-recap-does-not-report).

The ✅ all-clear claims nothing was held back from chat. It does **not** claim the watcher is alive:
this script never contacts it, and the one table it reads is written by the daemon's own `/inject`
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
be confused with a watcher that has stopped forwarding. Its total is in occurrences, the same unit
as the `count` on each listed group and as the headline's events-forwarded figure, so the listing
adds up to it. `*N alerts* went to chat` on the line above is a count of chat posts instead — one
per ledger row, whatever a row stands for.

What lands in that count is decided by `Event.Type` and nothing else: `get_severity_details` grades
any non-`Warning` event `Info`, however serious its reason reads. A held-back line is therefore not
a judgement that the event was minor. Reasons that appear in no recap at all are a separate case —
the watcher's `--reason` list in deploy/shared/start-services.sh never forwarded them, so nothing
was recorded to hold back. Do not read absence from a recap as absence from the fleet. See the
Severity Gate section of [`../docs/session_management.md`](../docs/session_management.md).

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

Two classes of event are graded `Critical` or `Warning` and never reached chat. Neither is **named**
here — no workload, no reason, no message — because the listing has one subject and that subject is
informational churn. Each is **counted**, on its own line above the body:

- **Alerts the daily ceiling withheld.** `ALERT_DAILY_LIMITS` stopped them. Reported as
  `⚠️ *N alerts withheld by the daily ceiling and never reached chat.*` `N` is the number of
  workload incidents chat lost, counted per pod and reason — not the number of times the ceiling
  refused a post, which is larger because a refusal makes the watcher re-offer the same failure on
  every later sighting. `GET /v1/alert-quota` reports the refusals, and
  `k8s_event_watcher_events_quota_suppressed_total` breaks them down by reason and namespace.
- **Alerts whose delivery failed.** `notified = 1` is written before the chat post is attempted, so
  on its own it records an intention to deliver rather than a delivery. When the post fails the
  ingestion side clears the flag and records why in `delivery_error`. Reported as
  `⚠️ *N alerts failed to post to chat.*` **No metric counts this class**, so that line and the
  ledger column are its only trace anywhere — which is why it is counted here rather than left to a
  metric that does not exist.

Both lines are printed only when the ledger was readable; an unreadable one reports as 🔴 and the
counts would be meaningless. Beyond counting, both totals veto the ✅ all-clear and the 🟢 header:
"Nothing was held back from chat today" printed over a day the ceiling ate thirty `Critical` alerts
is not silence about the drops, it is a denial of them. So such a day reports as 📊, with a count of
what went wrong and no all-clear over it.

The line between naming and counting is the contract. A count tells the reader to go and look; a
name would make this report the second place the incident is described, which is the duplication the
recap exists to avoid.

Such a day also prints "counts cover every severity; only informational events are listed" under the
headline. The headline totals are the whole ledger, the listing below them is `Info` alone, and on a
day with a ceiling drop or a failed delivery those two disagree by exactly the events this section
says are not reported. The line is what stops the gap reading as an arithmetic error. It is gated on
those two counts rather than printed always: a delivered alert also sits in the totals and not in
the listing, but "_N alerts_ went to chat" in the sentence above already accounts for it, and a note
printed every day is read on none of them.

The filter does not reach those counts. `EOD_EXCLUDE_NAMESPACES` removes a namespace from
the workload breakdown, the headline counts and the closing informational total, and **the two alert
tallies are exempt from it**: a ceiling drop or a failed delivery in an excluded namespace is counted
and still vetoes the ✅ and the 🟢. `kube-system` ships in the exclusion list and the watcher forwards
it anyway, so were the veto filtered too, a control-plane delivery failure would drop out of it and
the recap would end the day green over it. Excluding a namespace narrows what the report _says_ and
must not widen what it is willing to _claim_, so for those two the row is flagged rather than
skipped.

Informational churn is the exception, and deliberately so: it is dropped from `suppressed_info`,
which is the informational leg of the veto. Counting it there would put every stock install
permanently out of all-clear on `kube-system` `BackOff` noise, which is the thing the filter exists
to stop reporting. **The exclusion count is not a veto term of its own either.** It reads as though
it ought to be — the recap did not read those namespaces, so green overclaims by that much — but the
same `kube-system` traffic makes the count non-zero on an ordinary day, and vetoing on it would hold
the header at 📊 permanently, which grades nothing and costs 📊 the meaning it was added to carry.
The caveat goes in words instead: the qualifier line under the headline counts reads "namespaces in
`EOD_EXCLUDE_NAMESPACES` are outside this recap's scope" — once, whichever body the recap goes on to
print. So a day whose only withheld traffic was excluded informational events prints the ✅ and
grades 🟢, and what bounds that claim is the qualifier line rather than the emoji. What an exclusion
cannot do is any of the above to a ceiling drop or a failed delivery, which is the property that
matters and is the paragraph above.

## Running it by hand

```bash
python3 /opt/data/scripts/eod_report_generator.py
```

`--window-hours` widens or narrows the period (default 24, or 72 on a Monday), `--db` points at a
different session database, and `--cluster-name` overrides the name resolved from
`GKE_CLUSTER_NAME`. With no `--db`, the script reads `SESSION_KV_DB_PATH` — the same variable
`session_kv_server.py` and the operator use — before falling back to the packaged path.

The filter is an environment variable on the agent container:

| Variable                 | Default                                   | Effect                                            |
| ------------------------ | ----------------------------------------- | ------------------------------------------------- |
| `EOD_EXCLUDE_NAMESPACES` | `kube-system,kube-public,kube-node-lease` | Comma-separated; an empty value excludes nothing. |

Set it the way the alert ceiling's `ALERT_DAILY_LIMIT_*` are set — on the `PlatformAgent` CR, under
`spec.deployment.env`:

```yaml
spec:
  deployment:
    env:
      - name: EOD_EXCLUDE_NAMESPACES
        value: kube-system,kube-public,kube-node-lease,istio-system
```

**The CR is the only route that works, and this is why.** Environment on that container is rendered
by the operator and filtered through `safeSandboxEnvOverrides`, an allowlist — a name that is not on
it is dropped with no error, no event and no status condition, so the edit appears to succeed and
changes nothing. `EOD_EXCLUDE_NAMESPACES` is on it. Editing the Deployment with `kubectl set env` is not
a shortcut: it mutates `spec.template`, so it rolls the pod, and the next reconcile re-renders the
Deployment from the CR and reverts it.

Applying the CR change rolls the pod, and the next tick after it comes up uses the new value. The
script re-reads it on every run, so nothing further is needed once the pod is up. No value can fail
the run: `excluded_namespaces()` is a `getenv`, a `split(",")` and a `strip()`, with no parse step
to reject anything and no fallback behind it. That matters here because this job's stdout is the
chat message and a traceback would be a missing recap.

The failure mode is the quiet one instead. Unset and empty are different answers: with the variable
absent the three system namespaces apply, while a value that leaves nothing usable — `""`, `","`,
spaces — excludes nothing. Clearing the value widens the recap to control-plane churn rather than
restoring the default.
