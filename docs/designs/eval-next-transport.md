# The eval transport under `spec.mode: next`

> **STATUS — draft for review; nothing here is built.** The bench harness has one transport,
> `hack/ci-deploy.sh` has no mode flag, the gateway has no inject adapter, and the presubmit
> install runs `today`. The measurement that motivates the document is on
> gke-labs/kube-agents#1661; the presubmit run it cites is build `2100310325382352896`. The A2A
> owner answered the first draft's questions on 2026-09-17 and reviewed the draft the same day;
> the answers and the review's points are folded in below as decisions, dated where each lands.

**Scope:** how a bench case reaches the agent when the install runs the next stack, in two
stages, which transport each stage uses, and what each stage proves.
**Owns:** the transport principle, the stage boundaries, the list of what each stage proves, and
what the harness does with the A2A owner's decisions.
The harness itself is `bench/kube_agents_bench/harness.py`; the wire contract is
[`spec-a2a-payloads.md`](spec-a2a-payloads.md); the gateway, and the home of the inject adapter's
design text, is [`spec-chatops-gateway.md`](spec-chatops-gateway.md); the bus deployment is
[`spec-nats-deployment.md`](spec-nats-deployment.md); delegation as a bus task is
[`spec-subagent-profiles.md`](spec-subagent-profiles.md); the switch is
[`spec-mode-switch.md`](spec-mode-switch.md); the verdict a case's result feeds is
[`testing-strategy.md`](testing-strategy.md) §4.2.

## The principle

An eval is a customer's message, sent the way a customer sends it, graded on what the customer
gets back. Three things follow:

- **The entry point is the product's front door.** A case starts by sending a message where a
  customer sends one. Today that is Google Chat: a MESSAGE event on the Pub/Sub topic, pulled
  through the credential proxy's relay into the agent. Under `next` it is still Google Chat, and
  the message travels Pub/Sub, relay, A2A gateway, bus, executor. The harness should not know
  which, so that when the plumbing behind the door changes, the same case keeps running and
  starts grading the new plumbing.
- **The reply graded is what lands in front of the customer.** The answer in the Chat thread, the
  pull request opened, the cluster state observed. A verifier should not depend on anything the
  customer would never see.
- **The install under test is a customer's install.** Chat enabled, the mode set the way a
  customer sets it, no pod-level shortcuts.

### How today's transport fails all three

The `kubeagents` harness reaches the agent over `kubectl port-forward` to the `platform-agent`
Service and `POST /v1/responses` with the API key the presubmit reads out of
`platform-agent-secrets`. That door is not a customer's: no shipped surface calls the Responses
endpoint from outside the cluster. It is also identical in both modes, because the mode switch
renders the bus beside the agent and leaves the agent's HTTP server as it was.

The reply it grades is the Responses payload. When the agent delegates by filing a kanban card,
the harness reads the outstanding cards' statuses off the agent's kanban store with `kubectl exec`
every `AGENT_DELEGATION_POLL_INTERVAL` seconds (30 by default), and re-prompts the same
conversation with an instruction to call `kanban_show` only once a card reads done, archived,
blocked, failed or cancelled (or when the store cannot be read or does not know a card), until
every card has settled or
`AGENT_DELEGATION_TIMEOUT` elapses (1800 s by default, 2700 s in the presubmit, 3000 s for its
six full-audit units). The delivered card results are appended to the answer, and the worker's
report and terminal commands are read back with `kubectl exec` from
`/opt/data/kanban/attachments/<id>/` and `/opt/data/kanban/logs/<id>.log` in the agent pod, then
deleted. A customer sees the card result relayed to their thread; they never see the files, the
store reads, or the collecting turn, which is a model call the customer never made.

The consequence, measured: the presubmit matrix ran under `mode: next` with the A2A gateway in
`ErrImagePull`, the auth callout in `ImagePullBackOff` and the provisioning Job in `Error`, and
the job went GREEN (build `2100310325382352896`, admitted-case pass rate 90.0%, no case
collapsed, every failing repetition a phrase miss the same cases carry under `today`). The gate
cannot red on a broken next stack until a case sends through it. The same measurement found
nothing that passed under `today` and failed reproducibly under `next`, which is the other half
of the finding: the mode does not change what the cases see, because the cases never look.

## Stage 1: the gateway's inject adapter

