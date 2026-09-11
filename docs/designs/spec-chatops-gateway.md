# Chatops gateway design

- **Author:** [@bnaylor]
- **Date:** 2026-08-24
- **Status:** merged design of record; the gateway program is implemented (`a2a/gateway`: session registry, authority block, interceptors, supervisor duties, Discord and Google Chat adapters); the operator renders the gateway Deployment, its env and the `A2A_SPAWN_SESSIONS` arming under `mode: next` (`platformagent_a2a_manifests.go`), but not yet the Google Chat adapter's env, its projected relay token, the broker's side of it (`CREDENTIAL_PROXY_A2A_CHAT_AUDIENCE`, the gateway's ServiceAccount on `CREDENTIAL_PROXY_ALLOWED_CALLERS`, and the broker NetworkPolicy admitting the A2A gateway pod), or the A2A subscription and its IAM (the composition still provisions one Chat subscription)

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
4. Subscribes to the task's events subject and relays status and artifact updates back
   into the conversation.

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
the install's own LiteLLM, in-namespace, with no per-pod credential at all - the
spawned pod carries no ServiceAccount and no Workload Identity. Its bus credential is
the static worker user, injected as env. The auth callout does not reach it and arming
the callout did not change it: a session pod carries no ServiceAccount and no projected
token for the callout to resolve, so this closes when each session gets a principal of
its own rather than when the callout arms. The deployment spec owns the reasoning. Direct Vertex via WI
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
with nothing on its events subject at all - no pod, or a pod that never ran - is
released from the serialization at the conversation's next turn once it is older than
the first-event grace (`A2A_FIRST_EVENT_GRACE`, 10 minutes by default), with one line
in the conversation saying so. That release publishes no terminal: age alone is not
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
the supervisor of the sessions it spawns. The state is `canceled`, not `failed`:
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
gateway replays the context's tasks from JetStream, folds them into a transcript primer,
and hands it to the new pod as its first input. If the harness's own session file
happens to survive (it usually won't), `--resume` is a shortcut - correctness never
depends on it; session files are cache, the stream is the record. Task-stream retention bounds how far
back rehydration reaches (72h placeholder in the payload spec). I think that's a
feature: a three-day-silent thread restarting with fresh context is better than a bot
that suddenly remembers June. If review disagrees, the fix is a compacted transcript
topic, not longer task retention.

**Sweep**, as in the demo: a pod in a terminal phase whose task never emitted a final
event gets a terminal event published by the gateway, then deleted. This is the
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

**`identity` stays empty.** It is the link-level field - the verified identity of the
_publisher_, bound to the authenticated NATS connection. A server-stamped header was
ruled out empirically (8/24); the remaining choice - signed claim vs subject-derived
identity - belongs with the deployment spec's account design and the authority work.
The gateway has no business writing it.

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
but until connection-bound publisher identity lands, nothing stops another bus client
from publishing an envelope with an invented `authority` block. So consumers MUST NOT
authorize on it yet. It is carried now for the audit trail and for parity testing, and
it becomes decision-grade only when `identity` arms and the deployment spec's accounts
pin who may publish to the task subjects.

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
operations, normalized. If the Discord adapter leaks Discord-isms through that interface,
that's a bug in the interface, and better to learn it on the throwaway backend.

## The Google Chat adapter (added 9/5)

The first real adapter, and the production ingress. What makes it that is the identity
property above: the sender email Google Chat asserts is the same string as the cloud
principal and the RBAC subject, so there is no mapping table and no impersonation
surface in one. Same string, not same enforcement yet: the email rides the authority block,
which is advisory until publisher identity arms (the signed-claim-vs-subject-derived
decision "Requester identity" above leaves with the deployment spec and the authority
work) — nothing authorizes on it today, and when the requester does become
enforceable, this adapter is already carrying the string that decision needs. What the adapter costs is inheriting the existing Chat
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
