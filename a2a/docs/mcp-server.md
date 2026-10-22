# The kube-agents MCP server

- **Author:** [@adamparco]
- **Date:** 2026-10-20
- **Status:** draft for review
- **Companions:** [the A2A payload spec](../../docs/designs/spec-a2a-payloads.md) (task lifecycle, reserved artifact names),
  [the Hermes bridge doc](hermes-bridge.md) (the executor whose steps this renders)

## Purpose

`a2a/mcp` is the in-cluster Model Context Protocol server hosted by `a2a-gateway`
(`A2A_MCP_LISTEN`, default `127.0.0.1:8089`; `off`/`false`/`disabled`/`none` disables it)
that lets coding agents and MCP clients drive a kube-agents deployment
through its A2A JetStream bus: connect to the bus, send a message to a conversation and
stream the turn's steps back as it runs, inspect and steer tasks, read blackboard topics,
open the PRs an agent filed, project Swarm state, and receive push notifications over SSE
when something finishes out of turn. It is a bus client and a renderer. It executes nothing
on the cluster itself and changes no protocol: everything it reads is on the `TASKS`,
`DIRECTORY`, and `TOPICS-*` streams and the `chat.console.*` subjects, as the payload spec
defines them.

In-cluster, `a2a-gateway` mounts the HTTP/SSE MCP listener (`a2a/mcp/http_server.go`) at:
- `POST /mcp` — JSON-RPC 2.0 `tools/list`, `tools/call`, `resources/list`, `resources/read`
- `GET /mcp/events` — Server-Sent Events stream of live `PushEvent` items
- `POST /mcp/swarm` — stateless Swarm projection (`BuildSwarmState`) for a caller's root conversation and subagent lineage
- `GET /healthz` — unauthenticated health status

## Modules

| File                                       | Responsibility                                                                                                                                                                     |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `doc.go`                                   | Package-level overview of the `a2a/mcp` Model Context Protocol server and A2A v2 JetStream client                                                                                  |
| `http_server.go`                           | In-cluster HTTP/SSE MCP handler (`/mcp`, `/mcp/events`, `/mcp/swarm`, `/healthz`) and `NewInClusterServer` / `NewHTTPServer`                                                       |
| `server.go`                                | JSON-RPC 2.0 framing, `tools/call` dispatch, per-tool argument handling, progress notifications, and `SubscribePushEvents` fan-out                                                 |
| `server_handlers.go`                       | Tool handlers for `discover_deployments`, `connect_deployment`, `connection_status`, `send_message`, `manage_session`, `task_control`, `read_topics`, `inspect_pr`, `poll_events` |
| `schema.go`                                | The MCP tool and resource definitions (names, descriptions, input schemas) and `resources/read`                                                                                    |
| `config.go`                                | `ConfigStore`: deployments and cached bus credentials, console conversations and their bindings, push settings, and step-progress path                                             |
| `config_events.go`                         | `PushEvent` queue persistence (`AppendPendingEvents`, `DrainPendingEventsForAGY`, `DrainPendingEventsByID`) and conversation-scoped event routing                               |
| `config_lock_unix.go` / `config_lock_other.go` | Cross-process advisory file locking for `ConfigStore` updates                                                                                                                  |
| `exec_runner.go`                           | `CommandRunner` abstraction for local CLI discovery and port-forward probes                                                                                                        |
| `transport.go`                             | `TransportManager`: NATS/JetStream connection management and credential resolution                                                                                                 |
| `bus.go`                                   | `BusSession`: NATS/JetStream connection, stream bootstrap and backfill, the folded task/topic/card state, lookups                                                                  |
| `bus_ingest.go`                            | Envelope folding: status, artifacts (result, turn, progress, thinking, activity, steps), delegation and card linking, push-event detection, bounded eviction                       |
| `bus_turn.go`                              | Running a turn: publish to the console subject, wait with live step streaming, the per-task step cursor and its persistence                                                        |
| `session_pods.go`                          | `DeriveActiveSessionPods`: NATS `DIRECTORY` card inspection and Kubernetes pod probing for `/session` workers                                                                      |
| `format.go`                                | Markdown for status, deployments, topics, and push events                                                                                                                          |
| `format_steps.go`                          | Step collection (`StepDetail`) across primary, delegated, card, and wake tasks; custom-tool envelope                                                                               |
| `format_activity.go`                       | Kanban card state from the facts the executor states; tool-activity lines                                                                                                          |
| `format_turn.go`                           | The turn outcome body (answer, chat/v1 layout, debug panels)                                                                                                                       |
| `swarm_bridge.go`                          | Pure Swarm state projection (`BuildSwarmState`) from bus tasks, topics, and active `/session` workers                                                                             |
| `swarm_overview.go`                        | Markdown rendering of the Swarm fleet and session overview                                                                                                                         |
| `pr.go`                                    | `inspect_pr`: GitHub PR metadata and diff, written as local `.diff`/`.patch` files when writable                                                                                   |

