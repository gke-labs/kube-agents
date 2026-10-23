# Kubernetes API Proxy

## Summary

The shell sandbox runs the agent's terminal, file tools, and scripts in an isolated pod with no cloud or cluster credentials. Today, when the agent runs `kubectl`, a wrapper script in the sandbox sends the command-line arguments (`argv`) over HTTP to `credential-proxy`. The `credential-proxy` pod validates `argv` against a flag denylist and spawns a `kubectl` child process that holds the cluster credentials.

That `argv` wrapper has three structural limits:

1. It must re-implement `kubectl` flag parsing in Python to block dangerous flags (`--token`, `--server`, `--kubeconfig`, `--raw`), and any flag-parsing mismatch between the checker and `kubectl` becomes a policy bypass.
2. It spawns a `kubectl` and `gke-gcloud-auth-plugin` child process (~102 MiB RSS) inside `credential-proxy` for every call, which caps concurrency at 4–8 commands in a 1–2 GiB pod.
3. It runs `kubectl` on `credential-proxy`'s filesystem instead of the sandbox's filesystem, and it cannot return `Secret` metadata (`kubectl get secrets`) without either blocking all `Secret` access or exposing secret values.

This document designs **`kube-proxy`** (`k8s-api-proxy`), a standalone Go Kubernetes API reverse proxy that replaces the `credential-proxy` `kubectl` wrapper. Stock `kubectl` runs natively inside the shell sandbox against a credential-free `kubeconfig` that points to `kube-proxy`. `kube-proxy` authenticates the sandbox caller, parses each HTTP request with the Kubernetes API server's own `RequestInfoFactory`, enforces read-only and server-side dry-run verb rules, rewrites `Secret` reads to `PartialObjectMetadata` with response redaction, attaches the upstream cluster credential, and forwards the request to `kube-apiserver`.

`credential-proxy` remains in place for Git forge operations (`/v1/vcs/*`), read-only Cloud REST calls (`/v1/gcp/*`), and `gcloud` execution (`/v1/exec`). All Kubernetes operations move to `kube-proxy`.

| Layer                                      | Where it lives                                                                                                                       |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| Go module                                  | [`k8s-api-proxy/go.mod`](../../k8s-api-proxy/go.mod) (`github.com/gke-labs/kube-agents/k8s-api-proxy`)                               |
| Core HTTP handler and L7 policy            | [`k8s-api-proxy/pkg/k8sapiproxy/`](../../k8s-api-proxy/pkg/k8sapiproxy/)                                                             |
| Pluggable upstream transport interface     | [`k8s-api-proxy/pkg/k8sapiproxy/transport.go`](../../k8s-api-proxy/pkg/k8sapiproxy/transport.go)                                     |
| GKE HTTPS upstream transport (`gke-https`) | [`k8s-api-proxy/pkg/transport/gkehttps/`](../../k8s-api-proxy/pkg/transport/gkehttps/)                                               |
| Standalone proxy binary                    | [`k8s-api-proxy/cmd/k8s-api-proxy/`](../../k8s-api-proxy/cmd/k8s-api-proxy/)                                                         |
| Container image                            | [`deploy/kube-proxy/Dockerfile`](../../deploy/kube-proxy/Dockerfile)                                                                 |
| Sandbox `kubectl` and `kubeconfig` setup   | [`deploy/sandbox/Dockerfile`](../../deploy/sandbox/Dockerfile), [`deploy/sandbox/entrypoint.sh`](../../deploy/sandbox/entrypoint.sh) |
| Operator manifests                         | [`k8s-operator/internal/controller/kube_proxy_manifests.go`](../../k8s-operator/internal/controller/kube_proxy_manifests.go)         |

## How to read this document

Each section goes one level deeper than the section before it. Stop as soon as you have the level of detail you need.

