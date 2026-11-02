# k8s-api-proxy

The Kubernetes API proxy for the agent sandbox (`kube-proxy` sidecar in the `platform-agent` pod).
The sandbox's `kubectl` talks to it over cluster-routed URLs (`/clusters/<cluster-id>/...`), and
the proxy authenticates the caller (`pkg/k8sapiproxy/auth.go`), applies a per-request policy
(`pkg/k8sapiproxy/policy.go`), redacts Secret values (`pkg/k8sapiproxy/secrets.go`), strips the
caller's credentials (including a bearer token in `Sec-WebSocket-Protocol`), impersonation,
`Audit-ID` and `Connection`-named headers (`pkg/k8sapiproxy/headers.go`), forwards the request to
the cluster's API server through `pkg/upstream` with credentials the sandbox never holds, and emits
structured JSON audit records (`pkg/k8sapiproxy/audit.go`), Prometheus metrics
(`pkg/k8sapiproxy/metrics.go`), and OpenTelemetry spans (`pkg/k8sapiproxy/telemetry.go`).
`cmd/k8s-api-proxy` builds the production server binary.

`NewTokenReviewAuthenticator` reads the caller's bearer token from `Authorization` or a WebSocket
subprotocol, verifies it with the cluster's `authentication.k8s.io/v1` `TokenReview` API against
the proxy's audience, and checks that the authenticated `UserInfo.Username` is in the caller
allowlist. A verified token is cached for up to 30 seconds (or until five seconds before its JWT
`exp`, if sooner); an unauthenticated or unauthorized token is never cached, and the caller gets
the same 401 whether the token was missing, expired, or belonged to another service account.

`pkg/upstream` sends `in-cluster`, and the proxy's own cluster when it is given that cluster's
`gke_` ID via `--host-cluster-id`, to the API server through the pod's service account. Any other
`gke_` cluster is reached with the proxy's Google credentials (Workload Identity Federation or
Application Default Credentials): it describes the cluster through the Container API and picks its
endpoint by rules 1, 2 and 4 of
[`private-endpoint-selection.md`](../docs/designs/private-endpoint-selection.md) (the private
endpoint of rule 3 is not chosen yet). A resolved endpoint is reused for ten minutes and a failed
resolution for ten seconds; an endpoint that cannot be dialled is described again after ten
seconds. An IP endpoint is verified against the cluster's own CA and never the system roots, an
`http://` endpoint and a redirect are refused, and a `kind-` cluster has no upstream.

The policy has three modes. `read-only` permits `get`, `list` and `watch` and the discovery
paths. `read-only-dry-run`, the default and the mode kube-agents runs, also permits `create`,
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

When a request finishes, the handler records one `AuditEvent` through its `AuditSink`
(`NewJSONAuditSink` writes one JSON line per event). Its `Outcome` is `allowed`, `denied`,
`unauthenticated`, `upstream_error`, or `redaction_error`; on a watch or stream upgrade the event
is recorded when the stream closes, and if a Secret watch ends because an event could not be
redacted after the `200 OK` header was sent, the outcome is `redaction_error`.

It is a self-contained Go module. Run its checks from this directory:

```bash
gofmt -l .
go vet ./...
go test -race ./...
```
