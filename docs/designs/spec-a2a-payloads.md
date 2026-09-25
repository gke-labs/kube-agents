# A2A payload spec (a2a-jetstream/0.4)

- **Author:** @bnaylor
- **Date:** 2026-08-24
- **Status:** draft for review
- **Supersedes:** the demo protocol (`a2a-jetstream/0.1`). 0.2 was this doc's
  pre-amendment draft, never implemented; 0.3 added the ratified `authority` rules; 0.4
  moves the addressee into the task subjects, which is what makes connection-time
  authorization expressible on the task plane. Amended 9/9 without a version bump: the
  supervisor gets its own task subject, and the identity half of the consumer rule flips
  to subject-derived identity (the Verified identity section).

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

| Field                  | Rules                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `protocol`             | Required. Major.minor; bump major on breaking change. Consumers MUST reject unknown majors and MUST ignore unknown envelope fields within a major.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `envelopeId`           | Required, unique per envelope. The dedup key: JetStream redelivery means consumers will see repeats, and this is how the library delivers each envelope to the application at most once. The dedup window is bounded - an LRU or time window sized to the redelivery horizon (`MaxAckPending` × ack wait, plus margin), never an unbounded set that grows for the life of the process.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `correlationId`        | Required. Minted once by the gateway at the user interaction that starts a task. Copied verbatim on every hop; never re-minted by an intermediary. A task spawned in service of another task inherits its parent's value, and a follow-up or steer to a running task carries the task's original value - the steer is attributed by its own envelope and `authority` block, not by a new correlation. This is the identifier that spans question, hops, and resulting change.                                                                                                                                                                                                                                                                                                                                                                                           |
| `traceparent`          | Optional. W3C trace context, for OTel tooling. `correlationId` is authoritative; `traceparent` is a convenience and may be re-parented per span.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `taskId` / `contextId` | Required for kinds `message`, `status-update`, `artifact-update`, `cancel`. Optional for `topic-update` (present when a topic write happened in the course of a task - see Topics). Absent for `agent-card`, `agent-closed`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `ts`                   | Required. ISO-8601 UTC.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `from`                 | Required; only `from.session` is a presence rule. Never the source of identity or authority - both come from the subject an envelope was delivered on (Verified identity, below). On an identity-bearing subject `from` MUST agree with the writer the subject implies, and a disagreement is a protocol error, never a re-attribution. Refused on `…supervisor`, `…in` and the directory; on `…events` advisory as shipped - counted, and the envelope still published, delivered and folded - and hard only once an operator sets `A2A_STRICT_EVENTS_WRITER=true`. `from.profile` names the AgentProfile a worker runs as; mandatory (9/9) on the directory, where it is the profile binding. On a profile-addressed executor's events either it or `from.session` may carry the addressee token; neither alone is required. Display reads it; nothing decides on it. |
| `to`                   | Optional, on every class - nothing requires it to be present. Addresses an envelope to a named session, and consumers on a wildcard MUST ignore envelopes addressed elsewhere. Where it IS present on any task subject it MUST agree with that subject's addressee token. Event envelopes carry no `to`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `identity`             | **Reserved and permanently null** (decided 9/9). Verified identity is a property of the delivery, not a field. See below.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `authority`            | **Reserved**, advisory. Populated by the chatops gateway only. See below.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `kind`                 | Required. Enum below; selects the payload type.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `payload`              | The A2A object, per kind.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |

One rule spanning three fields: `correlationId`, `taskId`, and `contextId` are minted as
opaque random tokens and MUST NOT embed backend identifiers, thread titles, emails, or
any user content. They are the identifier class that escapes the pseudonymization rule
(they ride every subject and every envelope in the clear), and they stay clean by
construction, not by redaction - a `corr-{threadTitle}` would quietly put labelled
content on the bus.

### Reserved fields: `identity` and `authority`

Reserved in 0.2 as two names, so that filling them later would not be a protocol rev.
One of them is now decided the other way.

- `identity` stays **null, permanently** (decided 9/9). The
  verified identity of a publisher is the principal the subject implies - NATS enforces
  publish permissions at the connection, the server cannot stamp an identity into a
  message (measured, 8/24), and any identity bytes a publisher can set are advisory by
  definition, which is the property being refused. So the library exposes verified
  identity as a property of the delivery, never as a field it parses. The rules are in
  the Verified identity section below.
