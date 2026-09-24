# Chatops gateway design

- **Author:** [@bnaylor]
- **Date:** 2026-08-24
- **Status:** merged design of record; the gateway program is implemented (`a2a/gateway`: session registry, authority block, interceptors, supervisor duties, Discord and Google Chat adapters, and the console adapter); the operator renders the gateway Deployment, its env and the `A2A_SPAWN_SESSIONS` arming under `mode: next` (`platformagent_a2a_manifests.go`) plus, under its own eval flag, the inject backend below and its Service, principal map, token Secret and gateway fence, but not yet the Google Chat adapter's env, its projected relay token, the broker's side of it (`CREDENTIAL_PROXY_A2A_CHAT_AUDIENCE`, the gateway's ServiceAccount on `CREDENTIAL_PROXY_ALLOWED_CALLERS`, and the broker NetworkPolicy admitting the A2A gateway pod), or the A2A subscription and its IAM (the composition still provisions one Chat subscription)

## Purpose

This document defines the chatops gateway: the component that connects chat backends to
the A2A bus. It covers what a user session is, how sessions are spawned and reaped, how
requester identity gets onto the bus, what we do about group chats, and which chat backend
we stand up first for testing.

The demo gateway was single-user and trusting - one hardcoded "chatops" session, identity
asserted in an envelope field nothing verifies, no concept of a room. This doc is the
design for the real one.

Companion docs: the payload spec owns the envelope and the task lifecycle, and reserves
the `identity` and `authority` fields this doc names. The execution shape is settled -
pod per session, gateway coded to the headless CLI contract - and this design assumes
it. The NATS deployment spec owns accounts and connection-time authz.
The declarative subagent framework (its own doc) owns profile-addressed delegation -
the dispatcher, Jobs, `AgentProfile`s. This doc stops at the session boundary, with one
amendment (8/31): the Delegate flow below hands a single task to a fresh
gateway-spawned session worker, which stays inside the session model - the worker is an
incarnation of the conversation's own session, not a profile executor.

## The gateway holds no model

The demo gateway ran a Claude session of its own and used it to decide when to delegate.
That was fine for a demo and is wrong for the product. The gateway sees every human
message in the system, which makes it the component where a context window does the most
damage - anything in its context is influenceable by anyone who can type at it.

So the gateway is deterministic code: adapters, a session manager, a bus client. No
prompt, no tools, nothing to inject into. The judgment the demo gateway exercised moves
into the session pods, which is where the model already lives. The safety classifier
discussed for group chats slots in beside the gateway later as a veto - it can block or
reroute a message, and it never widens anything.

## What a session is

**A session is one backend conversation bound to one `contextId`, executed by at most one
pod at a time.** Concretely:

- The session key is the backend-qualified conversation id - a DM, or a thread in a
  group space (eg `discord:1234/5678`, `gchat:spaces/AAA/threads/BBB`). A channel or
  space is not a session; a conversation in it is.
- `contextId` is minted at first contact with a conversation and never changes. It is
  the durable name of the conversation on the bus. Minting MUST be create-only (a KV
  `Create`, compare-and-swap semantics), so that two replicas or a rehydrate racing
  first contact cannot fork a conversation - the loser reads and adopts the winner's
  value. The stage 1 gateway runs a single replica and serializes per conversation
  in-process, and minting is the KV `Create` this rule requires - the loser of a mint
  race re-reads and adopts the winner's record before the contextId reaches any
  envelope.
- The pod is an incarnation, not the identity. Reaping and respawning changes the pod
  and the bus session name; `contextId` persists across every incarnation.
- In a group thread, everyone in the room shares the one session. Attribution is per
  turn, in the envelope, not per pod.

This settles the payload spec's open `contextId` scope question: **per conversation
(thread or DM), not per pod and not per space.** Per-pod would break correlation across
a reap/resume cycle, which is the normal lifecycle, not an edge case. Per-space would
mix unrelated conversations into one context and make the room the unit of history,
which nobody wants from a busy channel.

Session state lives in a NATS KV bucket, keyed by session key: `contextId`, current pod
name, bus session name, last-activity timestamp, roster, the task history with each
task's own addressee (session addressees rotate per incarnation, so a straggler's
replay must use the addressee its subjects carried, not the record's current one), and
the active task's serialization record - `taskId`, `correlationId`, the echoed `ask`
(truncated) and its `submittedAt`, which feed the status card below; the `ask` copy is
user content, governed by the content rule in the identity section. Runtime state is
not git and not pod annotations; KV is the house answer. A gateway restart rediscovers its sessions
from KV plus pod labels, so a gateway crash strands nothing - the pods keep running and
the transcript is on the stream.

## Turns and tasks

One user turn is one A2A task. On each inbound chat message the gateway:

1. Verifies the sender against the backend's identity mechanism (below) and drops the
   message if it can't.
2. For a message that starts a task: mints a fresh `correlationId` - this is the
   originating user interaction the payload spec names, so minting happens here and
   nowhere else - plus a `taskId`. A follow-up or steer to a running task reuses that
   task's `taskId` and `correlationId`, and is attributed by its own envelope and
   `authority` block.
3. Publishes `kind: message` to `a2a.tasks.{session}.{taskId}.in` - the session is the
   addressee - with the conversation's `contextId` and the authority block below.
4. Subscribes to the task's `…events` and `…supervisor` subjects (one durable, both
   filters) and relays status and artifact updates back into the conversation. Its own
   supervisor terminals arrive through the same relay and retire the task exactly as an
   executor's terminal does.

   What that costs in grant terms, named because nothing else records it - and stated
   backwards in an earlier draft of this section. A single filter subject rides the
   CONSUMER.CREATE request SUBJECT (`…CREATE.<stream>.<name>.<filter>`, nats.go's
   `apiConsumerCreateWithFilterSubjectT`); a filter LIST travels in the request BODY,
   where no subject permission can see it. So the per-stream enumeration
   `spec-nats-deployment.md` uses to take `web` off `$JS.API.>` -
   `$JS.API.CONSUMER.CREATE.TASKS.>` - would PERMIT this relay, because the bare subject
   matches. What such a grant cannot do is CONSTRAIN which subjects the durable ends up
   reading. Narrowing the gateway the same way is therefore available and costs the relay
   nothing; what it does not buy is any bound on the filters, and that is the honest
   reason the gateway's consumer reach is still wide.

   The form that does refuse a multi-filter consumer is the one that pins the filter into
   the grant, and the session pod is ALREADY on it. `sessionGrants` issues three consumers
   by exact name with the filter riding the CREATE subject, pinned to the pod's own `…in`
   and `…events`; there is no supervisor filter among them and no unscoped
   `CONSUMER.CREATE`, and the pod's only subscribe grant is its inbox. Pinning and
   multi-filter are mutually exclusive by construction: the pin lives in the subject and
   the list does not. So a session pod can create neither a supervisor-filtered consumer nor a
   two-filter one, and cannot core-subscribe the subject either. `message/stream`'s "both
   subjects" is unmeetable for it today, and the narrower consequence is already shipping:
   the worker adapter's respawn check reads `…events` alone, so it cannot see a terminal
   its own predecessor's supervisor declared. The consequence is the split's, not the
   adapter's - on an install whose stream still holds pre-split supervisor terminals the
   check does see them, and stops when that retention window passes. Closing this means one
   more enumerated filter, not a wildcard.

