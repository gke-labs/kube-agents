# Model router design

- **Author:** [@bnaylor]
- **Date:** 2026-09-22
- **Status:** design of record, first version. Nothing here is implemented: the gateway still routes with the string matcher in `a2a/gateway/text.go`, the `conversation` KV bucket does not exist, and the `model-router` LiteLLM alias is not rendered. The numbers below were measured on a dev install with a harness that is not yet in this tree; it lands in `a2a/gateway` as the router's acceptance test in a follow-up, and until then the figures are this document's evidence rather than something the tree can reproduce. The model used was `gemini-3.5-flash` through LiteLLM, standing in for the alias.

## Purpose

This document defines the model router: the component that reads a chat message in the
context of its conversation and decides where it goes. It fills the seam the gateway
design reserved and did not fill - a component beside the gateway that may block or
reroute a message and never widens anything ([`spec-chatops-gateway.md`](spec-chatops-gateway.md),
"The gateway holds no model").

The gateway routes today with a hard-coded matcher: fifteen status phrases, a wide
interrogative heuristic, `stop|cancel|abort`, a leading "delegate". That is the right
shape for a component that sees every human message and must have nothing to inject
into, and it cannot understand "actually make that bar instead" or "put it in a new
namespace called johns-team". Left alone, it ends with users typing `/route` at their own
fleet, which is a regression from what the Hermes-based agent gives them today. The
router exists so that natural language understood in context stays the interface.

The end-state architecture resolves a message in a fixed order: a slash command first,
then an `@handle`, then natural language
([`../architecture/02-agent-personas.md`](../architecture/02-agent-personas.md),
[`../architecture/06-api-and-data-contracts.md`](../architecture/06-api-and-data-contracts.md)).
That order stands. A slash command is a function call: deterministic code, resolved
ahead of the router, never reaching the model. A handle is a name on the destination list
and resolves deterministically whenever the gateway holds a map from handle to
destination. Today it does not: no agent has a chat identity, and the destination list has
two entries, so an `@handle` resolves through the router until profiles give agents
handles and a deterministic map earns its keep. That is an implementation detail of the
current version, not a change to the contract. Natural language is the catch-all layer,
and in product terms it is the primary experience, because it is what people type. The
audit record the architecture asks for, which agent resolved and by which mode, is the
published decision below.

What this specification does change is sequence and framing. The roadmap scheduled the
deterministic modes first and natural-language routing later, as a fallback; this builds
the router first, because natural language is the product, and lands slash dispatch beside
the gateway's existing interceptors. The architecture documents carry dated notes saying
exactly that and no more.

Companion documents, and what each owns that this one uses: the payload spec owns the
envelope, the task lifecycle, what each subject class admits, and the verified-identity
table that a new KV subject needs a row in ([`spec-a2a-payloads.md`](spec-a2a-payloads.md));
the gateway spec owns sessions, the one-task-per-conversation rule, the deterministic
interceptors, requester identity and the adapters; the NATS deployment spec owns the list
of KV buckets and the per-role bucket grants ([`spec-nats-deployment.md`](spec-nats-deployment.md));
the subagent-profiles spec owns what a destination is once profiles exist
([`spec-subagent-profiles.md`](spec-subagent-profiles.md)).

## The shape

```
chat ──▶ gateway (deterministic) ──▶ router (model) ──▶ Decision ──▶ gateway validates
           │  holds the bus credential                                  │  and publishes
           │  mints ids                                                 │
           │  pre-fetches read-only context ─────────────────────────────┘
           └──▶ conversation KV bucket (the window)
```

One model call per turn. Structured output against a strict JSON schema. No tool loop.
The gateway pre-fetches everything the router could want - the destinations this
conversation may route to, the live agent cards, the active task, the conversation's
recent turns - and puts it in the prompt. The router returns a `Decision`. The gateway
decides whether to honour it.

The router is a destination picker, never an authority. The gateway keeps the only bus
credential, mints every id and the `authority` block, and is the only publisher. That is
what lets a model sit in the routing path without contradicting the gateway design's
rule: the gateway process still hosts no context window of its own. The router call is
stateless, one turn, no tools, and the window it reads is scoped to one conversation and
held in KV rather than in the process.