- `authority` will carry a _reference_ to an attenuating capability held in KV - who
  originally asked, what scope they hold, what this hop is permitted to do, each further
  hop a strict subset - per the capability envelope design
  (`docs/architecture/09-capability-envelope.md`): no token format, nothing signed in the
  envelope, the message carries a lookup id. The A2A `auth-required` task state is
  reserved alongside it.

Rules (**amended 8/24**, ratified from the gateway design; **the identity half flipped
9/9**): `identity` MUST NOT be populated by anyone, and an emitter that populates it is
non-conforming. `authority` is populated by the chatops gateway at ingress and by nothing
else - the verified requester and the audience snapshot, carried for audit and parity
testing. It is advisory: nothing yet stops a bus client from inventing an `authority`
block, so consumers MUST NOT make any authorization decision on it, and libraries MUST
pass it through untouched. Consumers MAY decide on the **subject-derived identity** of an
envelope on the task plane, under exactly the conditions the Verified identity section
states; that is the identity half of the old "neither field" rule, and it is the only
half that has flipped. The authority half flips when the capability envelope arms
(`docs/architecture/09-capability-envelope.md`) and not before: a flip that read as
covering both would license consumers to trust `authority.grants` while it is still
null.

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

| Subject                                     | Carries                                                                                                                                                                                                                                                                                                                                                                                                                |
| ------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `a2a.tasks.{addressee}.{taskId}.in`         | `message` (submission and follow-up input) and `cancel`, requester to executor. Two reader roles by design: the dispatcher consumes new-task submissions; the executor's own ephemeral consumer takes everything after the submission (follow-ups, steers, cancels).                                                                                                                                                   |
| `a2a.tasks.{addressee}.{taskId}.events`     | `status-update` and `artifact-update`, executor to anyone. **The executor and only the executor writes here** (9/9): the addressee's own principal is the subject's writer set, which is what makes its identity subject-derived.                                                                                                                                                                                      |
| `a2a.tasks.{addressee}.{taskId}.supervisor` | Added 9/9. The one terminal `status-update` a task's supervisor synthesizes for an executor that died or that it is tearing down on the requester's cancel - the gateway for chat sessions it spawned, the dispatcher's janitor for profile-addressed tasks. Supervisor to anyone; the executor's grant never reaches it. Same token count as `events`, so it shares the `TASKS` stream, its filters and its sequence. |
| `a2a.agents.{profile}`                      | `agent-card` when a profile is created, `agent-closed` tombstone on delete - published by the profile's owner (the operator once profiles are CRs), not by workers. Chat sessions are not discoverable services and publish no card.                                                                                                                                                                                   |
| `agents.hb.{agentType}.{owner}.{session}`   | Core-NATS heartbeat every 15 s, Synadia-compatible shape, outside the stream. `owner` is the owning scope/account name - a single fixed value until the multi-scope split is exercised.                                                                                                                                                                                                                                |

**The addressee token (added in 0.4) is the authorization seam.** `{addressee}` is the
executor's name - a profile, or a chat session. With it in the subject, connection-time
grants become exact: who may delegate to which profiles, who may emit events as which
executor, each a per-user subject-prefix grant. Without it (0.3 and earlier), every
grant collapsed to `a2a.tasks.>` and the deployment spec's connect-time property was
unimplementable on the task plane. An envelope's `to`, where it has one, MUST agree with
the subject's addressee token; a mismatch is a protocol error on every task
subject, not only on `…in`. `{addressee}` and `{taskId}` MUST be
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

