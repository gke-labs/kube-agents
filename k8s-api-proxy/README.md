# k8s-api-proxy

The home of a Kubernetes API proxy for the agent sandbox, which is being written. The plan is
that the sandbox's `kubectl` talks to it, and the proxy authenticates the caller, applies a
per-request policy, redacts Secret values, and forwards the request to the cluster's API server
with credentials the sandbox never holds. So far it has the policy (`pkg/k8sapiproxy/policy.go`)
and the HTTP handler around it (`handler.go`), which authenticates through a pluggable
`CallerAuthenticator`, removes the caller's credentials (including a bearer token in
`Sec-WebSocket-Protocol`), impersonation, `Audit-ID` and `Connection`-named headers, and forwards
through a pluggable `UpstreamTransport`. `pkg/upstream` implements the transport, and nothing
implements the authenticator yet or serves or deploys the handler.

`pkg/upstream` sends `in-cluster`, and the proxy's own cluster when it is given that cluster's
`gke_` ID, to the API server through the pod's service account. Any other `gke_` cluster is reached
with the proxy's Application Default Credentials: it describes the cluster through the Container
API and picks its endpoint by rules 1, 2 and 4 of
[`private-endpoint-selection.md`](../docs/designs/private-endpoint-selection.md) (the private
endpoint of rule 3 is not chosen yet). A resolved endpoint is reused for ten minutes and a failed
resolution for ten seconds; an endpoint that cannot be dialled is described again after ten
seconds. An IP endpoint is verified against the cluster's own CA and never the system roots, an
`http://` endpoint and a redirect are refused, and a `kind-` cluster has no upstream.

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