The gateway is in the path, and the presubmit's transport holds no bus credential (decided
2026-09-17). The
direct-bus transport the first draft of this document proposed proves the bus, the streams and
the executor (not the callout: both principals on that path are static users), but it leaves
out the gateway's routing, its session registry and the relay back, and it hands a second
process the one credential that may publish on `.in`. Stage 1
is therefore the next-stack analogue of the door the harness uses today: an **inject adapter in
the gateway**, a third backend beside Discord and Google Chat, HTTP on localhost or a ClusterIP
Service, off by default, rendered by the operator only under the eval flag. Its design text goes
in the gateway spec's "The test backend" section, which does not carry it yet; this document
records what the harness does with it. The direct-bus transport survives as a diagnostic, below.

Selected by `AGENT_TRANSPORT=inject`; unset, or `api`, is today's transport byte for byte, and
the presubmit exports nothing new until it chooses to. The exchange:

1. Port-forward the adapter's Service on `AGENT_CLUSTER_CONTEXT`, through the harness's existing
   forward machinery and its retry classes.
2. `POST /inject` with a synthetic conversation key, a principal the gateway resolves through
   the inject section of its principal map (below), the prompt as the text, and the run id, case
   id and repetition, `<run>/<case>/<rep>`, as the backend message id; the run id is minted fresh
   on every harness invocation, and the pin the api transport honours for its conversation id,
   `AGENT_CONVERSATION_ID`, is not honoured here, because on the api path a pinned id only
   shares a conversation while on this path it would replay an earlier run's result through
   the dedupe below. The gateway takes the message through `handleInbound` like a message from
   any backend: routing, the session record, `startTask`, and the relay back, with `taskId`,
   `contextId`, `correlationId` and the `authority` block minted by the gateway. The gateway
   hands the adapter no task id: its inbound handler only enqueues, and `startTask` mints the id
   and posts the submitted notice back through the adapter's `Post`. So the `POST` enqueues and
   then waits, inside the adapter, on the session record for its key until `ActiveTask`
   appears, bounded by a short accept bound, and answers with the task id it finds there. Three
   things end the wait as a refusal instead: the unverified-principal notice, which the drop
   path posts to the key through the same `Post` and writes no record behind; the placeholder's
   failure edit, "could not reach the bus", which `startTask` makes through the adapter's `Edit`
   when its publish fails, after it has written the record with the active task and before it
   clears it again, so the record alone cannot tell a published task from one that never
   reached the bus; and the bound expiring without `ActiveTask` appearing, whatever else the
   record holds. The harness classifies a refusal as the gateway refusing the injection or
   failing to publish it, infrastructure. The prompt is routed like any text, the gateway's
   literal affordances included, `Delegate` among them; the harness sends none, and a case whose
   prompt opens with one is the case's defect. The adapter dedupes on the backend message id,
   bounded the way the Chat adapter bounds its seen set: a repeated `POST` with an id it has
   already accepted gets the answer the first one got, the record's task id or the same refusal,
   and starts nothing. Because the id
   carries the run id, the dedupe is idempotence within one invocation's retries and never
   across invocations: a rerun of the same case and repetition mints a new run id, so a new key
   and a fresh session record, and the seen set never answers it with an earlier run's task.
   That is what makes the harness's retry classes safe on this path: the opening request is
   retried with the same
   body on a gateway status or a dropped connection, and `startTask` writes the session record
   before it answers, so without the dedupe the retry would reach `handleInbound` with an active
   task and be routed as a steer, no new task and no `ingress` line; and a fresh key per attempt
   is ruled out because it would start a second persona task doing the same mutations. The
   gateway's `ingress` log joins the backend message id to the `correlationId`, so with the run,
   case and repetition as that id, and the record storing the run id beside the case and
   repetition, the audit chain reaches the eval record with nothing added.