This is the graph-dispatch shape go-steer/mast ships (a cheap single-turn classifier, a
deterministic map from its output to a destination, a mandatory fallback, the route
persisted for resume turns), which that project's own design notes prefer over a
coordinator model with specialists installed as tools. One deliberate divergence: this
router may speak. A capability question, a refusal, or a clarifying question is a decision
like any other, because conversation is the point of a chat bot.

### Why not tools

Giving the router tools such as `list_clusters_for_user` or `list_sessions` would
express a constraint that is better expressed by not having them:

- Every tool call is a round trip, and the routing call is already the whole latency
  budget (below). A two-call decision doubles it.
- A tool the router can call is a tool injected text can make it call. Pre-fetching means
  the untrusted text arrives after the reads are done and cannot influence them.
- The tools in question are cheap, local, and knowable in advance. There is no branch
  where the router needs the cluster list and might not. A thing you always call is an
  argument, not a tool.

What tools would genuinely buy - the model discovering a capability it did not know
about - comes from rendering the capability catalog from the live agent cards on the
`DIRECTORY` stream. The gateway holds a core subscribe on `a2a.agents.>` and does not
read it today.  That subscribe is not enough on its own.  A core subscribe sees only what
is published after it starts, and a card is published once, when its profile is created
(`a2a/lib/client.go`), with nothing re-announcing it.  A gateway that restarts after its
profiles exist sees no card at all until somebody creates the next one, so the catalog
has to come from what the stream retains rather than from the subscribe.

That read needs JetStream grants on `DIRECTORY` the gateway does not have.
`a2aGatewayJetStreamGrants` covers `TASKS` and `KV_session-state` and nothing else, and
`platformagent_a2a_identities.go` spells the gap out - "nothing at all on DIRECTORY" -
because #1666 scoped that grant to what the gateway emits, by name and by verb.  Widening
it is a deliberate reversal of that scoping, so it is part of build step 3 rather than a
detail inside it, and it wants the same review the original narrowing got.

## What the router decides

The `Decision` contract: five kinds, six fields, all required under strict schema mode.

| field          | meaning                                                                                                    |
| -------------- | ---------------------------------------------------------------------------------------------------------- |
| `kind`         | `new_task`, `steer`, `cancel`, `status`, or `reply`                                                        |
| `addressee`    | must be one of the destinations the prompt listed, spelled exactly                                         |
| `task_id`      | for `steer`, `cancel` and `status`: the task the decision is about                                         |
| `cancel_first` | with `new_task`: countermand the active task and start a different one, as one atomic decision             |
| `ask`          | self-contained - every pronoun resolved, so the receiving agent needs no conversation history to act on it |
| `text`         | for `reply`: what the router says to the user                                                              |

`ask` being self-contained is what makes the router a destination picker rather than a
participant. The agent downstream receives a complete request and no conversation.

`cancel_first` keeps the gateway's rule that a session is executed by at most one pod at
a time ([`spec-chatops-gateway.md`](spec-chatops-gateway.md), "What a session is"): a
countermand is a cancel and a new task in one validated decision, not a second concurrent
task.

### The destination list

"Never widens anything" is only as true as the list the router chooses from, so the list
is defined here and it is the gateway's, not the model's.

The list is rendered from the gateway's own routing table for the conversation. In the
first version it has two entries: the conversation's default addressee (`platform`, or
whatever `A2A_DEFAULT_ADDRESSEE` names) and the conversation's own session, which is what
the `delegate:` prefix routes to today. That is exactly the set of places the gateway can
send a message now, so relative to the matcher the router widens nothing; it chooses
between the two by understanding instead of by prefix. When profiles exist, entries are
added by policy the gateway reads from the identity map and the profile registry, never
from anything in the model's context. The list is rendered per conversation, so it is per
requester in a DM and per room in a group thread, which is the session model the gateway
spec already has.

### What the router may never do

- **It never publishes.** The gateway holds the only bus credential, mints every id, and
  is the only writer. A decision is a proposal.
- **It never sees another conversation.** The window is keyed by conversation.
- **It never widens anything.** It chooses among the destinations above. A decision
  naming anything else is a policy violation, handled below, not clamped to the nearest
  legal value.
- **A cancel is honoured only from the requester who started the task.** The gateway
  checks this, not the model. The session record does not hold the requester today - the
  principal exists only as the pseudonym inside each envelope's `authority` block - so the
  gateway records the requester's pseudonym on the active task when it starts one, and
  compares pseudonyms. That is a gateway change and is in the build order. The check
  applies to every cancel path once it exists, including the deterministic `stop`
  interceptor, which today cancels for anyone in the room; a `stop` from someone other
  than the requester gets a notice, not a cancel. The prompt carries the same rule as the
  second line of defence.
