# PlatformAgent Credential Isolation

## Summary

### Goal

The PlatformAgent sandbox container must not receive API keys, access tokens,
refresh tokens, or Kubernetes ServiceAccount tokens through its environment or
filesystem.

Credential values returned by an approved tool call are an accepted risk. For
example, an approved `gcloud auth print-access-token` response may expose a
token to the agent. Preventing that disclosure is not part of this design.

### Design

Each PlatformAgent runs as one long-lived Pod with these managed containers:

1. `platform-agent`: the untrusted agent sandbox.
2. `platform-agent-dashboard`: the optional local dashboard.
3. `fluent-bit`: log forwarding.
4. `envoy-credential-proxy`: Envoy, the credentialed command and chat runtime,
   and the `k8s-event-watcher`, which forwards cluster events using a
   non-secret internal key.

The proxy is a **native sidecar** -- an entry in `initContainers` carrying
`restartPolicy: Always` -- and not an ordinary container, so
`kubectl get pod -o jsonpath='{.spec.containers[*].name}'` will not list it. It
is shaped that way because it owns port 8643, which the Service targets, and it
shares a network namespace with the sandbox: as two ordinary containers they
started together and raced for the bind, and the sandbox could take the port
external callers reach. A native sidecar starts before any app container, which
narrows that race but does not close it: the kubelet waits for the sidecar to have
**started**, plus its `startupProbe` if it declares one, and this container declares
only a `readinessProbe`. So the ordering guaranteed is of process creation, not of
Envoy having bound 8643. Giving the container a `startupProbe` on that port would
close it; see `buildPodTemplateSpec` in the operator.

Requires Kubernetes 1.29+, where `SidecarContainers` is beta and enabled by
default. It is alpha and off in 1.28, and GA in 1.33.

On 1.28 the install fails rather than degrading. The API server strips
`restartPolicy` from the init container, which leaves it declaring a readiness
probe that a non-restartable init container may not have, so the pod template is
rejected and the apply fails. There is no configuration in which the proxy runs
as an ordinary init container with credential isolation quietly weakened. The
chart's `kubeVersion` refuses the install before that point.

The sandbox calls wrappers for `gcloud`, `kubectl`, `gh`, and `git`. Wrappers
send a structured argument vector to Envoy at `127.0.0.1:8765`. Envoy forwards
requests over a private Unix socket to the credential runtime. Slack and Google
Chat use the same local relay.

Only trusted sidecars receive projected Kubernetes ServiceAccount (KSA) tokens.
The credential sidecar receives secret environment variables, credential state,
and its identity token. It also receives a second, separately-audienced
Kubernetes-API token, CA, and namespace projection, which the event watcher it
hosts uses to reach the management cluster. Neither token is mounted in the
agent or dashboard containers. The credential sidecar also authenticates callers of the
PlatformAgent API before forwarding requests with a non-secret internal
sentinel. Pod-wide automatic KSA token mounting is disabled.

### Guarantee

The operator does not place managed credentials in the sandbox container's:

- environment;
- root filesystem;
- persistent agent volume; or
- mounted ServiceAccount token path.

`spec.deployment.env` is applied to the credential sidecar because it may
contain credentials. A short allowlist may also be copied to the sandbox — the
four OpenTelemetry settings and the three `ALERT_DAILY_LIMIT_*` alert ceilings —
but only as literal values; all `valueFrom` sources are rejected. A name earns a
place on that list only if an arbitrary value for it cannot redirect state,
grant access, or change what code runs; `safeSandboxEnvOverrides` in
`k8s-operator/internal/controller/platformagent_manifests.go` is the list.
Reserved proxy, runtime-loader, and shell-startup variables cannot override the
operator's managed values.

### Limitation

Containers in one Pod share a network namespace and one Pod identity. The
sandbox does not have the KSA token file, but it can technically reach the GKE
metadata server used by the credential sidecar. A Pod-level NetworkPolicy
cannot block metadata for one container while allowing it for another.

This design therefore meets the scoped filesystem-and-environment goal, but it
does not provide the stronger identity boundary of separate Pods. It assumes
the agent does not deliberately request credentials from the metadata server.

## Scope

### In scope

- PlatformAgent only.
- Credentials managed by the operator.
- CLI forwarding for `gcloud`, `kubectl`, `gh`, and `git`.
- Slack and Google Chat credentialed relays.
- PlatformAgent API bearer-key termination in the credential sidecar.
- GitHub installation tokens minted through Minty.
- Sidecar health, lifecycle, rollout, and migration from the former proxy Pod.