3. Await the terminal of that task id, as [Completion signals](#completion-signals) says.
   Replies arrive the way the relay would post them to a conversation, over SSE or a `GET` on
   the conversation key, and the harness returns when the terminal lands or its deadline passes.
   The deadline on this path is the harness's own budget for the whole task,
   `AGENT_INJECT_TIMEOUT` (default 1800 s), not one request as `AGENT_HTTP_TIMEOUT` bounds on
   the api transport; it is floored at the gateway's first-event grace plus a margin (below),
   and the harness refuses to start below the floor. A timeout reads the conversation's state
   first, through the adapter's read route (below), classifies from what it reads, and then
   cancels whenever that state still holds an active task, whether or not an executor has
   touched it; the cancel goes through an explicit cancel route that takes the task id the
   adapter answered with, whether or not the record still holds it, and lands on the bus as
   `kind: cancel` exactly as the text route's `stop` does, and the harness never sends the stop
   text. The harness sends no message at the deadline, so nothing it does there can mint a
   task or spend a model turn. The failure edit of step 2 can also arrive after the answer,
   when the record showed the active task before the publish failed; the harness is already
   reading the conversation, so it treats that edit as the terminal: infrastructure, the
   gateway failed to publish, and no cancel, because nothing was ever on a subject.
4. Map the `result` artifact's text to the answer the verifiers read (`output` and
   `final_message`); map `activity` and `progress` artifacts into the trajectory when the
   executor publishes them. Token counts are not on the bus, and the scorer's liveness rule
   fails a record whose token total is null, so the inject record is written the way the
   diagnostic transport below writes its own: every lifecycle event of the task, `submitted`,
   `working` and the terminal, is a trajectory entry under the status-event name, the token
   buckets are null, the latency is set, and the scorer's rung-3 rule accepts a `working` or a
   final status entry as liveness evidence in place of a token total; `working` counts because
   a graded timeout, canceled at the budget with the cancel unconfirmed, has no final entry and
   would otherwise block as not a real run, and a `submitted` entry alone still blocks. A task
   nobody took reaches the scorer as infrastructure through the harness's marker, never as a
   record the rung blocks. That
   scorer rule lands with the diagnostic transport, and whichever of the two transports merges
   first brings it.

The adapter binds to localhost or a ClusterIP Service. A NetworkPolicy edge fences it from every
in-cluster pod but the eval runner's path; it does not govern the port-forward the harness uses,
which enters from the node, so the fence is not what keeps the door shut. What keeps it shut is a
bearer token the operator renders into a Secret beside the adapter's env, under the eval flag
only, which the harness reads the way the presubmit reads `API_SERVER_KEY` today. The door it
replaces admits key holders, and this one admits the same population rather than everyone
holding `pods/portforward` in the namespace; that matters because the task it starts runs on the
platform persona with the install's cluster and GitHub credentials, under an `authority` block
the gateway mints for a synthetic principal, past the allowed-users gate.

**The adapter cannot assert a real principal (decided 2026-09-17).** The gateway spec's test-backend
section keeps Discord structurally incapable of asserting a real principal through its mapping
table, and the inject door takes its principal from a request body, so it needs the same
property or it becomes an identity-minting door the day publisher identity arms. Three things
give it that property. The adapter resolves the principal through its own section of the
principal map, found by its prefix; the property is the section's entries, eval identities the
gateway cannot map to a cloud principal, and the prefix is only how the section is found. It
stamps its own `verifiedBy`, `inject-bearer`, so nothing downstream can mistake the block for one
`chat-event-topic-iam` verified. And an unmapped principal is dropped at ingress with the same
notice the Discord path gives (drop, log, no task); nothing is defaulted.

**The synthetic conversation is a session like any other.** Its key is
`inject:<run>/<case>/<rep>`, backend-qualified like `discord:` and `gchat:` keys so it cannot
collide with a real
conversation; its `Kind` is `dm`; its roster is the requester alone with `complete: true`; and
`openDirect` returns the same conversation. Those are the five adapter operations the
test-backend section names, inbound message with verified sender, conversation and thread
identity, roster read, post-to-conversation, and `openDirect`, and the inject adapter implements
all five rather than a subset the session record has to special-case. One more route sits on
the adapter's side of the door and is not a sixth backend operation: a pure read of the
conversation's state that mutates nothing. It returns what the session record holds for the key,
the active task with its `SubmittedAt`, from which the age in the nobody-took-it outcome is
computed, and `Detached`, which the harness reads after its own cancel so it never sends a
second one and grades the timeout from a detached record, the fold of the task's stream, its
state none, `submitted`, `working` or a terminal with the result text, and the last posted
message, plus the gateway's configured grace and the backend the gateway armed; the
infrastructure paragraph below says what the harness does with it.

**The door is not a backend in the one-backend guard's sense (decided 2026-09-17).** The guard in
`a2a/gateway/config.go` exists so a two-backend misconfiguration cannot silently stop consuming
Chat; a localhost door has no silent-stop failure mode, so it is a side door that may sit beside
exactly one real backend, and the guard keeps refusing two real ones. The door beside a real
backend is therefore not a refusal, which is what lets stage 2 run the inject and Chat transports
against one install and compare them, and meets the A2A owner's condition that the guard change
be coordinated with the Slack adapter in flight.
The door alone is also enough for the gateway to start (decided 2026-09-17 by the A2A owner,
whose reason is that this is what makes the adapter the answer for an eval install with no real
backend), and the eval install has none until stage 2 gives it one; the adapter's change makes
the guard say so in code and in the gateway spec's test-backend section. What that trades away is
the guard's no-backend refusal, which on a gateway with the door rendered can no longer tell an
install that wants no real backend from one whose relay URL failed to render. The adapter's
guard change drops only that refusal, only when the door is rendered, and keeps the two-backend
refusal. And the door-alone start is not silent: the gateway logs it and the read route reports
the armed backend as inject-only, so stage 2's preflight reads the armed backend and fails the
run as infrastructure when Chat is not the one, before any case grades. The stage-2 change that
renders the gateway's relay URL adds it to the operator's golden set, which is where a failed
render is caught from then on; until then the eval install has no real backend to lose.

