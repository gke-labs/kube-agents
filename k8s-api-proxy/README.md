# k8s-api-proxy

The Kubernetes API proxy for the agent sandbox (`<agent>-kube-proxy` deployment). The sandbox's
`kubectl` talks to it over cluster-routed URLs (`/clusters/<cluster-id>/...`), and the proxy
authenticates the caller (`pkg/k8sapiproxy/auth.go`), applies a per-request policy
(`pkg/k8sapiproxy/policy.go`), redacts Secret values (`pkg/k8sapiproxy/secrets.go`), removes the
caller's credentials (including a bearer token in `Sec-WebSocket-Protocol`), impersonation,
`Audit-ID` and `Connection`-named headers (`pkg/k8sapiproxy/headers.go`), forwards the request to
the cluster's API server through `pkg/upstream` with credentials the sandbox never holds, and emits
structured audit records (`pkg/k8sapiproxy/audit.go`), Prometheus metrics
(`pkg/k8sapiproxy/metrics.go`), and OpenTelemetry spans (`pkg/k8sapiproxy/telemetry.go`).
`cmd/k8s-api-proxy` builds the production server binary.

`NewTokenReviewAuthenticator` (`auth.go`) is the `CallerAuthenticator`: it accepts a bearer token
that a TokenReview on the host cluster finds valid for the configured audience, from a username on
an allowlist, and caches each acceptance or refusal for ten seconds by default. The audience should
be one the API server does not accept, so a token sent to the proxy cannot be used against the API
server directly; the operator picks it. A review that fails is a 503 rather than a 401.

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

The handler hands one `AuditEvent` per request to an `AuditSink` (`audit.go`) when the request
ends, so a watch or an `exec` session is recorded when it closes. The event holds the caller, the
cluster, the API server's classification of the request, the outcome (`forwarded`,
`unauthenticated`, `auth_unavailable` when the TokenReview fails, `refused` with its reason,
`upstream_error`, `canceled` when the caller gives up, or `aborted` when a response fails
mid-copy otherwise or an upgrade's 101 cannot be written), the status and the duration. An upgraded session that breaks off after its 101 is recorded as
`forwarded`, because the reverse proxy reports nothing about the stream itself. The handler
puts no body, query or credential in it, but an authentication or transport error is recorded as
returned. The caller never sees that error, because it can name the upstream. A Secret response
the handler refuses to pass on because it cannot redact it stays `forwarded`, with the cause as
the error and the handler's 502 as the status (on a watch, the stream's 200). The default sink
writes each event as one `slog` record, and the reverse proxy's own errors go to the same logger.

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

It is a self-contained Go module. Run its checks from this directory:

```bash
gofmt -l .
go vet ./...
go test -race ./...
```