### Out of scope

- Preventing credentials from appearing in approved command or tool output.
- Preventing an approved credentialed command from deliberately copying a
  credential into the shared workspace. Such credential disclosure and tool
  side effects require a separate approval/policy design.
- Preventing deliberate access to the shared Pod identity or metadata server.
- Arbitrary user-supplied init containers, sidecars, volumes, and mounts. These
  are trusted configuration and may intentionally weaken isolation.
- OperatorAgent and DevTeamAgent.
- General data-exfiltration prevention.

## Architecture

```text
PlatformAgent Pod

  platform-agent
    credential-free env and mounts
    CLI wrappers / chat adapters
              |
              | HTTP on 127.0.0.1:8765
              v
  envoy-credential-proxy
    Envoy listener
              |
              | private Unix socket
              v
    credential runtime
    real CLIs, Slack/Chat clients, Minty client
    secret env, KSA token, private temporary state
```

Envoy is the only listener for credentialed tool and chat requests. The
credential runtime listens on a Unix socket mounted only in the sidecar, so the
sandbox cannot bypass Envoy by calling the runtime directly. A separate sidecar
listener authenticates the existing PlatformAgent API on port 8643 and forwards
to the sandbox API on loopback using a non-secret sentinel.

The containers share a Pod, but not the same lifecycle: the credential proxy is
a native sidecar, so the kubelet starts it before every app container and stops
it after them. If the sidecar is not ready, the Pod is not ready -- a restartable
init container counts toward Pod readiness. If either Envoy or the credential
runtime exits, the sidecar exits and Kubernetes restarts it; during startup that
means the app containers have not begun yet, so a bootstrap failure shows up as
`Init:CrashLoopBackOff` rather than a running Pod with one bad container.

## Credential Placement

| Data                             | Sandbox                | Credential sidecar        |
| -------------------------------- | ---------------------- | ------------------------- |
| `spec.deployment.env`            | No                     | Yes                       |
| Slack tokens                     | No                     | Yes, Secret-backed env    |
| PlatformAgent external API key   | No                     | Yes, Secret-backed env    |
| Session KV API key and HMAC salt | Yes, Secret-backed env | Yes, API key only         |
| Automatic KSA token mount        | Disabled               | Disabled                  |
| Explicit projected KSA token     | Not mounted            | Read-only, one-hour token |
| gcloud/kubectl configuration     | No                     | Private `emptyDir`        |
| GitHub installation token/cache  | No                     | Private `emptyDir`        |
| Agent workspace                  | Yes                    | Yes, for proxied commands |

### The loopback-only exception

`SESSION_KV_API_KEY` and `SESSION_KV_SALT` are the only Secret-backed values the
sandbox receives, and they are the exception that proves the rule rather than a
relaxation of it. Both are pod-scoped: they authenticate and pseudonymise
nothing outside this Pod, and neither grants access to any external system, so
an agent that reads them out of its own environment gains nothing it did not
already have.

They cannot go behind the proxy, because the sandbox is not the client — it is
the server. `session_kv_server.py` runs in the sandbox and binds
`127.0.0.1:8699`; its callers are the event watcher in the credential sidecar,
the Platform MCP server, the `incident_context` plugin, and the gateway's
kanban notifier, which keys a delivered triage report to the thread it went
into. The key exists so
that the server can reject a request that did not come from one of them, which
means the server has to hold it. The salt is read by the Chat Agent plugins,
which also run in the sandbox, before any identity is written to disk; hashing
it anywhere else would mean shipping the plaintext address out of the sandbox
first, which is exactly what it exists to prevent.

Deliberately _not_ `API_SERVER_KEY`: that value is the non-secret loopback
sentinel `cluster-internal-trusted`, so reusing it here would authenticate
nothing. Both keys are optional in the CRD, so a Secret without them yields
containers without the variables rather than a pod that will not start. What
that costs is worth stating precisely, because one of the three consequences is
not a degradation: the `k8s-event-watcher` in the credential sidecar
authenticates to the Session KV server with `SESSION_KV_API_KEY` and treats an
empty value as fatal, so it exits on every start and **no cluster events are
watched at all** — silently, since the container stays Ready and no probe covers
the watcher. The other two are degradations: the Session KV server refuses every
authenticated request with a 503 and says why, and identity hashing falls back
to a per-process random salt with one warning.

