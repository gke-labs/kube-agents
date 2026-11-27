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

`pkg/mtlsauth` holds two authenticators for callers that arrive over mutual TLS. The binary runs
them as `--auth-mode=mtls` and `--auth-mode=requestheader`, with the flags further down; an
embedder builds them itself. Neither imports `client-go`.

```go
// The caller is the certificate: a workload's SPIFFE X.509-SVID, for example.
workloads, err := mtlsauth.NewClientCertAuthenticator(mtlsauth.ClientCertConfig{
	ClientCAFile:      "/etc/proxy/workload-ca.crt",
	IdentitySource:    mtlsauth.SourceURI, // or SourceDNS, SourceCN
	RequiredURIPrefix: "spiffe://example.org/ns/agents/",
})

// The caller is whoever an allowed front proxy names in request headers.
gateway, err := mtlsauth.NewFrontProxyAuthenticator(mtlsauth.FrontProxyConfig{
	ClientCAFile:     "/etc/proxy/gateway-ca.crt",
	NameSource:       mtlsauth.SourceURI,
	AllowedNames:     []string{"spiffe://example.org/ns/gateways/sa/egress"},
	UsernameHeaders:  []string{"X-Caller-User"},
	GroupHeaders:     []string{"X-Caller-Group"},
	PartitionHeaders: []string{"X-Caller-Tenant"},
})

handler, err := k8sapiproxy.NewHandler(k8sapiproxy.Config{
	Authenticator:      gateway,
	ExtraCallerHeaders: gateway.HeaderNames(), // the identity headers stop at the proxy
	// ...
})
```

Both read the client certificate from `Request.TLS`, and the listener must verify it when the
connection opens: `tls.VerifyClientCertIfGiven` (which keeps certificate-less requests such as
health probes working) or `tls.RequireAndVerifyClientCert`, with `ClientCAs` from
`certreload.NewCAPoolUnion` over every authenticator's bundle, set per handshake through
`GetConfigForClient` so a rotated bundle is advertised. Do not use `tls.RequestClientCert` or
`tls.RequireAnyClientCert`: a peer no CA vouches for could then make every request pay for a
chain search. In addition to the handshake, each authenticator verifies the certificate on every
request, against its own bundle only, for client authentication, at the current time: a
certificate that expired or whose CA left the bundle is refused on the next request of a
connection that is already open, HTTP/2 included, and a certificate issued under one
authenticator's CA is nothing to the other. A peer that presented more than eight certificates
is refused before any verification. A certificate with no
extended key usage extension is accepted, as `crypto/x509` accepts it; one that lists usages
without client authentication is refused. Revocation (CRL, OCSP) is not checked. The bundle is
reloaded like `Config.CAFile` above (`Reload` takes the `certreload.Options`); one that stops
loading is reported and the last good one keeps verifying, so neither authenticator ever answers 503.

The peer's name comes from the one certificate field the configuration names (`uri`, `dns` or
`cn`). No other field is tried when it is missing, and a certificate with two values there (two
URI SANs, two DNS SANs, two common names) is refused. Names are compared exactly, with no case
folding or pattern. A URI name is the certificate's own text with the scheme in lower case and
nothing else changed, and it must have one spelling only: a scheme, no userinfo, query or
fragment (not even an empty one), no percent-encoding, no `.` or `..` path segment, and the form
Go's `net/url` writes it back in (so no space, which it would escape). A certificate whose URI
is anything else is refused, and a configuration entry that is (`AllowedIdentities`,
`AllowedNames`, `RequiredURIPrefix`) is a constructor error. X.509
name constraints do not apply to the subject common name, so with `cn` a name-constrained CA in
the bundle can still issue any name.
`ClientCertAuthenticator` uses the name as `CallerIdentity.Username` and fills nothing else;
`AllowedIdentities` admits only the listed names and `RequiredURIPrefix` (with `uri` only) only
names that start with it; the prefix must be a URI with a scheme and a host, written as a name
is, and end in `/`, so that it cannot match a longer trust domain or path segment or every trust
domain. Without either, every client certificate
the CA issues is a caller.