A transport failure is classified as infrastructure with the same marker the api transport uses
for a dead tunnel: the adapter unreachable, the gateway refusing the injection or failing to
publish it, no executor taking the task, or an executor that accepted the task and never
started it. The window for a
task nobody took is the gateway's, not the harness's: `A2A_FIRST_EVENT_GRACE` bounds how long an
active task with nothing on its events subject may hold a conversation, and the never-started
heal in `handleInbound` releases the conversation on its next message with a notice naming the
task. The harness runs no second clock for that, and it does not read the answer off its own.
Its deadline on this path is its own budget for the whole task, `AGENT_INJECT_TIMEOUT`, default
1800 s, floored at the grace plus a margin (a constant the adapter's change owns; 60 s is the
proposal), and the harness refuses to start below the floor. The two clocks measure different
things: the grace says whether anybody took the task, the budget says how long a case may run,
and a deadline set at the grace, as an earlier draft had it, would grade a task's runtime with a
constant that exists to detect an idle bus. The floor is what makes the read at the deadline
unambiguous: the heal compares strictly against a clock `startTask` stamps after the harness's
own started, so a deadline under the grace could read an active task with nothing on its stream
that the gateway has not yet given up on, and a cancel sent to it gets "cancel sent" back with no
terminal ever following. The heal is not a clock either: it runs at the top of `handleInbound`,
before routing, on the next inbound message, and once it has released the conversation that
same message is routed as a new turn, so a status phrase sent to trigger it would start a task
that reads "status". The harness therefore sends no message at the deadline. It reads the
conversation's state through the adapter's read route, a pure read that mutates nothing: the
heal is a write under the per-conversation lock, and a route that performed it from outside
`handleInbound` would be a second writer racing the next inbound message for the record. The
harness classifies from one of six outcomes, by the state the route's fold returns: no active
task and a terminal posted, the run finished as the deadline fired, so it is graded like any
other; an active task with no executor event, nobody took it, infrastructure, whether or not the
gateway has released the conversation yet; an active task at `submitted` and never `working`, an
executor accepted it and queued it for the whole budget, infrastructure; an active task at
`working`, a graded timeout; and an active task whose stream already folds to a terminal, which
is the relay's lost record write, the event acked when the relay enqueued it, before the queued
write cleared `ActiveTask`, and an acked event is never redelivered. That fifth outcome is graded
like the first, with the answer read from the fold rather than the posted message, and gets no
cancel, because there is nothing left to cancel; the record then holds a finished task until
something releases it, which on a key never reused is nothing and costs nothing. The sixth is
the state a failed publish leaves, no active task and no terminal, the fold none and the last
posted message the failure edit; the harness ordinarily met that edit in step 3 long before
the deadline, and at the deadline it grades the same, infrastructure, no cancel. In every other
outcome that leaves an active task, the no-executor one included, the cancel then goes out for
the task id the adapter answered with, whether or not the record still holds it. The
classification comes from the read and never from
the cancel's answer: a cancel sent to a task nobody consumed gets "cancel sent" back and no
terminal follows, so the answer is not evidence, and the cancel is sent anyway because the
submission is durable on the task's `in` subject under the bridge's durable consumer, so a bridge
that first binds inside the stream's retention window would otherwise be handed the stale case
prompt and run it with the install's credentials. What the cancel buys on the bridge as it stands
is a bound, not a clean refusal: the durable consumer delivers serially and acks after the
handler, so the cancel is read only after the submission's accept returns, and by then an idle
worker, which a freshly bound bridge ordinarily has, has taken the run, published `working` and
spawned the stale prompt, so spawn-before-cancel is the common case and `canceled-before-start`
the exception; the cancel then kills it within the bridge's kill grace and the terminal is
`canceled-by-request`, which is the record. `canceled-before-start` is what a task
still queued gets, the `submitted` outcome above. The stage-1 bridge work below therefore
includes a look-ahead that makes the refusal clean. The release still happens on the next real
inbound message, as today, which matters
only if the key is reused, and the harness uses a fresh key per run, case and repetition. That is
how the harness and the gateway agree on what "nobody took it" means, one grace read from the
gateway rather than configured twice, and nothing the harness does at the deadline can mint a
task. A `failed`
terminal is not graded on its state alone, because the bridge publishes `failed` for its own
faults as well as the persona's. The reason rides as the terminal's status message, written
`reason: <token>` or `reason: <token> - <detail>`; the harness strips the `reason:` prefix and
takes the token up to the next space, so `bus-publish-failed at working` reads as
`bus-publish-failed`, and a message without the prefix is an unknown reason. The executors' own
reasons, the bridge's `bridge-shutdown`, `bridge-queue-overflow`, `bus-publish-failed`,
`spawn-failed` and `bridge-died-without-terminal-event` and the worker adapter's `worker-evicted`
and `bus-subscribe-failed` (`spawn-failed` is both), are infrastructure, the class the api
transport gives an exhausted transport retry, because they say the executor lost the task rather
than the persona failing it, the same line the profiles spec draws with `worker-evicted`; the
persona's reasons, `hermes-exited-nonzero` and `deadline-exceeded`, and any reason the harness
does not know, are graded failures. A `rejected` terminal, which both executors publish for a
submission with no text parts, is infrastructure and never graded, because it is the harness's
own defect. A `canceled` terminal after the harness's own cancel is the graded timeout above, and
`canceled-before-start` the infrastructure outcome above. The rule is the same on both
transports: the diagnostic transport below folds the same terminals and classifies them the same
way.