The projected token uses the audience `kubeagents-credential-proxy`, expires
after one hour, and is mounted only at
`/var/run/secrets/kubeagents/serviceaccount/token` in the credential sidecar.
The event watcher has a separate one-hour token with the Kubernetes API's
default audience, plus the cluster CA and Pod namespace, mounted at the
conventional in-cluster path in the same credential sidecar. Two differently
audienced tokens therefore sit side by side there: the proxy's own, which the
Kubernetes API will not accept, and the watcher's, which it will. Neither is
shared with the sandbox or dashboard.
Deleting a default token during startup is intentionally not used: projected
tokens rotate, and mount-time exclusion is reliable.

## Request Paths

### CLI commands

The sandbox image contains wrapper binaries instead of credential-aware CLI
binaries. A wrapper sends the executable name and argument array to the local
proxy. The credential runtime directly executes the corresponding real CLI and
returns output and exit status. It never evaluates an agent-supplied shell
command.

Only `gcloud`, `kubectl`, `gh`, and `git` are accepted. The proxy also rejects
known credential-disclosure, credential-replacement, and self-modification
operations, and the GitHub **write** path: merging a pull request
(`github.merge`), approving a review (`github.assent`), mutating through the
REST API (`github.api-mutation`), triggering workflows or releases
(`github.pipeline-trigger`), and repository administration — secrets,
variables, and repository deletion, archiving or editing
(`github.repo-administration`). Rulesets are not in that last list because
`gh ruleset` cannot change one: it has only `check`, `list` and `view`.
Reshaping a ruleset goes through `gh api`, so `github.api-mutation` is the
rule that refuses it and the id an operator will see.
Those five exist because the agent is the proposer: the review gate is only a
gate if the thing that opens a pull request cannot also merge it. Pipelines and redirections are interpreted by the sandbox shell
around an individual wrapper invocation, so they cannot execute inside the
credential sidecar. Requests that cannot be represented safely fail closed,
including:

- interactive TTY programs and password prompts;
- arbitrary binary or unbounded streaming input/output;
- file paths that refer to sandbox-only files;
- background processes or commands that outlive the request; and
- commands exceeding request, output, or timeout limits.

Standard input and full-duplex streaming require a future bounded protocol; the
wrapper does not silently consume an inherited protocol stream.

The current deny policy applies regular expressions to a normalised rendering of
the argument vector and permits flags before or between subcommands. Three
normalisations, and all of them matter for predicting what a rule will match:

- **Free-text flag values are dropped**, so prose the agent wrote is not searched
  as though it were a command path. Without this a pull request body containing
  the word "merge" tripped `github.merge`, which refused the product's own
  GitOps suggestions. The flag names remain, since a rule may key on one.
- **Attached shorthand values are split**, so `-XPUT` reads as `-X PUT`. gh,
  kubectl and gcloud are all Cobra/pflag, which accepts a shorthand's value with
  no separator, and a rule written for the separated spelling would otherwise
  miss it.
- **A shorthand buried in a cluster keeps its dash**, so `-iX PUT` reads as
  `-i -X PUT`. pflag also accepts a boolean shorthand and a value-taking one in
  the same token, and splitting only the first one off left the `-X` a rule
  matches on as a bare letter — enough for `gh api -iX PUT …/merge` and
  `gh auth status -at` to get through. Only the shorthands a shipped rule keys
  on are re-dashed, and the walk stops at the first non-letter, since
  everything from there is somebody's value rather than another flag.

A value that looks like a flag is never dropped: the free-text set is applied
without knowing the subcommand, and a name on it is not always value-taking.

This is an interim policy mechanism, not a general shell parser. If the policy grows
beyond these narrowly defined commands, it should use tool-specific argument
parsers over the structured argument vector.

### Agent-supplied kubeconfigs

A Cluster Agent profile pins itself to one cluster through `KUBECONFIG`, and
that file lives on the shared workspace volume, which the credential sidecar
also mounts. The sandbox can therefore choose the document a credentialed
`kubectl` opens.

A kubeconfig is executable configuration rather than data. Left unconstrained it
offers the sandbox several ways past the boundary this design establishes:

- `users[].user.exec.command` and `users[].user.auth-provider.config.cmd-path`
  run a program inside the credential sidecar, next to the credentials;