`FrontProxyAuthenticator` requires the peer's name in `AllowedNames` and then believes the
request's headers. Each of `UsernameHeaders`, `UIDHeaders` and `PartitionHeaders` is an ordered
list: the first header present in the request is read and must hold exactly one non-empty value,
with no fallback to a later name. The username is always required, and the UID and the partition
are required once their headers are configured, so a caller never lands in the empty partition.
Every value of every `GroupHeaders` header is a group, not split at commas. `AllowedPartitions`
admits only the listed partitions, with the rules of `jwtauth`'s. A header name may appear in one
list only and must pass `k8sapiproxy.ValidateCallerHeaderNames`, the check
`Config.ExtraCallerHeaders` applies. Every peer in `AllowedNames` can name any caller, so give front proxies a CA of their
own and list only the proxies that authenticate their callers and overwrite these headers.

In a chain of authenticators the two refuse differently. No client certificate, a certificate
that does not verify against the authenticator's bundle, one without a single name in the
configured field, and a name the allowlist or prefix does not admit are plain errors: the next
authenticator is tried, which is how a workload's certificate passes the front-proxy
authenticator and reaches its own. An allowed front proxy whose headers are missing, empty,
repeated or name a partition that is not allowed is refused with an error wrapping
`k8sapiproxy.ErrCallerRefused`, which ends the chain: no later authenticator admits the request
on a credential the proxy only forwarded. That holds only for the authenticators after it, so
the front-proxy authenticator must come first: with a bearer-token authenticator ahead of it, a
request from an allowed proxy that forwards a token is admitted under the token's identity and
the empty partition, and the headers are never read. The binary tries it first whatever order
`--auth-mode` lists. Its chain is not exported, so an embedder that combines these authenticators
writes its own chain, which must stop at an error wrapping `ErrCallerRefused`.

