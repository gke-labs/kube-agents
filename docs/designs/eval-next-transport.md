# The eval transport under `spec.mode: next`

> **STATUS — draft for review; nothing here is built.** The bench harness has one transport,
> `hack/ci-deploy.sh` has no mode flag, and the presubmit install runs `today`. The measurement
> that motivates the document is on gke-labs/kube-agents#1661; the presubmit run it cites is
> build `2100310325382352896`.

**Scope:** how a bench case reaches the agent when the install runs the next stack, in two
stages, and which questions are still open.
**Owns:** the transport principle, the stage boundaries, and the list of what each stage proves.
The harness itself is `bench/kube_agents_bench/harness.py`; the wire contract is
[`spec-a2a-payloads.md`](spec-a2a-payloads.md); the gateway is
[`spec-chatops-gateway.md`](spec-chatops-gateway.md); the bus deployment is
[`spec-nats-deployment.md`](spec-nats-deployment.md); the switch is
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

## Stage 1: the bus transport

The harness stands in for the gateway. Selected by `AGENT_TRANSPORT=a2a`; unset, or `api`, is
today's transport byte for byte, and the presubmit exports nothing new until it chooses to.

The exchange, in the gateway's own shape:

1. Port-forward the NATS Service the operator renders for the CR (`<cr>-a2a-nats`, client
   port 4222) on `AGENT_CLUSTER_CONTEXT`, through the harness's existing forward machinery and
   its retry classes. The forward enters from the node, which the NATS ingress NetworkPolicy does not
   govern, so the fence that admits only enumerated bus clients in-cluster does not have to name
   the harness.
2. Authenticate as the static `gateway` user, whose password the operator writes to
   `<cr>-a2a-nats-creds`. The NATS spec lists that user as a residue awaiting the callout; when it
   moves, the harness needs an identity of its own or stage 2 has removed the need.
3. Mint `taskId`, `contextId` and `correlationId` as the gateway does, subscribe to
   `a2a.tasks.{addressee}.{taskId}.events` and the supervisor subject, then publish one `message`
   envelope on `a2a.tasks.{addressee}.{taskId}.in` with the prompt as its single text part and
   `to` set to the addressee. `AGENT_A2A_ADDRESSEE` selects the addressee; the default is
   `platform`.
4. Fold status and artifact updates as `tasks/get` folds them and return when the terminal
   `status-update` lands or `AGENT_HTTP_TIMEOUT` elapses, publishing `cancel` on the way out of a
   timeout. On this path that variable bounds the whole task, not one request as it does on the
   api transport. No model turn is spent on status.
5. Map the `result` artifact's text to the answer the verifiers read (`output` and
   `final_message`); map `activity` and `progress` artifacts into the trajectory when the
   executor publishes them. Token counts are not on the bus; the record says so rather than
   failing.

A transport failure is classified as infrastructure with the same marker the api transport uses
for a dead tunnel: NATS unreachable, no `TASKS` stream, authentication refused, or no consumer
taking the task within a bounded window. A task a consumer took and finished with a `failed`
terminal is a graded failure.

**What it proves.** The NATS StatefulSet is up and reachable; the streams exist, which means the
provisioning Job completed, which means the callout authenticated it; an executor is attached to
`a2a.tasks.platform.*.in`, authenticated, and consuming; the task was consumed; the reply came
back as lifecycle events with a `result` artifact. These are exactly the components the measured
run had down while the job stayed green.

**What it skips.** Chat, Pub/Sub, the relay, the gateway's session registry and routing, the
`authority` block, the allowed-users gate, and the reply rendered into the thread.

**Which verifiers work.** `report_contains` reads the answer text and works unchanged.
`resource_property`, `fleet_resource_property` and `ledger_issue_contains` read the cluster and
GitHub and never touched the transport. `tool_called` reads the trajectory, and `worker_commands`
reads the kanban worker logs; on the bus path both have data only when the executor publishes
`activity` artifacts. The Hermes bridge publishes `result` alone; the worker adapter publishes
`activity` and `progress` beside it. A case that gates on either verifier therefore depends on
which executor answers, which is the first open question below.

