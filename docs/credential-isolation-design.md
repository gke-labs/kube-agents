# Credential-Isolated Agent Sandbox and Credential Proxy

## Status

Phase 1 implementation in progress. The operator now reconciles
`<agent>-sandbox` and `<agent>-credential-proxy`, the proxy executes raw command
text after blacklist evaluation, and credential-free CLI shims fail closed when
the proxy is unavailable. The existing PlatformAgent CR and provisioning flow
remain the deployment entry point for both workloads.

This is not yet the complete credential-isolation design. Sandbox
egress/metadata enforcement, admission checks, and migration of integrations
other than Google Chat remain future work. Google Chat Pub/Sub ingestion and
authenticated Chat API calls now run in the credential proxy; the sandbox uses
a credential-free relay protocol. The sandbox ServiceAccount has no Workload
Identity annotation or impersonation binding. API ingress uses a non-secret
cluster-trust sentinel rather than a mounted or injected Secret.

## Decision Summary

Each Hermes agent runs in a long-lived, untrusted sandbox pod. A dedicated,
long-lived credential proxy runs in a separate pod beside it as a one-to-one
logical pair. The two pods have the same desired lifecycle and are reconciled
by the operator as one unit.

The agent pod is not provisioned with platform-managed credentials in its
filesystem or runtime environment. It has no Kubernetes service account token,
cloud Workload Identity, Secret mount, secret-backed environment variable,
kubeconfig, cloud CLI configuration, Git credential store, SSH private key, or
integration token.

Credentialed CLIs and service integrations run only in the proxy pod. The agent
image contains credential-free wrapper scripts with familiar names such as
`gcloud`, `kubectl`, `git`, and `gh`. The agent forwards the exact raw CLI command
text to the proxy without rewriting it. After applying a declarative security
blacklist, the proxy executes the original text through a non-interactive shell
so normal quoting, pipelines, redirection, chaining, and other CLI composition
continue to work.

Raw credential disclosure and credential detection in otherwise legitimate
command output are out of scope for this phase. Commands whose purpose is to
print, export, copy, or configure credentials are rejected. These capabilities
may be designed later with explicit approval and disclosure semantics.

## Limitations

The credential boundary intentionally prevents the agent from administering or
extending the proxy environment. In particular:

- The agent cannot read proxy configuration, environment variables, credential
  stores, kubeconfigs, cloud CLI profiles, GitHub CLI configuration, Git
  credential helpers, SSH keys, service account tokens, or mounted Secrets.
- The agent cannot create, edit, select, or activate configuration in the proxy.
  Configuration written inside the Agent Sandbox affects only sandbox-local
  processes and cannot change the identity, target scope, or behavior enforced
  by the proxy.
- The agent cannot install, upgrade, or replace CLIs, plugins, extensions,
  credential helpers, or system packages in the proxy. If a required CLI or
  version is missing, it must be added to the pinned proxy image
  through the platform deployment process. Installing or copying a binary into
  the sandbox does not give it proxy credentials or create a new proxy
  capability.
- Operations that require credentials are available only when the proxy has a
  preconfigured integration and the request is not blocked by security policy.
  Upstream RBAC, IAM, and GitHub permissions remain authoritative for projects,
  clusters, namespaces, repositories, and accounts.
- The agent cannot modify the proxy filesystem, process environment, operating
  system, network policy, service account, Workload Identity binding, Secret
  mounts, deployment, or lifecycle. These changes require the operator or
  another trusted platform administration path.
- The proxy cannot directly read files created in the sandbox. Commands that
  depend on local configuration files, manifests, request bodies, credential
  files, or arbitrary file upload require the command text or stdin transport to
  carry that data, or a separately designed transfer interface.
- Direct CLI shims receive parsed process arguments, so they construct a
  shell-equivalent command with safe quoting; they cannot recover the original
  shell source byte-for-byte. Byte-for-byte command preservation applies when
  the terminal dispatcher submits its raw command through
  `credential-proxy-exec` or the `/v1/exec` endpoint.
- The proxy has a pod-local HOME and workspace shared by its concurrent
  requests. This supports CLI configuration and multiple repositories during a
  pod lifetime, but concurrent commands can contend on the same CLI config or
  working tree. State is lost when the proxy pod is replaced.
- The phase-one HTTP endpoint trusts callers on the cluster network. It does not
  authenticate the calling pod or originating user cryptographically. Protecting
  against compromised or malicious in-cluster workloads is outside this design;
  the endpoint remains ClusterIP-only and has structural request limits.
- Running two workloads increases namespace quota and scheduling requirements.
  Existing CPU and memory quotas sized for a single gateway can prevent the
  sandbox from being created even while the proxy is healthy. Provisioning must
  budget for the combined sandbox, proxy, sidecar, and integration footprint.
- Local file flags refer to the proxy workspace, not the sandbox filesystem.
  Stdin is supported by the raw proxy protocol, but direct executable shims do
  not consume inherited stdin because doing so can corrupt an MCP or other
  stdio-based parent protocol. Submit the entire pipeline as raw command text.
  Transparent arbitrary file upload, interactive TTYs, port forwarding, and
  long-lived streaming are not yet implemented.
- Only the proxy capabilities deployed by the platform are available. A skill,
  prompt, repository file, or agent-generated wrapper cannot persistently change
  the proxy image, identity, or platform-managed blacklist. Deliberate blacklist
  evasion through allowed shell behavior remains outside the threat model.

These constraints mean the agent can compose and process non-secret data in its
own sandbox, but it cannot self-service a missing credentialed integration. New
proxy capabilities require a reviewed platform change, a rebuilt or updated
proxy image when applicable, and deployment through the trusted control plane.

## Background

The Kubernetes Agentic Harness (`kube-agents`) runs Hermes agents with local
terminal access and operational capabilities. Agents must be treated as
untrusted because their behavior can be influenced by user input, retrieved
content, tool output, and prompt injection. System-prompt instructions are
behavioral guidance, not a credential boundary.

The current architecture exposes credentials to the agent runtime through
several paths:

- Kubernetes service account tokens are mounted by default;
- the agent service account may be mapped to GCP Workload Identity;
- API and Slack credentials are injected through environment variables;
- `gcloud`, `kubectl`, `git`, and `gh` run in the agent container;
- GitHub tokens are cached in the agent's home directory;
- local MCP subprocesses inherit the agent's environment and identity.

