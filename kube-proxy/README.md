# kube-proxy

The binary the `kube-proxy` image runs. It is the [`k8s-api-proxy`](../k8s-api-proxy/README.md)
server, with the same flags and environment variables, plus one thing that module cannot hold
because it is specific to kube-agents: the task credential gate.

`k8s-api-proxy/` stays generic and separately released. This module imports its server
(`pkg/proxyserver`) and the task credential verifier (`a2a/taskcred`) through `replace` directives,
and is built only into the image ([`deploy/kube-proxy/Dockerfile`](../deploy/kube-proxy/Dockerfile)).

With the gate off this is the same server with the same flags, and it differs from the generic
binary in three ways. It is built in this module, so a dependency both modules require resolves to
the higher of the two versions. The `a2a` credential libraries are linked in. And the credential
header is stripped from every caller's request before it is forwarded.

## The task credential gate

An `AgentProfile` worker runs one task at a time, and the dispatcher signs a short-lived credential
for each task that names the cluster the task may read. The gate (`taskgate/`) holds a worker's
requests to that cluster. It is the proxy's `Authorize` hook, so it runs after authentication and
before the verb policy, the response cache and the upstream.

For a caller the identity map names as a worker, a request is forwarded only when all of this holds:

- it carries exactly one `X-Kube-Agents-Task-Credential` header;
- the credential verifies for the `credential-proxy` audience against a configured signing key, and
  is inside the lifetime ceiling;
- its pod name and pod UID are the ones the caller's TokenReview names, and its profile and tier are
  the caller's entry in the identity map;
- it names a target cluster, and that target, written `gke_<project>_<location>_<cluster>`, equals
  the cluster the request addresses. A request path with no `/clusters/<id>` prefix is refused: with
  a default cluster it never matches a task's target, without one it is an invalid route.

The header is stripped before a request is forwarded, for every caller, whether the gate is on or
off.

### Who is gated

Only callers the identity map names. A caller the proxy admits whose username is not a key of the
map is not asked for a credential and is handled as `k8s-api-proxy` handles it. An empty map (`{}`)
turns the gate off, and with `--auth-mode=none` no caller has a username in the map, so the gate
gates nobody.

The broker decides differently. It gives a caller the profile role by the audience of its token,
and refuses a profile caller that has no map entry. Here the map alone decides, so a worker
ServiceAccount that is allowed to call the proxy and is missing from the map reaches every cluster
the proxy serves. Whoever renders the proxy's allowed callers must render the identity map from the
same source.

### When the credential is checked

Once, when a request starts. A watch, a log follow or an upgraded connection opened under one task
keeps streaming after its credential expires and after the pod moves on to another task. The
component that attaches the credential must close the worker's connections when a task ends.

### What answers before the gate

Route validation, exclusions and holds. A worker with no credential still receives those refusals,
a hold's reason text included: see the `Config.Authorize` paragraph of the
[`k8s-api-proxy` README](../k8s-api-proxy/README.md).

The credential broker applies the same rule to a worker's brokered `kubectl`
(`agents/platform/scripts/credential_proxy.py`), with one difference: the broker lets a worker with
no credential run commands that reach no cluster, and this gate refuses it, because every request
here reaches one.

## Configuration

The gate reads three environment variables, the ones the broker reads, in the same formats:

| Variable                              | Value                                                                                                                                     |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `CREDENTIAL_PROXY_PROFILE_IDENTITIES` | JSON object: worker ServiceAccount username (`system:serviceaccount:<namespace>:<name>`) to `{"profile": "<profile>", "tier": "<tier>"}`. |
| `A2A_TASK_SIGNING_PUBLIC_KEYS`        | The task-signing public keys, comma separated: one, or two during a key rotation.                                                         |
| `A2A_TASK_CREDENTIAL_MAX_LIFETIME`    | The longest lifetime a credential may have, as a Go duration (`6h` when unset, at most `168h`).                                           |

The gate is off by default: with `CREDENTIAL_PROXY_PROFILE_IDENTITIES` unset, blank or `{}`, the
binary behaves as `k8s-api-proxy` does and reads neither of the other two. A malformed identity
map, key list or lifetime stops the binary at startup. An identity map with no signing key starts,
logs a warning once, and refuses every worker.

## Refusals

A refused request gets `403 Forbidden` with a fixed message. The audit record carries one of two
reasons in `rejection_reason`, and the cause in `error`:

| Reason                   | Cause                                                                                                                       |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| `caller.task-credential` | No credential, more than one, one that does not verify, one for another pod, profile or tier, or one that names no cluster. |
| `caller.task-target`     | A valid credential for this caller that names a different cluster from the one the request addresses.                       |

The credential is never logged. An admitted request logs `task credential admitted` at `INFO` with
the task id and the credential's `jti`.

## Developing

```bash
cd kube-proxy
go build ./... && go vet ./... && go test ./...
```

`taskgate`'s tests read the fixtures `a2a/taskcred` shares with the broker's verifier
(`a2a/taskcred/testdata/credential_proxy_fixtures.json`).
