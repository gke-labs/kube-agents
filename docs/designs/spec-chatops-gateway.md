# Chatops gateway design

- **Author:** [@bnaylor]
- **Date:** 2026-08-24
- **Status:** draft for review

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
The declarative subagent framework (its own doc) owns what happens when a session
delegates work; this doc stops at the session boundary.

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
  the durable name of the conversation on the bus.
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
name, bus session name, last-activity timestamp, roster. Runtime state is not git and
not pod annotations; KV is the house answer. A gateway restart rediscovers its sessions
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

## Session lifecycle

The session manager is the demo's chatops code generalized from one-shot workers to
long-lived sessions. Four operations:

**Spawn.** First message in a conversation creates the pod: the demo's reference worker
shape (no ambient k8s credentials, scratch on emptyDir, 250m/512Mi requests), running the
headless harness behind a thin shim that bridges bus envelopes to the CLI's stream-json
stdin/stdout. Model auth is Workload Identity against the Vertex backend - no per-pod API keys. Cold
start is 5-10s; the adapter posts
a placeholder to the conversation while the pod comes up, which the demo already does.

**Stream.** The shim consumes envelopes addressed to its session, feeds them to the
harness, and maps the stream-json output to `status-update` and `artifact-update` events.
The gateway relays events to the conversation. The gateway never parses harness output;
that translation lives in the shim, next to the process it translates for.

**Reap.** Idle TTL since the last user message (30 minutes, config-backed). Reaping is
deleting the pod. Nothing is saved first, because
the stream already has everything - that's the whole point of the transcript of record.
The KV entry stays, holding the `contextId`.

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
event gets a terminal `failed` published by the gateway, then deleted. This is the
gateway's half of the payload spec's orphaned-task answer - it is the supervisor for
sessions it spawned; the dispatcher's janitor is the other half (settled 8/24).

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
    "subject": "hmac:b8813a…",
    "verifiedBy": "chat-signed-token"
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
gateway resolves plaintext at the boundaries that need it - `openDirect` now, RBAC
intersection when the authority work lands.

- `requester.principal` is the pseudonymized identity in _our_ trust domain; the gateway
  resolves it to the RBAC string at the boundary that needs one. `subject` is the
  backend-native immutable id, hashed likewise, kept for audit joins. `verifiedBy`
  names the mechanism that checked it at ingress.
- `audience` is a snapshot of the room at the moment of the ask (see group chats below).
- `grants` is reserved for the attenuating capability token when the authority work
  lands. Until then it is null and the field is advisory.

How `principal` gets established depends on the backend, and the three are not equal:

- **Google Chat:** requests carry a Google-signed token, and the resolved user email is
  the same string as the cloud principal and the RBAC subject. One trust domain, nothing
  to map. This is why gchat is the supported production ingress.
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
the chat window itself, no more. When per-user authority lands, the intersection for a
group session computes against the audience, not just the requester. That is the LCD
question, parked with its tool.

## The test backend

The brief: pick whichever backend gets us to test reality fastest with no review cycle,
and keep the other two as adapters. Comparison:

|                | Google Chat                                                    | Slack                                                        | Discord                                   |
| -------------- | -------------------------------------------------------------- | ------------------------------------------------------------ | ----------------------------------------- |
| Standup path   | Cloud project, Chat API config, app published to the workspace | App created in a workspace we admin                          | Bot token, self-owned server, invite link |
| Review cycle   | Workspace admin approval                                       | Workspace admin approval (we don't admin the corp workspace) | None - we own the server                  |
| Ingress shape  | Inbound HTTPS endpoint or Pub/Sub                              | Socket Mode (outbound WS) available                          | Outbound WS only, native                  |
| Identity story | Signed token, same trust domain                                | `user_id` + our mapping table                                | Test mapping table only                   |
| Threads / DMs  | Yes / yes                                                      | Yes / yes                                                    | Yes / yes                                 |

**Discord for test reality.** It is the only one of the three with no approval gate of
any kind - we create the server, so there is no admin to wait on - and its bot model is
outbound-websocket-only, which means no inbound endpoint on the dev cluster and no
ingress to secure for a test rig. The identity mapping table is a feature here rather
than a compromise: it keeps a toy backend structurally incapable of asserting a real
principal.

Google Chat stays the supported production ingress, for the trust-domain reason above -
it is the first real adapter, built during stage 2 once the interface is proven against
Discord. Slack follows when a customer asks, with the mapping table as a hard
prerequisite.

The adapter interface is what makes the pick cheap: inbound message with verified sender,
conversation and thread identity, roster read, post-to-conversation, `openDirect`. Five
operations, normalized. If the Discord adapter leaks Discord-isms through that interface,
that's a bug in the interface, and better to learn it on the throwaway backend.

## What stage 2 builds from this doc

- The gateway: Discord adapter, session manager (spawn / stream / reap / rehydrate /
  sweep), bus client, KV session registry.
- The session pod shim: bus-to-stream-json bridge, event mapping.
- The `authority` block, populated at ingress, advisory.
- Roster tracking and the `openDirect` primitive.

Not in stage 2: the classifier, the LCD permissions tool, the gchat and slack adapters,
`grants`, and anything that makes `authority` decision-grade.

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
