# k8s-api-proxy

The Kubernetes API proxy for the agent sandbox and A2A session pods (`<agent>-kube-proxy` deployment). Each caller's
`kubectl` talks to it over cluster-routed URLs (`/clusters/<cluster-id>/...` by default, without `--upstream-url`), and the proxy
authenticates the caller (a `CallerAuthenticator`; the binary defaults to `pkg/tokenreview`), applies a per-request policy
(`pkg/k8sapiproxy/policy.go`), redacts Secret values (`pkg/k8sapiproxy/secrets.go`), removes the
caller's credentials (including a bearer token in `Sec-WebSocket-Protocol`), impersonation,
`Audit-ID` and `Connection`-named headers (`pkg/k8sapiproxy/headers.go`), forwards the request to
the cluster's API server through `pkg/upstream` with credentials the sandbox never holds, and emits
structured audit records (`pkg/k8sapiproxy/audit.go`), Prometheus metrics
(`pkg/k8sapiproxy/metrics.go`), and OpenTelemetry spans (`pkg/k8sapiproxy/telemetry.go`).
`cmd/k8s-api-proxy` builds the standalone server binary from `pkg/proxyserver`, which holds its
flags, environment variables and lifecycle.

`tokenreview.NewAuthenticator` (`pkg/tokenreview`) is the binary's default `CallerAuthenticator` (`--auth-mode=token-review`): it accepts a bearer token
that a TokenReview on the host cluster finds valid for any configured audience (`Audience`, `Audiences`, or `AudienceCallers`), from a username in that audience's allowlist, and caches each acceptance or refusal for ten seconds by default. The audiences should
be ones the API server does not accept, so a token sent to the proxy cannot be used against the API
server directly; the operator picks them. A review that fails is a 503 rather than a 401. It also
holds each request to the pod its token was issued to (`podbinding.go`): the TokenReview names the pod
(`user.extra` `authentication.kubernetes.io/pod-name` and `pod-uid`), the proxy reads it (`get` on
pods in the caller's namespace), checks its UID, and requires the request's direct peer
(`RemoteAddr`, never a header) to be one of its `status.podIPs`. Anything short of that, a token
without a pod or an error reading the pod included, is the same 401 as an unknown token, so a
token is only good from its own pod and `kubectl port-forward` to the proxy is refused. In an `--auth-mode` chain that refusal is final (it wraps
`k8sapiproxy.ErrCallerRefused`), and `jwtauth` refuses a token with the `kubernetes.io` claim, so
no other authenticator admits a ServiceAccount token.

`jwtauth.NewAuthenticator` (`pkg/jwtauth`, `--auth-mode=jwt` in the binary) is for a platform that
mints a signed token per sandbox, such as an agent runtime that injects one at its egress gateway. It accepts a bearer
JWT that names its key (`kid`), verifies against the issuer's JSON Web Key Set fetched over HTTPS
(RS\*, PS\* or ES\*; never a symmetric algorithm), and carries the configured issuer, one of the
configured audiences, and an expiry it has not passed by more than a minute of clock skew. A
bearer token over 16 KiB is refused before it is parsed, and one whose unverified `iss` is not the
configured issuer is refused before any key set lookup, so another issuer's tokens cause no fetch. The
username, UID, groups and partition come from configurable claims (`UsernameClaim`, `UIDClaim`,
`GroupsClaim`, `PartitionClaim`; the username and UID are `sub` by default). Each is a claim path:
dots separate the keys of nested JSON objects, and `\.` is a dot inside a key, so
`example\.io.tenant` reads `claims["example.io"]["tenant"]`. This changes `UsernameClaim` and
`GroupsClaim` for an embedder: they were top-level claim names, so a value holding a dot (such as
`example.io/user`) is now read as a path and must be written with each dot escaped
(`example\.io/user`). The grammar has that one escape: a backslash anywhere else is itself, so a
key that ends in a backslash and is followed by a nested key cannot be expressed. A token without a non-empty string at a configured username, UID or
partition path is refused; `PartitionClaim` fills `CallerIdentity.Partition` and never falls back
to an empty partition. `AllowedPartitions` admits only the listed partitions (a list with only
empty entries is an error, not "any"), and
`RequiredSubPrefix` only a `sub` that starts with it. An optional `Allow` func
can refuse a caller; without one, every token the issuer mints for that audience is admitted. The
key set is cached for an hour and fetched again when a token names a key it lacks, at most every
ten seconds whether or not the fetch succeeds, so a rotated key works without a restart. Keys the
library cannot use (an unsupported type, an encryption key) are skipped. A key set that cannot be
fetched is a 503, except that a key already cached keeps verifying for up to six hours past the
set's expiry.
`Config.KeySetURL` is optional when the issuer is an `https://` URL: the key set URL is then the
`jwks_uri` of the metadata at `<issuer>/.well-known/openid-configuration`, used only when the
metadata's `issuer` equals the configured one exactly and its `jwks_uri` is `https://`. Discovery
runs as part of every key set fetch, under the same ten-second limit, so a key set the issuer
moved is followed at the next fetch; metadata that cannot be fetched or used is a 503, except that
cached keys keep verifying as below. The issuer must then have no query or fragment. A redirect on either fetch is
followed only on `https://` to the host and port being fetched, a configured `KeySetURL` included.
`Config.CAFile` names a PEM bundle of the roots the key set's and the metadata's server is
verified against instead of the system roots; it is re-read when it changes, like the server
certificate below, and the fetch then speaks HTTP/1.1 only and uses no HTTP proxy.

`upstream.Transport` (`upstream.New`, distinct from `upstream.StaticTransport` below) sends `in-cluster`, and the proxy's `--host-cluster-id` `gke_` cluster, to the API server through the pod's service account. Any other
`gke_` cluster is reached with the proxy's Google credentials (Workload Identity Federation or
Application Default Credentials): it describes the cluster through the Container API and picks its
endpoint by rules 1, 2 and 4 of
[`private-endpoint-selection.md`](../docs/designs/private-endpoint-selection.md) (the private
endpoint of rule 3 is not chosen yet). A resolved endpoint is reused for ten minutes and a failed
resolution for ten seconds; an endpoint that cannot be dialled is described again after ten
seconds. An IP endpoint is verified against the cluster's own CA and never the system roots, an
`http://` endpoint and a redirect are refused, and a `kind-` cluster has no upstream. Neither
the binary nor an embedder needs Google credentials to start, so a proxy on any Kubernetes cluster
serves `in-cluster` with its ServiceAccount alone and only a `gke_` request fails, naming the
missing credentials. The binary passes `Config.TokenSource` a source of its own that looks them up
on first use; an embedder that passes no `Config.TokenSource` gets Application Default Credentials
looked up when a `gke_` cluster is first requested. A failed lookup is tried again, and
finds a credentials file written since, once the failure cached for that cluster expires after ten
seconds; a metadata server is probed once per
process.

