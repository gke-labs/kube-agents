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
4. `event-watcher`: cluster-event forwarding using a non-secret internal key.
5. `envoy-credential-proxy`: Envoy plus the credentialed command and chat
   runtime.

The sandbox calls wrappers for `gcloud`, `kubectl`, `gh`, and `git`. Wrappers
send a structured argument vector to Envoy at `127.0.0.1:8765`. Envoy forwards
requests over a private Unix socket to the credential runtime. Slack and Google
Chat use the same local relay.

Only trusted sidecars receive projected Kubernetes ServiceAccount (KSA) tokens.
The credential sidecar receives secret environment variables, credential state,
and its identity token. The event watcher receives a separate Kubernetes-API
token, CA, and namespace projection. Neither token is mounted in the agent or
dashboard containers. The credential sidecar also authenticates callers of the
PlatformAgent API before forwarding requests with a non-secret internal
sentinel. Pod-wide automatic KSA token mounting is disabled.

### Guarantee

The operator does not place managed credentials in the sandbox container's:

- environment;
- root filesystem;
- persistent agent volume; or
- mounted ServiceAccount token path.

`spec.deployment.env` is applied to the credential sidecar because it may
contain credentials. Four allowlisted OpenTelemetry settings may also be copied
to the sandbox, but only as literal values; all `valueFrom` sources are rejected.
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

The containers have the same lifecycle because they are part of the same
Deployment and Pod. If the sidecar is not ready, the Pod is not ready. If either
Envoy or the credential runtime exits, the sidecar exits and Kubernetes restarts
it.

## Credential Placement

| Data                            | Sandbox     | Credential sidecar        |
| ------------------------------- | ----------- | ------------------------- |
| `spec.deployment.env`           | No          | Yes                       |
| Slack tokens                    | No          | Yes, Secret-backed env    |
| PlatformAgent external API key  | No          | Yes, Secret-backed env    |
| Automatic KSA token mount       | Disabled    | Disabled                  |
| Explicit projected KSA token    | Not mounted | Read-only, one-hour token |
| gcloud/kubectl configuration    | No          | Private `emptyDir`        |
| GitHub installation token/cache | No          | Private `emptyDir`        |
| Agent workspace                 | Yes         | Yes, for proxied commands |

The projected token uses the audience `kubeagents-credential-proxy`, expires
after one hour, and is mounted only at
`/var/run/secrets/kubeagents/serviceaccount/token` in the credential sidecar.
The event watcher has a separate one-hour token with the Kubernetes API's
default audience, plus the cluster CA and Pod namespace, at the conventional
in-cluster path. It is not shared with the sandbox or dashboard.
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
operations. Pipelines and redirections are interpreted by the sandbox shell
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

The current deny policy applies regular expressions to a shell-escaped rendering
of the argument vector and permits flags before or between subcommands. This is
an interim policy mechanism, not a general shell parser. If the policy grows
beyond these narrowly defined commands, it should use tool-specific argument
parsers over the structured argument vector.

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
- Separate projected ServiceAccount token volumes are mounted only by the
  credential sidecar and event watcher; neither is mounted by the agent or
  dashboard containers.
- Secret and credential-state volumes are mounted only by the credential
  sidecar.
- The sandbox and sidecar run non-root, drop all Linux capabilities, disallow
  privilege escalation, and use the runtime-default seccomp profile.
- The credential sidecar root filesystem is read-only; writable state uses
  bounded `emptyDir` volumes.
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
- interactive, streaming, and file-based CLI behavior is limited; and
- each new service needs an explicit proxy integration and policy.

If deliberate metadata or Pod-identity access becomes in scope, this design
must return to separate Pods or use a node/runtime mechanism that enforces
per-container network identity.

## Verification

CI and deployment tests should assert that:

1. the sandbox has no Secret-backed env, `spec.deployment.env`, secret volume,
   credential-state volume, or ServiceAccount token mount;
2. only the credential sidecar mounts proxy identity/state, and only the event
   watcher mounts its Kubernetes-API token projection;
3. only the credential sidecar receives Slack tokens and deployment env;
4. wrapper URLs resolve to `127.0.0.1:8765`;
5. Envoy can reach the Unix-socket backend and `/healthz` reflects both;
6. unsupported executables, raw shell requests, and blocked disclosure commands
   fail closed;
7. the old proxy Deployment and Service are absent after reconciliation; and
8. the external PlatformAgent API key is accepted by the sidecar and replaced
   before forwarding to the loopback-only sandbox API; and
9. Pod readiness fails when either Envoy or the credential runtime fails.