## The step contract

A turn's step list is what the user watches while the agent works, so it must not depend on
the prose a model happens to write. Steps travel as **`kube-agents.step/v1` records on the
`steps` artifact** (`a2a/lib/stepmsg`), one data part per step:

```json
{
  "schema": "kube-agents.step/v1",
  "kind": "plan | card | synthesis",
  "title": "Inspecting GKE cluster drift-peer-ap-1.",
  "thought": "the reasoning the model stated for it, when it did",
  "callIds": ["call_gke_1"],
  "card": {"id": "t_01", "title": "...", "assignee": "...", "status": "done", "verdict": "healthy", "summary": "...", "settled": 1, "total": 2, "parentTaskId": "task-..."}
}
```

- The Hermes bridge publishes a `plan` step per tool call the model planned (from the
  `post_api_request` hook: the call's name and arguments title it; the reasoning is its
  thought; `callIds` names the calls), a `card` step when a specialist Kanban card settles
  (from the board's SQLite state), and a `synthesis` step when the model stops to answer or
  when card results are folded into one answer. `thinking` carries the model's reasoning
  text only, as the payload spec reserves it for. Each `activity` entry also carries the
  `title` it was planned under and the `cardId` it concerns.
- The MCP server folds `steps` into `TaskRecord.Steps` and builds the step list from them:
  one row per record (same-titled rows merge), each activity filed under the step whose
  `callIds` name it, else under the step current at its time. A task-card task names its
  parent on its own first step (`card.parentTaskId`), so card progress files under the
  foreground task without timestamp heuristics.
- A task without step records (a Claude worker, an older executor) still renders: each
  reasoning chunk is a row in the model's own words, titled by its first sentence, with the
  tool calls of that moment under it; without reasoning, consecutive same-titled calls share
  a row.

`steps` is not a reserved artifact name - the set is open - so the relay ignores it, the
fold keeps it, and the web console lists it like any other artifact. Nothing in the renderer
parses English out of a title.

## Streaming a long turn to an MCP client

MCP has no streaming result, and a kube-agents turn routinely runs for minutes. `send_message`
waits up to `wait_timeout_seconds` (default 150 s) while emitting `notifications/progress`
frames, then returns either the answer or a `STREAMING_STEP_CONTINUE:` result that reports the
steps so far and instructs the model to call `task_control get` for the same task; each such
call returns the next batch of steps (the per-task cursor in `bus_turn.go`, persisted across
server restarts) until the final answer arrives.

## Push notifications

A terminal task, an `input-required` status, a `notice` with a PR, or a `chat.notify.*`
message that lands while no `send_message` is waiting becomes a `PushEvent` in the queue.
Subscribers on `/mcp/events` receive live `event: push` SSE frames, and every tool result
piggybacks whatever is still queued for the calling conversation.

## Operations

- **Bounds.** Seen-envelope and task maps are capped (`bus_ingest.go`), GitHub reads are
  size-limited (`pr.go`), step and thinking parts are budgeted per task by the executor, and
  the debug rendering of a 16-card fan-out stays under 18.5 KB (tested).
- **Tests.** `go test -race ./mcp/...` runs an embedded JetStream server with a simulated
  gateway and workers (`helpers_test.go`); the bridge's Kanban SQLite script is exercised
  against a python-built fixture (`hermes-bridge/kanban_test.go`).