**What it proves.** The NATS StatefulSet is up and reachable; the streams exist, which means the
provisioning Job completed, which means the callout authenticated it; the gateway started,
authenticated to the bus, routed the message, recorded the session and started the task; an
executor is attached to `a2a.tasks.platform.*.in`, authenticated, and consuming; the task was
consumed; the reply came back as lifecycle events with a `result` artifact and reached the
conversation the way the relay posts it. These are the components the measured run had down
while the job stayed green, the gateway among them.

**What it skips.** Chat, Pub/Sub, the relay's pull from the A2A subscription, the allowed-users
gate, an `authority` block that names a real principal, and the reply rendered into the thread.

**Which verifiers work.** `report_contains` reads the answer text and works unchanged.
`resource_property` and `fleet_resource_property` read the cluster and never touched the
transport. `tool_called` reads the trajectory, which on this path has tool-call data only when
the executor publishes `activity` artifacts; the Hermes bridge publishes
status updates and a `result` artifact and no `activity` or `progress` artifacts, while the
worker adapter publishes `activity` and `progress` beside the result, so a case
that gates on `tool_called` has no data on stage 1 until the bridge publishes activity or the
persona moves to the worker path. `worker_commands` reads the kanban worker logs by card id; on
this path it has data only once the case runner's delegation wait is rebuilt for it (Completion
signals), and until then a case that gates on it has no data on stage 1 either.
`ledger_issue_contains` finds the ledger by scanning the final message for a GitHub issue URL, so
it works on any transport that maps a result into the final message, which both new transports
do, and its grade depends on that mapping: the fleet-audit cases get the URL from the delegated
worker's card result, which today's wait folds into the final message, so on this path it has
the URL only once the rebuilt wait appends the delivered card results the same way, and until
then it fails as a graded failure with no issue URL in the report, not as an error.