**An executor has to be there.** On `main` the only executor for the addressee `platform` is the
Hermes bridge ([`a2a/docs/hermes-bridge.md`](../../a2a/docs/hermes-bridge.md)), a sidecar declared
on the CR through `spec.deployment.sidecars` whose image has no build configuration in this
repository. An install under `next` with no sidecar declared has a bus with nobody consuming
`platform` tasks, and every case on the a2a transport ends as infrastructure. That is the correct
reading of that install, and it is why the transport reports a task nobody took as
infrastructure rather than as a failed case.

## Stage 2: Chat ingress

The entry hop moves up to the front door. A case publishes a Chat MESSAGE event on the Pub/Sub
topic, under the runner's Workload Identity, in the layout `tests/e2e/gchat_agent_test.py`
already forges for the release gate; the sender is an identity on the install's allowed-users
list. The reply is read from the bus events of the task the gateway opened for that
conversation, or from the Chat thread. The principle prefers the thread, because it is what the
customer sees; the bus events grade one hop short and need no Chat read credential. Which the
harness reads is the eval crew's to decide when the stage is built. The stage-1 shortcut is
deleted in the same change: one transport, the customer's.

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
second Pub/Sub subscription with its IAM, which the composition does not yet provision. Two
decisions sit beside that list. The legacy Chat consumer still runs under `next`, and a topic
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
If the platform persona still delegates by filing a kanban card inside the turn, the terminal
says the card was filed, not that the work is done, and the harness is polling again one hop
further in. What stage 1 does after such a terminal turns on the second open question below. If
delegation becomes a child task on the bus, the harness awaits the child's terminal and the poll
is not ported. If kanban stays the delegation mechanism under `next`, delegating cases complete
only if the harness re-enters the kanban poll when the result names card ids, with the time cost
described above moved one hop further in.

## The CI flag

`EVAL_MODE_NEXT=1` in `hack/ci-deploy.sh` flips the presubmit's eval install to `next` after the
today-mode install has passed its own readiness and connectivity checks. It records the agent
Deployment's generation, merge-patches the CR, and waits for the generation to move before
asking any workload for status, because the flip is a rollout and a status read before it lands
describes the old pods. It then gates, in order, on the NATS StatefulSet, the callout Deployment,
the provisioning Job reaching `complete` (the Job depends on the callout and has been measured at
19.5 minutes under adverse conditions, so its bound is generous), and the agent Deployment. It
reports the A2A gateway's state and last log lines and never gates on it: the gateway crash-loops
without a chat backend, and the eval install has none until stage 2. The A2A images the flip
needs are built in the same Cloud Build step as the other images and set on the operator, because
the defaults point at a registry the pool projects cannot pull from. The eval matrix in
`hack/ci-eval-pr.sh` is unchanged.

The flag stays off by default for three reasons. Flipping the shared presubmit install changes
what every pull request measures, and that is the eval crew's decision, not a script default.
The next stack still has holes independent of any case (no resource requests on any A2A pod, a
gateway that needs a chat backend, images in a private registry), and a default-on flip would
red every pull request for reasons none of them caused. And until a case sends through the bus,
a run under `next` measures nothing a run under `today` does not; the flag exists so the matrix
can be run against `next` on demand while stage 1 lands.

## Open questions

Marked open on purpose; this document does not pick. Both are with the A2A owner, @bnaylor, on
the measurement record.

- **Which executor answers a customer addressed to `platform` under `next`?** The Hermes bridge
  is the persona the cases grade today, documented as scaffolding with a demolition date. The
  gateway-spawned session worker is the end-state executor and a different agent from the one
  the cases grade, and it is the one that publishes `activity` artifacts. Stage 1 is written to
  be executor-agnostic, but which verifiers have data, and whether the stage-1 numbers say
  anything about the shipped product, both turn on this answer, as does whether stage 1 builds
  against the bridge or waits for the worker path to carry the platform persona.
- **Does delegation from the platform persona become a child task on the bus,** with its own
  terminal the requester can await, and if so on the bridge path or only on the session-worker
  path? The gateway's delegate flow spawns a worker for a turn a user prefixes, which is a user
  affordance rather than agent-initiated delegation. The answer decides what stage 1 does after
  a terminal whose result names card ids: await the child task's terminal, or re-enter the kanban
  poll one hop further in and keep its time cost.
