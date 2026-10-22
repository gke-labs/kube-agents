# MCP server

A [Model Context Protocol](https://modelcontextprotocol.io) endpoint for one `spec.mode: next` install, so an external coding agent or IDE host can talk to it. It is a console client of the install's A2A bus: it holds the `console` bus credential and nothing else, and it can do on the bus exactly what the console page can.

It answers `connection_status`, the turn tools `send_message` and `task_control`, `manage_session`, and `poll_events`. Further tools, split out of gke-labs/kube-agents#3653, come in later changes.

## Turn it on

The server is off unless the operator runs with `A2A_MCP_DOOR=true`, an operator-level flag like the inject backend's. With it set, the operator renders, beside the console server:

- `<agent>-a2a-mcp`: a Deployment running `/mcp` from the `a2a-console` image, and a ClusterIP Service on port 8089;
- `<agent>-a2a-mcp-netpol`: deny-all ingress on that pod, so the only way in is `kubectl port-forward`;
- `<agent>-a2a-mcp-token`: the bearer token callers present, minted once by the operator.

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

To rotate the token, write a new value into the Secret. The server reads it on every request, so the new one is accepted, and the old one refused, as soon as the kubelet updates the mounted file, usually within a minute.

## What it reads

`connection_status` reports the bus endpoint and identity, whether the connection is TLS, the size of the four message streams, whether the gateway and the platform agent are consuming, and the agents registered on `DIRECTORY`. It reads all of it through the console's grants, at most once every five seconds whatever the call rate. Agent names and descriptions are shown as code, as published, shortened.

## Conversations

Each call names its conversation with `conversation` (a name; `default` if omitted) or `conversation_token` (the token an earlier answer reported, as `<token>` or `console:<token>`). A token given where a name goes, in either form, names that conversation too. There is no server-wide active conversation.

Names are this server's memory only, and they're shared:

- A restart forgets them, and so does the table itself past 1024 names, starting with the least recently used. A conversation resumes by its token either way.
- Every caller holds the same bearer token, so every caller sees and can use every name.
- A name is at most 128 characters, and holds nothing `list` couldn't show back as typed: no backticks, pipes, control or invisible characters, or spaces at either end or doubled.

`manage_session` handles the names and the console's control turns:

- `list`: the named conversations, most recently used first, with token, description and last use;
- `new`: create a name, with an optional description of at most 200 characters; an existing name is refused;
- `delete`: forget a name. The conversation's history stays on the bus, and a `/session` worker for it idles out by itself. To end one now, stop its running task and send `session_off` first;
- `session_on`, `session_off`, `status`, `help`: send `/session`, `/session off`, `status` or `help` on the conversation, exactly as `send_message` would, and return the answer. `/session` and `/session off` are the gateway's commands. `status` and `help` are messages to the agent: while a task runs, `status` replays its progress and `help` steers it.

The original client's `switch`, `debug_on`, `debug_off`, `transcript` and `clear_killed` are not offered; a call to one is answered with an error that says why.

## Card reports and poll_events

A turn that files a card gets the card's reports later, often minutes after the turn's own task ended: the gateway posts each one to the conversation as a `notify` frame on `chat.console.<token>.out`, naming the task whose turn filed the card. A delegation chain's terminal result is posted there too, as a `result` frame; the server doesn't keep those, since the chain's tasks hold the result and `send_message` and `task_control` read it there. A turn in progress reads one flag off its own result frame, below.

The server listens to every conversation it knows: every name in its table, and every token a caller has used. A conversation no name holds keeps its listener for an hour after its last use or report; that's checked when a listener is added, so one can stay longer. A conversation whose name is deleted while nothing is kept for it stops being listened to at once, unless a turn on it is still gathering. Listeners are capped at 1024: past that the least recently active goes, one without a name first, but a named conversation's listener can go too. It's listened to again the next time it's used, and frames posted in between are lost.

It keeps each conversation's `notify` frames, at most 256 of them or 1 MiB, whichever comes first, and 32 MiB across all conversations, dropping the oldest and counting what it dropped. An update to a report replaces it in place; a report's original that arrives after its update is dropped. Frames that aren't JSON are dropped, and a frame's text is cut at 16 KiB characters.

What it keeps is live only. Lost:

- frames posted before the server first heard of the conversation, or while its listener was evicted;
- frames past the bounds;
- everything, across a restart of the server.

`send_message` gathers the reports. Once the turn's task, and the tasks delegated from it, have ended, it keeps collecting the reports on any of those tasks until no new one has come for 15 seconds, or the wait ends, whichever is first. Only reports heard after the turn was sent count, so a report from an earlier turn on the same task isn't taken for this one. The turn also reads the gateway's `result` frame for its own task: one without `cardsPending` means no work went to cards, and gathering stops at once; with `cardsPending`, or when no result frame arrives (an older gateway), it waits for the quiet period as above. It returns them in the order they arrived, each labelled as a card's report and shown as quoted text, and says why it stopped: the tasks were still running, so these are only the reports that had arrived; or they ended and no new report came; or they ended and the wait ran out. A card's report can take minutes, so for card work pass a longer `wait_seconds`, or read later reports with `poll_events`.

`poll_events` reads a conversation's kept frames: `conversation` or `conversation_token` as for `send_message`, `since` (a cursor; 0 for everything kept), and `wait_seconds` (up to 60) to wait for a frame newer than `since`. A name must be one the server knows; an unknown name is refused rather than created. A token the server hadn't heard of is listened to from that call on. It returns the frames newer than `since` in cursor order, the new cursor, how many were dropped, and whether any dropped frame was newer than `since`. An updated report comes back under a new cursor. One cursor counts across all conversations, so a cursor stays valid when a conversation's listener goes and comes back; a `since` past the server's cursor, as after a restart, reads from 0.

The swarm view's pending events come from this buffer in a later change.
