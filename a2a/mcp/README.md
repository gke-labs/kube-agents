# MCP server

A [Model Context Protocol](https://modelcontextprotocol.io) endpoint for one `spec.mode: next` install, so an external coding agent or IDE host can talk to it. It is a console client of the install's A2A bus: it holds the `console` bus credential and nothing else, and it can do on the bus exactly what the console page can.

It answers `connection_status`, and the turn tools `send_message` and `task_control`. Further tools come in later changes.

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

To rotate the token, write a new value into the Secret. The server reads it on every request, so the new one is accepted, and the old one refused, as soon as the kubelet updates the mounted file, usually within a minute. A value that is blank, or holds a byte no request header could carry (a line break or another control character other than a tab), is replaced: the operator mints a new token over it.

## What it reads

`connection_status` reports the bus endpoint and identity, whether the connection is TLS, the size of the four message streams, whether the gateway and the platform agent are consuming, and the agents registered on `DIRECTORY`. It reads all of it through the console's grants, at most once every five seconds whatever the call rate. Agent names and descriptions are shown as code, as published, shortened.
