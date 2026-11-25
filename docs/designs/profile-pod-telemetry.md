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
context: each statement about today's code in this document. Not checked: a running install,
apart from the reads of "The open fact"; the code of the `hermes_otel` plugin, which is not in
this repository; the collector that GKE manages; whether the harness can write to the stderr of
its container; and the network policy of the dispatcher.

## What the platform agent's pod ships today

| Signal                      | What makes it                                                                                                                                                                                                                                                                            | Where it goes                                                                                                                                                                                                                                                                                                                                                                                                   |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Log lines                   | Hermes writes log files on the data volume. A `fluent-bit` sidecar, always on, tails the `logs` folder of the front door.                                                                                                                                                                | The sidecar writes each line to its own stdout as JSON. It has no cloud output: the node log agent ships that stdout. Its cursors are on the `system-metadata` volume.                                                                                                                                                                                                                                          |
| A record for each tool call | The `tool_call_audit` plugin, in the platform agent's own process: a start and an end record with the arguments, the result and the duration, masked and cut at 2000 characters. The same plugin writes a record for each approval request and answer, and one for each inbound message. | The `logs/audit.jsonl` of each Hermes profile, which the sidecar tails with a JSON parser. The Admin Console reads these records from the platform agent's namespace.                                                                                                                                                                                                                                           |
| A record for each message   | The `chat_message_audit` hook: three events for a message, cut at 4000 characters.                                                                                                                                                                                                       | The same file.                                                                                                                                                                                                                                                                                                                                                                                                  |
| Traces                      | The `hermes_otel` plugin exports spans for a turn, its tool calls and its MCP calls (from the observability page; the plugin's code is not in this repository). A boot step points it at the endpoint. A second plugin adds the user and the session.                                    | An endpoint from a ladder: the deployment's variable, `spec.telemetry.otlpEndpoint`, a variable of the operator, then discovery of a collector Service in the cluster (the collector that GKE manages first). If a probe finds none, the export is off. If discovery is off, or a probe does not complete and no earlier result exists, the endpoint is the collector that GKE manages. We render no collector. |
| Metrics                     | Ports on the platform agent's pod, the chat process, the broker, the A2A gateway and others.                                                                                                                                                                                             | The chart renders one PodMonitoring for each. The health rules read operator gauges, one gauge of the A2A gateway and `up`. No rule reads a log.                                                                                                                                                                                                                                                                |

The issue says that health rules read the pod's logs and traces. In the code, no rule reads a log
or a trace.

## What profile work ships today

| Signal                    | What exists                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The pod                   | One container, `worker`, and init containers on some installs, in the namespace `<agent namespace>-profiles`. No sidecar. Its one port serves `/readyz` and `/healthz`.                                                                                                                                                                                                                                                                                                                                                                                                                |
| The network               | The NetworkPolicy of the namespace permits no ingress. It permits egress only to DNS, the bus, the inference gateway and the broker.                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| Log lines of the worker   | The worker adapter writes JSON lines on stderr. Most lines for a task have the key `task`. A script run writes one line with the script, the exit code and the time. Of the harness's stderr, the adapter keeps the last 2048 bytes, for a failure reason. Of the harness's stdout, the adapter logs the first 200 characters of each line that it cannot decode, under the key `line`.                                                                                                                                                                                                |
| Log lines of other parts  | The dispatcher writes a line for each task with `profile`, `tier`, `kind` and `task`. The A2A gateway (below: the gateway) writes an ingress line and a task-end line with `taskId`. The key for the bus task has two names today: `task` (the adapter, the dispatcher) and `taskId` (the gateway, and two lines of the adapter). The `task_id` of the platform's tool records is the id of Hermes, not the bus task.                                                                                                                                                                  |
| Tool calls                | The adapter publishes the tool and its input as an `activity` artifact on the bus, at the start of a call, best effort. The bus gets no end event, no result and no duration. A script run publishes none. The gateway, the relay and the swarm tools read it.                                                                                                                                                                                                                                                                                                                         |
| How long the bus keeps it | The stream `TASKS` keeps at most 72 hours, 8 GiB and 4096 messages for each subject.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| The join key              | Each envelope has a `correlationId`, which the gateway makes once and each hop copies. [`spec-a2a-payloads.md`](spec-a2a-payloads.md) calls it authoritative. The envelope's `traceparent` is optional, and no caller sets it.                                                                                                                                                                                                                                                                                                                                                         |
| Traces                    | None from the pod: the `a2a` module has no OpenTelemetry dependency, and the Claude harness runs with its own telemetry off. The inference gateway has metrics for each model call, and spans only where `litellm.otel` is on (off by default in the chart).                                                                                                                                                                                                                                                                                                                           |
| Metrics                   | None on the pod. The dispatcher has no metrics port. The schedule controller counts runs and skips for each schedule. The gateway's task counter counts a delegation chain one time, under its root, so a delegated profile task has no count of its own.                                                                                                                                                                                                                                                                                                                              |
| Credentialed calls        | The profile role reaches four broker routes. Only the exec route writes an audit record: the tool, a label for the subcommand, the result status and the caller, never the arguments. The version-control route writes a metric and no record. The MCP relay writes two plain lines. The audit-report store writes one plain line. The Cloud API relay and the feedback route also write the record, but the profile role does not reach them. The task id and the `jti` of the credential are inside the text of `principal`; the profile shows only through the ServiceAccount name. |

## What a profile pod can forge

A profile pod runs a model on text that a tenant can write. Four facts limit what its own
signals are worth:

- A worker opens a connection for each task. The pod's own connection still reaches the events
  of each task of its profile, until its wide grants are removed
  ([#3889](https://github.com/gke-labs/kube-agents/issues/3889)). So an `activity` artifact can
  come from another task's pod of the same profile.
- The harness and the adapter run with one user id in one container. We did not test whether the
  harness can write a line to the container's stderr. This design does not depend on a "no". The
  adapter already copies up to 200 characters of a harness stdout line that it cannot decode
  into its own log line, as a value.
- The platform's own tool record has the same limit: the platform agent's process writes it.
- A profile task can read the log lines of a profile pod today, through a Cloud Logging read on
  the broker's exec route. See "Who can read these lines".

So nothing that a profile pod writes is a record of authority. For what a task did with a
credential, the broker's record is the only record that the pod cannot write; its `request_id` on
the exec route is the caller's value. The lines of the dispatcher, the gateway and the inference
gateway are also outside the pod's reach, but they say only that a task started, got a
credential, ended and called a model. A reader that must trust a line filters on the namespace
and the container, which the node log agent sets, and not on a key of the payload.

[`spec-a2a-payloads.md`](spec-a2a-payloads.md) calls the replay of the stream the audit trail and
puts `activity` in it. That stays true for what was published, inside the limits of the stream.
This document adds only that the content is the publisher's word.

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
the worker. A's cost is the list in "What the platform has and this proposal does not give", and
A depends on the open fact.

The issue asks for telemetry that the operator declares for each profile. Under A the operator
declares nothing on a profile pod: each signal comes from a path that each profile already has.
B is the option that declares it for each profile.

## The proposal, signal by signal

| Signal                 | Proposed counterpart                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | Grant that it needs                                                                                                                                                                                     | What changes                                          |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| Log lines              | The adapter's stderr through the node log agent, with no sidecar. The platform's sidecar exists only because Hermes writes files. Each line gets the run keys.                                                                                                                                                                                                                                                                                                                                      | None.                                                                                                                                                                                                   | The adapter's logger.                                 |
| The harness's stderr   | Stays a 2048-byte tail in the failure reason. It can hold tenant text.                                                                                                                                                                                                                                                                                                                                                                                                                              | None.                                                                                                                                                                                                   | Nothing.                                              |
| Credentialed calls     | The broker's audit record, on each route that the profile role reaches: also the version-control route, the MCP relay and the audit-report store. It gets keys of its own for the profile, the task and the trigger, from the verified credential.                                                                                                                                                                                                                                                  | None.                                                                                                                                                                                                   | The broker's audit record.                            |
| The activity of a task | The adapter writes each activity entry also as a log line. It is a trail of what the worker says it did, with the limits of "What a profile pod can forge". See the next section.                                                                                                                                                                                                                                                                                                                   | None.                                                                                                                                                                                                   | The adapter.                                          |
| Messages               | No new record. The task's messages stay on the bus, and the delivery of a result has its own records.                                                                                                                                                                                                                                                                                                                                                                                               | None.                                                                                                                                                                                                   | Nothing.                                              |
| The join of one task   | The task id and the `correlationId` in each log line of the adapter, the dispatcher and the gateway. This needs no exporter. The broker record carries the task id; see "The task keys" for its join.                                                                                                                                                                                                                                                                                               | None.                                                                                                                                                                                                   | The adapter, the dispatcher, the gateway, the broker. |
| Spans                  | Spans for a task and its hops, from the gateway and the dispatcher. No span from inside the worker (question 2).                                                                                                                                                                                                                                                                                                                                                                                    | An egress rule for those two to the collector in the cluster. The gateway's egress policy has none today. The operator renders no policy that selects the dispatcher's pods; the chart was not checked. | The gateway, the dispatcher, the operator's policies. |
| Metrics                | A counter and a duration at the dispatcher, by profile, tier, kind and end. The dispatcher knows the ends that it writes itself (failed by deadline or lease, canceled, rejected) and "finished" from the last renewal of the worker, which has no state. A split of finished into completed and failed needs a state field on that renewal or a read of `.events`. The dispatcher gets a metrics port and a PodMonitoring. No scrape of profile pods, and no change of the gateway's task counter. | A scrape of the dispatcher. For the split: a field on the renewal, or a read of `.events` for the dispatcher (question 6).                                                                              | The dispatcher, the chart's monitoring template.      |
| Health rules           | One rule from the dispatcher's counter: the share of task ends that the dispatcher failed, for each profile.                                                                                                                                                                                                                                                                                                                                                                                        | None.                                                                                                                                                                                                   | The chart's health rules.                             |

The gateway's task counter does not change, for two reasons: its series come from closed lists,
and the usage poller of the operator reads it. The dispatcher's counter feeds the health rule
only; no usage reading takes it.

### The task keys

A person who looks for one task must find it in each signal. We propose that each log line and
each record of a task carries the task id and the `correlationId`, each under one name, with the
profile, the tier and the trigger's origin and name. A metric carries only the labels with a
closed or small set of values: profile, tier, kind, the end and the trigger's origin (one value
today, `builtin`, or none). The broker takes each key from the verified task credential, never
from a value that the pod sends.

The task credential carries the task, the profile, the tier and the trigger today, not the
`correlationId`. So the broker record carries the task id and the `jti`, and a reader joins
through the mint line of the dispatcher, which has the `jti`. A `correlationId` claim is a change
of `a2a/taskcred` and of both verifiers (question 7).

The write-permit design (pull request [#7130](https://github.com/gke-labs/kube-agents/pull/7130),
open, not merged) has a row for the broker's line of an admitted unattended write. That line and the
record of this document must be one record; the keys follow one decision. A route that another
design opens to a profile writes the same record.

The requester keys follow
[`audit-logging-user-attribution.md`](audit-logging-user-attribution.md).

### The activity of a task

The activity entry is the tool and its input today. The open draft pull request
[#6796](https://github.com/gke-labs/kube-agents/pull/6796) (not merged at `be60d590d`) changes it to
the tool and a short title, with a scrub at the source and no arguments. This design assumes that
form. If #6796 changes or does not merge, the log line needs a scrub of its own before it ships.

What the trail is and is not:

- It is the worker's own word. It has the trust of the platform's tool record, and it is not an
  audit record. It does not use the `audit_event` key, so no reader takes it for one.
- It has the start of a call only. The driver of the adapter emits an end event for each tool step
  today (`EventToolResult`), with no name of the call and no result, and the adapter does not
  publish it. A line for the end and a duration is possible from it. A result, or an event from a
  hook, is a question of the hooks group
  ([#6671](https://github.com/gke-labs/kube-agents/issues/6671)), not of this document. The design
  of that group is pull request [#7253](https://github.com/gke-labs/kube-agents/pull/7253) (open,
  not merged).
- A script run has its own line and no activity entry.
- The Admin Console reads audit records from the platform agent's namespace. Profile work does
  not show there under this proposal (question 5).
- A profile task can read the lines of this trail today. See "Who can read these lines".

Open for the decider: one line for a tool call. The hooks design (pull request
[#7253](https://github.com/gke-labs/kube-agents/pull/7253), open, not merged) proposes in its rows 2
and 3 an adapter line when a tool call starts and when its result is in: the task id, a call id, the
tool, the status and the duration, with no input and no result. With the trail of this document, the
adapter then writes two lines for one call, with different fields. With a yes to one line, this
document changes so:

- The line for a tool call is the line of the hooks design. That design owns its source and its
  fields. This design owns its keys ("The task keys") and how it ships: stderr and the node log
  agent.
- This document's trail row becomes that line, and the adapter copies no activity entry to
  stderr.
- The scrubbed title of #6796 is in the line only if row 3 of the hooks design is answered "no".

### Who can read these lines

A profile task can run `gcloud logging read` on the broker's exec route today, held to the project
of its task. So a task whose target project is the install's project reads the container logs of the
profile namespace and of the platform namespace now: the adapter's lines, its copy of undecoded
harness output, and the failure reasons. The task need not be on the same profile. The
broker-surface design (pull request [#7120](https://github.com/gke-labs/kube-agents/pull/7120),
open, not merged) has a row for this read.

Option A adds no network rule, but each line that it ships lands in that reach.

Open for the decider: the reads of these lines through Cloud Logging. The broker already rewrites
the filter of a logging read, to drop the entries of an excluded cluster. With a yes to the same
rewrite for these lines, this document changes so:

- The rewrite drops the container logs of the profile namespace and of the platform namespace
  for the profile role, with a conformance row, and "Done when" gets that line.
- A duty that must read the install's own logs then needs a grant of its own.

With a no, the reach stays, and it is one more cost of option A under question 1.

### What the platform has and this proposal does not give

- Spans for each tool call and each MCP call, and the user and session attributes on them. They
  need spans from inside the worker.
- The arguments and the result of a tool call in a record.
- A record for each message.
- A record for each approval request and answer.

[`spec-nats-deployment.md`](spec-nats-deployment.md) says that the bus client library makes a
publish and a consume span with the trace context. That is not built. In a worker it also needs an
exporter and an egress rule, so it is part of question 2. If the answer is no span from inside
the worker, that paragraph of the spec needs an edit.

## The open fact

Does the node log agent ship the stderr of a profile pod? The published telemetry page says that
the node log agent ships the logs of all containers, and it names no profile pod.

Two reads of Cloud Logging, on two test installs of release 0.38.0, found no line and no pod event
from a profile namespace in two days. The same reads found the lines of the platform namespace.
No profile worker pod ran in that time: on the install that has the profile namespace, the
profiles of the eval runs ran inside the platform agent's pod. So the reads do not answer the
question, and the fact stays open until a read on an install where a profile worker pod ran.

If the node log agent ships those lines, the log row needs no new object. If it does not, option
A needs a decision: a sidecar in the profile pod with its own output, or a log path through the
bus.

## What stays out

- Usage measurement: the usage counters and their poller, the chat metrics, the outcome counts
  and the attribution tag.
- The hook and plugin mechanism for profile workers (#6671; its design is pull request
  [#7253](https://github.com/gke-labs/kube-agents/pull/7253)).
- The sidecar, the plugins and the hook of the platform agent's pod. They stay until that pod
  retires.
- A collector of our own, and a change of the endpoint ladder.
- Dashboards, and the delivery of results to chat.
- Any change of code. This document changes none.

## Questions

For the security approver:

Each question has our recommendation after it. Two more points are open for the decider in the
text, each with what a yes changes: one line for a tool call ("The activity of a task"), and the
reads through Cloud Logging ("Who can read these lines").

1. The issue asks for telemetry that the operator declares for each profile. Is option A accepted
   in place of that, with the losses of "What the platform has and this proposal does not give"?
   Recommendation: yes.
2. Spans (open question 6 of the inventory): none, spans from the gateway and the dispatcher with
   one egress rule for them, or also spans from inside the worker with an exporter and an egress
   rule in the profile namespace? Recommendation: the gateway and the dispatcher.
3. Is the broker's record the record of authority for profile work, and does it get a record on
   the version-control route, the MCP relay and the audit-report store? Recommendation: yes.
4. Is the activity trail on stderr wanted, given that it is the worker's own word? The other
   choices are no trail outside the bus, or an exporter on the bus (option C). Recommendation:
   yes.
5. Does the Admin Console show profile work in this group? Recommendation: no; later, from the
   broker's record.
6. Is a scrape of the dispatcher right, or must a profile pod have a scrape target of its own?
   Is the split of "finished" into completed and failed wanted? Recommendation: the dispatcher,
   and the split by a state field on the last renewal, which is the worker's own word.
7. Are the keys of "The task keys" the right set, may the trigger's name be a key of a log line,
   and does the task credential get a `correlationId` claim? Recommendation: yes, yes, and no
   claim now: the `jti` joins.
8. Is the retention of the log bucket sufficient for a line of profile work, with no rule of our
   own? Recommendation: yes.

## Done when

The security approver confirms this list in this document.

For #6674:

- Each signal of the platform agent's pod has a decision in this document: a profile
  counterpart, or "not needed" with a reason.
- Each log line of the adapter, the dispatcher and the gateway for a task carries the task id
  and the `correlationId`, each under one name, and each broker record carries the task id, with
  a test for each part.
- A call of a profile task on each broker route that the profile role reaches has a broker audit
  record with keys for the profile and the task, with a test.
- The adapter writes each activity entry as a log line with the task keys, with a test; or the
  approver's "no" to question 4 is recorded here.
- The gateway and the dispatcher export a span for a task and its hops, and their egress rule is
  named here; or the approver's "none" to question 2 is recorded here.
- The dispatcher exports a task counter by profile, tier, kind and end, and one health rule reads
  it.
- The NetworkPolicy of the profile namespace has no new rule, or each new rule is named here with
  the approver's decision and has a conformance row.
- The open fact has an answer from an install where a profile worker pod ran.
- With a yes to one line for a tool call: the adapter writes the line of the hooks design with
  the task keys, and copies no activity entry to stderr.
- With a yes to the rewrite: a logging read of the profile role does not return the container
  logs of the profile namespace and of the platform namespace, with a conformance row.

What these lines need from other work: the broker lines change `credential_proxy.py`, so they take
their turn in the order of work on that file. The activity trail assumes the entry form of #6796
(open, not merged) and changes the same file as that pull request.
