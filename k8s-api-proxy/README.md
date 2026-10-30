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

The policy has three modes. `read-only` permits `get`, `list` and `watch` and the discovery
paths. `read-only-dry-run`, the default and the mode kube-agents will run, also permits `create`,
`update` and `patch` carrying `dryRun=All`. `read-write` permits writes. Every mode refuses
stream upgrades (`exec`, `attach`, `portforward`, `proxy`; `read-write` can opt in),
credential minting, assigning pods to nodes, and the token and access review APIs. With Secret
redaction on, Secret subresources and Secret `update` and `patch` are refused in every mode,
since even a dry-run patch can reveal a value. Redaction covers what the Secrets API returns, not
what a workload prints: in `read-write`, a pod that echoes a Secret exposes it through its log.
The handler does not redact responses yet, so with redaction on it also refuses Secret `get`,
`list`, `watch`, `delete` and `deletecollection`, whose responses carry values even for a dry run.

It is a self-contained Go module, and nothing outside this directory builds it. Run its checks
from this directory:

```bash
gofmt -l .
go vet ./...
go test -race ./...
```