**The executor is the Hermes persona through the bridge sidecar (decided 2026-09-17).** The
session worker carries only the tool-less `chat` profile; running the platform persona as a
session pod waits on the profile and dispatcher work, which has no date, and the A2A owner set
none for the bridge's retirement: it goes when profiles land and the retirement ordering is
written. Stage 1 builds against the bridge
([`a2a/docs/hermes-bridge.md`](../../a2a/docs/hermes-bridge.md)), a sidecar declared on the CR
through `spec.deployment.sidecars` whose image has no build configuration in this repository. A
case addresses `platform` and does not care who answers; when the persona moves to a worker the
addressee stays `platform`, which is what the addressee token is for. An install under `next`
with no sidecar declared has a bus with nobody consuming `platform` tasks, and every case on the
inject transport ends as infrastructure. That is the correct reading of that install, and it is
why a task nobody took is infrastructure rather than a failed case. The bridge accepts a task by
publishing `submitted` and queues it behind `BRIDGE_CONCURRENCY` workers, default 2, and
publishes `working` only when a worker spawns the subprocess; the presubmit fans units out at
`EVAL_TASK_PARALLELISM`, default 4, the nightly at 6. At those defaults two of every four
concurrent units wait in the bridge's queue carrying an executor event and no subprocess, for as
long as the two ahead of them run. The eval install's sidecar therefore sets
`BRIDGE_CONCURRENCY` to at least `EVAL_TASK_PARALLELISM`, declared with the sidecar on the CR,
and the `submitted`-only classification above is the backstop rather than the fix: a queued
repetition that reaches the deadline is infrastructure, not a failed case, but it has still
spent its budget waiting. Two pieces of stage-1 work follow from building against the bridge,
neither of which exists today: nothing in this repository or the presubmit produces the bridge
image, and nothing declares the sidecar. The bridge doc says the image is fork-built for the
playground, the platform-agent image plus the bridge binary, in neither `images.json` nor the
release pipeline, and that it joins the release surface at stage-2 graduation or dies before it.
Stage 1 therefore adds a bridge Dockerfile beside the three A2A Dockerfiles in `a2a/`, built in
the same step the CI flag section gives the A2A images and tagged per pull request into the pool
project's registry, CI-only and not in `images.json`, consistent with that statement; and
`hack/ci-deploy.sh` under the flag declares the sidecar on the CR through
`spec.deployment.sidecars` with that image, the bus URL and credentials the bridge doc lists as
its env, and `BRIDGE_CONCURRENCY` at or above `EVAL_TASK_PARALLELISM`; and a look-ahead in the
bridge's worker, which before it spawns replays the task's `in` subject for a trailing `cancel`,
the one-subject read `tasks/get` already does on events, and finalizes `canceled-before-start`
when it finds one, so a cancel already in the stream is honoured without a spawn. Whether the
bridge image build lands in this repository, and the look-ahead with it, are the A2A owner's
call, and the open question below.

### The direct-bus transport, kept as a diagnostic

`AGENT_TRANSPORT=a2a` has the harness stand in for the gateway: port-forward the NATS Service
the operator renders for the CR (`<cr>-a2a-nats`, client port 4222), mint `taskId`, `contextId`
and `correlationId`, subscribe to `a2a.tasks.{addressee}.{taskId}.events` and
`a2a.tasks.{addressee}.{taskId}.supervisor` (the pair `tasks/get` folds: the executor's events,
and the terminal the gateway writes as supervisor when an executor dies without one; an install
that took the supervisor split inside the last retention window still holds older supervisor
terminals on `.events`), publish one `message` envelope on `a2a.tasks.{addressee}.{taskId}.in`
(`AGENT_A2A_ADDRESSEE` selects the addressee, default `platform`; the `eval` grant below reaches
`platform` alone, so any other value is refused by the static grant until it widens), fold the
events as `tasks/get`
folds them, and publish `cancel` on the way out of a timeout. The forward enters from the node,
which the NATS ingress NetworkPolicy does not govern, so the fence that admits only enumerated bus
clients in-cluster does not have to name the harness. It proves the bus, the streams and the
executor with the gateway out of the path (not the callout, which never sees a static user),
which is what makes it the tool for showing that the gateway itself is the thing that is down.
It is not the presubmit's transport.

Two conditions on it (decided 2026-09-17). It authenticates as its own `eval` principal, a
static user in the operator's render ([`spec-nats-deployment.md`](spec-nats-deployment.md),
"Accounts and connection-time authorization"), which the operator renders only under
`A2A_EVAL_PRINCIPAL=true`: publish on `a2a.tasks.platform.*.in`, subscribe on the matching
`.events` and `.supervisor`, nothing else. It never holds the gateway's credential, in any case,
and on an install without that flag the transport has no credential it may use. And it leaves
`authority` null and says so: the block is populate-by-gateway-only and advisory until publisher
identity arms
([`spec-chatops-gateway.md`](spec-chatops-gateway.md), "Requester identity on the bus"), so a
harness-invented shape would be a second writer of a field consumers may not decide on.

## Stage 2: Chat ingress

The entry hop moves up to the front door. A case publishes a Chat MESSAGE event on the Pub/Sub
topic, under the runner's Workload Identity, in the layout `tests/e2e/gchat_agent_test.py`
already forges for the release gate; the sender is an identity on the install's allowed-users
list. That forged sender is the impersonation surface `verifiedBy: chat-event-topic-iam` names,
anyone with `pubsub.publisher` on the topic is the sender, and stage 2 depends on that boundary
staying project IAM: the day it is replaced by a per-request proof, stage 2 needs a real sender.
The reply is read from the bus events of the task the gateway opened for that
conversation, or from the Chat thread. The principle prefers the thread, because it is what the
customer sees; the bus events grade one hop short and need no Chat read credential. Which the
harness reads is the eval crew's to decide when the stage is built. The presubmit's cases move
to the customer's door in the same change; the inject adapter stays a dev-only door behind the
eval flag, and the direct-bus transport stays a diagnostic.