| Section                           | What it gives you                                                                           |
| --------------------------------- | ------------------------------------------------------------------------------------------- |
| [Why](#why)                       | the problem, the architectural shift, and the benefits in one page                          |
| [Background](#background)         | how `credential-proxy` runs `kubectl` today and why `argv` wrapping hits a ceiling          |
| [The decision](#the-decision)     | why we move `kubectl` to an HTTP reverse proxy with a pluggable transport                   |
| [The design](#the-design)         | package split, wire protocol, L7 verb policy, `Secret` redaction, and multi-cluster routing |
| [The experiment](#the-experiment) | how we verify security parity, streaming, and memory footprint against `credential-proxy`   |

---

## Why

Sandboxed agents need to run stock `kubectl` in bash pipelines (`kubectl get pods -A -o json | jq ...`, `kubectl logs -f`, `kubectl apply --dry-run=server`) without holding cluster credentials inside the sandbox.

Today, `kube-agents` places a Python `argv` shim at `/opt/credential-proxy/bin/kubectl` inside the sandbox. The shim sends the `argv` list to `credential-proxy`, which inspects the flags and spawns a real `kubectl` subprocess on the trusted side. This approach couples security to `kubectl`'s client-side CLI flag grammar (~50 global flags, abbreviations, `--raw` URLs, and subcommand-specific flags) and pays ~102 MiB of child-process memory for every concurrent `kubectl` call.

`kube-proxy` moves the boundary from **`kubectl` CLI `argv`** to the **Kubernetes HTTP API wire protocol**:

- **Stock `kubectl` runs inside the sandbox**: The sandbox image ships the unmodified `kubectl` binary and a credential-free `kubeconfig` pointed at `kube-proxy`. Files, pipes, `-f -`, `--output-directory`, and `kubectl config view` execute locally inside the sandbox.
- **Canonical Kubernetes API parsing**: `kube-proxy` parses every HTTP request with `k8s.io/apiserver/pkg/endpoints/request.RequestInfoFactory`—the exact code `kube-apiserver` uses to resolve API groups, versions, resources, subresources, and verbs.
- **Safe `Secret` discovery without payload disclosure**: When `kubectl` reads `v1/secrets`, `kube-proxy` rewrites the request to `PartialObjectMetadata` and redacts `.data`, `.stringData`, and `kubectl.kubernetes.io/last-applied-configuration` on the response. Agents can verify whether a `Secret` exists and inspect its labels and timestamps without ever seeing secret values.
- **Zero child processes per request**: `kube-proxy` is a stateless Go HTTP reverse proxy. It replaces ~102 MiB per-command child processes with multiplexed HTTP/2 streams.
- **Reusable library with pluggable transport**: `k8s-api-proxy` separates the pure `http.Handler` policy engine (`pkg/k8sapiproxy`) from the upstream `http.RoundTripper` transport (`pkg/transport/gkehttps`). Other sandbox runtimes (such as NVIDIA OpenShell) and managed platforms can import `pkg/k8sapiproxy` directly and supply their own upstream transport.

---

## Background

### Two security assets: cluster credentials and in-cluster `Secret` data

A Kubernetes sandbox boundary must protect two distinct assets:

- **Asset A1 (Upstream cluster credential)**: The Google Cloud OAuth access token or Kubernetes `ServiceAccount` token that authenticates `kubectl` to `kube-apiserver`. If this token enters the sandbox, the agent can call `kube-apiserver` or Google Cloud APIs directly and bypass sandbox policy.
- **Asset A2 (In-cluster `Secret` values)**: Sensitive data stored inside the target cluster. This includes `v1/Secret` `.data` and `.stringData` fields, `kubectl.kubernetes.io/last-applied-configuration` annotations on `Secret` objects, `serviceaccounts/token` minting, and `pods/exec` into privileged pods.

### How `credential-proxy` handles `kubectl` today

In the three-pod layout ([`docs/designs/agent-shell-sandboxing.md`](agent-shell-sandboxing.md)), the agent's shell runs in `<agent>-shell` with no GCP or Kubernetes credentials. `/opt/credential-proxy/bin/kubectl` is a symlink to `credential_proxy_client.py`, which sends `POST /v1/exec` to `credential_proxy.py` in the `<agent>-credential-proxy` pod.

On every `kubectl` call, `credential_proxy.py`:

1. Authenticates the sandbox's projected `ServiceAccount` token via `TokenReview`.
2. Evaluates `argv` in `command_policy.py` against `KUBECTL_READ_VERBS`, `_KUBECTL_FLAGS_WITH_VALUE`, `_KUBECTL_IDENTITY_FLAGS`, and `policy.json`.
3. Resolves the target GKE cluster (`gke_<project>_<location>_<cluster>`), runs `gcloud container clusters get-credentials` if no kubeconfig exists on the broker's volume, and spawns a `kubectl` child process.
4. Buffers and truncates `stdout` and `stderr` up to `CREDENTIAL_PROXY_MAX_OUTPUT_BYTES` and returns a JSON envelope (`exit_code`, `stdout`, `stderr`) to the sandbox shim.

### Where `argv` wrapping hits a ceiling

1. **Checker-executor parsing divergence**: `kubectl` uses `pflag` and `cobra`, which accept flag abbreviations, repeated flags where the last flag wins, nested subcommands, and `--raw` paths. Every difference between `command_policy.py`'s parser and `kubectl`'s parser is a potential security bypass. For example, `command_policy.py` must refuse `kubectl apply --dry-run=server` altogether because a trailing `--dry-run=none` after an unparsed flag would execute a live write.
2. **All-or-nothing `Secret` access**: At the `argv` layer, blocking `secret` words in `policy.json` either prevents the agent from checking whether a required `Secret` exists or leaks secret payloads through `kubectl get all`, `--raw /api/v1/secrets`, or `last-applied-configuration` annotations.
3. **Filesystem split between sandbox and broker**: Because `kubectl` executes inside the `<agent>-credential-proxy` pod, any file flag (`-f ./manifest.yaml`, `--patch-file`, `cluster-info dump --output-directory`) references the broker's filesystem instead of the sandbox's filesystem. `credential_proxy_client.py` carries ~500 lines of special-case code just to translate `--kubeconfig`, `$KUBECONFIG`, and per-shell `.context` pin files across the pod boundary.
4. **Child-process memory ceiling**: Each concurrent `kubectl` + `gke-gcloud-auth-plugin` invocation in `credential-proxy` consumes ~102 MiB of resident memory ([`docs/designs/credential-proxy-child-memory-budget.md`](credential-proxy-child-memory-budget.md)). A 1 GiB Autopilot pod can admit only 4 concurrent commands across all kanban workers and chat sessions.

### Why `credential-proxy` stays for Git forges and `gcloud`

Git forge operations (`/v1/vcs/*`, designed in [`docs/designs/version-control-support.md`](version-control-support.md)), read-only Cloud REST calls (`/v1/gcp/*`, designed in [`docs/designs/gcp-api-relay.md`](gcp-api-relay.md)), and `gcloud` CLI commands operate on non-Kubernetes protocols and credentials (GitHub App / GitLab PAT tokens and Google Cloud APIs). Those routes remain in `credential-proxy`. Moving `kubectl` to `kube-proxy` removes Kubernetes CLI execution from `credential-proxy` without disturbing the VCS or Cloud API paths.

### Why generic HTTP credential proxies and IAM token downscoping are not enough

Two alternatives appear simpler at first glance, but neither protects **Asset A2** (`Secret` values) or enforces Kubernetes API semantics:

1. **Server-side IAM or RBAC token downscoping**:
   - Standard Kubernetes RBAC and Google Cloud IAM evaluate permissions at the resource and verb level (`get` or `list` on `secrets`). They cannot permit `kubectl get secrets` metadata while hiding `.data` and `.stringData` payloads.
   - Neither IAM nor RBAC can permit `create`, `update`, and `patch` requests only when the `?dryRun=All` query parameter is present.
2. **Generic HTTP credential proxies (such as Envoy or NVIDIA OpenShell's built-in HTTP proxy)**:
   - Generic sandbox proxies keep `Asset A1` outside the sandbox and inject `Authorization: Bearer <token>` on outbound HTTP requests.
   - However, generic proxies have no Kubernetes API grammar (`RequestInfoFactory`). Outbound L7 rules (such as OpenShell's `OPA/Rego` rules on `method` and `path`) inspect only request headers before forwarding. They cannot strip `Impersonate-*` or `X-Remote-*` headers, cannot enforce `?dryRun=All` on mutations, cannot rewrite `Accept` headers to `PartialObjectMetadata`, and cannot scrub `v1/Secret` response bodies (`Asset A2`).
   - Instead of replacing generic sandboxes, `kube-proxy` acts as the Kubernetes-aware backend behind them (see [Section 12](#12-integration-with-generic-agent-sandboxes-nvidia-openshell)).

---

## The decision

We will build **`kube-proxy`** as a standalone Go module (`k8s-api-proxy/`) inside this repository and route all sandbox `kubectl` traffic through it.

Three architectural decisions govern the design:

1. **Enforce policy on the Kubernetes HTTP API wire protocol, not on `kubectl` `argv`.**
   Every `kubectl` command ultimately sends standard Kubernetes REST requests (`/api/v1/...`, `/apis/<group>/<version>/...`) to `kube-apiserver`. Parsing the HTTP method, URL path, and query parameters with `k8s.io/apiserver/pkg/endpoints/request.RequestInfoFactory` eliminates CLI flag-parsing ambiguity and enforces the exact resource, subresource, and verb boundaries that `kube-apiserver` evaluates.
2. **Separate the core HTTP handler (`pkg/k8sapiproxy`) from the upstream transport (`pkg/transport/gkehttps`).**
   `pkg/k8sapiproxy` is a pure Go `http.Handler` that depends only on the Go standard library, `k8s.io/apimachinery`, and `k8s.io/apiserver`. It accepts an `UpstreamTransport` interface (`RoundTripCluster(ctx, clusterID, req)`) and knows nothing about how upstream tokens are minted or how cluster endpoints are dialed. For `kube-agents`, `pkg/transport/gkehttps` (`gke-https`) implements `UpstreamTransport` over HTTPS using GKE control-plane discovery (`container.googleapis.com`), in-cluster `ServiceAccount` credentials, and GKE Workload Identity OAuth tokens. Other environments can import `pkg/k8sapiproxy` and plug in generic kubeconfig transports, custom RPC tunnels, or Unix-socket transports without modifying the handler.
3. **Keep `k8s-api-proxy` as an independent Go submodule in `gke-labs/kube-agents`.**
   Following the existing pattern of `k8s-operator/go.mod` and `a2a/go.mod`, `k8s-api-proxy/go.mod` defines `module github.com/gke-labs/kube-agents/k8s-api-proxy`. External projects can import `github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy` without pulling in the rest of `kube-agents`, and the repository builds a dedicated `kube-proxy` container image alongside `agent-sandbox` and `credential-proxy`.

---

## The design

### 1. Module and package layout

```text
k8s-api-proxy/
├── go.mod                        # module github.com/gke-labs/kube-agents/k8s-api-proxy
├── go.sum
├── cmd/
│   └── k8s-api-proxy/
│       └── main.go               # CLI flags, server lifecycle, wires pkg/k8sapiproxy + pkg/transport/gkehttps
└── pkg/
    ├── k8sapiproxy/
    │   ├── handler.go            # Core http.Handler: auth -> route -> RequestInfo -> policy -> proxy
    │   ├── policy.go             # Verb allowlist, subresource denylist, dryRun=All check
    │   ├── secrets.go            # PartialObjectMetadata rewrite + Secret response redaction
    │   ├── headers.go            # Strip Authorization, Impersonate-*, X-Remote-*, unsafe Upgrade headers
    │   ├── auth.go               # CallerAuthenticator interface + TokenReview authenticator with TTL cache
    │   ├── audit.go              # AuditSink interface, AuditEvent struct, and JSON/slog audit sinks
    │   ├── metrics.go            # Closed-vocabulary Prometheus metrics collector
    │   ├── telemetry.go          # OpenTelemetry OTLP HTTP tracer setup
    │   └── transport.go          # UpstreamTransport interface
    └── transport/
        └── gkehttps/
            ├── transport.go      # GKE HTTPS UpstreamTransport (gke-https) for in-cluster and fleet GKE clusters
            └── gke_discovery.go  # Resolves gke_<project>_<location>_<cluster> via container.googleapis.com
```

#### Core interfaces (`pkg/k8sapiproxy`)

```go
package k8sapiproxy

import (
	"context"
	"net/http"
)

// CallerIdentity describes an authenticated sandbox caller.
type CallerIdentity struct {
	Username string
	UID      string
	Groups   []string
}

// CallerAuthenticator verifies that an incoming request comes from an authorized sandbox.
type CallerAuthenticator interface {
	AuthenticateRequest(r *http.Request) (*CallerIdentity, error)
}

// UpstreamTransport forwards a sanitized Kubernetes API request to the target cluster.
// The request URL Path and RawQuery are already normalized to canonical kube-apiserver paths
// (for example, "/api/v1/namespaces/default/pods").
type UpstreamTransport interface {
	RoundTripCluster(ctx context.Context, clusterID string, req *http.Request) (*http.Response, error)
}

// AuditSink records structured Kubernetes API audit events.
// Implementations must be safe for concurrent use by multiple goroutines.
type AuditSink interface {
	Record(ctx context.Context, event AuditEvent)
}

// Mode controls which Kubernetes API verbs the proxy permits.
type Mode string

const (
	// ModeReadOnly permits get, list, and watch on resources, plus safe discovery paths.
	ModeReadOnly Mode = "read-only"
	// ModeReadOnlyWithDryRun additionally permits create, update, and patch when dryRun=All is set.
	// This is the mode that kube-agents uses.
	ModeReadOnlyWithDryRun Mode = "read-only-dry-run"
	// ModeReadWrite permits mutating verbs (create, update, patch, delete, deletecollection)
	// for external agent platforms that authorize writes, while still stripping caller auth/impersonation
	// headers, redacting Secret payloads, and refusing credential-minting subresources.
	ModeReadWrite Mode = "read-write"
)

// Config configures the k8sapiproxy HTTP handler.
type Config struct {
	Authenticator            CallerAuthenticator
	Upstream                 UpstreamTransport
	DefaultClusterID         string
	Mode                     Mode
	RedactSecrets            bool
	AllowUpgradeSubresources bool
	Metrics                  *Metrics
	AuditSink                AuditSink
	TracerProvider           trace.TracerProvider
}
```

### 2. End-to-end request flow

```mermaid
sequenceDiagram
    participant K as Stock kubectl (in <agent>-shell pod)
    participant P as kube-proxy (pkg/k8sapiproxy)
    participant T as GKE HTTPS Transport (pkg/transport/gkehttps)
    participant A as Target kube-apiserver

    K->>P: GET /clusters/gke_proj_loc_clu/api/v1/namespaces/default/secrets<br/>Authorization: Bearer <projected-sa-token>
    P->>P: 1. Verify caller token via CallerAuthenticator (TokenReview cache)
    P->>P: 2. Extract clusterID ("gke_proj_loc_clu") & normalize path ("/api/v1/namespaces/default/secrets")
    P->>P: 3. Strip caller Authorization, Impersonate-*, X-Remote-* headers
    P->>P: 4. Parse RequestInfo (Verb="list", Resource="secrets", Version="v1")
    P->>P: 5. Enforce Policy (allow "list"; rewrite Accept -> PartialObjectMetadata)
    P->>T: 6. RoundTripCluster(ctx, "gke_proj_loc_clu", sanitizedReq)
    T->>A: GET /api/v1/namespaces/default/secrets<br/>Authorization: Bearer <upstream-gcp-or-ksa-token>
    A-->>T: 200 OK (PartialObjectMetadataList / SecretList)
    T-->>P: *http.Response
    P->>P: 7. Redact any residual .data, .stringData, last-applied-configuration
    P-->>K: 200 OK (Metadata only)
```

### 3. URL routing and multi-cluster addressing

`kube-proxy` serves both single-cluster and multi-cluster requests from one listener:

- **Explicit cluster prefix**: `/clusters/{clusterID}/{k8s-path...}`
  - `{clusterID}` is the keyword `in-cluster`, a GKE context name matching `^gke_[a-z0-9-]+_[a-z0-9-]+_[a-z0-9-]+\z`, or a `kind-<cluster>` context name.
  - `pkg/k8sapiproxy` extracts `{clusterID}` and rewrites `req.URL.Path` to `/{k8s-path...}` before running `RequestInfoFactory`.
- **Default cluster root**: `/{k8s-path...}` (any path not starting with `/clusters/`)
  - Routes to `Config.DefaultClusterID` (set from `KUBE_PROXY_DEFAULT_CLUSTER` or `KUBE_CONTEXT_NAME`, or `in-cluster` when unset).

Before parsing the path, `pkg/k8sapiproxy` rejects any request whose raw path contains `.` or `..` segments, empty internal segments (`//`), or percent-encoded slashes (`%2f`, `%2F`) with `400 Bad Request`. This guarantees that `RequestInfoFactory` and the upstream `kube-apiserver` interpret the URL path identically.

### 4. Sandbox `kubectl` and `kubeconfig` setup

In [`deploy/sandbox/Dockerfile`](../../deploy/sandbox/Dockerfile):

- The stock `kubectl` binary is installed at `/usr/local/bin/kubectl`.
- `/opt/credential-proxy/bin/kubectl` is removed; `/opt/credential-proxy/bin/gcloud` remains a symlink to `/usr/local/bin/credential-proxy-exec`.
- No client-side `kubectl` `exec` auth plugin (such as `gke-gcloud-auth-plugin`) is installed in the sandbox. Running `gke-gcloud-auth-plugin` inside the sandbox would place the upstream Google Cloud access token (**Asset A1**) inside the untrusted environment. Instead, `kube-proxy` attaches the upstream token outside the sandbox.

In the sandbox, `kubectl` uses a credential-free `kubeconfig` where every cluster entry points at the `<agent>-kube-proxy` Service over HTTPS, trusts the operator-generated CA certificate mounted at `/var/run/secrets/kubeagents/kube-proxy-ca/ca.crt`, and authenticates with the sandbox pod's projected `ServiceAccount` token (`CREDENTIAL_PROXY_TOKEN_FILE`, mounted at `/var/run/secrets/kubeagents/credential-proxy/token`):

```yaml
apiVersion: v1
kind: Config
current-context: gke_my-project_us-central1_host-cluster
clusters:
  - name: gke_my-project_us-central1_host-cluster
    cluster:
      server: https://platform-agent-kube-proxy:8443/clusters/gke_my-project_us-central1_host-cluster
      certificate-authority: /var/run/secrets/kubeagents/kube-proxy-ca/ca.crt
users:
  - name: kube-proxy-caller
    user:
      tokenFile: /var/run/secrets/kubeagents/credential-proxy/token
contexts:
  - name: gke_my-project_us-central1_host-cluster
    context:
      cluster: gke_my-project_us-central1_host-cluster
      user: kube-proxy-caller
```

(`client-go` sends `tokenFile` bearer tokens only over `https://` connections. The operator generates an ECDSA CA and server certificate in the `<agent>-kube-proxy-tls` Secret, mounts `tls.crt` and `tls.key` into `<agent>-kube-proxy`, and mounts only `ca.crt` read-only into `<agent>-shell`.)

How multi-cluster context switching works:

1. **`sandbox-entrypoint`**: On startup, writes the default `kubeconfig` at `/opt/data/.kubeconfig` (and sets `KUBECONFIG=/opt/data/.kubeconfig`) seeded with `KUBE_CONTEXT_NAME`, `certificate-authority: /var/run/secrets/kubeagents/kube-proxy-ca/ca.crt`, and `tokenFile: /var/run/secrets/kubeagents/credential-proxy/token` (from `CREDENTIAL_PROXY_TOKEN_FILE`).
2. **`gcloud container clusters get-credentials`**: When the agent runs `gcloud container clusters get-credentials <cluster> --location=<loc> --project=<proj>`, `credential_proxy_client.py` writes a `kubeconfig` entry whose `server:` points to `${KUBE_PROXY_URL}/clusters/gke_<project>_<location>_<cluster>`, whose `certificate-authority:` points to `/var/run/secrets/kubeagents/kube-proxy-ca/ca.crt`, and whose `user:` (`kube-proxy-caller`) references `tokenFile` from `CREDENTIAL_PROXY_TOKEN_FILE`. No cluster credentials or GCP tokens ever enter the sandbox.
3. **Stock `kubectl config` commands**: An agent or script can also register any fleet cluster directly inside the sandbox with `kubectl config set-cluster gke_<project>_<location>_<cluster> --server="${KUBE_PROXY_URL}/clusters/gke_<project>_<location>_<cluster>" --certificate-authority=/var/run/secrets/kubeagents/kube-proxy-ca/ca.crt`.

### 5. Caller authentication (`pkg/k8sapiproxy/auth.go`)

`kube-proxy` authenticates every request from the sandbox before inspecting the Kubernetes API path:

1. Reads `Authorization: Bearer <caller-token>` from the incoming request and immediately deletes the `Authorization` header so the caller's token can never travel upstream.
2. Verifies the token against the host cluster's `authentication.k8s.io/v1` `TokenReview` API with `spec.audiences` set from `CREDENTIAL_PROXY_AUDIENCE` (`"kubeagents-credential-proxy"`, matching the projected `ServiceAccount` token volume mounted into `<agent>-shell` at `/var/run/secrets/kubeagents/credential-proxy/token`).
3. Checks that `status.user.username` matches the configured sandbox caller `ServiceAccount` allowlist (`CREDENTIAL_PROXY_ALLOWED_CALLERS` / `-allowed-callers`).
4. Caches verified token hashes (`sha256(token)`) in memory for `min(60s, tokenExpiry)` (parsing the JWT `exp` claim to bound the cache lifetime) and evicts expired entries on write so repeated `kubectl` calls do not block on `TokenReview` round trips.

**Why the projected `ServiceAccount` token is not a cluster credential**:
The token mounted at `/var/run/secrets/kubeagents/credential-proxy/token` is a short-lived Kubernetes projected `ServiceAccount` JWT bound to `aud: ["kubeagents-credential-proxy"]`. It is not a Kubernetes API token (`aud: "https://kubernetes.default.svc"`) and not a Google Cloud access token (**Asset A1**). If a caller attempts to present this token directly to `kube-apiserver`, `kube-apiserver` rejects it because the audience does not match.

**Caller authentication across sandbox substrates**:
Because `Config.Authenticator` is an interface (`CallerAuthenticator`), deployments can choose the caller authentication mechanism that matches their sandbox boundary:

- **Projected `ServiceAccount` JWT (`TokenReview`)**: Default for multi-pod Kubernetes sandboxes (`kube-agents`).
- **Workload mTLS (`client-certificate` / `client-key` or SPIFFE X.509 SVID)**: The sandbox authenticates to `kube-proxy` with a client TLS certificate without reading a bearer token file.
- **Host-local network isolation (`veth` pair or Unix domain socket)**: When `kube-proxy` runs on the host or hypervisor boundary for a single local sandbox (such as a local VM or NVIDIA OpenShell supervisor), binding `kube-proxy` to the dedicated host-side interface or socket authenticates the caller by transport endpoint (`StaticAuthenticator`).

### 6. Request parsing and L7 verb policy (`pkg/k8sapiproxy/policy.go`)

`pkg/k8sapiproxy` constructs a `request.RequestInfoFactory` configured with `APIPrefixes: sets.NewString("api", "apis")` and `GrouplessAPIPrefixes: sets.NewString("api")`.

Every request is classified as either a **resource request** (`info.IsResourceRequest == true`) or a **non-resource request** (`info.IsResourceRequest == false`). `pkg/k8sapiproxy` supports three modes (`read-only`, `read-only-dry-run`, and `read-write`) so external agent platforms can select their posture. In `kube-agents`, the operator always configures `kube-proxy` with `read-only-dry-run` (`ModeReadOnlyWithDryRun`) so agents can validate manifests with `kubectl apply --dry-run=server` without mutating cluster state:

| Request class          | Allowed in `read-only`                                                                                                         | Allowed in `read-only-dry-run` (`kube-agents` default)                                                         | Allowed in `read-write`                                                                                                                                                                         | Refused in all modes (`403 Forbidden`)                                                                                                                                             |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Non-resource paths** | `GET` or `HEAD` on `/api`, `/apis`, `/version`, `/openapi/v2`, `/openapi/v3`, `/openapi/v3/*`, `/healthz`, `/livez`, `/readyz` | Same as `read-only`                                                                                            | Same as `read-only`                                                                                                                                                                             | Any other non-resource path (`/metrics`, `/debug/*`, `/logs/*`) or any non-`GET`/`HEAD` method                                                                                     |
| **Resource verbs**     | `get`, `list`, `watch`                                                                                                         | `get`, `list`, `watch`, plus `create`, `update`, `patch` **only when** query parameter `dryRun=All` is present | `get`, `list`, `watch`, `create`, `update`, `patch`, `delete`, `deletecollection`                                                                                                               | `proxy`, `connect` (and mutations without `dryRun=All` in `read-only` / `read-only-dry-run`)                                                                                       |
| **Subresources**       | Safe read subresources (`status`, `scale`, `log` via `get`, `list`, `watch`)                                                   | `status` and `scale` with `dryRun=All`                                                                         | `status`, `scale`, `log` (`get`, `list`, `watch`), `eviction` (`create`), plus `exec`/`attach`/`portforward`/`proxy` when `AllowUpgradeSubresources` (`-allow-upgrade-subresources`) is enabled | `exec`, `attach`, `portforward`, `proxy` (unless `ModeReadWrite` with `AllowUpgradeSubresources`), `ephemeralcontainers`, `binding`, `token` (`serviceaccounts/token`), `approval` |

**Dry-run query enforcement**:
When `Mode == ModeReadOnlyWithDryRun` and `info.Verb` is `create`, `update`, or `patch`:

- The proxy checks `req.URL.Query()["dryRun"]`.
- It admits the request only if `len(values) == 1 && values[0] == "All"`, and it explicitly sets `q.Set("dryRun", "All")` on the outbound query string so no duplicate parameter can override it.

**Native Kubernetes refusal responses**:
When the policy blocks a request, `kube-proxy` returns HTTP `403 Forbidden` with a standard `metav1.Status` JSON body:

```json
{
  "kind": "Status",
  "apiVersion": "v1",
  "metadata": {},
  "status": "Failure",
  "message": "kube-proxy policy blocked verb \"delete\" on resource \"pods\": this proxy enforces read-only cluster access. This is a permission boundary, not an error to work around: the change is refused however it is attempted.\npolicy rule: kubernetes.read-only\npolicy verb: delete",
  "reason": "Forbidden",
  "code": 403
}
```

Because `kubectl` natively renders `metav1.Status` responses, the agent sees a clean `Error from server (Forbidden): ...` message with exit code `1`.

### 7. `Secret` metadata rewrite and response redaction (`pkg/k8sapiproxy/secrets.go`)

When `Config.RedactSecrets` is enabled (the default) and `RequestInfo` matches core `v1` `secrets` (`info.APIGroup == "" && info.APIVersion == "v1" && info.Resource == "secrets"`):

1. **Subresource refusal**: Any subresource on a `Secret` is refused with `403 Forbidden`.
2. **Request header rewrite (Layer 1 — server-side stripping)**:
   - Deletes `Accept-Encoding` so `net/http.Transport` manages transparent gzip decompression and the upstream response arrives uncompressed for JSON inspection.
   - If the caller requested a table (`as=Table` in `Accept`, used by `kubectl get secrets`), the proxy preserves `as=Table` because `kube-apiserver`'s `Secret` table printer only returns `NAME`, `TYPE`, `DATA` (count), and `AGE`.
   - For all other `Accept` headers on `get`, `list`, `create`, `update`, and `patch` (such as `kubectl get secret foo -o yaml` or `-o json`, `kubectl apply --dry-run=server`, or `--raw /api/v1/secrets`), the proxy rewrites `Accept` to `application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1` (or `PartialObjectMetadataList` for `list`), instructing `kube-apiserver` to return only `TypeMeta` and `ObjectMeta`.
3. **Response body scrubber (Layer 2 — defense in depth)**:
   - On every response for a `secrets` request (including `PartialObjectMetadata`, `Table`, `watch` streams, or `dryRun=All`), `kube-proxy` decompresses any residual `Content-Encoding: gzip` stream, inspects the JSON payload, and scrubs sensitive payloads while preserving `Secret` key names:
     - Top-level and `items[*]` `.data` and `.stringData` keys are collected (along with any key names recorded in `metadata.managedFields[*].fieldsV1["f:data"]` and `["f:stringData"]`), `.stringData` is removed, and `.data` is populated with `"REDACTED"` values for each key so `kubectl get secret -o yaml` and `kubectl describe secret` display the key names without exposing secret bytes.
     - Annotation `kubectl.kubernetes.io/last-applied-configuration` in `metadata.annotations` (which otherwise contains the plaintext JSON of a `Secret` applied with `kubectl apply`) is deleted, and `kubeagents.x-k8s.io/redacted: "true (secret data values are redacted by kube-proxy)"` is added so `kubectl describe secret` explicitly indicates redaction.
   - If a `secrets` response exceeds the 64 MiB payload cap or cannot be parsed as valid JSON, `kube-proxy` fails closed and returns `502 Bad Gateway` rather than forwarding unverified bytes.

### 8. Header sanitization and streaming (`pkg/k8sapiproxy/headers.go`)

On every incoming request, before invoking `UpstreamTransport`, `pkg/k8sapiproxy` strips:

- `Authorization`, `Proxy-Authorization`, and `Cookie` (consumed or dropped at ingress and never forwarded)
- `Impersonate-User`, `Impersonate-Group`, `Impersonate-Uid`, and any header matching `^Impersonate-Extra-` (case-insensitive)
- `X-Remote-User`, `X-Remote-Group`, and `X-Remote-Extra-*`
- `Connection` and `Upgrade` (unless `ModeReadWrite` with `AllowUpgradeSubresources` is enabled and `Upgrade` is non-empty, in which case `Connection: Upgrade` is set)

It also appends `kproxy/<caller-username>` to the outbound `User-Agent` header so `kube-apiserver` audit logs record the originating sandbox identity alongside `kubectl`'s own `User-Agent`.

For allowed streaming requests (`kubectl logs -f` and `kubectl get -w`), `httputil.ReverseProxy` runs with `FlushInterval: -1` so chunks flush immediately to the sandbox without buffering.

### 9. Pluggable GKE HTTPS transport (`pkg/transport/gkehttps`)

`pkg/transport/gkehttps` (`gke-https`) implements `k8sapiproxy.UpstreamTransport` for GKE clusters in `kube-agents`:

1. **`in-cluster` (or host cluster matching `KUBE_CONTEXT_NAME`)**:
   - Uses `https://kubernetes.default.svc` with the mounted CA certificate (`CREDENTIAL_PROXY_KUBE_CA_FILE`, `/var/run/secrets/kubernetes.io/serviceaccount/ca.crt`) and the proxy pod's projected Kubernetes API `ServiceAccount` token (`CREDENTIAL_PROXY_KUBE_TOKEN_FILE`, `/var/run/secrets/kubeagents/kube-api/token`), avoiding external round trips to `container.googleapis.com` for the host cluster.
2. **Fleet GKE clusters (`gke_<project>_<location>_<cluster>`)**:
   - Validates `<project>`, `<location>`, and `<cluster>` against `^[a-z0-9][a-z0-9-]*\z`.
   - Calls `GET https://container.googleapis.com/v1/projects/{project}/locations/{location}/clusters/{cluster}` using the proxy pod's `GCPTokenSource` (Workload Identity Federation via `CREDENTIAL_PROXY_WIF_AUDIENCE`, `CREDENTIAL_PROXY_WIF_SERVICE_ACCOUNT`, and `CREDENTIAL_PROXY_WIF_TOKEN_FILE`, or the GCE metadata server).
   - Dials `https://<endpoint>` verified against the cluster's `masterAuth.clusterCaCertificate` root CA.
   - Caches the per-cluster `*http.Transport` and resolved endpoint URL in memory with a 5-minute TTL and automatic invalidation on connection failure.
   - Injects `Authorization: Bearer <gcp-access-token>` on every upstream request and refuses `3xx` upstream redirects (`502 Bad Gateway`) so the bearer token is never followed to a redirect target.

### 10. Observability: Prometheus metrics, OpenTelemetry tracing, and audit logging (`pkg/k8sapiproxy/metrics.go`, `pkg/k8sapiproxy/telemetry.go`, `pkg/k8sapiproxy/audit.go`)

`kube-proxy` exposes Prometheus metrics on a dedicated metrics-only HTTP listener (`KUBE_PROXY_METRICS_PORT=8444`, port name `kube-metrics`), exports OpenTelemetry trace spans via OTLP HTTP, and writes a structured JSON audit log entry for every Kubernetes API request:

1. **Prometheus metrics (`/metrics` on port `8444`)**:
   - All label values come from closed vocabularies so caller-supplied paths or CRD resource names cannot increase series cardinality.
   - `kubeagents_kube_proxy_requests_total` (`verb`, `resource`, `subresource`, `mode`, `dry_run`, `outcome`, `status_code`): Total Kubernetes API requests processed by `kube-proxy`.
   - `kubeagents_kube_proxy_rejected_requests_total` (`reason`, `verb`, `resource`, `subresource`, `mode`): Total requests rejected before reaching `kube-apiserver`, labeled by closed rejection reason (`unauthenticated`, `protocol_upgrade_blocked`, `invalid_route`, `discovery_path_blocked`, `auth_resource_blocked`, `secret_subresource_blocked`, `subresource_blocked`, `dry_run_required`, `verb_blocked`).
   - `kubeagents_kube_proxy_secrets_redacted_total` (`action`): Total `core/v1` `Secret` redaction actions (`accept_header_rewritten` and `payload_scrubbed`).
   - `kubeagents_kube_proxy_request_duration_seconds` (`verb`, `resource`, `outcome`): Histogram of request durations in seconds.
   - `kubeagents_kube_proxy_in_flight_requests`: Gauge of requests currently in flight.
   - `process_start_time_seconds`: Gauge of the process start timestamp since the Unix epoch in seconds.
2. **OpenTelemetry distributed tracing**:
   - The operator injects `OTEL_SERVICE_NAME=<agent>-kube-proxy`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`, and `OTEL_RESOURCE_ATTRIBUTES` (or `OTEL_SDK_DISABLED=true` when the cluster has no collector).
   - For every Kubernetes API request, `kube-proxy` extracts incoming W3C `traceparent`/`tracestate` headers before header sanitization, starts a server span (`k8s.api.proxy <verb> <resource>`), records structured attributes (`k8s.cluster.id`, `k8s.verb`, `k8s.resource`, `k8s.subresource`, `k8s.namespace`, `k8s.dry_run`, `kube_proxy.mode`, `kube_proxy.outcome`, `kube_proxy.rejection_reason`, `kube_proxy.secret_redacted`, `http.response.status_code`), and wraps the upstream round trip in a child client span (`k8s.api.upstream`).
3. **Pluggable audit sink and structured JSON audit logging (`pkg/k8sapiproxy/audit.go`)**:
   - `pkg/k8sapiproxy` defines the `AuditSink` interface (`Record(ctx context.Context, event AuditEvent)`), `AuditSinkFunc`, and `NewMultiAuditSink` so OSS deployments and hosted services can route typed `AuditEvent` records to different backends.
   - By default (`NewJSONAuditSink(os.Stdout)`), `kube-proxy` emits a newline-delimited JSON audit record (`event_type: "k8s_api_audit"`) via `log/slog` for every Kubernetes API request (excluding `/_kube_proxy/healthz` health probes) with `cluster`, `caller_username`, `caller_uid`, `caller_groups`, `http_method`, `path`, `verb`, `api_group`, `api_version`, `resource`, `subresource`, `namespace`, `name`, `dry_run`, `mode`, `outcome`, `rejection_reason`, `secret_redacted`, `status_code`, `duration_ms`, `user_agent`, `trace_id`, and `span_id`.
   - Request and response bodies and authentication credentials are never written to the audit log.

### 11. Deployment topology in `kube-agents`

The operator reconciles a dedicated **`<agent>-kube-proxy` Deployment and ClusterIP Service** alongside `<agent>-credential-proxy` ([`k8s-operator/internal/controller/kube_proxy_manifests.go`](../../k8s-operator/internal/controller/kube_proxy_manifests.go)):

- **Separate Deployment and Service**: `<agent>-kube-proxy` runs as its own Deployment (`Recreate` strategy) and exposes port `8443` on the `<agent>-kube-proxy` ClusterIP Service, plus port `8444` (`kube-metrics`) for Prometheus scraping. A dedicated `NetworkPolicy` restricts ingress on port `8443` to the `<agent>-shell` pod, and admits `gmp-system` on port `8444`.
- **Operator-managed TLS (`<agent>-kube-proxy-tls` Secret)**: The operator generates an ECDSA P-256 CA and server certificate with SANs `<agent>-kube-proxy`, `<agent>-kube-proxy.<namespace>`, `<agent>-kube-proxy.<namespace>.svc`, and `<agent>-kube-proxy.<namespace>.svc.cluster.local`. The `<agent>-kube-proxy` pod mounts `tls.crt` and `tls.key` at `/var/run/secrets/kubeagents/kube-proxy-tls`. The `<agent>-shell` pod mounts only the public `ca.crt` key read-only at `/var/run/secrets/kubeagents/kube-proxy-ca/ca.crt`.
- **Upstream identity**: `<agent>-kube-proxy` uses the same Workload Identity / Workload Identity Federation configuration as `<agent>-credential-proxy` to authenticate upstream to `kube-apiserver` and `container.googleapis.com`, and binds its ServiceAccount via a `ClusterRoleBinding` (`<namespace>-<agent>-kube-proxy-tokenreview` -> `ClusterRole/system:auth-delegator` and `<namespace>-<agent>-kube-proxy-reader` -> `ClusterRole/<namespace>-<agent>-cluster-reader`).
- **Clean split of responsibilities**: `<agent>-credential-proxy` continues serving `/v1/vcs/*`, `/v1/gcp/*`, and `gcloud` `/v1/exec` on port `8765`. `<agent>-shell` receives `KUBE_PROXY_URL=https://<agent>-kube-proxy.<namespace>.svc.cluster.local:8443` and routes all `kubectl` operations through `<agent>-kube-proxy`.

### 12. Integration with generic agent sandboxes (NVIDIA OpenShell)

Generic agent sandboxes such as NVIDIA OpenShell (`v0.1.2`) isolate agent processes and route outbound HTTP traffic through an egress proxy with `OPA/Rego` L7 policy rules. However, generic HTTP credential injection does not understand Kubernetes API semantics:

- **Outbound-only `OPA/Rego` evaluation**: OpenShell's `OPA/Rego` L7 engine evaluates only outbound request metadata (`method`, `path`, `host`) before forwarding. It cannot rewrite `Accept` headers to `PartialObjectMetadata`, cannot inspect or scrub `v1/Secret` response bodies (**Asset A2**), cannot strip `Impersonate-*` or `X-Remote-*` headers, and cannot require `?dryRun=All` on `POST`/`PUT`/`PATCH` requests.
- **GKE `kubeconfig` and error-message token leaks in OpenShell `0.1.2`**: OpenShell's built-in `install-kubeconfig.sh` helper fails when parsing standard `gcloud`-generated GKE `kubeconfig` files (`args: null` under `users[*].user.exec`), and when an upstream `kube-apiserver` connection fails, OpenShell's HTTP connector returns the injected `Authorization: Bearer <access-token>` header inside the `502` error message to the sandbox.

`kube-proxy` does not replace generic sandboxes. Instead, it serves as the Kubernetes-aware backend for OpenShell in two ways:

1. **Explicit `tls: skip` proxy endpoint (zero changes to OpenShell)**:
   - Run `k8s-api-proxy` outside the OpenShell sandbox (on the host or in the cluster) and configure the sandbox `kubeconfig` `server:` to point to `https://<kube-proxy>:8443`.
   - In the OpenShell sandbox policy, add a `network_policies` rule for the `kube-proxy` endpoint with `tls: skip` so `kubectl` connects to `kube-proxy` over TLS directly while `kube-proxy` enforces `RequestInfoFactory` verb policy, `Secret` redaction, and upstream GKE authentication.
2. **OpenShell `SupervisorMiddleware` gRPC adapter**:
   - OpenShell defines a `SupervisorMiddleware` gRPC service (`AuthorizeRequest` and `TransformResponse` in `proto/openshell/sandbox.proto`) for external L7 inspection.
   - A small gRPC adapter around `pkg/k8sapiproxy` can implement `AuthorizeRequest` (running `RequestInfoFactory` policy, header sanitization, and `PartialObjectMetadata` header rewrites) and `TransformResponse` (running `Secret` payload redaction) directly inside OpenShell's proxy pipeline.

---

## The experiment

We verify the prototype against the current `credential-proxy` `kubectl` wrapper across four test suites before switching the default in the operator:

1. **Differential parser and policy suite (`k8s-api-proxy/pkg/k8sapiproxy/policy_test.go`)**:
   - Sends the same corpus of Kubernetes API paths (standard resources, cluster-wide `/api/v1/secrets`, aggregated APIs, CRDs named `secrets.example.com`, subresources `pods/log`, `pods/exec`, `serviceaccounts/token`, `--raw` paths, path-traversal encodings, and `dryRun` query permutations) through both `pkg/k8sapiproxy` and a real `kube-apiserver` `RequestInfoFactory` to verify zero parser disagreement and fail-closed behavior.
2. **`Secret` redaction suite (`k8s-api-proxy/pkg/k8sapiproxy/secrets_test.go`)**:
   - Verifies that `kubectl get secrets`, `kubectl get secret <name> -o yaml`, `kubectl get secret <name> -o jsonpath='{.data}'`, `kubectl get secrets -w`, and `kubectl get --raw /api/v1/secrets` return resource metadata (`name`, `namespace`, `labels`, `creationTimestamp`) while never returning `.data`, `.stringData`, or `kubectl.kubernetes.io/last-applied-configuration` bytes.
3. **End-to-end sandbox `kubectl` compatibility**:
   - Verifies stock `kubectl` inside `<agent>-shell` across pipelines (`kubectl get pods -A -o json | jq`), streaming (`kubectl logs -f`, `kubectl get events -w`), server-side dry-run (`kubectl apply -f - --dry-run=server`), and multi-cluster context switching (`kubectl --context=gke_<project>_<location>_<cluster>`).
4. **Concurrency and memory benchmark**:
   - Runs 16 parallel multi-cluster `kubectl` listing passes from `<agent>-shell` and measures resident memory in the `<agent>-credential-proxy` pod compared to the 102 MiB-per-child `argv` wrapper baseline.
5. **Future extension — Large-scale fleet simulation (`UpstreamTransport` for 100+ clusters and 10,000+ workloads)**:
   - Provisioning 100+ live GKE clusters and 10,000+ workloads for fleet-scale `bench/` evaluations is expensive and slow. Because `pkg/k8sapiproxy` depends only on the `UpstreamTransport` interface (`RoundTripCluster(ctx, clusterID, req)`), we can add a simulated or replay `UpstreamTransport` that serves pre-canned cluster snapshots or parameterized synthetic topologies (100+ clusters, 10,000+ workloads, with injected failure modes such as `CrashLoopBackOff`, `OOMKilled`, or stockout events). Stock `kubectl` and multi-cluster audit scripts inside `<agent>-shell` run against `/clusters/gke_<project>_<location>_<cluster>/...` through the full proxy policy and `Secret` redaction pipeline without provisioning live GKE clusters.
