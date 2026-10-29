# MCP server

A [Model Context Protocol](https://modelcontextprotocol.io) endpoint for one `spec.mode: next` install, so an external coding agent or IDE host can talk to it. It is a console client of the install's A2A bus: it holds the `console` bus credential and nothing else, and it can do on the bus exactly what the console page can.

It answers `connection_status`, `read_topics` and `inspect_pr`, the turn tools `send_message` and `task_control`, and `manage_session`. Further tools come in later changes.

## Turn it on

The server is off unless the operator runs with `A2A_MCP_DOOR=true`, an operator-level flag like the inject backend's. With it set, the operator renders, beside the console server:

- `<agent>-a2a-mcp`: a Deployment running `/mcp` from the `a2a-console` image, and a ClusterIP Service on port 8089;
- `<agent>-a2a-mcp-netpol`: deny-all ingress on that pod, so the only way in is `kubectl port-forward`;
- `<agent>-a2a-mcp-token`: the bearer token callers present, minted once by the operator.

The operator also renders the server's ServiceAccount, `<agent>-a2a-mcp`, and its token Secret, `<agent>-a2a-mcp-token`. If either name is already taken by an object the operator does not control, the server is not rendered and the PlatformAgent carries the `A2AMCPServer` condition ([conditions](../../docs/site/src/content/docs/operator/platformagent-crd.md)).

Unset the flag and all of it is removed, the token included.

## Connect a client

Forward the Service, and read the token into your client's configuration without printing it, for example into an environment variable the client reads:

```sh
kubectl -n kubeagents-system port-forward svc/platform-agent-a2a-mcp 8089:8089
```

```sh
export KUBE_AGENTS_MCP_TOKEN="$(kubectl -n kubeagents-system get secret platform-agent-a2a-mcp-token -o jsonpath='{.data.token}' | base64 -d)"
```

Reading the Secret needs `get` on it, and the port-forward needs `pods/portforward`; together they are the access gate.

Point an MCP client at `http://localhost:8089/mcp` with the header `Authorization: Bearer <token>`. The endpoint takes JSON-RPC 2.0 over `POST`, one JSON answer per request. It refuses any request that carries an `Origin` header, so a browser page cannot use it. `GET /healthz` answers without a token.

A request that names an MCP revision this server doesn't speak (the `MCP-Protocol-Version` header) is refused with 400; a request without the header is served.

To rotate the token, write a new value into the Secret. The server reads it on every request, so the new one is accepted, and the old one refused, as soon as the kubelet updates the mounted file, usually within a minute. A value that is blank, or holds a byte no request header could carry (a line break or another control character other than a tab), is replaced: the operator mints a new token over it.

## What it reads

`inspect_pr` reads one pull or merge request, with its comments and diff, from a repository the install manages. It asks the credential broker's `proposal-view` with the pod's own projected token, so it works on GitHub and GitLab and the pod holds no forge credential; the broker refuses any repository outside the install's managed list, and the token's role reaches that one read and nothing else. The tool is offered only when the operator has wired the broker in (`A2A_MCP_BROKER_URL`). Everything the pull request's authors wrote, its title, description, comments and diff, is shown as data: fenced, capped on line boundaries with a count of what was cut, control and bidi characters shown as `<U+XXXX>`, and introduced by a line saying it is the authors' content.

`connection_status` reports the bus endpoint and identity, whether the connection is TLS, the size of the four message streams, whether the gateway and the platform agent are consuming, and the agents registered on `DIRECTORY`. It reads all of it through the console's grants, at most once every five seconds whatever the call rate. Agent names and descriptions are shown as code, as published, shortened.

`read_topics` reads the install's standing topics and agent cards, through the same grants. `list` shows every topic provisioned on `TOPICS-STATE` and `TOPICS-JOURNAL` with its newest entry, or that it has none yet (up to 256 per stream; past that the list says so). `read` shows one topic: a state topic's latest entry, or a journal topic's newest 16 entries from the last 1024 messages of its stream, or its latest entry alone if none falls in that window. `cards` shows the agent cards on `DIRECTORY`. A topic is named by its name, `shared.<name>` or `agent.<agent>.<name>`, or its subject; a bare name that several topics share is refused as ambiguous. A name that is not dot-separated subject tokens is refused before the bus is asked, and one that names no provisioned topic is refused before any consumer is opened. A repeated call within five seconds is answered from the last read. Entries are written by agents and shown as quoted data: one line each in the list, fenced and capped at 2 KiB per summary and per data block in a read. Every caller reads every topic the console can.

## Conversations

Each call names its conversation with `conversation` (a name; `default` if omitted) or `conversation_token` (the token an earlier answer reported, as `<token>` or `console:<token>`). A token given where a name goes, in either form, names that conversation too, whether or not this server still holds a name for it. There is no server-wide active conversation.

Names are this server's memory only, and they're shared:

- A restart forgets them, and so does the table itself past 1024 names, starting with the least recently used. A conversation resumes by its token either way.
- Every caller holds the same bearer token, so every caller sees and can use every name.
- A name is at most 128 characters, and holds nothing `list` couldn't show back as typed: no backticks, pipes, control or invisible characters, spaces at either end, doubled or other than the plain space, and nothing spelled like a token (`mcp` and 24 lower-case hex digits).

`manage_session` handles the names and the console's control turns:

- `list`: the named conversations, most recently used first, with token, description and last use;
- `new`: create a name, with an optional description of at most 200 characters; an existing name is refused;
- `delete`: forget a name, given as the name or as the conversation's token. The conversation's history stays on the bus, and a `/session` worker for it idles out by itself. To end one now, stop its running task and send `session_off` first;
- `session_on`, `session_off`, `help`: send `/session`, `/session off` or `help` on the conversation, exactly as `send_message` would, and return the answer. `/session` and `/session off` are the gateway's commands. `help` is a message to the agent, sent like any other: while the conversation's turn is still running it waits for it to end, as a second `send_message` does.

The original client's `switch`, `debug_on`, `debug_off`, `transcript` and `clear_killed` are not offered, and neither is `status` yet: sent as a message, it would wait out a running turn rather than report its progress, so it comes back once it can (read a task's progress with `task_control get` meanwhile). A call to one is answered with an error that says why.
