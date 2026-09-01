# A2A payload spec (a2a-jetstream/0.4)

- **Author:** @bnaylor
- **Date:** 2026-08-24
- **Status:** draft for review
- **Supersedes:** the demo protocol (`a2a-jetstream/0.1`). 0.2 was this doc's
  pre-amendment draft, never implemented; 0.3 added the ratified `authority` rules; 0.4
  moves the addressee into the task subjects, which is what makes connection-time
  authorization expressible on the task plane.

## Purpose

This document defines the wire protocol for agent-to-agent messaging over NATS JetStream:
the envelope, the payload schemas, the task lifecycle, and the topic namespace. It is the
contract the stage 1 client library implements. The doc ends with the conformance
assertions that library must pass.

The demo protocol worked, but it smashed A2A semantics together with NATS-native message
shapes in one flat structure. This spec is where that stops. The fix is a layering rule,
not a new protocol - the envelope is ours, the payloads are standard A2A, and neither layer
reaches into the other.

Companion docs: the NATS deployment spec owns stream provisioning, accounts, and
connection-time authz. The chatops gateway design owns what a user session is. This doc
defines subjects and retention classes. (Both docs landed 8/24; the layout here won the
reconciliation, and the deployment spec binds to these subjects.)

## A2A schemas or NATS-native shapes?

This decision has gone back and forth twice, so it gets an actual argument here rather than
an assertion, plus the conditions that would flip it.

The two candidates:

- **A2A** (Linux Foundation, currently 1.0): task state machine, typed message Parts,
  taskId/contextId correlation, agent cards, streaming status and artifact events.
  Designed for HTTP/JSON-RPC, but the object schemas are transport-independent.
- **The Synadia agent protocol** (currently 0.3): NATS-native request/reply against a live
  harness. Verb-first subjects, discovery via `$SRV`, prompt in, typed chunks streamed
  back, empty-payload terminator. Simple and idiomatic.

The Synadia protocol is good at what it is for: talking to a harness process that is alive
right now. There are four things it does not have. The bus needs all four:

- **Durability.** It runs over core NATS request/reply. Status is answered by asking the
  live agent, so if nobody was subscribed, the answer is gone. We want status answered by
  stream replay, because replay is also the audit trail. This is probably the single
  biggest reason to not build the interior on it.
- **A task lifecycle.** Its stream protocol is a 60-second inactivity timeout and an
  empty-message terminator, and lost chunks are undetectable by design (the spec says so
  plainly). Fine for an interactive prompt. Not fine for a task that runs for an hour and
  needs a durable terminal state.