Short credential lifetimes reduce the period during which a stolen credential
is useful, but they do not prevent exposure. This design replaces ambient agent
credentials with a separate trusted execution boundary.

## Industry and OSS Prior Art

This section records the relevant behavior of mature and emerging systems as
reviewed on July 13, 2026. The systems differ in maturity and threat model, but
they converge on one architectural rule: sandboxing protects only secrets that
remain outside the sandbox. When credentials are needed, a trusted,
protocol-aware component should apply them at the last responsible point.

| System                                                                                                                                                                  | Credential-isolation approach                                                                                                                                                                                                                                                                                                                                | Implication for this design                                                                                                                                                                                                                              |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Hermes Agent security policy](https://github.com/NousResearch/hermes-agent/blob/main/SECURITY.md)                                                                      | Treats OS-level isolation as the only load-bearing boundary against an adversarial LLM. It recommends whole-process wrapping for untrusted input and points to NVIDIA OpenShell for provider-backed credential injection. It explicitly says environment filtering, approvals, output redaction, and tool allowlists are heuristics rather than containment. | The entire Hermes process tree, including code execution, file tools, MCP subprocesses, plugins, hooks, and skills, must run in the credential-free Agent Sandbox. A terminal backend alone is insufficient.                                             |
| [Hermes Agent user-guide security](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/security.md)                                          | Docker and remote backends omit host environment variables by default, but skill-declared variables, `terminal.env_passthrough`, `docker_forward_env`, credential-file declarations, and explicit MCP environment settings can put credentials back into the sandbox. Credential files are mounted read-only, which still makes them agent-readable.         | Disable every Hermes credential and environment passthrough path. Read-only Secret mounts do not satisfy this design. Existing Hermes approval and redaction features remain defense in depth only.                                                      |
| [NVIDIA OpenShell providers](https://docs.nvidia.com/openshell/latest/sandboxes/manage-providers)                                                                       | Keeps real credentials in a gateway provider store. The sandbox sees opaque placeholders; an L7 proxy resolves them only in matching HTTP headers, query parameters, or URL paths after TLS termination. Unresolved placeholders fail closed.                                                                                                                | This is the closest implementation precedent. Prefer proxy-side, endpoint-bound credential substitution for HTTP services. A non-secret placeholder is acceptable, but no real provider value may enter the agent pod.                                   |
| [Docker Sandboxes credentials](https://docs.docker.com/ai/sandboxes/security/credentials/) and [network policy](https://docs.docker.com/ai/sandboxes/governance/local/) | A host-side proxy matches declared services and injects credentials into outbound HTTP requests. The agent sees a sentinel value. Docker warns that registry credentials placed in `~/.docker/config.json` are readable by the agent and less isolated. Sandbox egress is proxy-controlled and can be default-deny.                                          | Confirms that credential storage and network mediation must be outside the agent. Domain, service, and header matching are stronger than forwarding arbitrary commands. Agent egress must not bypass the injecting proxy.                                |
| [Claude Code sandbox settings](https://code.claude.com/docs/en/settings) and [cloud security](https://code.claude.com/docs/en/security)                                 | Supports denying credential files and environment variables, or replacing selected variables with per-session sentinels that a TLS-terminating sandbox proxy substitutes only for configured hosts. Cloud sessions translate a scoped sandbox credential into the real GitHub credential at a secure proxy.                                                  | Add centrally enforced file and environment denials as defense in depth, while keeping the primary boundary structural. If placeholders are used, bind them to an explicit host allowlist and prevent project-local configuration from weakening policy. |
| [HashiCorp Boundary credential management](https://developer.hashicorp.com/boundary/docs/credentials)                                                                   | Distinguishes credential brokering, which returns credentials to the client, from credential injection, which applies them inside a protocol-aware session without exposing them. Boundary calls injection the most secure workflow and cannot inject application credentials into opaque generic TCP sessions.                                              | Do not confuse a secrets broker that returns a token with credential isolation. Injection requires understanding the application protocol; an opaque tunnel or generic remote shell cannot enforce the same property.                                    |
| [LF agentgateway backends](https://agentgateway.dev/docs/standalone/latest/configuration/backends/)                                                                     | Centralizes model and MCP routing and supports gateway-owned backend authentication.                                                                                                                                                                                                                                                                         | Model-provider and remote MCP credentials should be held by an internal gateway rather than configured in Hermes or a local MCP subprocess.                                                                                                              |

Hermes community discussions are directionally consistent with the official
documentation. Practitioners report moving away from `.env` files toward
credential brokers and placeholder substitution, and repeatedly note that a
vault which returns a secret to Hermes still exposes it to the agent. These
threads are useful operational feedback, not authoritative security guarantees:
[credential-management discussion](https://www.reddit.com/r/hermesagent/comments/1ui3137/credential_management_whats_the_state_of_the_art/)
and
[Hermes with OpenShell](https://www.reddit.com/r/hermesagent/comments/1s8y35q/put_hermes_agent_inside_nvidias_openshell_sandbox/).

### Prior-Art Conclusions

The proposed separate-pod credential proxy follows the same fundamental pattern
as OpenShell, Docker Sandboxes, Claude Code cloud execution, and Boundary:
credentials stay in a trusted component and are applied on behalf of an
untrusted client. It is therefore a sound direction for the stated scope.

The raw command proxy is less restrictive than the dominant protocol-aware
pattern. This design intentionally trusts the agent's raw command text and
executes it as a shell command for compatibility. The blacklist is a targeted
operational guardrail, not a containment boundary against a malicious command or
an exhaustive defense against equivalent spellings and indirect execution.

Where practical, the implementation should bypass CLI emulation and use a
typed, protocol-aware adapter:

- Kubernetes API requests with verb, resource, namespace, and object scope
  enforced server-side;
- HTTP or gRPC gateways that inject authentication only for allowlisted upstream
  identities and routes;
- remote MCP servers whose backend credentials exist only at an MCP gateway;
- model and telemetry gateways that own provider and exporter credentials.

CLI wrappers remain useful for broad compatibility while these adapters are
built. Upstream RBAC, IAM, GitHub permissions, workload isolation, and resource
limits constrain the actual authority available to raw CLI commands.

## Scope

### In Scope

We limit the information agents have access to -- in particular, security credentials and tokens.

This phase establishes and enforces one primary security property:

> The platform does not provision, inject, mount, cache, or grant the Agent
> Sandbox direct access to any credential or credential-producing identity
> material used to authenticate supported services.

This invariant covers platform-managed credentials used by Kubernetes, cloud
providers, GitHub, Slack, model providers, observability backends, agent API
ingress, and other supported integrations while those credentials are stored,
injected, or used by the platform. It does not claim that CLI, tool, model, API,
log, object, error, or other authorized output is credential-free. An authorized
operation may return credentials or other sensitive values to the agent. That is
permitted by this design and does not violate the credential-isolation
invariant.

For this document, the runtime environment includes:

- process environment variables and `/proc/*/environ`;
- container image layers and writable container filesystems;
- persistent volumes, `emptyDir`, projected volumes, and shared volumes;
- Kubernetes service account token projections;
- cloud metadata and workload-identity endpoints;
- CLI homes and caches such as `.config/gcloud`, `.kube`, `.aws`, `.azure`,
  `.git-credentials`, `.config/gh`, and SSH configuration;
- credentials passed through process arguments, stdin, Unix sockets, or
  inherited file descriptors;
- credentials embedded in wrapper scripts or proxy client configuration.

This phase includes:

- a hardened, credential-free Agent Sandbox;
- a separate, credentialed proxy pod dedicated to each agent;
- credential-free CLI wrapper scripts;
- an exact raw CLI command-text protocol;
- declarative security-blacklist evaluation followed by non-interactive shell
  execution of the original text;
- one-to-one lifecycle management of the agent and proxy deployments;
- fail-closed behavior when the proxy is absent or unhealthy;
- deployment, upgrade, teardown, admission, and conformance changes required to
  preserve the invariant.

### Not in Scope

The following are explicitly deferred:

- returning raw access, identity, refresh, installation, bot, or service account
  tokens through commands or interfaces whose explicit purpose is to disclose
  proxy-managed credentials;
- slash-approval semantics for raw credential disclosure;
- guaranteeing confidentiality after a raw credential is intentionally returned
  to the agent;
- detecting, classifying, redacting, blocking, or otherwise protecting
  credentials and sensitive values returned by otherwise authorized CLI, tool,
  model, API, log, Kubernetes object, or error output;
- preventing the agent from storing, processing, retransmitting, or exfiltrating
  credentials and sensitive values received through otherwise authorized
  output;
- comprehensive data-loss prevention for non-credential sensitive data;
- interactive CLI sessions, terminal emulation, port forwarding, or arbitrary
  remote process attachment;
- uploading arbitrary local files from the agent to the proxy;
- a new pod or Job for every CLI invocation;
- complete compatibility with every upstream CLI command, plugin, extension,
  flag, or local-filesystem workflow;
- a shared multi-tenant proxy serving unrelated agents.

Output classification may be added in a future design. Until then, commands
whose explicit purpose is to print or export the proxy's own credentials fail
closed, while credentials incidentally or intentionally contained in otherwise
authorized operation output are returned unchanged.

## Goals

- Remove all platform-managed credentials and credential-producing identities
  from the agent pod.
- Preserve common CLI-oriented agent skills through wrapper compatibility.
- Execute authenticated operations only in the dedicated proxy pod.
- Preserve exact raw command text and normal shell/CLI behavior.
- Apply the same security blacklist whether a request arrives through a wrapper
  or directly.
- Deploy, upgrade, monitor, and delete each agent and proxy as one logical unit.
- Keep the current long-lived agent lifecycle.
- Fail closed without restoring local credentials or local authenticated CLIs.

## Non-Goals

- Treating short-lived credentials as equivalent to credential isolation.
- Relying on model behavior or system prompts for enforcement.
- Making every operation performed through the proxy safe or non-destructive.
- Preventing a fully compromised agent from persisting non-credential data.
- Preventing all data exfiltration through allowed model or tool interfaces.
- Defending against malicious raw command text, blacklist evasion, or an agent
  that intentionally uses allowed shell functionality to access credentials or
  perform destructive operations.

## Threat Model

The Agent Sandbox remains untrusted as a place to store credentials, but this
design trusts the agent, raw command text, and cluster-internal callers. The
agent can:

- execute arbitrary programs available in the sandbox;
- read and write all files accessible to the agent user;
- read every agent process environment and argument list;
- call wrappers with arbitrary strings;
- bypass wrappers and call reachable proxy endpoints directly;
- persist malicious state in the long-lived agent workspace;
- attempt direct Kubernetes, cloud API, metadata, GitHub, Slack, and internet
  access;
- submit arbitrary shell syntax, pipelines, redirection, substitutions, CLI
  plugins, and commands permitted by the proxy image and runtime.

The proxy implementation, Kubernetes control plane, other in-cluster workloads,
sandbox runtime, deployment controller, administrative identities, and incoming
commands are trusted. A malicious command, compromised in-cluster workload,
prompt-injection-driven command, or deliberate blacklist bypass is outside the
threat model. The blacklist prevents routine accidental disclosure; it is not a
caller authorization or containment boundary.

## Security Invariants

1. The agent pod has no platform-managed Secret mount or secret-backed
   environment variable.
2. `automountServiceAccountToken` is `false` for the agent pod.
3. The agent Kubernetes service account has no cloud workload identity mapping.
4. The agent cannot reach cloud metadata or identity endpoints.
5. No real authenticated CLI, credential helper, or credential cache exists in
   the agent image or writable volumes.
6. All credentialed integrations run outside the agent pod.
7. Wrapper scripts contain no reusable proxy credential.
8. The proxy applies the same blacklist whether called through a wrapper or
   directly.
9. The proxy executes the exact accepted raw command text through a fixed,
   non-interactive shell without rewriting it.
10. A command matching a configured blacklist rule is rejected before shell
    execution and returns a stable security-policy error.
11. The proxy cannot mount or write credentials into an agent-accessible volume.
12. Proxy failure never causes fallback to locally authenticated execution.
13. The operator never reports the logical agent ready unless both the agent and
    its dedicated proxy are ready and protocol-compatible.

## Design

### Logical Agent Pair

For each agent custom resource, the operator creates a dedicated pair:

```text
Agent custom resource
  |
  +-- Agent Sandbox Deployment (<agent>-sandbox)
  |     - long-lived sandbox pod
  |     - Hermes and credential-free wrappers
  |     - no Secret, KSA token, or Workload Identity
  |
  +-- Credential Proxy Deployment (<agent>-credential-proxy)
  |     - long-lived trusted pod
  |     - real CLIs and service integrations
  |     - dedicated KSA, RBAC, IAM, and Secret mounts
  |
  +-- ClusterIP Service for the proxy
  +-- NetworkPolicies
  +-- ConfigMaps and policy
  +-- separate ServiceAccounts
```

The pair runs in the same namespace but in separate pods. It is one-to-one: a
proxy accepts requests only for its paired agent, and a wrapper resolves only
that proxy. A single generic proxy holding credentials for multiple agents is
not part of this design.

The pair is "side by side" logically, not as containers in one pod. Separate
pods provide separate Kubernetes service accounts, Workload Identity, network
policy, filesystem mounts, and failure domains.

### Agent Sandbox

The Agent Sandbox remains long-lived, matching the current agent lifecycle. It
uses a hardened runtime such as gVisor or Kata Containers and includes:

- Hermes;
- the local terminal backend;
- credential-free wrapper scripts;
- agent skills and configuration;
- explicitly approved persistent agent data;
- no functional locally authenticated service CLIs.

The pod boundary wraps the whole Hermes process tree. Configuring only a remote
or containerized terminal backend is insufficient because Hermes code execution,
MCP subprocesses, plugins, hooks, and skills may execute outside that terminal
backend. All of them must inherit the same credential-free filesystem,
environment, identity, and egress policy.

Hermes configuration in the Agent Sandbox must not use
`required_environment_variables`, `required_credential_files`,
`terminal.env_passthrough`, `terminal.docker_forward_env`,
`terminal.credential_files`, or explicit MCP secret environment settings for
platform-managed integrations. Installation and admission checks reject skills,
plugins, or configuration that reintroduce these paths. If a client requires a
credential-shaped environment variable, it may receive an explicitly
non-secret sentinel such as `proxy-managed`; the real value remains in the
proxy or gateway.

Required pod controls include:

```yaml
spec:
  runtimeClassName: gvisor
  automountServiceAccountToken: false
  serviceAccountName: <agent-name>-sandbox
  securityContext:
    runAsNonRoot: true
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: agent
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
```

The sandbox service account has no RBAC unless a non-credentialed requirement is
explicitly reviewed. It has no `iam.gke.io/gcp-service-account` annotation or
equivalent cloud identity.

Persistent agent storage is assumed attacker-controlled. It must never be
mounted into the proxy. Proxy Secrets, homes, temporary directories, sockets,
and logs must never be mounted into the agent.

The long-lived sandbox differs from a disposable-sandbox security ideal. A
successful compromise may persist in its writable workspace until the agent is
deleted or explicitly reset. This is an accepted tradeoff for this phase because
the scoped objective is credential isolation and lifecycle compatibility.

### Credential Proxy

The Credential Proxy is also long-lived and has the same desired lifecycle as
its paired agent. It contains the real supported CLIs and integration runtimes.
It owns:

- Kubernetes service account tokens and kubeconfigs;
- GCP, AWS, Azure, or other workload identities;
- GitHub App identities and installation-token minting;
- Slack and other chat credentials;
- agent API ingress credentials;
- model-provider credentials when a separate shared model gateway is not used;
- any future integration credential required by a supported service.

Credentials are mounted or injected only into proxy containers. The proxy uses
a dedicated service account and the narrowest RBAC and IAM that covers the
enabled integrations. Separating read-only and mutation authority into distinct
identities is recommended future hardening when it requires additional pods.

The proxy pod is also hardened: it runs as non-root, drops unnecessary Linux
capabilities, disables privilege escalation and host namespaces, uses a
read-only root filesystem except for bounded temporary storage, and uses a
restrictive seccomp or AppArmor profile. A hardened RuntimeClass is required
because the shell and real CLIs process agent-controlled text and upstream data.

The proxy process is long-lived, but each raw command runs in a new shell process
group with:

- a clean, explicit environment;
- a fixed or newly created temporary home directory;
- a fixed temporary working directory;
- bounded runtime and output size;
- no inherited agent environment;
- cleanup after completion, timeout, cancellation, or crash.

The proxy must not persist agent command strings, stdin, or output in its
credential directories. Long-lived CLI caches are disabled unless a specific
integration requires one. The blacklist covers known direct cache and credential
disclosure commands, but the trusted raw-command model does not claim that a
malicious shell command cannot inspect accessible proxy files.

The initial one-proxy-pod-per-agent design gives that proxy pod the aggregate
authority required by the agent's enabled integrations. Kubernetes provides one
pod service account, so it cannot provide distinct Workload Identities to
different containers in that pod. This is acceptable for the credential
isolation scope but increases proxy blast radius. If integration-specific or
read-versus-write identities are required, the logical proxy must expand into
multiple dedicated proxy pods rather than pretending containers in one pod have
independent pod identity.

### Credentialed Non-CLI Services

Moving only CLI credentials is insufficient. Every service that currently needs
a credential inside Hermes must move outside the Agent Sandbox or be fronted by
a trusted gateway. This includes:

- the `API_SERVER_KEY` used to authenticate agent API ingress;
- Slack bot and app tokens;
- Google Chat or Pub/Sub identities;
- GitHub token minting and authenticated Git operations;
- Kubernetes and GCP identities used by MCP tools;
- model-provider API keys;
- cloud observability export credentials.

For inbound agent API authentication, a trusted ingress or proxy validates the
credential and forwards an authenticated, credential-free request to the agent.
The agent service is reachable only from that trusted component.

For model access, the agent calls an internal model gateway with no provider key.
The gateway owns and injects the model-provider credential. For telemetry, the
agent sends to an internal collector that owns any export credential.

### CLI Wrapper Interface

The agent terminal or wrapper dispatcher receives the raw command text produced
by the agent and forwards that exact text to the paired proxy. It does not
re-quote, canonicalize, split, reorder, remove, or add tokens. Example:

```text
kubectl get pods -n team-a -o json | jq '.items[].metadata.name'
```

The request preserves the exact command:

```json
{
  "apiVersion": "cli.proxy.kubeagents.io/v1alpha1",
  "requestId": "018f4b75-8f7c-7f10-b9f7-a59f163a3c20",
  "command": "kubectl get pods -n team-a -o json | jq '.items[].metadata.name'",
  "context": {
    "kubernetes": {
      "contextName": "gke_example_us-central1_cluster-a",
      "projectId": "example",
      "location": "us-central1",
      "clusterName": "cluster-a",
      "defaultNamespace": "team-a"
    }
  }
}
```

The request does not include the sandbox environment, credential configuration,
or working directory. Transport serialization uses a real JSON encoder so the
command field round-trips byte-for-byte. The proxy records a hash and bounded
audit copy of the original command before policy evaluation.

Every request containing `kubectl` requires an explicit Kubernetes context in
the envelope. The proxy prepares an isolated per-request kubeconfig containing
that context and never relies on shared `current-context` state. The context is
separate from the command, so selecting it does not rewrite the raw text.

A normal executable wrapper cannot observe shell operators outside its own
argument array. Therefore, complete pipelines are forwarded at the agent
terminal/tool invocation boundary, before a sandbox-local shell evaluates them.
Purely local commands may continue to use the sandbox terminal; credentialed CLI
requests use the raw-command proxy dispatcher.

### Exact Shell Execution

After blacklist evaluation, the proxy passes the exact original command text to
a fixed non-interactive shell, equivalent to:

```text
execve("/bin/bash", ["bash", "--noprofile", "--norc", "-c", raw_command], clean_environment)
```

This intentionally supports normal shell quoting, pipelines, redirection,
chaining, substitutions, variables, and CLI behavior without implementing or
hard-coding CLI subcommands and flags. The proxy does not rewrite the command.

Execution still has structural resource limits that do not change command
semantics: maximum request bytes, runtime, process count, output bytes, temporary
storage, and concurrent requests. The shell runs with a fixed read-only image,
clean environment, bounded temporary home and working directory, no profile or
startup files, and the proxy pod's network and workload identity.

### Declarative Security Blacklist

Before execution, the proxy evaluates the raw text and a parsed copy of its shell
syntax against a versioned, declarative blacklist loaded from platform-managed
configuration. Parsing is used only for detection; the accepted original text is
still executed unchanged. Rules are data, not hard-coded branches in the proxy.

The initial blacklist covers direct disclosure or replacement of credentials
owned by the proxy and self-modification of its authenticated tooling:

| Rule category                | Initially blocked examples                                                                                                                                 |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| GCP access-token disclosure  | `gcloud auth print-access-token`, `gcloud auth print-identity-token`, `gcloud auth application-default print-access-token`, `gcloud config config-helper`  |
| GitHub token disclosure      | `gh auth token`, `gh auth status --show-token`                                                                                                             |
| Kubernetes token disclosure  | `kubectl create token`, `kubectl config view --raw`                                                                                                        |
| Git credential disclosure    | `git credential fill`                                                                                                                                      |
| Proxy credential replacement | `gcloud auth login`, `gcloud auth activate-service-account`, `gcloud auth application-default login`, `gh auth login`, `gh auth refresh`, `gh auth switch` |
| Tool self-modification       | `gcloud components install`, `gcloud components update`, `gh extension install`, `gh extension upgrade`                                                    |

The platform can add, remove, or version rules without rebuilding the proxy.
Every rule has an identifier, rationale, match tests, bypass regression cases,
and an optional remediation message.

When a rule matches, the proxy does not start the shell. It returns a stable,
agent-visible response such as:

```json
{
  "status": "blocked",
  "code": "SECURITY_POLICY_BLOCKED",
  "rule": "github.token-disclosure",
  "message": "Command blocked for security reasons."
}
```

The rejection and rule identifier are audited without credential values. The
agent may choose a non-blocked operation or report the policy restriction to the
user.

The blacklist is intentionally not claimed to stop a malicious agent. A trusted
command can use equivalent quoting, variables, scripts, direct HTTP calls, or a
new upstream CLI feature in ways a known-pattern blacklist does not recognize.
Preventing deliberate blacklist evasion is outside scope. The primary controls
for impact remain upstream RBAC/IAM/GitHub permissions, the minimal proxy image,
pod isolation, network policy where enforced, and audit.

### Generic CLI Coverage

Phase one attempts to support all normal behavior of the installed `kubectl`,
`gcloud`, `gh`, and `git` versions, except commands matched by the declarative
security blacklist or operations denied by upstream authorization. Examples
include:

```text
kubectl get pods -n team-a -o json
kubectl describe deployment web -n team-a
kubectl logs web-abc -n team-a --tail=100
gcloud container clusters describe cluster-a --location=us-central1
gcloud logging read <filter> --limit=50 --format=json
gh issue list --repo example/project --limit 20
gh pr view 123 --repo example/project --json title,state,url
kubectl get pods -o json | jq '.items[] | .metadata.name'
```

`kubectl logs` is a required phase-one capability. Request time and output limits
apply to it in the same way as every other command. Long-running `--follow`
requests require the transport to propagate cancellation and backpressure but
are not blocked by command policy merely because they stream.

Application logs may contain credentials, personal data, or other sensitive
content written by workloads. This is an accepted phase-one output risk, not a
reason to remove `kubectl logs`. The proxy does not claim that authorized
application output is secret-free.

### Trusted Cluster Transport

The proxy trusts requests arriving over the cluster-internal network. It does
not require mTLS, a caller token, pair authentication, or independently verified
end-user context. The one-to-one ClusterIP Service is an operational routing
mechanism, not an authorization boundary. Service-specific authority is fixed by
the proxy's upstream RBAC, IAM, and GitHub permissions.

This choice keeps reusable proxy credentials out of the sandbox and matches the
stated threat model: credential placement is protected, while compromised or
malicious in-cluster callers are not. The proxy still applies request limits,
explicit execution context, the security blacklist, and audit logging.

### Network Isolation

The intended hardened deployment uses default-deny ingress and egress. Agent
egress permits only the credential proxy, internal model gateway, internal
telemetry collector, controlled DNS, and other explicitly credential-free
internal dependencies. The current target cluster does not enforce Kubernetes
NetworkPolicy, so the metadata exclusion declared by the phase-one manifests is
not enforced there and remains an explicit deployment gap.

The agent cannot directly reach:

- the Kubernetes API;
- cloud metadata or workload-identity endpoints;
- cloud provider APIs;
- GitHub, Slack, or other credentialed service APIs;
- proxy credential stores or administrative endpoints;
- arbitrary internet destinations.

The proxy has separate egress rules for only its supported upstream services.
Standard Kubernetes NetworkPolicy selects pods and namespaces rather than
service accounts, so stronger workload identity is enforced by the CNI, service
mesh, or proxy where required.

### Long-Lived Lifecycle

The Agent Sandbox Deployment and Credential Proxy Deployment are long-lived.
They have matching desired presence, not necessarily identical process uptime:
Kubernetes may restart either pod independently.

The operator enforces the logical pair as follows:

1. Create dedicated agent and proxy service accounts.
2. Create proxy RBAC, IAM bindings, and Secret references.
3. Create the proxy ClusterIP Service and the sandbox's metadata-deny egress
   policy. Cluster-internal callers are trusted by this threat model; caller
   authentication is not part of the protocol.
4. Create and wait for the proxy Deployment to become ready.
5. Create or update the Agent Sandbox Deployment.
6. Start the agent only after an init check confirms a ready and
   protocol-compatible proxy.
7. Continuously mark the agent container unready when proxy health or protocol
   compatibility fails.
8. Report the custom resource `Ready` only when both Deployments are ready.
9. Reconcile drift in either Deployment, Service, policy, or identity.
10. Delete both Deployments and their scoped resources through owner references
    and finalizer-controlled cleanup.

Absolute simultaneous availability cannot be guaranteed across separate pods.
The enforceable requirement is that the pair is always declared and reconciled
together, the agent is not considered operational without its proxy, and no
credentialed local fallback exists.

If the proxy fails after agent startup:

- wrappers fail closed;
- the agent readiness check fails;
- the logical agent status becomes degraded;
- Hermes may remain running for conversation continuity, but credentialed tools
  are unavailable;
- the operator attempts to restore the proxy;
- no credential moves into the agent pod.

### Version Compatibility and Upgrades

Wrappers and proxies expose a versioned protocol. The Agent Sandbox Deployment
declares the required proxy protocol and service policy version. The proxy
readiness endpoint reports supported versions without returning sensitive
state.

The operator upgrades the proxy first, waits for compatibility and readiness,
then upgrades the agent. During rollback, it restores a compatible proxy or
disables the wrappers; it never restores local credentials or authenticated
local CLIs.

Rolling upgrades must preserve one-to-one routing. A proxy revision must not
temporarily accept traffic from an unrelated agent. If this cannot be guaranteed
with rolling updates, use `Recreate` or revision-specific Services.

## Technical Details

### Proposed Resource Naming

For an agent named `platform-agent`:

```text
Deployment/platform-agent-sandbox
Deployment/platform-agent-credential-proxy
Service/platform-agent-credential-proxy
ServiceAccount/platform-agent-sandbox
ServiceAccount/platform-agent-credential-proxy
ConfigMap/platform-agent-cli-policy
NetworkPolicy/platform-agent-sandbox-metadata-deny
```

The `-sandbox` and `-credential-proxy` suffixes are the canonical workload and
resource names throughout the operator, manifests, policies, Services,
dashboards, alerts, and documentation. The sandbox contains Hermes and
credential-free wrappers. The credential proxy contains the real authenticated
`gcloud`, `kubectl`, `gh`, and supported `git` binaries, their platform-managed
configuration, and the enforcement and audit process that invokes them.

Every resource has labels identifying the owning agent UID and pair ID. The
proxy Service selector includes the immutable pair ID, not only a user-controlled
agent name.

### Operator and CRD Changes

The custom resource should expose a constrained proxy configuration rather than
arbitrary proxy pod fields. Example:

```yaml
spec:
  credentialProxy:
    enabled: true
    image: <pinned-proxy-image>
    policyRef:
      name: platform-agent-cli-policy
    services:
      - kubectl
      - gcloud
      - git
      - gh
```

The referenced policy is platform-managed data. A representative blacklist
shape is:

```yaml
apiVersion: cli.proxy.kubeagents.io/v1alpha1
kind: CLIProxyPolicy
metadata:
  name: platform-agent-cli-policy
spec:
  defaultAction: execute
  blockedMessage: Command blocked for security reasons.
  rules:
    - id: gcp.access-token-disclosure
      match:
        executable: gcloud
        argvSequence: [auth, print-access-token]
    - id: github.token-disclosure
      match:
        executable: gh
        argvSequence: [auth, token]
    - id: kubernetes.token-disclosure
      match:
        executable: kubectl
        argvSequence: [create, token]
```

The rule engine walks every simple command in the parsed shell syntax, including
pipelines, chained commands, and substitutions, solely to evaluate blacklist
rules. If no rule matches, it executes the untouched original command text.
Policy matching and execution behavior are covered by tests for quoting,
pipelines, global flags, and common alternate spellings. These tests improve
normal enforcement but do not change the explicit trust assumption for raw
commands.

The operator owns:

- proxy Deployment and Service generation;
- dedicated agent and proxy service accounts;
- proxy-only Secret mounts and secret-backed environment variables;
- proxy IAM and RBAC attachment;
- agent/proxy NetworkPolicies;
- wrapper endpoint and non-secret pair configuration;
- readiness, version compatibility, status, drift reconciliation, and cleanup.

Arbitrary agent `env`, init containers, sidecars, and volumes must not be able to
restore a credential path. These extension points are removed, constrained, or
validated by admission policy.

### Admission Enforcement

Validating admission policy or webhooks reject Agent Sandbox pods that:

- omit `automountServiceAccountToken: false`;
- reference a Secret through `env`, `envFrom`, volume, CSI, projected volume, or
  init/sidecar container;
- use a cloud Workload Identity annotation;
- mount a proxy volume, socket, home, or credential path;
- lack the required RuntimeClass or security context;
- use an unapproved image, init container, sidecar, ephemeral container, volume,
  or host namespace;
- expose direct public ingress or unapproved egress;
- include a real authenticated CLI or credential helper contrary to image
  policy.

Proxy pods are separately validated to ensure their credential mounts never
appear in the agent pod template and that proxy identities are scoped to the
owning pair.

### Deployment Script Changes

Provisioning scripts must treat proxy deployment as mandatory, not optional:

1. Create or update integration Secrets for the proxy namespace.
2. Create the proxy Kubernetes service account and cloud IAM binding.
3. Ensure the agent Kubernetes service account has no IAM binding and no
   Workload Identity annotation.
4. Render the custom resource with `credentialProxy.enabled: true`.
5. Apply admission and network policies before starting the agent.
6. Deploy the proxy and wait for rollout and protocol readiness.
7. Deploy the agent and wait for both members of the pair.
8. Run a post-deployment credential-isolation check against the agent pod.
9. Fail provisioning if either member is absent, incompatible, or unready.

Teardown removes the agent and proxy together, revokes proxy IAM bindings, and
deletes proxy Secrets according to their ownership policy. It must not leave a
credentialed orphan proxy reachable from another workload.

Developer scripts and examples must follow the same paired deployment path.
There is no manifest fallback that deploys a credentialed agent without its
proxy.

### Post-Deployment Verification

Automated verification checks the running agent container for:

- Secret-derived environment variables;
- service account token mount paths;
- cloud and Kubernetes credential files;
- Git and GitHub credential stores;
- SSH private keys;
- cloud metadata reachability;
- direct Kubernetes, cloud API, GitHub, and Slack reachability;
- unexpected real CLI binaries or credential helpers;
- mounts shared with the proxy;
- wrapper/proxy protocol compatibility.

The check reports credential names and paths, never raw values.

### Failure Handling

- Invalid transport envelopes and requests exceeding structural resource limits
  return a stable rejected status.
- Blacklist matches return `SECURITY_POLICY_BLOCKED` and the matching rule ID
  without starting the shell.
- Shell syntax errors, unknown executables, CLI errors, and upstream
  authorization failures are returned as normal command failures.
- Proxy unavailability produces a wrapper error and never invokes a local CLI.
- Timeout or cancellation kills the entire shell process group and cleans its
  temporary home and working directory.
- Proxy restart invalidates in-flight requests.
- Agent restart reconnects only to its paired proxy.
- Pair identity or protocol mismatch marks both the wrapper capability and
  logical agent unavailable.
- Cleanup failure marks the proxy degraded and blocks further commands until the
  affected state is quarantined or reset.
- Audit and application logs do not include proxy environment variables or
  credential files.

## Tradeoffs

### Benefits

- Credentials are removed from the untrusted agent filesystem and environment.
- Existing CLI-oriented skills require fewer immediate changes.
- Separate pods provide independent service accounts, Workload Identity, Secret
  mounts, network policy, and upgrades.
- A dedicated proxy limits cross-agent credential and state sharing.
- Long-lived pods preserve current agent session and operational behavior.
- The security blacklist is applied consistently even when wrappers are
  bypassed.

### Costs

- The proxy becomes required infrastructure for every agent.
- One proxy per agent increases pod count, memory, upgrades, and operational
  overhead.
- Executing trusted raw text through a shell gives the agent the full authority
  of the proxy identity and installed tools.
- A blacklist cannot comprehensively recognize equivalent, obfuscated, indirect,
  or newly introduced credential-disclosure behavior.
- Shell execution increases the proxy's attack surface and makes command-level
  authorization less precise than a typed API.
- Long-lived agent sandboxes allow malicious non-credential state to persist.
- Long-lived proxies require careful cleanup to prevent state accumulating
  between commands.
- Separate pods cannot provide perfectly simultaneous startup or availability.
- Credential-bearing command output remains an acknowledged future risk.

### Why Not a Same-Pod Sidecar

A same-pod sidecar could hide a Secret volume from the agent container, but the
containers would share a network namespace and pod identity. Kubernetes
NetworkPolicy could not allow upstream egress only for the proxy container, and
GKE Workload Identity would not naturally be isolated to one container. Separate
pods make the required identity and network boundary explicit.

### Why Accept Raw Shell Commands

Exact command strings provide the highest compatibility with current skills,
CLI behavior, quoting, pipelines, and shell composition. The design explicitly
trusts this command channel and accepts that blacklist enforcement is incomplete
against malicious or deliberately obfuscated input. Risk is bounded primarily
by upstream permissions, pod isolation, network policy where enforced, resource
limits, a minimal read-only image, and auditing rather than by parsing
every CLI command and flag.

Typed APIs remain preferable for high-risk, mutation, file-transfer, streaming,
and credential-disclosure capabilities and may replace string emulation over
time. Mature credential-injection systems generally understand the destination
protocol and bind credential use to an upstream identity and request shape.
Raw shell commands do not provide that assurance by themselves, so typed
interfaces may still replace high-risk capabilities where stronger policy is
needed.

### Why Keep Long-Lived Pods

The current agent depends on long-lived sessions and persistent operational
context. Matching the proxy lifecycle reduces reconnect and cold-start behavior.
The design accepts persistence risk while keeping credentials outside the agent.
Per-command child processes limit process-state accumulation without changing
pod lifecycle. The proxy HOME and workspace persist for the life of the pod so
CLI configuration and multiple repositories can be reused; replacement of the
proxy pod clears that `emptyDir` state.

## Rollout Plan

### Foundation

1. Inventory every credential, identity, CLI, SDK, integration, mount, and
   authenticated network path currently available to the agent.
2. Implement the two canonical workloads, explicit execution context, metadata
   blocking, admission enforcement, audit records, and pair-aware readiness.
3. Build digest-pinned sandbox and credential-proxy images. Require the approved
   hardened RuntimeClass and security context for both.
4. Add static, protocol, policy, negative, and running-pod conformance tests
   before routing production operations through the proxy.

### Phase 1: Credentialed CLI Isolation

1. Move Kubernetes, cloud, GitHub CLI, and supported Git credentials and
   identities into `<agent>-credential-proxy`.
2. Replace real sandbox CLIs with credential-free `kubectl`, `gcloud`, `gh`, and
   `git` wrappers and remove their local credential stores and helpers.
3. Forward exact raw CLI command text and support normal shell composition,
   including pipelines, redirection, mutations, and `kubectl logs`, subject to
   upstream permissions, the declarative security blacklist, and resource
   limits.
4. Cut over each migrated capability atomically. Once a capability uses the
   proxy, proxy failure fails closed; the operator never restores its legacy
   local credential, configuration, binary, or authenticated fallback.

The sandbox remains transitional and is not credential-clean while non-CLI
integrations still hold credentials there. Phase-one status and documentation
must say this explicitly.

### Phase 2: Complete Sandbox Credential Removal

1. Move Slack, MCP, model-provider, and observability credentials to
   protocol-aware proxies or gateways; Google Chat and the non-secret trusted
   API ingress path have already moved in the phase-one implementation.
2. Remove remaining Secret references, service account tokens, Workload
   Identity, credential passthrough, and credential-producing endpoints from the
   sandbox.
3. Enable the complete credential-isolation conformance gate only after running
   verification proves that no legacy path remains.

### Phase 3: Capability Expansion

1. Add richer streaming, file-transfer, and terminal transport where the initial
   raw text request/response transport is inadequate.
2. Add typed APIs for capabilities that later require stronger operation-level
   policy than trusted raw shell execution provides.
3. Split high-risk or read/write identities when upstream least privilege
   requires separate proxy workloads.

## Acceptance Criteria

The design is complete when automated tests demonstrate that:

- the platform does not inject credential values or secret-backed variables into
  the agent container environment;
- the entire Hermes process tree runs inside the Agent Sandbox, not only its
  terminal backend;
- Hermes skill, terminal, credential-file, and MCP environment passthrough paths
  cannot inject a platform-managed credential into the Agent Sandbox;
- no Secret or service account token is mounted in the agent pod;
- the agent has no cloud Workload Identity and cannot reach metadata endpoints;
- common cloud, Kubernetes, Git, GitHub, SSH, and integration credential paths
  contain no platform-provisioned credential in agent-accessible filesystems;
- proxy credentials are present only in the paired proxy pod;
- agent and proxy share no credential-bearing volume or filesystem;
- direct Kubernetes, cloud, GitHub, Slack, and other credentialed API access from
  the agent is blocked;
- real authenticated CLIs are absent from the agent image and wrappers contain
  no reusable credential;
- bypassing wrappers and calling the proxy directly receives identical context,
  blacklist, and resource-limit checks;
- accepted raw command text is delivered byte-for-byte and executed by the fixed
  non-interactive shell without rewriting;
- pipelines, redirection, chaining, substitutions, variables, and generic CLI
  flags work subject to the proxy runtime and upstream authorization;
- every configured blacklist rule blocks before shell startup and returns the
  stable `SECURITY_POLICY_BLOCKED` response with its rule identifier;
- credential-printing and credential-export commands are rejected;
- proxy failure never triggers local fallback;
- the operator creates, upgrades, reports, and deletes agent/proxy pairs as one
  logical unit;
- the logical agent is never `Ready` when its proxy is absent, unhealthy, or
  protocol-incompatible;
- post-deployment verification fails provisioning when any platform-managed
  credential path is found in the agent;
- long-lived proxy restarts, timeouts, cancellations, and repeated commands do
  not create agent-visible credential files or shared proxy state.

## Future Work

- Explicit slash approval and user-only raw credential disclosure.
- Credential detection and redaction in command output, logs, objects, and
  errors.
- Typed APIs or MCP tools for mutation, file transfer, streaming, and remote
  execution.
- Safe manifest and patch upload for `kubectl apply` and Git workflows.
- Stronger disposable or resettable agent sandbox lifecycle options.
- High availability for paired proxies without weakening one-to-one identity.
- Formal data-loss prevention for model, telemetry, DNS, and tool-output paths.
- Automated discovery and regression testing of blacklist rules against pinned
  CLI versions.

## Open Questions and Remaining Concerns

### Proxy Authority Is Still Agent Authority

Credential placement isolation prevents ambient credential files and identities
from living in the Agent Sandbox. It does not guarantee that a trusted raw shell
command cannot extract a credential from the proxy or abuse its authority. A
prompt-injected agent that requests `gcloud projects add-iam-policy-binding` or
`kubectl delete` may cause operational damage without seeing a credential, and a
deliberately obfuscated command may bypass a blacklist rule. This design accepts
those cases as outside its command-channel threat model. Verified end-user
identity and upstream least-privilege RBAC, IAM, and GitHub permissions are the
authoritative controls; the blacklist is not an authorization system.

### CLI Coverage Policy

The proxy executes exact raw command text through its fixed shell and attempts
to preserve all behavior supported by the installed CLI and helper versions.
Limits arise from the minimal proxy image, separate sandbox/proxy filesystems,
non-interactive transport, resource bounds, upstream permissions, and explicit
security blacklist rather than hard-coded CLI subcommands or flags.

### Proxy Workload Identity

The proxy needs narrowly scoped Kubernetes and cloud identities. Current
platform-agent IAM is broad. Moving the same broad roles into a proxy removes
credential exposure but does not reduce operational blast radius. RBAC and IAM
should be split by integration and read-versus-write capability.

### Trusted In-Cluster Callers

The design assumes every workload able to reach the ClusterIP proxy is trusted.
It therefore does not protect proxy authority from a compromised or malicious
in-cluster pod. If that assumption changes, workload authentication and caller
authorization require a separate design.

### Deployment Atomicity

Kubernetes cannot guarantee two separate pods are always simultaneously running.
The operator can guarantee desired paired resources, ordering, readiness,
fail-closed behavior, and continuous reconciliation. If "always side by side"
means stronger process-level availability, an explicit availability target and
HA design are required.

### Long-Lived State

Both pods are long-lived. A compromised agent can persist malicious state in its
workspace, and a buggy proxy may accumulate temporary CLI state. Per-command
temporary homes, bounded storage, cleanup, periodic health checks, and an
operator-triggered reset mechanism are recommended even though pod-per-command
execution is out of scope.

### Credentialed Integrations Embedded in Hermes

Slack and some MCP services still run inside or alongside Hermes logic. Their
credentials require protocol-aware relays, as implemented for Google Chat, not
merely CLI wrappers.

### Output Risk

Output classification is out of scope, but legitimate commands such as
`kubectl logs`, `kubectl describe`, cloud debug output, or Git remote inspection
can return credentials stored by other workloads. Known direct credential
commands are rejected, but this does not eliminate incidental disclosure. This
risk is explicitly accepted for required phase-one capabilities such as bounded
`kubectl logs`. It remains outside the phase-one credential-provisioning
invariant and is mitigated only by least-privilege read scope, output bounds,
auditing, and later output classification and redaction.