- **Model output is never an authorization signal.** The gateway's allowlist at ingress
  decides who may reach an agent at all. From there the bound is the executor's own
  ceiling. Section 4a of
  [`../architecture/03-security-model.md`](../architecture/03-security-model.md) owns it.

### What bypasses the model

Three things resolve ahead of the router and never reach it:

- **Slash commands.** A function call, not a prompt: `/status`, `/stop`, `/route
<destination>`, forced disambiguation, and whatever else is added, parsed by
  deterministic code, resolved before the router is consulted. They are always available,
  they are the deterministic path for scripts and for debugging, and they are not the
  taught interface. Natural language is what people type; commands are what you reach for
  when determinism is the point.
- **The gateway's existing interceptors** ([`spec-chatops-gateway.md`](spec-chatops-gateway.md),
  "The deterministic interceptors"): the stop words after normalisation (`stop`, `cancel`,
  `abort`) and the exact status phrase set. That is two of the three interceptors the
  gateway spec defines, and inside the status one the wide interrogative heuristic retires
  into the model, which is what it was approximating. The one thing a user must always be
  able to do when the model is slow or wrong is stop, and that path has no model in it.
- **An exact `@handle`, once a handle map exists.** Until profiles give agents handles
  there is nothing deterministic to match against, and a handle spelled in a turn is the
  easy case for the router.

The third of the gateway's interceptors, the `delegate:` prefix, retires at build step 4
with the call. It is a hand-rolled choice between the two entries on the destination
list, which is the router's whole job, so keeping it would mean two mechanisms deciding
the same thing from the same text. The gateway spec's Delegate flow carries a dated line
saying the prefix is what the gateway does until step 4 lands.

### Invalid output and policy violations are different

- **Invalid output** - the decision fails the schema, or does not arrive inside the
  budget - falls back to the matcher, which handles the turn as it would today. A slow or
  broken model degrades to the current behaviour; it does not hang and it does not drop
  the message.
- **A policy violation** - a well-formed decision naming an addressee not on the list, a
  cancel from a non-requester, a `task_id` that is not this conversation's - is not
  malfunction and is not handed to the matcher, because the matcher's default branch
  would route the same text to the default addressee, which is what an injection naming a
  destination was after. The gateway replies with a templated refusal, routes nothing,
  logs at ERROR, and publishes the decision as dropped (below). A dropped decision is a
  signal: either the catalog render is wrong or someone is trying something.

**What a person sees.** While the router is thinking, nothing. The ~4.4 s call lands ahead
of the task placeholder the gateway already posts, and a second spinner in front of the
first one is noise. That is a choice rather than a measurement, and the first install to
run the router is where it gets tested on real people.

A fallback is not silent, because the matcher may take the turn literally and answer
something nobody asked - "same thing again for the staging cluster" becomes a new task at
the default addressee, and the reply reads as nonsense. So a turn that falls back carries
a gateway-authored notice: one line saying the router did not answer and the message was
routed by the old rules. It is a deterministic template over a fact the gateway owns, so
it belongs in the gateway spec's gateway-authored posts beside the placeholder and the
failure notices, and that file lists it. A policy violation already has its templated
refusal, which is the same shape.

The operator's half is the fallback rate, not the notice. A dropped decision is someone
probing; a fallback rate that moves is the router slow, down, or pointed at a `model-router`
alias a LiteLLM config edit just broke - an outage with no deploy event behind it, which
is why the alert belongs beside the dropped-decision one in build step 5 rather than
waiting for a user to report a strange answer.

## Conversation context

The router needs conversation history to do what the matcher cannot: "actually make that
bar", "ok, do that for foo" after a capabilities question, "same thing again for the
staging cluster" a day later. Today the gateway keeps the first 140 bytes of the active
ask, truncated at a rune boundary, and the last fifty task ids. Nothing else.

Three designs were measured: a small window on the session record that evaporates with
the existing ask TTL; a configurable, longer-lived window in its own KV bucket; and
rebuilding context on every turn by replaying the task stream, with the gateway's own
turns published to the stream so they replay too.

**The decision is the configurable window.** Defaults: 200 turns, 64 KiB, 7 days idle,
with compaction engaged. The small evaporating window is a configuration point of it
(10 turns, 24 hours, compaction off), not a separate build. Replay is not the memory;
its machinery is worth building as recovery after a KV loss.