The backend-native message id is recorded against the `correlationId` in the gateway's
ingress log, so the audit chain runs chat message -> correlationId -> every hop -> change.

Tasks serialize per session. A message that arrives while a task is `input-required` is
the follow-up input for that task (same `taskId`, per the payload spec). **Decided 8/24,
reversing this doc's draft:** a message that arrives while a task is `working` is
_injected_ as steering - a follow-up `message` on the same `taskId`, forwarded to the
harness stdin, absorbed at its next turn boundary. That matches what `kanban_comment`
gives users today, the adapter owes the same stdin path to `input-required` anyway, and
each steer carries its own `authority` block, so group-room attribution stays clean. A
cancel affordance ("stop") maps to `kind: cancel` and stays the hard interrupt; wiring it
to a chat gesture is adapter polish, later.

**The deterministic interceptors (amended 8/31).** The gateway holds no model, so its
affordances are literal: a small set of normalized phrases and prefixes, deterministic
by construction. Three interceptors run on each turn, ahead of the routing above:

- **Status ask.** While a task is active, a message matching the status set ("status",
  "what is it doing", …) is answered by stream replay - a status card carrying the
  task's state, the echoed ask, an elapsed clock since submission, the transition
  history, and the latest `progress` line, labeled as replay so nobody reads it as a
  live claim about the executor. It never reaches the executor.
- **Stop.** "stop" / "cancel" / "abort", exact after normalization - the text form of
  the cancel affordance above; a backend-native gesture stays adapter polish, later.
  A stopped task whose terminal event has not yet arrived DETACHES: it stops
  serializing the conversation - new turns start new tasks - while its events, if
  they ever arrive, still relay. "Detached" and "non-detached" below mean exactly
  this state and its absence.
- **Delegate.** A prefix, not a phrase, and recognized only when no live task
  serializes the conversation; its own section below.

Two routes exist, and the terms recur below: a conversation is **fixed-routed** when
its tasks address the standing executor configured at deploy time (the platform front
door), and **session-routed** when they address the conversation's own spawned worker.
The two differ on steers: a session worker absorbs them at its next turn boundary,
while the standing front door refuses them with an honest status reply - the refusal
posture the payload spec's steering rule records.

The status matcher's width bias inverts per executor, and the inversion is the
contract, not a tuning detail. Beyond the exact phrase set there is a wide
interrogative rule (status-shaped words in an interrogative frame), and it applies only
where the executor refuses steers - the fixed-route front door - because there a stolen
false positive costs nothing. Where the executor absorbs steers, a session worker, only
the exact phrases match: a stolen steer there is a dropped correction, and a
status-shaped steer is a question the worker can answer itself. Anything no interceptor
claims during a `working` task is a steer, per the 8/24 decision above.

**Gateway-authored posts (amended 8/31).** Step 4's relay - events in, chat out - is
not the whole output story: the gateway authors a small set of posts of its own. The
placeholder that opens a task ("submitted…", which becomes the rolling line the relay
edits), the status card, the steer acknowledgement, the release line for a task that
produced no first event inside the grace (its id and the grace; Session lifecycle
below), and failure notices (a submission or steer that never reached the bus). All
are deterministic templates over facts the
gateway itself owns - its own publishes, its own registry, stream replay - which is
what keeps them inside the no-model rule. They also say only what the gateway knows:
the steer acknowledgement reports that the steer is on the stream, and what it says
next is conditioned on the route the same way the width bias above is, because the
gateway knows the route and the two executors do different things: on a
session-routed conversation, that the worker picks it up at its next turn boundary
if the task is still running; on a fixed-routed one, that the standing executor does
not take mid-task input and the reply will say so. Neither claims the steer was
absorbed, which the gateway cannot know. The payload spec's refusal posture - both
the fixed-route refusal and the race-window one - is what closes the loop on the
stream.

## The Delegate flow (added 8/31)

A turn starting with the word "delegate" plus a separator routes that one task to a
freshly spawned session worker. The prefix is stripped; the rest of the original text,
casing and punctuation intact, is the task. The gateway mints a fresh bus session name,
publishes the task with the new session as its addressee, and spawns the worker through
the same machinery that serves session-routed conversations - a delegate is an
incarnation, not a new kind of executor, and everything above about incarnations
(minted bus session name per spawn, `contextId` persisting) applies. The guard,
stated because the delete rule below depends on it: a delegation is recognized only
when no live task serializes the conversation. While a task is `working`,
"delegate: …" is a steer like any other text - the routing above claims status asks
and stops first and steers everything else, and the delegate prefix is consulted
only when the turn would start a task. A bare "delegate" with nothing after it is
not a delegation, and while the spawner is dark the prefix is not an affordance at
all: the text passes through as an ordinary turn.

Two rules keep the conversation's route coherent:

- **One task.** The delegation covers exactly the prefixed task. On a fixed-route
  conversation, the next plain ask re-homes to the configured default addressee - never
  to a dead delegate session. On a session-routed conversation, the delegate
  incarnation becomes the next standing incarnation, which is inside the session
  route's contract: incarnations rotate anyway.
- **The previous incarnation dies at delegation, deliberately.** A lingering pod from
  an earlier incarnation is deleted, not merely untracked: reap walks session records
  and sweep sees only terminal pod phases, so an untracked Running pod would hold its
  bus credential with nothing able to reclaim it. The active-task guard above bounds
  which pods this can reach: a delegation is only recognized when no task serializes
  the conversation, so the pod is either idle or running a DETACHED task - and
  detached is not closed, so the deletion rule in Session lifecycle applies and that
  task's terminal is published first. The state is that rule's to name, not this
  section's. With that done, the delete is what reap or sweep would have done anyway.

## Session lifecycle

The session manager is the demo's chatops code generalized from one-shot workers to
long-lived sessions. Four operations:

**Spawn.** First message in a conversation creates the pod: the demo's reference worker
shape (no ambient k8s credentials, scratch on emptyDir, 250m/512Mi requests; egress
fenced (8/31) to DNS, the bus, and LiteLLM - the deployment spec owns the policy),
running the headless harness behind a thin shim that bridges bus envelopes to the CLI's
stream-json stdin/stdout. Model auth, as shipped (amended 8/31): the worker talks to
the install's own LiteLLM, in-namespace, with no _cloud_ credential at all - the spawned
pod has no Workload Identity. **Amended 9/8:** it does now carry a ServiceAccount and a
bus credential of its own. The static `worker` password is gone from the pod entirely; in
its place is a projected ServiceAccount token, audience-bound to the bus and bound by the
kubelet to this pod, which the auth callout resolves into grants derived from the attested
pod name - this task's events, this session's three consumers, this session's inbox. The
harness can still read that credential, because it runs at the same UID in the same pod;
what changed is that reading it buys the authority the harness already had (gke-labs#1270).
The deployment spec owns the reasoning. Direct Vertex via WI
stays the target, and arming it is a policy change as well as an IAM one: the session
egress fence encodes the shipped path (no 443, no metadata route), which is where a
piecemeal flip fails loudly instead of silently widening. Cold start is 5-10s; the
adapter posts a placeholder to the conversation while the pod comes up, which the demo
already does.

**Stream.** The shim consumes envelopes addressed to its session, feeds them to the
harness, and maps the stream-json output to `status-update` and `artifact-update` events.
The gateway relays events to the conversation. The gateway never parses harness output;
that translation lives in the shim, next to the process it translates for.

**Reap.** Idle TTL since the last user message (30 minutes, config-backed). Reaping is
deleting the pod. Nothing is saved first, because
the stream already has everything - that's the whole point of the transcript of record.
The KV entry stays, holding the `contextId`. Reap never deletes a pod out from under a
live task: an active task that has not detached (see Stop above) exempts the session
from the idle TTL. The exemption is safe because the pod's end has owners. The session
worker's adapter enforces a task deadline (30 minutes default, config-backed): at the
deadline it kills the harness process group and publishes the terminal event itself,
and a pod that dies wedged reaches a terminal phase where Sweep takes over. The
spawner sets the pod-level `activeDeadlineSeconds` above that deadline (the adapter's
deadline plus a fixed grace for the image pull), so a wedged adapter also lands in
Sweep's domain instead of holding its bus credential indefinitely. Two ends still have
no terminal to wait for, and the record carries an independent bound for each rather
than a justification that assumes a terminal that may not come. The `ask` copy is
cleared by the reap scan once it is older than `A2A_ASK_TTL` (24 hours by default,
under the stream's retention, which is what the content posture below needs). A task
with nothing on either of its event subjects - no pod, or a pod that never ran - is
released from the serialization at the conversation's next turn once it is older than the
first-event grace (`A2A_FIRST_EVENT_GRACE`, 10 minutes by default), with one line in the
conversation saying so. Both subjects, not just `…events`: the fold reads them together,
and assertion 9 exists because a task whose only event is its supervisor's terminal is not
empty. That release publishes no terminal: age alone is not
evidence, a first event that is merely late could still arrive, and no supervisor path
ever sees a task with no pod, so its submission ages out with the stream's retention -
named here rather than papered over. Otherwise the terminal event this chain
guarantees is what deletes the active-task record (and the `ask` copy riding it). A
detached task is the exception on both counts: it does
not exempt the session, so reap may delete a pod whose harness is still working, and
the supervisor rule below is what keeps that from being a silent stop.

**One rule for every pod the gateway deletes itself** (stated once here because four
paths reach it - reap, Sweep, Delegate, and any future one): if the pod is running a
DETACHED task, the gateway publishes that task's terminal event before deleting, as
the supervisor of the sessions it spawns - on the task's `…supervisor` subject, which
only the gateway's grant reaches (the 9/9 split), never on its `…events`. The state is
`canceled`, not `failed`:
every task this rule reaches is detached, and detached means a `stop` already
published a cancel, so the gateway is finishing the cancel the requester asked for
rather than reporting an error. That keeps assertion 13's enumeration intact
(`canceled`, or `completed` if the race was lost) and keeps replay able to tell a
stopped task from one that broke. Deleting first strands the task
non-terminal for the whole retention window - the adapter's deadline dies with the
process, and a deleted pod never reaches the terminal phase Sweep watches for - which
would break both that assertion and the payload spec's every-task-has-a-supervisor
rule. Do not
reason from the config defaults here: the adapter's deadline runs from task start and
the idle TTL from the last user message, so which fires first is a property of two
independently tunable numbers, not a guarantee.

**Rehydrate.** The next message on a reaped conversation spawns a fresh pod. The
gateway replays the context's tasks from JetStream - `tasks/get`, which folds each
task's `…events` and `…supervisor` together - into a transcript primer, and hands it to
the new pod as its first input. If the harness's own session file
happens to survive (it usually won't), `--resume` is a shortcut - correctness never
depends on it; session files are cache, the stream is the record. Task-stream retention bounds how far
back rehydration reaches (72h placeholder in the payload spec). I think that's a
feature: a three-day-silent thread restarting with fresh context is better than a bot
that suddenly remembers June. If review disagrees, the fix is a compacted transcript
topic, not longer task retention.

**Sweep**, as in the demo: a pod in a terminal phase whose task never emitted a final
event gets a terminal event published by the gateway on the task's `…supervisor`
subject, then deleted. This is the
gateway's half of the payload spec's orphaned-task answer - it is the supervisor for
sessions it spawned; the dispatcher's janitor is the other half (settled 8/24). The
state follows the same rule as every other supervisor publish below: `failed` for a
task whose executor died mid-work, `canceled` where the task had already detached and
the gateway is finishing a cancel the requester published. Sweep reaches detached
tasks routinely - a worker that exits or wedges after a `stop` leaves exactly this
shape - so an unconditional `failed` here would report broken for every task a user
stopped, which is the distinction the rule exists to keep.

## Requester identity on the bus

The payload spec reserves two envelope fields and this doc names what goes in them.

**`identity` stays empty - permanently, as of 9/9.** It is the link-level field: the
verified identity of the _publisher_, bound to the authenticated NATS connection. A
server-stamped header was ruled out empirically (8/24), and the choice that was left
open here - signed claim vs subject-derived identity - is now settled as
subject-derived. The publisher is the principal the subject implies, because the
subject's writer set is pinned by connect-time grants. There is nothing for a
publisher to write into `identity` that a consumer would be right to read, so the
field is not "unarmed pending a mechanism"; it is empty because the mechanism that
replaced it puts the answer somewhere a publisher cannot reach. The gateway has no
business writing it, and neither does anyone else.

**`authority` is the request-level field, and the gateway populates it.** Who asked,
verified how, in front of whom:

```json
"authority": {
  "requester": {
    "principal": "hmac:9f4c21…",
    "backend": "gchat",
    "subject": "hmac:9f4c21…",
    "verifiedBy": "chat-event-topic-iam"
  },
  "audience": {
    "conversation": "gchat:spaces/AAA/threads/BBB",
    "kind": "group",
    "roster": ["hmac:9f4c21…", "hmac:77d0e2…"],
    "rosterComplete": true
  },
  "grants": null
}
```

**Identifiers in `authority` are pseudonymous (decided 8/24).** Principals, subjects,
and roster entries are HMAC-SHA256 with the install's salt before anything is written to
the bus - the same posture the shipped attribution path applies before writing session
metadata (`docs/designs/audit-logging-user-attribution.md`). The bus holds labelled
content at rest for the whole retention window, so it gets the same treatment as the
session KV. The plaintext join lives in the gateway's local ingress log, and the
gateway resolves plaintext at the boundaries that need it - `openDirect` now, the
lowest-common-denominator grant computation when the authority work lands.

**The salt is `SESSION_KV_SALT`, the one the install already provisions** (settled
8/31). It is generated once into `platform-agent-secrets`, deliberately never
rewritten on upgrade - rotating it re-anonymises every user and breaks correlation
with their own history - and it is what the shipped attribution path already hashes
with. Using it is what makes the "same posture" claim above true rather than
approximate: one human hashes to one value in session metadata and in
`authority.requester.principal`, so the cross-surface audit join resolves. One
presentation caveat rides that sentence: session metadata stores the full 64-hex
digest while the gateway publishes `hmac:` plus the first 32 hex characters, so the
join key is the digest - a prefix match, not string equality. A second
salt would not merely be redundant, it would silently yield nothing on exactly the
join this rule exists to preserve. The requirement that follows: one value per
install, read by every gateway replica from that Secret. Deriving a salt from
another credential - the stage 1 gateway derives from the bus password when none is
configured - is a deviation on two counts, the broken join and a de-anonymization
key handed to whoever holds that credential over an identifier space (chat emails, a
room roster) small enough to enumerate. That derivation is HKDF-SHA-256 over the
password under a fixed info string rather than a digest of it, which is what a
credential is permitted to pass through and nothing more: it leaves both counts
where they are, and HKDF has no work factor, so a hand-set weak password is no
harder to recover from a leaked salt than it was. Changing the derivation at all
re-salts every pseudonym on an install running the fallback - the never-rewritten
property above is a property of the provisioned Secret, not of a value computed from
a credential.

**The rule covers identifiers, not content (stated explicitly 8/31; it was always the
design, never written down).** Task content cannot be pseudonymized without destroying
it - the executor has to read the ask - so the submission message rides the TASKS
stream in the clear for the whole retention window, which is exactly why the deployment
spec calls W a tenancy decision. What the rule forbids is identifiers that carry
content; the payload spec's opaque-token rule is the same rule seen from the other
side. Within that posture, the gateway may hold a bounded copy of the active task's ask
in the session KV - truncated, one revision, deleted with the active-task record at the
terminal event or when the gateway releases the record (Session lifecycle) - for
status rendering. The copy adds no new audience (the gateway is
the bucket's only reader and writer, by grant - see the deployment spec's note on the
one residual write route) and has a shorter horizon than the stream copy it
duplicates. What would breach the posture is content anywhere with a wider audience or
a longer life than the stream already grants it - which makes the horizon a condition
on the copy, not a property of it. The horizon holds only where a terminal event is
guaranteed, so the ends Session lifecycle names as having none are cases where this
justification does not hold on its own, and the `ask` TTL and first-event grace named
there are what carry it.

- `requester.principal` is the pseudonymized identity in _our_ trust domain; the gateway
  resolves it to the RBAC string at the boundary that needs one. `subject` is the sender
  id in the backend's own vocabulary, hashed likewise, kept for audit joins — Discord's
  immutable snowflake; on Google Chat the asserted email, which is also the principal, so
  the two hashes are equal there (as in the example above). `verifiedBy`
  names the mechanism that checked it at ingress.
- `audience` is a snapshot of the room at the moment of the ask (see group chats below).
- `grants` is reserved for the attenuating capability token when the authority work
  lands. Until then it is null and the field is advisory.

How `principal` gets established depends on the backend, and the three are not equal:

- **Google Chat:** the sender email is asserted by Google Chat itself — Google
  authenticated the user's session, and the event reaches us over a Pub/Sub topic only
  Google's Chat service accounts may publish to. The resolved email is the same string
  as the cloud principal and the RBAC subject: one trust domain, nothing to map. This
  is why gchat is the supported production ingress. (Corrected 9/5: an earlier version
  of this bullet said requests carry a Google-signed token. A per-request signed token
  is a property of the HTTPS-endpoint app shape, which we do not ship; on the Pub/Sub
  shape the mechanism is topic IAM, and the impersonation surface is exactly the set of
  identities holding `pubsub.publisher` on the topic. The Google Chat adapter section
  below names it precisely.)
- **Slack:** join on the immutable `user_id` against a mapping table we maintain from our
  own IdP. Never `profile.email` - whether that field is IdP-asserted or user-editable
  depends on workspace config we don't control.
- **Discord:** a checked-in test mapping table from Discord user id to a test principal.
  Test-only, by construction: a Discord identity never maps to a real cloud principal,
  full stop.

**What advisory means, stated plainly:** the gateway verifies the requester at ingress,
but nothing stops another bus client from publishing an envelope with an invented
`authority` block. So consumers MUST NOT authorize on it yet. It is carried now for the
audit trail and for parity testing.

**Corrected 9/9: it does not become decision-grade "when `identity` arms."** `identity`
never arms; see above. And subject-derived publisher identity, which did land for the
task plane on 9/9, is not enough on its own either - it says which principal wrote the
bytes, while `authority` claims which human asked. An executor writing its own
`…events` subject is the legitimate writer of that subject and can still put any
`authority` block it likes in the envelope. What `authority` needs is a rule binding
the block to the one publisher entitled to originate it, on subjects only that
publisher writes; that is the authority half of the consumer rule, and it is still
owed.

The payload spec has carried this rule since 0.3: `authority` is populate-by-gateway-only,
consumers forbidden from deciding on it, libraries pass it through untouched.

## Group chats: who is in the room

Group support is required - teams live in shared channels, and every chat product trains
them to expect the bot there. The full answer (a classifier judging what's appropriate
for a mixed audience, a lowest-common-denominator permissions tool to feed it) comes
later. What the gateway builds now is the substrate those need:

- **Roster.** The adapter tracks membership per conversation from the backend's
  membership API and events. The roster is mapped principals where the mapping exists,
  backend subjects where it doesn't - pseudonymized like every identifier in `authority`.
- **Audience in every envelope.** Each turn's `authority.audience` snapshots the roster
  at ask time. Snapshots, deliberately: when the classifier later asks "who could have
  read this," the answer is in the envelope for that turn, not reconstructed from
  membership history. Rosters cap at 32 entries; past that, `rosterComplete: false` and
  the eventual LCD tool reads membership live instead.
- **A DM-switch primitive.** The adapter interface includes `openDirect(requester)`, so
  a reply can be routed to the asker privately with a notice left in the room. The
  gateway ships the primitive; the classifier that decides to use it comes later. Until
  then everything posts to the room it came from.

One property worth stating because it falls out of the session model: a group session's
context is labeled for the room. Everyone in the thread shares the pod, so anything the
harness reads into context is readable by the whole roster - which is exactly as leaky as
the chat window itself, no more. When per-user authority lands, the
lowest-common-denominator grants for a group session compute against the audience, not
just the requester. That is the LCD question, parked with its tool.

## The test backend

The brief: pick whichever backend gets us to test reality fastest with no review cycle,
and keep the other two as adapters. Comparison:

|                | Google Chat                                                    | Slack                                                        | Discord                                   |
| -------------- | -------------------------------------------------------------- | ------------------------------------------------------------ | ----------------------------------------- |
| Standup path   | Cloud project, Chat API config, app published to the workspace | App created in a workspace we admin                          | Bot token, self-owned server, invite link |
| Review cycle   | Workspace admin approval                                       | Workspace admin approval (we don't admin the corp workspace) | None - we own the server                  |
| Ingress shape  | Inbound HTTPS endpoint or Pub/Sub                              | Socket Mode (outbound WS) available                          | Outbound WS only, native                  |
| Identity story | Google-asserted email, IAM-locked topic, same trust domain     | `user_id` + our mapping table                                | Test mapping table only                   |
| Threads / DMs  | Yes / yes                                                      | Yes / yes                                                    | Yes / yes                                 |

**Discord for test reality.** It is the only one of the three with no approval gate of
any kind - we create the server, so there is no admin to wait on - and its bot model is
outbound-websocket-only, which means no inbound endpoint on the dev cluster and no
ingress to secure for a test rig. The identity mapping table is a feature here rather
than a compromise: it keeps a toy backend structurally incapable of asserting a real
principal.

Google Chat stays the supported production ingress, for the trust-domain reason above -
it is the first real adapter, specified in its own section below. Slack follows when a
customer asks, with the mapping table as a hard prerequisite.

The adapter interface is what makes the pick cheap: inbound message with verified sender,
conversation and thread identity, roster read, post-to-conversation, `openDirect`. Five
operations, normalized. The console adapter is the third implementation and the smallest;
see its own section. If the Discord adapter leaks Discord-isms through that interface,
that's a bug in the interface, and better to learn it on the throwaway backend.

### The inject backend (added 9/17)

A third backend beside Discord and Google Chat, and the one with no human on the other end:
an HTTP door into `handleInbound` for a program. It is the next-stack analogue of the door
the eval harness uses today, which posts to the agent's own `/v1/responses` and bypasses chat
entirely - a path identical under both modes, so a run through it says nothing about the stack
`mode: next` renders.

**Why it is a backend rather than a bus client.** The harness could publish a submission
straight to `a2a.tasks.platform.{taskId}.in`, and that proves the bus, the callout, the streams
and the executor. It leaves out the gateway: its routing, its session registry, the relay back,
and whatever the router becomes in round 3. It also needs a bus identity, and the only static
user whose grants fit a requester is the gateway's own - handing a second process the one
credential that may publish on `.in`. Going through the gateway instead means the gateway keeps
that credential, mints the ids and the `authority` block itself, and the harness needs neither a
NATS client nor a principal of its own.

**A side door, not a fourth backend.** The one-backend guard refuses two real backends because
two processes on one relay durable split its event deliveries, and the symptom is a gateway that
looks healthy and answers half the messages. A local HTTP door has no such failure mode, so it
may sit beside exactly one real backend and the guard keeps refusing two of those - which is what
lets one install run the eval door and the Chat relay and compare them. The door alone also
counts as an ingress, so an eval install with neither a Discord token nor a Chat relay starts its
gateway instead of crash-looping. That last sentence is a decision, not an assumption of this
section: the A2A owner decided it on the eval transport's design doc (`eval-next-transport.md`,
2026-09-17), and the guard's own comment in `config.go` records it the same way. A gateway that
starts on the door alone says so - a warning at start, and `injectOnly` on the door's read route -
because a `mode: next` install whose relay URL failed to render looks exactly the same and must
not be silent about it.

**The three endpoints.**

- `POST /inject` takes a conversation key, an author and the text. The adapter prefixes the key
  with `inject:` and delivers an `InboundMessage`; from there the turn is indistinguishable from
  a Discord message - the text is routed the way any text is, the gateway's literal affordances
  included, so a prompt whose first word is `delegate` or `stop` reaches those paths rather than
  the persona. The harness sends none of them, and a case whose prompt opens with one is the
  case's defect. The reply carries the task id, so the caller can await a named task rather
  than guessing which post belongs to its submission, and the gateway's first-event grace, so the
  caller can set its own deadline above the window the gateway already owns.

  A task id in the reply means the submission is on the task's `in` subject, not merely that the
  gateway minted one. `startTask` announces the id, posts the placeholder and writes the record
  before it publishes; if the publish fails it edits the placeholder to say so, releases the
  conversation and returns, and nothing is on any subject. So the door waits for the publish
  (`TaskObserver.TaskAccepted`) before it answers, and a caller is never handed an id whose
  terminal cannot come. Everything else is a refusal, carried as a code the caller branches on
  rather than as prose it would have to match: `unverified-author` (the door's principal map does
  not carry the author, so the gateway dropped the message), `publish-failed`, `no-task` (a steer,
  a status answer or a stop) and `no-answer` (the bound expired with the gateway visibly doing
  nothing, or the door's own retention scrolled past the answer: more tasks than it keeps started
  on the conversation, or the conversation itself was evicted and minted again under the wait). A repeat of the same backend message id is answered with the same refusal, and the
  reply the conversation received is in the same payload. All four are infrastructure to the
  harness: in none of them did an agent see the prompt.

  A refusal is read off the end of the turn rather than guessed from the transcript. The gateway
  tells the adapter when an inbound turn finishes and when it drops a message
  (`InboundObserver`), because neither is knowable from what the conversation received: the heal
  posts twice before `startTask` announces, so a door concluding "answered without a task" from
  posts alone can tell its caller nothing started while the turn goes on to mint a task that then
  runs unobserved; and the unverified-sender notice is posted once per sender, so a second
  message from the same author posts nothing at all. The notice's own dedupe stays the gateway's.

- `GET /conversations/{key}` returns what the relay posted, as a sequence a caller pages through
  with `after`, plus `wait` to block for something new and `task` to name the task whose terminal
  the answer should carry. Those three are the whole of "await terminal of task id X". Replies
  come back the way the relay posts them - the placeholder, the rolling progress line's edits,
  the deliverable, the terminal - because what a verifier grades has to be what a customer would
  have read. With `probe=1` the same read is also the **read route**: a pure read of the
  conversation's session record and of the active task's stream, which mutates nothing - no
  heal, no lock, no post, no publish, no write. It returns the record's active task with its
  `submittedAt`, age and `detached` flag, the latest executor state the stream shows (none,
  `submitted`, `working`, or a terminal, with `final`), whether `working` was ever on the stream
  (`reachedWorking`, read off the fold's history, because two events can land between a
  caller's reads and the latest state alone would hide the one that says a model ran) and, when
  the stream is terminal, the
  fold of it: whose word the terminal is (`terminalSource`, the executor's for a terminal on the
  task's events subject and the supervisor's for one on its supervisor subject - distinct from
  the gateway's own source, which says it could not publish the task at all), the result
  artifact's text and the terminal's status message; plus the conversation's last post, the
  gateway's configured first-event grace, and the armed backend with `injectOnly`. The gateway
  classifies nothing on it; the harness does. It is a pure read because the never-started heal
  is a write under the per-conversation lock inside the keyed queue, and a read that performed
  it would be a second writer racing the next inbound message (the A2A owner's constraint). It
  is a read rather than a message because every message a program could send to the gateway is
  itself a turn - a status phrase falls through to `startTask` and mints a task whose ask is the
  phrase, and a cancel sets `Detached`. The harness asks for it on every poll, which is where the
  task's lifecycle comes from, and reads six outcomes off it: a terminal posted, grade normally;
  an active task with no executor event and an age past the grace, infrastructure (no executor
  took it, whether or not the gateway has since released the conversation on some turn - the
  harness keys every case and repetition to a fresh conversation, so release timing does not
  matter); `submitted` only for the whole budget, infrastructure (the bridge queued it behind its
  concurrency cap and never ran it); a state past `submitted` that is never `working` for the
  whole budget (`input-required`, say), infrastructure (an executor took it and did not run it);
  `working` at the budget, a graded timeout; and an active
  record whose stream already folds to a terminal, which is the relay's lost record write (the
  relay acks a terminal before it clears `ActiveTask` and writes the record, and on a key never
  reused no heal arrives) - graded like a finished run from the fold's result text when the
  terminal is the executor's, infrastructure when it is the supervisor's (an executor that died
  or never ran), and never cancelled. The budget is the harness's own
  (`AGENT_INJECT_TIMEOUT`, 1800 s by default), floored at the grace plus a margin and refused
  below it before anything is started, not the grace itself: the grace stays the gateway's
  nobody-took-it detector, and a presubmit that runs more units in parallel than the bridge has
  slots would otherwise grade every queued unit as a timeout.
- `POST /conversations/{key}/cancel` stops the conversation's running task. An explicit route
  rather than the `stop` text the chat path matches: a program should not reach a control path
  through a phrase list the gateway is free to change, and an ask that happens to be the word
  "stop" is indistinguishable from an intent. It lands on the bus as the same `kind: cancel`
  envelope the text route publishes. The body may name the task (`taskId`, the id the POST
  answered with); named, the cancel is published whether or not the record still holds the task
  as active, from the task's history entry (its addressee and correlation id), and refused for a
  task the conversation never held. The harness sends it after a read has classified the task,
  never before, and in every outcome that leaves an active task - `working` at the budget (a
  graded timeout), queued for the whole budget, never taken by any executor, and a read that
  could not classify - always naming the task. The classification is the read's and never the
  cancel's answer; the cancel bounds a stray run. It has to be named because for a task nobody
  took the cancel turn itself runs the never-started heal first, which releases the record
  while the submission is still on the in subject, and the bridge's durable consumer delivers
  from the start of the stream, so a bridge that binds later within retention would run the
  stale prompt. On today's bridge the cancel does not prevent that spawn: the consumer delivers
  serially, an idle worker spawns the stale prompt before the cancel is dispatched, and the
  cancel kills it inside the kill grace with a `canceled-by-request` terminal;
  `canceled-before-start` is what a task still queued behind the cap gets. A pre-spawn look-ahead
  for a trailing cancel is bridge work, not the door's.

All five adapter operations are implemented rather than a subset the session manager has to
special-case. The session key is `inject:<key>`, `Kind` is `dm`, `Roster` is the requester alone
with `complete: true` - a synthetic conversation has one participant and no membership API to be
incomplete about - and `openDirect` returns that same conversation, because there is no second
surface to switch to. The harness keys every case and repetition to a fresh conversation,
`inject:<run>/<case>/<rep>` with a run id minted fresh per invocation and never pinned from the
environment (a pinned id would have the dedupe below answer a rerun with a previous invocation's
task and its terminal), and sends the same
`<run>/<case>/<rep>` as the backend message id, so the gateway's `ingress` log joins it to the
`correlationId` and the audit chain reaches the eval record, which stores the three beside the
task id. The door also dedupes an accepted POST on that id, bounded like the Google Chat adapter's
redelivery set: a retry of a POST whose connection dropped carries the same body and id - by then
`startTask` has written the active task, so an undeduped retry would be routed as a steer on it -
and is answered with the task the first POST started, starting nothing, including a retry that
arrives while the first turn is still in flight. The id is unique per invocation, so a rerun is
never mistaken for a retry, and each status turn of the harness's delegation wait carries its own,
`<run>/<case>/<rep>/status-<n>`, so the dedupe does not fold it into the opening task.

The gateway tells the adapter a task's ends through an optional `TaskObserver` interface the
chat backends do not implement: a human reads the chat, so rendered text is their whole
interface, while a program must not have to parse `✅ **completed**` to know a task is over. The
start and the accept are separate calls because the id and the submission are different facts,
as the refusal above turns on. The terminal says who declared it, which is the difference between
an executor that failed, a task that never reached the bus, the supervisor's word about an
executor that died, and one the never-started heal released past `A2A_FIRST_EVENT_GRACE` - four
states that look alike to a caller and mean the agent's fault, an outage, a dead executor, and an
install with no executor. It also carries the executor's reason verbatim - the
terminal status message the bridge and the worker adapter write as `reason: <token>[ - detail]` -
because a failed terminal is not always the persona's failure: the harness reads the token and
classifies the executors' own reasons (`bridge-shutdown`, `bridge-queue-overflow`,
`bus-publish-failed`, `spawn-failed`, `bridge-died-without-terminal-event`, `worker-evicted`,
`bus-subscribe-failed`), a `rejected` terminal and a `canceled-before-start` as infrastructure,
and grades the persona's (`hermes-exited-nonzero`, `deadline-exceeded`) and any reason it does not
know; a `canceled` after the harness's own cancel is the graded timeout. An eval install that
declares the bridge sidecar sets `BRIDGE_CONCURRENCY` to at least the harness's parallelism
(`EVAL_TASK_PARALLELISM`), because the bridge publishes `submitted` when it queues a task behind
its cap and `working` only when it spawns, and a unit queued for the whole budget is
infrastructure, not a graded answer.

**Identity.** An injected author is resolved through the door's **own** principal map, at a
prefixed key, and the value must be an eval identity. Three refusals follow from that, and
together they are what keeps a door that takes its author from a request body structurally
incapable of asserting a real principal - the property the Discord mapping table has and the
reason this section calls that table a feature:

- the lookup is `inject:<author>` in a map of the door's own, so an entry written for a real
  backend's sender is unreachable from here;
- a value outside the eval namespace is refused rather than honoured, so a mistake in the map is
  a lockout and never a privilege;
- an unmapped author is dropped with the notice the Discord path gives - logged, told once, no
  task. Nothing is defaulted.

`verifiedBy` is `inject-bearer`, its own value and deliberately nothing a real backend stamps:
the map decides _which_ principal an author id stands for, and what admitted the caller is the
door's token rather than Discord's authenticated websocket or Chat's IAM-locked topic. A consumer
that treated the three alike is what this value exists to stop.

**Posture: a dev and eval door.** Five things confine it.

1. **Every request carries a bearer token.** The operator renders it into a Secret beside the
   door and the gateway refuses to arm the door without one; there is no unauthenticated mode.
   This is the control, not a second layer over the bind and the fence below: those govern
   pod-network traffic, and the eval runner arrives through `kubectl port-forward`, which the
   kubelet serves from inside the pod's network namespace and is exempt from both. Without the
   token the population that can drive the platform persona -
   with the install's cluster and GitHub credentials, past the allowed-users gate - would be
   everyone holding `pods/portforward` in the namespace rather than the holders of the key the
   door stands in for.
2. **It is off unless armed**, by an operator-level environment variable rather than a CRD
   field. A CRD field would put "render the door that maps a body-supplied principal" in the API
   a cluster's owner edits, and the operator would be obliged to honour it. Whether an install is
   an eval install is a property of who deployed the operator - the same shape as the A2A image
   overrides. **The adapter renders only under that flag and never in a chart or install path a
   customer reaches**, and that is a property tests check rather than a comment to trust: the
   operator's own render tests assert that with the flag unset the rendered object set carries
   no inject Service, no inject env on the gateway, no inject NetworkPolicy and no token Secret,
   and the conformance suite (`tests/conformance/test_A_authority.py`) asserts that every render
   site consults the flag, that the flag is an operator environment variable and not a CRD
   field, and that the door's principal lookup cannot reach a cloud identity.
3. **It listens on the gateway pod's loopback**, `127.0.0.1:8099`, not every interface. A pod
   dialling the inject port is refused before any policy is consulted; the port-forward still
   reaches it because the kubelet dials from inside the pod's network namespace. This is the
   posture the agent's dashboard already has, and it is what keeps every other pod from
   reaching a listener it would otherwise only need a token to use.
4. **Its Service is a ClusterIP.** Not a NodePort, not a LoadBalancer, and it routes nothing:
   it exists so that `kubectl port-forward svc/<cr>-a2a-inject` resolves to the pod and the
   port.
5. **While it is armed, a NetworkPolicy fences the gateway pod against every pod on the cluster
   network**: ingress with no rules. It is a second control over the edge the bind already
   closes, kept so that a reader of the rendered objects sees the intent and so that a later
   change to the bind address does not open the pod network by itself. Neither it nor the bind
   governs the port-forward, which the kubelet serves from inside the pod's network namespace;
   a `hostNetwork` pod on the gateway's node is in the node's namespace, not the pod's, so the
   loopback bind is not reachable from it either. The token is what any caller that does reach
   the listener still has to hold.

**What it also settles.** The gateway refuses to start without a backend, which makes it
crash-loop on any install with neither a Discord token nor a Chat relay - so a `mode: next`
install never reads Ready and nothing can rollout-gate on it. An eval install with this door
armed has an ingress the guard accepts, by the decision recorded above, and starts.

## The Google Chat adapter (added 9/5)

The first real adapter, and the production ingress. What makes it that is the identity
property above: the sender email Google Chat asserts is the same string as the cloud
principal and the RBAC subject, so there is no mapping table and no impersonation
surface in one. Same string, not same enforcement yet: the email rides the authority block,
and `authority.requester` is advisory — permanently so, as far as the envelope is
concerned. The signed-claim-vs-subject-derived question this paragraph used to leave open
was answered on 9/9, and the answer is subject-derived identity (Verified identity, in
`spec-a2a-payloads.md`): a property of the subject a message arrived on, which says
nothing about a chat sender. Making the requester enforceable is the capability
envelope's job rather than `identity`'s. Nothing authorizes on the email today, and when
that lands this adapter is already carrying the string it needs. What the adapter costs is inheriting the existing Chat
integration's operational surface, and this section records how it sits on it.

**Ingress topology: the existing app registration and topic, a dedicated A2A
subscription, consumed through the credential proxy.** A Chat app configuration is
per-GCP-project, so "take Chat events directly" means a second project — not an
adapter-PR dependency. And two consumers on one subscription split deliveries randomly,
so the A2A path gets its own subscription on the existing topic: each consumer acks its
own subscription and the who-acks question dissolves. The subscription is pulled by a
second `GoogleChatRelay` instance in the credential proxy (routes
`/v1/chat/a2a/events`, `/v1/chat/a2a/events/ack`, `/v1/chat/a2a/events/nack`), enabled
only when `A2A_GOOGLE_CHAT_SUBSCRIPTION_NAME` is set alongside the project id. The
gateway pod stays cloud-credential-free: it authenticates to the proxy the way the
legacy chat caller does — a projected ServiceAccount token verified by TokenReview —
but with its OWN audience — whatever `CREDENTIAL_PROXY_A2A_CHAT_AUDIENCE` names on the
proxy; nothing in-tree fixes the string yet, the operator wiring will — conferring
the `a2a-chat` role, because the legacy chat caller is the LLM-driven Hermes pod and a
shared role would let a prompt-injected agent pull and ack the A2A gateway's events,
silently consuming user asks. The event routes demand `a2a-chat`; `/v1/chat/api`
admits both chat roles, since posting rides one shared app credential either way. The
one Chat credential in the deployment stays in the broker, where the relay moved it, and the passthrough
keeps the destructive-method denylist and the error-scrubbing in force for the new
path without new code.

**Two wire shapes, one decoder.** A Chat app publishes one of two event layouts to its
topic, and which one is a property of how the app was registered, not of the message.
The Chat-API registration sends `{"type":"MESSAGE","space":…,"message":…}` — the layout
`tests/e2e/gchat_agent_test.py` forges. An app configured through the Google Workspace
add-on surface sends the add-on event object: `{"commonEventObject":…,"chat":{"user":…,
"eventTime":…,"messagePayload":{"space":…,"message":…}}}`, with no top-level type at
all — the interaction kind is which `chat.*` payload key is present
(`messagePayload`, `addedToSpacePayload`, `removedFromSpacePayload`, `buttonClickedPayload`,
`appCommandPayload`, `widgetUpdatedPayload`). Measured 2026-09-09: every event from the
app in `bnaylor-kagents-dev` arrived in the add-on layout, and a decoder that read only
the legacy one acked all of them away. The adapter decodes both into one normalized
event, and every event that is not a turn is acked WITH a log line naming why
(`gchat event is not a turn`): an adapter that acks silently is indistinguishable from
one that receives nothing, which is how the second layout went unnoticed until live
traffic. The captured payloads live in `a2a/gateway/testdata/gchat/` — email, display name,
user id, space id, avatar, domain and the one-time redirect token replaced, every other
byte as published — and the tests run against them.

Ingress is at-most-once, by decision rather than accident: the adapter acks each
pulled event before handing it to the session manager. Acking after a durable publish
would be at-least-once, but the redelivery dedupe is in-memory, so a redelivery
racing a slow publish across a restart becomes a DUPLICATE task — a worse failure
than a lost ask, which a user retries by typing again. It also matches the other
backends' ingress semantics: the Discord and Slack websockets redeliver nothing.

**Coexistence is by activation, not routing.** `mode: next` is additive, so a next
install still runs the legacy chat consumer. A topic fans out to every subscription:
an install that enables the A2A subscription while the legacy path is live will answer
every message twice. The per-install choice of which brain consumes Chat belongs to
the operator's mode seam (the mode switch's per-component override sketch) and does
not exist yet; the A2A relay instance arms only on explicit configuration
(`A2A_GOOGLE_CHAT_SUBSCRIPTION_NAME`), so arming it beside the legacy consumer is a
stated choice, never a default.

**`verifiedBy: "chat-event-topic-iam"`, and what was actually verified.** The gateway
verified that the event arrived through the credential proxy from a subscription on the
topic whose only permitted publishers are Google's Chat service accounts
(`chat-api-push@system.gserviceaccount.com` and the gsuiteaddons service identity, per
`terraform/modules/chat-pubsub`), and that Google Chat asserted `sender.email` after
authenticating the user's Google session. It did NOT verify a per-request signature —
none exists on the Pub/Sub shape. The impersonation surface is the set of identities
holding `pubsub.publisher` on the topic; the e2e suite exercises the agent by forging
events onto it, deliberately. That surface is a project-IAM boundary, which is the same
trust domain as the principal itself — but it is a boundary, not a proof, and the name
says so.

**Identity plumbing: the email is the id.** `InboundMessage.AuthorID` is the
Google-asserted sender email — the same string the shipped attribution path already
uses as the user id on Google Chat (`docs/designs/gchat-session-metadata-data-flow.md`),
so the cross-surface audit join (session metadata ↔ `authority.requester.principal`)
holds by construction. Resolution to a principal is the identity function, gated by an
allowlist rather than a mapping table: the operator-pinned allowed-users set the legacy
path already enforces, carried as environment (`A2A_GCHAT_ALLOWED_USERS`, or
`A2A_GCHAT_ALLOW_ALL_USERS` stated explicitly), because environment is what the agent
cannot rewrite. An unlisted sender is dropped with a visible once-per-sender notice in
the conversation, not silently. Messages whose sender is not `HUMAN` or carries no
email are dropped at the adapter. `argumentText` is read as Chat computes it: in a
space it is the text with the app mention stripped, so a bare mention is an empty ask
and drops; in a DM (measured) it equals `text` with nothing stripped and no mention
annotation — a typed `@app` there is plain text to Chat, and is delivered verbatim.

**Conversation keys.** `gchat:spaces/AAA/threads/BBB` for a message in a threaded
space — the canonical example above. `gchat:dm/spaces/AAA` for a DM space, whole space
one session — and, because a DM space is threaded, replies render in the thread of the
latest ask (measured: without that, an answer to a question asked inside a DM thread
landed top-level). Presentation only; the key and the session do not move. A space whose threading state does not support replies (`UNTHREADED_MESSAGES`), or a
`GROUP_CHAT`, binds the whole space as one conversation, `gchat:space/spaces/AAA`;
anything that is not positively a DM is read as a group, since a space misread as a DM
would bind every thread in it to one session — the honest reading of "a space is
not a session, a conversation in it is" on a surface where the space is the only
conversation there is.

**Roster.** `spaces.members.list` through the API passthrough, first page, complete
only when the page says so — same one-page posture as Discord threads. Measured
2026-09-09: under the app credential the membership resource carries `users/{id}`,
`displayName` and `type` and NO email, so nothing in the response joins a member to the
principal the requester was verified as. The bridge is the event itself, which carries
both `sender.name` (`users/{id}`) and `sender.email`: the adapter remembers the pair
for every sender it has seen and substitutes the email at roster time, so a member who
has spoken hashes to the same pseudonym as the requester and the audience snapshot
holds one entry per human. A member who has never spoken stays `users/{id}`, hashed as
a backend subject — the roster rules above, where no mapping exists. The requester is
always present in the snapshot regardless.

**Display split.** The existing integration's `default` versus `debug` mode
(`GoogleChatSpec.Mode`) is honoured by the relay: under `default` the rolling line
carries state transitions but never the turn-by-turn narration, with no-op edits
deduplicated; under `debug` the full rolling line runs. Carried as
`A2A_CHAT_DISPLAY_MODE`; the operator owns feeding it from the same CR field, and unset
resolves to `debug` (the historical rendering, so Discord installs are unchanged) while
the CR field's own default is `default` — the render is what makes the two agree. The
split is the legacy field honoured in the new relay, not a new knob.

**openDirect.** `spaces.findDirectMessage` by user resource name, falling back to
`spaces.setup` when no DM space exists yet. The email alias in `users/{…}` is accepted
only under a user credential; the app credential the relay holds answers it 403
(measured), and `spaces.setup` under the app credential needs `chat.app.spaces.create`
plus admin approval, so the fallback is refused too. The adapter therefore resolves an
email to the immutable `users/{id}` it learned from that person's own event, which
`findDirectMessage` does accept; a person who has never spoken cannot be opened. Ships
as the primitive, unused, like the other backends.

## The console adapter (added 9/23)

The web console's chat door. Not a chat product's ingress: the browser is a bus client
already (the `web` read surface), so the door is on the bus too, and the identity story is
the one every other writer here has.

**Transport.** Core NATS, no stream. The browser publishes a frame -
`{"messageId","text","kind"}`, `kind` defaulting to `text` - to `chat.console.<token>.in`;
the adapter posts the gateway's own notices back as `{"messageId","text","edit"}` on
`chat.console.<token>.out`. Outside `a2a.>` on purpose: this is chat transport, not bus
protocol, and the payload spec's agreement rules do not apply to it. The answers never
travel here. They stream through TASKS, which the console page renders directly, so a lost
`.out` frame across a reconnect costs a notice and never an answer. An `.in` frame published
while no gateway subscription is live (the gateway restarting, or the NATS config not yet
rolled) is dropped by core NATS without trace; the browser sees only a pending entry that
never attaches. A receipt frame on `.out` is the natural follow-up for the page.

**Conversation.** `console:<token>`, one dot-free DNS-1123 label per browser tab, kind `dm`.
The gateway treats it like any DM: one session per conversation, spawned on the first turn,
reaped at the idle TTL.

**Identity.** The `console` NATS user is the only principal granted publish on
`chat.console.*.in` (spec-nats-deployment.md, the console surface). So a frame there is from
`console`, and the adapter reports that as the author; the gateway resolves it to the fixed
principal `nats:console`, `verifiedBy: nats-grant`, with no mapping table - the mechanism is
the connect-time grant, which is the same subject-derived identity `identity` was retired in
favour of. That resolution is bound to the console conversation: the string `console`
arriving on a Discord conversation is an unmapped id and drops. One shared principal is the
posture until the account split gives each person a credential, at which point the entry
becomes one inbound subject per principal and nothing else here changes.

**Backend per message.** One gateway process runs its configured chat backend and the console
together, through a mux that dispatches `post`/`edit`/`roster` on the conversation prefix.
The backend named in `authority.requester.backend`, in the drop notice and at verification
is therefore the conversation's, derived from that prefix, with the configured backend as
the fallback for an unprefixed id. `openDirect` takes a bare user id and goes to the
configured backend.

**Bounds.** A frame's text is capped at 16 KiB; over it, the frame is refused with a notice
naming the cap. Empty, malformed and mis-shaped frames drop with a log line each, as does a
frame whose `kind` is anything but `text`. A NATS render that predates the console identity,
or a NATS pod not yet rolled onto the new one, refuses the adapter's subscription
asynchronously; the adapter logs that with the remedy rather than boot-failing,
because the chat backend beside it is still good.

## What stage 2 builds from this doc

- The gateway: Discord and Google Chat adapters, session manager (spawn / stream / reap / rehydrate /
  sweep), bus client, KV session registry.
- The session pod shim: bus-to-stream-json bridge, event mapping.
- The `authority` block, populated at ingress, advisory.
- Roster tracking and the `openDirect` primitive.

Not in stage 2: the classifier, the LCD permissions tool, the slack adapter, `grants`,
and anything that makes `authority` decision-grade. (The gchat adapter was on this
list until 9/5; it now has its own section above.)

## Inherited from the kanban retirement (added 8/24)

The subagent framework's kanban inventory assigns the gateway three rebuilds. Recording
them here so the contract lives in the doc that owns it. They land with the kanban
retirement (stage 3), on the gateway built in stage 2:

- **The rolling progress line.** Render `progress` artifacts into a single edited chat
  message at zero model cost. The bar to meet is the current kanban heartbeat-notes
  rendering.
- **Subscription and wake policy.** Auto-subscribe the originating thread, inherit to
  child tasks, don't wake the requester for routine completion. `correlationId` plus
  durable replay is the substrate; the policy is gateway session state.
- **The report-by-thread store.** A conversation's delivered reports keep enough context
  that "apply Option A" replies resolve. No bus home; gateway session state in KV, like
  the rest of it.

## Open Questions

Calls for [@bnaylor]:

- ~~**Payload spec amendment.**~~ Ratified 8/24; the payload spec has carried it since
  0.3.
- ~~**Idle TTL.**~~ Decided 8/24: 30 minutes, config-backed.
- ~~**Rehydration horizon.**~~ Decided 8/24: retention is the horizon. The compacted
  transcript topic stays the named fix if real users disagree.
- ~~**Discord test tenancy.**~~ Decided 8/24: I own the test server; the bot token is a
  plain Secret in the dev cluster. Test-only posture, stated out loud.
- ~~**Queue vs inject for mid-task messages.**~~ Decided 8/24: inject. The design
  section carries it; the payload spec's steering rule is the A2A shape.
- ~~**Roster cap.**~~ Decided 8/24: 32 stands; large rooms are live-read territory for
  the LCD tool.