Only the header failures of a front proxy that verifies now are final. A proxy whose certificate
expired, or whose CA was rotated out of the bundle, is a peer like any other to the
authenticator, and its requests fall through. With a bearer-token authenticator behind it, a
request such a proxy forwards with the caller's own `Authorization` header is then admitted
under the token's identity and that authenticator's partition. Do not chain a bearer-token
authenticator behind the front-proxy authenticator on a listener where the proxy forwards
callers' `Authorization`, or have the proxy strip it. The binary's chain closes this itself: a
connection that presented a client certificate is never admitted on a bearer token (the
certificate rule, further down). A certificate that verified but whose name
`AllowedIdentities`, `RequiredURIPrefix` or `AllowedNames` does not admit is the authenticator's
to judge, and is still a plain error by choice, so a chain can admit the caller another way, as
`jwtauth`'s filters do.

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
`upstream_error`, `canceled` when the caller gives up, `throttled` when the in-flight cap or a caller's own limit
answered 429 (only the caller's limit sets a reason), or `aborted` when a response fails mid-copy otherwise or an upgrade's 101 cannot be
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

The handler bounds what it forwards at once. `--max-in-flight` (`KUBE_PROXY_MAX_IN_FLIGHT`,
`Config.MaxInFlight`; 24 in the binary, `0` for no cap) is one cap every caller shares: a request
over it is answered `429 Too Many Requests` with `Retry-After: 1`, which client-go, and so
`kubectl`, waits out and retries up to ten times. A watch and the `log`, `exec`, `attach`,
`portforward` and `proxy` subresources stay open for as long as the caller follows them, and take
no slot.

That cap does not stop one caller from holding all of it, or from opening watches without end.
`Config.CallerLimits` does. It returns, for a caller, a `CallerLimits`: `MaxInFlight`, the most
requests of the kind the shared cap counts that the caller may have in flight, and
`MaxLongRunning`, the most watches and long-running subresource requests it may have open (a `log`
request counts as one whether or not it follows). The two are counted apart, so a caller that
follows a watch for an hour keeps its whole in-flight limit, and a zero or negative field is no
limit. The binary has one pair of limits, for every caller an embedder's func does not give limits
of its own:

| Flag                            | Environment variable                     | Meaning                                                                                               |
| ------------------------------- | ---------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `--max-in-flight-per-caller`    | `KUBE_PROXY_MAX_IN_FLIGHT_PER_CALLER`    | `CallerLimits.MaxInFlight` for a caller the embedder sets none for. `0`, the default, is no limit.    |
| `--max-long-running-per-caller` | `KUBE_PROXY_MAX_LONG_RUNNING_PER_CALLER` | `CallerLimits.MaxLongRunning` for a caller the embedder sets none for. `0`, the default, is no limit. |

A `CallerLimits` can also put the caller in a group: `Group` names it, and `GroupMaxInFlight` and
`GroupMaxLongRunning` are the same two limits for every caller that returns that name, counted
together. A request is admitted only when its caller and its group both have room, counts for both,
and gives both back when it ends. That bounds a set of callers in aggregate, which per-caller
limits cannot: enough callers, each under its own limit, still fill the shared cap. When callers of
one group return different limits, each request is held to the limits its own caller returned,
against the one count. No flag sets a group. A group whose `GroupMaxInFlight` is not below a
non-zero `--max-in-flight` can take every shared slot, so it reserves nothing for other callers;
the handler logs a warning once for each such group name, on `Config.Logger`. A key the func sets
and a username are separate namespaces, as group names are, so a caller whose username equals
another caller's key is still counted on its own.

A caller at a limit gets the shared cap's answer, the same `429` with `Retry-After`, audited as
`throttled` with one of four values of `rejection_reason` and counted under it in
`kubeagents_kube_proxy_rejected_requests_total`: `caller_in_flight_limit` or
`caller_long_running_limit` for the caller's own limit, which is checked first, and
`caller_group_in_flight_limit` or `caller_group_long_running_limit` for its group's. The shared
cap's `429` has no reason. The check runs after `Config.Authorize` and the policy, so a request
they refuse counts for nothing, and before the shared slot is taken, so a caller at a limit never
holds one. A request counts until the handler has finished it: when the response is copied, when a
watch or a log follow ends or its caller disconnects, and when an upgraded connection closes. A
request answered from the response cache, or joined to another caller's fetch, counts like any
other, as it does for the shared cap.

A caller's own limits are counted under `CallerLimits.Key`, which is the username unless the func
sets it; the partition is not part of it. With the default, pods that share a ServiceAccount share
its limits, and an embedder that wants a count per pod puts the pod in the key. Under
`--auth-mode=none` every caller is `unauthenticated`, so the flags' limits are then one more
shared cap. Each replica counts for itself. Set an in-flight limit well under `--max-in-flight`,
and know what a low one costs: against an API server without aggregated discovery a cold `kubectl`
asks for every group version at once, some thirty requests, so under a smaller limit it completes
only through client-go's retries of the `429`s, and a request answered `429` ten times fails.

The binary serves TLS with the certificate chain in `--tls-cert-file` (`KUBE_PROXY_TLS_CERT_FILE`)
and the private key in `--tls-key-file` (`KUBE_PROXY_TLS_KEY_FILE`) when both are set; either one
empty serves plain HTTP, which `mtls` or `requestheader` in `--auth-mode` refuses to start with. One PEM file can hold both the `CERTIFICATE` blocks and the `PRIVATE KEY`:
give its path to both flags. Files without a matching certificate and key stop the proxy at
startup. The files are reloaded
without a restart (`pkg/certreload`): at most once a second, a TLS handshake checks their size,
modification time, device and inode (so a Secret or projected volume's `..data` symlink swap is
seen), and a change whose bytes differ is parsed for the next handshakes. A changed file that does
not load, such as one read mid-rotation, is logged, counted in
`kubeagents_kube_proxy_certificate_reload_failures_total` (which also counts a changed
`--upstream-ca-file`, `--mtls-client-ca-file` or `--requestheader-client-ca-file` that does not
load), and the last good certificate keeps
being served. `certreload.CAPool` does the same for a CA bundle an embedder dials with: its
`Transport` verifies each new connection against the current bundle, with hostname and IP SAN
checks unchanged, speaks HTTP/1.1 only (`ForceAttemptHTTP2` off), and uses no HTTP proxy.
`certreload.NewCAPoolUnion` pools several bundles, such as the client CAs a listener advertises
for more than one authenticator; each file reloads on its own, and one that stops loading keeps
its last good certificates in the pool while the others follow their files.

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
`Transfer-Encoding`, `Connection` or `Upgrade`, is an error from `NewHandler`. The binary sets no
`Config.Authorize`, and puts in `Config.ExtraCallerHeaders` only the headers
`--auth-mode=requestheader` reads.

A program that needs only those two options on top of the binary, or one more TokenReview audience
with callers of its own, does not rebuild the server from the packages: its `main` calls `proxyserver.Run` (`pkg/proxyserver`), which is what
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
`k8s-api-proxy`.

`Options.CallerLimits` is `Config.CallerLimits` for an embedder that limits some callers and not
others. The two per-caller flags stay the limits of a caller it does not answer for: a
`MaxInFlight` or `MaxLongRunning` it returns as zero takes the flag's value, and a negative one is
no limit for that caller whatever the flag says. Its key and group fields are used as returned.

`Options.AudienceCallers` maps a TokenReview audience to the caller usernames admitted for it, and
adds those entries to the audiences the flags and the environment configure. A caller admitted this
way is held to its pod like any other TokenReview caller, so the proxy's ServiceAccount needs `get`
on `pods` in that caller's namespace. An audience the server already accepts is an error at startup
and not a union of the two caller lists, so an embedder cannot widen who `--audience` or
`CREDENTIAL_PROXY_SESSION_AUDIENCE` admits. These are startup errors too: an audience name that is
empty, padded with whitespace or holds a comma; an audience with no caller, or with a blank one; a
caller that another audience already admits; and a non-empty map when `--auth-mode` does not include
`token-review`. Each is an `*AudienceCallersError`, so an embedder that fills the map from settings
of its own can find it with `errors.As` and name those settings. An empty map changes nothing.

`proxyserver.NewHandler` returns the handler `Run` would serve for the same arguments and options,
with the flags registered on a `flag.FlagSet` the caller passes and nothing started. It is for a
program that serves the handler itself and for an embedder's tests of its `Options`. With `mtls`
or `requestheader` in `--auth-mode`, that program's own listener must verify client certificates
as `Run`'s does; `NewHandler` does not require `--tls-cert-file` and `--tls-key-file`, which only
`Run`'s listener reads.

Everything else is the binary's: the same flags, environment variables and
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

A list of `token-review` and `jwt` tries them in order and admits the caller the first one accepts;
with `mtls` or `requestheader` also listed, that holds for a request whose connection presented no
client certificate (the certificate rule, further down).
When none does, the answer is a 503 if any of them could not decide (a failed TokenReview, a key
set or metadata that could not be fetched), and a 401 only when every one refused. Each of these
bearer-token authenticators sees the request's original `Authorization` header until one accepts it, so with `token-review` first a
platform-issued token is sent to the host cluster's TokenReview API before `jwt` verifies it;
list `jwt` first to keep it local. The audit record's `caller_authenticator` is the mode that
admitted the caller (`token-review`, `jwt`, `mtls` or `requestheader`, set by the binary for a
single mode too), so a JWT caller named like a ServiceAccount is told apart from it.

`--partition-by-caller=partition` keeps cached responses and coalesced fetches within one
`CallerIdentity.Partition`, and needs a mode that names one: `jwt` with `--jwt-partition-claim`,
or `requestheader` with `--requestheader-partition-headers`. Each of those two modes that is
listed needs its flag, and `mtls` cannot be listed at all, since a certificate names no
partition. A partition is one partition whichever of the two modes named it. In a list with
`token-review`, the callers TokenReview admits carry no partition and share the empty one.
`--partition-by-caller=username` keeps them within one username, whichever authenticator named
it, and is refused at startup with more than one mode in `--auth-mode`, where two authenticators
could name different callers alike; `mtls` alone and `requestheader` alone take it. The binary's upstreams pick no credential per caller, so the
partition separates what callers can read from the cache, not what the upstream authorizes.

`--auth-mode` also takes `mtls` and `requestheader`, the two authenticators of `pkg/mtlsauth`
above, alone or in a list with each other, `token-review` and `jwt`. The rules for certificates,
names and headers are the library's, as described there.

| Flag                                 | Environment variable                          | Meaning                                                                                                     |
| ------------------------------------ | --------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `--mtls-client-ca-file`              | `KUBE_PROXY_MTLS_CLIENT_CA_FILE`              | PEM bundle of the CAs a caller's certificate must chain to. Required with `mtls`.                           |
| `--mtls-identity-source`             | `KUBE_PROXY_MTLS_IDENTITY_SOURCE`             | Certificate field the username is read from: `uri`, `dns` or `cn`. Required with `mtls`.                    |
| `--mtls-allowed-identities`          | `KUBE_PROXY_MTLS_ALLOWED_IDENTITIES`          | Comma-separated identities admitted, compared exactly; empty admits every certificate the CA issues.        |
| `--mtls-required-uri-prefix`         | `KUBE_PROXY_MTLS_REQUIRED_URI_PREFIX`         | Prefix the certificate's URI must start with, ending in `/`; needs `--mtls-identity-source=uri`.            |
| `--requestheader-client-ca-file`     | `KUBE_PROXY_REQUESTHEADER_CLIENT_CA_FILE`     | PEM bundle of the CAs a front proxy's certificate must chain to. Required with `requestheader`.             |
| `--requestheader-name-source`        | `KUBE_PROXY_REQUESTHEADER_NAME_SOURCE`        | Certificate field the front proxy's name is read from: `uri`, `dns` or `cn`. Required with `requestheader`. |
| `--requestheader-allowed-names`      | `KUBE_PROXY_REQUESTHEADER_ALLOWED_NAMES`      | Comma-separated names of the front proxies whose headers are believed. At least one is required.            |
| `--requestheader-username-headers`   | `KUBE_PROXY_REQUESTHEADER_USERNAME_HEADERS`   | Comma-separated headers holding the username; the first one present is read. At least one is required.      |
| `--requestheader-uid-headers`        | `KUBE_PROXY_REQUESTHEADER_UID_HEADERS`        | Comma-separated headers holding the UID; when set, a front proxy's request without one is refused.          |
| `--requestheader-group-headers`      | `KUBE_PROXY_REQUESTHEADER_GROUP_HEADERS`      | Comma-separated headers whose every value is a group.                                                       |
| `--requestheader-partition-headers`  | `KUBE_PROXY_REQUESTHEADER_PARTITION_HEADERS`  | Comma-separated headers holding the partition; when set, a front proxy's request without one is refused.    |
| `--requestheader-allowed-partitions` | `KUBE_PROXY_REQUESTHEADER_ALLOWED_PARTITIONS` | Comma-separated partitions admitted; needs `--requestheader-partition-headers`.                             |

`mtls` admits the holder of a client certificate that chains to `--mtls-client-ca-file`. The
certificate's name is the username; there is no UID, group or partition.

```bash
k8s-api-proxy \
  --auth-mode=mtls \
  --tls-cert-file=/etc/proxy/tls/tls.crt \
  --tls-key-file=/etc/proxy/tls/tls.key \
  --mtls-client-ca-file=/etc/proxy/workload-ca/ca.crt \
  --mtls-identity-source=uri \
  --mtls-required-uri-prefix=spiffe://example.org/ns/agents/ \
  --upstream-url=https://kubernetes.default.svc \
  --upstream-token-file=/var/run/secrets/proxy/upstream/token \
  --upstream-ca-file=/var/run/secrets/proxy/upstream/ca.crt
```

A sandbox whose certificate carries the URI `spiffe://example.org/ns/agents/sa/sandbox` is the
caller `spiffe://example.org/ns/agents/sa/sandbox`; a certificate from the same CA for another
namespace is answered `401`.

`requestheader` admits whoever an allowed front proxy names. The proxy is known by its client
certificate, under a CA of its own, and the caller by the headers the flags list.

```bash
k8s-api-proxy \
  --auth-mode=requestheader \
  --tls-cert-file=/etc/proxy/tls/tls.crt \
  --tls-key-file=/etc/proxy/tls/tls.key \
  --requestheader-client-ca-file=/etc/proxy/gateway-ca/ca.crt \
  --requestheader-name-source=uri \
  --requestheader-allowed-names=spiffe://example.org/ns/gateways/sa/egress \
  --requestheader-username-headers=X-Caller-User \
  --requestheader-group-headers=X-Caller-Group \
  --requestheader-partition-headers=X-Caller-Tenant \
  --partition-by-caller=partition \
  --upstream-url=https://kubernetes.default.svc \
  --upstream-token-file=/var/run/secrets/proxy/upstream/token \
  --upstream-ca-file=/var/run/secrets/proxy/upstream/ca.crt
```

The gateway connects with its certificate and sends `X-Caller-User: alice` and
`X-Caller-Tenant: team-a`. The proxy takes the request to be from `alice` in partition `team-a`,
and forwards it without those headers: every header the flags name goes into
`Config.ExtraCallerHeaders`, with any an embedder passes in `Options.ExtraCallerHeaders`. The
header names have no defaults.

Trusting a front proxy means believing whatever it sends. Every holder of a certificate that
chains to `--requestheader-client-ca-file` and carries a name in `--requestheader-allowed-names`
can act as any caller in any partition, and the audit record names the caller it sent and not the
proxy. So the CA signs front proxies and nothing else, the list holds only proxies that
authenticate their callers, and the headers are ones the proxy overwrites on every request. When
both modes are listed and the two CA flags name one file, the proxy stops at startup if
`--mtls-identity-source` and `--requestheader-name-source` differ, since a workload's certificate
could then carry an allowed front proxy's name in the other field, and otherwise starts with a
warning that every certificate under that CA with an allowed name is a front proxy. Two files
holding the same CA are not detected: keeping them apart is the operator's to do. A
peer that is not an allowed front proxy gains nothing by sending them: they are ignored, and
stripped before the request is forwarded.

With either mode, each TLS handshake asks for a client certificate and verifies one that is given
(`tls.VerifyClientCertIfGiven`) against the union of the listed modes' CA files, through
`GetConfigForClient` and `certreload.NewCAPoolUnion`. It requires none, so a health probe and a
bearer-token caller still connect, and `/_kube_proxy/healthz` answers without a certificate. A
peer that presents a certificate no listed CA signed fails the handshake. The CA files are
reloaded like the serving certificate: a rotated CA is accepted, and the one it replaced refused,
by the next handshake and the next request, and a certificate that expired or whose CA left its
bundle is answered `401` on the next request of a connection that is already open, HTTP/2
included. A changed file that does not load keeps the bundle it replaced, and is logged and
counted in the reload failure metric once for each pool that reads the file: the listener's and
each authenticator's that the file is given to. With neither mode the handshake asks no peer for a certificate.

The order of `--auth-mode` does not set the precedence of these two modes. The binary tries
`requestheader` first and `mtls` second, then `token-review` and `jwt` in the order listed. The
front proxy's final refusal binds only the authenticators after it, so its place is fixed and not
left to the order an operator wrote; refusing a list written in another order would add a startup
error and no safety.

The certificate rule: a request whose connection presented a client certificate is admitted
by `requestheader` or `mtls`, or not at all: when neither admits it the answer is `401`, and
`token-review` and `jwt` are not consulted. A request on a connection without a certificate goes
to `token-review` and `jwt` as it does without the certificate modes. The rule is there for a
front proxy whose certificate expired, or whose CA was rotated out, on a connection it still
holds open: it is no longer a front proxy to `requestheader`, and a request it forwards with the
caller's own `Authorization` header would otherwise be admitted under the token's identity, with
the proxy's identity headers ignored. The connection still carries the certificate it opened
with, and that decides.

| The connection presented                                                             | Answer                                                                     |
| ------------------------------------------------------------------------------------ | -------------------------------------------------------------------------- |
| An allowed front proxy's certificate, with identity headers                          | The caller the headers name (`requestheader`); a bearer token is ignored.  |
| An allowed front proxy's certificate, with a header missing, empty or repeated       | `401`; a bearer token is not consulted.                                    |
| A front proxy's certificate that expired, lost its CA or is not in the allowed names | `401`; a bearer token is not consulted.                                    |
| A certificate `mtls` admits                                                          | The certificate's name (`mtls`); a token and identity headers are ignored. |
| A certificate under the `mtls` CA that expired, or that the prefix or list refuses   | `401`; a bearer token is not consulted.                                    |
| No certificate, and a bearer token                                                   | `token-review` and `jwt` in the order listed.                              |
| No certificate and no token                                                          | `401`.                                                                     |

The rows hold for every list: `requestheader,jwt`, `mtls,token-review` and
`mtls,requestheader,jwt` differ only in which rows can occur. What the rule costs is that a
certificate and a bearer token are not alternatives on one connection. A peer that presents a
certificate under one of the client CAs cannot fall back to its token, where the library alone
would let a chain admit it another way. A workload meant to authenticate with a token connects
without a certificate, or holds one whose CA is in neither client CA file (Go's TLS client, for
one, then sends none, since the listener names the CAs it accepts). `token-review` is unchanged
for the pods it serves: they present no certificate, and its pod check still requires the
request's direct peer to be the pod, so a ServiceAccount token that a front proxy forwards was
refused before and is now not reviewed. Neither certificate mode answers `503`, so a request on a
connection with a certificate is admitted or answered `401`.

The proxy stops at startup, naming the flag, when:

- `mtls` or `requestheader` is listed and `--tls-cert-file` or `--tls-key-file` is empty;
- both are listed, their CA flags name one file, and their two source flags differ;
- `mtls` is listed without `--mtls-client-ca-file` or `--mtls-identity-source`, or
  `requestheader` without `--requestheader-client-ca-file`, `--requestheader-name-source`,
  `--requestheader-allowed-names` or `--requestheader-username-headers`;
- `--mtls-required-uri-prefix` is set and `--mtls-identity-source` is not `uri`;
- `mtls` is listed with another mode without `--mtls-identity-source=uri` and
  `--mtls-required-uri-prefix`, which keep a certificate from carrying a username another
  authenticator issues, such as `system:serviceaccount:<namespace>:<name>`;
- `--requestheader-allowed-partitions` is set without `--requestheader-partition-headers`;
- a `--mtls-*` or `--requestheader-*` flag is set without its mode, or a list-valued one holds
  only separators;
- a name, a prefix or a header is one the library refuses (a URI with a second spelling, a prefix
  that does not end in `/`, a header in two lists or one the proxy needs), or a CA file does not
  load;
- `--partition-by-caller` is `partition` or `username` in a combination the paragraph above
  refuses.

[`examples/`](examples/) holds two reference deployments for a sandbox runtime outside
kube-agents. In [`examples/egress-gateway/`](examples/egress-gateway/), one Deployment serves every
tenant: the runtime's egress gateway injects a signed token per sandbox, the proxy verifies it with
`--auth-mode=jwt` and separates tenants in its cache with `--partition-by-caller=partition`, and
the manifests cover the serving certificate, the Service name the certificate, the kubeconfig and
the gateway rule share, and the NetworkPolicy that admits the gateway's pods; its README has the
`requestheader` flags for a gateway that authenticates with its own certificate instead. In
[`examples/local-bridge/`](examples/local-bridge/), one process per sandbox listens on loopback
with `--auth-mode=none` and forwards to a local `--upstream-url`.
`pkg/proxyserver/examples_test.go` builds the binary's handler from the container `args` in the
manifests, from the fenced flag blocks in the two example READMEs and from the two command
blocks above, so an unknown flag or a refused combination there fails the module's tests; flags
named in prose are not checked.

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
`AuthorizeAttributes`, `AuthorizeError`, `CallerLimits`, `UpstreamTransport`, `AuditSink`, `CacheStore`,
`AuditEvent`, `ValidateCallerHeaderNames`, `proxyserver.Run`, `proxyserver.NewHandler`, `proxyserver.Options`, `proxyserver.AudienceCallersError` and `pkg/mtlsauth` may change in any release. The version moves with every
kube-agents release, whether or not this module changed; a release at `1.0.0` or later tags no
module until the module's own versioning is decided.