The handler hands one `AuditEvent` per request to an `AuditSink` (`audit.go`) when the request
ends, so a watch or an `exec` session is recorded when it closes. The event holds the caller (with its partition and the authenticator that admitted it, when the
authenticator names them), the
cluster, the API server's classification of the request, the outcome (`forwarded`,
`unauthenticated`, `auth_unavailable` when the TokenReview or a key set fetch fails, `refused` with its reason,
`upstream_error`, `canceled` when the caller gives up, `throttled` when the in-flight cap
answered 429, or `aborted` when a response fails mid-copy otherwise or an upgrade's 101 cannot be
written), the status, the duration, and the response cache's result when the cache handled the request (a `hit` or `coalesced` request never reached the cluster). An upgraded session that breaks off after its 101 is recorded as
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

`pkg/k8sapiproxy/cache.go` provides an optional HTTP response cache (`CacheConfig`, `CacheStore`,
`CacheStatsProvider`, and the default `MemoryCacheStore`). By default, `cmd/k8s-api-proxy` enables a
64 MiB in-memory cache (`--cache-max-bytes` / `KUBE_PROXY_CACHE_MAX_BYTES`; `0` disables the cache)
with a 2 MiB per-entry limit (`--cache-max-entry-bytes` / `KUBE_PROXY_CACHE_MAX_ENTRY_BYTES`), a `0s`
default TTL for resource `get` and `list` responses (`DefaultResourceCacheTTL`, configured via
`--cache-resource-ttl` / `KUBE_PROXY_CACHE_RESOURCE_TTL`), and a `30s` default TTL for discovery
responses (`DefaultDiscoveryCacheTTL`, configured via `--cache-discovery-ttl` /
`KUBE_PROXY_CACHE_DISCOVERY_TTL`). With the `0s` default resource TTL, the proxy coalesces concurrent
in-flight resource `GET` and `LIST` requests without post-flight storage across per-pod
`MemoryCacheStore` replicas. Operators and embedders can set `--cache-resource-ttl` /
`KUBE_PROXY_CACHE_RESOURCE_TTL` or `CacheConfig.DefaultResourceTTL` when callers stick to one replica
(`sessionAffinity`); invalidation is per replica, so a shared `CacheStore` alone keeps stale entries.
Health and metrics discovery paths (`/healthz`, `/livez`, `/readyz`, `/metrics`), watches,
subresources, and dry-run writes are never cached. Concurrent cache misses coalesce into one
upstream request, `Secret` payloads are redacted before cache storage, non-dry-run mutations and
`401`/`403` responses invalidate the affected resource scope, and responses carry RFC 9211
`Cache-Status` and `Age` headers while honoring RFC 9111 `Cache-Control` directives. Cache lookups,
evictions, byte size, and entry count are exported on the metrics listener as
`kubeagents_kube_proxy_cache_requests_total`, `kubeagents_kube_proxy_cache_evictions_total`,
`kubeagents_kube_proxy_cache_bytes`, and `kubeagents_kube_proxy_cache_entries`.