| A2A operation                    | On the bus                                                                                                                                                                                                                                                                                                                                                                                                                      |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `message/send` (new task)        | Publish `kind: message` to `a2a.tasks.{addressee}.{taskId}.in`. The publisher mints `taskId` - a deviation from HTTP A2A, where the server mints it, but the subject has to exist before anyone can answer on it.                                                                                                                                                                                                               |
| `message/stream`                 | The same publish, plus subscribe to BOTH the `events` and `supervisor` subjects - which a session pod cannot do today under its derived grants; see spec-chatops-gateway.md. Streaming is not an optional capability here; it is how the bus works. A subscriber to `events` alone is not wrong about anything it sees, but it never sees the terminal a supervisor declares, so it waits out a task the bus has already ended. |
| `tasks/get`                      | Replay the `events` and `supervisor` subjects from sequence 1, in stream order, and fold them into a `Task`. No live executor required - this is the durability payoff. The two subjects share the stream sequence, so the fold needs no merge; whichever terminal the stream holds first is the task's, and the other is a post-final drop.                                                                                    |
| `tasks/cancel`                   | Publish `kind: cancel` to the `in` subject. The executor emits a terminal `canceled`. A task racing to completion may emit `completed` first; both orders are legal and the terminal event wins.                                                                                                                                                                                                                                |
| `tasks/resubscribe`              | JetStream consumer resume from the last delivered sequence. Comes with the transport.                                                                                                                                                                                                                                                                                                                                           |
| push notification config methods | Not mapped. The bus is push; the library reports these as unsupported.                                                                                                                                                                                                                                                                                                                                                          |

### Event ordering rules

- The first event on a task is a `status-update` with state `submitted`, published by the
  executor on accepting the message. (The Synadia `ack` chunk collapses into this.)
- Exactly one event carries `final: true`, and it is a terminal `status-update` - counted
  across the task's `…events` and `…supervisor` subjects together (9/9), in stream order.
