# A Findings Queue for the Bootstrap Inventory Scan

> **STATUS — partly implemented.** §12 items 1–6 ship in
> `agents/platform/scripts/findings_queue.py`, `inventory_findings.py` and the Session KV server:
> the two tables and their indexes (§3.1), the rubric and the severity it derives (§4), the upsert
> rules (§5.2), the seven endpoints and MCP tools (§6.1), and the inventory sweep's registration
> path (§5). Item 7 ships in part: `agents/platform/scripts/findings_nudge.py` and the
> `findings-morning-nudge` cron job post §7.2's message behind its change gate, so an unchanged
> queue — including an empty one — says nothing. §7.2's weekly message regardless does not ship, and
> nor does §7.1's backlog document the nudge is meant to link to. §7.5's SOP-driven job is not how it
> runs; it is a `no_agent` script. Items 8–13 do not ship, so no finding reaches a pull request and
> §8 and §9 remain design. The pipeline
> this design extends does ship: the sweep in
> [`inventory.md`](../../agents/platform/governance/inventory.md), the ranking in
> [`inventory_prioritize_sop.md`](../../agents/platform/governance/inventory_prioritize_sop.md), and
> the remediation-PR machinery described in
> [`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md).

The first-time environment sweep finds everything and reports five things. On a neglected fleet the
gap between those numbers is the whole problem: the sweep ranks dozens of findings, renders the top
five, rolls the rest into one `Also found: N items` line, and the ranking work is discarded with the
process that did it. Nothing re-reads the remainder, nothing re-verifies it against a cluster that
has since changed, and the only route back to it is the user thinking to ask.

This design turns the discarded remainder into a durable queue: one row per problem, ordered by a
published rubric, published in full as a list the user can read whenever they want, re-verified
before anything is asserted about it, and carrying a prepared fix at the top of the order. Chat gets
a daily line saying what moved and an immediate message when something breaks, rather than being the
only place the queue exists.

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
| `severity`                                   | `critical` \| `major` \| `minor`. Derived from `rank_score`, not judged separately, with one floor for findings that are failing now (§4.2)                                                                                |
| `rank_score`                                 | the ordering key. Every row has one, gated or not (§4.4). Written at registration, changed only by a named re-rank event                                                                                                   |
| `rubric`                                     | the per-measure vector behind `rank_score` (§4), stored so the rank is auditable and so §4.2's floor stays a predicate over it rather than a column                                                                        |
| `provider_managed`, `actionable`             | the two gates (§4.4). They govern surfacing and PR eligibility, never whether a row is scored                                                                                                                              |
| `title`, `detail`                            |                                                                                                                                                                                                                            |
| `root_cause`                                 | nullable; the column `incidents` never had                                                                                                                                                                                 |
| `recommendation`                             | `{action, rationale, risk}`, required and non-empty on every row                                                                                                                                                           |
| `remediation`                                | `{kind, path, note}`; `kind` ∈ `manifest` \| `gcloud` \| `manual`. `note` is required and `path` is rejected unless `kind` is `manifest` — the audit validator's rule, kept so §9 can hand a row to `remediate` unmodified |
| `pr_url`, `pr_state`                         | null where `kind != "manifest"`. Opaque to the core: a URL it was handed and a word from `open`/`merged`/`closed`, never parsed and never used to call a repository (§6.2)                                                 |
| `state`                                      | `queued` → `surfaced` → `accepted` \| `dismissed` \| `resolved`, plus `snoozed` and `stale`                                                                                                                                |
| `first_seen`, `last_verified`, `surfaced_at` |                                                                                                                                                                                                                            |
| `surface_count`, `snoozed_until`             | how many nudges have named it, and the explicit silence (§7)                                                                                                                                                               |
| `alarmed_at`                                 | when the alarm last fired for this row, and the edge trigger that stops it firing again while the same fault persists. §7.2 owns the rule                                                                                  |
| `verification`                               | `{kind, command, still_failing_when}` — how to ask the cluster whether this is still true. Written at registration by whoever found it (§7.4)                                                                              |
| `chat_id`, `thread_id`                       | null until surfaced; written _after_ the send. The join to `incidents`, and not a delivery input (§8)                                                                                                                      |

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
lifecycle is `state`, not age, and no row is deleted by a timer. A finding that reaches a terminal
state stays in the table: `dismissed` in particular has to outlive the sweep that found it, or the
next sweep re-registers what the user already rejected and it reappears on the list (§5.2).

### 3.1 The schema

```sql
CREATE TABLE IF NOT EXISTS findings (
    id               TEXT PRIMARY KEY,
    source           TEXT NOT NULL,               -- inventory | event-watcher | audit
    check_slug       TEXT NOT NULL,
    cluster          TEXT NOT NULL,
    namespace        TEXT NOT NULL DEFAULT '',    -- '' for cluster-scoped, matching derive_finding_id
    object           TEXT NOT NULL,
    title            TEXT NOT NULL,
    detail           TEXT NOT NULL DEFAULT '',
    root_cause       TEXT,
    severity         TEXT NOT NULL,               -- critical | major | minor, derived (§4.2)
    rank_score       INTEGER NOT NULL,
    rubric           TEXT NOT NULL,               -- JSON {B, L, detect, recover, C}
    provider_managed INTEGER NOT NULL DEFAULT 0,
    actionable       INTEGER NOT NULL DEFAULT 1,
    recommendation   TEXT NOT NULL,               -- JSON {action, rationale, risk}
    remediation      TEXT NOT NULL,               -- JSON {kind, path, note}
    verification     TEXT NOT NULL,               -- JSON {kind, command, still_failing_when}
    pr_url           TEXT,                        -- opaque; see below
    pr_state         TEXT,                        -- open | merged | closed
    state            TEXT NOT NULL DEFAULT 'queued',
    first_seen       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_verified    TIMESTAMP,
    last_verification TEXT,                       -- JSON {outcome, observed, at}; see §7.4
    surfaced_at      TIMESTAMP,
    surface_count    INTEGER NOT NULL DEFAULT 0,
    snoozed_until    TIMESTAMP,
    alarmed_at       TIMESTAMP,                   -- §7.2's alarm edge trigger
    chat_id          TEXT,
    thread_id        TEXT,
    likelihood       INTEGER GENERATED ALWAYS AS (json_extract(rubric, '$.L')) VIRTUAL,
    blast_radius     INTEGER GENERATED ALWAYS AS (json_extract(rubric, '$.B')) VIRTUAL
)
```

Six choices in there are not free.

**`last_verification` is a column, not a line in a log.** §7.4 asks the verifier to record what it
observed, and the three outcomes are only distinguishable to a later reader if the observation
survives: `unverifiable` with "Error from server (Forbidden)" and `unverifiable` with a timeout are
the same row otherwise, and neither is the same as a check that ran. `last_verified` answers when,
not what.

**`check_slug`, not `check`.** `CHECK` is a SQLite keyword and `CREATE TABLE findings (check TEXT)`
is a syntax error; `"check"` parses but leaves every query one forgotten quote away from the same
error. The column is the audit streams' check slug (§10) whatever it is called here.

**`likelihood` and `blast_radius` are generated columns, not stored ones.** §4.2's floor and §7.2's
alarm both select on L, and a `WHERE json_extract(rubric, '$.L') = 10` cannot use an index.
Generated columns can be indexed and cannot drift from the vector they are computed from, which is
the objection §4.2 raises against keeping an `active` flag in a column of its own: a second copy of
a derived fact is free to disagree with the fact. These are not a second copy — SQLite recomputes
them from `rubric` on read, so a re-rank that moves L moves them in the same statement.

**`pr_url` is a URL, not an issue number.** The core stores what it was handed and returns it; it
never parses the string, never extracts a number from it, and never calls a repository. The compact
alternative — `pr_number INTEGER`, with the link rebuilt as `github.com/{repo}/pull/{n}` — puts
GitHub inside the core, so supporting another repository system becomes a schema migration and a
code change. A URL needs neither, and `open`/`merged`/`closed` are words every repository system
uses. §6.2 is the general form of this rule.

**`json_extract` over separate columns for the three JSON blobs.** `recommendation` and
`remediation` carry the audit schema's own shapes so that §9 can hand a row to `remediate`
unmodified; decomposing them into columns would mean recomposing them at promotion time, which is
the mapping bug this reuse exists to avoid.

**No column says where the list was published.** The backlog document (§7) is one document for the
whole queue, so its location is not a property of any finding — putting it on the row would write
the same URL onto thirty of them. It belongs to the publisher, in a table of its own:

```sql
CREATE TABLE IF NOT EXISTS queue_publications (
    publisher      TEXT PRIMARY KEY,     -- backlog | nudge
    target_kind    TEXT NOT NULL,        -- github-issue | repo-file | chat
    target_ref     TEXT,                 -- URL or path; opaque to the core
    content_hash   TEXT,                 -- what was last published, for §7.2's change gate
    last_published TIMESTAMP
)
```

One row per publisher, but the two publishers write it for different reasons — the backlog to
remember the document it rewrites, the nudge to remember the hash it posted — so a write that omits
a column leaves it alone rather than nulling it. A full-row replace would let the nudge's hash update
erase the URL the backlog needs on its next run, and the symptom of that is a second backlog document
rather than an error.

`snoozed_until` is stored as UTC `YYYY-MM-DD HH:MM:SS`, whatever form the caller sent, because the
expiry sweep asks `snoozed_until <= datetime('now')` and that is a string comparison. An ISO
timestamp with a `T` or an offset sorts wrong against it, so a snooze kept in the shape it arrived in
would expire at the wrong hour or never.

The indexes follow the queries the publishers actually run:

```sql
CREATE INDEX IF NOT EXISTS findings_ranked ON findings(state, rank_score DESC);
CREATE INDEX IF NOT EXISTS findings_urgent ON findings(likelihood, blast_radius, alarmed_at);
CREATE INDEX IF NOT EXISTS findings_object ON findings(cluster, namespace, object);
CREATE INDEX IF NOT EXISTS findings_pr     ON findings(pr_state) WHERE pr_state IS NOT NULL;
```

`findings_ranked` serves the backlog rewrite, which reads every open row in score order, and the
nudge, which reads the first few of the same list. `findings_object` groups a workload's findings
together as §7.1 renders them. `findings_urgent` serves the alarm, which selects on L and
`alarmed_at` rather than on score. `findings_pr` is narrow on purpose: it exists only so the
promotion reconciler (§3.2) can find rows with a live pull request without walking the backlog.

### 3.2 Lifecycle: who moves a row, and on what

`fleet-audit-issue-ledger.md` §4 computes finding state per run and never stores it, because a
ledger is rendered fresh each time. A queue cannot do that: `snoozed` and `dismissed` are decisions a
person made once and nothing in the cluster records them. So state here is stored, and every
transition has exactly one actor.

| state       | entered when                                                   | by                                                | effect on publishing                                          |
| ----------- | -------------------------------------------------------------- | ------------------------------------------------- | ------------------------------------------------------------- |
| `queued`    | registration, for an id not already present                    | any source (§5)                                   | on the list, in score order                                   |
| `surfaced`  | a nudge or an on-demand pull named it                          | the publisher, after send                         | stays on the list; `surface_count` records how often          |
| `snoozed`   | the user said "not now" and gave or implied a date             | user, via a kanban card; the daily job returns it | off the list until `snoozed_until`, then back to `surfaced`   |
| `accepted`  | the user took it on — working it, or its PR is open            | user, via a kanban card                           | its own section of the list; still re-verified                |
| `dismissed` | the user rejected it — won't fix, or not a real problem        | user, via a kanban card                           | off the list permanently; sticky against every automated path |
| `resolved`  | re-verification found it no longer reproduces                  | the daily job                                     | off the list; kept as the record that it was fixed            |
| `stale`     | the object it names no longer exists, so it cannot be verified | the daily job                                     | off the list; distinct from `resolved` on purpose             |

**`dismissed` is a sink, and the leak out of it is two steps long.** §5.2 blocks the obvious
revival — the next sweep re-registering what the user rejected — but verification is a second
automated writer on the same row, and `resolved` and `stale` are precisely the two states §5.2 then
treats as "it came back". A daily job allowed to write either onto a dismissed row does not resurrect
it that day; it arms the _following_ sweep to do so, which is the same bug with a night's delay and
no obvious cause. So verification records its observation and its freshness on a dismissed row and
moves nothing. The one actor that may reverse a dismissal is the person who made it, through the
same kanban card they made it with.

**`resolved` and `stale` are different answers and must not be merged.** "The Deployment no longer
crash-loops" and "the namespace is gone, so nobody can say" look identical to a query that only
checks whether the finding still reproduces. Recording the second as the first is how a queue
reports a fix that never happened — the failure mode `derive_finding_id`'s docstring describes from
the other direction, where a renamed key made four unfixed criticals read as resolved.

**Only three of the seven transitions belong to a person, and none of them can be inferred.** §6 is
explicit that the Chat Agent holds no tools for this table, so a user replying "snooze that" in a
thread does not reach the row. The reply becomes a kanban card assigned to `platform`, exactly as
[`scan_completed.md`](../../agents/chat/defaults/onboarding/scan_completed.md) already prescribes
for the on-demand pull, and the `platform` worker makes the transition. This is the one place where
the queue's boundary costs a round trip, and it is the same boundary §6 declines to route around.

Silence transitions nothing. §7.1 says why: inferring dismissal from an unanswered message is how
this design would end up back at ignoring its own top item.

**One transition has no actor yet, and it is the gap to close at implementation.** `pr_state` needs
someone to notice a merge. The core cannot do it — noticing a merge means talking to a repository,
which §6.2 puts outside the core — and `remediate`'s own reconcile runs on the audit stream's
schedule against the ledger rather than against this table. The cheapest answer is for the daily job
to reconcile the rows `findings_pr` returns as part of the run it already makes, writing the result
back through `PATCH /v1/findings/{id}`: a handful of `gh pr view` calls bounded by the number of open
promotions, not by the size of the backlog, which is the same cost principle as §9's promotion rule.
Whoever builds that job owns this; it is listed in §12.

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
rather than re-infer it.

The queue cannot take that option, and not because its rows arrive bare. Most of them arrive with a
severity — the audit streams write `critical`/`major`/`minor` from `audit_report.py`'s `SEVERITIES`,
and the sweep file states one either in a `severity=` field or in its `Priority 1 / 2 / 3` grouping.
The problem is that those are three bars set by three authors, plus a watcher whose events carry no
severity at all, and nothing makes an audit `critical` and a sweep `Priority 1` the same claim about
the fleet. Preserving them yields three orderings side by side, and the queue's whole output is one
order. So it computes on a single scale, and needs that computation to be stable — a stronger
requirement than the SOP faced, since the SOP mostly avoided computing at all. A report can absorb
the instability; a queue cannot, because the list would reshuffle every morning from a fleet that
had not changed.

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

**`rank_score = round(B × L × (detect + recover) × C)`**, giving a range of 1 to 480. `findings_queue.py` normalises C to an integer percent before the multiply and rounds half up, so the whole score is integer arithmetic — Python's `round()` is banker's rounding and would send two rubrics that differ only in C to the same score at an exact half. The percent is a storage detail of the `rubric` column; the endpoints take and return C as 1.0, 0.9 or 0.6.

**Severity is derived, not judged separately:** `critical` at 150 and above, `major` from 40 to 149,
`minor` below 40. These are initial thresholds, and §7.2 records what a dry run against a simulated
fleet says about them: the bands are wide in the middle, with `major` holding half the queue. That
is tolerable because nothing selects on the band — the list orders by `rank_score` and the band is
a label — but it is the first thing to calibrate against a real sweep, and the calibration belongs
here when it exists.

**One floor overrides the thresholds: a finding that is failing now, on something the user depends
on, is `critical` whatever it scores.** Formally, `L = 10 ∧ B ≥ 3` floors `severity` at `critical`.
Without it the arithmetic labels a live outage by its blast radius, and a CrashLoopBackOff taking
out a third of a three-replica serving Deployment arrives as `major` — the same word as a missing
readinessProbe on the workload beside it, which the score also puts at 90. A queue that cannot say
"this one is on fire" in the word it uses for severity has mislabelled the only category the reader
sorts on by eye.

`B ≥ 3` is where the floor stops, and it is doing real work. It is the rubric's own line between
something the user depends on — a whole serving workload, degraded capacity on one, anything
cluster-wide — and a single pod, a batch job, or a non-production workload at B = 2. An OOMKilled
dev Job is genuinely failing now and genuinely not a `critical`; flooring on `L = 10` alone would
say otherwise, and a `critical` that fires for a crash-looping scratch pod is a `critical` nobody
reads twice.

The floor is derived from the stored vector rather than kept in a column of its own. §4.6 persists
`{B, L, detect, recover, C}` on every row, so "is this actively impacting the user" is a predicate
over two of those fields and stays correct through a re-rank by construction. A separate `active`
column would be a second copy of the same fact, free to disagree with the vector the moment §4.6's
re-rank moves L.

The threshold is load-bearing in one place. `critical` is one of the three conditions on the
auto-promotion sweep in `finish`, which is what opens a pull request without being asked. It is
**not** a condition on `remediate`, which passes `auto_promote=False` and "opens what was named and
nothing else" — and `remediate` is the call §9 puts on this path. So on the queue's own path the
threshold decides nothing about promotion; it decides what the surfaced message calls the finding,
and what a future `finish`-style sweep would pick up if one were ever pointed at this table.

That is also what makes the floor cheap. Widening `critical` would be alarming if `critical` opened
pull requests, and on the queue's path it does not: promotion is bounded by the promotion slice (§9), not
by the band. The one thing to carry forward is that a `finish`-style sweep pointed at this
table later would inherit the floor as a promotion trigger — so whoever builds that decides then
whether an actively-failing finding should auto-promote, rather than acquiring the answer by
accident from a labelling rule written here.

### 4.3 Worked examples

These belong in the SOP alongside the anchors. Reproducibility comes from the examples at least as
much as from the scale.

| finding                                                        | B   | L   | d   | r   | C   | score | severity           |
| -------------------------------------------------------------- | --- | --- | --- | --- | --- | ----- | ------------------ |
| static project-editor SA key in a Secret, no Workload Identity | 8   | 6   | 3   | 3   | 1.0 | 288   | critical           |
| CrashLoopBackOff, single-replica serving Deployment            | 5   | 10  | 1   | 2   | 1.0 | 150   | critical           |
| CrashLoopBackOff, one of three serving replicas                | 3   | 10  | 1   | 2   | 1.0 | 90    | critical — floored |
| no `readinessProbe`, 3-replica serving Deployment              | 3   | 6   | 3   | 2   | 1.0 | 90    | major              |
| Shielded Nodes disabled, Standard node pool                    | 8   | 2   | 3   | 3   | 0.9 | 86    | major              |
| no resource requests, BestEffort QoS                           | 3   | 6   | 2   | 2   | 1.0 | 72    | major              |
| OOMKilled, nightly batch Job in a dev namespace                | 2   | 10  | 1   | 2   | 1.0 | 60    | major              |
| Managed Service for Prometheus not enabled                     | 1   | 1   | 2   | 1   | 1.0 | 3     | minor              |

Two of those pairings are the argument for the rubric. The service-account key outranks the live
CrashLoopBackOff, deliberately: one workload is down and someone can see it, while the other is a
project-scoped credential nobody is watching and nothing would report. And Shielded Nodes shares the
key's blast radius but scores a third of it, because L is what separates "is handing out access
right now" from "would be catastrophic if someone first compromised a node."

A third pairing is the argument for §4.2's floor, and it is the row the arithmetic gets wrong on its
own. The partial CrashLoop and the missing readinessProbe both score 90, on the same kind of
workload, and they are not the same class of problem: one is failing now and one is a gap that may
never fire. They still rank adjacently — that is the score doing its job, and publishing the whole
list (§7.1) is what stops the outage crowding out the gap — but they no longer carry the same
word.
The dev Job below them is the floor's other edge: also failing now, also L = 10, and left at `major`
because B = 2 says nothing the user depends on is down.

This is also why not CVSS. It scores a vulnerability in a product, has no notion of blast radius
inside a fleet, and says nothing at all about the reliability findings that are most of this queue.

### 4.4 Two gates, and why they run after scoring

**Every row is scored, including the ones no one will ever be asked to fix.** The SOP can set a
finding aside before ranking because its output is a list; the queue's is an order, and a row with
no score has no place in one. Both gates therefore run on scored rows and change what happens to a
finding after it has a rank, never whether it gets one. That also keeps the two gates
reversible — a namespace stops being provider-managed, a recommendation acquires a concrete next
step — without a rescoring pass to go and find the rows that were skipped.

**Ownership.** Step 3's set-aside carries into the queue with that one change: objects in
`kube-system`, `kube-public`, `kube-node-lease`, and any namespace matching `gke-*` or `gmp-*` are
provider-managed. The operator does not own the manifest, cannot change it, and "a recommendation to
do so is not weak advice, it is impossible advice." Those rows are scored, flagged
`provider_managed`, and **never get a pull request** — there is no file to change — and they are not
named in a nudge, arriving instead the way the SOP already delivers them, as a single rolled-up
observation phrased as an observation rather than an instruction. They stay on the list, where a
reader can see them without being told to act on them.

**The fault exception is not a scoring exception; it is a surfacing one.** The SOP is
explicit that a provider-managed workload that is _actively broken_ — crash-looping, not ready,
OOMKilled, a node not registering — "stays rankable and is reported normally", because "the operator
still cannot patch the spec, but they need to know, and the action is real: it is a support case or
an upgrade, not a manifest edit." A support case is a next step. So such a finding sits on the list at
its score like any other, carries §4.2's floor if it reaches it, and appears with its recommendation
and no PR link. Suppressing it would mean the fleet's own agent watching a GKE-managed component
fail and saying nothing because the fix is a support ticket, which is the failure mode this queue
exists to end. §7.3 measures what that used to cost: under the chat-only design an unfixable fire
was exactly the case that starved the queue, since it held the one channel indefinitely. On a
published list it holds the top row and nothing underneath it is hidden.

**Actionability.** A finding with no concrete next step sorts after every actionable finding
whatever its score. This is a flag rather than a multiplier on purpose — a multiplier lets a B=8
unactionable observation float back above work someone could actually do. Note what it does not
catch: "unactionable" is about the absence of a next step, not about who takes it. A provider-managed
fault has one, so it is actionable; an SLO-practice observation has none, so it is not.

### 4.5 What is deliberately not a measure

**How hard the fix is.** You do not demote a critical problem because its remedy is expensive; that
is how important work never gets done. Fix cost enters exactly once, as a tie-break: between two
findings of comparable score, prefer the one whose `remediation.kind` is `manifest`, because that
one can be handed over today as a reviewable diff.

### 4.6 Persist the vector, not just the total

Store `{B, L, detect, recover, C}` on the row. It makes the rank auditable, lets a list entry explain
itself in one line — "failing now, whole workload, nothing alerts on it" — and turns a re-rank into
a single-measure edit rather than a re-judgement of the whole finding.

Re-ranking is a **named event**, never a silent recomputation. Four events move a score:
re-verification at surface time finds the finding now firing (L 6 → 10); a watcher incident lands on
an object a queued finding already names; a dated deadline crosses into the horizon; and
re-verification finds the fault has stopped firing while the gap behind it remains (L 10 → 6), which
is the only one of the four that lowers a score and the one that clears `alarmed_at` (§7.2).
Nothing else.

That fourth event is not `resolved`. A CrashLoopBackOff that stops crash-looping because someone
raised its memory limit is fixed; one that stops because the Deployment was scaled to zero is a
missing `readinessProbe` that is no longer firing, and §7.4's re-verification answers only the
narrow question its stored command asks.

**Cross-source ordering needs no special rule.** A watcher incident is by definition failing now, so
L places it above posture gaps by construction. Say this where the rubric is documented, because the
obvious alternative — a per-source priority band — is worse and a reader will reach for it.

## 5. Who writes it

**Inventory.** Extend `inventory_prioritize_sop.md` rather than adding a stage. It already ranks
everything in Step 3; today Step 4 discards all but five. Have it register the full set with rubric
vectors, then render the delivered report from what it registered. One ranking pass, no new card,
and the `Also found: N items` line becomes a pointer into the queue instead of a dead end.

What that stage may decide is narrower than it first looks. Asked to enumerate the findings out of
the raw file's prose and make the registration call itself, it lost findings three ways: it decided
for itself what counted as a finding, so two instrumented runs over the same nine-finding file
registered seven and three, and not the same three; a batch rejected for one missing field came
back one field at a time and was abandoned; and one accepted call read as done. Enumeration is not a
judgement, so the sweep writes the findings as a machine-readable block
(`inventory.md` Step 4) and `agents/platform/scripts/inventory_findings.py` owns both ends —
`extract` produces the numbered set, `register` refuses to send anything until every number carries
a score. The stage's judgement is scoring, which is the part that needs a model.

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

### 5.1 What a source has to supply, and what it cannot

Registration is not "hand over the finding". A row needs four things the finding itself does not
obviously carry, and the three sources are unequal in their ability to produce them.

| what registration needs         | inventory                                 | audit stream                         | event watcher                  |
| ------------------------------- | ----------------------------------------- | ------------------------------------ | ------------------------------ |
| `check_slug`                    | adopt the audit vocabulary (§10)          | already has one                      | **has none** — see below       |
| `{B, L, detect, recover, C}`    | the extended prioritize SOP classifies it | its own SOP classifies it            | **cannot** — Go, no model turn |
| `recommendation`, `remediation` | already written by the sweep              | already in the audit schema          | from the reason, by table      |
| `verification`                  | the check's own detection query           | the `####` section's detection query | the object's condition         |

**The watcher is the hard case and the document previously waved at it.** "One more call on a path
that exists" is true of the HTTP request and false of everything in the request body. The watcher
sees `(involvedObject, reason, message)`. It has no check slug, so it cannot call
`derive_finding_id`, so the cross-source collision §10 depends on does not happen — a
CrashLoopBackOff the watcher reports and one the sweep reports would sit in two rows. And it runs in
Go with no model in the loop, so it cannot classify a rubric vector.

Both are fixed by the same small thing: **a static reason-to-check map, in the watcher, alongside
the reason allowlist it already carries.** A `BackOff`/`CrashLoopBackOff` event maps to the same
slug the obtainability stream uses for a crash-looping workload; `FailedMount`, `OOMKilling`,
`NodeNotReady` likewise. The map gives the watcher a `check_slug`, which gives it an id, which is
what makes its row and the sweep's row the same row. For the vector it supplies only what an event
actually tells you — L = 10, because a watcher finding is by definition failing now (§4.6 already
says this), and C = 1.0, because the fault was observed directly — and leaves B, detect and recover
to a default per reason in the same map. Those are the two measures an event cannot see, and a
default that is sometimes wrong is a re-rank (§4.6), not a wrong identity.

Reasons outside the map register nothing. The watcher keeps doing what it does today — open a
troubleshooting session — and the queue stays out of it, which is better than a row whose check slug
was invented to fill the column.

### 5.2 Re-registration: what a second sweep does to the first one's rows

Every source re-runs, so every registration is an upsert against an id that may already exist. The
rule is per state, and two of the seven are the whole point of writing it down:

- `queued`, `surfaced`, `snoozed`, `accepted` — update `detail`, `last_verified`, and the rubric
  vector; **do not touch `state`, `surface_count`, `snoozed_until`, or `alarmed_at`.** A
  re-registration is the same problem seen again, not a new one, and clearing a snooze the user set
  is how a queue starts
  nagging.
- `dismissed` — **stays dismissed, and the sweep does not resurrect it.** This is the sticky case.
  Without it the next sweep re-registers what the user explicitly rejected, the row goes back to
  `queued`, and it reappears on the list — which is not a queue with a dismissal, it is a queue that
  forgets. Record the re-observation on the row so the count is honest; do not act on it.
- `resolved`, `stale` — a re-registration means it came back. Move to `queued`, keep `first_seen`,
  clear `surface_count` and `alarmed_at`. A recurrence is news and should be allowed to trigger the
  alarm again, but it is the same problem with a history, not a new one.

**A finding that stops being reported is not thereby resolved.** This is the reciprocal case and the
one an upsert cannot express, because it is about the rows a run did _not_ mention. The temptation
is to treat absence from the latest sweep as a fix and close everything missing — cheap, and wrong
in the expensive direction. A sweep that failed halfway, ran against one cluster of two, or lost a
credential produces exactly the same absence as a fleet that got healthier overnight, and closing on
it announces fixes that did not happen.

So absence downgrades confidence rather than deciding anything: on a completed run, a `queued` row
the run did not re-report has `C` lowered to 0.6 — the rubric's own value for "inferred from
absence" — which re-ranks it down without asserting anything about it, and the definite answer comes
from §7.4's verification the next time the row is named in a nudge. Only a run that reports its own scope as
complete for that cluster may do even this much; a partial run touches nothing.

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

### 6.1 The surface is HTTP, and MCP is a wrapper over it

The obvious answer is "new MCP tools on
[`platform_mcp_server.py`](../../agents/platform/scripts/platform_mcp_server.py)", and it is the
wrong layer to start from. MCP is how an _agent turn_ reaches something, and only one of the three
callers here is an agent turn.

[`session_kv_server.py`](../../agents/platform/scripts/session_kv_server.py) owns the SQLite file
and already exposes `incidents` over HTTP — `POST /v1/incidents`, `GET /v1/incidents/by-thread`,
`GET /v1/incidents/recent`, each behind `verify_api_key`. `findings` sits in the same database and
gets the same treatment, for two reasons that are not stylistic:

- **The event watcher cannot call an MCP tool.** It is Go, it speaks HTTP to this server today, and
  §5.1 makes it a first-class writer. An endpoint has to exist whatever the agent uses.
- **The ordering has to be code.** §4.1 rejects model-side scoring because three runs on identical
  input produced 3, 6 and 6 items. Ordering has the same exposure and a worse blast radius, because
  it decides what a person reads first rather than what a report happens to say. So the sort is a
  Python function beside the table, reached as an endpoint that returns rows already ordered — not a
  prompt that asks a model to rank.

| endpoint                                         | caller                            | does                                                                                   |
| ------------------------------------------------ | --------------------------------- | -------------------------------------------------------------------------------------- |
| `POST /v1/findings`                              | prioritize worker, watcher, audit | upsert a batch under §5.2's per-state rules; returns created/updated/suppressed per id |
| `GET /v1/findings/ranked`                        | any publisher (§7)                | the open queue in the order below; the whole list, ordering in code                    |
| `GET /v1/findings`                               | the `platform` worker             | the on-demand pull, filterable by cluster, state, severity                             |
| `POST /v1/findings/{id}/surfaced`                | any publisher                     | after the send: `surface_count`, `surfaced_at`, `chat_id`, `thread_id`                 |
| `PATCH /v1/findings/{id}`                        | the `platform` worker             | the three human transitions (§3.2), plus `pr_url`/`pr_state` reconciliation            |
| `POST /v1/findings/{id}/verified`                | the daily job                     | the three-outcome result of §7.4, with what was observed                               |
| `GET`/`PUT /v1/findings/publication/{publisher}` | any publisher                     | read and write that publisher's row in `queue_publications` (§3.1)                     |

**What `/ranked` means by "open", and what order it returns.** Open is `queued`, `surfaced`, and
`accepted`; a `snoozed` row rejoins them when the daily job's expiry sweep returns it to `surfaced`
(§3.2), which is a stored transition rather than a predicate the query evaluates — otherwise the
backlog shows a row that `GET /v1/findings` still reports as snoozed.

The order is `actionable` descending, then `rank_score` descending, then cluster, namespace, object,
title and id ascending — §4.4's gate, then the rubric, then a deterministic tie-break. It is one
function, `findings_queue.ranked_sort_key`, which is also what `GET /v1/findings` sorts by before it
applies `limit`, so a capped list returns the worst rows rather than whichever ones SQLite reached
first. No
grouping: `/ranked` answers "what is worst", and grouping a workload's findings together is how the
backlog is _rendered_ (§7.1), not what the order means. Putting it here would have cost the property
that the first three rows are the three highest-scoring findings, which is what §7.2's nudge names.

Then, and only then, the MCP layer: thin tools on `platform_mcp_server.py` that call those endpoints
on loopback. That is not a new pattern — `send_notification` and `report_to_chat` are already
exactly this, MCP tools whose bodies are a request to the Session KV server built with
`_session_kv_headers`. An agent turn gets tools; the watcher gets the endpoint underneath them; the
selection logic gets a unit test instead of a prompt.

**Not a CLI.** `audit_report.py` is the counter-example worth naming, because §9 shells out to it
and a reader will ask why the queue does not follow suit. That script is a CLI because it drives
`git` and `gh` against a repository and holds no state of its own between runs. The queue is the
opposite on both counts: its state is a database another process already owns, and a second writer
to that file is a locking problem rather than a convenience.

### 6.2 The core knows nothing about repositories

That endpoint list is the boundary, and it is worth stating what the boundary is for. **Storing and
ranking findings is one job; putting the ranked list where a human sees it is another.** The first
is the core — the `findings` table, the rubric, verification, the state machine — and it produces a
ranked list and nothing else. The second is a publisher: it reads that list and renders it into
chat, a GitHub issue, a file in a repository, or something not yet written. Publishers are
interchangeable and the core does not know which one is running.

The core therefore contains no repository concepts at all. No issue numbers, no `gh` calls, no
branch names, no string that only parses on one host. This is not speculative tidiness: the install
already depends on GitHub in two heavier places — the seven audit ledgers are GitHub issues, and
`remediate` opens GitHub pull requests — and work to support other repository systems has to
abstract both. The queue's core should have nothing in it for that work to touch, and its publishers
should adopt whatever abstraction that work produces rather than growing a second one.

Three things follow, and they are cheap now and migrations later:

- **`pr_url`/`pr_state` are opaque** (§3.1). The core stores what it was handed.
- **Where the list was published is publisher state**, in `queue_publications`, not a column on
  thirty findings.
- **A publisher needs only three operations of its target**: create it once, rewrite its contents in
  place, close it. Every repository system can do all three, and so can a file in git.

**§9 is the one exception, and it is deliberate.** Promoting a finding to a pull request means
talking to a repository, and it is triggered by the core's own ordering. It stays outside the core
proper — the daily job shells out to `remediate` and writes the resulting URL back through the API —
so the core still holds no repository code. A reader who expected promotion to be a core feature
should read §9 as a publisher-side action with a core-side record.

## 7. Publishing the queue

The core produces one ranked list. Getting it in front of a person is a separate job, done by
publishers reading `GET /v1/findings/ranked` (§6.2). Three ship:

| publisher       | what it is                                                 | cadence                                     |
| --------------- | ---------------------------------------------------------- | ------------------------------------------- |
| **the backlog** | the whole ranked list, as one document, rewritten in place | after every sweep and every daily run       |
| **the nudge**   | a three-line chat message: count, top three, link          | daily, when the list changed (§7.2)         |
| **the alarm**   | a chat message about one finding that is failing now       | the run that finds it crossing §4.2's floor |

**The backlog is the queue; chat is how you hear about it.** That split is the whole of §7, and it
is a deliberate reversal of the obvious design, in which a cron job posts a couple of findings into
chat every morning and chat is the only place the queue is ever visible. §7.3 is the measurement
that killed that version.

### 7.1 The backlog document

One document per install, holding every open finding — id, score, severity, object, the one-line
recommendation, `last_verified`, and the pull request link where there is one. This publisher is
where grouping happens: it takes `/ranked`'s flat score order and gathers each
`(cluster, namespace, object)` together, ordering the groups by their highest-scoring member,
because findings cluster hard on objects and a workload's five problems are one visit to one
manifest. Rewritten in place after each run rather than appended to, so what a reader sees is the
queue as it is now.

**Today that document is a GitHub issue**, an eighth alongside the seven the audit streams already
keep. `audit_report.py` states the pattern at the top of the file — each stream "owns exactly ONE
open GitHub **issue** — its ledger — rewritten in place" — and the queue owns one more of the same
kind. It cannot reuse the seven: §10 makes a finding one row whatever source saw it, and the point
is a single order across all of them, which seven ledgers cannot express.

**Where there is no usable issue tracker, it is a `FINDINGS.md` committed to the GitOps repository**,
rewritten each run. That needs nothing but git, which is the real common denominator, and every
repository system renders markdown. It loses the per-finding comment thread — no great loss, since
§6.1's API already owns accept, dismiss and snooze, and §3.2 routes them through kanban cards rather
than through comments.

**Why a document and not more chat.** Almost every mechanism the chat-only design needed exists to
compensate for chat forgetting: a re-offer interval per severity band, a stored next-offer date, a
count of how many times a finding had been shown, an interval that widens as a message stops being
news, and a rule for batching a workload's findings into one message so they would not consume three
separate mornings. All of it answers one question — _will the user ever see the thing we did not
show today_ — and a list that stays on screen answers it by construction. What is left is the
ranking, which is §4, and telling someone the list moved, which is §7.2.

### 7.2 The nudge

One chat message a day: how many findings are open, the top three by `rank_score`, and a link to the
backlog. Three lines. It is a status line, not a report, and specifically not a re-offer — it names
the top of a list the reader can open in full, so nothing is hidden by not being named.

**It posts only when the list changed, with a weekly message regardless.** Changed means a finding
entered, left, or moved into or out of the top three. An identical message every morning is one the
reader stops opening inside a fortnight, which is #774 arriving by a different route; a silent week
is indistinguishable from a broken job, which the weekly message fixes. An empty queue produces the
weekly line and nothing else, consistent with the `[SILENT]` convention the governance jobs already
use.

**The alarm does not wait for the gate.** A finding that crosses §4.2's floor — `L = 10` and
`B ≥ 3`, failing now on something the user depends on — gets its own message, carrying the full
shape: the issue, its root cause, the recommendation, and the pull request. It posts whether or not
the list changed, and the nudge omits what the alarm has just sent rather than naming it twice in
one morning. Where the finding came from the event watcher, that watcher's existing `inject_message`
path already reports the fault within the minute, and the alarm's job is to attach the queue's
ranking and its prepared fix to something the user has already heard about.

**It fires on the crossing, not on the condition.** A CrashLoopBackOff stays over the floor until
someone fixes it, so "post whenever `L = 10 ∧ B ≥ 3`" is a message every morning about a fault the
user heard about on day one — the 90× repeat §7.3 measured, rebuilt in the one publisher that posts
without a change gate to stop it. So the run alarms on rows over the floor whose `alarmed_at` is
null and stamps it; §4.6's fourth re-rank event clears it when the fault stops firing, as does
§5.2's recurrence. A fault that persists alarms once and then lives on the list; a fault that clears
and comes back alarms again.

The floor is narrower than the list's own top, deliberately: it requires `B ≥ 3`, so an OOMKilled
dev batch Job can head the list on a quiet morning without waking anyone. It appears in the nudge,
which is the right volume for it.

`_claim_alert_quota` is spent in `inject_message` alone, so neither publisher consumes nor is
constrained by the per-severity daily budget. Their own cadences are the limit.

**Silence is not consent; `snooze` is.** The user needs an explicit way to say "I know, not now" — a
`snoozed` state with a `snoozed_until`, the backlog equivalent of an alert silence. Inferring
dismissal from an unanswered message is how this design would end up back at ignoring its own top
item. Inferring it from an unopened list would be the same mistake in a new medium.

**A finding stuck at the top is itself a finding.** When the same `critical` heads the list for
several weeks with no state change, the nudge should say so rather than name it a fifteenth time.
Either the rubric mis-scored it or the fix is blocked on something, and both are worth surfacing.
`surface_count` and `first_seen` are what make that detectable.

### 7.3 What a dry run showed, and why there is no selection

An earlier draft of §7 chose two findings a morning and posted them into chat, and nothing else was
published anywhere. Those rules were run against a simulated neglected fleet: two clusters, 32 findings scored by
§4 using real check slugs, 30 of them past §4.4's ownership gate, and nobody fixing anything.

The ranking held up. The order is defensible end to end, both gates sort correctly, and the two
findings that tie at 90 (`probes-liveness` and `probes-readiness` on the same workload) break
deterministically on `_finding_sort_key`. **The selection did not.** An earlier draft gave urgency
both slots and re-offered `L = 10` findings on a flat daily interval; over ninety days it surfaced
**6 of 30 findings**, one of them 90 times.

| selection rule                             | findings surfaced | whole queue seen | worst repeat |
| ------------------------------------------ | ----------------- | ---------------- | ------------ |
| fires take both slots, flat daily re-offer | 6 / 30            | never            | 90×          |
| \+ elapsed deadline becomes L = 10         | 6 / 30            | never            | 90×          |
| \+ second slot reserved from urgency       | 26 / 30           | never            | 90×          |
| \+ re-offer interval widens for L = 10 too | 30 / 30           | day 25           | 16×          |
| \+ that slot batches by object             | 30 / 30           | day 12           | 16×          |

**The failure reproduced #774 inside its own fix.** Twenty-four findings never surfaced at all, and
the highest-scoring of them were not marginal: a `critical` CrashLoopBackOff on a production
payments workload, a `critical` single-replica session store, an expiring quota. They were starved
by two findings that outscored them, neither of which the operator could fix with a manifest edit —
a provider-managed `kube-system` CrashLoop, which §4.4 keeps rankable on purpose, and a certificate
whose deadline had passed. Both re-offered every morning and neither ever resolved.

Note which lever the fix was not. Both starving findings were correctly ranked and correctly
surfaced; the `kube-system` CrashLoop is a real fault and the queue should say so on the day it
starts. Suppressing them would have reached 30/30 by making the queue worse at its job.

Four rounds of selection rules got the number from 6/30 to 30/30 and the fastest full pass down to
day 12. **A published list is 30/30 on day one**, and needs none of them. That is the argument for
§7.1 stated as a measurement rather than a preference: every rule in that table is machinery for
rationing a scarce channel, and the scarcity was self-inflicted. What survives from the exercise is
the ranking it validated and the grouping-by-object it found — one workload in the simulated fleet
carried seven findings and two cluster objects carried five and three, which is why §7.1 groups
rather than lists flat.

The simulation is a design aid, not a test. It assumes the rubric's own scores are right and models
a fleet rather than measuring one. What it can show is a delivery rule starving its own queue, which
it did.

### 7.4 Re-verification, and why the finding has to carry its own check

Re-checking before publishing is one line of §7 and the hardest thing in this document to
build, because there is no generic way to ask a cluster whether a finding is still true. What
"still failing" means is a property of the individual finding: for a CrashLoopBackOff it is a pod
phase, for an expiring certificate a date, for a missing NetworkPolicy the absence of an object, for
a project-scoped service-account key the continued existence of a binding. A per-check-slug query
does not cover it either — the same slug at two objects can need different questions.

Two ways to answer, and the choice matters more than it looks.

**A model turn reasons it out from the finding's prose.** Flexible, and it fails in the direction
that costs most. This is §4.1's instability aimed at the one judgement where being wrong is
unrecoverable: a finding wrongly marked `resolved` leaves the queue and is never offered again.
`derive_finding_id`'s docstring records what that looks like in production — on 2026-08-03 the 16:34
run announced four unfixed criticals as resolved, in writing, three internet-reachable control
planes among them, because a join key had been re-derived rather than computed. A re-verification
that re-reasons the fault from prose every morning is the same mechanism with a shorter fuse.

**The finding carries its own check, written when it was found.** At registration the source has the
detection in hand — it is how it found the thing — so it writes down how to ask again. That is the
`verification` column: `kind` (`kubectl`, `gcloud`, or `manual`), a read-only `command`, and
`still_failing_when`, the condition on that command's output which means the problem persists. §10's
owned checks inherit theirs from the `####` detection query in the owning stream's SOP, which
`test_check_rosters_match_the_sops` already keeps honest; §10.1's three unowned checks acquire one
when they get their sections, which is a third reason to write them; the watcher's comes from the
reason map in §5.1.

The stored check is the default and the model turn is the bounded fallback, not the other way round.
Where `kind` is `manual` — a Workload Identity migration, an SLO practice observation — no command
can settle it, and the list says so rather than guessing.

**What gets verified, now that everything gets published.** Not the whole backlog: verification runs
commands against live clusters, and doing it for thirty findings every morning makes the cost scale
with fleet neglect. The daily job verifies three sets — anything the nudge is about to name, anything
crossing §4.2's floor, and anything about to be promoted (§9) — which is a handful, bounded by what
is being asserted rather than by what is stored. Everything else on the list carries its
`last_verified` timestamp beside it, so a reader can see how fresh each row is. **A list that shows
its own staleness is honest in a way a two-item message cannot be**, which is the one thing
publishing everything makes easier rather than harder.

**Three outcomes, never two.** This is the part that has to survive contact with implementation.

| outcome              | means                                                | effect                                                        |
| -------------------- | ---------------------------------------------------- | ------------------------------------------------------------- |
| still reproduces     | the command ran and `still_failing_when` held        | `last_verified` advances; publish it                          |
| no longer reproduces | the command ran and the condition did not hold       | `resolved` (§3.2); close any open PR through `remediate`      |
| could not verify     | the command failed, timed out, or the object is gone | `last_verified` **does not advance**; stay queued, or `stale` |

Collapsing the third into the second is the entire failure mode. An expired credential, an
unreachable control plane, a `kubectl` that returned nothing because the context was wrong — each
produces "the condition did not hold" from a query that never really ran. The distinction is between
"asked and got no" and "did not manage to ask", and only the first may resolve a finding. Where the
object itself is gone the answer is `stale`, which §3.2 keeps separate from `resolved` for this
reason.

A finding that cannot be verified for several consecutive attempts is worth surfacing as such. It
usually means the queue has lost access to something it used to be able to see, which is a fleet
problem wearing a queue problem's clothes.

### 7.5 The job, and the SOP it runs

All three publishers run from one job in [`jobs.json`](../../agents/platform/cron/jobs.json), with an
SOP under `agents/platform/governance/`, in the shape every other job on that roster already takes:
the entry schedules it and names the profile, the SOP is what the turn actually does. One job rather
than three because they share the same input and the same verification pass, and splitting them
would run it three times.

- **`findings_publish_sop.md`**, beside `inventory_prioritize_sop.md`. Six steps: fetch the list
  from `GET /v1/findings/ranked`; reconcile `pr_state` for rows with a live pull request (§3.2); run
  §7.4's verification on its three sets and post the results back; promote what §9 says to promote;
  rewrite the backlog document (§7.1); then post the nudge, or the alarm, or neither (§7.2).
- **The roster entry**, `deliver: "chat"` like the seven audits — the nudge and the alarm are chat
  messages, and the backlog rewrite is something the turn does with its own tools. Daily, scheduled
  _after_ the daily audit jobs: the latest lands at 09:20, they are themselves a source (§5), and a
  run that goes first publishes a list a day behind its own inputs.

The SOP is deliberately thin on judgement. The ordering is decided by the endpoint (§6.1),
verification by the stored check (§7.4), promotion by §9, the ranking by §4, and whether to post at
all by the change gate (§7.2) — so what is left for the turn is running commands and writing
English, which is what a model should be doing here. An SOP that re-opens any of those decisions has
reintroduced the instability each of them was written to remove.

**Two `queue_publications` rows** (§3.1) carry what the job needs to remember between runs: the
backlog's `target_ref`, so the rewrite finds the document it wrote last time rather than opening a
second one, and the nudge's `content_hash`, which is what "the list changed" compares against.

## 8. How a queued finding reaches a human

The obvious reading of the `chat_id`/`thread_id` columns — that a finding carries the chat it should
be sent to — is wrong, and worth heading off before a reader reaches for it.

**The destination belongs to the delivery mechanism, not the row.** A `deliver: "chat"` cron job
routes through the relay, whose routing and send-then-store ordering
[`cron-report-relay.md`](cron-report-relay.md) owns; what matters here is that `_send_to_chat`
called with no `chat_id`/`thread_id` posts to the bare
active platform, resolved by `get_active_platform` from `config.yaml` and landing in the install's
home channel. No job on the roster carries a chat id; neither does this one.

**The columns are an output.** The relay's own ordering is the pattern: send first, read back the
thread the send resolved to, store only then. On a finding those columns record where it was
surfaced, which is what lets a reply in that thread resolve back to the finding — and is the join to
`incidents`.

**Binding at discovery time would be the bug.** The sweep and the event watcher both produce
findings when no chat session need exist. A destination captured then is either absent or stale by
the time the finding is surfaced.

**Why the one-shot report needed more, and the nudge does not.** The
[`bootstrap_onboarding` plugin](../../agents/chat/defaults/plugins/bootstrap_onboarding/plugin.py)
exists because the single-use delivery job could not fall back to a home channel: it pins the job to
an origin captured from a live human turn and declines to mark the profile aligned when there is
none, so the one copy of the report is never lost. A recurring nudge carries no such risk — a day
with no reachable channel costs nothing, because the list is still there and tomorrow's message
names the same top three. That contrast is the reason the nudge may use the home channel when the
bootstrap delivery may not, and it is stronger here than it was for the daily drip §7 replaced:
the backlog document does not go through chat at all, so a broken channel delays the notification
rather than losing the finding.

**One consequence to name.** A finding named in a nudge lands in the home channel; one surfaced
by an on-demand pull lands in the asking user's thread. Both write their own `chat_id`/`thread_id`,
so a finding surfaced twice needs a rule: the most recent surface wins, because the point of the
columns is to route a follow-up reply, and the most recent surface is the one someone is replying
to.

**Two delivery hazards to verify during implementation rather than assume.** A named-profile cron
job needs `platforms` present in the profile config to reach chat at all, and the Google Chat
home-channel configuration can silently fail gateway delivery if written in the wrong shape. Both
are properties of the deployed config rather than of this design, and both should be checked against
a running install before the job is declared working.

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

**Promote a slice, not the list.** Publishing everything makes this the question it was not
before: if the whole backlog is visible, does the whole backlog get a pull request? No. Scan-time
promotion on a neglected fleet lands dozens of PRs before the user has read one finding, and a
visible list does not change that arithmetic. So promotion is bounded to three findings a run: the
three highest-scoring rows that are `actionable`, whose `remediation.kind` is `manifest`, and whose
`pr_url` is null. The rest sit on the list with their recommendation and no PR until they reach the
top. **Cost follows what is asserted, not what was found.**

**Each of those three conditions excludes a way the slice would otherwise jam.** Bounding on the
nudge's top three alone would let three findings that cannot move hold the pipeline shut behind
them, and the workload whose missing `readinessProbe` is a four-line diff would wait on them
indefinitely. §7.2's stuck-at-top rule notices that state and says so in the nudge; it does not
clear it.

- **`kind = manifest`** excludes what no pull request can express — a Workload Identity migration, a
  control-plane upgrade, a provider-managed fault, which §4.4 keeps at the top of the list on
  purpose and which can sit there for as long as a support case takes.
- **`pr_url IS NULL`** makes promotion one-shot per finding. A row leaves the slice the moment it
  has a pull request and does not come back, whatever becomes of it: `remediate` "leaves a live PR
  untouched rather than force-pushing over a reviewer", so three findings under review — the normal
  steady state of a working install — would otherwise occupy every slot until someone merged them.
  A human-rejected PR is the same shape and worse, because `remediate` "never [re-proposes] after a
  human's rejection", so that row could never yield anything and would hold a slot forever. Nothing
  is lost by dropping it: re-proposal after a harness stale-close happens on `remediate`'s own
  branch, on the audit stream's schedule, and needs no slot here.

Verification (§7.4) already covers "anything about to be promoted", so it follows the slice without
a rule of its own.

The list should say so, per row, rather than leaving a reader to infer that a missing PR means the
harness failed to produce one. A finding below the slice reads "no fix prepared yet"; one whose
`remediation.kind` is not `manifest` reads that no fix can be prepared at all. Those are different
statements and conflating them is how a reader stops trusting the column.

**No `/remediate` command on this path.** The word has two senses worth separating. The CLI
subcommand is the mechanism; `/remediate <finding-id>` on a ledger issue is one caller of it, and
auto-promotion inside `finish` is the other. Fleet-audit gates its long tail behind the human
trigger because seven streams reporting at once would otherwise be "a notification firehose". The
top-slice bound above is already that gate, so the trigger would be a second lock on the same door.
The firehose argument does not transfer to the backlog document either: it is one document that
replaces its own contents, so a fleet with sixty findings produces the same single notification as
one with six.

**Not every finding can have a pull request, and the list must say which it is giving.**
`remediation.kind` is `manifest`, `gcloud`, or `manual`. A Workload Identity migration or a
control-plane upgrade is not a file in a repository. Those findings surface with their
recommendation and no PR link, stated as such rather than left ambiguous.

A provider-managed fault (§4.4) is always one of these. It is `manual`, its `note` names the support
case or the upgrade rather than a patch, and it is the case where saying so explicitly matters most:
this is the one class of finding that can head the list and offer no fix, and a reader who is not
told why will read the missing PR as the harness having failed to produce one.

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
a re-rank. The user is told once. The queue's computed severity governs the row and the stream's
stated severity governs the ledger issue, and they can differ on the same finding — they are answers
to different questions, one asking where this ranks against the whole fleet and one asking how bad
it is for that stream. Promotion carries the queue's, since both use `SEVERITIES`' three words and
the promoted document is the queue's claim about the finding. Had the sweep kept its own vocabulary, the same problem would carry
two ids, appear once on the list and again on a ledger, and nothing in the schema could tell that from
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

This keeps the fleet-audit ledgers out of the queue's job. `findings` remains the ranked source and
§7.1's backlog document its published form; the audit streams keep their own ledger issues, one per
stream, reporting on their own scans. The queue's document sits alongside them and is not one of
them — same mechanism, different owner and different question. The only thing crossing between them
is a PR's label and its `Part of #` backlink.

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
`remediate` wholesale. It leaves the shape of the first-time report alone — the delivered
`INVENTORY.md` keeps its five-item cap and is still posted verbatim, and its `Also found: N items`
line gains a link to the backlog (§5). What does change is which five: §5 renders the report from
what the sweep registered, so §4's single scale decides the order and §4.2's thresholds supply the
severity word, where `inventory_prioritize_sop.md` today preserves the severity each finding was
found with. That is the point of computing on one scale (§4.1) and not a side effect, but it is a
change to the report and this section should not claim otherwise. And `INVENTORY.raw.md` stays where
it is as the full-detail record of what a sweep saw, which the queue references rather than
replaces.

## 12. What building this consists of

Collected from the sections above so an implementation can be scoped and split, in dependency order.
Each item names the section that specifies it; nothing here is new.

**The core — no repository code in any of it (§6.2).**

| #   | deliverable                                                                                           | where |
| --- | ----------------------------------------------------------------------------------------------------- | ----- |
| 1   | `findings` and `queue_publications` tables, indexes, and exemption from `cleanup_old_records`         | §3.1  |
| 2   | Upsert rules per existing state, including sticky `dismissed`, and the absence-lowers-confidence rule | §5.2  |
| 3   | The seven HTTP endpoints, with the ordering as a tested Python function rather than a prompt          | §6.1  |
| 4   | Thin MCP tools over those endpoints on `platform_mcp_server.py`                                       | §6.1  |

**The writers.**

| #   | deliverable                                                                                   | where      |
| --- | --------------------------------------------------------------------------------------------- | ---------- |
| 5   | `inventory_prioritize_sop.md` extended to register the full extracted set with rubric vectors | §5         |
| 6   | The rubric's anchors and worked examples written into that SOP                                | §4.2, §4.3 |
| 7   | Reason-to-check map in `k8s-event-watcher`, plus its registration call                        | §5.1       |

**The publishers and the job that runs them.**

| #   | deliverable                                                                                        | where    |
| --- | -------------------------------------------------------------------------------------------------- | -------- |
| 8   | `findings_publish_sop.md` and its `jobs.json` entry, scheduled after the daily audits              | §7.5     |
| 9   | The backlog publisher: render the ranked list, create-or-rewrite its document, record `target_ref` | §7.1     |
| 10  | The nudge's `content_hash` change gate and weekly floor, and the alarm's `alarmed_at` edge trigger | §7.2     |
| 11  | Verification of §7.4's three sets, and its three outcomes including "could not verify"             | §7.4     |
| 12  | Promotion-slice promotion through `remediate`, and `pr_state` reconciliation for open promotions   | §9, §3.2 |

**Independent.**

| #   | deliverable                                                                                                                       | where |
| --- | --------------------------------------------------------------------------------------------------------------------------------- | ----- |
| 13  | Three `####` sections — `startupProbe`, `readOnlyRootFilesystem`, ResourceQuota/LimitRange — in the SOPs of their nearest streams | §10.1 |

Items 1–4 stand alone and can be built and tested with no repository and no cluster, which is the
practical point of §6.2's boundary. Items 5–7 can land in any order once 4 exists. Items 8–12 need
everything above; 9 is the only one that talks to a repository, and 10 works against chat alone, so
an install with no GitOps repository can run the nudge without the backlog. Item 13 is independent of
all of it and is work the audit streams arguably owe anyway.

Two things to settle against a running install rather than on paper, both already flagged: the
`platforms` key a named-profile cron job needs in order to reach chat at all, and the Google Chat
home-channel shape (§8).