The binary serves TLS with the certificate chain in `--tls-cert-file` (`KUBE_PROXY_TLS_CERT_FILE`)
and the private key in `--tls-key-file` (`KUBE_PROXY_TLS_KEY_FILE`) when both are set; either one
empty serves plain HTTP. One PEM file can hold both the `CERTIFICATE` blocks and the `PRIVATE KEY`:
give its path to both flags. Files without a matching certificate and key stop the proxy at
startup. The files are reloaded
without a restart (`pkg/certreload`): at most once a second, a TLS handshake checks their size,
modification time, device and inode (so a Secret or projected volume's `..data` symlink swap is
seen), and a change whose bytes differ is parsed for the next handshakes. A changed file that does
not load, such as one read mid-rotation, is logged, counted in
`kubeagents_kube_proxy_certificate_reload_failures_total` (which also counts a changed
`--upstream-ca-file` that does not load), and the last good certificate keeps
being served. `certreload.CAPool` does the same for a CA bundle an embedder dials with: its
`Transport` verifies each new connection against the current bundle, with hostname and IP SAN
checks unchanged, speaks HTTP/1.1 only (`ForceAttemptHTTP2` off), and uses no HTTP proxy.

Cached responses and coalesced fetches are shared across callers, which is safe while the
`UpstreamTransport` uses one credential for everyone, as `pkg/upstream` does. An embedder whose
transport picks a credential per caller or tenant sets `Config.CallerPartition`: its value for the
authenticated caller is part of the cache and coalescing keys, so requests share a response or a
fetch only within a partition, and the transport reads it with `PartitionFromContext` to choose
the credential. Coalescing runs even with a `0s` resource TTL, so a per-tenant transport without a
partition needs the cache off (`Config.Cache` nil). A write evicts the resource's entries
in every partition, since the data changed for every caller; a `401` or `403` evicts them in the
caller's partition only, since only that credential's access changed.

An embedder with a rule of its own about who may ask what sets `Config.Authorize`. The handler
calls it for every request that names a cluster and classifies as an API request, after the
exclusion and hold checks, with the request as received (headers not yet stripped, no body), the
`CallerIdentity`, the cluster ID and the `RequestInfo`. It runs before the policy, the
response cache, in-flight coalescing and the upstream, on a cache hit too, so a caller it refuses
is never served a response fetched for another caller and never joins another's fetch. Any error
is a `403 Forbidden`, audited as `refused` with `rejection_reason` `authorize.refused`; the caller
reads a fixed message and the error goes to the audit record only. Returning an `AuthorizeError`
chooses the reason and the message instead; the rejection metric counts such a reason as `other`.
There is no "could not decide" answer: a hook whose dependency is unavailable fails closed, and a
hook that panics refuses the request the same way, with the panic value in the audit record only.
The hook does not hide a cluster's scope state: route validation, exclusions, holds and
classification answer before it, so a caller it would refuse still receives those refusals, a
hold's reason text included, and an embedder that must keep that from unauthorized callers cannot
rely on the hook for it. It runs before the request takes an in-flight slot and gets no deadline
beyond the request's context, so a hook that does I/O must bound it itself.
`CallerIdentity.Extra` carries what else the authenticator verified about the caller, under the
authenticator's own keys: `pkg/tokenreview` copies the TokenReview's `status.user.extra`, where
Kubernetes names the pod of a pod-bound ServiceAccount token
(`authentication.kubernetes.io/pod-name`, `authentication.kubernetes.io/pod-uid`). The default
audit sink does not log it. `Config.ExtraCallerHeaders` names further request headers to strip
before forwarding, on plain, watch and upgrade requests alike, for a deployment that passes a
caller's identity or credential in headers of its own; the authenticator and `Config.Authorize`
still read them. An entry that is not an HTTP header name, or is `Host`, `Content-Length`,
`Transfer-Encoding`, `Connection` or `Upgrade`, is an error from `NewHandler`. The binary sets
neither option.

