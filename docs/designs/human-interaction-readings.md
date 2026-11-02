# Human interaction: chat sessions and the people behind them

**Status:** proposed, 2026-10-30. Workstream 4 of the telemetry epic that asked five product
questions of `status.usage` (its question: how often do customers interact with kube-agents).
Builds on [`usage-counters-producer.md`](usage-counters-producer.md) (the counters and the poller)
and [`proposal-outcome-readings.md`](proposal-outcome-readings.md) (the readings slot: a gauge the
operator projects as it stands, with its own miss streak).

## 1. The question and what answers it today

"How often do customers interact with kube-agents" has three parts: how many conversations
people start, how many people they are, and how a maintainer who does not run the install can
read either. `status.usage` answers the first only in mixed form and the other two not at all.

| Part                       | Today                                                                                                                                                                                                                                                                                  | Gap                                                                                                                                        |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Conversations people start | `sessionsTotal` folds two document counters: the event watcher's triage sessions (`k8s_event_watcher_triage_sessions_total`) and the chat sessions (`kubeagents_chat_sessions_total` from the Hermes plugin and `kubeagents_a2a_gateway_sessions_total` from the A2A gateway, summed). | A quiet install whose watcher triages events reads a growing `sessionsTotal` with no person involved. The chat half is only in Prometheus. |
| People                     | `chatMessagesInboundTotal` and `lastHumanActiveTime` say that someone wrote, and when. The session store keeps a pseudonym per session (`SessionMetadata.user_id`, an HMAC of an address, or a platform's opaque member id).                                                           | Nothing counts distinct people. One person writing fifty messages and fifty people writing once read the same.                             |
| A maintainer's read        | Every number lives in the customer's project: the CR, the operator's gauges on its metrics port, the copy-paste summary block.                                                                                                                                                         | The epic asks for the answer to be written down, so nobody adds an egress to get it.                                                       |

## 2. Decisions

### 2.1 `chatSessionsTotal`: the chat half of `sessionsTotal`, as its own field

The poller already keeps the two halves as separate document counters (`sessionsChat`,
`sessionsTriage`) and projects their sum to `sessionsTotal`. The chat half gets a status field of
its own, `chatSessionsTotal`, projected from `sessionsChat` alone. `sessionsTotal` stays the sum:
the summary and existing dashboards read it, and a split that renames it buys nothing.

`chatSessionsTotal` is a counter under the standard contract (`…Total`, monotonic across restarts,
clamped to `[0, 1<<53]`): the same document counter, one more projection. On a next install the
chat half sums the Hermes plugin's sessions series and the A2A gateway's, as `sessionsTotal` does
today. Its first value is the document's chat half as it stands. An upgrade keeps the usage
document, and the poller's re-seed (`usageStatusSeed`) puts the whole of `sessionsTotal` into that
half, triage included, because a status without the new field cannot tell the halves apart: an
install whose document was ever re-seeded while `sessionsTotal` was above zero starts
`chatSessionsTotal` with those triage sessions in it, once, and the docs say so. The seed changes
in one case only: a status that carries `chatSessionsTotal` seeds the chat half from it, clamped
to `sessionsTotal`, and the triage half from the difference. A status without the field, whatever
the reason (the first poll after the upgrade, a served CRD that holds the field back, a return
from a rollback whose write dropped it), seeds as today, the whole into the chat half, so a re-seed
never hides past chat sessions in the triage half. The floor rule stays what it is: the pair's
floors are summed, and a half is unfolded only when the status carries no sessions at all, so a
document that predates the halves migrates under a split seed as it does under today's. The
read-back floor on the halves gains `chatSessionsTotal <= sessionsTotal`. It does not move
`lastHumanActiveTime`: a chat session begins with a message `chatMessagesInboundTotal` already
counted, which is the rule `usageHumanCounters` states. The export gauge
(`kubeagents_platformagent_usage{field="chatSessionsTotal"}`) and the feedback summary follow the
field, and a served CRD that predates it holds it back under the pruning record, like the other
newer fields.

### 2.2 People: a count of pseudonyms the A2A gateway has seen in a window

Next-mode installs are the target: chat arrives through the A2A gateway, which resolves the sender
to a principal on every turn and hashes it (`Authority.Requester.Principal`, `hmac:` plus a
salted HMAC-SHA256 prefix, `Pseudonymizer` in `a2a/gateway/principal.go`). The gateway is the
producer. The Hermes session store is not: a today install gets no people count in this design,
and a platform a next install still serves through Hermes (Slack, while Chat is on the A2A door)
is outside the count until it moves to the door. The docs say both.

**State.** The gateway keeps its durable state in NATS JetStream key-value buckets (`session-state`
without a TTL, `feedback` with a 720-hour one). It gets one more bucket, `people`, history 1, a
720-hour TTL, a small size cap, and the same grants as `feedback`:

| Key            | Value                              | Written                                                                                                                                             |
| -------------- | ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `people.<hex>` | the time of the last put, RFC 3339 | on each verified turn from a chat platform, for that turn's principal, at most once an hour per key, so the time is within an hour of the last turn |

`<hex>` is 32 hex characters of an HMAC-SHA-256 of the resolved principal under a key derived for
this bucket alone (HKDF of the attribution salt with the info string `people`), so a key joins
neither the authority block's `Requester.Principal` nor the session store's pseudonyms, and a
key-value key admits no colon anyway. A person is that principal: the principal map folds a
platform's member id into one identity where an entry exists, and no backend is part of the key.
A put refreshes the entry, so the bucket's TTL deletes a person's key 30 days after their last
put, within an hour of their last turn; the hourly bound keeps a busy conversation from writing
the bucket on every message. A put that fails (the bucket full, the bus away) is logged and never
ends the turn; the count under-counts until the next put succeeds. The bucket's size cap is sized
for a hundred thousand people, far past any install, and the gateway checks at start that the
bucket has a max age, as it does for `feedback`, and writes nothing to one without.

This is a new durable per-person record, and the design says so rather than calling it nothing:
one pseudonym and one last-active time per person, for 30 days after their last turn. Who can
read it: the gateway's NATS user reads and writes the bucket; the provision and seed users hold
stream create and info on it, as on `feedback`, and stream info lists a bucket's keys (the ChatOps
page says the same of `feedback`), so they can list the pseudonyms, never the people. Anyone who
holds the attribution salt and a directory of addresses can re-identify a key of a Google Chat
sender by hashing the directory, as they can the authority block's pseudonyms today; the salt sits
in the credentials Secret, which the inference gateway also mounts, and narrowing that mount is a
follow-up outside this design. The record has an off switch, `spec.telemetry.peopleSeen` (the chart's
`platformAgent.telemetry.peopleSeen`), on by default: off, the gateway writes no key, deletes
every key the bucket holds at its next start, and serves no sample, and the two fields stay
absent. A new spec field has the chart's standing guard: `platform-agent-cr.yaml` refuses to
render `peopleSeen: false` against a served CRD that lacks the field, with the `lookup`-gated
`fail` every other new spec field there carries, because `helm upgrade` never applies `crds/` and
an API server serving the older schema would prune the value silently while the record is
written; `upgrade.sh --upgrade-mode=harness` and a hand `lifecycle.sh apply` take that route. A single person is removed with a key-value delete of their key by whoever holds the gateway's
credentials; the gateway's watch honours deletes, so the count drops at once rather than at the
TTL. A delete, the purge included, removes the value and leaves a tombstone that carries the key
name (the gateway's restricted permissions refuse the stream purge that would clear it, as the
ChatOps gateway design records): a pseudonym under the bucket's own key, with no time, listable
by the provision and seed identities until the bucket's max age expires it, within 30 days. So a
removed person is gone from the count at once and from the bucket's subjects within 30 days,
which is the bound the anti-goal below sets. The ChatOps page gets a paragraph that says all of this, beside its "Answer feedback" section,
which is where the page already says what a kept record holds and who can read it.

