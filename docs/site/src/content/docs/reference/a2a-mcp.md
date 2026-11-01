---
title: MCP server
description: The opt-in A2A MCP server, a Model Context Protocol endpoint through which an external coding agent or IDE talks to a spec.mode next install over its message bus.
sidebar:
  order: 11
---

The MCP server is a [Model Context Protocol](https://modelcontextprotocol.io) endpoint for one `spec.mode: next` install, so an external coding agent or IDE host can talk to it. It is a console client of the install's message bus: it holds the `console` bus credential and nothing else, and it can do on the bus exactly what the [A2A console](/kube-agents/reference/user-interfaces/#a2a-console) page can. It is off by default and renders nothing under `today`.

## Turning it on

The server is rendered only while the operator runs with `A2A_MCP_DOOR=true`, an operator setting like the ones [Operator settings](/kube-agents/install/quickstart-gke/#operator-settings) describes, on top of `spec.mode: next`. With it set, the operator renders beside the console server:

- `<name>-a2a-mcp`: a Deployment running the MCP binary from the `a2a-console` image, a ClusterIP Service on port 8089, and the ServiceAccount of the same name;
- `<name>-a2a-mcp-netpol`: a deny-all ingress NetworkPolicy on that pod, so the only way in is `kubectl port-forward`;
- `<name>-a2a-mcp-token`: a Secret holding the bearer token callers present, minted once by the operator.

If a ServiceAccount or Secret of those names already exists and the `PlatformAgent` does not own it, the server is not rendered and the resource carries the `A2AMCPServer` condition ([status conditions](/kube-agents/operator/platformagent-crd/#status)). Unset the flag and all of it is removed, the token included.

## Connecting a client

Forward the Service, and read the token into your client's configuration without printing it, for example into an environment variable the client reads:

```bash
kubectl -n kubeagents-system port-forward svc/platform-agent-a2a-mcp 8089:8089
```

```bash
export KUBE_AGENTS_MCP_TOKEN="$(kubectl -n kubeagents-system get secret platform-agent-a2a-mcp-token -o jsonpath='{.data.token}' | base64 -d)"
```

Reading the Secret needs `get` on it, and the port-forward needs `pods/portforward`; together they are the access gate. Point the MCP client at `http://localhost:8089/mcp` with the header `Authorization: Bearer <token>`. The endpoint takes JSON-RPC 2.0 over `POST`, one JSON answer per request. It refuses any request that carries an `Origin` header, so a browser page cannot use it, and `GET /healthz` answers without a token. A request naming an MCP revision the server does not speak (the `MCP-Protocol-Version` header) gets a 400; one without the header is served.

To rotate the token, write a new value into the Secret. The server reads the mounted file on every request, so the new token is accepted and the old one refused as soon as the kubelet updates the mount, usually within a minute. A blank value, or one holding a byte no request header could carry, is replaced: the operator mints a new token over it.

## The tools

| Tool                | What it does                                                                                                                                                                                                                                                                                       |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `connection_status` | Reports the bus endpoint and identity, whether the connection is TLS, the size of the four message streams, whether the gateway and the Platform Agent are consuming, and the agents registered on the directory. Answered from a cache at most once every five seconds.                           |
| `read_topics`       | Reads the install's standing topics and agent cards: `list` shows every provisioned topic with its newest entry, `read` shows one topic, and `cards` shows the agent cards. Entries are shown as quoted data, capped per block.                                                                    |
| `send_message`      | Sends one message to the install as a console user and waits for the answer, on a named conversation so that later turns continue it. A turn still running when the wait ends returns its task, to be read later with `task_control`.                                                              |
| `task_control`      | Reads a task's state and answer (`get`, optionally waiting for it to finish), or stops a conversation's running task (`stop`).                                                                                                                                                                     |
| `inspect_pr`        | Reads one pull or merge request, with its comments and diff, from a repository the install manages, through the credential broker; the pod holds no forge credential. The operator wires the broker in whenever it renders the server, so the tool is always offered.                              |
| `manage_session`    | Keeps the names of this server's conversations and sends the console's control turns: `list`, `new` and `delete` manage the names, and `session_on`, `session_off` and `help` send `/session`, `/session off` or `help` on a conversation and return the answer ([Conversations](#conversations)). |
| `poll_events`       | Reads the card reports the gateway posted to a conversation since a cursor, waiting up to 60 s for a new one. `send_message` gathers a turn's card reports itself; this is for reports that come later.                                                                                            |

A message sent through `send_message` reaches the A2A gateway through its console door, the same path the A2A console's chat pane takes, so the Planning Agent answers it or files kanban cards as for any chat message ([Conversations under `spec.mode: next`](/kube-agents/concepts/chatops/#conversations-under-specmode-next)). Everything `inspect_pr` returns that a pull request's authors wrote is shown as data: fenced, capped on line boundaries with a count of what was cut, and introduced by a line saying it is the authors' content.

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