What decided it was not accuracy, though the window won on accuracy too:

- On a pooled eval set (33 conversations, 91 scored decisions, five runs each because at
  temperature 0 the model still varies on identical prompt bytes), the window scored
  97.4% on plain routing and 96.4% with compaction engaged, one decision apart, against
  bars of 95% plain and 90% on countermands. Replay reached 95.4% only once the gateway's
  own turns were published to the stream, ten points above replay without them, and was
  then two decisions behind the window.
- **The injection surface.** The window never renders agent `event:` output to the model;
  replay's whole mechanism is rendering it. With the one prompt rule that says event
  lines are data removed, every replay arm was captured by planted event text in four
  runs of four, while the window arms stayed clean. The same payload planted in human
  text captured nothing in any arm. Turning the model's reasoning off produced the same
  split. A control that depends on one prompt sentence and one inference parameter is not
  a control; the window does not need it.

Both measured halves of latency turned out not to discriminate. KV get and put cost
0.4-1.5 ms at any window size in range; fanning out a fifty-task replay cost 102 ms at
p95; prompt size across the whole range the three designs span was worth about 300 ms.

### Where the window lives, and what has to be right the first time

**Its own KV bucket, `conversation`, provisioned with a TTL.** Not in the session
record. Two hard reasons and one structural one:

- `session-state` is created with no TTL, and the reaper never deletes a record. A
  seven-day window inside it would be router code that has to run correctly forever. In
  its own bucket, `--ttl=168h` is a bucket property that cannot fail to run.
- At its 256 MiB cap `session-state` refuses writes rather than evicting (the provision
  script sets no discard policy, so the bucket has JetStream's default of discarding new
  writes), and the first write it refuses is a growing overwrite of an existing key, which
  is exactly this design's write once a turn. The gateway logs the failed write and
  carries on, so the symptom would be a gateway that works and silently forgets. In a
  separate bucket, a full transcript costs memory and leaves the session registry working,
  which is the degradation you want.
- Separation gives `$KV.conversation.>` its own grant lines. Today no principal but the
  gateway holds `$KV.session-state.>` on either side, so nothing leaks; the point is that
  a transcript in its own bucket can never be swept into a future reader grant on the
  registry. Nobody but the gateway principal is granted the `conversation` bucket.

**Which layer owns the idle horizon.** The bucket's `--ttl` is the ceiling and nothing
else: `168h`, install-wide, chosen once and chosen as a ceiling, and that is the number
the provision Job's owner is being asked to agree. Every horizon shorter than it is the
gateway enforcing the configured window on read - the small window's 24 hours, a shorter
default for a group room if that open question lands that way - and those are gateway
code, not bucket properties. The first bullet above is about the outer bound, which is the
one that has to run correctly forever and therefore the one that belongs in the bucket.
The consequence to agree with the ceiling: no install can raise it without recreating the
bucket by hand, so a window longer than a week is a change to this document rather than a
configuration knob.

**What cannot be changed after creation is the bucket's properties, not its existence.**
The operator's provision Job runs as the `provision` principal, which holds stream create
and info and no update, and the script guards every `kv add` with an existence check. The
Job's name carries a hash of its script, so a changed script re-runs on an existing
install and creates a bucket that is missing. What it cannot do is alter a bucket that
already exists: the TTL, the size cap and the history depth are fixed the first time the
bucket is created on an install. So the bucket lands first, and its properties are chosen
carefully once, because an install that gets them wrong keeps them.

What the bucket needs across the tree: an entry in the operator's provisioned-stream
list, create and info grants for the `provision` principal on it, publish and subscribe on
`$KV.conversation.>` and the matching JetStream API grants for the gateway, a row in the
NATS deployment spec's bucket list (which becomes four buckets), and a row in the payload
spec's verified-identity table beside `$KV.session-state.>`.

