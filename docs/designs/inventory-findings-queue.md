# A Findings Queue for the Bootstrap Inventory Scan

> **STATUS — design of record; not yet implemented.** Nothing in this repository stores a finding
> beyond the run that produced it. The pipeline this design extends does ship: the sweep in
> [`inventory.md`](../../agents/platform/governance/inventory.md), the ranking in
> [`inventory_prioritize_sop.md`](../../agents/platform/governance/inventory_prioritize_sop.md), and
> the remediation-PR machinery described in
> [`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md). Treat everything below as the
> reference design.

The first-time environment sweep finds everything and reports five things. On a neglected fleet the
gap between those numbers is the whole problem: the sweep ranks dozens of findings, renders the top
five, rolls the rest into one `Also found: N items` line, and the ranking work is discarded with the
process that did it. Nothing re-reads the remainder, nothing re-verifies it against a cluster that
has since changed, and the only route back to it is the user thinking to ask.

This design turns the discarded remainder into a durable queue: one row per problem, ordered by a
published rubric, drained a couple of items a day, re-verified at the moment it is surfaced, and
carrying a prepared fix where a fix can be prepared.

## 1. Why the delivered report is the wrong container

The shipped path is four stages, described in full by the
[`bootstrap_onboarding` README](../../agents/chat/defaults/plugins/bootstrap_onboarding/README.md)
and the site's [ChatOps concepts page](../site/src/content/docs/concepts/chatops.md), which are
canonical for it. In outline: `bootstrap_scan_gate.py` files a kanban card to `platform`; that
worker follows `inventory.md` and writes the complete findings to `/opt/data/INVENTORY.raw.md`; a
second card follows `inventory_prioritize_sop.md`, collapsing duplicates and ranking everything
before rendering at most five items to `/opt/data/INVENTORY.md`; `bootstrap_delivery.py` posts that
file to chat verbatim. The cap has one exception, which matters to the argument below: when
critical findings alone exceed five they are never capped and never rolled up, so the list is
exactly those criticals. A fleet with six criticals gets all six; a fleet with one critical and
forty gaps gets five.

Three things go wrong at the last step. The shape of the problem is the one
[`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md) §1 sets out for the pull request as a
container — a delivery artifact pressed into service as a lifecycle record — though the specific
failures differ.

**The ranking is computed and thrown away.** Step 3 of the prioritization SOP scores every surviving
finding on three dimensions. Step 4 reads the top of that order and discards the rest of it. The
sixth-ranked finding and the fortieth are indistinguishable by the time the report is written — both
are a contribution to one integer in `Also found: 14 informational items`.

**A display cap is doing the work of a lifecycle.** Five is the right number of items to put in a
first chat message. It is not a statement that the sixth problem is resolved, deferred, or accepted,
and there is nowhere to record which of those it is. `inventory_prioritize_sop.md` is careful that
nothing is dropped _from the report_ — "every finding in the raw file is either shown, merged into a
shown finding, or counted in the roll-up" — but a count is not a queue, and the guarantee stops at
the message boundary.

**A snapshot has no mechanism for noticing it has gone stale.** `INVENTORY.raw.md` records what was
true during one sweep. A finding fixed the following week stays in the file; a finding that got
worse stays at its original severity. Re-running the sweep produces a second file rather than
updating the first, because nothing joins the two.

## 2. Why not the `incidents` table

The Session KV server already stores something that sounds like this. It is not, and the difference
is worth stating because the name invites the mistake —
[`incident_context`](../../agents/platform/plugins/incident_context/__init__.py) already carries a
comment saying so. The schema, from
[`session_kv_server.py`](../../agents/platform/scripts/session_kv_server.py):

```sql
CREATE TABLE IF NOT EXISTS incidents (
    chat_id    TEXT NOT NULL,
    thread_id  TEXT NOT NULL,
    report     TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, thread_id)
)
```

**The grain is wrong, and no amount of added columns fixes it.** The primary key is a chat thread,
so a row is one conversation rather than one problem. A five-finding report is one row; one problem
discussed in three threads is three rows. A table keyed on the conversation cannot be ordered by
priority, because priority is a property of the problem.

**A queued finding has no thread.** Most of the queue has never been mentioned to anyone — that is
what makes it a queue. Those findings cannot hold a row in a table whose key they do not have.

**There is no column for anything the queue needs.** No severity, cluster, namespace, object, check,
state, or root cause. Root cause exists only as prose inside the opaque `report` blob, which is not
a thing you can sort by.

**`cleanup_old_records` would drain it.** Incident reports are deleted after `CLEANUP_TTL_DAYS`,
fourteen by default. A backlog stored there evaporates in a fortnight, which is issue #774's
complaint restated as a cron job.

