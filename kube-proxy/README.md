# kube-proxy

The binary the `kube-proxy` image runs. It is the [`k8s-api-proxy`](../k8s-api-proxy/README.md)
server, with the same flags and environment variables, plus one thing that module cannot hold
because it is specific to kube-agents: the task credential gate, and with it the workers the proxy
admits and the limits it holds each of them to.

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
turns the gate off.

### Who is admitted as a worker

A worker's ServiceAccount token is issued for the profile audience
(`CREDENTIAL_PROXY_PROFILE_AUDIENCE`), the audience the broker gives the profile role by. With the
gate on, the binary accepts that audience from exactly the usernames that are keys of the identity
map (`proxyserver.Options.AudienceCallers`), so a caller admitted for the profile audience is always
a caller the gate checks. A ServiceAccount with a valid profile-audience token and no map entry is
refused at authentication with `401`, as the broker refuses it. There is no separate list of worker
callers to keep in step with the map.

The map's usernames are admitted for the profile audience only. The server refuses to start when
one of them is also in `CREDENTIAL_PROXY_ALLOWED_CALLERS` or `CREDENTIAL_PROXY_SESSION_CALLERS`, or
when the profile audience equals the proxy's own audience or the session audience. A worker's pod
is held to its address like every TokenReview caller, so the proxy's ServiceAccount needs `get` on
`pods` in each namespace a worker runs in.

One caveat remains. A caller admitted through another audience, such as a ServiceAccount listed in
the proxy's own allowed callers, is not in the map and so is not gated: it reaches every cluster the
proxy serves. A worker ServiceAccount must be named in the map and in no other caller list.

The profile audience needs `token-review` in `--auth-mode`. With the gate on, `--auth-mode=none` or
`jwt` alone stops the binary at startup. Each of these startup errors names
`CREDENTIAL_PROXY_PROFILE_AUDIENCE` and `CREDENTIAL_PROXY_PROFILE_IDENTITIES` and says what to
change.

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

## Worker limits

Every caller shares the proxy's in-flight cap (`--max-in-flight`, 24 requests per replica), and a
watch or a log follow is outside it. So that one worker can neither hold the shared slots nor open
streams without end, the binary gives each caller the identity map names two limits of its own
(`proxyserver.Options.CallerLimits`), per replica:

| Limit                                                | Default | Variable                              |
| ---------------------------------------------------- | ------- | ------------------------------------- |
| Requests in flight                                   | 8       | `KUBE_PROXY_PROFILE_MAX_IN_FLIGHT`    |
| Watches, log follows and other long-running requests | 8       | `KUBE_PROXY_PROFILE_MAX_LONG_RUNNING` |

A worker at a limit is answered `429 Too Many Requests` with `Retry-After: 1`, as the shared cap
answers, which `kubectl` waits out and retries. The audit record's `rejection_reason` is
`caller_in_flight_limit` or `caller_long_running_limit`. The two counts are separate, a request
the gate or the policy refuses counts for neither, and a stream counts until it closes. The
[`k8s-api-proxy` README](../k8s-api-proxy/README.md) owns the mechanics.

The limits belong to the worker's ServiceAccount username, so the pods of one profile and tier
share them. Callers outside the map, the shell and session pods, get no limit from this binary;
`--max-in-flight-per-caller` and `--max-long-running-per-caller` set one for them.

Eight requests in flight is a third of the shared 24, so two workers at their limit leave the
other callers a third of the slots. It is twice the four concurrent commands the broker allows the
profile role (`CREDENTIAL_PROXY_PROFILE_SLOTS`), because one `kubectl` command is several requests:
against an API server without aggregated discovery it fetches every group version at once, and a
request answered `429` ten times fails. The broker's variable is not reused, since it counts
commands across all profile callers and these count requests per ServiceAccount. Eight streams
cover one `kubectl logs -f` over a selector, which follows up to five pods, beside a few watches.

## Configuration

The gate reads four environment variables, the ones the broker reads, in the same formats:

| Variable                              | Value                                                                                                                                     |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `CREDENTIAL_PROXY_PROFILE_IDENTITIES` | JSON object: worker ServiceAccount username (`system:serviceaccount:<namespace>:<name>`) to `{"profile": "<profile>", "tier": "<tier>"}`. |
| `CREDENTIAL_PROXY_PROFILE_AUDIENCE`   | The audience a worker's ServiceAccount token is issued for. Required when the identity map names a worker.                                |
| `A2A_TASK_SIGNING_PUBLIC_KEYS`        | The task-signing public keys, comma separated: one, or two during a key rotation.                                                         |
| `A2A_TASK_CREDENTIAL_MAX_LIFETIME`    | The longest lifetime a credential may have, as a Go duration (`6h` when unset, at most `168h`).                                           |

The worker limits are two more, which only this binary reads:

| Variable                              | Value                                                                                                |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `KUBE_PROXY_PROFILE_MAX_IN_FLIGHT`    | Requests one worker ServiceAccount may have in flight: an integer, `8` when unset, `0` for no limit. |
| `KUBE_PROXY_PROFILE_MAX_LONG_RUNNING` | Long-running requests it may have open: an integer, `8` when unset, `0` for no limit.                |

The gate is off by default: with `CREDENTIAL_PROXY_PROFILE_IDENTITIES` unset, blank or `{}`, the
binary behaves as `k8s-api-proxy` does and reads none of the other five, so a profile audience set
on its own adds no audience and a worker limit set on its own limits nobody. A malformed identity
map, key list, lifetime or worker limit stops the binary at startup, and so does an identity map that names a worker with the profile audience unset or blank:
the proxy would admit none of the workers it is asked to gate. The broker starts in that state,
because there the audience confers the role and a broker without it has no profile callers. An
identity map with no signing key starts, logs a warning once, and refuses every worker.

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