- `clusters[].cluster.server` and `proxy-url` choose where the access token
  minted by `gke-gcloud-auth-plugin` is sent, with `certificate-authority-data`
  supplied by the same author so TLS still validates;
- `users[].user.tokenFile` reads a sidecar file of the author's choosing and
  sends its contents as the bearer token; and
- `insecure-skip-tls-verify` removes the remaining obstacle to the above.

None of this is visible to the deny policy described above, which matches
against the argument vector — the argv is only ever `kubectl get pods`.
Validating the document instead would mean maintaining a denylist over a format
that keeps growing, and would not hold regardless: the sandbox can rewrite the
file between the check and the open.

The proxy therefore treats an agent-supplied kubeconfig as a **name, not as
content**. This is possible because the sandbox never legitimately authors one.
Every kubeconfig the system uses is produced by `gcloud container clusters
get-credentials`, which already runs in the sidecar. The proxy reads exactly one
string out of the caller's file — `current-context` — accepts it only if it is a
well-formed `gke_<project>_<location>_<cluster>` name, and regenerates the
kubeconfig itself into a directory backed by a sidecar-only `emptyDir`. That
regenerated file is what every proxied command runs against. No field the
sandbox wrote is ever interpreted, and there is no check-then-open window,
because the sandbox never had a handle on the document that is opened.

The same substitution is applied to a `--kubeconfig` flag in the argument
vector, which `kubectl` prefers over the environment; covering only the
environment would leave the flag as an equivalent path. `get-credentials` is
handled as the one command permitted to author a kubeconfig: it writes into the
sidecar's own directory, the result is filed under the context it selects, and a
copy is then written to the workspace path the caller asked for. The visible pin
that profile scaffolding records and the Cluster Agent preflight inspects
therefore still exists, without being what a later command opens.

Consequences:

- Naming a cluster is not additional authority. `get-credentials` is bound by
  the same Workload Identity the sidecar already holds, so the sandbox can only
  name clusters that identity could already reach.
- Only GKE contexts are supported, because the context name is what makes
  regeneration possible. A pin the proxy cannot regenerate from — no
  `current-context`, a non-GKE context name, or a merged `path1:path2` list — is
  rejected with `400` rather than honored.
- A cache miss costs one `get-credentials`, preceded by one
  `clusters describe` to decide whether the control plane should be reached
  over its DNS endpoint. The common paths warm the cache themselves, since
  profile scaffolding and context switching both begin with that command. That
  describe is memoised per cluster for a minute rather than for the life of the
  sidecar: the endpoint can be opened or closed on a running cluster, and the
  proxy is a daemon that would otherwise keep acting on the configuration it
  first saw.
- `current-context` is read with a real YAML parser, so a valid kubeconfig in
  any legal spelling is recognized, but deliberately with PyYAML's pure-Python
  `safe_load`. The C loader recurses in C and terminates the sidecar with
  `SIGSEGV` on a deeply nested document, where the Python loader raises a
  catchable error. The input is chosen by the sandbox, so this is a
  denial-of-service boundary rather than a performance choice.

### Chat

Slack and Google Chat adapters send credential-free request payloads to Envoy.
The credential runtime owns platform tokens and performs the external API call.
Allowlisted users, payload size limits, and file size limits are enforced by the
relay.

### PlatformAgent API

The Kubernetes Service sends API traffic to port 8643 on the credential
sidecar. The sidecar validates the configured external bearer key and replaces
it with the sandbox's fixed, non-secret sentinel before forwarding to port 8642.
Existing API clients retain bearer-key authentication without placing the real
key in the sandbox.

### GitHub and Minty

The credential sidecar obtains a Google OIDC identity token and calls Minty.
Minty validates CEL authorization rules for the authenticated agent identity and
requested repository, then brokers a repository-scoped GitHub installation
token with a maximum one-hour lifetime. The GitHub App private key remains in
Cloud KMS and signing uses `AsymmetricSign`.

The workspace is mounted at the same path in both containers so proxied Git
commands operate on the agent's repository. Git authentication, CLI config, and
token caches remain on a separate sidecar-only volume. The sandbox receives
only command output, never a mounted Git credential file.

## Kubernetes Details

- The Pod uses the configured PlatformAgent KSA for the credential sidecar's
  Workload Identity.
- `automountServiceAccountToken: false` applies to the Pod.
- Two separately projected ServiceAccount token volumes are mounted only by the
  credential sidecar — its own, and the event watcher's; neither is mounted by
  the agent or dashboard containers.
