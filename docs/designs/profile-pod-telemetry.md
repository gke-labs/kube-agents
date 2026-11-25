# Telemetry and Log Shipping for Profile Pods

> **STATUS: draft for a decision.** Nothing here ships. This is the telemetry design for the epic
> [#6526](https://github.com/gke-labs/kube-agents/issues/6526), for its group
> [#6674](https://github.com/gke-labs/kube-agents/issues/6674). Each network rule, port and record
> below needs the decision of the security approver before any code, and the approver confirms
> the "Done when" list here.

The platform agent's pod ships its logs, a record for each tool call and each message, and its
traces. A profile pod has no log shipper, no OpenTelemetry variable and no exporter
([`platform-as-profile-inventory.md`](platform-as-profile-inventory.md), blocker 15). So work that
moves to a profile becomes less visible than the same work on the platform agent. This document
has one row for each signal, says what profile work has today, and proposes the counterpart.

## What was checked

Checked in the code on `main` at `be60d590d`, by the author and by a second read in a clean
context: each statement of the two "today" sections and of "What a profile pod can forge". Not
checked: a running install, the inside of the `hermes_otel` plugin, the collector that GKE
manages, and whether the model's process can write to the stderr of its container.

## What the platform agent's pod ships today

| Signal                      | What makes it                                                                                                                                                              | Where it goes                                                                                                                                                                                                                          |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Log lines                   | Hermes writes log files on the data volume. A `fluent-bit` sidecar, always on, tails the `logs` folder of the front door.                                                  | The sidecar writes each line to its own stdout as JSON. It has no cloud output: the node log agent ships that stdout. Its cursors are on the `system-metadata` volume.                                                                 |
| A record for each tool call | The `tool_call_audit` plugin, in the agent's own process: a start and an end record with the arguments, the result and the duration, masked and cut at 2000 characters.    | The `logs/audit.jsonl` of each Hermes profile, which the sidecar tails with a JSON parser. The Admin Console reads these records from the agent's namespace.                                                                           |
| A record for each message   | The `chat_message_audit` hook: three events for a message, cut at 4000 characters.                                                                                         | The same file.                                                                                                                                                                                                                         |
| Traces                      | The `hermes_otel` plugin exports spans for a turn, its tool calls and its MCP calls. A boot step points it at the endpoint. A second plugin adds the user and the session. | An endpoint from a ladder: the deployment's variable, `spec.telemetry.otlpEndpoint`, a variable of the operator, then discovery of the collector that GKE manages. With no collector found, the export is off. We render no collector. |
| Metrics                     | Ports on the gateway pod, the chat process, the broker, the A2A gateway and others.                                                                                        | The chart renders one PodMonitoring for each. The health rules read operator gauges, one gateway gauge and `up`. No rule reads a log.                                                                                                  |

## What profile work ships today

| Signal                    | What exists                                                                                                                                                                                                                                                                                                                                                          |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The pod                   | One container, `worker`, and init containers on some installs, in the namespace `<agent namespace>-profiles`. No sidecar. Its one port serves `/readyz` and `/healthz`.                                                                                                                                                                                              |
| The network               | The NetworkPolicy of the namespace permits no ingress. It permits egress only to DNS, the bus, the inference gateway and the broker.                                                                                                                                                                                                                                 |
| Log lines of the worker   | The worker adapter writes JSON lines on stderr. A line for a task has the key `task`. A script run writes one line with the script, the exit code and the time. Of the harness's stderr, the adapter keeps the last 2048 bytes, for a failure reason.                                                                                                                |
| Log lines of other parts  | The dispatcher writes a line for each task with `profile`, `tier`, `kind` and `task`. The gateway writes an ingress line and a task-end line with `taskId`. The key for the task has three names today: `task`, `taskId`, and `task_id` in the audit records.                                                                                                        |
| Tool calls                | The adapter publishes the tool and its input as an `activity` artifact on the bus, at the start of a call, best effort. There is no end event, no result and no duration. A script run publishes none. The gateway, the relay and the swarm tools read it.                                                                                                           |
| How long the bus keeps it | The stream `TASKS` keeps at most 72 hours, 8 GiB and 4096 messages for each subject.                                                                                                                                                                                                                                                                                 |
| The join key              | Each envelope has a `correlationId`, which the gateway makes once and each hop copies. [`spec-a2a-payloads.md`](spec-a2a-payloads.md) calls it authoritative. The envelope's `traceparent` is optional, and no caller sets it.                                                                                                                                       |
| Traces                    | None from the pod: the `a2a` module has no OpenTelemetry dependency, and the Claude harness runs with its own telemetry off. The inference gateway has spans and metrics for each model call.                                                                                                                                                                        |
| Metrics                   | None on the pod. The dispatcher has no metrics port. The schedule controller counts runs and skips for each schedule. The gateway's task counter counts a delegation chain one time, under its root, so a delegated profile task has no count of its own.                                                                                                            |
| Credentialed calls        | The broker writes an audit record for a call on the exec route, the Cloud API relay and the feedback route: the tool, a label for the subcommand, the result status and the caller, never the arguments. The version-control route writes a metric and no record. The MCP relay writes two plain lines. The profile and the task are inside the text of `principal`. |

## What a profile pod can forge

A profile pod runs a model on text that a tenant can write. Three facts limit what its own
signals are worth:

- The pod's bus connection can publish on the events of each task of its profile, until the
  worker has a connection for each task ([#3889](https://github.com/gke-labs/kube-agents/issues/3889)).
  So an `activity` artifact can come from another task's pod of the same profile.
- The harness and the adapter run with one user id in one container. We did not test whether the
  harness can write a line to the container's stderr. This design does not depend on a "no".
- The platform's own tool record has the same limit: the agent's process writes it.

So nothing that a profile pod writes is a record of authority. The broker's record is the only
one that the pod cannot write. A reader that must trust a line filters on the namespace and the
container, which the node log agent sets, and not on a key of the payload.

## The options

- **A. No new rule in the profile namespace.** Each signal leaves the pod on a path that exists:
  stderr for the node log agent, and the bus. Other parts carry the metrics and the spans.
- **B. The platform's form on each profile pod.** The operator sets the `OTEL_*` variables with
  the same ladder, the worker gets an exporter, and the namespace gets an egress rule to the
  collector. The worker gets a metrics port, a PodMonitoring and an ingress rule.
- **C. One exporter on the bus.** A consumer of the `TASKS` stream, outside the profile namespace,
  turns the activity artifacts into records.

Our recommendation is A. Each rule in the profile namespace is a grant that a prompt injection
can use, and A adds none. C adds a reader of the bus that inherits the limits of the stream (72
hours, 4096 messages, best effort) and writes what a pod can forge or flood into a second log. B
gives the most detail, and it is the form to take only if the approver wants spans from inside
the worker.

The issue asks for telemetry that the operator declares for each profile. Under A the operator
declares nothing on a profile pod: each signal comes from a path that each profile already has.
B is the option that declares it for each profile.

## The proposal, signal by signal

| Signal                 | Proposed counterpart                                                                                                                                                                                                 | Grant that it needs                                                                                                                     | What changes                                          |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| Log lines              | The adapter's stderr through the node log agent, with no sidecar. The platform's sidecar exists only because Hermes writes files. Each line gets the run keys.                                                       | None.                                                                                                                                   | The adapter's logger.                                 |
| The harness's stderr   | Stays a 2048-byte tail in the failure reason. It can hold tenant text.                                                                                                                                               | None.                                                                                                                                   | Nothing.                                              |
| Credentialed calls     | The broker's audit record, on each route that a profile can call: also the version-control route and the MCP relay. It gets keys of its own for the profile, the task and the trigger, from the verified credential. | None.                                                                                                                                   | The broker's audit record.                            |
| The activity of a task | The adapter writes each activity entry also as a log line. It is a trail of what the worker says it did, with the limits of "What a profile pod can forge". See the next section.                                    | None.                                                                                                                                   | The adapter.                                          |
| Messages               | No new record. The task's messages stay on the bus, and the delivery of a result has its own records.                                                                                                                | None.                                                                                                                                   | Nothing.                                              |
| The join of one run    | The `correlationId` and the task id in each log line and each record. This needs no exporter.                                                                                                                        | None.                                                                                                                                   | The adapter, the dispatcher, the gateway, the broker. |
| Spans                  | Spans for a task and its hops, from the gateway and the dispatcher. No span from inside the worker (question 1).                                                                                                     | An egress rule for those two to the collector. The A2A gateway's egress policy has none today. The dispatcher's policy was not checked. | The gateway, the dispatcher, the operator's policies. |
| Metrics                | A counter and a duration at the dispatcher, by profile, tier, kind and end state. The dispatcher gets a metrics port and a PodMonitoring. No scrape of profile pods, and no change of the gateway's task counter.    | A scrape of the dispatcher.                                                                                                             | The dispatcher, the chart's monitoring template.      |
| Health rules           | One rule from the dispatcher's counter: the share of failed task ends for each profile.                                                                                                                              | None.                                                                                                                                   | The chart's health rules.                             |

The gateway's task counter does not change, for two reasons: its series come from closed lists,
and the usage poller of the operator reads it.

### The run keys

A person who looks for one run must find it in each signal. We propose that each log line and
each record of a task carries the task id and the `correlationId`, each under one name in each
part, with the profile, the tier and the trigger's origin and name. A metric carries only the
labels with a closed or small set of values: profile, tier, kind, end state and the trigger's
origin. The broker takes each key from the verified task credential, never from a value that the
pod sends. The requester keys follow
[`audit-logging-user-attribution.md`](audit-logging-user-attribution.md).

### The activity of a task

The activity entry is the tool and its input today. The work in
[#6796](https://github.com/gke-labs/kube-agents/pull/6796) makes it the tool and a short title,
with a scrub at the source and no arguments. This design assumes that form. Then one scrub, at
the source, serves the bus and the log line, and the log line has nothing more to mask.

What the trail is and is not:

- It is the worker's own word. It has the trust of the platform's tool record, and it is not an
  audit record. It does not use the `audit_event` key, so no reader takes it for one.
- It has the start of a call only. The end, the result and the duration need an end event, and
  where an event of a tool call comes from in a profile worker is a question of the hooks group
  ([#6671](https://github.com/gke-labs/kube-agents/issues/6671)), not of this document.
- A script run has its own line and no activity entry.
- The Admin Console reads audit records from the agent's namespace. Profile work does not show
  there under this proposal (question 4).

### What the platform has and this proposal does not give

- Spans for each tool call and each MCP call, and the user and session attributes on them. They
  need spans from inside the worker.
- The arguments and the result of a tool call in a record.
- A record for each message.

[`spec-nats-deployment.md`](spec-nats-deployment.md) says that the bus client library makes a
publish and a consume span with the trace context. That is not built. In a worker it also needs an
exporter and an egress rule, so it is part of question 1.

## The open fact

Does the node log agent ship the stderr of a profile pod? The published telemetry page says that
the agent ships the logs of all containers, and it names no profile pod. One read of the log
store of a live install found no line from a profile namespace in two days. It also found no pod
event in such a namespace, so most probably no profile pod ran there in that time. The fact stays
open until a read on an install where a profile pod ran.

If the agent ships those lines, the log row needs no new object. If it does not, option A needs a
decision: a sidecar in the profile pod with its own output, or a log path through the bus.

## What stays out

- Usage measurement: the usage counters and their poller, the chat metrics, the outcome counts
  and the attribution tag. One fact for its owners: the counters that read audit records do not
  see profile work.
- The hook and plugin mechanism for profile workers (#6671).
- The sidecar, the plugins and the hook of the platform agent's pod. They stay until that pod
  retires.
- A collector of our own, and collectors outside GKE. The endpoint ladder stays as it is.
- Dashboards, and the delivery of results to chat.
- Any change of code. This document changes none.

## Questions

For the security approver:

1. Spans: none, spans from the gateway and the dispatcher with one egress rule for them, or
   also spans from inside the worker with an exporter and an egress rule in the profile
   namespace?
2. Is the broker's record the record of authority for profile work, and does it get a record on
   the version-control route and the MCP relay?
3. Is the activity trail on stderr wanted, given that it is the worker's own word? The other
   choices are no trail outside the bus, or an exporter on the bus (option C).
4. Does the Admin Console show profile work, and from which record?
5. Is a scrape of the dispatcher right, or must a profile pod have a scrape target of its own?
6. Which keys must each signal carry? May the trigger's name be a key of a log line?
7. How long must a line of profile work stay? The bus keeps at most 72 hours; a log line stays as
   long as the log bucket keeps it.

## Done when

The security approver confirms this list in this document.

For #6674:

- Each signal of the platform agent's pod has a decision in this document: a profile
  counterpart, or "not needed" with a reason.
- Each log line of the adapter, the dispatcher and the gateway for a task, and each broker
  record, carries the task id and the `correlationId`, with a test for each part.
- A call of a profile task on each broker route that a profile can call has a broker audit
  record with keys for the profile and the task, with a test.
- The dispatcher exports a task counter by profile, tier, kind and end state, and one health
  rule reads it.
- The NetworkPolicy of the profile namespace has no new rule, or each new rule is named here with
  the approver's decision and has a conformance row.
- The open fact has an answer from an install where a profile pod ran.

What these lines need from other work: the broker lines change `credential_proxy.py`, so they take
their turn in the order of work on that file. The activity trail assumes the entry form of #6796.