What it adds to the proof: the relay pulls the A2A subscription, the gateway authenticates to the
broker with its own audience, the gateway mints the session and the `authority` block, the
allowed-users gate admits the sender, and the reply reaches the thread.

The stage is blocked on product work the status line of
[`spec-chatops-gateway.md`](spec-chatops-gateway.md), which is canonical for this list, names as
not yet rendered by the operator: the Google Chat adapter's env, including the relay URL; the
projected relay token and the `a2a-chat` audience on the broker
(`CREDENTIAL_PROXY_A2A_CHAT_AUDIENCE`); the gateway's
ServiceAccount on `CREDENTIAL_PROXY_ALLOWED_CALLERS`; the broker NetworkPolicy admitting the A2A
gateway pod; the allowed-users set carried to the gateway (`A2A_GCHAT_ALLOWED_USERS`); and the
second Pub/Sub subscription with its IAM, which the composition does not yet provision.

The eval crew owns that operator work (decided 2026-09-17), under three conditions. The change
cites the gateway spec's sections rather than restating them, and leaves the Google Chat
adapter's `verifiedBy: chat-event-topic-iam` and its allowed-users gate exactly as "The Google
Chat adapter" section has them: the gate is the operator-pinned allowed-users set carried as
environment, and the `verifiedBy` value names a project-IAM boundary, not a per-request proof. It
lands in the same change that gives the gateway rendered under `next` the credential proxy's chat
relay URL as its backend, so an install with Google Chat configured has a gateway that starts
without a Discord Secret; a render that drops the relay URL leaves a gateway on the door alone,
which the read route reports as inject-only and the stage's preflight fails as infrastructure
before any case runs, the guard paragraph in stage 1 saying why the guard no longer catches it.
And it settles the one-backend guard with the Slack adapter in flight, so the relay URL beside a
Slack credential is still a refusal and never a collision, while the inject door beside the relay
URL is not one (stage 1, above), so the install this stage wires runs both transports.

Two decisions sit beside that list. The legacy Chat consumer still runs under `next`, and a topic
fans out to every subscription, so an install that arms the A2A subscription beside it answers
twice; the gateway spec leaves the per-install choice of which consumer takes Chat to the mode
switch's per-component override, which does not exist. And the eval install runs with
`GOOGLE_CHAT_ENABLED=false`; enabling it means a Chat app registration and a space per pool
project, because a Chat app configuration is per GCP project.

## Completion signals

Today's wait costs one `kubectl exec` against the kanban store per poll interval and one model
turn per card that settles (plus a turn whenever the store cannot be read or does not know a
card), notices completion only at a poll boundary, and still needs that collecting turn, which
slows down exactly when the agent is rate-limited. A delegated case that waits ten minutes spends
about twenty store reads and one turn.

On the bus a task has a lifecycle the requester can watch: `status-update` events, a `result`
artifact, one terminal event with `final: true`, and `cancel` as a real envelope rather than a
dropped connection. The harness returns when the terminal lands, at whatever second it lands,
and `tasks/get` by replay answers status without a live executor.

The caveat that decides how much of the time cost stage 1 removes: the bridge runs one
`hermes -p platform chat -Q -q <prompt>` per task and publishes its terminal when that turn ends.
Kanban stays the delegation mechanism inside the persona under `next` (decided 2026-09-17).
Agent-initiated delegation as a child task on the bus is designed and not built:
[`spec-subagent-profiles.md`](spec-subagent-profiles.md) has an orchestrator delegate by
publishing a submission on `a2a.tasks.{profile}.{taskId}.in` ("How a profile becomes a pod"), a
dispatcher turning submissions into Jobs, and the child inheriting the parent's `correlationId`
("Thinking and status, back over the bus"); none of it exists on `main`. The gateway's
`delegate:` route ([`spec-chatops-gateway.md`](spec-chatops-gateway.md), "The Delegate flow") is
a different thing: a human-initiated route that hands the conversation to a fresh session
addressee, a sibling of the platform task rather than a child of it, and the persona cannot
invoke it. So when the persona files a card inside the turn, the bridge's terminal says the turn
ended and the card was filed, not that the work is done.