- Nothing follows the final event, on either subject. An event after `final` is a
  protocol error the library must surface, not ignore - and surface means a structured
  warning and a metric, with the late event dropped. It MUST NOT terminate the consumer:
  a zombie worker flushing its buffer onto `…events` after the supervisor's terminal
  landed on `…supervisor` must not be able to crash a gateway or dispatcher.
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
  enumeration holds for every path a cancel can take. **The supervisor writes it on
  `…supervisor`, never on `…events`** (ratified 9/9).
  Until 9/9 the two shared `…events` and this doc said `from` was how replay told them
  apart; `from` is publisher-asserted, so a hostile executor could end its own task
  wearing the supervisor's identity and the record read as infrastructure. The split
  buys attribution, not prevention: an executor can still terminate its own task on its
  own `…events`, and what changes is that it now reads as the executor doing so, which
  is detectable. Where no supervisor principal exists yet - profile-addressed tasks
  until the dispatcher lands - an executor finalising its predecessor incarnation's
  orphan (the Hermes bridge's startup sweep) is the executor finishing its own task,
  writes on `…events` as itself, and is not a supervisor write. **Migration:** `TASKS`
  keeps 72h, so for one retention window after an install takes the split its stream
  still holds legitimate supervisor terminals on `…events`; the writer-class check on
  `…events` (Verified identity, below) ships advisory. It does not harden when the window
  passes. Hardening is one deliberate operator flip - `A2A_STRICT_EVENTS_WRITER=true` in
  the gateway's environment, default false - and nothing arms it on a schedule or reverts
  it. An install that never sets it counts advisories for the life of the install, which
  is the part worth naming: a retention window is the earliest the flip is SAFE, not a
  date on which it happens.

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
four carry reserved semantics. An `activity` entry is one `data` part whose object carries
`tool`, `input` when the call had one, and may carry `callId`, `status` (`completed`, `error`, or
`interrupted` for a call still open at the terminal), `errorType` (the executor's own word for
an error), `durationMs` and `at`; the library
validates the part kind (assertion 18), and a reader tolerates the keys it does not know.

## Verified identity (added 9/9)

**An envelope's publisher is the principal implied by the subject it arrived on** - the
stored subject, on replay - and that implication is decision-grade exactly where two
conditions hold. No signed claim, no signing key on the hot path, no key registry at
replay: every check is a field-to-token comparison, plus the one supervisor name a
consumer is configured with. The alternatives this beat - a signed claim per envelope,
a server-stamped header, a key registry consulted at replay - were taken through
successive hostile review rounds before it was ratified; this section is the contract, and the
reasoning that is load-bearing for reading it is restated here rather than cited.

**Condition 1 - the writer set.** The subject's writer set equals the principals its
tokens name, counted over the full write surface: publish grants, and every JetStream
route by which stored bytes can be made to land on a subject (deliver-subject
redirection, `RePublish`, transforms, sources, `STREAM.RESTORE`, message delete and
purge). Writer sets are permissions invariants and belong in `tests/conformance/`.

**Condition 2 - the envelope agrees with the subject**, checked per subject class,
closed-world on kind: on an identity-bearing subject only the kinds enumerated for its
class are admissible, and any other kind - `topic-update` included - is a disagreement.
Disagreement on any applicable check is a protocol error and the message carries no
identity. A consumer checks it at delivery, a publisher's library refuses it at the
source, and replay skips and counts it.

| Subject class                        | Admissible kinds                   | `taskId` | Writer the subject implies, and the check                                                                                                                                                                                                           |
| ------------------------------------ | ---------------------------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `…events`                            | `status-update`, `artifact-update` | = token  | The addressee: `from.session` for a chat session, `from.profile` for a profile-addressed executor; at least one MUST equal the addressee token. Advisory as shipped; hard only once an operator sets `A2A_STRICT_EVENTS_WRITER=true`.               |
| `…supervisor`                        | `status-update`, `final: true`     | = token  | The supervisor the render assigns that addressee - a lookup against the render, not a token. A consumer that knows its supervisor's name checks `from.session` against it; one that does not checks the negative form, `from` is not the addressee. |
| `…in`                                | `message`, `cancel`                | = token  | A requester. Not computable from the tokens, so the check is the negative form - `from` is not the addressee. `to` is conventionally present here and checked when it is, but is not required; see the note below.                                  |
| `a2a.agents.{profile}`               | `agent-card`, `agent-closed`       | absent   | The profile's owner; `from.profile` MUST equal the subject token (the profile binding, mandated 9/9 before any card publisher exists). A card MUST NOT be refused for lacking a `taskId`.                                                           |
| `agents.hb.>`, `$KV.session-state.>` | no envelope                        | -        | Condition 2 is inapplicable; condition 1 carries them alone. Heartbeat identity is live-only - no stream, no replay, no audit record.                                                                                                               |

**Where `to` is checked, since the table splits it.** There is no presence rule, on any
class. An earlier draft of this section said `to` was REQUIRED on `…in`; no assertion
states that and `a2a/lib` implements no such check, so the claim is withdrawn rather than
promoted - event envelopes carry no `to`, and requiring one would make every legitimate
event a protocol error, which is why the field stayed optional in the first place. What IS
a rule, on every task subject, is CHECKING a `to` that is present: it must equal the
subject's addressee token, `…events` and `…supervisor` included, and a disagreement is a
protocol error like any other. That is assertion 4's second clause, and the library applies
it class-independently, at publish and at delivery both, because an event carrying another
session's `to` has no legitimate producer. A consumer that reads the `…in` row alone and
skips the check on the other two classes has implemented the narrower of the two and is the
reason this is spelled out here.

Topics are deliberately outside this enumeration. Agent-scoped topics already have
exclusive writers and could join by the same rule; shared topics are multi-writer by
design and their attribution stays `from`-advisory.

**What is decision-grade today, and what is not.** `…supervisor`, for every addressee:
the gateway is the only principal in the render holding publish on it and every other
principal is refused at the server - decision-grade on the publish half of Condition 1.
The redirection half rests on a measured property, not on an absence of subscribers: a
consumer's deliver subject can aim replay of stored messages at another stream's subject,
but the bytes arrive under their ORIGINAL subject, so a consumer that reads the subject it
was delivered on is not fooled (`spec-nats-deployment.md` measures this; assertion 23
pins it). Do not read that as "nothing can reach the subject." Core subscriptions on task
subjects do exist and are the exposed party - `web` subscribes `a2a.>` and `worker`
`a2a.tasks.>`, both covering `a2a.tasks.*.*.supervisor` - and both hold `CONSUMER.CREATE`
on `TASKS` unscoped. No conformance assertion pins the redirection route for
`a2a.tasks.*.*.supervisor` the way `spec-nats-deployment.md` pins it for `a2a.agents.>`,
and the credential published to a browser is inside that gap. Treat that as the open edge,
and scope its closure to `web` and `worker` - closing it on the gateway's relay alone
would leave it open. `…events`, for every addressee
_including a session pod_: not yet. The session's own grant is derived per incarnation
and reaches only its own pod's subjects, but the static `worker` credential the Hermes
bridge still holds publishes `a2a.tasks.*.*.events` - a wildcard over the addressee
token, so it reaches a session's `…events` exactly as it reaches a profile's. Until the shared
`worker` credential is retired - the Hermes bridge is the last holder - that subject has
two writers on both classes. The suite records that rather than asserting it
away: `test_A3_the_events_subject_has_no_rendered_writer` in
`tests/conformance/test_A_authority.py`, decorated `@known_violation("A3", ...)` against
gke-labs/kube-agents#1316. Read the file, not this sentence - the record is the suite's,
never this document's, and which assertions exist changes faster than this paragraph.
What the derived grant buys in the meantime is a bound on what a _session_ can forge - it cannot write
another session's subject - which is not the same property as a consumer being able to
read the writer off the subject. The directory: the profile binding closes the
direct forge, and what remains is relocation of stored bytes through a browser
credential's consumer-create grant, which only the account split closes; directory
identity is not decision-grade until then. `$KV.session-state.>` likewise waits on the
account split. **A consumer MUST NOT treat identity as decision-grade on a class this
paragraph does not name as such.**

**The residues, named so they are not rediscovered.** A stored non-final event
re-injected onto its own subject after the dedup window, on a still-running task, folds
as a genuine-looking transition with valid derived identity; the candidate bound is a
monotonicity check in the fold (a relocated copy arrives at a later stream sequence with
an older `ts`), not built. And identity ends at a bridge: the bridge is the publisher,
and a chat user's identity is the gateway's `authority` block, which is the capability
envelope's to make decision-grade.

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

Task subjects (`a2a.tasks.>`) get their own limits-retention stream with `max_age: 72h`
and `max_msgs_per_subject: 4096`, and the directory (`a2a.agents.>`) is last-value
(`max_msgs_per_subject: 1`, so the tombstone replaces the card). Stream layout is owned
by [the NATS deployment spec](spec-nats-deployment.md), including what the task stream's
per-subject cap costs a replay that reaches it - see assertion 9 below. The numbers in this section were ratified 8/24 as dev
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
   protocol error. (Refined 9/9: checking a `to` that is present is every task subject's
   rule, `…events` and `…supervisor` included, which is where it already was. Nothing
   requires `to` to be present on any class, and event envelopes carry none.)
5. A redelivered envelope (same `envelopeId`) reaches the application at most once.

Payloads:

6. Every payload survives a parse and re-serialize with semantics preserved, including
   unknown A2A object fields.
7. A kind/payload type mismatch is surfaced as a protocol error, never passed through.
8. An envelope over the max message size fails client-side with an A2A error before
   publish. A FilePart with inline `bytes` over the threshold is refused with the same
   error.

Lifecycle:

9. The first event on every task is a `status-update` with state `submitted`, on
   `…events`. A task whose only event is its supervisor's terminal (the executor never
   ran) is the one exception, and it is terminal at its first event. A replay can also
   open past the `submitted` without any publisher breaking the rule: `TASKS` carries a
   per-subject message limit with `discard: old`, so a task that outruns the limit loses
   its oldest events first and its oldest event is exactly this one. That is a retention
   consequence, and the fold reports it (`lib.Task.SubmittedMissing`, logged by the
   replay path) so the two are distinguishable - a truncated history must not read like
   a complete one. Assertion 11 is bounded by the same thing: replay matches a live
   subscriber only for the events retention still holds.
