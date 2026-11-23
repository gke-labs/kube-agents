---
title: MCP server
description: The A2A MCP server, a Model Context Protocol endpoint through which an external coding agent or IDE talks to an install over its message bus.
sidebar:
  order: 11
---

The MCP server is a [Model Context Protocol](https://modelcontextprotocol.io) endpoint for one install, so an external coding agent or IDE host can talk to it. It is a console client of the install's message bus: it holds its own bus credential, `console-mcp`, and nothing else. On the bus it reads what the [A2A console](/kube-agents/reference/user-interfaces/#a2a-console) page reads and sends messages the way the page does, but it can't publish on the console's confirmation subject. It runs by default.

## What the operator renders

Unless the operator runs with `A2A_MCP_DOOR=false`, it renders beside the console server:

- `<name>-a2a-mcp`: a Deployment running the MCP binary from the `a2a-console` image, a ClusterIP Service on port 8089, and the ServiceAccount of the same name;
- `<name>-a2a-mcp-netpol`: a deny-all ingress NetworkPolicy on that pod, so the only way in is `kubectl port-forward`;
- `<name>-a2a-mcp-token`: a Secret holding the bearer token callers present, minted once by the operator.

Set `operator.mcpServer.enabled: false` in the chart to turn it off. An `A2A_MCP_DOOR` entry in `operator.extraEnv`, as [Operator settings](/kube-agents/install/quickstart-gke/#operator-settings) describes, also works and wins: empty leaves it on, boolean spellings are parsed, and any other value keeps it off. Turning it off removes all of those objects, the token included. If a ServiceAccount or Secret of those names already exists and the `PlatformAgent` does not own it, the server is not rendered and the resource carries the `A2AMCPServer` condition ([status conditions](/kube-agents/operator/platformagent-crd/#status)).

Its pod adds one to the namespace's session pod quota while enabled, and the chart's quota check counts it with the standing pods ([Prerequisites](/kube-agents/install/prerequisites/)).

## Connecting a client

To connect Claude Code, Antigravity or another local MCP host through the workstation bridge, which reads the token and opens the port-forward itself, follow [Using kube-agents over MCP](/kube-agents/install/mcp/). A client that talks to the server directly connects as below.

Forward the Service, and read the token into your client's configuration without printing it, for example into an environment variable the client reads:

```bash
kubectl -n kubeagents-system port-forward svc/platform-agent-a2a-mcp 8089:8089
```

```bash
export KUBE_AGENTS_MCP_TOKEN="$(kubectl -n kubeagents-system get secret platform-agent-a2a-mcp-token -o jsonpath='{.data.token}' | base64 -d)"
```

Reading the Secret needs `get` on it, and the port-forward needs `pods/portforward`; together they are the access gate. Point the MCP client at `http://localhost:8089/mcp` with the header `Authorization: Bearer <token>`. The endpoint takes JSON-RPC 2.0 over `POST`, one JSON answer per request. It refuses any request that carries an `Origin` header, so a browser page cannot use it, and `GET /healthz` answers without a token. A request naming an MCP revision the server does not speak (the `MCP-Protocol-Version` header) gets a 400; one without the header is served.

The workstation bridge (`kube-agents-mcp`) reads `KUBE_AGENTS_MCP_TOKEN` too. Set `KUBE_AGENTS_MCP_TOKEN_DEPLOYMENT` to the saved name of the deployment the token belongs to. The bridge matches it exactly, never by a base name or kube context. When it is set, the token goes only to the deployment saved under exactly that name, even with one deployment saved. If it names none of them, the bridge sends the token nowhere and logs a warning. When it is unset, the token goes to the only saved deployment, or nowhere, with a warning, when several are saved.

The match fails closed. The bridge renames a deployment to `<kube-context>/<name>` when another context holds one called `<name>`. After that rename, or after the deployment is removed, the old value names nothing. The token is not sent until you set the variable to the new saved name. If a different deployment is later saved under the same name, the variable names that one, so update or unset it when you remove an install.

The bridge's token belongs to the deployment's saved target, not its name. A connection whose kube context, namespace or direct MCP URL differs from the saved deployment's does not get it, such as a `connect_deployment` call that overrides `kube_context` or `namespace`. The bridge reads that target's own token Secret instead, where there is one, and logs a warning. Such an override applies to that connection only: the bridge doesn't save it or make it the active deployment, and reconnects to it until the next `connect_deployment`.

When a turn call carries `_meta.conversation_id` or `stream_steps: true`, `send_message` and `task_control` (`get`) return a `_custom_tool_result` JSON envelope (`title`, `markdown`, `result`) after each step window on the task's `steps` artifact. The workstation bridge sets `stream_steps: true` only when its host passes `_meta.conversation_id` (or a vendor-prefixed `*/conversation_id`); a host that sends only `_meta.progressToken` keeps the single-answer form.

The bridge's `KUBE_AGENTS_MCP_STREAM_STEPS` setting (or `--stream-steps`) overrides this. `off` sends `stream_steps: false` and returns plain results even when the host passes a conversation id. `on` sends `stream_steps: true` for every host. A `stream_steps` the call names itself always wins, though under `off` its step windows come back as plain text, without the envelope. With streaming on, each step's `title` and `markdown` reach the host. While the task is still running, `result` starts with ``STREAMING_STEP_CONTINUE: Task `<addressee>/<task-id>` is actively running``, prompting the host to call `task_control` (`get`) for the next step.

A streamed `get` can re-read a finished turn that delegated, outside the stream that sent it. With no live watch of its chain, it settles at once. It returns the root's own answer and a note that the delegated tasks were not followed.

The bridge offers `poll_events` too and forwards it to this server, so card reports that arrive after a `send_message` answer can be read with the `conversation_token` and `since` the answer gives.

A `send_message` that answers a card's report passes `reply_to`, the task the report's heading gave as `on task`; the agent then reads that report before answering. That read happens on the [platform route](/kube-agents/concepts/chatops/#session), the install's default; a conversation on its own session pod does not read it yet. Without it, a reply under a report starts from the words alone.

To rotate the token, write a new value into the Secret. The server reads the mounted file on every request, so the new token is accepted and the old one refused as soon as the kubelet updates the mount, usually within a minute. A blank value, or one holding a byte no request header could carry, is replaced: the operator mints a new token over it.

## The tools

| Tool                | What it does                                                                                                                                                                                                                                                                                                                                       |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `connection_status` | Reports the bus endpoint and identity, whether the connection is TLS, the size of the four message streams, whether the gateway and the Platform Agent are consuming, and the agents registered on the directory. Answered from a cache at most once every five seconds.                                                                           |
| `read_topics`       | Reads the install's standing topics and agent cards: `list` shows every provisioned topic with its newest entry, `read` shows one topic, and `cards` shows the agent cards. Entries are shown as quoted data, capped per block.                                                                                                                    |
| `send_message`      | Sends one message to the install as a console user and waits for the answer, on a named conversation so that later turns continue it. A turn still running when the wait ends returns its task, to be read later with `task_control`. `addressee` and `parameters` send the turn to an AgentProfile ([below](#sending-a-turn-to-an-agentprofile)). |
| `task_control`      | Reads a task's state and answer (`get`, optionally waiting for it to finish), or stops a conversation's running task (`stop`). `task_id` is an alias for `task`, and `wait_timeout_seconds` for `wait_seconds`.                                                                                                                                    |
| `inspect_pr`        | Reads one pull or merge request, with its comments and diff, from a repository the install manages, through the credential broker; the pod holds no forge credential. The operator wires the broker in whenever it renders the server, so the tool is always offered.                                                                              |
| `manage_session`    | Keeps the names of this server's conversations and sends the console's control turns: `list`, `new` and `delete` manage the names, and `session_on`, `session_off` and `help` send `/session`, `/session off` or `help` on a conversation and return the answer ([Conversations](#conversations)).                                                 |
| `poll_events`       | Reads the card reports the gateway posted to a conversation since a cursor, waiting up to 60 s for a new one. `send_message` gathers a turn's card reports itself; this is for reports that come later.                                                                                                                                            |

A message sent through `send_message` reaches the A2A gateway through its console door, the same path the A2A console's chat pane takes. The Planning Agent answers it or files kanban cards as for any chat message ([Conversations through the A2A gateway](/kube-agents/concepts/chatops/#conversations-through-the-a2a-gateway)). Everything `inspect_pr` returns that a pull request's authors wrote is shown as data: fenced, capped on line boundaries with a count of what was cut, and introduced by a line saying it is the authors' content.

### Sending a turn to an AgentProfile

`send_message` can send a turn straight to an AgentProfile instead of the conversation's agent, as the A2A console can. `addressee` names the profile, for example `cluster`, as one token with no spaces, dots or wildcards. `parameters` is an object of string values holding the profile's task parameters, for `cluster` its `project`, `location` and `cluster`, and is refused without an `addressee`. An `addressee` is refused on a stop word, a bare `/feedback` or `/feedback summary`, and any `/session` text, a message after it included, since those are commands for the conversation rather than tasks. An addressed turn is a new task for the profile, never a session.

The gateway refuses an unknown profile, a tier the profile doesn't run at, or a target outside the install's scope, and its notice is the turn's answer. `read_topics` with `action: cards` lists the agent cards by profile. Otherwise the turn is answered from the profile's task, which the gateway names on its status line. A turn still running when the wait ends returns the task as `<profile>/<task-id>`, and `task_control` reads it under that reference, which always resolves. A bare id resolves only while this server still holds the task.

Once `ProfileSupervisorForm` latches, the operator sets `A2A_PROFILE_SUPERVISOR_CUTOFF` and mounts a profile names file beside it (`A2A_PROFILE_NAMES_FILE`). A task is then read as a profile task when its addressee is in that file, which lists the bound profiles and the ones still draining. Nothing else decides it, not the agent cards and not how this server learned the task. So a session task whose name a profile has since taken reads as still running if its gateway terminal was stored after the cutoff. If the names file can't be read, every task is read as a profile task.

`GET /healthz` reports the cutoff the server runs with and where it came from: `env`, `unset`, or `malformed` for a value that did not parse, which the server reads as its own start time. It also reports whether the names file is loaded and how many terminals the cutoff has refused.

Two known limits:

- The gateway posts the status line before it publishes the submission. If that publish fails, the turn reports a running task that never existed, until its wait runs out.
- Against a gateway older than this change, the status line names no task, so an addressed turn returns after the gateway's notice with no task, about ten seconds later.

The server also serves the workstation bridge's swarm view at `POST /mcp/swarm`, behind the same `Origin` refusal and bearer token. The bridge posts its conversation table and gets back the members and board posts it shows in its host's Swarm UI. The README describes what it reads and its limits.

## Conversations

Every turn tool names its conversation. `conversation` is a name, `default` when omitted, and turns with the same name continue the same conversation. `conversation_token` is the token an earlier answer reported, as `<token>` or `console:<token>`, and continues that conversation whether or not the server still holds a name for it. There is no server-wide active conversation.

Names are the server's own memory, and every caller shares them, since all callers present the same bearer token. A restart forgets them, and past 1024 names the table drops the least recently used; a conversation resumes by its token either way. A name is at most 128 characters and holds nothing a listing could not show back as typed: no backticks, pipes, control or invisible characters, no leading, trailing or doubled spaces, and nothing spelled like a token.

`manage_session` keeps the names and sends the console's control turns:

- `list` shows the named conversations, most recently used first, with token, description and last use; up to 50 are listed and the rest counted.
- `new` creates a name, with an optional description of at most 200 characters, and refuses a name already in use.
- `delete` forgets a name, given as the name or as the conversation's token. The conversation's history stays on the bus, and a session pod for it idles out on its own; to end one now, stop its running task and send `session_off` first.
- `session_on`, `session_off` and `help` send `/session`, `/session off` or `help` on the conversation, exactly as `send_message` would, and return the answer. The first two are the gateway's commands ([`/session`](/kube-agents/concepts/chatops/#session)); `help` is a message to the agent, so it waits for a running turn to end first.

The original client's `switch`, `debug_on`, `debug_off`, `transcript`, `clear_killed` and `status` are not offered; a call to one is answered with an error that says why, and `task_control` `get` reads a task's progress meanwhile.

[`a2a/mcp/README.md`](https://github.com/gke-labs/kube-agents/blob/main/a2a/mcp/README.md) is canonical for the server, the tool list included.