Stage 1 handles that in three parts. The transport awaits the terminal of a named task id,
"await the terminal of task X" rather than "await the task I submitted", for everything the
executor does itself, which is most cases and removes the store reads and the collecting turn. For a terminal whose result
names card ids, the case runner waits for the cards one hop further in, with the time cost above
moved with it. Today's wait cannot be re-entered as it is: it is a method of the api transport
that re-posts `/v1/responses`, takes card ids from `kanban_create` tool results and statuses from
the kanban store or from `kanban_show` payloads in the trajectory, and gives up after three status
turns that report nothing, and
on this path the trajectory holds no tool calls, only the lifecycle entries of step 4. Stage 1
writes the wait again for the inject path: card ids and statuses read from the `result` text, the
status question sent as a new turn on the same conversation key with its own backend message
id, `<run>/<case>/<rep>/status-<n>`, so the dedupe does not answer it with the opening task,
the delivered card results appended to the graded answer as
today's wait appends them, so `ledger_issue_contains` and `report_contains` see what the worker
returned, and the worker logs read by those ids for `worker_commands`. It
lives in the case runner and not in the transport, so it can be deleted without touching the
transport. Reading card ids out of `result` text is interim: a structured artifact for them is
a bridge change outside this document, and the text read is the first thing child tasks delete.
When child tasks exist, the parent's events name the child's task id, the same await code awaits
it, and the wait goes.

## The CI flag

`EVAL_MODE_NEXT=1` in `hack/ci-deploy.sh` flips the presubmit's eval install to `next` after the
today-mode install has passed its own readiness and connectivity checks. It records the agent
Deployment's generation, merge-patches the CR, and waits for the generation to move before
asking any workload for status, because the flip is a rollout and a status read before it lands
describes the old pods. It then gates, in order, on the NATS StatefulSet, the callout Deployment,
the provisioning Job reaching `complete` (the Job depends on the callout and has been measured at
19.5 minutes under adverse conditions, so its bound is generous), and the agent Deployment. It
reports the A2A gateway's state and last log lines and never gates on it: the gateway refuses to
start without a backend, and the eval install has none until the inject adapter is rendered. The
same flag is what the operator renders the inject adapter's env, Service and NetworkPolicy
under; once it does, the gateway starts and the flag can gate on it too. The flag reaches the
operator as an operator environment variable, the pattern the A2A image overrides use, and not
as a CRD field (decided 2026-09-17 by the A2A owner on the adapter's issue): the door maps a
body-supplied principal, and nothing a customer can set on a `PlatformAgent` may render it. The
rendered object set with the flag unset carries no inject Service, env or NetworkPolicy, and that
is a property for the conformance suite to check rather than a comment to trust. The A2A images
the flip needs are built in the same Cloud Build step as the other images and set on the
operator, because the defaults point at a registry the pool projects cannot pull from; the bridge
image is built in that step too, and the flip declares the bridge sidecar on the CR with it (the
executor paragraph in stage 1 says what the sidecar carries), because a flip without the sidecar
leaves a bus on which nobody consumes `platform` tasks. The eval matrix in `hack/ci-eval-pr.sh`
is unchanged.

The flag stays off by default for three reasons. Flipping the shared presubmit install changes
what every pull request measures, and that is the eval crew's decision, not a script default.
The next stack still has holes independent of any case (no resource requests on the NATS,
gateway or provisioning pods, a gateway with no backend until the adapter lands, no executor
until the sidecar is declared, images in a
private registry), and a default-on flip would red every pull request for reasons none of them
caused. And until a case sends through the gateway, a run under `next` measures nothing a run
under `today` does not; the flag exists so the matrix can be run against `next` on demand while
stage 1 lands.

## Open questions

Marked open on purpose; this document does not pick. The first draft's two questions for the A2A
owner, which executor answers `platform` and whether delegation becomes a child task on the bus,
were answered on 2026-09-17 and are recorded above as decisions, as was how the eval flag
reaches the operator (The CI flag). What remains is the eval crew's, and one item the A2A
owner's:

- **Which reply stage 2 grades,** the bus events of the task the gateway opened or the Chat
  thread. The principle prefers the thread; the bus events grade one hop short and need no Chat
  read credential. The eval crew decides when the stage is built.
- **Whether the bridge image build lands in this repository, and the bridge look-ahead with it.**
  Stage 1 needs a bridge image the presubmit can pull, and the bridge doc says no build config
  for it ships here. The proposal in stage 1 is a CI-only Dockerfile beside the A2A ones, outside
  `images.json`, and a pre-spawn look-ahead for a trailing cancel in the bridge's worker, without
  which a late-binding bridge spawns a cancelled submission before killing it; the A2A owner
  decides both.