**What the watcher path stores is a message, not a finding.** `inject_message` posts the alert,
hands off to `trigger_agent_troubleshooter`, and returns without a row; the two statements that
write the table are the cron relay's and `platform_mcp_server.py`'s. The watcher does reach the
second of those — `trigger_agent_troubleshooter` registers session routing, and the troubleshooting
agent's threaded `send_notification` then POSTs `/v1/incidents` — so `incident_context` is right
that the watcher is one of two writers. The point survives the correction: what lands is the
composed chat message, which for the relay's own writes the server describes as "the relay's
composed output rather than the specialist's finding." Neither writer produces a ranked, stateful
row for a problem.

So `findings` is a new table beside `incidents` with a disjoint job: `findings` is the queue,
`incidents` is the chat-thread transcript. ("Ledger" is reserved throughout this document for the
fleet-audit GitHub issue, which is a third thing again — §10.) They meet at a foreign key,
described in §8.

## 3. The `findings` table

Grain is one problem: one check, at one object, on one cluster.

| column                                       | notes                                                                                                                                                                                                                      |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`                                         | identity; primary key. Derived, never written by a model — see below                                                                                                                                                       |
| `source`                                     | `inventory` \| `event-watcher` \| `audit`                                                                                                                                                                                  |
| `check`                                      | the check slug; the same vocabulary the audit streams use (§10)                                                                                                                                                            |
| `cluster`, `namespace`, `object`             | `namespace` empty for cluster-scoped objects                                                                                                                                                                               |
| `severity`                                   | `critical` \| `major` \| `minor`. Derived from `rank_score`, not judged separately (§4)                                                                                                                                    |
| `rank_score`                                 | the ordering key. Written at registration, changed only by a named re-rank event                                                                                                                                           |
| `rubric`                                     | the per-measure vector behind `rank_score` (§4), stored so the rank is auditable                                                                                                                                           |
| `provider_managed`, `actionable`             | the two gates (§4)                                                                                                                                                                                                         |
| `title`, `detail`                            |                                                                                                                                                                                                                            |
| `root_cause`                                 | nullable; the column `incidents` never had                                                                                                                                                                                 |
| `recommendation`                             | `{action, rationale, risk}`, required and non-empty on every row                                                                                                                                                           |
| `remediation`                                | `{kind, path, note}`; `kind` ∈ `manifest` \| `gcloud` \| `manual`. `note` is required and `path` is rejected unless `kind` is `manifest` — the audit validator's rule, kept so §9 can hand a row to `remediate` unmodified |
| `pr_url`, `pr_state`                         | null where `kind != "manifest"`                                                                                                                                                                                            |
| `state`                                      | `queued` → `surfaced` → `accepted` \| `dismissed` \| `resolved`, plus `snoozed` and `stale`                                                                                                                                |
| `first_seen`, `last_verified`, `surfaced_at` |                                                                                                                                                                                                                            |
| `surface_count`, `snoozed_until`             | drive the re-offer interval and the explicit silence (§7)                                                                                                                                                                  |
| `chat_id`, `thread_id`                       | null until surfaced; written _after_ the send. The join to `incidents`, and not the drip's routing input (§8)                                                                                                              |

**`id` is derived from the finding's own fields**, by the rule `audit_report.py` already implements
in `derive_finding_id`: the dotted concatenation of `(check, cluster, namespace, object)`, each
segment reduced to `[a-z0-9-]` by `_id_segment` so a value can never manufacture a segment boundary.
Reuse it rather than inventing a second scheme, for the reason that function's docstring gives at
length — an identity an LLM re-derives from prose every morning is not an identity. It records two
separate costs: the identity had been written five different ways by five SOPs, and on 2026-08-03 a
single stream spelled the same nine problems three different ways in three consecutive runs, on
which day the 16:34 run announced four unfixed criticals as resolved. Reusing it
also means an inventory finding and an audit finding about the same object produce the same string,
which §10 depends on.

**`findings` is exempt from `cleanup_old_records`.** A backlog with a TTL is not a backlog. The
lifecycle is `state`, not age: a finding leaves the table when it is resolved, dismissed, or has
stopped reproducing for long enough to be marked `stale`, and each of those is a decision something
made rather than a timer.

## 4. The priority rubric

Dozens of findings is the expected case. What makes that tolerable is that only the top of the order
needs a fix today, so the ordering is not a presentation detail — it is the mechanism the rest of
the design rests on.

### 4.1 Why a rubric and not a judgement

`inventory_prioritize_sop.md` Step 3 states the intuition well and leaves the scoring to the model.
Step 1 of the same SOP measures what re-derivation costs, in the narrower case of re-deriving
severity the sweep file already states: it is "the main source of run-to-run instability in this
stage — measured over three runs on identical input, inferred ranking produced 3, 6 and 6 items with
only two findings common to all three," and the SOP's response is to preserve the stated severity
rather than re-infer it. The queue cannot take that option, because most of its rows arrive with no
severity to preserve. It needs the ranking to be stable when it is genuinely being computed, which
is a stronger requirement than the SOP faced. A report can absorb the instability; a queue cannot,
because the drip would offer a different "today's two" each morning from a fleet that had not
changed.

Anchored ordinals are the fix. Each measure is a small classification against written text rather
than a holistic judgement, so the same finding classifies the same way twice. `sort_findings` holds
the equivalent contract for the ledger today — "a pure function of the finding set, never of its
input order" — and the rubric extends that guarantee across sources.

### 4.2 Where the measures come from

The SRE Workbook's risk analysis prices a risk as **impact × duration ÷ time between occurrences**,
where duration is time-to-detect plus time-to-repair. Those terms are the measures. They subsume
Step 3's three dimensions rather than replacing them: its "failing now versus latent" is L, its
blast radius is B, and its "is the action concrete" becomes a gate.

**B — blast radius: what fails.**

|     |                                                                            |
| --- | -------------------------------------------------------------------------- |
| 8   | cluster-wide or control-plane                                              |
| 5   | an entire serving workload                                                 |
| 3   | degraded capacity — some replicas, or a load-bearing non-serving component |
| 2   | one pod, a batch job, or a non-production workload                         |
| 1   | no runtime consequence; hygiene only                                       |

Step 3's tie-break survives: a finding on the cluster the agent itself runs on ranks above the same
finding elsewhere. **For a credential or permission finding, B is the scope of what it grants, not
the workload holding it** — a project-level service-account key mounted in one pod is an 8, not a 2.
State this explicitly in any SOP that applies the rubric, because the natural reading gets it wrong.

**L — likelihood: when it bites** (the 1/TTF term).

|     |                                                                                                                                                |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| 10  | failing now — CrashLoopBackOff, OOMKilled, expired certificate, PVC at capacity, a credential known to be exposed                              |
| 6   | fires on the next ordinary event — a rollout, node drain, autoscale, preemption; also a long-lived broad-scope credential reachable from a pod |
| 4   | dated — a real deadline exists: an API removed in the next minor, a key or certificate expiry, a quota trend crossing                          |
| 2   | needs an abnormal event — zone loss, traffic past the current peak                                                                             |
| 1   | no failure mode; posture only                                                                                                                  |

**E — exposure: how long it hurts** (TTD + TTR), each scored 1–3.

|     | detect                                                                  | recover                                                                                                      |
| --- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| 3   | silent — no probe, no alert, no telemetry on the path; a user tells you | state at risk or manual multi-step recovery — no backup, an RWO StatefulSet, a rotation plus an access audit |
| 2   | visible if someone looks                                                | an ordinary rollout or rollback                                                                              |
| 1   | something alerts on it today                                            | self-healing                                                                                                 |

**C — confidence**, a multiplier: 1.0 measured, where the fault or object was observed directly; 0.9
read from live object state; 0.6 inferred from absence or a heuristic.

**`rank_score = round(B × L × (detect + recover) × C)`**, giving a range of 1 to 480.

**Severity is derived, not judged separately:** `critical` at 150 and above, `major` from 40 to 149,
`minor` below 40. These are initial thresholds, and §7.2 records what a dry run against a simulated
fleet says about them: the bands are wide in the middle, with `major` holding half the queue. That
is tolerable because nothing selects on the band — the drip orders by `rank_score` and the band is
a label — but it is the first thing to calibrate against a real sweep, and the calibration belongs
here when it exists.

The threshold is load-bearing in one place. `critical` is one of the three conditions on the
auto-promotion sweep in `finish`, which is what opens a pull request without being asked. It is
**not** a condition on `remediate`, which passes `auto_promote=False` and "opens what was named and
nothing else" — and `remediate` is the call §9 puts on this path. So on the queue's own path the
threshold decides nothing about promotion; it decides what the surfaced message calls the finding,
and what a future `finish`-style sweep would pick up if one were ever pointed at this table.

### 4.3 Worked examples

These belong in the SOP alongside the anchors. Reproducibility comes from the examples at least as
much as from the scale.

| finding                                                        | B   | L   | d   | r   | C   | score | severity |
| -------------------------------------------------------------- | --- | --- | --- | --- | --- | ----- | -------- |
| static project-editor SA key in a Secret, no Workload Identity | 8   | 6   | 3   | 3   | 1.0 | 288   | critical |
| CrashLoopBackOff, single-replica serving Deployment            | 5   | 10  | 1   | 2   | 1.0 | 150   | critical |
| no `readinessProbe`, 3-replica serving Deployment              | 3   | 6   | 3   | 2   | 1.0 | 90    | major    |
| Shielded Nodes disabled, Standard node pool                    | 8   | 2   | 3   | 3   | 0.9 | 86    | major    |
| no resource requests, BestEffort QoS                           | 3   | 6   | 2   | 2   | 1.0 | 72    | major    |
| Managed Service for Prometheus not enabled                     | 1   | 1   | 2   | 1   | 1.0 | 3     | minor    |

Two of those pairings are the argument for the rubric. The service-account key outranks the live
CrashLoopBackOff, deliberately: one workload is down and someone can see it, while the other is a
project-scoped credential nobody is watching and nothing would report. And Shielded Nodes shares the
key's blast radius but scores a third of it, because L is what separates "is handing out access
right now" from "would be catastrophic if someone first compromised a node."

This is also why not CVSS. It scores a vulnerability in a product, has no notion of blast radius
inside a fleet, and says nothing at all about the reliability findings that are most of this queue.

### 4.4 Two gates, applied before scoring

**Ownership.** Step 3's set-aside carries into the queue unchanged: objects in `kube-system`,
`kube-public`, `kube-node-lease`, and any namespace matching `gke-*` or `gmp-*` are
provider-managed. The operator does not own the manifest, cannot change it, and "a recommendation to
do so is not weak advice, it is impossible advice." Those rows are flagged, never drip, and never
get a pull request. The SOP's exception holds: one that is _actively broken_ is scored normally,
because the action is real even though it is a support case rather than a manifest edit.

**Actionability.** A finding with no concrete next step sorts after every actionable finding
whatever its score. This is a flag rather than a multiplier on purpose — a multiplier lets a B=8
unactionable observation float back above work someone could actually do.

### 4.5 What is deliberately not a measure

**How hard the fix is.** You do not demote a critical problem because its remedy is expensive; that
is how important work never gets done. Fix cost enters exactly once, as a tie-break within a drip
slot: between two findings of comparable score, prefer the one whose `remediation.kind` is
`manifest`, because that one can be handed over today as a reviewable diff.

### 4.6 Persist the vector, not just the total

Store `{B, L, detect, recover, C}` on the row. It makes the rank auditable, lets the drip explain
itself in one line — "failing now, whole workload, nothing alerts on it" — and turns a re-rank into
a single-measure edit rather than a re-judgement of the whole finding.

Re-ranking is a **named event**, never a silent recomputation. Three events move a score:
re-verification at surface time finds the finding now firing (L 6 → 10); a watcher incident lands on
an object a queued finding already names; a dated deadline crosses into the horizon. Nothing else.

**Cross-source ordering needs no special rule.** A watcher incident is by definition failing now, so
L places it above posture gaps by construction. Say this where the rubric is documented, because the
obvious alternative — a per-source priority band — is worse and a reader will reach for it.

## 5. Who writes it

**Inventory.** Extend `inventory_prioritize_sop.md` rather than adding a stage. It already collapses
duplicates in Step 2 and ranks everything in Step 3; today Step 4 discards all but five. Have it
register the full collapsed set with rubric vectors, then render the delivered report from what it
registered. One ranking pass, no new card, and the `Also found: N items` line becomes a pointer into
the queue instead of a dead end.

**The event watcher.** [`k8s-event-watcher`](../../k8s-operator/cmd/k8s-event-watcher/) already
POSTs to the same Session KV server, so registering a finding is one more call on a path that
exists.

Its dedup cache and the queue's identity are different mechanisms with different lifetimes, and the
design should say which owns what. `EventKey` is `(involvedObject.uid, reason)` over a rolling
window — the watcher's
[README](../../k8s-operator/cmd/k8s-event-watcher/README.md) is canonical for the window and its
`WATCHER_DEDUP_WINDOW` override — and it answers "should this event open a troubleshooting
session?" A pod UID changes on every recreate, so
a CrashLoopBackOff that is rescheduled ten times is ten dedup keys. `derive_finding_id` is
`(check, cluster, namespace, object)` and answers "is this the same problem?", which across ten
recreations of the same Deployment's pod it is. The watcher keeps its cache for session suppression;
the queue keys on the finding id and updates the existing row.

## 6. Who reads it

The Chat Agent has no tools to read this, and that is deliberate rather than an oversight to route
around. Its toolsets are `mcp-router`, `kanban`, and memory — enforced in three layers in
[`agents/chat/config.yaml`](../../agents/chat/config.yaml), the last of them a denylist applied to
every platform key so that no reintroduced base bundle can grant terminal, file, or exec access.

So an on-demand pull is not a query. It is a kanban card assigned to `platform`, which is exactly
what [`scan_completed.md`](../../agents/chat/defaults/onboarding/scan_completed.md) already
prescribes for the full inventory: "You hold no tools for reading it yourself, so the same rule
applies as everywhere else: file it, do not promise it." The queue reuses an established boundary
rather than working around one.

New MCP tools land on
[`platform_mcp_server.py`](../../agents/platform/scripts/platform_mcp_server.py), which already
talks to the Session KV server on loopback.

## 7. The drip

A new job on the Platform Agent roster in
[`jobs.json`](../../agents/platform/cron/jobs.json) with `deliver: "chat"`, the same shape as the
agent-driven watchdogs already on it.

**The drip asks a different question from the queue.** The queue's order is `rank_score`; the drip
asks what must be done _today_, and that is L rather than the total. It fills two slots from two
separate pools:

- **The urgent slot** takes the highest-scoring finding at L = 10 (failing now), or at L = 4 with
  its deadline inside a seven-day horizon. Empty when nothing is firing.
- **The drain slot** takes the highest-scoring finding that has never been surfaced, falling back to
  the highest-scoring one whose re-offer interval has come round. **Urgency cannot take this slot**
  — see §7.2, where letting it do so is what breaks the design.

A finding is a candidate unless it is `resolved`, `dismissed`, `stale`, `snoozed`, or inside its
re-offer interval. It is emphatically not restricted to `queued`: a finding moves to `surfaced` the
first time it is posted, and §7.1 is about the ones that must keep coming back after that.

**The drain slot batches by object.** When the drain pick is one of several findings queued against
the same `(cluster, namespace, object)`, it brings the others with it as sub-items of a single
message. They are one visit to one manifest and usually one pull request; splitting them across
three weeks of mornings asks the user to open the same file three times. This does not widen the
message beyond one object, and it does not apply to the urgent slot, where the point is that one
thing is on fire.

**Two is a ceiling, not a quota.** The argument is Step 4's, about its five, and it transfers
intact: "Report the number of distinct problems the cluster actually has… Never pad toward five…
Padding is the failure this stage was built to fix; a short report is the success case, not an
incomplete one." A drip that posts two items every morning because two is the number teaches the
user to stop reading it, which costs more than the backlog does. It posts what genuinely warrants
today — often one, sometimes none. An empty queue produces no message at all, consistent with the
`[SILENT]` convention the governance jobs already use.

**Re-verify at surface time, not on a sweep.** The job re-checks only the candidates it is about to
post, updating `last_verified` and resolving anything already fixed. Verification cost then scales
with what is surfaced rather than with the size of the backlog, and it answers the staleness half of
#774 directly: nothing reaches a human without having been checked against the live cluster in the
same run.

**The cap is the only limiter, and it is enough.** `_claim_alert_quota` is spent in `inject_message`
alone; the cron relay path does not touch `alert_quota`, so the drip neither consumes nor is
constrained by the per-severity daily budget. A two-item ceiling is one chat message a day, which is
self-limiting by construction.

### 7.1 Re-offering, and why it is not "surfaced once, then quiet"

The tempting rule — a finding leaves contention once it has been surfaced — is wrong for the case
that matters most. Something failing now and still unfixed is still today's most important thing,
and going quiet about it is the queue failing at its only job. What actually needs guarding against
is nagging someone daily about a missing probe. L already separates those, so it sets the re-offer
interval. This is Alertmanager's `repeat_interval` applied to a backlog.

- **L = 10, failing now: tomorrow, then easing** — the next drip, the one after, then two days,
  four, and weekly from there. A firing critical is an unacknowledged page and it keeps the urgent
  slot for as long as it burns, but the interval widens, because by the fourth identical morning
  the message has stopped being news and started being the reason the user skims past it. §7.2 is
  the measurement: an unfixable fire on a flat interval is what starves the rest of the queue.
- **L = 4, dated: an interval that tightens toward the deadline** — weekly while it is far, daily
  inside the last two days. The deadline is the reason the finding has a date on it. **A deadline
  that has passed is no longer dated; it is failing now**, so the finding becomes L = 10 and takes
  that row's interval. Without this it matches "inside the last two days" forever and re-offers
  daily for good.
- **Everything else: exponential backoff** on the drain slot — one day, three, seven, twenty-one,
  then quarterly. Not never, but not every morning.

**A re-offer is a reminder, not a re-explanation.** The first surface gets the full shape: the
issue, its root cause, the recommendation, and the pull request. Every re-offer after it is one line
— what it is, how long it has been failing, and the PR link. Repeating the full treatment verbatim
each morning is the thing that teaches someone to stop reading, and it is avoidable without going
silent.

**Silence is not consent; `snooze` is.** The user needs an explicit way to say "I know, not now" — a
`snoozed` state with a `snoozed_until`, the backlog equivalent of an alert silence. Inferring
dismissal from an unanswered message is how this design would end up back at ignoring its own top
item.

**A finding re-offered many times without action is itself a finding.** After several unactioned
re-offers of a `critical`, the drip should say so. Either the rubric mis-scored it or the fix is
blocked on something, and both are worth surfacing instead of a sixth identical reminder.

Specify the message shape once and have the on-demand pull reuse it: the issue, its root cause, the
`recommendation`, and the pull request link — or, where `remediation.kind` is not `manifest`, the
recommendation together with an explicit statement that there is no PR for this one.

### 7.2 What a dry run of the selection rules showed

The rules above were run against a simulated neglected fleet before this document was settled: two
clusters, 32 findings scored by §4 using real check slugs, 30 of them rankable after §4.4's
ownership gate, and nobody fixing anything. The ranking held up — the order is defensible end to
end, both gates sort correctly, and the two findings that tie at 90 (`probes-liveness` and
`probes-readiness` on the same workload) break deterministically on `_finding_sort_key`.

The selection did not. An earlier draft of this section gave urgency both slots ("fires displace
the drain, deliberately") and re-offered L = 10 findings on a flat daily interval. Over ninety days
that draft surfaced **6 of 30 findings**, and one of them 90 times:

| selection rule                             | findings surfaced | whole queue seen | worst repeat |
| ------------------------------------------ | ----------------- | ---------------- | ------------ |
| fires take both slots, flat daily re-offer | 6 / 30            | never            | 90×          |
| \+ elapsed deadline becomes L = 10         | 6 / 30            | never            | 90×          |
| \+ drain slot reserved from urgency        | 26 / 30           | never            | 90×          |
| \+ re-offer interval widens for L = 10 too | 30 / 30           | day 25           | 16×          |
| \+ drain batches by object                 | 30 / 30           | day 12           | 16×          |

Three things are worth reading off that table.

**The failure reproduced #774 inside its own fix.** Twenty-four findings never surfaced at all,
and the highest-scoring of them were not marginal: a `critical` CrashLoopBackOff on a production
payments workload, a `critical` single-replica session store, an expiring quota. They were starved
by two findings that outscored them, neither of which the operator could fix — a provider-managed
`kube-system` CrashLoop, kept rankable by §4.4's active-fault exception, and a certificate whose
deadline had passed. Both re-offered every morning and neither ever resolved. A queue whose top
item cannot be actioned must still drain beneath it.

**Reserving the drain slot is the single largest correction** (6 → 26), and widening the L = 10
interval is what closes the rest (26 → 30). They fix different halves: the first stops urgency
consuming the backlog's throughput, the second stops one permanent fire consuming urgency. Neither
alone is enough.

**Batching by object doubles throughput for free** (day 25 → day 12) because findings cluster hard
on objects — one workload in the simulated fleet carried seven, and the two cluster objects carried
five and three. That is not an artifact of the simulation; a workload with no probes usually has no
requests and no PDB either.

The elapsed-deadline rule changed no throughput number, because the certificate it fixes was
outranked anyway. It is in §7.1 as a correctness fix: without it a finding whose deadline has passed
re-offers daily forever, which is the nagging behaviour §7.1 exists to prevent.

The simulation is a design aid, not a test — it assumes the rubric's own scores are right and
models a fleet rather than measuring one. What it can show is a selection rule starving its own
queue, which it did.

## 8. How a queued finding reaches a human

The obvious reading of the `chat_id`/`thread_id` columns — that a finding carries the chat it should
be sent to — is wrong, and worth heading off before a reader reaches for it.

**The destination belongs to the delivery mechanism, not the row.** A `deliver: "chat"` cron job
routes through the relay, whose routing and send-then-store ordering
[`cron-report-relay.md`](cron-report-relay.md) owns; what matters here is that `_send_to_chat`
called with no `chat_id`/`thread_id` posts to the bare
active platform, resolved by `get_active_platform` from `config.yaml` and landing in the install's
home channel. No job on the roster carries a chat id; neither does the drip.

**The columns are an output.** The relay's own ordering is the pattern: send first, read back the
thread the send resolved to, store only then. On a finding those columns record where it was
surfaced, which is what lets a reply in that thread resolve back to the finding — and is the join to
`incidents`.

**Binding at discovery time would be the bug.** The sweep and the event watcher both produce
findings when no chat session need exist. A destination captured then is either absent or stale by
the time the finding is surfaced.

**Why the one-shot report needed more, and the drip does not.** The
[`bootstrap_onboarding` plugin](../../agents/chat/defaults/plugins/bootstrap_onboarding/plugin.py)
exists because the single-use delivery job could not fall back to a home channel: it pins the job to
an origin captured from a live human turn and declines to mark the profile aligned when there is
none, so the one copy of the report is never lost. A recurring drip carries no such risk — a day
with no reachable channel costs nothing, because the finding stays `queued` and is offered again.
That contrast is the reason the drip may use the home channel when the bootstrap delivery may not.

**One consequence to name.** A finding surfaced by the drip lands in the home channel; one surfaced
by an on-demand pull lands in the asking user's thread. Both write their own `chat_id`/`thread_id`,
so a finding surfaced twice needs a rule: the most recent surface wins, because the point of the
columns is to route a follow-up reply, and the most recent surface is the one someone is replying
to.

**Two delivery hazards to verify during implementation rather than assume.** A named-profile cron
job needs `platforms` present in the profile config to reach chat at all, and the Google Chat
home-channel configuration can silently fail gateway delivery if written in the wrong shape. Both
are properties of the deployed config rather than of this design, and both should be checked against
a running install before the drip is declared working.

## 9. The fix arrives with the finding

A queue of problems is a chore list. A queue of problems each carrying a reviewable fix is worth
draining, and almost all of the machinery for the second one already exists.

**`audit_report.py remediate` opens the pull request.** Not `submit-suggestion`, which refuses this
job itself: PRs opened through it get no dedupe and nothing ever closes them, "which is how one
workload's findings once became five near-duplicate PRs." `remediate` keys the branch on the files
the fix touches, so a rerun cannot duplicate; leaves a live PR untouched rather than force-pushing
over a reviewer; re-proposes on the same branch after a harness stale-close but never after a
human's rejection; and closes the PR when the finding stops reproducing. The discriminator between
those two closes is the `audit:stale-closed` label, which marks **the harness's own** close: strip
it and the close becomes a human veto.

**Prepare at surface time, not at scan time.** The two timings are identical from the user's seat —
the fix is waiting when they hear about the problem — and enormously different in cost on a first
scan of a neglected fleet. Scan-time promotion lands dozens of pull requests before the user has
read one finding. Surface-time promotion prepares a fix only for what the drip is about to post, so
the ordering bounds the cost rather than fleet health doing it. This is the same principle as
re-verifying at surface time, and the design should state them together: **cost follows what is
surfaced, not what was found.**

**No `/remediate` command on this path.** The word has two senses worth separating. The CLI
subcommand is the mechanism; `/remediate <finding-id>` on a ledger issue is one caller of it, and
auto-promotion inside `finish` is the other. Fleet-audit gates its long tail behind the human
trigger because seven streams reporting at once would otherwise be "a notification firehose". The
drip's two-item ceiling is already that gate, so the trigger would be a second lock on the same
door.

**Not every finding can have a pull request, and the drip must say which it is giving.**
`remediation.kind` is `manifest`, `gcloud`, or `manual`. A Workload Identity migration or a
control-plane upgrade is not a file in a repository. Those findings surface with their
recommendation and no PR link, stated as such rather than left ambiguous.

**What `remediate` needs from the queue.** Three required arguments, of which one is free. `--finding
<id>` is the free one: the queue is promoting a row it has already chosen, and §3 makes `id` the
same string the audit machinery derives. The other two cost something. `--findings-file` is a JSON
document validated against the audit schema, so the queue must synthesize a one-finding document at
promotion time rather than passing a database row — which is why §3's `recommendation` and
`remediation` columns carry the audit schema's own shapes rather than convenient ones. And
`--audit <id>` is validated against the hard allowlist in `AUDITS`, where "an id not listed here is
rejected before any git/gh call", with that stream labelling the PR and linking its ledger issue.
§10 is where that id comes from, and §10.1 is the case where it does not exist yet.

## 10. Overlap with the audit streams

The seven audit streams re-scan much of what the bootstrap sweep looks at, on their own schedules,
into their own ledger issues. Comparing the sweep in `inventory.md` against the check rosters in
`AUDITS` shows the overlap is not partial — it is nearly total:

| what the sweep checks           | stream that owns it           | check slug                            |
| ------------------------------- | ----------------------------- | ------------------------------------- |
| liveness / readiness probes     | `obtainability-audit`         | `probes-liveness`, `probes-readiness` |
| requests, limits, QoS class     | `obtainability-audit`         | `no-requests`, `no-memory-limit`      |
| HPA coverage                    | `obtainability-audit`         | `no-hpa`, `hpa-cannot-scale`          |
| NetworkPolicy                   | `compliance-audit`            | `netpol-missing`                      |
| Workload Identity               | `compliance-audit`            | `workload-identity-off`               |
| `runAsNonRoot` security context | `compliance-audit`            | `podsecurity-gaps`                    |
| Shielded Nodes                  | `fleet-consistency-drift`     | `shielded-nodes`                      |
| Dataplane V2                    | `fleet-consistency-drift`     | `datapath-provider`                   |
| Managed Service for Prometheus  | `fleet-consistency-drift`     | `managed-prometheus`                  |
| node auto-upgrade               | `security-patch-orchestrator` | `no-autoupgrade`                      |

**This resolves two questions at once, so adopt the audit check slugs as the inventory sweep's
vocabulary.**

The first question is what happens when both sources report the same problem. If the sweep emits
`probes-readiness` on the object `obtainability-audit` also emits it on, `derive_finding_id` yields
the same string for both, so they are one row rather than two findings to reconcile. Whichever
arrives first creates it; the other updates `last_verified` and, if the rubric vector moved, triggers
a re-rank. The user is told once. Had the sweep kept its own vocabulary, the same problem would carry
two ids, drip once and appear on a ledger separately, and nothing in the schema could tell that from
two real problems.

The second is which `--audit` id a promoted finding uses, given the allowlist. It uses the stream
that owns the check — which the table above already determines, and which needs no new stream, no
new SOP, and no change to shared code the seven live streams depend on.

### 10.1 The residue, and the three checks that need a home

Five sweep checks have no owning stream. Two of them cost nothing: SLO and error-budget alerting
practice, and the OTel collector inventory, are unactionable observations caught by §4.4's gate.
They never reach `remediate`, so the allowlist never sees them; the queue ranks and surfaces them
with their recommendation and no PR, exactly as §9 says.

The other three are manifest-kind, promotable, and unowned, which is the case per-check routing
cannot serve:

| unowned sweep check                         | where it is stated                    | nearest stream        |
| ------------------------------------------- | ------------------------------------- | --------------------- |
| `startupProbe` absent                       | `inventory.md` Probes Check           | `obtainability-audit` |
| `readOnlyRootFilesystem` absent             | `inventory.md` Security Context Check | `compliance-audit`    |
| namespace has no ResourceQuota / LimitRange | `inventory.md` Multi-Tenancy Audit    | `compliance-audit`    |

Each is a pod-spec or namespace YAML edit — the most promotable kind of finding there is — and each
would be rejected by `validate_audit_id` for want of a stream. The near misses are worth naming so
nobody re-derives them: `obtainability-audit` covers `probes-liveness` and `probes-readiness` but
has no startup-probe check; `podsecurity-gaps` covers `runAsNonRoot`, `runAsUser`, and
`seccompProfile` but not read-only root filesystems, and its remediation template sets
`allowPrivilegeEscalation` and `capabilities` without touching them; `netpol-missing` covers the
NetworkPolicy third of the multi-tenancy check and leaves quotas and limit ranges.

**Give each of the three a `####` section in the SOP of its nearest stream.** That is the
implementation task this design names rather than papers over: a heading, a detection query, a
severity, and a remediation shape, after which `test_check_rosters_match_the_sops` re-derives the
roster in CI and the check has an id like any other. It is work the audit streams arguably owe
anyway — a startup probe is an obtainability concern whether or not the bootstrap sweep exists.
Until it is done, those three findings queue and surface as `kind: manual`, with the recommendation
spelled out and no PR.

This keeps the fleet-audit ledger out of the queue's job. `findings` remains the ranked surface and
the thing the drip reads; the audit streams keep their ledger issues as their own reporting
surface; the only thing crossing between them is a PR's label and its `Part of #` backlink.

_Rejected alternative:_ an eighth `AuditSpec` for bootstrap inventory, holding whatever no other
stream owns. It would solve §10.1 without touching the existing SOPs, which is its real appeal. It
is still the wrong trade: `AuditSpec` requires a SOP under `agents/platform/governance/` whose
`####` check headings are re-derived in CI by `test_check_rosters_match_the_sops`, so the cost is
restructuring `inventory.md` into a check roster and maintaining a coverage denominator for a job
that runs once per install — and it buys a second name for the eight checks that already have one,
to give three checks a first name. Three `####` sections in the SOPs that should have had them is
the smaller change and leaves one vocabulary.

## 11. What this does not do

It does not replace the audit ledgers. It builds no new remediation-PR machinery, reusing
`remediate` wholesale. It does not change what the first-time report looks like — the delivered
`INVENTORY.md` keeps its cap and stays verbatim. And `INVENTORY.raw.md` stays where it is as the
full-detail record of what a sweep saw, which the queue references rather than replaces.