A program that needs only those two options on top of the binary does not rebuild the server from
the packages: its `main` calls `proxyserver.Run` (`pkg/proxyserver`), which is what
`cmd/k8s-api-proxy` calls.

```go
func main() {
	err := proxyserver.Run(context.Background(), os.Args[1:], proxyserver.Options{
		Version:            version,
		Authorize:          authorize,
		ExtraCallerHeaders: []string{"X-Example-Credential"},
	})
	if err != nil {
		log.Fatal(err)
	}
}
```

`Options.Authorize` and `Options.ExtraCallerHeaders` become the `Config` fields of the same names,
and `Options.Version` is what `--version` and the startup log report after the name
`k8s-api-proxy`. Everything else is the binary's: the same flags, environment variables and
defaults, the same log lines, and the same drain on `SIGINT` or `SIGTERM`. `Run` returns the error
the binary exits on, and `nil` after `--version` or a drained shutdown. It registers the flags on
`flag.CommandLine`, so a process calls it once. A flag that does not parse exits the process there;
if the program gave `flag.CommandLine` another error handling, `Run` returns the parse error and
starts nothing.

Each kube-agents GitHub Release attaches the binary for Linux and macOS on amd64 and arm64
(`k8s-api-proxy_<X.Y.Z>_<os>_<arch>.tar.gz`), for running the proxy outside a cluster; verify an
archive against the release's signed `checksums.txt`. `k8s-api-proxy --version` prints the release an
archive was built for; the `kube-proxy` image prints the commit it was built from, and a local build
prints `dev`. The image runs [`kube-proxy/cmd/kube-proxy`](../kube-proxy/README.md), the same server
with the kube-agents task credential gate. Archives from releases before the flag existed have no `--version`. Releases published before the pipeline built
it carry none.

For standalone sidecar or loopback bridge deployments outside a host cluster, `cmd/k8s-api-proxy`
provides opt-in flags. `--auth-mode=none` (`KUBE_PROXY_AUTH_MODE`; default `token-review`)
skips `TokenReview` setup, records `caller_username` as `unauthenticated`, logs a startup warning,
and rejects `--allowed-callers`, `CREDENTIAL_PROXY_SESSION_AUDIENCE`, and
`CREDENTIAL_PROXY_SESSION_CALLERS`. `--auth-mode=none` requires `--listen-addr`
(`KUBE_PROXY_LISTEN_ADDR`) to be set explicitly, for example `127.0.0.1:8443` on a host loopback
bridge. `--upstream-url=<url>` (`KUBE_PROXY_UPSTREAM_URL`) forwards
`in-cluster` requests to one static `http://` or `https://` endpoint through `upstream.NewStatic`
(`StaticConfig`, `StaticTransport`); with it and an `--auth-mode` without `token-review`, the binary
reads no host cluster ServiceAccount file and looks up no cloud credentials. It rejects `--host-cluster-id`,
`--container-api-url`, `--scope-file`, `--hold-file`, `--exclude-host-cluster-id`, and
`CREDENTIAL_PROXY_SCOPED_SA_POOL`, and sets `Config.DefaultClusterID` to `in-cluster` so bare
`/api/...` and `/clusters/in-cluster/...` paths both work (`SplitClusterPathWithDefault`).
`--deny-streaming` (`KUBE_PROXY_DENY_STREAMING`, `Policy.DenyStreaming`) refuses `watch` requests,
`log` `follow` streams (`ReasonStreamingBlocked`), and protocol upgrades with `403 Forbidden`, and
rejects `--allow-upgrade-subresources`.

Two more flags let the static upstream be an API server the proxy authenticates to itself, on any
conformant Kubernetes cluster. Both need `--upstream-url` with an `https://` URL and stop the proxy
at startup otherwise.