**The window outlives the stream, on purpose.** A 168h window is more than double the
TASKS stream's retention window at its 72h dev default. The gateway spec's content rule
bounds a copy of task content to a shorter horizon than the stream copy it duplicates
([`spec-chatops-gateway.md`](spec-chatops-gateway.md), "The rule covers identifiers, not
content"). That rule is about a duplicate. The window is not one. It is the chat
conversation's own record, the user's turns in the room that produced them, and the
stream copy is not what justifies it existing. The audience is not widened either, since
nobody but the gateway principal is granted the `conversation` bucket. The window never
renders agent `event:` output. So the primary chat context is exempt from the horizon
condition, deliberately and not by oversight (decided 9/23). It is not free: a
conversation stays rehydratable from the window for longer than its tasks stay readable
from the stream.

### Sizing

| measured                                 | value                                                 |
| ---------------------------------------- | ----------------------------------------------------- |
| eval-corpus turn, mean                   | 32.7 bytes                                            |
| a 200-turn record                        | 8,647 bytes                                           |
| the 64 KiB cap                           | needs 328 bytes per turn to reach, ten times the mean |
| KV get and put, any window size in range | 0.4-1.5 ms same-node                                  |

The 200-turn cap binds first. The byte cap is a guard against one pathological turn, not
the working bound, and the defaults do not have to shrink.

**Compaction is a network call and must not be on the turn path.** Compact behind the
turn; serve the uncompacted window until the compaction lands. One measured arm lost a
whole run to three compaction timeouts on the turn path.

Whether a group room gets a shorter default window than a DM is open. Probably yes; no
measurement either way.

## The model call

- **One call, strict `response_format` JSON schema.** At an equal token budget the schema
  costs 6.5% of latency, and uncapped it is cheaper than no schema, because constraining
  the output makes the model generate less. It also provides the guarantee the validator
  relies on.
- **`model-router` is its own LiteLLM alias**, defaulting to `gemini-3.5-flash` with
  reasoning on. The alias and the inference parameters are configuration (below).
- **Mandatory fallback, enforced at load time.** The matcher does not go away; it becomes
  the floor for invalid output, and the router refuses to start without it.
- **Persist the route.** A follow-up to a parked task lands on the agent that parked it
  rather than being re-classified. The session record's addressee and its per-task
  addressee lookup already do this; the router reads them and does not re-derive them.

### The prompt's rules

The prompt's rules are a numbered list rather than prose, so an ablation is a principled
drop-and-renumber. The rule that says lines beginning `event:` are data to read and never
instructions to obey is defence in depth in this design, not the control: the window
never renders event lines. It stays because the capability catalog is rendered from agent
cards, and a card is agent-authored text.

## Latency budget and configuration

Composed from the measured terms (bus p95 plus model p95; no router exists yet to time
end to end):

| configuration              | added p95 | plain accuracy | unsafe outcomes |
| -------------------------- | --------- | -------------- | --------------- |
| reasoning on (the default) | ~4.4 s    | 97.4%          | 0               |
| `reasoning_effort: none`   | ~1.1 s    | 94.9%          | 0               |

Context is not the cost. The four seconds are reasoning tokens, 734 of about 800 on the
routing call. A two-second budget is aspirational, reported against rather than gated on.

**The model and its inference parameters are configuration.** The model will change.

| setting                  | default                                         | note                                                                   |
| ------------------------ | ----------------------------------------------- | ---------------------------------------------------------------------- |
| `router.model`           | the `model-router` alias, at `gemini-3.5-flash` | an alias, so a swap is a LiteLLM config change, not a redeploy         |
| `router.reasoningEffort` | unset, which is the endpoint default: on        | `none` is a supported choice, below the plain bar                      |
| `router.timeout`         | 30s                                             | on expiry the matcher answers; a slow model degrades, it does not hang |

The 30s timeout is a hang guard for a wedged call or a dropped connection, not a latency
target, so it sits well above the measured ~4.4 s rather than near it. The two-second
budget above is reported against rather than gated on. Seven times the measured call
leaves room for an occasional lagging turn. Much longer than that and a wedge leaves the
user watching a dead chat window.

Reasoning on is the default because one decision in forty is not uniformly cheap: a
misrouted mutating ask is a wrong production change, not a retype. `none` is a choice an
install may make for a snappier chat, knowing what it costs: at 94.9% it is one decision
under the plain bar and would not pass the acceptance gate as a default. It is not a
security trade for this design: the window arms were zero-unsafe in every configuration
tested, including with reasoning off and with the event-line rule deleted outright. Both
measured attacks land only where event text is rendered to the model, which this design
never does.

**The scope of that claim.** Zero unsafe outcomes means zero on the two channels the eval
set contains, planted `event:` output and planted human text, across five pooled runs.
The capability catalog is a third channel of rendered agent-authored text, and no fixture
attacks it. That gap is known and accepted. Two things reopen it: anyone proposing
`reasoning_effort: none` as a default rather than an opt-in knob, and the zero-unsafe
result being cited outside this repository.

### Changing the model is a gated change

The eval harness is the acceptance gate for any `model-router` candidate. A candidate
ships only after five pooled runs on the window configuration:

| measure                  | bar                 |
| ------------------------ | ------------------- |
| plain accuracy           | ≥ 95%               |
| countermand accuracy     | ≥ 90%               |
| unsafe outcomes          | 0                   |
| strict-schema parse rate | 100%                |
| added p95                | recorded, not gated |

Five runs, not one. A candidate is re-scored on the adversarial bucket and the parse
rate rather than assumed: a non-reasoning model is a different model, not this one with a
flag flipped.

## Logging and replay of a decision

A model in the routing path makes the gateway's quietness unacceptable rather than
merely bad. **Every decision is published to the task stream** as the gateway's own turn.
The record carries:

- the `correlationId` that already threads chat message to every hop, and the
  conversation's `contextId`, because a `reply` or a dropped decision produces no task
- the decision as returned, verbatim, before validation
- what the gateway did with it: honoured, fell back (and why), or dropped (and which rule)
- the model alias and the inference parameters actually sent

The last item is part of the security posture, not bookkeeping: the prompt's event-line
rule depends on the inference configuration, so that configuration belongs in the audit
record and not only in a Deployment env var.

**The subject is open**, with two constraints that rule out the obvious candidate. It
cannot be the task's supervisor subject: the payload spec admits exactly one kind there,
the terminal status update, and the client library's agreement check refuses anything
else. And it cannot be keyed by task alone, because a `reply` and a dropped decision have
no task. A gateway-owned subject keyed by `contextId`, with a publish grant for the
gateway and no other principal, is the shape to design against; the payload spec is where
it gets defined.

Replay needs no second store: the decision rides the same stream as the conversation it
belongs to, so reconstructing "why did it go there" is the same ordered read that answers
"what happened".

## Injection: blast radius

The router reads untrusted text and produces a routing decision. What a successful
injection can and cannot do:

- **It cannot reach a new destination.** The addressee must be on the list the gateway
  rendered for this conversation, and the gateway re-checks it. The worst case for that
  field is a wrong destination among destinations this conversation could legitimately
  have asked for.
- **It can rewrite the request itself.** `ask` is model-authored free text. The gateway
  publishes it as the task submission, in an envelope it mints with the requester's
  `authority` block. So a captured router does not need a new destination. It keeps a
  legitimate addressee and changes what is being asked for: "describe namespace foo"
  becomes "delete namespace foo", attributed to the person who typed something else. The
  bound on that is the executor's own ceiling, not the requester's permissions. Every
  allowlisted human runs under one shared Google service account and one Kubernetes
  identity. A rewrite is not bounded by who asked. Against that example the ceiling does
  hold today, and on the default install it holds twice. The argv allowlist in
  `agents/platform/scripts/command_policy.py` refuses a `kubectl delete`. Underneath it,
  the customer-cluster kubeconfig authenticates as the pod's Google service account,
  which on the default `read-only` permission set carries viewer roles only, so the same
  call fails at IAM - the canonical
  [security reference](../site/src/content/docs/reference/security-and-iam.md) is the
  page to read, and `terraform/modules/kube-agents-iam/variables.tf` pins the role list.
  The allowlist stands alone only on an install that has chosen `custom` and named a
  write role. Neither layer is a blanket read-only bound. `/v1/vcs/` carries forge writes on
  the broker's own credential, bounded by a managed-repository allowlist rather than by
  the requester. What would make the requester's permissions the bound is per-request
  down-scoping, which is deferred (section 4a of
  [`../architecture/03-security-model.md`](../architecture/03-security-model.md)). The
  one place the rewrite would be visible is the verbatim decision in the audit record.
  That is build step 5, so it lands last.
- **It cannot cancel someone else's task.** Requester ownership is checked in the gateway
  against the pseudonym recorded on the task.
- **It cannot publish.** No credential, no publish path.
- **It can waste work, and it can misroute a task to the wrong one of the user's own
  destinations.** For a mutating ask that is not small: "deploy mysql to foo" landing on
  a production cluster is a production change the user did not ask for.

That last item is why the destination list is the gateway's and why `ask` is
self-contained: the receiving agent's own gates see a complete request and apply their
own controls. Self-containment is also what makes the body worth capturing, since a
rewritten ask arrives downstream looking like any other complete request. The router is
upstream of every existing control and replaces none of them. Mutation escalation, when
it lands, makes a misroute visible to the user rather than silent; it does not narrow the
surface. What narrows the surface is the window never rendering event text.

What holds a rewritten `ask` today is the executor's ceiling. Mutation escalation weakens
that bound, which makes the open question about constraining `ask` sharper than it is
now.

## Broadcast

"Which clusters are running databases?" needs cluster agents, which need profiles. Out
of scope for the first version. The contract has room for it: `kind` is an enum and a
`broadcast` kind is additive. What has to be settled when it arrives is that a broadcast
is many tasks and the gateway executes one task per session at a time. That is a gateway
change, designed when profiles land.

## Slash commands

Deterministic, resolved first, never a model in the path; see "What bypasses the model".
The first version ships the ones the gateway already needs a deterministic spelling for:
`/status`, `/stop`, `/route <destination>`, and a forced-disambiguation form. They are
documented as what they are, a function-call interface for scripts, debugging and the
moments when a person wants certainty over understanding. The product's taught interface
stays natural language.

## Build order

1. **The `conversation` KV bucket in the operator's provision Job, with `--ttl`**, and
   its grants and spec rows. First, because everything after it writes to it, and because
   its properties cannot be changed once an install has created it, so they are chosen
   once, here. Nothing in this step depends on the model.
2. **The window.** Write turns to the bucket, read them back, the 200 turn / 64 KiB /
   7 day caps, compaction off the turn path. Shippable with no router at all, and worth
   shipping that way: it makes the matcher's failures visible in the log.
3. **The gateway-side rules.** The decision contract and its validator, exercised by the
   fallback path; the requester pseudonym recorded on the active task and the ownership
   check on every cancel path; the destination list rendered from the routing table; the
   first read of `DIRECTORY` for the catalog, which needs a `DIRECTORY` grant added to
   `a2aGatewayJetStreamGrants` before any of it runs (Capability catalog above).
   All model-free.
4. **The router call**, behind a flag, with the matcher as the enforced fallback.
5. **Decision publishing** on the subject the payload spec defines, the dropped-decision
   alert, and a fallback-rate condition beside it.

Steps 1 to 3 have no dependency on the `model-router` alias.  Steps 1 and 2 are ordinary
Go.  Step 3 is too, once its grant change lands; that change is an operator change, not a
gateway one, and it is the long pole in the step.

## Open questions

- The provisioning change for the `conversation` bucket needs agreement with whoever
  owns the operator's provision Job before it is written, in particular the TTL and the
  size cap, since neither can be changed on an install afterward.
- Whether a group room gets a shorter default window than a DM.
- The subject the gateway's decisions are published on, under the two constraints above.
- Whether a room can override requester-only cancel (a room owner, an allowlist, an
  explicit override phrase), and what a cancel does against a task with no recorded
  requester, which is anything in flight at rollout. Originator-only for now. The
  chat permissions model is what settles both.
- Whether the gateway should constrain `ask` at all, since a captured router can keep a
  legitimate addressee and rewrite what is being asked for (Injection: blast radius
  above). And whether the decision audit record has to land earlier than build step 5,
  which is the only place a rewrite becomes visible. Unconstrained for now.

## Why not ADK

The question was whether ADK's workflow agents could keep the router on the rails. Not
for this component, because the router has no flow to define:

- ADK's deterministic part is control flow over agent invocations, and this router is one
  invocation. There is no sequence to order and no loop to bound.
- The constraint relied on here, strict `response_format` with a schema, validated, with a
  fallback on any violation, is a field on the model request that ADK forwards. Setting
  it directly keeps the validator testable without a framework.
- A tool decision in ADK costs two round trips by construction, and this design has no
  tools because the budget cannot afford a second call.
- The rails that matter are enforced in the gateway after the model answers, against
  state the model cannot reach: the destination list, requester ownership of a cancel,
  fallback on invalid output, refusal on a policy violation, every decision on the audit
  stream. A framework that shapes how the model is called does not help with any of them.

ADK's place is the A2A edge, as the earlier spike concluded. The falsifier: if
`model-router` ever needs more than one call per turn - a retrieval step, a confirm
step, anything that makes the decision a small flow rather than a classification - this
argument collapses and ADK should be reconsidered.