10. Exactly one event has `final: true` across the task's `…events` and `…supervisor`
    subjects together, its state is terminal, and any event after it on either subject
    is surfaced as a protocol error - warn-and-drop, with the consumer loop surviving.
    One case of this is expected rather than hostile, and an operator alerting on the
    violation counter has to know it: per-subject CAS cannot span two subjects, so the
    janitor's terminal on `…supervisor` is no longer serialized against the executor's own
    terminal on `…events`. A genuine race produces exactly one counted post-final drop, and
    the counter does not distinguish it from a forged one.
11. A `tasks/get` materialized by replay of both subjects yields the same terminal state
    and artifact set a live subscriber of both saw, and a task whose supervisor terminal
    predates the split (stored on `…events`) still replays to that terminal.
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

Verified identity (added 9/9):

22. A supervisor emits only terminal `status-update`, and only on `…supervisor`. A
    non-final or non-status envelope on a supervisor subject is a protocol error; a
    supervisor terminal on an executor's `…events` is a writer-class disagreement. That
    last clause is about the writer set, not about every envelope an install holds: on an
    install that has not taken the split, and for one retention window after one that has,
    the supervisor's own terminals are legitimately on `…events` and a consumer that
    refuses them refuses every reap, Sweep and Delegate the gateway performed. That is the
    Migration note above, and it is why the `…events` writer-class check ships advisory
    behind `A2A_STRICT_EVENTS_WRITER` rather than refusing - the disagreement is counted,
    and hardening is the operator's flip.