- **Typed payloads.** Prompt text plus base64 attachments. Agent-to-agent handoffs need
  structured data - there is decent evidence that unstructured narrative handoffs degrade
  downstream task feasibility badly compared to schema-constrained ones
  ([arXiv:2607.18265](https://arxiv.org/html/2607.18265v1) measured roughly 48% vs 96%).
- **Correlation across hops.** There is no identifier that survives one agent asking
  another agent to do something. Our whole audit story is "one identifier spans the user's
  question, every hop, and the change it caused."

A2A has a named construct for each: durable status/artifact update events, the task state
machine, typed Parts (text, data, file), and taskId/contextId. It also has `auth-required`
as a first-class task state, which gives the parked authority work somewhere to land
without a protocol rev.

The honest counterargument: adoption surveys consistently show A2A being used at trust
boundaries between organizations, while teams that own all their agents in one process use
their framework's native shapes. If our interior were one process, that logic would apply
and Synadia-native would win. It is not one process. It is multiple agents with
independent lifetimes, a durable audit requirement, and a gateway that will eventually face
external A2A callers. The boundary-driven case is our case.

What we do not get from this choice: A2A ecosystem client libraries, which all assume HTTP.
The bus mapping deviates from A2A-over-HTTP in a few places (noted below), so the library
is ours to write either way. What we get is the object schemas, the lifecycle semantics,
and a gateway edge that speaks standard A2A to the outside world without translation.

**Verdict: A2A objects are the payload layer on the bus. The Synadia protocol survives in
exactly two places** - the harness edge, where the adapter speaks it to the local harness
process, and the presence plane (heartbeats and `$SRV` discovery), which carries no task
payloads and is already Synadia-compatible. Everything between agents is an A2A object in
our envelope.

### What would flip this answer

The decision rests on two claims. Each is falsifiable in stage 1. If either fails we
should flip rather than patch.

1. **Every interior property has a named A2A home.** The test case is the Synadia
   mid-stream `query` chunk: the adapter must map it onto an `input-required` status
   transition, and the reply onto a follow-up Message with the same taskId, as a stateless
   per-task translation. If that mapping needs adapter state beyond the current task, or
   needs new envelope-level semantics, the claim is false.
2. **Extensions stay in the envelope.** If during stage 1 we find ourselves stuffing
   semantics into A2A `metadata`/`extensions` fields because A2A has no home for them
   (beyond routing and trace, which the envelope owns), we have rebuilt the demo's hybrid
   with extra steps. At that point Synadia-native with a homegrown lifecycle is the
   honest design.

And the reverse: the Synadia protocol is 0.x and moving. If a future version grows a
durable JetStream task lifecycle, this argument should be re-run, not defended.

## The envelope

Every message on the A2A subjects is one JSON envelope.

```json
{
  "protocol": "a2a-jetstream/0.4",
  "envelopeId": "env-8f3a…",
  "correlationId": "corr-2b91…",
  "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
  "taskId": "task-77c0…",
  "contextId": "ctx-51ee…",
  "ts": "2026-08-24T17:00:00Z",
  "from": { "session": "worker-brisk-otter", "agentType": "claude-code" },
  "to": { "session": "chatops" },
  "identity": null,
  "authority": null,
  "kind": "message",
  "payload": {}
}
```

**The layering rule: everything below `payload` is a standard A2A object. Everything above
it is ours.** The envelope owns transport concerns - routing, correlation, identity,
versioning. Payloads never carry routing or identity, and the envelope never carries task
content. This rule is what the conformance suite enforces and what the demo protocol
lacked.

### Field rules

| Field                  | Rules                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `protocol`             | Required. Major.minor; bump major on breaking change. Consumers MUST reject unknown majors and MUST ignore unknown envelope fields within a major.                                                                                                                                                                                                                                                                                                                            |
| `envelopeId`           | Required, unique per envelope. The dedup key: JetStream redelivery means consumers will see repeats, and this is how the library delivers each envelope to the application at most once. The dedup window is bounded - an LRU or time window sized to the redelivery horizon (`MaxAckPending` × ack wait, plus margin), never an unbounded set that grows for the life of the process.                                                                                        |
| `correlationId`        | Required. Minted once by the gateway at the user interaction that starts a task. Copied verbatim on every hop; never re-minted by an intermediary. A task spawned in service of another task inherits its parent's value, and a follow-up or steer to a running task carries the task's original value - the steer is attributed by its own envelope and `authority` block, not by a new correlation. This is the identifier that spans question, hops, and resulting change. |
| `traceparent`          | Optional. W3C trace context, for OTel tooling. `correlationId` is authoritative; `traceparent` is a convenience and may be re-parented per span.                                                                                                                                                                                                                                                                                                                              |
| `taskId` / `contextId` | Required for kinds `message`, `status-update`, `artifact-update`, `cancel`. Optional for `topic-update` (present when a topic write happened in the course of a task - see Topics). Absent for `agent-card`, `agent-closed`.                                                                                                                                                                                                                                                  |
| `ts`                   | Required. ISO-8601 UTC.                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `from`                 | Required. Routing and display only. `from.profile` (optional, added 8/24) names the AgentProfile a worker runs as, so renderers don't parse session names. Until the identity work lands the whole field is publisher-asserted and MUST NOT be used for authorization - a compromised publisher can claim any value here.                                                                                                                                                     |
| `to`                   | Optional. Addresses an envelope to a named session. Consumers on a wildcard MUST ignore envelopes addressed elsewhere.                                                                                                                                                                                                                                                                                                                                                        |
| `identity`             | **Reserved.** See below.                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `authority`            | **Reserved**, advisory. Populated by the chatops gateway only. See below.                                                                                                                                                                                                                                                                                                                                                                                                     |
| `kind`                 | Required. Enum below; selects the payload type.                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `payload`              | The A2A object, per kind.                                                                                                                                                                                                                                                                                                                                                                                                                                                     |

One rule spanning three fields: `correlationId`, `taskId`, and `contextId` are minted as
opaque random tokens and MUST NOT embed backend identifiers, thread titles, emails, or
any user content. They are the identifier class that escapes the pseudonymization rule
(they ride every subject and every envelope in the clear), and they stay clean by
construction, not by redaction - a `corr-{threadTitle}` would quietly put labelled
content on the bus.

### Reserved fields: `identity` and `authority`

The security work is parked, but adding these fields later is a protocol rev and reserving
them now is two names. So they are reserved now.

- `identity` will carry the verified identity of the publisher - the real one, bound to the
  authenticated connection, as opposed to the advisory `from`.
- `authority` will carry a _reference_ to an attenuating capability held in KV - who
  originally asked, what scope they hold, what this hop is permitted to do, each further
  hop a strict subset - per the capability envelope design
  (`docs/architecture/09-capability-envelope.md`): no token format, nothing signed in the
  envelope, the message carries a lookup id. The A2A `auth-required` task state is
  reserved alongside it.

Rules until then (**amended 8/24**, ratified from the gateway design): `identity` MUST NOT
be populated by anyone. `authority` is populated by the chatops gateway at ingress and by
nothing else - the verified requester and the audience snapshot, carried for audit and
parity testing. It is advisory: until connection-bound publisher identity arms, nothing
stops a bus client from inventing an `authority` block, so consumers MUST NOT make any
authorization decision on either field. Libraries MUST pass both through untouched. The
open question on where `identity` actually lives (header vs signed claim) is at the end of
this doc.

### Kinds and payload types

| `kind`            | Payload                       | Notes                                                                                                                             |
| ----------------- | ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `message`         | A2A `Message`                 | Task submission, and follow-up input to an `input-required` task. `role`, `parts[]`, `messageId`, `taskId`, `contextId` per A2A.  |
| `status-update`   | A2A `TaskStatusUpdateEvent`   | State transitions. Terminal events set `final: true`.                                                                             |
| `artifact-update` | A2A `TaskArtifactUpdateEvent` | Streamed output, including incremental chunks per A2A chunking rules.                                                             |
| `cancel`          | empty object                  | A2A models cancel as an RPC method, not an object, so the envelope kind is the method. `taskId` in the envelope names the target. |
| `agent-card`      | A2A `AgentCard`               | Published by the profile's owner when the profile is created, not by workers.                                                     |
| `agent-closed`    | empty object                  | Tombstone on profile deletion; replaces the card.                                                                                 |
| `topic-update`    | A2A `Artifact`                | See Topics.                                                                                                                       |

A kind/payload mismatch is a protocol error, not something to pass through.

Payload size: the library enforces the bus's max message size client-side and fails with
an A2A error before publishing - the server refuses an oversized publish with a protocol
error and a closed connection, and the failure should be a typed error at the source, not
a transport failure downstream.
FileParts above the inline threshold - 128KiB dev default - MUST use `uri` rather than
`bytes`, backed by the JetStream Object Store (decided 8/24; see Open Questions).

## Task lifecycle on JetStream

Task states are A2A's: `submitted`, `working`, `input-required`, `completed`, `failed`,
`canceled`, and `rejected` (native in A2A 1.0) for an executor that refuses work before
starting it.
(`auth-required` is reserved with the authority field.) Terminal states are `completed`,
`failed`, `canceled`, `rejected`.

### Subjects

| Subject                                   | Carries                                                                                                                                                                                                                                                              |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `a2a.tasks.{addressee}.{taskId}.in`       | `message` (submission and follow-up input) and `cancel`, requester to executor. Two reader roles by design: the dispatcher consumes new-task submissions; the executor's own ephemeral consumer takes everything after the submission (follow-ups, steers, cancels). |
| `a2a.tasks.{addressee}.{taskId}.events`   | `status-update` and `artifact-update`, executor to anyone                                                                                                                                                                                                            |
| `a2a.agents.{profile}`                    | `agent-card` when a profile is created, `agent-closed` tombstone on delete - published by the profile's owner (the operator once profiles are CRs), not by workers. Chat sessions are not discoverable services and publish no card.                                 |
| `agents.hb.{agentType}.{owner}.{session}` | Core-NATS heartbeat every 15 s, Synadia-compatible shape, outside the stream. `owner` is the owning scope/account name - a single fixed value until the multi-scope split is exercised.                                                                              |

**The addressee token (added in 0.4) is the authorization seam.** `{addressee}` is the
executor's name - a profile, or a chat session. With it in the subject, connection-time
grants become exact: who may delegate to which profiles, who may emit events as which
executor, each a per-user subject-prefix grant. Without it (0.3 and earlier), every
grant collapsed to `a2a.tasks.>` and the deployment spec's connect-time property was
unimplementable on the task plane. The envelope's `to` MUST agree with the subject's
addressee token; a mismatch is a protocol error. `{addressee}` and `{taskId}` MUST be
dot-free tokens - lowercase alphanumerics and hyphens, DNS-1123-shaped - because dots
are NATS token separators, and a dotted value silently changes the subject's token
count out from under every wildcard filter. (Topic tokens already carry this rule; it
is the same rule.) Session names (`<profile>-<animal>`) and sanitized profile names
comply by construction; the library enforces it anyway. Per-task (rather than
per-executor) scoping stays the parked tightening with the authority work.

(0.1's `.request` becomes `.in` because it now carries follow-up input and cancel, not just
the one submission.)

### Mapping the A2A operations

A2A 1.0 defines its operations as JSON-RPC methods. On a bus, most of them stop being
calls and become properties of the stream:

| A2A operation                    | On the bus                                                                                                                                                                                                        |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `message/send` (new task)        | Publish `kind: message` to `a2a.tasks.{addressee}.{taskId}.in`. The publisher mints `taskId` - a deviation from HTTP A2A, where the server mints it, but the subject has to exist before anyone can answer on it. |
| `message/stream`                 | The same publish, plus subscribe to the `events` subject. Streaming is not an optional capability here; it is how the bus works.                                                                                  |
| `tasks/get`                      | Replay the `events` subject from sequence 1 and fold the events into a `Task`. No live executor required - this is the durability payoff.                                                                         |
| `tasks/cancel`                   | Publish `kind: cancel` to the `in` subject. The executor emits a terminal `canceled`. A task racing to completion may emit `completed` first; both orders are legal and the terminal event wins.                  |
| `tasks/resubscribe`              | JetStream consumer resume from the last delivered sequence. Comes with the transport.                                                                                                                             |
| push notification config methods | Not mapped. The bus is push; the library reports these as unsupported.                                                                                                                                            |

### Event ordering rules

- The first event on a task is a `status-update` with state `submitted`, published by the
  executor on accepting the message. (The Synadia `ack` chunk collapses into this.)
- Exactly one event carries `final: true`, and it is a terminal `status-update`.
- Nothing follows the final event. An event after `final` is a protocol error the
  library must surface, not ignore - and surface means a structured warning and a
  metric, with the late event dropped. It MUST NOT terminate the consumer: a zombie
  worker flushing its buffer after the supervisor's terminal event must not be able to
  crash a gateway or dispatcher.
- `input-required` flow: executor publishes `status-update` with state `input-required`
  carrying an A2A message that asks for the input. The requester publishes a follow-up
  `kind: message` with the same `taskId` to `…in`. Executor resumes and publishes
  `working`.
- Steering (added 8/24; refusal posture recorded 8/31): a follow-up `message` on `…in`
  while the task is `working` is legal. It is steering input - delivered to the
  executor, incorporated at its next turn boundary, no state transition implied. The
  hard interrupt is `cancel`, not a steer. An executor that cannot absorb input
  mid-turn (today's standing front door) refuses instead: a non-final `status-update`
  carrying the task's CURRENT state, visible on the stream - never a silent drop, and
  never a state change caused by the follow-up alone. Assertion 21's stdin delivery
  applies to absorbing executors; a refusal satisfies its never-silently-dropped half.
- Turn accounting is the steering contract (amended 8/31, from the worker adapter). A
  harness driven over stream-json emits one `result` per user turn, so once steers
  exist, "the harness produced a result" no longer means "the task is done." The
  executor's adapter counts turns - the opening prompt is one, each absorbed steer adds
  one, each harness `result` settles one - and the result that settles the count is the
  task's deliverable. Racing steers are drained before that decision. A steer that
  still arrives after the deliverable is chosen is answered with the refusal shape
  above - a non-final `status-update` carrying the task's current state, published
  before the terminal event - so the requester learns the correction missed on the
  stream, not from silence; nothing stream-visible marks this window otherwise, since
  the choice of deliverable is adapter-internal. The stage 1 adapter logs and counts
  the drop without publishing the refusal yet - a recorded deviation, closed with the
  rest of the adapter work. Without this rule, the first result after a steer would
  terminate the task with the pre-steer answer and the correction would be silently
  lost.
- There is deliberately no protocol-level inactivity timeout. Liveness is judged from
  heartbeats and consumer health, which the deployment spec owns. A task whose executor
  died without a terminal event gets terminal `failed` written by its supervisor - the
  gateway for chat sessions it spawned, the dispatcher's janitor for profile-addressed
  tasks. Ratified 8/24. One refinement (8/31): `failed` is the state for an executor
  that died mid-work, but where the supervisor is finishing a cancel the requester
  already published - the executor is being torn down deliberately, on that cancel -
  the terminal is `canceled`. The supervisor writes what happened, and assertion 13's
  enumeration holds for every path a cancel can take.

### Reserved artifact names

Added 8/24, ratified with the subagent framework. `artifact-update` payloads name their
artifact, and four names are reserved so renderers and audit tooling can rely on them:

| Name       | Content                                                                                                                                                         |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `result`   | The deliverable, chunked per A2A chunking rules                                                                                                                 |
| `thinking` | Reasoning deltas. Debug views only                                                                                                                              |
| `activity` | Tool-call trace, one entry per invocation. Always in the audit replay                                                                                           |
| `progress` | Agent-authored milestones, renderable to chat at zero model cost. Stage 1 derives these from model narration; the subagent framework spec records the deviation |

Artifact names are data, so the set can grow without touching the envelope; only these
four carry reserved semantics.

## Topics

Tasks are conversations. Topics are the blackboard: durable, named subjects where agents
publish what they currently know, so the next question starts from standing state instead
of a cold diagnosis. (The file-based blackboard this replaces at stage 3 is
`docs/designs/agent-communication.md`, today's design of record for platform-to-cluster
exchange.)

### Namespace

```
a2a.topics.agent.{agent}.{topic}     one owning agent writes, everyone may read
a2a.topics.shared.{topic}            shared state with a designated writer set
```

Topic tokens are kebab-case, no dots (dots are NATS separators). Write access is enforced
at connection time by the account design in the deployment spec, not by consumers checking
`from`. The set of topics is provisioned configuration, not something publishers invent
at runtime (decided 8/24: provisioned-only). The designed future shape for self-serve,
if emergent-coordination experiments want it, is a per-agent scratch prefix -
`a2a.topics.scratch.{agent}.>` - where an agent invents names inside its own namespace,
granted as one wildcard at connection time. Not built; recorded so the door has a shape.

Worked examples:

| Subject                                       | Class   | Writer                 | Content                                          |
| --------------------------------------------- | ------- | ---------------------- | ------------------------------------------------ |
| `a2a.topics.agent.platform.upgrade-readiness` | state   | platform agent         | DataPart: current per-cluster readiness verdicts |
| `a2a.topics.agent.platform.version-skew`      | state   | platform agent         | DataPart: skew summary across the fleet          |
| `a2a.topics.shared.blueprint`                 | state   | designated writer, TBD | DataPart: the shared environment model           |
| `a2a.topics.shared.annotations`               | journal | any agent              | TextPart/DataPart: dated observations            |

### Payload

A `topic-update` envelope carries an A2A `Artifact`: `name` is the topic, parts are
typically one DataPart (the structured state) with an optional TextPart summary. When the
update happened in the course of a task, the envelope carries that task's `taskId` and
`correlationId` - which is the audit thread from a user's question to the standing state it
changed. Updates published on an agent's own schedule carry a `correlationId` minted for
that run.

### Retention classes

Every topic is provisioned into one of two classes. Publishers do not choose retention;
the class does.

| Class       | Semantics                                                          | JetStream shape                         |
| ----------- | ------------------------------------------------------------------ | --------------------------------------- |
| **state**   | Current answer plus short history. Survives restarts indefinitely. | `max_msgs_per_subject: 8`, no age limit |
| **journal** | Append-only record, ages out.                                      | limits retention, `max_age: 30d`        |

Task subjects (`a2a.tasks.>`) get their own limits-retention stream with `max_age: 72h`,
and the directory (`a2a.agents.>`) is last-value (`max_msgs_per_subject: 1`, so the
tombstone replaces the card). The numbers in this section were ratified 8/24 as dev
defaults; the GA window is a product and tenancy decision, escalated to product.
Long-term audit archival is the deployment spec's exporter.

## Conformance assertions

The stage 1 client library ships with a suite that asserts all of the following.
Resilience assertions (server restart, reconnect behavior) live in the NATS deployment
spec's requirements; 19 and 20 are repeated here because the suite is one suite. Where
an assertion needs more than the library to prove (21), it names its home.

Envelope:

1. An envelope with an unknown protocol major is rejected. Same-major envelopes with
   unknown fields are accepted and the unknown fields ignored.
2. The library never emits an envelope missing `protocol`, `envelopeId`, `correlationId`,
   `ts`, `from`, or `kind`, nor one missing `taskId`/`contextId` for the kinds that
   require them, nor one whose `taskId` or addressee fails the dot-free token rule.
3. The library never populates `identity`. It populates `authority` only on the gateway's
   ingress path; every other producer emits it null. Inbound values are passed through
   byte-identical and are not consulted for any decision.
4. A consumer on a wildcard ignores envelopes whose `to` names another session, and an
   envelope whose `to` disagrees with its subject's addressee token is surfaced as a
   protocol error.
5. A redelivered envelope (same `envelopeId`) reaches the application at most once.

Payloads:

6. Every payload survives a parse and re-serialize with semantics preserved, including
   unknown A2A object fields.
7. A kind/payload type mismatch is surfaced as a protocol error, never passed through.
8. An envelope over the max message size fails client-side with an A2A error before
   publish. A FilePart with inline `bytes` over the threshold is refused with the same
   error.

Lifecycle:

9. The first event on every task is a `status-update` with state `submitted`.
10. Exactly one event has `final: true`, its state is terminal, and any event after it
    is surfaced as a protocol error - warn-and-drop, with the consumer loop surviving.
11. A `tasks/get` materialized by replay yields the same terminal state and artifact set a
    live subscriber saw.
12. A follow-up message with the same `taskId` resumes an `input-required` task, and the
    next status event is `working`. A follow-up during `working` is delivered to the
    executor and does not by itself change task state.
13. A cancel always results in a terminal event - `canceled`, or `completed` if the race
    was lost - never a silent stop.

Correlation:

14. `correlationId` is preserved verbatim across every hop the library mediates, and a
    child task created through the library inherits its parent's value.
15. Every event a task emits carries the `taskId` and `correlationId` of its originating
    message.

Topics:

16. `topic-update` payloads are valid Artifacts and topic tokens contain no dots.
17. Reading a state-class topic returns the latest entry per subject without replaying
    history.

Artifacts (added 8/24, with the subagent framework):

18. Every completed task carries at least one `result` artifact, and the reserved
    artifact names are used only for their defined content.

Resilience (shared with the deployment spec's requirements):

19. The client survives a NATS server restart and resumes delivery without a process
    restart.
20. After a reconnect, the consumer resumes with no gap, and assertion 5 still holds.

Steering delivery (added 8/25, with the dual-reader rule):

21. A steering message published to a running task reaches the executor's harness stdin
    exactly once, including under JetStream redelivery. No dispatcher path consumes a
    steer without delivering it - a steer to a live task is delivered to the executor,
    never dropped. The executable test lands with the worker adapter, asserting against
    a stub harness that echoes its stdin - proving "reached the harness stdin" needs the
    adapter, not the library alone.

## Open Questions

Calls for @bnaylor, not silently resolved here:

- **Where does verified identity live?** If the NATS server can stamp the authenticated
  connection identity into a message header the publisher cannot set, the reserved
  `identity` field becomes a header and no per-message signing is needed. If it cannot,
  the payload gets signed. Needs verification against the NATS docs before the security
  rev - the answer changes the shape of the reserved field.
  **Update 8/24:** verified, it cannot - tested empirically on v2.10.29, docs and source
  swept through v2.14.5. `identity` is not a header. The
  one exception (`Nats-Request-Info`, unforgeable but only on cross-account service
  imports, and stripped on JetStream ingest) doesn't apply to our single-account stage 1.
  Remaining choice: signed claim in the envelope vs subject-derived identity where
  publish permissions are exclusive - and the 0.4 addressee-scoped subjects widen
  exactly that exclusive surface, which strengthens the subject-derived option.
- ~~**Large artifact storage.**~~ Decided 8/24: JetStream Object Store, object TTL
  tracking W for task artifacts, 128KiB inline threshold as the dev default. An external
  bucket is a stage 2 pluggable alongside the audit exporter - an option, never a
  requirement, same pattern as Cloud Logging.
- ~~**Topic provisioning.**~~ Decided 8/24: provisioned-only, with the scratch-prefix
  shape recorded in the namespace section for when emergent coordination gets its
  experiment.
- ~~**Orphaned tasks.**~~ Settled 8/24, in two ratified halves: every task has a
  supervisor, and the supervisor is the janitor - the gateway for chat sessions it
  spawned, the dispatcher for profile-addressed tasks. A supervisor's grant is publish
  on its own addressees' `…events` subjects - subject-level, since NATS permissions
  cannot see the envelope `kind`; that a supervisor emits only terminal `status-update`
  is a conformance assertion, not a connect-time control. Its synthesized events carry
  its own identity in `from`, so replay always distinguishes "the worker said failed"
  from "the supervisor declared it dead."
- ~~**`contextId` scope.**~~ Settled by the gateway design, 8/24: one per backend
  conversation (thread or DM), minted at first contact, persistent across pod
  incarnations.
- ~~**Retention numbers.**~~ Ratified 8/24 as dev defaults. The GA window is a
  product/tenancy decision, escalated on that list.
