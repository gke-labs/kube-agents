# k8s-api-proxy

The home of a Kubernetes API proxy for the agent sandbox, which is being written. The plan is
that the sandbox's `kubectl` talks to it, and the proxy authenticates the caller, applies a
per-request policy, redacts Secret values, and forwards the request to the cluster's API server
with credentials the sandbox never holds. Only the policy exists so far
(`pkg/k8sapiproxy/policy.go`); nothing serves or deploys it.

The policy has three modes. `read-only` permits `get`, `list` and `watch` and the discovery
paths. `read-only-dry-run`, the default and the mode kube-agents will run, also permits `create`,
`update` and `patch` carrying `dryRun=All`. `read-write` permits writes. Every mode refuses
stream upgrades (`exec`, `attach`, `portforward`, `proxy`; `read-write` can opt in),
credential minting, assigning pods to nodes, and the token and access review APIs. With Secret
redaction on, Secret subresources and Secret `update` and `patch` are refused in every mode,
since even a dry-run patch can reveal a value. Redaction covers what the Secrets API returns, not
what a workload prints: in `read-write`, a pod that echoes a Secret exposes it through its log.

It is a self-contained Go module, and nothing outside this directory builds it. Run its checks
from this directory:

```bash
gofmt -l .
go vet ./...
go test -race ./...
```
