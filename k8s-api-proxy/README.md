# k8s-api-proxy

The home of a Kubernetes API proxy for the agent sandbox, which is being written. The plan is
that the sandbox's `kubectl` talks to it, and the proxy authenticates the caller, applies a
per-request policy, redacts Secret values, and forwards the request to the cluster's API server
with credentials the sandbox never holds. So far it has the policy (`pkg/k8sapiproxy/policy.go`)
and the HTTP handler around it (`handler.go`), which authenticates through a pluggable
`CallerAuthenticator`, removes the caller's credentials (including a bearer token in
`Sec-WebSocket-Protocol`), impersonation, `Audit-ID` and `Connection`-named headers, and forwards
through a pluggable `UpstreamTransport`. Neither interface has an
implementation yet, and nothing serves or deploys the handler.

The handler hands one `AuditEvent` per request to an `AuditSink` (`audit.go`) when the request
ends, so a watch or an `exec` session is recorded when it closes. The event holds the caller, the
cluster, the API server's classification of the request, the outcome (`forwarded`,
`unauthenticated`, `refused` with its reason, `upstream_error`, `canceled` when the caller gives
up, or `aborted` when a response fails mid-copy otherwise or an upgrade's 101 cannot be written),
the status and the duration. An upgraded session that breaks off after its 101 is recorded as
`forwarded`, because the reverse proxy reports nothing about the stream itself. The handler
puts no body, query or credential in it, but an authentication or transport error is recorded as
returned. The caller never sees that error, because it can name the upstream. The default sink
writes each event as one `slog` record, and the reverse proxy's own errors go to the same logger.

The policy has three modes. `read-only` permits `get`, `list` and `watch` and the discovery
paths. `read-only-dry-run`, the default and the mode kube-agents will run, also permits `create`,
`update` and `patch` carrying `dryRun=All`. `read-write` permits writes. Every mode refuses
stream upgrades (`exec`, `attach`, `portforward`, `proxy`; `read-write` can opt in),
credential minting, assigning pods to nodes, and the token and access review APIs. With Secret
redaction on, Secret subresources and Secret `update` and `patch` are refused in every mode,
since even a dry-run patch can reveal a value. Redaction covers what the Secrets API returns, not
what a workload prints: in `read-write`, a pod that echoes a Secret exposes it through its log.
To redact, the handler asks the API server for a Secret's metadata only (or the `Table` kubectl
requests), then rebuilds the Secret the caller asked for with each value replaced by base64 of
`REDACTED`, and drops any annotation holding a copy of the Secret. Keys come from `managedFields`,
so a Secret whose managers recorded none comes back without `data`; its `type` is not in its
metadata and is absent unless the caller asked for a `Table`. The handler never receives a value, so
it cannot tell an empty one from a set one: `kubectl describe secret` prints every key as 8 bytes,
the placeholder's size, and an empty key looks the same as a populated one. A response the handler
cannot redact becomes a 502, or on a watch ends the stream with an `ERROR` event.

It is a self-contained Go module, and nothing outside this directory builds it. Run its checks
from this directory:

```bash
gofmt -l .
go vet ./...
go test -race ./...
```