23. Envelope-subject agreement, per class and closed-world on kind, as the Verified
    identity table states: a relocated envelope whose kind, `taskId`, `to` or writer
    class disagrees with its subject is a protocol error at delivery and at
    publish, and is skipped and counted on replay. A legitimate card is never refused
    for lacking a `taskId`.
24. The writer sets themselves - `…events` the executor only, `…supervisor` the
    supervisor only, `…in` requesters only with the executor's grant never reaching it,
    and no principal outside the trust root holding a server-originated write route onto
    an identity-bearing subject - are permissions invariants, and per `AGENTS.md` they
    belong in `tests/conformance/` rather than in the library suite. That is a placement
    rule and not a pointer to existing coverage: read `tests/conformance/` itself before
    relying on any of them being asserted. One of the three cannot pass as stated in any
    case - the static `worker` user holds publish on every addressee's `…events`, so
    that writer set is not yet single-writer, and it closes when `worker` is retired.

## Open Questions

Calls for @bnaylor, not silently resolved here:

- ~~**Where does verified identity live?**~~ Decided 9/9: in the subject, not in a field
  (the Verified identity section). The server cannot stamp an identity into a message
  (verified 8/24 on v2.10.29, docs and source swept through v2.14.5; `Nats-Request-Info`
  is unforgeable only on cross-account service imports and is stripped on JetStream
  ingest), so the signer would have been the client - a key resident in a session pod
  running model output against untrusted input, exfiltratable and usable offline. The
  subject-derived option keeps enforcement at the server and has no canonicalization
  surface, no second key directory, and no cryptography at replay. `identity` stays
  null forever; the supervisor subject is the one structural change the decision
  needed.
- ~~**Large artifact storage.**~~ Decided 8/24: JetStream Object Store, object TTL
  tracking W for task artifacts, 128KiB inline threshold as the dev default. An external
  bucket is a stage 2 pluggable alongside the audit exporter - an option, never a
  requirement, same pattern as Cloud Logging.
- ~~**Topic provisioning.**~~ Decided 8/24: provisioned-only, with the scratch-prefix
  shape recorded in the namespace section for when emergent coordination gets its
  experiment.
- ~~**Orphaned tasks.**~~ Settled 8/24, in two ratified halves: every task has a
  supervisor, and the supervisor is the janitor - the gateway for chat sessions it
  spawned, the dispatcher for profile-addressed tasks. ~~A supervisor's grant is publish
  on its own addressees' `…events` subjects - subject-level, since NATS permissions
  cannot see the envelope `kind`; that a supervisor emits only terminal
  `status-update` is a conformance assertion, not a connect-time control. Its
  synthesized events carry its own identity in `from`, so replay always distinguishes
  "the worker said failed" from "the supervisor declared it dead."~~ **Amended 9/9:**
  the last clause was the part that did not hold. `from` is written by the publisher,
  so an executor could put the supervisor's identity on its own terminal and replay
  could not tell. A supervisor now publishes on `…supervisor`, which its grant reaches
  and no executor's does, so the distinction is the subject rather than the field, and
  the writer set enforces it at connect time. That a supervisor emits only terminal
  `status-update` is still not a connect-time control - NATS permissions cannot see
  `kind` - but it is now a delivery-time refusal (assertion 22) rather than only a
  conformance assertion.
- ~~**`contextId` scope.**~~ Settled by the gateway design, 8/24: one per backend
  conversation (thread or DM), minted at first contact, persistent across pod
  incarnations.
- ~~**Retention numbers.**~~ Ratified 8/24 as dev defaults. The GA window is a
  product/tenancy decision, escalated on that list.
