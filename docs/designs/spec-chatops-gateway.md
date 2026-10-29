# Chatops gateway design

- **Author:** [@bnaylor]
- **Date:** 2026-08-24
- **Status:** merged design of record; the gateway program is implemented (`a2a/gateway`: session registry, authority block, interceptors, supervisor duties, Discord, Google Chat and Slack adapters, the console adapter, and the A2A door with its eval and Google sign-in identity classes); the operator renders the gateway Deployment, its env, the `A2A_SPAWN_SESSIONS` arming and the console door (`A2A_CONSOLE_DOOR=true`, so a gateway with no chat backend starts on the console alone) under `mode: next` (`platformagent_a2a_manifests.go`), with the metrics listener (`A2A_METRICS_PORT`, container port `a2a-metrics`) and the gateway's own metrics-only NetworkPolicy (the collector and the operator, on 9096) on every next gateway (see "Metrics"), plus, under its own eval flag, the inject backend below and its Service, principal map, token Secret and gateway fence, and under its own flag the A2A door and the same four objects (but not yet the door's Google sign-in env); and, when `spec.integration.googleChat` is enabled under `next`, the Google Chat adapter's env (the home channel the chat.notify route posts to included), its projected relay token, and the broker's side of it (the A2A relay instance on the install's one subscription, `CREDENTIAL_PROXY_A2A_CHAT_AUDIENCE`, the gateway's ServiceAccount on `CREDENTIAL_PROXY_ALLOWED_CALLERS`, and the broker NetworkPolicy admitting the A2A gateway pod); the legacy Hermes consumer is not rendered under `next`, so the composition's one Chat subscription is the whole of the Pub/Sub the install needs; and, when `spec.integration.slack` is enabled under `next` and Chat is not, the Slack adapter's env (`SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` through the CR's `botTokenSecretRef` and `appTokenSecretRef`, and the CR's `allowedUsers` as `A2A_SLACK_ALLOWED_USERS` and `A2A_SLACK_ALLOW_ALL_USERS`, and the home channel the chat.notify route posts to as `A2A_SLACK_HOME_CHANNEL`), with the legacy consumer - the broker's Socket Mode relay and the Hermes slack platform - not rendered, so one Slack app has one Socket Mode connection; and the `a2a-slack-principal-map` mount either; of the pieces "Sessions by default" names as transition work, the `/session` opt-in is built (`a2a/gateway`: `/session`, `/session <text>`, `/session off`); the session pod's temporary read-only cluster view (a third broker caller under the operator's `A2A_SESSION_CLUSTER_VIEW` flag, off by default) is built, and now that gateway-side `AllowedUsers` enforcement has shipped it retires on the default flip (#2371), not at that enforcement's landing; the gateway-minted child task is built (a session asks with the `delegate` artifact; the gateway checks the platform agent's allowlist - the turn's requester, every steer author, and every author the conversation's current incarnation has seen - mints with the turn's authority and `via`, and wakes the session on the child's terminal; one child at a time, `A2A_DELEGATION_DEPTH_MAX` 3); the session pod's persona and the cluster agent's read-only GKE diagnosis skills ship in the worker image (`a2a/cmd/session-persona`); not yet the `chat` profile's own skills (discovery and standing state, below) or the default flip

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
incarnation of the conversation's own session, not a profile executor - and a second
(9/25): "Sessions by default" below, where the session itself delegates.

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
  group space (eg `discord:1234/5678`, `gchat:spaces/AAA/threads/BBB`); on a backend whose
  DMs are threaded, a thread in a DM can be its own conversation too (the Slack and
  Google Chat adapter sections say when). A channel or space is not a session; a
  conversation in it is.
- `contextId` is minted at first contact with a conversation and persists across pod
  incarnations for the lifetime of the session record (until pruned after `A2A_SESSION_TTL`
  of inactivity). It is the durable name of the conversation on the bus. Minting MUST be create-only (a KV
  `Create`, compare-and-swap semantics), so that two replicas or a rehydrate racing
  first contact cannot fork a conversation - the loser reads and adopts the winner's
  value. The stage 1 gateway runs a single replica and serializes per conversation
  in-process, and minting is the KV `Create` this rule requires - the loser of a mint
  race re-reads and adopts the winner's record before the contextId reaches any
  envelope.
- The pod is an incarnation, not the identity. Reaping and respawning changes the pod
  and the bus session name; `contextId` persists across every incarnation during the
  session record's retention horizon.
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

  **Stopping the cards (gke-labs#3263).** Under `next` the front door's turn often ends
  seconds after it files a kanban card, and the card is not a task the gateway holds. With
  `A2A_CARD_STOP=true` (off by default; the operator renders it from its own environment), a
  `stop` that names no task also sends the default addressee a card-stop control: a task of
  its own whose message is the `cardStop` data part alone (spec-a2a-payloads, "Card work
  after the task ends"). The bridge has session-kv archive every open card in the
  conversation and the cards they spawned, and answers with what it stopped. The gateway
  sends it:
  - with nothing running, in a conversation that has run a task;
  - right after the cancel, when a task is running, since the turn may already have filed a
    card;
  - on a later `stop` while a cancelled task's terminal is still pending, in place of
    "cancel already sent".

  One control at a time. A `stop` while one runs gets "🛑 already stopping this
  conversation's cards — the answer comes when the executor confirms", or "cancel sent" when it cancelled a turn, and marks the control:
  that stop's turn, or one started since the control went out, may have filed cards the
  control has already passed by. When the marked control ends, however it ends, one
  follow-up control goes out. It posts no placeholder and says nothing unless it stopped a
  card or could not finish. One mark is held however many stops arrive. If a question asked
  after the stop is running by then, the follow-up would wait for it and archive its cards
  too, so it is not sent, and the room gets "⚠️ cards filed since the last stop weren't
  stopped, because a newer question is running; say stop again to stop them". It is not sent
  either when the card stop was turned off meanwhile (a restart with `A2A_CARD_STOP` off:
  "… because stopping cards from chat has been turned off") or the conversation moved to a
  session worker (`/session`, or a `delegate` re-home: "… because this conversation now goes
  to a session worker"). The control
  posts "🛑 stopping this conversation's cards…" and
  edits it into the answer: "🛑 stopped N cards: <title>, …" naming at most five, then
  "and K more", and clauses for what the stop could not promise (cards already finished,
  ones that could not be stopped, ones still being stopped, a worker that may still start,
  more cards than one stop takes, a stop that ran out of time). Titles are model text, so
  each is cut to one line of at most 80 runes, with angle brackets and `@` replaced by
  look-alikes so a title can't mention anyone, `[`, `]`, backticks, `~`, `|`, `*` and `_`
  neutralised so it can't make a masked link or bleed formatting, and every dot and colon,
  full-width and ideographic forms included, replaced by look-alikes (`․`, `꞉`) so a bare URL
  or domain is not linked either. A stop that
  found nothing open answers "🤷 nothing is running", which is then true, or "🤷 no card was
  open in this conversation" when a question asked after the stop is already running. A control that is rejected (an older bridge
  answers `no-text-parts`), fails, is canceled, completes with no `cardStop` part, or has
  no terminal inside `A2A_CARD_STOP_TIMEOUT` (75s, above the bridge's worst case of about
  60s) gets "⚠️ couldn't confirm that this conversation's cards stopped; if the agent filed a card for it, it may still be working", never a success. On an armed install the card notice ("stopping a card
  from chat isn't supported yet") is used only where no control is sent: a conversation whose
  tasks go to a session worker (the session route, or after `delegate`). Past the timeout the gateway also cancels the
  control. Each control's end is one log line, `card stop ended`, with the terminal's
  `state`, its `reason` token and an `outcome`: `answered`, `no-result` (completed with no
  part), `not-stopped` (rejected, failed or canceled; `rejected` with `no-text-parts` is a
  bridge older than the control), `timed-out`, `expired` (started before a gateway restart)
  or `not-started` (never reached the bus).

  The doors differ. The inject door's cancel route reports the control as a stop that
  reached the bus (`cancelPublished`). On the A2A door the placeholder could never be
  edited (its Edit reaches only a task's line) and the answer comes after the turn with no
  task to carry it, so a `stop` there answers "🛑 sent a stop to this conversation's cards;
  this door doesn't report how it went". The console renders answers off TASKS, so the
  page may show the bridge's own text for the control beside the gateway's reply. The control is never the conversation's active task
  and never in its task history: the next message is an ordinary turn, and a status ask
  does not report on it. It is not sent on the session route, to a `delegate` session, for
  a top-level DM `stop`, or for a program's cancel that names a task.

- **Delegate.** A prefix, not a phrase, and recognized only when no live task
  serializes the conversation; its own section below.

Two routes exist, and the terms recur below: a conversation is **fixed-routed** when
its tasks address the standing executor configured at deploy time (the platform front
door), and **session-routed** when they address the conversation's own spawned worker.
The two differ on steers: a session worker absorbs them at its next turn boundary,
while the standing front door queues them and answers each as a further turn after the
current one, with a status notice per follow-up (the payload spec's steering rule).

The status matcher is the exact phrase set wherever the executor runs follow-ups: a
stolen steer there is a lost correction, and a status-shaped steer is a question the
agent can answer itself. A wider interrogative rule (status-shaped words in an
interrogative frame) applies only to a running task whose addressee refuses follow-ups,
the platform executor's `cli` executor, which refuses each one `no-resume`: there a
stolen false positive costs nothing, and a missed one is an acknowledgement followed by
a refusal. The gateway cannot see the executor, so it learns this per addressee from
the first `no-resume` refusal it relays, and forgets it when a later notice from that
addressee says a follow-up was queued; the first follow-up to such an addressee after
the gateway starts is still acknowledged and then refused. A detached task gets the
exact phrases on either route: after a stop, the wide reading of "any update on the
rollout" would steal a new task to replay a dead one. Anything no interceptor claims
during a `working` task is a steer, per the 8/24 decision above.

**Gateway-authored posts (amended 8/31).** Step 4's relay - events in, chat out - is
not the whole output story: the gateway authors a small set of posts of its own. The
placeholder that opens a task ("submitted…", which becomes the rolling line the relay
edits; none on a backend that shows a task's status itself, `StatusShower`, which is Slack with
Agent Sessions, where a submission that never reached the bus is posted instead), the status card, the steer acknowledgement, the release line for a task that
produced no first event inside the grace (its id and the grace; Session lifecycle
below), the notice the reap scan posts once when that grace passes with no turn to
carry the release line, and failure notices (a submission or steer that never reached the bus). All
are deterministic templates over facts the
gateway itself owns - its own publishes, its own registry, stream replay - which is
what keeps them inside the no-model rule. They also say only what the gateway knows:
the steer acknowledgement reports that the steer is on the stream, and what it says
next is conditioned on the route, because the gateway knows the route and the two
executors do different things: on a
session-routed conversation, that the worker picks it up at its next turn boundary
if the task is still running; on a fixed-routed one, "got it, I'll take that next".
A task with nothing on its stream
yet gets neither, on either route: both assume an executor holds the task, and none
has shown it does (a session pod may still be starting, or nothing took the task), so
the line promises no reply and, when the task has a submission time to measure from,
says when the conversation frees up. The executor's notice on the stream corrects it when the follow-up was not taken
(the task's follow-up limit reached, which the platform executor counts per task, the
task already ending, or an executor that cannot continue a session: the platform executor
runs follow-ups on its `api` executor only, and its `cli` executor refuses each one
`no-resume` as it arrives), and the gateway posts that refusal at once. At the task's
terminal the gateway says, with a count, which follow-ups the executor never answered or never
ran. The never-answered line posts only once the gateway has heard a steer notice from that
addressee since it started: an executor that predates the notices answers none, and every
follow-up would read as missed. Those counts are the relay's cache. A gateway restart in
between forgets the follow-ups it sent and the notices before it, so that terminal never
posts the never-answered line, and its not-run line counts only the notices that arrived
after the restart. Each earlier turn's answer posts as it completes; the result is the
last turn's answer and the only deliverable a program behind a door receives. A steer into a delegated
child is checked against the target's list first (rule `delegation.child-steer`);
refused, it is not published, its author is not recorded, and the room is told the
target is not reachable from here.

**The busy notice.** The gateway tells a person when their turn is going to wait, on the
turn's own status line. Once a fixed-route turn's task is on the bus, the gateway counts the
fixed addressee's other outstanding work: the session-state records whose active task was
published to it, the turn's own task left out. The count is a read of the bucket (one KV
watch over the session records), not an in-memory counter, so it is the same after a
gateway restart as before it, and it falls as terminals arrive, because a terminal deletes
the active task from its record. A terminal published while the gateway was down still
lowers it: the relay's durable delivers the terminal once the gateway is back. Past the
first-event grace a task is read from the stream and left out if nothing is on it, by the
same test the never-started release below uses, or if its newest event is a terminal (a
record whose clear was lost, which the heal would release on that conversation's next
turn), so neither holds the number up. A detached task is counted while it is still its
record's active task; a new turn in that conversation replaces it. At or above
`A2A_BUSY_NOTICE_AT` the gateway edits the turn's "⏳ submitted…" placeholder, the message
the relay's rolling-line edits target, to a queued state: "⏳ **queued** — 3 requests are
ahead of yours; I'll start on it as soon as there's room" ("1 request is" for one). It posts
nothing of its own, except on a backend that keeps no rolling line (`StatusShower`, Slack with
Agent Sessions), where the notice is a post of its own that no later edit replaces. The relay's first edit past submitted (working, or the terminal)
replaces the queued text as it would the placeholder, so the notice is never left under a
"completed" header, where a separate post read as the output of some earlier command; the
executor's own submitted event (the bridge publishes one on accept, before it has a worker)
does not replace it. The line never moves backwards: the edit is made only while the task's
line is still in its submitted state, and is skipped otherwise. It is made under the
conversation's session lock, the same lock every relay batch renders under, which on the
inbox path keeps the relay out from the placeholder post to the edit, so there the line is
always still submitted; the check is for any path that loses that ordering. A turn whose placeholder post failed has no line to edit and gets the
notice as a post. Nothing is refused, held back, or dropped; the notice is information, and
a count that fails sends none. The operator renders the threshold as the bridge's worker
count, 10 by default, so the turn told it waits is the first one that finds every worker
taken; the operator's own `A2A_BUSY_NOTICE_AT` overrides it. It goes to every chat backend
(Google Chat, Slack and Discord all edit the line in place) and the console. The inject and
A2A doors get none, because their callers read the status line as data: the A2A door takes
the line's first edit as the task going to `working`, so a queued edit would report a task
no worker has as running, and the inject door hands every edit to the eval harness. The
fallback post would be worse again, since both read every unedited post of a task as its
output.

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
  and sweep takes a Running pod only when it is a reused pod its record no longer names,
  so an untracked Running one-task pod would hold its bus credential with nothing able
  to reclaim it. The active-task guard above bounds
  which pods this can reach: a delegation is only recognized when no task serializes
  the conversation, so the pod is either idle or running a DETACHED task - and
  detached is not closed, so the deletion rule in Session lifecycle applies and that
  task's terminal is published first. The state is that rule's to name, not this
  section's. With that done, the delete is what reap or sweep would have done anyway.

## Sessions by default (added 9/25)

The routing judgment lives in the session, not the gateway. "The gateway holds no model"
above says where the demo gateway's judgment went: into the session pods. For a while the
plan carried one narrow exception, a model call the gateway itself would make to pick a
destination for free text. That exception is withdrawn (2026-09-25). A model in the routing
path does not scale well and produces results a user cannot predict, and the gateway is the
one component every human message passes through. The gateway stays deterministic code.

What does the free-text work instead is the conversation's own session: a pod per
conversation (thread or DM), as "What a session is" defines it, running the session worker
(the `chat` profile on the worker image) with skills, built as part of this, that let it know
the system - discover agents, clusters and topics; answer from standing state; delegate to the
platform agent or to a profile and receive what comes back. The gateway relays into the chat as it does for
any task; the session posts nothing itself. It is the same machinery as the session route
and the Delegate flow above, made the default rather than an affordance. To its user a
session-routed conversation is a persistent agent session tied to the chat, as durable as
the conversation and resumed by a fresh incarnation when one is reaped.

The resolution order does not change ([architecture 02](../architecture/02-agent-personas.md),
"Chat entrypoints"): a slash command first, an explicit handle second, and the session for free
text on a thread nothing has bound. Thread affinity stands as the
architecture docs state it: a thread a slash command or a handle routed to an agent keeps
its unaddressed follow-ups on that agent until re-addressed, and inside a session, affinity
to whatever the session last delegated to is the session's own to keep. The gateway spends
no inference in any of the three. The third hands the turn to the session, and the session's
judgment, like all model output, is never an authorization signal: the gateway's own sender
verification runs before dispatch exactly as before, and the `authority` block is stamped at
ingress as before (advisory today; see "Requester identity on the bus").

The gateway owns a second slash command, `/feedback` (added 10/19). Bare, it answers
deterministically with the two places a kube-agents report goes, the issue tracker and the
public feedback form, and starts no task; with text after it, the gateway passes the message
through as typed, and the Planning Agent composes the report and asks for a yes before the
platform worker files it through the credential broker. The command, the persona flow and the
broker verb are designed in [`chat-feedback-command.md`](chat-feedback-command.md). `/feedback summary` (added 2026-10-21) is the one text form the gateway answers itself: the install's identifier-free usage and feedback summary, from the `PlatformAgent` resource and the gateway's task records, with no task started (part 7 of the same document).

Delegation on a human's behalf keeps the human's bound, and the gateway is where it is kept.
A session does not mint a child task itself: it asks the gateway to, over the same local
channel its turns arrive on, naming the addressee and the task text. The gateway is the only
party that holds what the check needs - the requester the turn came in with and the install
salt that pseudonymizes it (the bus and the session KV carry only the pseudonym, and the
gateway hashes the allowlist the same way to compare), the target agent's `AllowedUsers`, and
the `.in` credential - so it enforces the target's allowlist against that requester, mints the
child with the `authority` block stamped as at ingress, and relays the child's events into the conversation as it does
for any task. This is the Delegate flow above with the session, rather than a "delegate:"
prefix, as the one asking; the machinery is the same. The session's routing judgment never
becomes an authorization decision, because the session never writes an `authority` block and
never holds a credential to publish on anyone's `in`: a session cannot reach an agent for a
person who could not have addressed that agent directly, and the worst-case bound the
architecture docs state for a mis-route (an agent the human is already allowed to reach)
holds for a delegation too. The audit record for a session-routed turn names the mode and the
session at the gateway; the child task's envelope names the target, the session that asked,
and the same requester.

**The request is the reserved `delegate` artifact** on the session's own task, one `data` part
shaped as `lib.DelegateRequest` (`{"addressee": "platform", "text": "..."}`), its text bounded at
`lib.DelegateTextCap` (16 KiB) - the same bound the wake's result is held to in the other
direction. The worker adapter serves it to the harness as one MCP tool, `mcp__a2a__delegate`,
over a stdio server the harness is started with via `--mcp-config` (every harness argv, with the
tool or without it, also carries `--strict-mcp-config` and `--setting-sources user`, so no turn
loads MCP servers or project settings from the scratch directory, which an earlier turn in a
reused pod could have written; a reused pod also removes the harness's user settings file before
each turn); calling it is what ends the
turn - the turn's own result is the one line `delegated to platform` - before the gateway has
even seen the request. `A2A_DELEGATE_TOOL=off` is the tool's own switch, read wherever the tool
is advertised or served, and it governs the tool even over an explicit `A2A_ALLOWED_TOOLS`
override, so an operator overriding the harness's tool list does not have to re-name it to keep
it off; `A2A_DELEGATE_SOCKET` names the unix socket the tool and the adapter agree on (a path in
the pod's scratch volume by default). A call reaching the adapter after the turn has already
ended - the deadline hit, the adapter received a `stop`'s cancel, or an earlier call this turn
already delegated - is refused there, inside the same turn, and never reaches the gateway. The
adapter knows of a `stop` only once its cancel arrives, so a call that raced the cancel, or an
artifact already on the stream when the gateway handles the `stop`, still reaches the gateway; the
gateway's own check below ignores it.

**The gateway acts on it only from the task it started**, addressed to the record's current
incarnation and still that incarnation's active task, and only while that task has not been
stopped; a straggler from a retired incarnation, an event after the active task has moved on, or a
request from a turn the human stopped (whichever of the two the gateway handled first) is ignored
and logged under one rule (`delegation.stale`),
never relayed - a different rule from a part that fails to parse, is blank, or is over the text cap,
which is ignored and logged separately (`delegation.malformed`). Only `platform` can be the
addressee today.
Depth is bounded by `A2A_DELEGATION_DEPTH_MAX` (default 3; a child's depth is its parent's plus
one, and a wake inherits its child's) - `FromEnv` refuses a configured value under 1 at boot
rather than read it as "off", which is the worker's own `A2A_DELEGATE_TOOL` switch, a different
knob. An absent `AllowedUsers`, or one that renders as `[""]`, admits every authenticated user, the
same as today's ingress door; a list present but blank once its entries are trimmed and split
admits nobody, the rule #2207 already holds for the Chat ingress list. The A2A door (backend
`a2a`) is the exception that fails closed: its principal map admits programs, not people an
operator listed, so a delegation checked against anyone who came in through it - requester,
steer author or incarnation-set member - needs a list for that backend, and no list is nobody
(`delegation.door-unlisted`). Nothing renders one yet (#2478 is the CR field), so a door turn
cannot delegate today. The inject door is deliberately not held to that rule: it exists only where
an operator arms it on a dev or eval install, its principal map already names who may use it, and
live runs and evals delegate through it. The target's `AllowedUsers`
is checked three times before a mint: against the turn's own
requester, stored hashed as it came in (the earlier ground work); against every human who steered
the turn, each stored hashed the same way and capped at eight - past the cap the delegation
refuses rather than silently drop an author; and against every hashed author whose text has
reached the session's current incarnation, requesters and steerers of every turn alike, capped at
sixteen and cleared with the requester copy at `A2A_ASK_TTL` - a cleared or overflowed set refuses
rather than admits. That third check is load-bearing: one incarnation serves every turn while
its pod is live (Session lifecycle, "Stream") and keeps what each turn told it, and an incarnation
started cold reads the transcript primer (below, "Rehydrate"), which carries every earlier turn
the stream still holds. So when the pod is started, the requesters and steer authors of every turn
the primer replays join the set, and each later turn the live pod takes adds its own. The set
grows for the pod's life; one that overflows or reaches `A2A_ASK_TTL` refuses delegation until
the next cold start, which fails closed. A turn whose requester is no longer on
record (cleared at `A2A_ASK_TTL`, with its text) is left out of the primer whole, so the pod never
reads text whose authors the check can't count. The bound is the conversation as the primer
carries it, not one incarnation. What else a session carries across incarnations is
#2370. One live child per conversation, a detached-but-still-running one counting as live:
the delegating turn's own repeat is ignored and logged, while a later turn's request reaching the
gateway while that child still runs is refused with a notice naming the running task
(`delegation.busy`) - a human asking again deserves an answer, the turn that already got one does
not. Every refusal names the target only, held until the delegating turn's own answer has posted
so the room reads "delegated to platform" first and the refusal after it; the audit line carries
the rule, the backend, and the hashed subject that failed it. A steer into the running child is
checked the same way before it is recorded or published (`delegation.child-steer`, or
`delegation.door-unlisted` for a door the target does not list): the child runs on `platform`'s
executor, which acts on a steer as a further turn (on its `api` executor), so an author the target's list refuses is
refused here, with the same target-only notice and an audit line, and nothing reaches the child.

**The child** carries the platform agent's own `in` subject, the parent's `correlationId`, and the
parent's stored `authority` with fresh grants and `via: {taskId, session}` naming the turn and
incarnation that asked. The conversation's rolling line for it, on a backend that keeps one (not
Slack, a `StatusShower`), is the same "working on" line any task gets, suffixed
`(delegated to platform)`; the parent's own line is left in place on the
parent's history entry and closes on the parent's own terminal, not the child's. From the mint on,
the child is the conversation's active task: a follow-up steers it, `stop` cancels it, a status ask
reports it. A heal releases the child from the conversation's active-task slot, and retires its
route the same way a terminal would, in its two ordinary cases only: the child's terminal is on the
stream, undelivered by the relay, or the child produced nothing within `A2A_FIRST_EVENT_GRACE`. A
child still running is left active. A known limit: a child that hangs without ending blocks
further delegation in its conversation until its terminal arrives (from its executor or the
supervisor) or the record is reaped.

One exception stands while its flag is on: the session pod's temporary read-only cluster view
(`spec-mode-switch.md`, "Switches inside `next`") lets a session read clusters through the
credential broker directly, with the platform agent's scope and no child task, so the only gate
between the requester and that read is the gateway's ingress allowlist, not the target's
`AllowedUsers`. The view retires on the default flip (#2371); until then, on an install whose
operator has turned the flag on and whose CR narrows the platform agent's allowlist, a person the
gateway refuses a delegation to `platform` can still read its clusters through a session's view.

Delegation ends the turn. A session's turn is a conversation turn, bounded at thirty minutes
by the `chat` profile's own deadline, and the executors it delegates to run to two hours; the
two do not nest. When the session delegates, its turn completes with a reply that says so
("delegated to `platform`, task `t_…`"), the gateway relays the child's events into the
thread as it does for any task, and the child's terminal wakes the session for one more turn
with the child's result as its input, so the session can synthesize it (the wake can't delegate
again; below). A follow-up
the human sends while the child runs steers the child, through the gateway, as follow-ups do
today: the platform executor queues each one and answers it after the current turn (on its
`api` executor; the `cli` executor refuses it `no-resume`), and
the gateway checks the steer's author against the target's list first, as it checked the
delegation's. A session pod
is therefore busy for seconds per turn, not for the life of the work it delegated, which is also
what keeps the per-conversation pod cost small. The delegating turn's answer is decided at the
call, so an eviction that lands before its harness has exited (a fast child's wake that could not
go to this pod retires it) completes the turn with that answer rather than failing it
`worker-evicted`.

**The wake** is one new turn on the delegating session, `via.session` naming the parent's own
incarnation, started on the child's `completed`, `failed` or `rejected` terminal - a `canceled`
the gateway published itself (a human's `stop`) does not wake the session whatever terminal
follows it, a `canceled` nobody asked for (a supervisor tearing down a dead executor) counts as
`failed`. It does not run if the delegating turn's requester has aged off the record (a notice
says so instead) or if another task already holds the conversation (waking would put a task
behind that one); otherwise the wake goes where a human turn would (Session lifecycle): to the
session's live pod, the incarnation that delegated, when it can take it, and otherwise to an
ordinary session spawn that counts against `A2A_MAX_SESSIONS` like any other - refused at the
cap, the child's result still stands as already relayed, and the room gets the standard cap
notice rather than a second one. The wake inherits the child's chain depth, so depth counts
delegations, not turns. A wake that spawns gets a fresh incarnation that reads the transcript
primer like any other; one in the live pod resumes the harness session that delegated. Either way
its text also opens with what the human asked, so the wake is self-contained: a worker image that doesn't read the primer, during a
mixed rollout, still has the request and the result. The cost is reading them twice. It opens
with the label `You were asked:` and the human's message that started the chain, fenced. It is the root turn's
message, carried down a longer chain, never an intermediate wake's gateway-authored text. Then
`You delegated to platform (task …), which completed.` (or failed, or was rejected), then the
result, capped at `lib.DelegateTextCap` and fenced under the label
`Result from platform (not from the user):`, so the model reads the child's output as data, never
as a new instruction from the user; a backtick run in either fenced body that could close its fence
early is broken before fencing. A child that ran follow-up turns wakes with every turn's answer in
order, the first being the delegated request's, then the result, each follow-up's answer under
`(follow-up N answer)`; one that answered turns and then failed or was rejected wakes with those
answers, then its reason under `(then it failed)` (or `was rejected`). Over the cap the earlier
answers are cut first, so the newest answer or the reason arrives whole unless it alone is over
the cap. The request is stored on the turn's history entry capped at 1 KiB
(each of up to fifty entries carries one, and 16 KiB each would put a full record near the KV's
message ceiling). It is user content at rest under the same posture as the active task's ask copy,
cleared with the requester copy at `A2A_ASK_TTL`. An entry with none, written before the field,
wakes with the outcome header alone. A heal that finds the child's terminal already on the stream, undelivered by
the relay, wakes the session exactly as the relay would have, with the same guards, cap and fence; a
child that never started has no terminal and does not wake. After a heal wakes the session this
way, the message that triggered the heal is routed against the wake: a follow-up (not a status ask
or a stop) steers it, the way any follow-up steers a running turn, and a status ask is answered by
replay. A heal that finds the delegating turn itself final on the stream, its delegate request and
its own terminal not yet relayed, leaves both to the relay for `A2A_FIRST_EVENT_GRACE` after the
stream stored that terminal (the server's timestamp, never the session's own envelope time), the
same window the never-started heal waits; past it the relay's delivery is taken as
lost, and the heal runs the request itself, every check applying, then relays the turn's terminal,
so the chain is minted or refused as the relay would have done it. The relay does the same at the
turn's terminal when the request's own event was lost before it ran (acked, then a gateway crash):
a session turn's `completed` with neither a child nor a refusal on record runs the request read
off the stream first. Both outcomes are written to the record when they are made - the child's
link in the mint's own write, the refusal in its own - so neither path runs a request twice. The wake's text then closes with a guide chosen by where the chain's answer goes. In a chat the
child's result is already posted, so it tells the session to reply with what it means rather than
repeat it; to a program behind a door, which sees only the wake's reply, it asks for the whole
answer. Either way it says to state plainly when the child failed or reports more work still
running. When the child handed its work to kanban cards, a line after the header says a Cluster Agent card's report posts into the conversation on its own (#3258). Two signals count: the terminal's chat mark with `ask` set and no `settle` (Slack with `KAGE_SLACK_UX` on), and the `handoff` artifact (spec-a2a-payloads), which the bridge's API executor publishes on any recorded route, Google Chat and Slack with the flag off included. The wake has no delegate tool: a pod spawned for a wake that serves one task gets
`A2A_DELEGATE_TOOL=off` from the spawner, and a pod that serves every turn has the tool turned
off by its adapter for each task whose authority block carries `via`, which only a wake's does
and only the gateway can publish. A wake that could delegate read an interim answer such as "still
checking, the results will post here" as a reason to ask again, and each ask woke another pod
until the depth bound refused, with nothing answered. A message the person sends while the wake
runs steers the wake like any other running turn, so it is answered without the tool as well. A
question sent after the wake answers is a new human turn and has the tool.

**To a program behind a door, the chain is one task.** A caller through the A2A door (or the
inject door) submitted one task, so the gateway tells the adapter's task observers about the chain
under that task's id, its root (the human or door turn the chain started from, carried on every
entry of the chain), and never names the child or the wake: no start, accept, deliverable or
terminal under either id. The turn that delegated ends quietly, and the root's one deliverable and
terminal come at the chain's end - the wake's result, then the wake's terminal, in that order; when
no wake runs, the root ends `canceled` for a requester's `stop` and `failed` with
`reason: wake-not-started - …` otherwise, with nothing delivered. The hand-off line
(`delegated to <addressee>`) is never a deliverable: a session turn whose stream carries a delegate
request and which minted no child ends the root `failed`, with nothing delivered -
`reason: delegation-refused - <the room's notice>` when the gateway refused it, and
`reason: delegation-not-started - <why>` otherwise (the child could not reach the bus, the turn was
stopped, the request was malformed, or its outcome was lost or could not be read from the stream).
The reason is kept on the turn's history entry when it is known, and the relay, the heal and the
read route decide the end by the one rule, so all three report the same. A turn with no delegate
request on its stream ends as its own terminal says. A heal composes with it: a healed
child's result is not the root's deliverable, the heal's wake delivers as the relay's does. A
cancel naming the root while the chain runs stops the running task, and the read route reads the
root as the chain's running task, never final from a chain task's own terminal. Once the chain has
settled it reports the end of the chain's last task as the root's: the wake's terminal and result,
or, when no wake followed, the end the observers were told. Chat backends read the posts, which are unchanged.

Transition. `platform` remains the default addressee until the session can hand platform
topics on to the platform agent without the user noticing. The route is a deploy-time
setting today (`A2A_DEFAULT_ADDRESSEE`), and the Delegate flow covers one task; the
per-conversation opt-in is `/session`: deterministic, resolved before status, stop and steer,
naming a route rather than a handle. A bare `/session` marks the conversation session-routed
and the next message opens the pod; `/session <text>` marks it and runs the text as the first
turn; `/session off` releases the incarnation (refused while a session task runs; stop it
first) and re-homes to the default addressee. The conversation's `contextId` is unchanged by
either. Without a spawner the on-forms answer with a note and change nothing, and the way back
still works; on an install whose default is already the session route the bare forms answer
with a note and `/session <text>` is `<text>`, the ordinary turn; `/session <text>` while a task is still running turns the route on and holds the text, unless the running task is the session's own, in which case the text steers it as a plain message would. On Slack a leading slash belongs to the Slack client, which
refuses a command it has not registered, so there the form is `@<bot> /session`: the mention is
stripped before the gateway reads the text, and in a channel thread the next message needs the
mention too: the adapter forwards an unmentioned reply only once a task has started there (or under the bot's own post), and
the ack says so. In a Slack DM the next message has to be in the same thread (the Slack
adapter's conversation keys), and the ack says that instead. The way back is answered even on an install whose spawner has since been disarmed, so a
record left session-routed by a rollback can always re-home. It is a debugging and opt-in door for the transition,
not the taught interface. The default flips when the delegation primitive lands: the session's request to
the gateway to mint a child task to a named addressee, the gateway's allowlist check and
mint, the relay of the child's events into the conversation, and the wake-up turn on the
child's terminal. The session's bus grants do not change for it (its only subscribe grant is
its inbox, and it publishes only on its own task); what is new is the request shape between
session and gateway and the gateway's record of which conversation a child belongs to. The
request is the reserved `delegate` artifact on the session's own task; the gateway's record is the
task history entry, which carries the turn's requester (backend and pseudonymized author id,
in the KV only) and its pseudonymized attribution, so a child minted later and the wake-up after
it run under the turn that asked. Working
state richer than the transcript primer rehydrate builds today - what the session has
discovered and decided, keyed by `contextId` - is part of the same work, so a resumed
conversation is a resumed agent. A warm pool of ready pods is deferred; the cold start is
paid.

What this costs is pods: one per live conversation, bounded by the session cap and the
idle TTL. That is the figure to measure before the default flips.

## Session lifecycle

The session manager is the demo's chatops code generalized from one-shot workers to
long-lived sessions. Four operations:

**Spawn.** First message in a conversation creates the pod: the demo's reference worker
shape (no ambient k8s credentials, scratch on emptyDir, 250m/512Mi requests; egress
fenced (8/31) to DNS, the bus, and LiteLLM, except, under the operator's
`A2A_SESSION_CLUSTER_VIEW` flag, the credential broker (`spec-mode-switch.md`) - the
deployment spec owns the policy),
running the headless harness behind a thin shim that bridges bus envelopes to the CLI's
stream-json stdin/stdout. Model auth, as shipped (amended 8/31): the worker talks to
the install's own LiteLLM, in-namespace, with no _cloud_ credential at all - the spawned
pod has no Workload Identity. **Amended 9/8:** it does now carry a ServiceAccount and a
bus credential of its own. The static `worker` password is gone from the pod entirely; in
its place is a projected ServiceAccount token, audience-bound to the bus and bound by the
kubelet to this pod, which the auth callout resolves into grants derived from the attested
pod name - this incarnation's events (every task it serves), this session's three consumers, this session's inbox. The
harness can still read that credential, because it runs at the same UID in the same pod;
what changed is that reading it buys the authority the harness already had (gke-labs#1270).
The deployment spec owns the reasoning. Direct Vertex via WI
stays the target, and arming it is a policy change as well as an IAM one: the session
egress fence encodes the shipped path (no 443, no metadata route), which is where a
piecemeal flip fails loudly instead of silently widening. Cold start is 5-10s; the
adapter posts a placeholder to the conversation while the pod comes up, which the demo
already does. **Amended 10/9 (#2825):** the pod is the conversation's, not the turn's. It is
spawned with `A2A_SESSION_REUSE=true` and the annotation `a2a.kubeagents.dev/session-reuse`,
and the conversation's later turns go to it (Stream, below). The gateway spawns again only when
no pod can take the turn: none is live (the first turn, after a reap or an eviction, after the pod
died), the pod was spawned without the flag (an older gateway's, which exits after one task, or a
one-shot Delegate's off the session route), a stopped task is still finishing on it, its last task
never started, its lifetime has no room left for a whole task, or the read that checks all this
fails. The old pod is retired as before. `A2A_SESSION_REUSE=false` on the gateway turns reuse off,
for running pods too, and every turn spawns.

**Stream.** The shim consumes envelopes addressed to its session, feeds them to the
harness, and maps the stream-json output to `status-update` and `artifact-update` events.
The gateway relays events to the conversation. The gateway never parses harness output;
that translation lives in the shim, next to the process it translates for.

A later turn is a new task on the same session. The gateway keeps the bus session name
and publishes the submission on `a2a.tasks.<session>.<task>.in` once a pod `get` says the pod is
running, its worker has not exited, it is not being deleted, it carries the reuse annotation, and
its lifetime has room for a whole task; a failed read spawns fresh instead. The task's capability
is minted as every task's is, with the session name as its delegate, and the session's author set
carries on, because the incarnation has not changed. Between tasks the shim waits on one consumer
filtered `a2a.tasks.<session>.*.in`: the origin consumer's name with the filter the callout
already grants it, so a later turn asks the bus for nothing the first did not. It takes the first
submission on a task id it has not run and passes over everything else on those subjects (a late
steer or cancel for a task that has ended). Each task runs as the first did: the respawn check,
the capability check, its own consumer on its own `…in` subject for steering and stop, its own
deadline and its own terminal. The harness is started again for each task with
`--resume <session id>`, the id the previous run reported, so the model has the conversation
without a primer; the primer goes to the first harness run only. The delegate tool is decided per
task as well: off for a wake (see "The wake" above). SIGTERM while the shim is idle exits 0 with
nothing published; mid-task it is that task's eviction, as before. A harness run that reports no
session, or a task that ended without its terminal reaching the bus, ends the process instead of
waiting for the next task: the pod reaches a terminal phase, Sweep closes what is owed, and the
next turn starts cold with the primer.

**Reap.** Idle TTL since the last user message (30 minutes, config-backed). The record's
`lastUserMessage` is the clock: every verified turn moves it, and the answer to that turn does
not. Reaping is deleting the pod. Nothing is saved first, because
the stream already has everything - that's the whole point of the transcript of record.
The KV entry stays while active, holding the `contextId`. To bound bucket growth, an
idle session whose pod has been reaped and whose last activity is older than `A2A_SESSION_TTL`
(7 days by default, sitting well beyond the TASKS stream's 72-hour retention) has its
KV session record pruned by the reaper. Pruning deletes the session record from KV,
bounding bucket growth to a ~100-byte tombstone marker per conversation (under the
bucket's `--history=1` limit) rather than accumulating multi-KB session records, rosters,
and task histories. (Reclaiming markers completely requires `STREAM.PURGE`, which the gateway's
restricted data-plane permissions deliberately refuse and which can be handled by administrative
maintenance or bucket TTL in future work.) Pruning also drops any associated task routing even
if an active task was left uncompleted (e.g. an executor that died without a terminal or an
abandoned turn), since the retention horizon guarantees that stream retention and task deadlines
have long elapsed. Reap never deletes a pod out from under a
live task: an active task that has not detached (see Stop above) exempts the session
from the idle TTL. The exemption is safe because the pod's end has owners. The session
worker's adapter enforces a task deadline (30 minutes default, config-backed): at the
deadline it kills the harness process group and publishes the terminal event itself,
and a pod that dies wedged reaches a terminal phase where Sweep takes over. For a pod that
serves one task the spawner sets the pod-level `activeDeadlineSeconds` above that deadline (the
adapter's deadline plus a fixed grace for the image pull), so a wedged adapter also lands in
Sweep's domain instead of holding its bus credential indefinitely. A pod that serves its
conversation cannot have that bound, so its `activeDeadlineSeconds` is the session's maximum
lifetime (`A2A_SESSION_MAX_LIFETIME`, 4 hours by default; the gateway refuses to start below the task deadline plus
twice the grace), and the reap scan holds each of its tasks to the old bound instead: a running
task older than the task deadline plus the same grace (counted from its submission, or from the
pod's start when the pod started after it, so a first task's scheduling and image pull do not
come out of the grace) is closed by the gateway as its supervisor
(`failed`, on the `…supervisor` subject, unless a final is already on the stream) and the pod is
deleted. A pod without room left in its lifetime for a whole task is not handed one, so the
lifetime and that check never fire together. The pod's bus credential still lapses with its
connection's grant TTL (an hour less jitter) and is renewed by the client's reconnect, which
re-reads the pod-bound token. Two ends still have
no terminal to wait for, and the record carries an independent bound for each rather
than a justification that assumes a terminal that may not come. The `ask` copy is
cleared by the reap scan once it is `A2A_ASK_TTL` old (24 hours by default,
under the stream's retention, which is what the content posture below needs), and the
same pass clears each task history entry's requester and attribution at the same age,
so a child task can no longer be minted on behalf of a turn older than that. A task
with nothing on either of its event subjects - no pod, or a pod that never ran - is
released from the serialization at the conversation's next turn once it is older than the
first-event grace (`A2A_FIRST_EVENT_GRACE`, 10 minutes by default), with one line in the
conversation saying so. Both subjects, not just `…events`: the fold reads them together,
and assertion 9 exists because a task whose only event is its supervisor's terminal is not
empty. That release publishes no terminal: age alone is not
evidence, a first event that is merely late could still arrive, and no supervisor path
ever sees a task with no pod, so its submission ages out with the stream's retention -
named here rather than papered over. A human who waits rather than writes is told too: the reap scan
posts one line once the grace has passed with nothing on the stream, naming the task and
saying the next message starts a new task. It marks the active-task record before it
posts, so the line goes out at most once per task, across gateway restarts, and it
releases nothing; the release stays with the next turn, which reads the stream again
first. The line is for a placeholder somebody may still be watching, so it has a ceiling:
a task older than three graces (30 minutes by default) gets none, and neither does a
record the same reap pass deletes past `A2A_SESSION_TTL`. Without the ceiling the first
pass after a rollout, or after an outage longer than the grace, would post into every
conversation that wedged in the last week. Otherwise the terminal event this chain
guarantees is what deletes the active-task record (and the `ask` copy riding it). A
detached task is the exception on both counts: it does
not exempt the session, so reap may delete a pod whose harness is still working, and
the supervisor rule below is what keeps that from being a silent stop.

**The session cap** (`A2A_MAX_SESSIONS`) counts live pods, and with one pod per live
conversation it fills with conversations nobody is in right now. When it is full and a
conversation needs a pod, the gateway evicts the one idle longest - a live pod whose conversation
has no task at all, oldest last user message first - and posts one line in that conversation: its
session was paused to make room, and its next message starts fresh with the conversation so far.
Only with no idle pod is the turn refused, with the notice it always had. A conversation running
anything (a task, a stopped task finishing, a delegated child whose wake will need the pod) is
never evicted; each candidate is re-checked under its own conversation's lock, and a lock that is
held means busy. A reused pod that its conversation's record names neither as its pod nor as the
session of a running task (a retirement whose delete failed, or a turn whose last record write was
lost and whose task has since ended) never exits on its own, so it goes first, with no notice,
and Sweep deletes it anyway within a minute.

**One rule for every pod the gateway deletes itself** (stated once here because four
paths reach it - reap, Sweep, Delegate, and any future one): if the pod is running a
DETACHED task, the gateway publishes that task's terminal event before deleting, as
the supervisor of the sessions it spawns - on the task's `…supervisor` subject, which
only the gateway's grant reaches (the 9/9 split), never on its `…events`. The state is
`canceled`, not `failed`:
every task this rule reaches is detached, and detached means a `stop` already
published a cancel, so the gateway is finishing the cancel the requester asked for
rather than reporting an error. That keeps assertion 13's enumeration intact
(`canceled`, or the race-lost outcomes it lists) and keeps replay able to tell a
stopped task from one that broke. Deleting first strands the task
non-terminal for the whole retention window - the adapter's deadline dies with the
process, and a deleted pod never reaches the terminal phase Sweep watches for - which
would break both that assertion and the payload spec's every-task-has-a-supervisor
rule. Do not
reason from the config defaults here: the adapter's deadline runs from task start and
the idle TTL from the last user message, so which fires first is a property of two
independently tunable numbers, not a guarantee. **Amended for delegation:** a detached task is
not always this pod's own - a session that delegated and then had its own pod deleted (Delegate's
previous-incarnation rule, Reap, Sweep) left a child running on `platform`'s executor, not this
one, so this rule checks whose task it is before publishing anything; a detached task running
elsewhere is untouched here; the pod being deleted ends nothing for it, and that child still blocks
a new delegation in its conversation (above) until its own terminal, or a heal, retires it.

**Rehydrate.** The next message on a reaped conversation spawns a fresh pod. The
gateway replays the context's tasks from JetStream - `tasks/get`, which folds each
task's `…events` and `…supervisor` together - into a transcript primer, and hands it to
the new pod as its first input. The primer carries both sides of each earlier turn: what
the user asked (the copy session-state keeps until `AskTTL`) and each result, labelled by
who answered, and how a turn that didn't finish ended (failed, stopped or rejected, with the
executor's reason), each fenced so a user's text can't pass for an earlier answer. A turn past
`A2A_ASK_TTL` is left out. It keeps the
newest turns that fit 8 KiB and says when earlier ones were dropped. The pod gets it as a
downward-API file from its own annotation, and the worker puts it ahead of the new message,
framed as history rather than instructions. The annotation is readable by anyone who can get
pods in the install namespace, and the copy of the user's text on it isn't cleared at
`A2A_ASK_TTL` the way session-state's is; it goes when the pod is reaped. Earlier results were
already on the same annotation. Moving the primer to a per-pod Secret is tracked separately. Inside
a live pod the shim resumes the harness's own session from turn to turn (`--resume`, Stream above);
the session file goes with the pod, and correctness never depends on it - session files are cache,
the stream is the record. Task-stream retention bounds how far
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
stopped, which is the distinction the rule exists to keep. A pod that served several turns
names only its first task in its annotations (the gateway has no `patch` on pods), so Sweep also
reads the conversation's record and closes the task it holds as active when that task ran on this
pod: a pod that dies on turn N closes turn N.

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
  "grants": {
    "capability": { "key": "root.task-9f3c4b…", "revision": 412 }
  }
}
```

`grants` is `null` on a turn with no task behind it - a status ask, a refusal the gateway
answers itself. On a submission it carries the reference above and nothing else: not the
tier, not the scope. See the payload spec's Authority section for what a consumer may do
with it, which is resolve it, never read it.

**Identifiers in `authority` are pseudonymous (decided 8/24).** Principals, subjects,
and roster entries are HMAC-SHA256 with the install's salt before anything is written to
the bus - the same posture the shipped attribution path applies before writing session
metadata (`docs/designs/audit-logging-user-attribution.md`). The bus holds labelled
content at rest for the whole retention window, so it gets the same treatment as the
session KV. The plaintext join lives in the gateway's local ingress log, and the
gateway resolves plaintext at the boundaries that need it - `openDirect` now, the
lowest-common-denominator grant computation if it is ever built. That one is still
unbuilt (9/9): the authority work landed as the capability envelope, which answers
"may this task do this" per task rather than computing a grant set per principal, so
it does not reach this boundary.

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
- `grants` carries the attenuating capability's **reference** - a key in the `cap` bucket
  and the revision the write returned - and never the capability itself. Armed 9/9. It is
  null on any turn with no task behind it, and null on a submission is a refusal, not a
  pass.

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
- **Slack:** the immutable `user_id`, admitted by the allowed-users list. A mapping table
  we maintain from our own IdP joins it to an IdP identity when it names the member;
  otherwise the principal is the member id itself, `slack:<user_id>`, as Chat's is the
  email Google asserts. Never `profile.email` - whether that field is IdP-asserted or
  user-editable depends on workspace config we don't control.
- **Discord:** a checked-in test mapping table from Discord user id to a test principal.
  Test-only, by construction: a Discord identity never maps to a real cloud principal,
  full stop.

**What advisory means, stated plainly:** the gateway verifies the requester at ingress,
but nothing stops another bus client from publishing an envelope with an invented
`authority` block. **That is still true of `requester` and `audience`, permanently.**
They are the audit trail and consumers MUST NOT authorize on them, ever.

**Corrected 9/9: it did not become decision-grade "when `identity` arms."** `identity`
never arms; see above. And subject-derived publisher identity, which did land for the
task plane on 9/9, is not enough on its own either - it says which principal wrote the
bytes, while `authority` claims which human asked. An executor writing its own
`…events` subject is the legitimate writer of that subject and can still put any
`authority` block it likes in the envelope.

**Armed 9/9, and not by the rule that paragraph predicted.** The answer is not a rule
about who may originate the block - such a rule would have to be enforced by consumers
reading a field, which is the shape being refused. `grants` carries a reference to an
entry in a KV bucket the gateway is the only principal permitted to write, and a consumer
does not read the entry (no broker may read the store at all) or trust the block. It
hands the reference to the verifier, which authenticates the caller from the subject it
arrived on and answers whether a verb is permitted. So an executor is free to write any
`authority` block it likes, and it buys nothing: an invented reference names a key the
gateway never wrote, and a stolen one names a capability whose `delegate` is a different
principal. Both are refused, by the verifier and by the server's own subject permissions
rather than by an honour system.

The payload spec's Authority section states the consumer rule and its four conditions;
the mechanism is `docs/architecture/09-capability-envelope.md`. What survives from 0.3
unchanged: `authority` is populate-by-gateway-only and libraries pass it through
untouched.

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
it is the first real adapter, specified in its own section below. ~~Slack follows when a
customer asks, with the mapping table as a hard prerequisite.~~ **Update 9/4:** Slack did
not wait for a customer to ask - it was resequenced against the order stated here and
written ahead of gchat (which followed on 9/5), with the mapping table built as part of
the card rather than left as a later chore. Both adapters now have a section below, and
those sections are the design of record for each.

The adapter interface is what makes the pick cheap: inbound message with verified sender,
conversation and thread identity, roster read, post-to-conversation, `openDirect`. Five
operations, normalized. The console adapter is the third implementation and the smallest;
see its own section. If the Discord adapter leaks Discord-isms through that interface,
that's a bug in the interface, and better to learn it on the throwaway backend.

### The inject backend (added 9/17)

A backend beside Discord, Google Chat and Slack, and the one with no human on the other end:
an HTTP door into `handleInbound` for a program. It is the next-stack analogue of the door
the eval harness uses today, which posts to the agent's own `/v1/responses` and bypasses chat
entirely - a path identical under both modes, so a run through it says nothing about the stack
`mode: next` renders.

**Why it is a backend rather than a bus client.** The harness could publish a submission
straight to `a2a.tasks.platform.{taskId}.in`, and that proves the bus, the callout, the streams
and the executor. It leaves out the gateway: its routing, its session registry, the relay back,
and the session agent's routing once conversations are session-routed by default. It also needs a bus identity, and the only static
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
  the persona. The harness sends none of them as a prompt, and a case whose prompt opens with
  one is the case's defect; a stopped ask's `stop`, sent after the opening turn, is the one it
  sends itself. The reply carries the task id, so the caller can await a named task rather
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
  runs unobserved; and the unverified-sender notice is posted once per sender (never for an
  unaddressed message), so a second
  message from the same author posts nothing at all. The notice's own dedupe stays the gateway's.

- `GET /conversations/{key}` returns what the relay posted, as a sequence a caller pages through
  with `after`, plus `wait` to block for something new and `task` to name the task whose terminal
  the answer should carry. Those three are the whole of "await terminal of task id X". Replies
  come back the way the relay posts them - the placeholder, the rolling progress line's edits,
  the deliverable, the terminal - because what a verifier grades has to be what a customer would
  have read. With `probe=1` the same read is also the **read route**: a pure read of the
  conversation's session record and of the active task's stream (or, with `task=`, of a named task the record owns, its active one or one of the last fifty it ran; another conversation's task, or one older than that, is not read; the named
  task's stream whether or not the record still holds it as active - the relay clears the active
  task when it posts the terminal, and that is how a harness reads a finished run's trace after
  the release), which mutates nothing - no
  heal, no lock, no post, no publish, no write. It returns the id of the task it was asked about
  (the active one when none was named), with its `submittedAt`, age and `detached` flag when that
  task is the record's active one (a `task=`
  read of a task the record no longer holds as active carries none of the three), the latest executor state the stream shows (none,
  `submitted`, `working`, or a terminal, with `final`), whether `working` was ever on the stream
  (`reachedWorking`, read off the fold's history, because two events can land between a
  caller's reads and the latest state alone would hide the one that says a model ran) and, when
  the stream is terminal, the
  fold of it: whose word the terminal is (`terminalSource`, the executor's for a terminal on the
  task's events subject and the supervisor's for one on its supervisor subject - distinct from
  the gateway's own source, which says it could not publish the task at all), the result
  artifact's text and the terminal's status message; whenever the stream was read, final or
  not, the task's tool-call trace (`activity`, the data parts of the activity artifact in
  arrival order per artifact, which is stream order for an executor that appends to one artifact
  id, present as `[]` when the executor called nothing and absent when no stream was
  read, because the relay never posts that artifact and this is the harness's only view of it, newest 1000 entries when a run has more, with `activityDropped` counting the rest)
  and the progress artifact's latest line (`progress`); plus the conversation's A2A `contextId`
  from its record (absent when there is no record; the bridge's `api` executor names its Hermes
  session after it, which is how the harness finds the kanban cards a turn filed), the
  conversation's last post, the
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
  as active, from the task's history entry (its addressee, correlation id and capability),
  and refused for a task the conversation never held. The harness sends it after a read has
  classified the task,
  never before, and in every outcome that leaves an active task - `working` at the budget (a
  graded timeout), queued for the whole budget, never taken by any executor, and a read that
  could not classify - always naming the task. The classification is the read's and never the
  cancel's answer; the cancel bounds a stray run. It has to be named because for a task nobody
  took the cancel turn itself runs the never-started heal first, which releases the record
  while the submission is still on the in subject, and the bridge's durable consumer delivers
  from the start of the stream, so a bridge that binds later within retention would run the
  stale prompt. The bridge honours that cancel before it spawns: its worker replays the task's
  `in` subject and answers a cancel it finds there with `canceled-before-start` and no spawn,
  the terminal a task still queued behind the cap gets too; a cancel that lands after that read
  kills the run inside the kill grace with a `canceled-by-request` terminal. That look-ahead is
  the bridge's work, not the door's
  ([`a2a/docs/hermes-bridge.md`](../../a2a/docs/hermes-bridge.md), "Lifecycle, steering, cancel").

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

The gateway tells the adapter a task's ends through an optional `TaskObserver` interface that
Discord and Google Chat do not implement: a human reads the chat, so rendered text is their
whole interface, while a program must not have to parse `✅ **completed**` to know a task is
over. (The Slack adapter implements it for one reason of its own, given in its section: a task
starting or ending in a thread is what its session-thread cache keys on.) The
start and the accept are separate calls because the id and the submission are different facts,
as the refusal above turns on. The terminal says who declared it, which is the difference between
an executor that failed, a task that never reached the bus, the supervisor's word about an
executor that died, and one the never-started heal released past `A2A_FIRST_EVENT_GRACE` - four
states that look alike to a caller and mean the agent's fault, an outage, a dead executor, and an
install with no executor. It also carries the executor's reason verbatim - the
terminal status message the bridge and the worker adapter write as `reason: <token>[ - detail]` -
because a failed terminal is not always the persona's failure: the harness reads the token and
classifies the executors' own reasons (`bridge-shutdown`, `bridge-queue-overflow`,
`bus-publish-failed`, `spawn-failed`, `bridge-died-without-terminal-event`, `hermes-rate-limited`,
`hermes-api-unreachable`, `hermes-api-refused`, `session-busy`, `worker-evicted`, `bus-subscribe-failed`), a `rejected` terminal and a `canceled-before-start` as infrastructure,
and grades the persona's (`hermes-exited-nonzero`, `deadline-exceeded`, `hermes-api-failed`,
`hermes-api-unreadable`, `hermes-api-read-failed`, `hermes-api-oversize`, `hermes-unstructured-reasoning`) and any reason it does not
know; a `canceled` after the harness's own cancel is the graded timeout. An eval install's bridge
runs at least the harness's parallelism (`EVAL_TASK_PARALLELISM`) in workers - the operator
renders the bridge, so the setting is the operator's `A2A_BRIDGE_CONCURRENCY`, or
`BRIDGE_CONCURRENCY` on a bridge sidecar the install declares itself - because the bridge publishes `submitted` when it queues a task behind
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
   network**: two ingress rules, which admit the managed-Prometheus collector's namespace and the
   operator's pods to the metrics-only port and nothing to the door (see "Metrics" below). The gateway's own fence, which renders on every next gateway whether or not a door is armed, admits the same, so this copy is the door's record of the intent rather than the pod's only fence. It is a second control over the edge the bind already
   closes, kept so that a reader of the rendered objects sees the intent and so that a later
   change to the bind address does not open the pod network by itself. Neither it nor the bind
   governs the port-forward, which the kubelet serves from inside the pod's network namespace;
   a `hostNetwork` pod on the gateway's node is in the node's namespace, not the pod's, so the
   loopback bind is not reachable from it either. The token is what any caller that does reach
   the listener still has to hold.

**What it also settles.** The gateway refuses to start without a backend. It used to be
rendered regardless, so any install with neither a Discord token nor a Chat relay carried a
gateway Deployment that crash-looped forever and nothing could rollout-gate on. The operator
now asks first: a `mode: next` install with no chat backend - no `discord-bot` Secret in the
namespace, no door armed and neither `spec.integration.googleChat` nor `spec.integration.slack`
enabled - gets no gateway Deployment at all, its `Ready` counts the rest
of the stack (NATS, the auth callout, the provisioning Job's first completion, the sandbox, the
broker, today's gateway), and an `A2AGateway` condition (`status: False`, `Reason: NoChatBackend`) names what
would render it. A gateway that already exists when its last backend goes is not deleted,
because deleting it would take every session pod that hangs off its UID, and it is not left at
one replica either, because that replica exits on `no chat backend` and crash-loops. The
operator applies it at zero replicas, keeping the object and its UID, and its own NetworkPolicy
(see "Metrics" below), and writes the same `A2AGateway` condition; `Ready` does not wait on it. So the backend question is asked on every
pass: the door flags, Chat and Slack answer it without a read, and an install whose only backend is a
Secret pays one uncached read per pass. When a backend comes back, the next pass applies one
replica on the same Deployment, and the condition stays, as `WaitingForReplica`, until that
replica is ready. The dark state shares the reconcile's 30 s requeue, so a Secret, which is
not watched, is seen within one requeue; a door flag comes back with the operator restart that
changing it causes, and Chat or Slack with the CR edit. An eval install with this door armed has an ingress
the guard accepts, by the decision recorded above, and the render counts the door as a backend
for the same reason. An install that enables Google Chat or Slack under `next` has a backend
by that fact alone: the render asks the CR before it reads any Secret, and, because the gateway
refuses two real backends, renders one of them in this order - Chat, then Slack, then the
`discord-bot` reference - so a `discord-bot` Secret left in the namespace does not stop a Chat
or Slack gateway starting. With both integrations enabled, Chat holds the gateway and Slack
stays on the legacy consumer rather than reaching nobody. The Slack refs are rendered on the
gateway as required references, whatever the CR's own copy says, because the gateway refuses
half a pair at boot: a missing Secret or key holds the pod at container creation, named in its
events, instead of starting a pod that exits. Disabling Google Chat or Slack on an install whose
gateway has no other backend takes the same path as removing the Secret from under a Discord
gateway, or turning off the door an eval gateway started on: the existing gateway goes to zero
replicas with the condition, its session pods stay, and enabling Chat or Slack again, creating a
`discord-bot` Secret or arming a door brings it back on the same object. The gateway's
secret-env digest is stamped only on a pass that renders it with a backend, never on the zero
apply, so a dark gateway whose Secret is gone costs no read of it.

_Update 2026-10-22: superseded, every `mode: next` install runs a console-only gateway. The gateway
starts on the console door alone (`A2A_CONSOLE_DOOR=true`, which the operator renders on every next
gateway) when no chat backend is configured, so the operator no longer withholds it or scales it to
zero, and a backend taken away leaves the same gateway running on the console. `NoChatBackend` is no
longer written; an install upgraded from an operator that wrote it has its zero-replica gateway
scaled back to one, and the condition stays, as `WaitingForReplica`, until that replica is ready.
Where that operator never created the gateway, the condition clears on the first pass and Ready
waits on the new gateway.
The paragraphs above are the history._

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

**Ingress topology: the existing app registration, topic and subscription, consumed
through the credential proxy by whichever brain the mode names.** A Chat app configuration is
per-GCP-project, so "take Chat events directly" means a second project — not an
adapter-PR dependency. Two consumers on one subscription split deliveries randomly, so one consumer
holds it at a time (below), and under `next` the subscription is pulled by the A2A
`GoogleChatRelay` instance in the credential proxy (routes
`/v1/chat/a2a/events`, `/v1/chat/a2a/events/ack`, `/v1/chat/a2a/events/nack`), enabled
only when `A2A_GOOGLE_CHAT_SUBSCRIPTION_NAME` is set alongside the project id. The
gateway pod stays cloud-credential-free: it authenticates to the proxy the way the
legacy chat caller does — a projected ServiceAccount token verified by TokenReview —
but with its OWN audience — the one the operator renders as
`CREDENTIAL_PROXY_A2A_CHAT_AUDIENCE` on the proxy, `kubeagents-credential-proxy-a2a-chat` —
conferring
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
backends' ingress semantics closely enough: the Discord websocket redelivers nothing,
and Slack's Socket Mode, which does redeliver unacked envelopes, carries its own
in-adapter dedupe ring for exactly that (the Slack section below).

**Coexistence is by mode.** A topic fans out to every subscription and two consumers on one
subscription split its deliveries, so exactly one brain consumes an install's Chat, and
`spec.mode` chooses it: under `today` the Hermes `google_chat` platform pulls the
subscription through the broker's legacy relay instance; under `next` the operator arms the
A2A relay instance on the same subscription, gives the gateway the adapter's env and token,
and renders neither the Hermes platform nor its relay env. One subscription rather than a
second one for the A2A path, because the unacked backlog then follows the live consumer
across a flip in either direction, where a second subscription would hold up to seven days
of stale asks for the legacy consumer to drain on a rollback. The per-install choice of a
different pairing is the mode switch's per-component override, sketched and not built; the
predicate the operator consults (`a2aChatArmed`) is where it would be read. With the Hermes
platform off, the agent's proactive posts (alerts, cron findings, audit reports) go to the
gateway instead, over the chat.notify route below. And the handover is not instantaneous: the pass
that flips a Chat install to `next` turns the Hermes consumer off while the gateway may still
be held (the callout gate, a provisioning Job, an image pull), so until the gateway is up
nobody consumes the subscription - messages wait in it rather than being lost, and the CR
reads `Provisioning` naming the gateway, which is the signal. In the other direction, a
message Hermes pulled and had not acked before its pod rolled is redelivered to the A2A
relay after the subscription's ack deadline; if Hermes replied before acking, that one
message is answered by both brains, once.

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
space — the canonical example above. In a DM space, top-level messages share one conversation,
`gchat:dm/spaces/AAA`, answered top-level, and a message typed inside an existing thread is
a side thread, its own conversation `gchat:dm/spaces/AAA/threads/BBB`, answered in that
thread. This is the main-flow and side-thread rule of the Hermes Google Chat adapter that
`today` runs.
Chat attaches a thread to every DM message, the one it auto-created for a top-level message
included, so the thread name cannot tell them apart; Chat's `message.threadReply` flag does
(true only for a reply in a thread, per the Chat API `Message` resource; measured: the
captured in-thread DM reply carries it and the top-level DMs omit it). Hermes infers the
split from a persisted per-thread inbound count instead, and the gateway keeps no count.
The two differ on one case: the first reply into a thread Hermes has counted no inbound
message in, such as the thread under a bot-posted report, is main flow to Hermes
(`cron-report-relay.md`, "The first reply into a report's thread") and a side thread here. A record minted under the thread-less key before side threads existed still
parses and posts top-level, and `openDirect` returns that key, so an unsolicited post to a
DM lands top-level. A space whose threading state does not support replies (`UNTHREADED_MESSAGES`), or a
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
`A2A_CHAT_DISPLAY_MODE`; the operator feeds it from the same CR field, rendering `default` when
the field is unset; the gateway's own unset resolves to `debug` (the historical rendering, so
Discord installs are unchanged), and the render is what makes the two agree. The
split is the legacy field honoured in the new relay, not a new knob.

**openDirect.** `spaces.findDirectMessage` by user resource name, falling back to
`spaces.setup` when no DM space exists yet. The email alias in `users/{…}` is accepted
only under a user credential; the app credential the relay holds answers it 403
(measured), and `spaces.setup` under the app credential needs `chat.app.spaces.create`
plus admin approval, so the fallback is refused too. The adapter therefore resolves an
email to the immutable `users/{id}` it learned from that person's own event, which
`findDirectMessage` does accept; a person who has never spoken cannot be opened. Ships
as the primitive, unused, like the other backends.

**Proactive posts: the chat.notify route.** A message the agent raises with nobody asking
(an alert, a cron finding, an audit report) has no conversation to answer, and under `next`
the Hermes platform that used to post it is off. So the agent asks the gateway, which holds
the Chat credential, to post it. The agent container runs `a2a notify`, which publishes a
core NATS request on `chat.notify.gchat` carrying `{"text", "thread"?, "wait_ms"?, "conversation"?, "context_id"?, "blocks"?, "chat"?, "update"?, "reply_to"?}` (on Slack's subject, below, `chat.notify.slack`) with its reply subject
under `chat.notify.reply.agent.`; the gateway posts the text into the home space
(`googleChat.homeChannel`, carried as `A2A_GCHAT_HOME_CHANNEL`), as a new thread or as a reply
on a thread of that space, and answers with `message_id` (the field `hermes send --json`
prints) and `thread_id`, the thread it landed in, or an `error` naming why nothing was
posted. The answer goes out once the first part has landed; a long text is chunked, and
the rest follows into the same thread. A request the gateway took and did not answer in
time may still post, so `a2a notify` exits with its own status then, and the alert path
treats that as sent rather than sending again. `wait_ms` is how long the requester waits:
a request that fails or is refused after it has nobody to tell and is already recorded as
possibly posted, so the gateway logs it as lost, at error, with its first line. When the
route is not there (no gateway subscribed, or the bus unreachable) `a2a notify` exits with
a third status, nothing posted; the alert path, which has one shot, waits through about
half a minute of those before giving up. The agent-side callers
(`agents/platform/scripts/chat_notify.py`) switch on `A2A_NOTIFY_PLATFORM`, which the operator
renders exactly when `a2aChatArmed` holds and `homeChannel` is a space name, and send to every
other platform through `hermes send` as before. The kanban notifier's report back to a gateway
conversation (below), and onboarding's first report into the conversation the user first wrote in,
are armed by their own variable, `A2A_NOTIFY_CONVERSATIONS`, which the
operator renders whenever the gateway arms the route: `homeChannel` unset (the route then
serves conversation requests only) or a space name. The Hermes kanban notifier, which posts a card's events into the thread the card
subscribes to and wakes its creator when a card blocks or fails, reaches the same route
through a send-only stand-in adapter that exists only inside the notifier
(`deploy/docker/patches/kanban_chat_notify.py`); nothing else in the Hermes gateway treats
the platform as connected. Only the subscription's thread is forwarded: a thread of the home
space goes as `--thread`, under the home-space rule (a thread of another space is refused), and a
gateway conversation key (below) goes as `--conversation` with `--context`. A subscription with
no thread is not delivered. A fanned-out child card's answer folds into its parent's on this
path (`fold_fanout`): each child inherits the thread's subscription, and the parent completes
only after its children with the synthesis, so the child's `completed` is held while the parent
works and until the parent's own answer has been delivered on that thread, then dropped with its
cursor moved past it, and posted after all if the parent ends blocked, its subscription is gone,
or the hold passes two hours (then led "Interim result from <card title> …"). One that arrives after
the parent's answer has posted opens with "Late result from <card title>:". On Slack with
`KAGE_SLACK_UX` on, a child whose parent still works is not held: its `completed` delivers, and
`kanban_progress_lines` settles its plan row and posts nothing, as under `today`. Its other events (blocked, gave up, progress) post as before. A route probe (an empty notify, which an armed gateway refuses at once) tells the
notifier when the route is unavailable (the gateway restarting), and it holds deliveries
unclaimed then; only a send that meets the outage before the next probe spends one unit of
the subscription's failure budget. Once, when routed delivery first goes live on an install,
events that are already more than six hours old are advanced past without posting: they are
the backlog nothing could deliver before.

A request may name a conversation instead of the home channel: `conversation` (the gateway's
session-record key, such as `gchat:spaces/A/threads/B`, or `slack:C1/1712.0001`) and `context_id`, on either backend's
subject. This
is how a kanban card filed in a gateway conversation reports back to it. The hermes-bridge's
`api` executor records, before each turn, the conversation its Hermes session answers: the
platform, the conversation key from the task's `authority.audience.conversation`, and the
task's `contextId`, stored in the pod's session-kv (`PUT /v1/sessions/{id}/route`, loopback
only, accepted for `a2a-*` sessions alone). `kanban_event_routing` swaps that route in for the
`api_server` origin when the turn files a card, following Hermes's compression lineage
(`parent_session_id`, parent ended `compression`) when the session has since been renamed. The
kanban notifier then sends the card's report with `--conversation` and `--context`. The gateway
posts it, through its own adapter and into the conversation's thread, only when it holds a
session record for that key carrying that context id: a conversation the agent is working in.
The bound is the set of live records rather than a secret: the agent principal, the one sender,
can read every live conversation's context id (its Hermes sessions are named for them), so the
check refuses a conversation with no record, a made-up key and a stale route, not a post into
another conversation the agent holds a session for. No record, another
context, or a record that cannot be read gets one refusal whatever the reason; a malformed request (a key on another backend, a thread and a conversation
together, a conversation with no context id) is refused by name. Nothing is posted in either
case. The subjects and grants are the same as for a home post; a route the
bridge cannot record is said at the end of the turn's answer, since the card's report will not
arrive.

Slack has the same route on its own subject, `chat.notify.slack`. Home posts are armed when the
gateway holds Slack (`a2aSlackArmed`) and `slack.homeChannel`, carried as
`A2A_SLACK_HOME_CHANNEL`, is a channel id (`C...`, or `G...` for a private channel);
`A2A_NOTIFY_PLATFORM` is then `slack`. Conversation posts are armed whenever the gateway holds
Slack, `homeChannel` set or not, and `A2A_NOTIFY_CONVERSATIONS` is then `slack`
(`a2aSlackNotifyArmed`); a `homeChannel` that is set but is not a channel id turns the whole
route off, as on Chat. Chat holds the single-backend gateway when both are enabled, so at most
one platform is ever routed.

**The home-channel record.** Under `next`, Slack on the gateway or not, the operator owns a ConfigMap
`<agent>-home-channel` with no data, applied beside the gateway Role and torn down with the stack.
The gateway Role reaches that one name with `get` and `patch`. `spec.integration.slack.homeChannelPolicy`
(`auto`, also absent, or `manual`) is rendered to the gateway as `A2A_SLACK_HOME_POLICY`, with the
record's name as `A2A_HOME_CHANNEL_RECORD` and the gateway's own image as `A2A_SELF_IMAGE`. In this
release the agent's env and the `HomeChannelUnset` condition follow `homeChannel` alone. The change
that makes the operator render the agent's `SLACK_HOME_CHANNEL` and `A2A_NOTIFY_PLATFORM` from "the
CR, else the record" and the switch that arms the gateway's learn ship as one, so no release holds a
signal nothing answers. The gateway's half (`a2a/gateway/slack_home_channel.go`) sits behind that
switch, `A2A_HOME_CHANNEL_LEARN=true`, which only the operator change that admits the record renders;
that change merges after the gateway code, never before it. With the switch set, the policy `auto`
and `homeChannel` empty, the gateway reads the record at boot: a recorded channel is checked again
with `conversations.info` and adopted, with `reader` re-stamped to the gateway's own image and a stale
`slack.refused` cleared; one that is shared, archived, not joined, or not a channel id is written to
`slack.refused` as one of the four words the operator matches, `shared`, `archived`, `not-member` or
`malformed`, nothing is adopted and the learn is armed, so the first mention that passes replaces the
channel (one archived after it was learned is the ordinary case); one whose check cannot be made (a
Slack API error) is adopted in memory for that boot with no re-stamp, so `reader` never claims a check
that did not happen. An empty record arms the learn: the first verified, top-level channel mention that is not a command, whose
channel passes the same check, is written once (`slack`, `slack.setBy`, `slack.setAt`, `writer`,
`reader`), the gateway's notify route is armed on it, and one line in the learning thread names the
channel; a candidate the check refuses is written to `slack.refused` while the record names no
channel and does not name that reason already (a write that fails is tried again at the next
refusal; while a channel is recorded, `slack.refused` describes it, only the boot check writes it,
and a refused candidate writes nothing) and the next mention may learn; a record another writer
filled first is adopted after the same check, with `reader` re-stamped, or replaced when that check
refuses it; a group direct message is refused without a call and a direct message is never a
candidate. One hand-reachable
state exists: `kubectl set env` of that switch on the gateway Deployment survives the operator's
server-side apply, and the gateway then learns, writes and posts while the agent env stays unrouted
and `HomeChannelUnset` stays True. `reader` compares the image reference only: under a floating tag
such as `latest` the same tag can name a different image, which the key cannot tell. `patch` on the
object is the RBAC bound; data-only merge patches with the read `resourceVersion` as the precondition
are the gateway's contract.

A conversation request carries no `blocks`; its layout is the `chat` field instead ("Chat messages" under the Slack adapter). A Slack `thread` is `<channel>/<ts>`, admitted only when the channel is the home channel
and posted on the `ts`, or a bare `ts`, which names no channel, so the gateway checks it only for
shape and posts it into the home channel: a `ts` from elsewhere can only ever thread (or fail to
thread) there. The kanban notifier sends the qualified form, with the card's own channel, so a
card filed under `today` in a DM or another channel and delivered after the switch to `next` is
refused rather than reported into home; a Slack subscription with no channel is not sent. Slack
lays out a home-channel request's `chat` message as it lays out a conversation's (the fleet
audit's report card, `a2a notify --chat-file`): one that breaks a rule, or that Slack refuses,
is dropped and its `text` posted, and the request's `update` and `mark` are not read. Chat posts
the `text`. Slack also takes Block Kit: a request may carry `blocks`, a JSON array posted as one message with
`text` as its notification and fallback (`a2a notify --blocks-file`). A backend without blocks, Chat today, refuses such a request
rather than dropping them, and Slack's own refusal (`invalid_blocks`) comes back as the
`error`, so the caller falls back to text. Blocks cannot be escaped the way text is, so the
gateway refuses any block that carries a mention (`<!channel>`, `<!here>`, `<!subteam^…>`,
`<@U…>`, a bare `@here`, `@channel` or `@everyone`, which a mrkdwn text object may parse into one, or a rich_text `broadcast`, `user` or `usergroup` element), and the caller posts the
escaped text instead. The gateway answers clicks only on the choice buttons and rating controls
it draws itself (the Slack adapter section), so a button on such a card would be clicked into
nothing, or, as a rating block, rate under a task id the sender chose: the gateway refuses any
interactive block or element in raw blocks (an `actions` or `input` block, a button, a select, a
date or time picker, a text input, a `context_actions` block and its `feedback_buttons` and
`icon_button` elements). The blocks are decoded into slack-go's types and sent again,
so a block type it does not know travels as written and a field it does not model may not. A
home channel that is not a channel id leaves the route unarmed; the gateway logs
"chat.notify route not armed" at start.

The console has a route of its own, `chat.notify.console` (added 10/22), so a kanban card filed
from a console turn reports back into its conversation. It serves conversation requests only:
the console has no home channel, so a home post is refused. It runs whenever the gateway runs,
as the console does, with no variable arming it. Each request is one frame on
`chat.console.<token>.out` with `"kind": "notify"`, a minted `messageId` (`n-<boot>-<n>`), and
the whole `text`, not chunked; a `chat` message's text is its fallback, and the console renders
text. An `update` key edits the frame it posted, as it edits a chat message elsewhere. A
request may carry `reply_to`, the gateway task whose turn filed the card (`a2a notify
--reply-to`), which the frame passes on as `replyTo` so a console client can join the report to
its turn. A `reply_to` that is not a task id's shape is dropped and the report still posted.
The task reaches the card this way. The hermes-bridge records it on a console conversation's
route (`task_id`). `kanban_event_routing` snapshots it into the card's subscription when the
card is filed, as `console:<token>#<taskId>` (`#` cannot occur in a console token, which is one
DNS-1123 label). The kanban notifier splits it back off as `--reply-to`. Hermes has no console
platform, and its notifier reads a subscription's platform through its own `Platform` enum, so
the notify patch registers one in Hermes's platform registry. That registration is kanban-era
and goes when kanban does.

A notify is not a task. It mints no capability, starts no executor, opens no session and
carries no `authority` block; the requester rules above do not apply, because nobody
requested it. What bounds it is where it may land and who may send it. Where: the home space,
or a conversation the gateway holds a session record for carrying the request's context id; a
thread of another space, or a conversation without that record and context, is refused before
any post (on Slack, a home post lands in the home channel only, which the gateway sets on every
post). Who: the agent principal alone publishes `chat.notify.gchat`, `chat.notify.slack` and
`chat.notify.console`, and reads `chat.notify.reply.agent.>`; the gateway alone reads the first
three and publishes the fourth. The answer does not go to the agent's `_INBOX` for the reason the verifier's does not:
the agent reads its JetStream replies there, and a gateway able to publish into it could
forge them; a request whose reply subject is outside the namespace is dropped unanswered.
The agent could already post the same text to the same channel or conversation through
`hermes send` under `today`, so the route moves the post to the process holding the credential rather than adding
a reach. A reply a human types in a notify's thread arrives at the gateway as an ordinary
message from that human, through the usual ingress checks, and starts a conversation of its
own: nothing binds it to the investigation that raised the alert. (On Slack the bot rooted
a notify thread, so a reply there is a turn with or without a mention; see "Which messages
become turns" below and #3246.)

## The Slack adapter (added 9/4)

The mapped-identity adapter behind the five-operation interface, built ahead of gchat
and the first backend with a real identity join - gchat needs none, since the email it
asserts is already the principal. Transport is Socket Mode - an outbound websocket, so no inbound endpoint
on the cluster and no ingress to secure, the same property that made Discord cheap. The
existing `SlackSpec` already carries the two Secret refs Socket Mode needs (bot token
for the Web API, app token for the socket), and under `next` they arm the gateway's Slack
backend, the way `spec.integration.googleChat` arms its Chat one (`a2aSlackArmed`). The same
refs feed the credential broker's own Socket Mode connection on the legacy path
(`credential_proxy.py`, `SlackRelay`), and Slack spreads an app's events across every
connection it has open, so two consumers would split one workspace's messages. The mode
chooses, as it does for Chat: under `next` the broker is not handed the pair and the Hermes
slack platform and its relay env are off (`legacySlackConsumer`); under `today`, or under
`next` while Chat holds the gateway, the legacy path keeps Slack. `allowedUsers` carries
over the way Chat's does (2026-10-06): the operator renders it as `A2A_SLACK_ALLOWED_USERS`,
normalized the way the gateway reads it, with `A2A_SLACK_ALLOW_ALL_USERS` from the legacy
consumer's rule on the raw list (absent, or a single empty string, is everyone), so one CR
means one thing in both modes. The gateway then admits a Slack sender only if they belong
to the bot's own workspace, or the list names them by workspace as `T…:U…` (a Slack Connect
guest from another organisation is otherwise not a turn; see the mapping-table paragraph
below), and the list admits them (exact match, no case fold: the `user_id` for our own
member, the `T…:U…` entry for a guest). The principal map below is an optional override for attribution, not a second
gate. One thing does not carry over on the flip:
the broker reads `SLACK_BOT_TOKEN` as a comma-separated list, one token per workspace,
where the gateway's adapter takes one token, so a multi-workspace install stays on
`today`. `homeChannel` does carry over, as the home channel the gateway's chat.notify route
posts proactive alerts into (below); `/sethome` no longer reaches it.

The operator does not enforce the multi-workspace rule. The arm reads the CR alone and never opens the
token Secret, so an install already on `next` whose bot-token Secret holds a list is flipped
by the operator upgrade that brings the arm: the broker is no longer handed the pair, the
gateway takes the whole list as one token, and Slack refuses it at `auth.test`. The pod does
not exit: the console is the mux's essential backend, so the Slack backend is contained and
retried on a backoff, the pod stays Running and Ready, and Slack has no consumer until the
Secret holds one workspace's token or the install goes back to `today`. Nothing in the CR's
`.status` says why, since no `A2AGateway` condition is written for a gateway the arm called
configured; the gateway pod's log carries the cause, and its events do for a missing Secret
or key. Reading the Secret in the arm would catch it, at the cost of a
Secret read on every pass and an arm that turns on Secret contents rather than the CR; the
CR-only arm was kept and the limitation documented instead (2026-10-06). The installer
checks the applies it makes (#2812). `install.sh`, its menu's apply and a full `upgrade.sh`
refuse an apply that hands Slack to the gateway while `SLACK_BOT_TOKEN` holds a list, and
also refuse the token when they recover it from the Secret
([`scripts/installer/README.md`](../../scripts/installer/README.md)). Three paths still
reach the state above unchecked: `upgrade.sh`'s operator and harness arms, which re-tag the
release; an operator upgrade made any other way; and a CR edited by hand.

Version skew double-consumes Slack, and this is where Slack differs from Chat. The arm uses
Chat's rule, so an unrecognised `spec.mode` reads as `today` and the legacy consumer renders
again: the broker's Socket Mode relay and the Hermes slack platform. The reconciler freezes
the A2A objects on skew rather than touching them, and a Slack gateway holds the token pair
in its own pod env, so the frozen gateway keeps its own Socket Mode connection beside the
legacy one, until the operator recognises the mode again. Slack delivers each event to one
of the two connections. Both admit by `allowedUsers`, but only the gateway refuses another
workspace's member unless a `T…:U…` entry names them, an entry the legacy consumer never
matches, so a Slack Connect guest is answered or dropped depending on which
consumer receives the event (as is a sender whose map entry carries the reserved `slack:`
prefix, which only the gateway refuses). Chat's
frozen gateway goes quiet instead, because it reaches Chat only through the broker's A2A
relay, which the re-rendered broker drops. The Chat-identical rule was kept and the
behaviour documented (2026-10-06).

**Conversation keys.** `slack:{channel}/{thread_ts}` for a channel thread and
`slack:dm/{channel}/{thread_ts}` for a DM thread. Slack threads are implicit - replying
with a `thread_ts` creates one - so a channel mention binds the session to the mention
message's own ts as thread root, with no thread-creation failure mode to handle. A DM is
keyed the same way, to match the Hermes Slack platform on `today` (2026-10-08): a
top-level DM is a new conversation rooted on its own ts, and its placeholder, status
edits and answer post in that thread; a DM typed inside a thread is that thread's
conversation, so it steers the running task or follows up in the same context, as a
channel thread reply does. Two top-level questions are two conversations. On Slack a
thread is the session in a DM as in a channel. So `stop`, a status question and
`/session` act on the thread they are typed in. A `stop` or a status question typed at
the top of the DM is a conversation with nothing running in it, so it starts no task
and its reply says to send it in the question's thread instead; the adapter marks such
a message top-level (`InboundMessage.TopLevel`, set only for a top-level DM), and the
gateway keys the where-to-send-it reply on that mark. A `stop` in a thread with
nothing running gets "nothing is running": the plain reply when the conversation has
never run a task (a question refused at the session cap, say), and otherwise one that
adds that if the agent filed a card, that card keeps working until it finishes, since
`stop` cannot reach a card (gke-labs#3263). With `A2A_CARD_STOP=true` the second case
stops the conversation's cards instead (Stop, above). A top-level status question matches the exact
status phrases only; one shaped like a question ("how is the deploy doing") is an ask.
A bare `/session` asks for the next message in its thread. Slack's Stop button on an Agent
Session's Working… line sends `agent_session_stopped`, which the adapter turns into a cancel that
names no task, keyed on the thread's conversation, with `IntentCancel`, which no chat message
carries, so the gateway tells the button from the word. The event names no team for the presser, so
the adapter checks the workspace itself: a member the allowed-users list names passes, anyone else
must be in this workspace by `users.info` (cached per member for five minutes), and a lookup that
fails, or a workspace auth.test never named, drops the press. Only a requester the conversation's record
holds (a task's requester or steer author, or a session author) may press it; anyone else's press
cancels nothing and posts nothing (`stopgate.go`), while typed `stop` keeps its rule. A record that
cannot tell who asked gates nobody: nothing ran, a requester was cleared past `AskTTL`, a steer or
session author was dropped past its cap, or the history is at its cap (`cannotTellWhoAsked`). With
the card
stop armed the button cancels the turn without the `🛑` lines and sends the same control, and the
gateway words the result as the Slack Stop's one reply (`slack_stop.go`); unarmed, it is a typed
`stop`.

The thread-less `slack:dm/{channel}` parses and posts top-level, and no inbound message
is keyed that way. It is the form a DM record from before DMs threaded carries, so such
a record relays its task's terminal into the DM where the ask was, and a message sent
since starts its own thread rather than steering or stopping that task. It is also what
`openDirect` returns, which makes a post the user did not ask for a new top-level
message; a reply under it roots its own conversation.

**Which messages become turns.** DMs carry every message, in a thread or not; a DM
thread needs no task started in it to carry a reply. A channel message must
mention the bot, and the ask's own ts is the thread the session will live in. A thread
reply is a turn when it mentions the bot, when the thread's root is the bot's own post
(a `chat.notify` alert or report: Slack sends the root's author as `parent_user_id` on every
reply, the same rule as the today path's Slack adapter, #3246), or when the thread is a
session thread - one the gateway has started a task in. That is what lets a session thread carry every message
(the Discord parity) without making every thread in a joined channel a session. A
thread becomes a session thread only when the gateway starts a task in it - a verified
sender's mentioned ask, in the channel or in someone else's thread - and the adapter
learns that from the gateway's `TaskStarted` and, on a cache miss, from the session
registry, which it asks whether a task has started in that thread (a record alone is
not enough, since one is minted for any verified turn, a "stop" with nothing running
included). Apart from the bot-rooted rule above, nothing is derived from the root
message. A mention on its own starts
nothing and makes nothing a session thread, whoever typed it, so a channel mention from
a sender the gateway refuses (another workspace's member, or one the allowlist does not
admit) roots nothing. A session thread stays one while a
task runs there or the session has had activity within the idle TTL; past that, the
thread needs a fresh mention. The activity that counts is the last task's own: its start,
and an executor's terminal for it, so the window opens at the answer, not at the ask that
started a long task. A verified turn that starts nothing (a "stop" with nothing running) moves
the reap's clock but not this one, and the reap's own supervisor terminal moves neither. The
bound is activity, not the record: the session record
outlives the reap (which deletes the idle pod and keeps the record), so the registry
answers from the active task and the last activity, hands back the bound its answer
holds to, and the adapter expires its cached answer on that bound - the registry's own,
not a TTL of its own counting - and asks again. Two subtypes count as turns besides
plain messages: `thread_broadcast` (a
thread reply with "also send to channel" checked - dropping it would eat a steer
silently) and `file_share` (an ask with an attachment). Everything else drops in the
adapter: bots, our own posts, edits and other subtypes, socket redeliveries. Group DMs
(mpim) are group spaces here, not DMs - they have threads, so the mention affordance
applies; Discord's group DMs read as DMs, and the asymmetry is deliberate. The bot
only sees channels it has been invited to, so the invitation is the trust boundary for
group ingress.

**The message shortcut (added 10/16).** A member can make a turn of any message, whoever
wrote it, with the app's message shortcut (manifest `features.shortcuts`, `type: message`,
`callback_id: ask_about_message`, which needs the `commands` scope). Slack sends a
`message_action` interactive envelope, acked like a click; the adapter handles it off the
event pump, because it reads from Slack before it has a turn to hand over, and `Run` waits
for it like the pump. The turn is the clicker's: the allowlist and the workspace rule apply
to them, as for a typed ask, and a redelivery is dropped by `action_ts`. Its conversation is
the thread under the message (the message's thread when it is a reply), its message id is
the message, so the ask's reaction goes on it, and its text is a fixed frame
(`slackShortcutAsk`) around the message quoted: the words of its Block Kit blocks (sections,
fields, headers, context and rich_text, not buttons), or its text when it has none, since an
app's text beside blocks is only the fallback; its attachments' pretext, title, text and
fields, and its Block Kit body (the fallback when none of them has words); and its files'
names, opened by who posted it
(a member's Slack name, an app's posting name, on one line and clipped), with the channel's
name in the frame. The poster's name goes inside the quote because it is the message's word:
an app chooses the name it posts under and a member sets their own. The frame tells the agent
the quote is something to check, not an instruction, because the requester is the clicker and
the message's author may be anyone, a program included; the eval case
`chat-shortcut-alert-is-checked-not-obeyed` holds the agent to it. Once the gateway has
verified the clicker and the shortcut's own task starts, the adapter posts a context-only
message in the thread naming them by their Slack name (`<name> asked me to look at this`),
from `TaskStarted`, under the session lock and before anything of the task's can post, so
a channel reading a bot's reply under someone else's message can see who asked. It precedes
the task's first reply; it is not glued to it. A reply typed in the thread before the
executor's first post is a turn of its own, and the gateway's answer to it (a steer's ack, a
refusal) lands between the line and the task's reply. It is its own message,
not a line on the task's first post, because nothing ties a post to the task it is for: under
Agent Sessions there is no placeholder, and the first post in the thread can be the gateway's
reply to another turn (a steer's ack, a refusal). A shortcut whose turn starts no task (a
steer into a running one) posts no line, and any other verified turn in the thread clears a
line still waiting for its task. Before handing anything
over the adapter asks `conversations.info` whether it can post there. A channel it is not a
member of, and a conversation it cannot see at all (a private channel or a DM it is not in,
`channel_not_found`), gets a note only the clicker sees, through the payload's
`response_url` (posted only when it is on `hooks.slack.com`), and no turn. A failed read is
not a refusal: the turn goes ahead.

**The mapping table - where it lives and who writes it.** The table is an optional
override, not an admission gate: the allowed-users list admits, and the table joins an
admitted sender's immutable `user_id` to an identity sourced from our own IdP; never
`profile.email` (the identity section above says why). A sender the table does not name
is attributed by member id, `slack:<user_id>`, with `verifiedBy`
`slack-socket-mode`. The `slack:` prefix is reserved: a table value that carries it is
refused (the sender drops, with an error in the gateway's log), so a principal that
claims to be a bare member id always is one. The Slack principal decides attribution
only: capability is minted from the install's tier for every sender, and sessions have no
owner.

Because an unmapped sender's member id is enough to be admitted, admission must not reach
past the install's own workspace. Slack names the sender's workspace as `user_team` on a
message in a channel shared between workspaces, and a message's own `team` field names the
workspace it was posted from. The gateway refuses a message that names a workspace other
than the bot's own (from `auth.test`) in either: it is not a turn, unless the allowlist names
that member of that workspace as `T…:U…` (2026-10-21, #3294). The sender's workspace is
`user_team` when set, else the message's `team`; when both are set and differ, no entry is
matched, and when both are empty the sender is our own member, as below. An admitted
guest's turn carries `T…:U…` as its author id, so the allowlist and the target allowlist
match that one entry, and the guest's principal is `slack:T…:U…`. The principal map cannot
name a guest: it is a Secret, and a Secret key cannot hold `:`. A
plain `U…` entry keeps meaning our own member and never admits a guest, and allow-all, an
empty list, admits none. When a refused message addressed the
bot (a DM or a mention), the refusal is logged with the sender's member id, and the sender
gets one reply in that message's thread, once per workspace and member, saying the bot
answers only its own workspace and naming the `T…:U…` entry that would admit them. A click on one of the bot's buttons by such a member is
refused the same way, and shares that once-per-sender notice, in the question's thread. Without a team id from `auth.test` it refuses any
message that names a workspace, except a guest the list names, and warns once at start. A message that names no workspace
in either field is taken as our own member's, which rests on Slack setting them on a
shared channel's messages; no captured Slack Connect payload has confirmed it yet. Under allow-all this is the only boundary on
who may reach the bot, and it is stricter than the legacy consumer, which has no such
check. Under Enterprise Grid a member of a sister workspace in a shared channel is refused
too unless named, since the event does not carry the sender's enterprise.

The table is a Kubernetes Secret, mounted read-only at the gateway's principal-map path, same file format the Discord ConfigMap uses.
`a2a-slack-principal-map` is the name the operator mounts under `next`: alone at the
gateway's one principal-map path, optional, so an install without its table is the
gateway's own case (it runs, and every listed sender is attributed by member id, which
the gateway notes in its log at start). Alone because the gateway reads that
directory as one flat map and resolves a Slack sender against every key in it: the
hand-made `principal-map` ConfigMap, Discord's table, mounted there too would let a
ConfigMap write add a Slack `user_id`, so a Slack-armed gateway does not mount it. Every
other gateway mounts that ConfigMap, as before. The gateway reads the map once, at start, so a table
created or edited after the gateway is running takes effect at its next restart. Nothing
in-tree creates its content. A
Secret rather than a ConfigMap because a
write to this table grants a principal - it is an impersonation primitive, and it holds
emails besides. Write access is the install admin's, through the install path. No
product ServiceAccount (gateway, platform-agent, broker, session workers) gets write on
it, so nothing an agent can be talked into doing edits its own identity table; the
conformance suite's D1 holds that no Role the operator mints names `secrets`. A
`principalMapSecretRef` on `spec.integration.slack`, not built, would let an install bind
another name and make write authority "may write the PlatformAgent CR", with changes in
the API server audit log. Generating
the Secret's content from the IdP is a job we do not build yet; until it exists the
table is maintained by hand, which is honest at the current install count.

**Unverified senders.** Dropped at ingress, as everywhere (on Slack that is an unlisted
sender; an unmapped one is attributed by member id, below) - but visibly now: the gateway
posts a one-line notice to the conversation, once per sender, and keeps the structured
log line. The exception is a message the sender addressed to nobody (`Unaddressed`: an
unmentioned Slack reply under one of the bot's own posts, #3246), which is dropped with a
info-level log line only and does not count as the sender's notice. A shortcut's
conversation is a thread under someone else's message, so the gateway posts no notice
there either (`Shortcut`); the adapter sends it to the clicker alone through the
shortcut's `response_url`, every time. A silent drop of a real user is a support burden. Per sender, not per
conversation - a channel mention mints a fresh conversation every time, so a
conversation-scoped dedupe would be no bound at all. The memory is capped and evicted
wholesale at the cap, so the worst an unverified sender can do is make one notice
repeat. This is gateway behavior, not Slack behavior, so Discord and Google Chat get
it too - the notice names the remedy for whichever backend it fires on (the allowed-users
list on Slack and gchat; on Slack it also names the principal map, for the one refusal the
map still makes, a listed member whose entry carries the reserved `slack:` prefix). A listed
sender the table does not name is attributed by member id, not dropped.

**Roster.** Channel membership via the members API, one page; past a page the roster
reports incomplete rather than paging (the roster cap truncates far below it anyway).
Slack has no per-thread membership, and anyone in the channel can read the thread, so
channel membership is the honest answer to "who could have read this." Members are
resolved through the same rule as the requester, the allowed-users list and then the map,
so a mapped member the list refuses is recorded by their raw backend id, never under the
principal the gateway declined to grant them, and a listed member the map does not name
as `slack:<user_id>`, the principal they get as a requester.

**Reactions, clicks and Agent Sessions.** The adapter draws a chat message's `mark`
(spec-a2a-payloads.md) as reactions on the asking message: today's emoji per ask kind, ⏸️ for
`blocked`, ❌ for `failed`, nothing for `done`. The gateway owns the state (the chain's newest
task's mark, the terminal default, a conversation notify's mark) and records what it drew on
the chain's root history entry, so a mark draws only its difference, across a restart too; the
adapter owns which emoji a token shows, and removes only its own reactions. A delegated child's
terminal takes no default, since it is relayed before the wake that digests it exists: the
wake's terminal settles the chain, or the child's end does when no wake starts. A chain that a
click starts, or a typed answer to the conversation's open question (`InboundMessage.Answer`),
continues that ask's work, so it marks the ask of the conversation's previous chain, from what
is already drawn there: a click is no message of its own, and a card that settles after its
question was answered then lifts ⏸️ on the ask rather than marking the answer. The chain it
continued no longer marks that ask, so its late terminal (a stop confirmed after the answer)
leaves the reactions to the continuing chain. Interactive
envelopes are acked like events. A click on a choice button the renderer drew is a message from
the clicker whose text is the choice's reply, handed to the gateway like a typed one, so the
allowlist and the map judge it the same way and it carries no intent. The gateway tells the
adapter whether each message's sender passed (`VerifiedObserver`), and only then does the
adapter rewrite the clicked question: its choice buttons removed, "✓ <name>: <label>"
added, the name from `users.info` (display name, real name, handle; never an id). A
typed reply in a conversation whose question is still open answers it the same way, with the
reply's first words; a gateway command (a stop, a status ask or a short question about
progress in the wide form, `/session`, a delegate) answers nothing. A question is answered once, and a second click on it is dropped. The open question
is the latest message the adapter laid out with choices in the conversation, other than choices
that are all an answer's next steps (`next`), which ask nothing, and one laid out
with choices again is a fresh question whatever answered it before; an edit that takes its
choices away (a card's question settled by its notify `update` key, laid out or as text) closes
it, and keeps the answered line when one was drawn. The open question and the answered set live in
the process, so a question posted before a restart keeps its
buttons until clicked, and a typed reply then rewrites nothing. A thread's Agent Session is
`processing` from `TaskStarted` until the chain ends, and past that while the latest chain's
mark still carries its ask (card work still open), `suspended` while its settle is `blocked`,
and `closed` after. When the latest chain's mark holds no open work and an older ask's still
does (its cards still running, or blocked), the session follows the newest such ask instead,
so it holds until every chain's cards have settled. Only an older ask a notify `update` key
is bound to holds it, and for at most four hours after its mark was last drawn or a keyed notify last repeated it (the latest across every chain that marked the ask, so a click or typed answer continuing it keeps the clock), since nothing else can settle an older ask and a lost settle would otherwise hold it for good. A terminal that draws nothing still tells Slack when that bound alone has moved the session. `closed` is sent even for a thread this process never opened, since a restarted gateway
otherwise leaves the old process's Working… on it; the thread is renamed once from its first
verified ask that is not a gateway command, with today's title rules: the ask up to its first
question, sanitised. That state is process
memory too, so after a restart the next verified ask in a thread may title it again. `assistant_thread_started`, and `app_home_opened` on the Messages tab, get today's
three suggested prompts. `agent_session_stopped` is the Stop button, above.

**One backend per gateway process.** The relay binds one durable, and two gateways on
one durable split event deliveries - so config counts the armed backends and refuses
any combination but exactly one: a Slack pair, a Discord token, and a gchat relay URL
are mutually exclusive, and none of the three is also a refusal. A second backend is a
second Deployment with its own durable, when we want one. The inject side door is not
in that count - it may sit beside any one of the three, for the reason the inject
section gives - and neither is the console adapter, which has no durable to split.

`verifiedBy` is `slack-socket-mode+principal-map` when our table joined the sender to a
principal, and `slack-socket-mode` when it did not: either way Slack authenticated the
sender over the socket and asserted the `user_id`. Rendering
into mrkdwn is a narrow deterministic translation; the legacy Hermes converter stays
where it is. A line pass runs first, outside fenced blocks, for the forms mrkdwn has no
syntax for: a heading, ATX or setext, becomes a bold line (shedding any bold pairs
inside it, and left plain when a `**` that is not half of a pair remains), a
thematic break is dropped, a list marker
becomes `•`, and a pipe table becomes a fenced block with its columns padded (a cell
keeps a code span's content without its backticks, sheds the marks of closed bold pairs, and shows a link as
its label and URL, since a code block renders no link; only an escaped `\|` stays inside
a cell). A table the chunker cuts gets its header and delimiter rows again at the head of the next chunk, so each part renders as a table. A
heading run onto the end of a sentence with no line break is moved to its own line
first. Bold and links are translated after it. The translation leaves
code spans as written, converts bold only on a closed `**` pair, never alters a
link's destination, and refuses a link whose label carries a URL naming a host other
than the one it opens. A link the agent already wrote in the surfaces' own `<url|text>`
form is kept as the link it is when it is closed, sits in prose (not in a code span or
after an unclosed fence), has an `http(s)` URL with no whitespace, angle bracket or pipe
in it, and has text that names no other host: the refusal a markdown link with that
label meets. The markdown rules read a kept link as a link, so a bold pair
around it converts and a markdown link that overlaps it is refused. Posts ask Slack not
to unfurl links or media: an answer cites console and documentation links, and each
unfurled into a preview card under it. Everything else posted is escaped first (`&`,
`<`, `>`), a kept link's URL and text included - relayed text is executor-authored, ie
model output, and an unescaped `<!channel>` in a result would ping the room. The Google
Chat adapter applies the same markdown rules, and keeps the same links, behind its own
defang, in two halves: the link defang reads prose, link text and an unclosed fence but
not a closed code span, a fenced block or a kept link; the mention defang reads
everything, code included, but a kept link, whose URL and text hold no `<`. The adapters translate each chunk of a result alone, so the chunker closes a
fenced block it cuts and reopens it with a bare fence at the start of the next chunk,
reading the cut with the same code-span parse the adapters use: every chunk is balanced on
its fences, so a cut block never leaks its closer into the next chunk, the fences added stay
within the chunk cap, and the text between them is the original, byte for byte. When a cut
falls inside a code span opening delimiter, or inside a span that can be carried intact into the
next chunk whose start lies in the second half of the budget, the cut moves back to the span's start,
preserving the span intact in the next chunk. A fenced block longer than a chunk is split with
balanced fences as before; a multi-line double-backtick span the rule cannot move (longer than a
chunk, or beginning before the second half of the budget) is still split with its closer in the next
chunk, a display defect tracked as a follow-up. The opener's
language tag is not carried onto the reopened fence (it is the rest of the opener's line, unbounded),
so a continuation chunk loses the tag on Discord.

**Chat messages (added 10/13).** A task's carriers may hold a `kube-agents.chat/v1` message
beside their text (spec-a2a-payloads.md, "Chat messages"), and the relay reads it with
`a2a/lib/chatmsg` on every carrier that spec lists: the result and a finished turn on their last
chunk, a progress artifact, a `notice` artifact (one message of its own, posted once), the
message of an `input-required` status, and a terminal status, whose chat message is its `mark`
alone. A chat message that breaks a rule is dropped whole, logged with the rule it broke and
counted (`chat_messages_total{outcome="invalid"}`, under Metrics), and its text posts as it
would have; a bad `mark` drops alone, and a chat message that carries only a `mark` lays
nothing out. A result recovered by replaying the stream after a restart arrives folded, which loses the chunk a chat message rode on, so the last-chunk rule is not applied to it. What survives reaches the adapter as a message value,
`PostMessage` and `EditMessage` with the text and the chat message, beside the string `Post`
and `Edit`. Discord, Google Chat, the inject door, the console and the A2A door answer
`ErrTextOnly`, and the relay posts the text through `Post`, chunked under the cap, so each of
them posts exactly what it posted before chat messages existed. The relay never hands the
adapter a chat message without its text, and a backend that cannot hold one, or whose API
refuses it, gets the text instead, once. Every carrier's `mark`, the terminal's included, is
the gateway's to apply (`mark.go`): only from the chain's newest task, with the terminal
default for a newest task that ends without one, drawn through the adapter's `React` and
`RemoveReaction` (the Slack adapter section).

The Slack adapter lays a chat message out as Block Kit, in the spec's fixed order and in the
`today` presenter's shape (`slack_presenter.blocks_report`, `with_side_bar`,
`slack_status.plan_blocks`), with the text as the message's `text` (escaped mrkdwn, so a
notification and a thread read-back carry it): the headline bold with the note after it on the
same line, as one `rich_text` block; the detail under it; the rows between two dividers, each led
by its severity as inline code; the `after` lines; the fold as a collapsible `container`,
collapsed (a fold with neither text nor a table is omitted), its text as `rich_text` blocks of
at most 2,500 characters each, cut between elements (a long fenced block between its lines, a
long list between its items, its numbering kept; one item past that stands alone), and its table as a native `table`
block whose first row is the column names; then the buttons, choices before links, five to an
`actions` block, the one `primary` entry drawn as Slack's primary button. A choice's `value` is
what a click sends, its `reply` or else its `text`, and a link button opens its url; action ids
are the presenter's `kage.choice.<n>` and `kage.link.<n>`. A progress message's `plan` is
one top-level `task_card` per item, each row's steps listed under it and marked `✓`, the last
step of an `in_progress` row `◌`; not Slack's `plan` block, which Slack draws as its title alone,
folded, and allows once per message, so the plan's `title` is the fallback text's only. A `tone` colours a side bar (green `#2EB67D`, yellow `#ECB22E`,
red `#E01E5A`) holding every block under the headline. Slack refuses a `container` inside the
attachment a side bar is, and an attachment draws after every top-level block, so a message with
a fold keeps the fold and everything above it top-level and the bar holds only the buttons; with
no buttons it has no bar, and its tone shows nowhere (the text states the fact either way). Markdown
in the detail and the fold's text is drawn as rich text (`slackRichDocument`): a heading as a bold
line, a fenced block as `rich_text_preformatted` (one inside a list item loses the item's indent),
bullet and numbered lists as `rich_text_list` with their nesting as its `indent` and a numbered
list's first number less one as its `offset`, a quote as `rich_text_quote`, a table as a preformatted block
with its columns lined up, a rule dropped, and paragraphs a blank line apart. Inline, it and the
rows and `after` keep code spans, bold, italic, strike and links whose url passes the links rule,
the label check included; anything else shows as written. Every field goes out as `rich_text` or
`plain_text`, which Slack shows as written, so a `<!here>` or a bare `@here` in one is inert text;
`refuseSlackChat` still refuses a layout carrying a mention in a mrkdwn text object
(`blocksMention`, the notify route's check), which the renderer no longer writes. The blocks go on
the wire as the renderer wrote them, not through slack-go's types.
With Agent Sessions wired (every Slack adapter the gateway builds) there is no live message in a
Slack thread, since the adapter is a `StatusShower` for any conversation with a thread; where a backend keeps one, the live message shows
the newest progress chat message under every display mode, and keeps it when the task ends, its text turning to the terminal line; a progress artifact without one turns
the message back to text. A layout the backend refuses is not offered again until the progress
chat message changes, and the Slack adapter's text `Edit` sends the blocks and side bar cleared,
so a live message laid out before a gateway restart does not keep a stale plan. A post or edit
that fails for any other reason (a rate limit, an outage, a message that is gone) is no refusal:
it is not counted, and the live message tries the layout again on its next artifact. A click on
a choice button is a message from the clicker whose text is the button's `value` (Reactions,
clicks and Agent Sessions, above); a link button opens its url and its click is only acked.
That is the exception to the notify route's refusal of interactive blocks: a raw `blocks`
request is the sender's layout and the gateway cannot vouch for what a click sends, while a chat
message's buttons are the gateway's own, built from validated choices whose click text the
gateway reads as an ordinary message. The fold's text is drawn as rich text as the detail is,
so a heading there shows as a bold line and a list as a list, as `toMrkdwn` renders them in a
text answer. `a2a/gateway/testdata/slack-chat/` holds the layout of every chat
message the shared cases in `tests/conformance/chat/` expect, checked on every run.

A conversation notify request (below) reads its `chat` field and `update` key the same way.
The `chat` object, with the request's `text` as its fallback, is laid out on Slack and posted as
the text elsewhere. The first request with an `update` key posts one message, its text cut to one
chunk since an edit reaches only one message, and a later request with the same key in the same
conversation edits it, laid out or as text, on Google Chat too. A key with no message here
(another conversation's, one forgotten, one lost to a restart) or a message the backend says can no longer be edited posts anew. A laid-out edit that fails otherwise (a rate limit, an outage) is tried once more, laid out, and failing again leaves the message as it was and answers the request with the error: a text edit would strip the card and close an open question.
A conversation keeps 16 keys and the gateway keys for 1,024 conversations, the least recently used
forgotten first, so a plan edited all along outlives the questions filed after it. A notify's
`mark` is drawn once its post lands, on the ask of the chain its `update` key is bound to
(spec-a2a-payloads.md, "Card work after the task ends"): the first keyed `mark` binds the key
to the conversation's latest chain, recorded on that chain's root history entry beside its
mark (at most 16 keys a chain, the least recently used dropped first, as the notifier drops them), so the binding survives a restart and a later `mark` with the
key reaches that ask while a newer chain runs. A `mark` with no key goes to the latest chain
once nothing runs, and is dropped, logged and counted (`notify_marks_dropped_total`) while a
chain runs.

**The Home tab.** A member opening the app's Home tab (`app_home_opened`, tab `home`) is answered
with `views.publish`: a dashboard of five panels, with no model in the loop (`slack_home.go`). Fleet,
Running, Recent pull requests and Last audit are one board snapshot, read over the board route: the
gateway publishes an empty request on `chat.board` with its reply under `chat.board.reply.gateway.`,
and the Hermes bridge answers with session-kv's `GET /v1/board` (`agents/platform/scripts/board_snapshot.py`):
every Cluster Agent profile's cluster and any cluster a finding names, with the findings queue's open findings for each, the worst 15 listed beside the total, the running
kanban cards, the newest `pr_opened` events, and the newest finished fleet-audit run from the report
store; and `first_report_pending` while onboarding has filed its sweep, its first report is not out
(the findings nudge's own marker pair, `findings_nudge.first_report_markers`: the delivery claim, or
on the conversation path the recorded outcome, because a claim there can be released), and delivery
has not yet put the sweep's findings in the queue (`.bootstrap_findings_registered`), with `first_message_pending` while no message has primed onboarding either (no
`.bootstrap_greeted`) and the sweep is under a week old, past which the plugin primes nothing. The tab then draws a cluster with no open finding as not yet
checked instead of green, and asks for the first message. Waiting on you is the adapter's own record: the open question of each conversation whose
latest verified asker is the viewer, so it is per member and the rest is the install's, and it holds
only questions posted since the gateway started. One snapshot serves every open for 30 seconds, and a failed read for 5; one republish pass runs at a time, from one read. A
member who opened the tab in the last ten minutes is published to again, ten seconds after a burst
of changes: a card's plan moving or a mark changing reads the board again, a question asked or
answered recomputes who is waiting and reads the board only once the snapshot has aged out. At
most 50 members are republished to. Admission is the inbound allowlist (`slackPrincipal`) and,
when allow-all is what admits the member, their `users.info` team being the bot's (a failed lookup publishes nothing; a republish goes only to members admitted when they opened the tab and looks nothing up again); anyone else gets one line and no read of the board.
A board that cannot be read draws Fleet with a line saying so, never an empty fleet.

**The board-health card's buttons.** The `kanban-board-health` job's stuck tasks reach Slack as a
chat message whose rows carry the cards each stands for (`tasks`) and the buttons to draw under it
(`actions`, `retry` and `drop`; spec-a2a-payloads.md, "Chat messages"). The renderer lays such a
row out as its text over an actions block keyed by the row (`kage_board.row.<n>`), labels the
buttons Retry and Drop itself, and puts the row's ids, joined by commas, in their value
(`slack_board.go`). A tap is never a turn. It is admitted by the Home tab's rule above, then sent
over the board route as a request with a body (`lib.BoardAction`), which the Hermes bridge relays
to session-kv's `POST /v1/board/actions`; that unblocks (Retry) or archives (Drop) the row's cards
while they are still `blocked`, and answers each card's outcome. The row is then edited in place:
its buttons give way to "✓ Retrying. <name>, <time>", "Dropped. <name>, <time>", or "No longer
stuck. <name>, <time>" when the cards had already moved, the time as Slack's date token; a tap that did not land
keeps the buttons under a line saying so, and so does a row whose edit Slack refused, so a second tap asks again. One tap per row at a time, and one edit of a card at a time, each from the blocks the last one sent, in this process. A message whose rows carry buttons is drawn without its side bar, so every row stays in the message's own blocks where the edit finds it.

### Answer feedback (added 10/16)

A chat user rates the final answer of an ask 👍 or 👎, and the rating keeps the exchange so an
operator can turn it into an eval case: a 👎 one the answer should fail, a 👍 one it should pass. The gateway marks the one message that answers the ask
(`OutMessage.Feedback`, the root task id of the ask's chain), and it is one of two. When the
Planning Agent answers itself, it is the completed result of the task whose end is the chain's
(`observedAs`), never a hand-off line (`handOffEnd`), the same message the `DeliverableObserver`
is handed. When a task of the chain hands the ask to kanban cards (its terminal mark keeps the
ask's kind, `cardsPending`, or it published `handoff`; a delegated child's counts), the chain's
root records it (`CardsAnswer`) and no relay answer in the chain is marked, the acknowledgement
and a wake's reply included. The answer is then the card report the kanban notifier flags with
`answer_to` on its conversation notify: the report of the card whose settle finished the ask's
plan (so one per ask; a fan-out's children fold into their rows and post nothing, so it is the
creator's), naming that plan's `update` key. The notify route marks it under the ask the key is
bound to (`notifyChainIndex`, bound by the plan's first mark), laid out or, for a report that
posts as text (an actionable one), on its last chunk; a key no ask holds is not marked rather than
guessed at; so is a key bound to a chain that did not hand its ask to cards, and a second card
answer for an ask that already has a rated one (an ask's follow-up cards can settle a second plan),
which the gateway remembers per process. Some asks go unrated rather than rated wrongly: the notifier keeps the plan in
memory, so a card that settles an ask after the agent restarted is not flagged (one that sent a progress note since the restart opened a new plan, whose key binds to the conversation's latest ask, and is rated only if that ask was handed to cards too, taking that ask's one rated card answer, so its own report then posts unrated); a last card that
gives up without a hard-wall reason posts no answer to flag; a plan whose first post Slack refused has no key, so its ask's answer is not flagged; a plan records one answer, at the settle that first finished it, so a card unblocked after it gave up adds no second rated report; the flag is read once, so a report
whose send is retried goes out unflagged; and a plan's key binds to the conversation's latest ask
at the plan's first mark, so a follow-up asked before that mark takes the card's answer. Plan rows, progress, a question waiting on the
user, notices, any other report, a card failure the wake explains and a keyed notify are not
marked. A text answer chunked under the cap
carries the mark on its last chunk. A card answer that also offers a next step or options keeps
them as its buttons, with the rating controls under them as the last block.

The Slack adapter draws the mark as Slack's own feedback buttons: a `context_actions` block holding
one `feedback_buttons` element (hover text "Good answer" and "Bad answer"), the message's last
block, inside its side bar when it has one (an attachment draws after every top-level block, and
Slack draws the block there too, seen live). The block's id carries the task id
(`kage.feedback.<task>`). A text chunk carrying the mark is posted as sections (more than one when
escaping takes it past a section's cap) and the block; a chunk Slack refuses posts as text. Every other backend ignores the mark. A tap is never a turn,
and a tap from a sender the gateway would not take a turn from (`resolvePrincipal`), or from
another workspace's member, changes nothing.

A 👍 is counted and kept with the ask and the rated answer, and nothing is posted (Slack fills the
tapped icon itself); a rater has one rating per answer, so it replaces that rater's 👎 on the same
answer and drops its note, which said what went wrong. A 👎 is
counted and kept with the rated answer first, one KV write, and only once it is kept opens a modal
on the tap's trigger, which lapses three seconds after the tap ("What went wrong?" over one
optional field, "Anything else?", of up to 500 characters, and a line telling the rater how long
their question and the answer are kept, read from the bucket's max age, or, when the kept record
holds no ask (past `AskTTL`, a reaped session), how long the answer is kept: "This answer is kept on
your install for 30 days so an operator can turn it into a test."); a write that fails opens
nothing. The gateway takes the ask from the session record: the human message that started the
rated chain (`TaskRef.Request` on the root entry, as written and held to its 1 KiB cap, until
`AskTTL` clears it), so a later message in the thread, or another person's, is never taken for
it, and the adapter reads nothing of the thread; a draft whose ask was no longer on record says so
in its `prompt` rather than shipping an empty one. The answer is the rated message's text, which for a text answer longer than one chunk is
its last chunk. Send writes the note into the kept 👎 and posts "Thanks. I kept this answer for
your team to review." to the rater alone, followed by a link to the project's feedback form
(`https://gke-labs.github.io/kube-agents/feedback`, nothing pre-filled) and a warning that it opens
a public issue, so the install's details stay out of it (`chat.postEphemeral`, in the thread); with no 👎 kept (its
write failed, or a 👍 replaced it) it keeps nothing and posts nothing. Cancel keeps the 👎 without a note. A note and
a re-rating merge under the record's revision (`mergeFeedback`), so neither loses the other's part,
and the ratings on one answer by one rater run one at a time, so a 👍 tapped while the 👎 is still
being written replaces it. A redelivered Send (Socket Mode is at-least-once) is handled once.
No new scope: `chat:write` covers the ephemeral line, and `views.open` takes only the trigger.

A kept rating is a JSON record in the `feedback` KV bucket, one key per answer and rater
(`<task>.<rater>`, so re-rating replaces it): the rating, the note, the platform, the rater's Slack
member id, the conversation and rated message, the task and context ids, the ask and the answer.
It is user content and may hold cluster data, which the bucket's posture answers: the provision Job
creates it with a 30-day max age (`--ttl=720h`), so NATS drops a record 30 days after the rating;
the gateway turns feedback on only when the bucket exists and has a max age, and leaves answers
unmarked otherwise (an install whose operator predates the bucket, or a bucket someone recreated
without one), looking again every minute for 30 minutes so that a provision Job finishing after an
upgraded gateway started still turns it on, then stopping, so a NATS server that never grants the
gateway the bucket does not log a refused request every minute; and the gateway's user is the only reader and writer of its
records by grant (provision and seed hold STREAM.INFO on it, which can list the keys: task id and rater). It is capped at 64MiB, past which a rating is counted but not kept. Logs carry the task id, the
conversation and the rating, never the text; `answer_feedback_total` carries only the platform and
the rating.

An answer whose layout is already at Slack's 50-block cap posts laid out without the controls. The
answered line a click or typed reply leaves on an answer's question goes above the controls, which
stay the last block.

An operator reads the bucket through the gateway's own binary, in its pod, since nothing else holds
the bucket's credentials:
`kubectl exec -n kubeagents-system deploy/<name>-a2a-gateway -- /gateway feedback export [--since 7d] > feedback.tar`.
Each kept rating becomes a directory `feedback-<date>-<hash>/` (more digits when two would share one)
holding `task.yaml`, a draft case per [`bench-case-format.md`](bench-case-format.md) (the ask as its
`prompt`, `deployer: noop`, and TODOs for `domain`, `owner` and the `verification_spec`; a 👎's
header comment and `expected_output` carry its note, a 👍's say the rated answer was good and the
checks should pass on it), and `transcript.md`, the ask, the answer and any note with their ids. The draft is for a person to finish and submit; `transcript.md` holds the
install's data and stays out of the pull request. The install never writes to this repository, and
no sink files the drafts anywhere: nothing is filed automatically into the public kube-agents
repository, and a rating leaves the install only if the customer chooses to send it.

The Slack Home tab ends, under a divider, with "This week: N 👍, M 👎", the kept ratings given in
the last seven days (`FeedbackRecorder.Tally`, every current record read over one watcher),
reused for five minutes (one minute after a failed read) and left out when feedback is off or the
tally cannot be read; a new rating shows once the count is read again.

An install turns it off with `spec.integration.slack.answerFeedback: false` (default `true`). The
operator renders `A2A_SLACK_ANSWER_FEEDBACK=false` on the gateway only for an explicit `false`, so an
install that never set the field keeps its gateway pod unchanged; the gateway then never looks for
the bucket, marks no answer and hands the adapter no recorder, so a tap on a button already posted
is acked and changes nothing (Slack still fills the tapped icon; no box opens).

## The console adapter (added 9/23)

The web console's chat door. Not a chat product's ingress: the browser is a bus client
already (the `web` read surface), so the door is on the bus too, and the identity story is
the one every other writer here has.

**Transport.** Core NATS, no stream. The browser publishes a frame -
`{"messageId","text","kind"}`, `kind` defaulting to `text` - to `chat.console.<token>.in`;
the adapter posts the gateway's own notices back as `{"messageId","text","edit"}` on
`chat.console.<token>.out`. Outside `a2a.>` on purpose: this is chat transport, not bus
protocol, and the payload spec's agreement rules do not apply to it. Each task's end goes out
on `.out` too (added 10/22), as every observer is told it (`observeTaskTerminal`): once per
delegation chain, under the task the turn started, with the hand-off rule applied. It is one
frame with `"kind": "result"`, the A2A `state`, the task the turn started as `taskId` and the
agent that turn was addressed to as `addressee` (not whichever agent answered at the chain's end),
and `messageId` `r-<taskId>-<state>`, so a repeated end (a redelivery, a heal) repeats its id while
a different end of the same task (a never-started heal, then its late completion) is a new frame. An
end the gateway declared itself (the bus unreachable, a task released for producing nothing) is
worded as the page's notice is. A completed
task that handed work to kanban cards (the bridge's `handoff` artifact) carries `"cardsPending":
true`, so a client waits for the cards' notify frames; without it, no handoff was seen and the
client need not wait. Its text is the deliverable for a completed task and the gateway's notice
otherwise. A chain that ends at a child with no wake (the session could not take one) ends failed,
carrying the notice; a console client reads the child's answer from TASKS. A kanban card's report into the conversation is a frame too, with `"kind": "notify"` and the
turn it answers as `replyTo` ("Proactive posts: the chat.notify route", the console's route). A
notice carries none of those fields, so a client waiting on a turn can tell the three apart. The
page reads answers from TASKS and gets the notices, so it skips the result frames, and a lost result
frame costs it nothing. It shows the notify frames, which it has no other source for. A card's
report is not on TASKS, so a notify frame published while no client holds the conversation's `.out`
subscription (the tab closed, a page reconnecting, the MCP server restarting) is lost: core NATS
keeps nothing, and the notifier answers the card as posted. This is a known limit of the
kanban-era route; card reports become bus child results with the profile dispatcher. A console client such as the MCP server reads each task's end from these frames as it happens. `.out` is not on a stream, so a result frame published while the client is not subscribed (a restart, a reconnect) is not replayed. The `console` identity holds TASKS consumer grants, so the client recovers a missed end from the task's own events there. Turn answers to a follow-up inside a live task are not
sent; that client reads them from TASKS or waits for the end. An answer past 256 KiB, or one
whose escaped frame would pass the server's `max_payload`, is cut and says so; TASKS keeps
it whole. An `.in` frame published
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
The console adapter stamps its own backend on every message it delivers, the way the inject
door does, so `authority.requester.backend`, the drop notice and verification name the console
rather than the configured backend. Where there is no message to ask (the relay holding a
session record), the `console:` prefix answers the same question. `openDirect` takes a bare
user id and goes to the configured backend. With no real backend, as on an inject-only eval
install, the console is the only chat backend and runs without a mux. With no backend and no
door, `A2A_CONSOLE_DOOR=true` lets the gateway start on the console alone; the operator renders
it on every next gateway (2026-10-22), so every `mode: next` install runs one.

The inject door, when armed, sits beside the mux rather than inside it, for the reason in "A
side door, not a fourth backend" above: the gateway finds the door's probe and observers by
type assertion on the top of the adapter stack, and the mux has no probe and no key for an
`inject:` conversation. The mux does pass the task observer calls and the session lookup on to
the backend that owns the conversation, since the Slack adapter learns its session threads
from them. The console runs whenever the gateway runs, so an inject-only gateway also exits when the console
adapter does.

**The console outlives the chat backend.** The console is the way in when chat is broken, so
the two do not share a fate. When the console adapter stops, the gateway exits and restarts.
When the chat backend stops (a bad or revoked Discord or Slack token, a Socket Mode give-up),
the mux logs `chat backend stopped` at error with the backend in a `backend` field, and runs
that backend again after a delay that doubles from one second to a one-minute cap, while the
console keeps serving. A run that lasted at least the cap starts the doubling over. A backend
that returns without an error while the gateway is still running has stopped all the same and is
handled the same way. Google Chat never takes this path, since its adapter retries pulls itself.
A dead chat backend therefore shows as that log line on a Running pod, not as a restart.

**Bounds.** A frame's text is capped at 16 KiB; over it, the frame is refused with a notice
naming the cap. Empty, malformed and mis-shaped frames drop with a log line each, as does a
frame whose `kind` is anything but `text`. At most 8 turns wait behind one console
conversation: a chat platform paces its own senders, but the console credential can publish as
fast as it likes, so past the cap a frame is dropped. The first drop in each fill logs and posts
one notice, and the rest are silent until the conversation has room again. A NATS render that predates the console identity,
or a NATS pod not yet rolled onto the new one, refuses the adapter's subscription
asynchronously; the adapter logs that with the remedy rather than boot-failing,
because the chat backend beside it is still good.

## The A2A door (added 9/24)

The gateway's ingress for an agent caller: an A2A client (Antigravity, an ADK agent, the MCP
bridge that fronts Claude, `curl`) that speaks the A2A protocol over HTTP. A second side door
beside the inject door, built on the same contract, and differing in what it speaks rather
than in what it may do. This section is the door as `a2a/gateway/a2adoor.go` implements it.

**A side door, again.** The inject door's reasoning holds unchanged. The door holds no bus
credential, mints no id the bus sees and writes no `authority` block; a verified caller's
message becomes an `InboundMessage` into `handleInbound`, and from there the turn is a chat
turn - routed by the same matcher, subject to the same first-event grace, relayed back through
the same `post`/`edit`. It sits beside the mux with the inject door, under the composite that
forwards the gateway's probe and observers by conversation prefix, and it is not counted by the
one-real-backend guard for the reason the inject door is not. Either door alone starts a
gateway; with both doors and no real backend the default attribution is the inject door's, and
every message through either stamps its own backend.

**Wire.** JSON-RPC 2.0 over HTTP at `/a2a`, and the agent card at
`/.well-known/agent-card.json`. Four methods:

- `message/send` is a turn. The door answers with the A2A `Task` once the submission is on the
  bus (`TaskObserver.TaskAccepted`, the inject door's rule: a caller is never handed an id whose
  terminal cannot come), or with the gateway's reply as an A2A `Message` when the turn started
  no task (a status answer, a steer, a refusal). `configuration.blocking: true` holds the call
  until the task's terminal, bounded, which is the one-call `curl` demo. `message.taskId` names
  a running task of the caller's own and lands the text on its conversation, where the gateway
  treats it as a steer. `message.messageId` is the dedupe key: a retry after a dropped
  connection is answered with the task the first attempt started, and starts nothing.
- `tasks/get` returns the `Task` as the door holds it. Scoped to the caller: a task another
  caller started is not found rather than forbidden, so the door confirms no id it will not
  serve.
- `tasks/cancel` is a cancel turn on the task's conversation - the same `kind: cancel` envelope
  the inject door's cancel route and the chat path's `stop` publish - answered once the cancel is
  on the bus (`TaskObserver.CancelPublished`) with the `Task` as it stands. The executor decides
  when the task is canceled; the client polls `tasks/get` for that terminal as for any other.
- `message/stream` is refused as unsupported and the card says `streaming: false`. It lands
  next, as SSE frames from the same observer hooks.

Only text parts are accepted. A data or file part has no home on a chat turn, so it is refused
at the door in the protocol's own terms rather than dropped.

**The Task object** is assembled from what the relay posted on the door's conversation, which
is what a chat user would have read. The rolling progress line (`startTask`'s placeholder, which
the relay edits) is `status.message`; the state is `submitted` from `TaskStarted`, `working` from
the first edit, and the terminal state from `TaskTerminal`, whose reason - the executor's
terminal status message, verbatim, which is `reason: <token>` on a failure - replaces the line
on a terminal that carries one. Every other post under the task is an agent message in
`history`, after the caller's own; every message inside the Task carries the binding's
`kind: "message"`, which the bus payload's own message struct does not. On a completed terminal
the task's one artifact, named `result` as the bus names it, is the deliverable the relay hands
the door whole before it posts it in chat-sized chunks (`DeliverableObserver`; the heal path
hands over the artifact the stream carries), so the chunks stay history and the artifact is
never inferred from a post's position. A turn that delegates is one Task through its whole chain
("Sessions by default"): the child's and the wake's posts land in its `history`, and the wake's
result is its artifact. A chain that runs past the gateway's task deadline is no longer exempt
from the door's conversation cap while the caller waits, which is a known limit. The door keeps up to 4 MiB of it; a longer deliverable
is cut at a rune boundary with `metadata.resultTruncatedFrom` carrying the original length, so a
client can fall back to `history`, which carries the chunks whole. Everything the door keeps per
task (the caller's text, the posts, the deliverable) is summed across tasks under a 64 MiB budget;
past it the oldest tasks are gone whole, the protocol's `not found`, as under the task-count cap.
A turn's reply is assembled from a per-conversation log bounded in bytes too, and a reply whose
head fell off it says so in `metadata.postsEvicted`. A completed task the relay
handed nothing for (a non-text result) renders no artifact. `metadata.terminalSource` carries whose word the terminal
is, for the reason the inject door's read route carries it. A2A clients read exactly `status`,
`artifacts` and `history`, so nothing here is invented for them. Not mapped yet: an executor's
non-final `input-required` reaches `history` as the relay's ask line, not `status.state`, which
stays `working`; no executor in this tree publishes it, and the streaming change carries status
events through as they come.

**Conversation.** `a2a:<caller>:<contextId>`, kind `dm`. The caller is part of the key so two
callers naming the same `contextId` do not share a conversation; a caller that sends none is
minted one and reads it back on the `Task`. `Roster` is the caller alone, complete;
`openDirect` returns the caller's last conversation, as on the inject door. A Google-verified
caller (below) is `:google:<email>` inside the door, so its conversation is
`a2a::google:<email>:<contextId>`: an eval caller may not contain a colon, so no eval key starts
`a2a::` and no eval caller equals a Google one, which keeps the two classes' conversations and
task scopes apart even though the static token's holder names their caller freely.

**Identity, first version: the eval class.** The caller names itself - the `X-A2A-Caller` header,
or `message.metadata.caller` for a client that cannot set headers - and is resolved through the
door's **own** principal map at the prefixed key `a2a:<caller>`, whose value must be an eval
identity. The three refusals are the inject door's, and for the same reason: the door takes its
caller from the request, so the map is the only thing between a token holder and a principal of
their choosing. An unmapped or unnamed caller is refused with a JSON-RPC error and starts nothing;
nothing is defaulted. `verifiedBy` is `a2a-bearer`, its own value, so an external agent's
submission and an eval harness's are distinguishable downstream even though both resolve into
the eval namespace today.

**Identity, second class: a developer signed in with Google.** A bearer that is not the door's
static token is checked, when `A2A_DOOR_GOOGLE_CLIENT_ID` is set, as a Google OAuth access token:
Google's tokeninfo endpoint must answer for it with the install's one pre-registered client as
its audience or authorized party, a verified email, and an expiry still ahead. The email, as
Google sent it, is the principal - the same string the Google Chat adapter carries for the same
person, case-preserved as Chat keeps it for the audit join - admitted only if it is on the
door's allowlist (`A2A_DOOR_ALLOWED_USERS`, compared case-insensitively; empty admits nobody, and
there is no allow-all). The door checks the list before it holds any conversation or submission for
the caller (the verifier's cache of the token's verdict is the one thing kept, bounded in
bytes), so an account off it cannot fill the door's bounded state, and the gateway checks it again before it
resolves the principal. The authority block records backend `a2a-google` and
`verifiedBy` `a2a-google-token`. A request carrying the token may not also name a caller: the
header or `message.metadata.caller` beside a token is refused, so a token never travels with a
name that disagrees with it. A token Google refuses (tokeninfo's 400) is a 401 a client answers
by signing in again; an account off the list is a 403; a check that could not be made (Google
unreachable or answering with an error of its own, more checks in flight than the door allows)
is a 503. An admission is remembered for at most five minutes or until the token
expires, under a byte bound; a refusal is not remembered. This is a verifier beside the eval
map, never an entry in it: the static token and its map work as before. The roster resolves the
class's own way, never through the chat principal map that is the gateway's default.

Validating a token at the door is not the per-user token brokerage the permission model
declined: the door holds a client id and an allowlist (which the gateway checks again), never a refresh token or a
client secret. The principal this class asserts is attribution: the task's capability is the
install's, as it is for every ingress. Still to come as verifiers of their own, each with its
own `verifiedBy`: JWT access tokens from an OpenID Connect provider (Entra, Okta), and the
unattended class (an organisation's service identity, read-only against protected targets).

**The card is the catalog.** One skill per destination this door routes to, which today is the
gateway's default addressee. When profiles land the list is rendered from `DIRECTORY` and the
caller's entitlements, per caller, and it is the same list the router's capability catalog is
built from. The card is the one unauthenticated route, because discovery reads it to learn which
security scheme to present; it discloses the endpoint URL, the scheme and the default
destination's name, none of which a 401 hides. With the Google class armed the card lists a
second scheme beside the bearer, `openIdConnect` at Google's OpenID configuration, as an
alternative.

**Posture.** Every RPC request carries a bearer token (`A2A_DOOR_TOKEN`, required whenever
`A2A_DOOR_LISTEN` is set, no unauthenticated mode), or, with the Google class armed, a Google
access token in its place; a bearer shaped like a Google access token (`ya29.`) is then sent
to Google's tokeninfo endpoint to be checked, and any other is refused without leaving the
cluster; the caller map is its own file
(`A2A_DOOR_PRINCIPAL_MAP`); the card advertises `A2A_DOOR_PUBLIC_URL` when set, and otherwise
the address the card was fetched from (the request's host, or the forwarded host and scheme
behind a proxy), since behind a port-forward or an ingress the listen address is reachable by
nobody. The operator renders the door the way
it renders the inject door, under its own operator-level flag (`A2A_AGENT_DOOR=true`, never a
CRD field): a loopback bind on its own port, its own one-entry map admitting one caller, a
token Secret minted once, a ClusterIP Service for the port-forward, and the gateway fence
under the door's own name, so each door comes and goes with its own flag. The render counts
the door as a backend the way it counts the inject door (`a2aGatewayBackend`): a `mode: next`
install with no `discord-bot` Secret and this door armed gets its gateway rather than the
`NoChatBackend` condition, which is what the gateway's own start-up check already accepts. The
operator does not yet render the Google class's env; until it does, and until the door has
ingress a customer reaches, it is a dev and eval door like the other.

_Update 2026-10-22: superseded, every `mode: next` install runs a console-only gateway, so the door
no longer decides whether the gateway renders; `a2aGatewayBackend` and the `NoChatBackend` path are
gone (see the update note under "What it also settles")._

## Metrics (added 10/6)

The gateway serves Prometheus counters on a metrics-only listener, the credential broker's
pattern copied: its own port, `/metrics` and nothing else (any other path is 404, any method
but GET or HEAD 405), every interface rather than loopback because its caller is the managed-Prometheus
collector on the pod network. `A2A_METRICS_PORT` names the port; unset means no listener, and a
value that is not a port, or is either door's port, refuses the boot. A port that will not bind
costs the gateway its metrics and logs an `ALERT` line; conversations carry on. The operator
renders `A2A_METRICS_PORT=9096` and declares container port `a2a-metrics` on 9096 from one
constant (`a2aGatewayMetricsPort`), and the chart's `<name>-a2a-gateway-monitoring`
`PodMonitoring` scrapes 9096 every 30 seconds behind the `platformAgent.podMonitoring` switch. The
gateway's own NetworkPolicy, `<name>-a2a-gateway-netpol`, renders wherever the gateway Deployment
does, door or no door, including a gateway an older operator scaled to zero replicas for want of
a backend (only the stack's teardown removes it), and admits two peers, to 9096 alone: the `gke-gmp-system` namespace, the
broker's second rule with the gateway's port in it, and the operator's pods from the operator's
namespace, the rule the broker's and the Hermes gateway's policies carry for the usage poller,
which reads the gateway's inbound-message counter. Each armed door renders a copy under its own
name, and the doors' ports admit no pod. The gateway's fence used to render only with a door, which
left the metrics port, bound on every interface, reachable from the whole pod network on an install
with neither door armed; it renders on every `next` install now so that it is not (decided
10/6 on #2473). It is Ingress-only, so the gateway's own dials to the bus are untouched.

The site's [Observability page](../site/src/content/docs/concepts/observability.md) is
canonical for what an operator reads off these series; this section is the design. Every label
value comes from a closed list, never from a task, a conversation or an executor's
text, so nothing a chat user or an executor sends can mint a series:

| Metric                                               | Labels                                                                                                                                           | Counts                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `kubeagents_a2a_gateway_task_terminals_total`        | `state`: `completed`, `failed`, `canceled`, `rejected`, `other`; `source`: `executor`, `supervisor`, `gateway`, `gateway-never-started`, `other` | Each task terminal the gateway delivers to a conversation, counted once where every terminal path converges (`observeTaskTerminal`), by its final state and by whose word it is (`TerminalSource`): the executor's on the events subject, the supervisor's on the supervisor subject, or the gateway's own for a task whose submission never reached the bus (`gateway`) or that no executor took inside `A2A_FIRST_EVENT_GRACE` (`gateway-never-started`). A delegation chain is counted once under its root; a session turn that asked to delegate but started no child counts as `failed` even when the executor reported `completed`. The operator's usage poller reads `failed` and `rejected` into `status.usage.chatTurnsErroredTotal`. |
| `kubeagents_a2a_gateway_gchat_events_received_total` | none                                                                                                                                             | Google Chat events pulled from the broker's relay, before parsing or classification. A payload that does not parse, or an event that is not a turn, still counts.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `kubeagents_a2a_gateway_gchat_pulls_total`           | `outcome`: `events`, `empty`, `failed`                                                                                                           | Pulls of the Chat relay: one that returned an event, one that returned nothing, one that failed (the relay refused or did not answer, or the pull was never made or could not be read). The same counts as the 15-minute `gchat events received` line, without the reset.                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `kubeagents_a2a_gateway_chat_messages_total`         | `outcome`: `rendered`, `text_only`, `refused`, `invalid`                                                                                         | Each `kube-agents.chat/v1` message the gateway hands an adapter, and each it drops, on the relay and on a conversation notify ("Chat messages" under the Slack adapter): laid out by the backend; posted as its text by a backend that renders none; refused by a backend that does (too large for it, or its API said no) and posted as its text; or, under `invalid`, dropped for a broken rule with the text standing, or a bad `mark` dropped alone with the layout still rendering. Counted per hand-over, so a live message laid out again on each progress artifact counts each time.                                                                                                                                                   |
| `kubeagents_a2a_gateway_notify_marks_dropped_total`  | none                                                                                                                                             | Conversation notify marks dropped because they name no chain (no `update` key bound to one) while a chain runs in the conversation ("Chat messages" under the Slack adapter).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `kubeagents_a2a_gateway_messages_inbound_total`      | `platform`: `googlechat`, `slack`, `discord`, `console`, `other`                                                                                 | Each chat message the gateway accepted as a turn, counted in `handleInbound` before the sender is verified, so a message from a sender the principal map does not know counts, as the `chat_metrics` plugin counts the Hermes-side ingress before its allowlist; the doors' turns (inject, a2a, a2a-google) are programs' and are not counted. The operator's usage poller reads it into `status.usage.chatMessagesInboundTotal` beside the plugin's series.                                                                                                                                                                                                                                                                                   |
| `kubeagents_a2a_gateway_answer_feedback_total`       | `platform`: `googlechat`, `slack`, `discord`, `console`, `other`; `rating`: `up`, `down`, `other`                                                | Each rating a verified sender gave an answer ("Answer feedback" under the Slack adapter), counted before the 👎's record is written, so a rating the bucket could not keep still counts; a rating changed counts again.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |

The body also carries `process_start_time_seconds`, captured once at start and never
re-derived, which the operator's usage poller reads to tell an in-place restart from a counter
that fell. Every series is created at zero, so a `rate()` over a state that has not happened reads 0
rather than no data. A pull carries at most one event today, so `gchat_events_received_total`
equals `gchat_pulls_total{outcome="events"}`; they are separate so the first keeps its meaning if a
pull ever returns more than one. An install that Chat publishes nothing to shows `empty` rising
and `events` flat; a refused relay shows `failed` rising.

The terminal counter counts every terminal the gateway hands its adapter, not only the relay's.
It counts in `observeTaskTerminal`, the funnel each route ends in once: the relay; the stale-task
heal, which delivers the terminal the relay missed, under the source its subject gives it; the
never-started heal (`gateway-never-started`); `observeChildEnd` for a child whose end started no wake;
and a submission that never reached the bus (`gateway`). A delegation chain is counted once under
its root: `observedAs` keeps both the turn that delegated and its child from reaching `observeTaskTerminal`
separately, and a session turn that asked to delegate but started no child is counted as failed (`handOffEnd`)
even when the executor reported completed. The failures the gateway declares itself (`gateway-never-started`,
`gateway`, and a delegated turn with no child) show `failed` rising under them during a bus outage or on an install
with no executor, rather than reading zero failed tasks while the chat shows ❌. The executor's reason token is not a label.
The gateway does not know the executors' tokens, which are theirs to define, so it has no closed
list to draw one from, and an open one is the cardinality the labels exist to avoid.

## What stage 2 builds from this doc

- The gateway: Discord, Google Chat and Slack adapters, session manager (spawn / stream
  / reap / rehydrate / sweep), bus client, KV session registry.
- The session pod shim: bus-to-stream-json bridge, event mapping.
- The `authority` block, populated at ingress: `requester` and `audience` advisory,
  `grants` decision-grade by resolution.
- Roster tracking and the `openDirect` primitive.

Not in stage 2: the classifier, the LCD permissions tool, and the transition work
"Sessions by default" names (the gateway-minted child task, the `chat` profile's skills,
the warm pool). (The gchat and slack adapters were on this list until 9/5 and 9/4
respectively; each now has its own section above. `grants` and "anything that makes
`authority` decision-grade" were on it until 9/9, when they landed - the bullet above is
what replaced them. The `/session` opt-in came off when it shipped.)

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