- Secret and credential-state volumes are mounted only by the credential
  sidecar.
- The sandbox and sidecar run non-root, drop all Linux capabilities, disallow
  privilege escalation, and use the runtime-default seccomp profile.
- Every container the operator builds — the credential-cleanup init container,
  sandbox, dashboard, sidecar, log shipper — has a read-only root filesystem;
  writable state uses bounded `emptyDir` volumes. The sandbox and dashboard
  share a 2Gi `/tmp` scratch volume: the entrypoint runs several hermes
  invocations with `HOME=/tmp` before the agent starts, and those two
  containers already share the data PVC, so the shared volume is not a new
  channel between them. Note the lifetime this changes: `/tmp` used to be each
  container's own writable layer, discarded whenever that container restarted.
  An `emptyDir` is scoped to the Pod, so its contents now survive a container
  crash or restart and are visible to both containers' next boot. The log
  shipper gets no `/tmp`: it buffers in memory
  and keeps its tail database on its own volume. Containers supplied through
  `spec.deployment.sidecars`/`initContainers` are appended to the Pod as
  written; the webhook does not require a read-only root of them, so a CR can
  still add a writable container to this Pod.
- A policy ConfigMap hash is placed on the Pod template to trigger rollout when
  command policy changes.
- The operator reports Ready only when the combined Pod is ready.

## Deployment and Migration

The operator always creates the sandbox and Envoy credential sidecar together
in the existing `<agent>-gateway` Deployment and retains the existing
`<agent>-data` PVC. Before the sandbox starts, a managed init container removes
legacy gcloud, GitHub, Git, Kubernetes, AWS, Azure, Docker, npm, and Python
credential files from that PVC. This preserves agent state without carrying
credentials forward from an older deployment. It deletes operator-owned
resources from the abandoned two-Pod design:

- `<agent>-credential-proxy` Deployment;
- `<agent>-sandbox` Deployment;
- `<agent>-credential-proxy` Service;
- `<agent>-sandbox` ServiceAccount; and
- `<agent>-sandbox-metadata-deny` NetworkPolicy.

Deletion refuses to remove resources not owned by the PlatformAgent.

The credential-sidecar image contains Envoy, the real credential-aware CLIs,
and the credential runtime. The sandbox image contains only the wrappers for
those CLI names.

## Tradeoffs

Benefits:

- one Deployment and one Pod lifecycle;
- no managed credential env or files in the sandbox container;
- no separate proxy Service or cross-Pod availability coordination;
- a single local surface for CLI and chat policy; and
- credentials remain usable without adding real cloud CLIs to the sandbox.

Costs:

- no hard network or identity boundary between the sandbox and sidecar;
- a custom command-forwarding protocol must be maintained;
- interactive, streaming, and file-based CLI behavior is limited;
- configuration files the sandbox supplies to a credentialed command must be
  regenerated rather than read, which bounds them to what the sidecar can
  reproduce — for kubeconfigs, GKE contexts only; and
- each new service needs an explicit proxy integration and policy.

If deliberate metadata or Pod-identity access becomes in scope, this design
must return to separate Pods or use a node/runtime mechanism that enforces
per-container network identity.

## Verification

CI and deployment tests should assert that:

1. the sandbox has no `spec.deployment.env`, secret volume, credential-state
   volume, or ServiceAccount token mount, and no Secret-backed env other than
   the two pod-scoped Session KV values named above — the assertion enumerates
   them, so a third one cannot be added without amending this list;
2. only the credential sidecar mounts proxy identity/state, and only the event
   watcher mounts its Kubernetes-API token projection;
3. only the credential sidecar receives Slack tokens and deployment env;
4. wrapper URLs resolve to `127.0.0.1:8765`;
5. Envoy can reach the Unix-socket backend and `/healthz` reflects both;
6. unsupported executables, raw shell requests, and blocked disclosure commands
   fail closed;
7. a command given an agent-authored kubeconfig runs against a regenerated one,
   with no `exec`, `server`, `proxy-url`, or `tokenFile` value from the supplied
   document reaching it, whether it arrives through `KUBECONFIG` or
   `--kubeconfig`;
8. the old proxy Deployment and Service are absent after reconciliation;
9. the external PlatformAgent API key is accepted by the sidecar and replaced
   before forwarding to the loopback-only sandbox API; and
10. Pod readiness fails when either Envoy or the credential runtime fails.
