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
the harness re-prompts the same conversation every `AGENT_DELEGATION_POLL_INTERVAL` seconds (30
by default) with an instruction to call `kanban_show` on the outstanding ids, until every card
reads done, blocked or archived or `AGENT_DELEGATION_TIMEOUT` elapses (1800 s by default, 2700 s
in the presubmit). The delivered card results are appended to the answer, and the worker's
report and terminal commands are read back with `kubectl exec` from
`/opt/data/kanban/attachments/<id>/` and `/opt/data/kanban/logs/<id>.log` in the agent pod, then
deleted. A customer sees the card result relayed to their thread; they never see the files, and
the poll turns are model calls the customer never made.

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
direct-bus transport the first draft of this document proposed proves the bus, the callout, the
streams and the executor, but it leaves out the gateway's routing, its session registry and the
relay back, and it hands a second process the one credential that may publish on `.in`. Stage 1
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
   the inject section of its principal map (below), the prompt as the text, and the case id
   plus repetition as the backend message id. The gateway takes the message through
   `handleInbound` like a message from any backend: routing, the session record, `startTask`,
   and the relay back, with `taskId`, `contextId`, `correlationId` and the `authority` block
   minted by the gateway. The response names the task id. The gateway's `ingress` log joins
   the backend message id to the `correlationId`, so with the case and repetition as that id
   the audit chain reaches the eval record with nothing added.
3. Await the terminal of that task id, as [Completion signals](#completion-signals) says.
   Replies arrive the way the relay would post them to a conversation, over SSE or a `GET` on
   the conversation key, and the harness returns when the terminal lands or its deadline passes.
   The deadline on this path is the gateway's first-event grace plus a margin (below), never
   `AGENT_HTTP_TIMEOUT` alone, and it bounds the whole task, not one request as that variable
   does on the api transport. A timeout reads the conversation's state first, through the
   adapter's read route (below), and cancels only once that state shows an executor took the
   task; the cancel goes through an explicit cancel route on the conversation, which lands on the
   bus as `kind: cancel` exactly as the text route's `stop` does, and the harness never sends the
   stop text. The harness sends no message at the deadline, so nothing it does there can mint a
   task or spend a model turn.
4. Map the `result` artifact's text to the answer the verifiers read (`output` and
   `final_message`); map `activity` and `progress` artifacts into the trajectory when the
   executor publishes them. Token counts are not on the bus; the record says so rather than
   failing.

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
give it that property. The adapter resolves the principal through its own prefixed section of
the principal map, whose entries map to eval-only identities and never to a cloud principal. It
stamps its own `verifiedBy`, `inject-bearer`, so nothing downstream can mistake the block for one
`chat-event-topic-iam` verified. And an unmapped principal is dropped at ingress with the same
notice the Discord path gives (drop, log, no task); nothing is defaulted.

**The synthetic conversation is a session like any other.** Its key is `inject:<key>`,
backend-qualified like `discord:` and `gchat:` keys so it cannot collide with a real
conversation; its `Kind` is `dm`; its roster is the requester alone with `complete: true`; and
`openDirect` returns the same conversation. Those are the five adapter operations the
test-backend section names, inbound message with verified sender, conversation and thread
identity, roster read, post-to-conversation, and `openDirect`, and the inject adapter implements
all five rather than a subset the session record has to special-case. One more route sits on
the adapter's side of the door and is not a sixth backend operation: a pure read of the
conversation's state that mutates nothing. It returns what the session record holds for the key,
the active task with its `SubmittedAt` and `Detached`, whether any executor event exists on the
stream, and the last posted message, plus the gateway's configured grace and the backend the
gateway armed; the infrastructure paragraph below says what the harness does with it.

**The door is not a backend in the one-backend guard's sense (decided 2026-09-17).** The guard in
`a2a/gateway/config.go` exists so a two-backend misconfiguration cannot silently stop consuming
Chat; a localhost door has no silent-stop failure mode, so it is a side door that may sit beside
exactly one real backend, and the guard keeps refusing two real ones. The door beside a real
backend is therefore not a refusal, which is what lets stage 2 run the inject and Chat transports
against one install and compare them, and the Slack adapter in flight agrees on the same reading.
The door alone is also enough for the gateway to start (decided 2026-09-17 by the A2A owner,
whose reason is that this is what makes the adapter the answer for an eval install with no real
backend), and the eval install has none until stage 2 gives it one; the adapter's change makes
the guard say so in code and in the gateway spec's test-backend section. What that trades away is
the guard's no-backend refusal, which on a gateway with the door rendered can no longer tell an
install that wants no real backend from one whose relay URL failed to render. The adapter's
guard change drops only that refusal, only when the door is rendered, and keeps the two-backend
refusal. And the door-alone start is not silent: the gateway logs it and the read route reports
the armed backend as inject-only, so stage 2's preflight reads the armed backend and fails the
run as infrastructure when Chat is not the one, before any case grades; the operator's render of
the relay URL is covered by its golden tests, which is where a failed render is caught.