A turn counts when the gateway has verified its sender and its backend is a chat platform a
person writes on: one of the gateway's three chat backends, Google Chat, Slack or Discord, named
as such in the code and not by reference to any counter's label set. The program doors (`inject`,
`a2a`, and the Google door) are not people, and an eval or CI install must not read its inject
principals as such; the console is not counted either, since every console user resolves to the
one principal `nats:console`. The inbound counter counts before the sender is verified and counts
the console, so the two populations differ by the unverified share and the console's turns.

**Series.** The gateway's metrics listener, the one the usage poller already scrapes on a next
install, serves the count as a gauge computed at scrape time from the bucket's keys and values:

| Series                                       | Type  | Labels                   | Meaning                                                                                                                                                                                                                                                                          |
| -------------------------------------------- | ----- | ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `kubeagents_a2a_gateway_people_seen{window}` | gauge | `window` = `7d` \| `30d` | Distinct principals whose recorded time, the last put and so within an hour of their last verified chat turn, is inside the window; both counts are taken from the recorded times (a watch does not see a key age out, so the bucket's TTL is storage pruning, never the count). |

The windows are fixed: 30 days is the window the stalled-onboarding alert already reads
`lastHumanActiveTime` against and the bucket's lifetime; 7 days is a week. The listener serves
every gateway series under one scrape timeout, so the count must not cost a scan per scrape: the
gateway keeps the bucket's keys and times in memory, loaded once at start and fed by a watch on the
bucket, drops an entry from its own count once its time leaves the 30-day window, and the scrape
counts the map. While the map is not loaded (the first seconds after a start, or a watch that fell
behind and is being rebuilt) the gauge serves no sample, never a stale or partial number.

**Salt.** The gateway's salt is `SESSION_KV_SALT`, which the installer reconciles on every install
and upgrade, then `A2A_ATTRIBUTION_SALT`, then a value derived from the NATS password: stable
across restarts in every case, so a person hashes the same from one day to the next. A rotation of
the salt gives everyone a new key and reads as more people for one window; the docs say so.

**The operator** projects the gauge as a reading, through the slot `proposal-outcome-readings.md`
added: a third reading kind, `people`, with two fields, `peopleSeen7d` and `peopleSeen30d`
(`*int64`, a 0 is a reading, absent until the first reading and after two polls in a row without
one, `max` across pods, never moving `lastActiveTime`). They are readings and not counters because
a window count falls when people stop writing, which a monotonic counter cannot say. The operator reads the `people` reading only while no chat platform the install enables is
served through Hermes: not Chat on its legacy consumer, not Slack on its legacy consumer (the
facts that choose the Slack consumer), and not Teams, which Hermes alone serves and the gateway
has no backend for; while any of them is, the fields stay absent, since a real 0 would say nobody
wrote while those platforms' users were writing, and the summary prints "no reading". The slot as
that design specifies it reads the broker's body alone; this design generalises it to a target per
reading, the
`people` reading coming from the A2A gateway's listener. Its miss rule follows the target: a poll
that read the gateway and found no `people_seen` sample, or failed to read it, is a miss; an
install with no A2A gateway never reads one, and the fields stay absent. The work ships as two pull
requests: the first carries `chatSessionsTotal` (2.1), the bucket and the series, which depend on
nothing; the second carries the operator's `people` reading, the summary rows and the five answers
(2.4), and lands after `proposal-outcome-readings.md` has merged (merge order, not a stack). The
two fields are exported on the usage gauge and printed by the summary with a source suffix ("as
the gateway reports"), the form that design introduces for the forge's readings.

**Privacy.** The bucket holds the pseudonyms the gateway already stamps on every turn's authority
block, for 30 days after a person's last turn, under the grants `feedback` has (the gateway user
reads and writes it; the provision and seed users hold stream create and info on it, as on
`feedback`).
What leaves the pod
is an integer per window: the scrape is the operator's and a collector's, the pseudonym appears in
no label, no log line and no status field, and the summary prints the integer. A count of one says
that one person used the install, which `chatMessagesInboundTotal > 0` and `lastHumanActiveTime`
already say. The zero-PII contract of `usage-counters-producer.md` holds: enums and integers only.

### 2.3 How a maintainer reads these across installs

The answer the epic asks to have written down: kube-agents adds no egress for its usage numbers.
A maintainer reads an install's `status.usage`, these fields included, in one of two ways, both
under the customer's control:

- the operator's gauges (`kubeagents_platformagent_usage`, `_usage_time_seconds`, `_phase`) on the
  manager's metrics port, scraped by the customer's collector into a Prometheus or Managed
  Prometheus project the customer chooses to share, the dashboards and alerts reading that project;
- the summary block of the feedback summary, which the customer copies into a ticket or a chat.

Nothing beyond those by design: a third path would be an egress of usage data the customer did
not configure. `usage-counters-producer.md` gets this as a short section, so the next person who
asks finds the decision beside the counters it governs.

### 2.4 The five questions, answered on the observability page

The epic's first "done when" is a one-line answer per question on the observability page, naming
the field. Four of the five are answered by `proposal-outcome-readings.md` and the fifth by this
design; the page gets a short list in the second implementation pull request, once every field it
names exists on `main`:

| Question                             | Field                                                                                                                                                                                                                                  |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| How many PRs does kube-agents create | `remediationsProposedTotal` (every `proposal-create` that succeeded); `proposalsAuthored` (the forge's count now)                                                                                                                      |
| How many merge                       | `remediationsMergedTotal`, a reading of the forge                                                                                                                                                                                      |
| How many close without a merge       | `remediationsClosedUnmergedTotal`, a reading of the forge                                                                                                                                                                              |
| How many human touches per PR        | `proposalHumanTouches` over `proposalsAuthored`; the per-proposal distribution is `kubeagents_vcs_proposal_touches_per_proposal` in Prometheus                                                                                         |
| How often do customers interact      | `chatSessionsTotal` and `chatMessagesInboundTotal` for how much (console conversations included), `peopleSeen7d` and `peopleSeen30d` for how many people (the console's one shared principal excluded), `lastHumanActiveTime` for when |

The breakdown of proposals by what started them (Workstream 3) is not part of this design.

## 3. What changes, by component

- **A2A gateway** (`a2a/gateway/`): the `people` bucket, named in `a2a/lib/kv.go` beside
  `feedback` and created and granted wherever `feedback` is: the provision Job's `kv add` line
  and the `a2aProvisionedStreams` seed list in `platformagent_a2a_manifests.go`, the gateway
  user's `$KV.people.>` publish and subscribe grants and the provision identity's stream grants
  in `platformagent_a2a_identities.go`, the bucket's name in `a2aGatewayJetStreamGrants`
  (stream info, direct get, consumer create and consumer delete, the four `feedback` carries,
  which the bind, a read, the watch and its stop need) with `TestGatewayGrantNamesTheBucketItsDataPlaneWritesTo` extended to it, and every
  other place that spells a bucket's name (the operator's bucket constant, the hard-coded stream
  lists and the authorization test, the auth-callout goldens); the hourly-bounded put on each
  verified chat turn after the principal and backend checks; the key derivation (HKDF of the
  attribution salt, info `people`); the start-time check that the bucket has a max age, as
  `feedback`'s; the `spec.telemetry.peopleSeen` switch, read from the CR by the operator into the
  gateway's environment, with the purge at start when it is off; the watch-fed map, which does not
  ignore deletes, and the gauge on the metrics listener. Tests beside the gateway's: a turn
  writes the key once an hour and a door or console turn never, a failed put leaves the turn
  alone, the two windows count the right keys from their times, an entry past 30 days leaves the
  count without a restart, a deleted key leaves it at once, an unloaded map serves no sample, the
  key of a principal differs from its authority hash, the switch off writes nothing and purges,
  and the bucket's TTL and grants are rendered and named in step
  (`TestSeedGrantsAndProvisionScriptNameTheSameStreams` extended to the identities file).
- **Operator** (`usage_counters_scrape.go`, `usage_counters_fold.go`, `usage_counters_poller.go`,
  `usage_gauges.go`, `common_types.go`, the CRD copies): `chatSessionsTotal` projected from
  `sessionsChat`; the one new seed case in `usageStatusSeed` (a status that carries the field),
  `usageFieldFloor` unchanged in rule and extended to read the split, and the read-back floor; a
  target per reading in the slot; the `people` reading kind, read from the A2A gateway's listener
  only while every enabled chat platform is on the door, with its two specs and miss streak; the `spec.telemetry.peopleSeen` field and its render into the gateway; the rule that reads the
  reading only while no chat platform is served through Hermes (Chat, Slack, Teams); the pruning
  record for the three newer fields; the export rows. Tests beside the existing `usage_counters_*_test.go`: the projection, a re-seed from a
  status with the field (split, and clamped when the chat half is above the sum) and one from a
  status without it (the whole into the chat half, as today),
  `TestMigrateUsageDocument_TheSessionsHalvesAreUnfoldedOnlyWithoutASessionsStatus` extended to the
  split seed shape, the reading's presence and clearing, the fall projected as it stands.
- **Chart** (`charts/kube-agents/templates/platform-agent-cr.yaml`, `values.yaml`, the schema):
  `platformAgent.telemetry.peopleSeen` rendered into `spec.telemetry`, with the `lookup`-gated
  `fail` against a served CRD that lacks the field, beside the guards the other new spec fields
  carry, and a test beside theirs in `tests/`.
- **Feedback summary** (`feedback_summary.py` and `a2a/gateway/feedback_summary.go`): the three
  fields, the two readings with their source suffix, the two copies held in step by
  `GatewayCopyTest` in `test_feedback_summary.py`, which reads the Go copy (added with the proposal
  readings' implementation; this design extends its expected lists).
- **Series names** (`tests/test_usage_counters_series_names.py`): the new family and its label
  pinned between the gateway and the operator.
- **Docs**: the observability page (the series, the fields, the five answers, the three fields
  absent on an older served CRD beside the existing skew note), the CRD page (the same, and the
  switch), the fields page (generated), the ChatOps page, beside its "Answer feedback" section (the per-person record, who can read it, the switch, the deletion), `usage-counters-producer.md` (the cross-install
  section), and a pointer in `proposal-outcome-readings.md` where it defines the slot this design
  generalises.

## 4. Goals, non-goals, what stays out

Goals: a per-field answer to how many conversations people start and how many people they are;
no new state beyond one small key-value bucket that prunes itself; no new egress.

The epic's "done when" also asks for `chatSessionsTotal`'s history to be honest: an install
re-seeded before this release reads its first value with triage sessions in it, said above and in
the docs, rather than hidden behind a baseline the poller would have to keep.

Non-goals: per-person anything (no field, label or log carries a pseudonym); a split of
`sessionsTotal` itself; a people count on a today install, or for a platform a next install still
serves through Hermes; a count of people who only read (reactions and views are not interactions
here).

Anti-goals: a record of more than a pseudonym and a last-active time, or kept longer than 30
days, which would make the bucket a roster of who uses the install; a count that a maintainer can
reach without the customer handing it over.

## 5. Eval exemption

Exempt under `.agents/rules/eval_driven_development.md` ("When it applies"): the change alters
nothing an agent does. It is a key-value bucket, a metrics listener, the poller and the status
schema, with no model call in any of their paths; no prompt, skill or SOP changes, and no text
reaches the model.

## 6. Left to the builder

Where in the turn path the put sits (after the principal resolves and the backend check, before
or after the session mint); how the watch rebuilds its map after a gap; the exact pruning record
name; the order of the new rows in the summary block; test fixture shapes.