| Flag                    | Environment variable             | Meaning                                                                                    |
| ----------------------- | -------------------------------- | ------------------------------------------------------------------------------------------ |
| `--upstream-token-file` | `KUBE_PROXY_UPSTREAM_TOKEN_FILE` | File holding the bearer token sent upstream (`StaticConfig.BearerTokenFile`).              |
| `--upstream-ca-file`    | `KUBE_PROXY_UPSTREAM_CA_FILE`    | PEM bundle of the roots the upstream's server is verified against (`StaticConfig.CAFile`). |

The token replaces the caller's `Authorization` header, which is never forwarded. `https://` is
required with it even for a loopback address, so the token never crosses a cleartext connection.
Surrounding whitespace is trimmed, and the file is read again at most once a second, so a rotated
token (a projected ServiceAccount token, for one) is sent without a restart. A file that is
missing, unreadable, empty, over 64 KiB or not a single header-safe token stops the proxy at
startup, as does a path that is not a regular file. Later, while the file is in that state, a
request that reaches the upstream fails with `502`, with the cause in the audit record's `error`,
rather than being sent with an earlier token. Three things outlast a withdrawn or changed token:
the earlier one can still be sent for up to a second, until the file is read again; a request
already in flight (a watch, a log follow or an exec opened with it) keeps streaming; and the
response cache is not emptied, so an answer fetched with it, a discovery document by default, is
served until its TTL ends (`--cache-max-bytes=0` turns the cache off). Rotate the file
atomically (a rename, or a projected volume's symlink swap) so no read sees it half written. The
token is never logged. With `--auth-mode=none` every peer that can reach the listener acts with
the token's identity, and the proxy logs a warning at startup: bind `--listen-addr` to loopback or
otherwise restrict who can reach it.

The CA bundle replaces the system roots for the upstream and is reloaded like `--jwt-ca-file`
(`certreload.CAPool`): a rotated CA is trusted from the next connection, a changed bundle that does
not load is logged, counted in `kubeagents_kube_proxy_certificate_reload_failures_total`
and the previous one kept, and the hostname or IP SAN check is unchanged. A bundle file that is
removed or stays unreadable is reported once, and the roots loaded before it keep being trusted
until the proxy restarts: to withdraw trust from a CA, replace the bundle's contents instead of
deleting the file. With `--upstream-ca-file` the upstream connection ignores `HTTPS_PROXY` and the
other environment proxy settings (with a token file alone it still uses them) and still offers
HTTP/2. For an embedder, `StaticConfig.OnCAReloadError` receives the failed reloads, and neither
`StaticConfig.BearerTokenFile` nor `StaticConfig.CAFile` can be combined with a `TLSClientConfig`
that sets `InsecureSkipVerify`.

`--auth-mode` also takes `jwt`, alone or in a comma-separated list with `token-review`
(`token-review,jwt`); `none` cannot be combined. `jwt` verifies callers with `pkg/jwtauth`, configured by
the flags below, each defaulting to its environment variable. `--allowed-callers` is required only
when `token-review` is listed. The binary sets no `Allow` func: it admits every token the issuer signs for
one of the audiences, narrowed only by `--jwt-required-sub-prefix` and `--jwt-allowed-partitions`.
A flag of an authenticator that is not listed (`--jwt-*` without `jwt`; `--allowed-callers` or the
session variables without `token-review`) stops the proxy at startup instead of being ignored.
Values are trimmed, and a whitespace-only value is unset.

| Flag                        | Environment variable                 | Meaning                                                                                                            |
| --------------------------- | ------------------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| `--jwt-issuer`              | `KUBE_PROXY_JWT_ISSUER`              | The `iss` a token must carry. Required.                                                                            |
| `--jwt-jwks-url`            | `KUBE_PROXY_JWT_JWKS_URL`            | The key set's `https://` URL. Optional when the issuer is an `https://` URL (discovery).                           |
| `--jwt-audiences`           | `KUBE_PROXY_JWT_AUDIENCES`           | Comma-separated `aud` values accepted. At least one is required.                                                   |
| `--jwt-username-claim`      | `KUBE_PROXY_JWT_USERNAME_CLAIM`      | Claim path of the username; `sub` when empty.                                                                      |
| `--jwt-uid-claim`           | `KUBE_PROXY_JWT_UID_CLAIM`           | Claim path of the UID; `sub` when empty.                                                                           |
| `--jwt-groups-claim`        | `KUBE_PROXY_JWT_GROUPS_CLAIM`        | Claim path of the groups, a string or a list of strings.                                                           |
| `--jwt-partition-claim`     | `KUBE_PROXY_JWT_PARTITION_CLAIM`     | Claim path of the caller's partition; a token without it is refused.                                               |
| `--jwt-required-sub-prefix` | `KUBE_PROXY_JWT_REQUIRED_SUB_PREFIX` | Prefix the token's `sub` must start with.                                                                          |
| `--jwt-allowed-partitions`  | `KUBE_PROXY_JWT_ALLOWED_PARTITIONS`  | Comma-separated partitions admitted; needs `--jwt-partition-claim`. Only separators is an error.                   |
| `--jwt-ca-file`             | `KUBE_PROXY_JWT_CA_FILE`             | PEM bundle of the roots the issuer's server is verified against (`Config.CAFile`).                                 |
| `--partition-by-caller`     | `KUBE_PROXY_PARTITION_BY_CALLER`     | `none` (default), `partition` or `username`: what `Config.CallerPartition` separates by. Passed empty is an error. |

A list of several authenticators tries them in order and admits the caller the first one accepts.
When none does, the answer is a 503 if any of them could not decide (a failed TokenReview, a key
set or metadata that could not be fetched), and a 401 only when every one refused. Every listed
authenticator sees the request's original `Authorization` header until one accepts it, so with `token-review` first a
platform-issued token is sent to the host cluster's TokenReview API before `jwt` verifies it;
list `jwt` first to keep it local. The audit record's `caller_authenticator` is the mode that
admitted the caller (`token-review` or `jwt`, set by the binary for a single mode too), so a JWT
caller named like a ServiceAccount is told apart from it.

`--partition-by-caller=partition` keeps cached responses and coalesced fetches within one
`CallerIdentity.Partition`, and needs `jwt` with `--jwt-partition-claim`; in a list with
`token-review`, the callers TokenReview admits carry no partition and share the empty one.
`--partition-by-caller=username` keeps them within one username, whichever authenticator named
it, and is refused at startup with more than one mode in `--auth-mode`, where two authenticators
could name different callers alike. The binary's upstreams pick no credential per caller, so the
partition separates what callers can read from the cache, not what the upstream authorizes.

[`examples/`](examples/) holds two reference deployments for a sandbox runtime outside
kube-agents. In [`examples/egress-gateway/`](examples/egress-gateway/), one Deployment serves every
tenant: the runtime's egress gateway injects a signed token per sandbox, the proxy verifies it with
`--auth-mode=jwt` and separates tenants in its cache with `--partition-by-caller=partition`, and
the manifests cover the serving certificate, the Service name the certificate, the kubeconfig and
the gateway rule share, and the NetworkPolicy that admits the gateway's pods. In
[`examples/local-bridge/`](examples/local-bridge/), one process per sandbox listens on loopback
with `--auth-mode=none` and forwards to a local `--upstream-url`.
`pkg/proxyserver/examples_test.go` builds the binary's handler from the container `args` in the
manifests and from the fenced flag blocks in the two example READMEs, so an unknown flag or a
refused combination there fails the module's tests; flags named in prose are not checked.

It is a self-contained Go module. Run its checks from this directory:

```bash
gofmt -l .
go vet ./...
go test -race ./...
```

`k8sapiproxy-test.yml` runs those and gosec, built from `k8s-operator/go.mod`'s pin (this module
declares none); `make gosec` at the repository root runs the same scan over this module.

Every kube-agents release `X.Y.Z` tags this module `k8s-api-proxy/vX.Y.Z` at the release commit,
so a program can embed the handler with its own authenticator, upstream transport and audit sink:

```bash
go get github.com/gke-labs/kube-agents/k8s-api-proxy@latest  # or @vX.Y.Z
```

The module is v0: `Config`, `Policy` and its modes, `CallerAuthenticator`, `CallerIdentity`,
`AuthorizeAttributes`, `AuthorizeError`, `UpstreamTransport`, `AuditSink`, `CacheStore`,
`AuditEvent`, `proxyserver.Run` and `proxyserver.Options` may change in any release. The version moves with every
kube-agents release, whether or not this module changed; a release at `1.0.0` or later tags no
module until the module's own versioning is decided.