A transport failure is classified as infrastructure with the same marker the api transport uses
for a dead tunnel: the adapter unreachable, the gateway refusing the injection, or no executor
taking the task. The window for that last one is the gateway's, not the harness's:
`A2A_FIRST_EVENT_GRACE` bounds how long an active task with nothing on its events subject may
hold a conversation, and the never-started heal in `handleInbound` releases the conversation on
its next message with a notice naming the task. The harness runs no second clock, and it does
not read the answer off its own. Its deadline on this path is the grace plus a margin, a constant
the adapter's change owns (60 s is the proposal), and never `AGENT_HTTP_TIMEOUT` alone: the api
default of that variable and the grace default are both ten minutes, and the heal compares
strictly against a clock `startTask` stamps after the harness's own started, so a deadline at the
grace sends a cancel to a task nobody consumes, gets "cancel sent" back, and no terminal ever
follows. The heal is not a clock either: it runs at the top of `handleInbound`, before routing,
on the next inbound message, and once it has released the conversation that same message is
routed as a new turn, so a status phrase sent to trigger it would start a task that reads
"status". The harness therefore sends no message at the deadline. It reads the conversation's
state through the adapter's read route, a pure read that mutates nothing: the heal is a write
under the per-conversation lock, and a route that performed it from outside `handleInbound` would
be a second writer racing the next inbound message for the record. The harness classifies from
one of four outcomes: no active task and a terminal posted, the run finished as the deadline
fired, so it is graded like any other; an active task with no executor event and an age inside
the grace the route returns, the gateway's clock has not reached the grace yet, so wait the
margin and read once more; an active task with executor events, a graded timeout, and only now
does the cancel go out; an active task with no executor event and an age past the grace, nobody
took it, infrastructure, whether or not the gateway has released the conversation yet. The
release still happens on the next real inbound message, as today, which matters only if the key
is reused, and the harness uses a fresh key per case and repetition. The cancel follows the read
rather than preceding it, because the read is what says whether an executor holds the task: a
cancel sent to a task nobody consumed gets "cancel sent" back and no terminal ever follows. That
is how the harness and the gateway agree on what "nobody took it" means, one grace read from the
gateway rather than configured twice, and nothing the harness does at the deadline can mint a
task. A task an executor took and finished with a `failed` terminal is a graded failure.

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
transport. `tool_called` reads the trajectory, which on this path
has data only when the executor publishes `activity` artifacts; the Hermes bridge publishes
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
why a task nobody took is infrastructure rather than a failed case.

### The direct-bus transport, kept as a diagnostic

`AGENT_TRANSPORT=a2a` has the harness stand in for the gateway: port-forward the NATS Service
the operator renders for the CR (`<cr>-a2a-nats`, client port 4222), mint `taskId`, `contextId`
and `correlationId`, subscribe to `a2a.tasks.{addressee}.{taskId}.events` and
`a2a.tasks.{addressee}.{taskId}.supervisor` (the pair `tasks/get` folds: the executor's events,
and the terminal the gateway writes as supervisor when an executor dies without one; an install
that took the supervisor split inside the last retention window still holds older supervisor
terminals on `.events`), publish one `message` envelope on `a2a.tasks.{addressee}.{taskId}.in`
(`AGENT_A2A_ADDRESSEE` selects the addressee, default `platform`; the `eval` grant below reaches
`platform` alone, so any other value is refused at the callout until the grant widens), fold the
events as `tasks/get`
folds them, and publish `cancel` on the way out of a timeout. The forward enters from the node,
which the NATS ingress NetworkPolicy does not govern, so the fence that admits only enumerated bus
clients in-cluster does not have to name the harness. It proves the bus, the callout, the streams
and the executor with the gateway out of the path, which is what makes it the tool for showing
that the gateway itself is the thing that is down. It is not the presubmit's transport.

Two conditions on it (decided 2026-09-17). It authenticates as its own `eval` principal in the
identity map the callout reads ([`spec-nats-deployment.md`](spec-nats-deployment.md), "Accounts
and connection-time authorization"): publish on `a2a.tasks.platform.*.in`, subscribe on the
matching `.events` and `.supervisor`, nothing else. It never holds the gateway's credential, in
any case, and until
that row is rendered the transport has no credential it may use. And it leaves `authority` null
and says so: the block is populate-by-gateway-only and advisory until publisher identity arms
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

Today's wait costs one model turn per poll interval, notices completion only at a poll boundary,
and slows down exactly when the agent is rate-limited, because the status question is itself a
model call the agent has to answer. A delegated case that waits ten minutes spends about twenty
turns asking.

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
executor does itself, which is most cases and removes the poll turns. For a terminal whose result
names card ids, the case runner waits for the cards one hop further in, with the time cost above
moved with it. Today's wait cannot be re-entered as it is: it is a method of the api transport
that re-posts `/v1/responses`, takes card ids from `kanban_create` tool results and statuses from
`kanban_show` payloads in the trajectory, and gives up after three turns that report nothing, and
on this path the bridge publishes no trajectory. Stage 1 writes the wait again for the inject
path: card ids and statuses read from the `result` text, the status question sent as a new turn
on the same conversation key, the delivered card results appended to the graded answer as
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
operator, because the defaults point at a registry the pool projects cannot pull from. The eval
matrix in `hack/ci-eval-pr.sh` is unchanged.

The flag stays off by default for three reasons. Flipping the shared presubmit install changes
what every pull request measures, and that is the eval crew's decision, not a script default.
The next stack still has holes independent of any case (no resource requests on the NATS,
gateway or provisioning pods, a gateway with no backend until the adapter lands, images in a
private registry), and a default-on flip would red every pull request for reasons none of them
caused. And until a case sends through the gateway, a run under `next` measures nothing a run
under `today` does not; the flag exists so the matrix can be run against `next` on demand while
stage 1 lands.

## Open questions

Marked open on purpose; this document does not pick. The first draft's two questions for the A2A
owner, which executor answers `platform` and whether delegation becomes a child task on the bus,
were answered on 2026-09-17 and are recorded above as decisions, as was how the eval flag
reaches the operator (The CI flag). What remains is the eval crew's:

- **Which reply stage 2 grades,** the bus events of the task the gateway opened or the Chat
  thread. The principle prefers the thread; the bus events grade one hop short and need no Chat
  read credential. The eval crew decides when the stage is built.
