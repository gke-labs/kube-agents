# Credential-Isolated Agent Sandbox and Credential Proxy

## Purpose

Separate the untrusted agent runtime from platform-managed credentials without
removing the operational CLI capabilities agents need.

Each logical agent consists of two Kubernetes workloads:

```text
user or chat event
        |
        v
<agent>-sandbox  -- credential-free protocol -->  <agent>-credential-proxy
        |                                             |
        | uncredentialed dependencies                 | authenticated APIs
        v                                             v
model and telemetry gateways                 Kubernetes, GCP, GitHub, Chat
```

The sandbox runs Hermes, tools, skills, and agent-generated code. The credential
proxy owns authenticated CLIs, cloud identity, service configuration, and
credentialed service adapters.

## Scope

This design applies to `PlatformAgent`. `OperatorAgent` and `DevTeamAgent` are
outside its scope.

The design limits credentials and credential-producing identity made directly
available to the agent through:

- environment variables and process environments;
- Secret, service account, and credential volume mounts;
- container image contents and writable filesystems;
- persistent volumes and CLI credential caches;
- cloud metadata and Workload Identity endpoints;
- inherited arguments, stdin, sockets, and file descriptors.

Trusted services include the credential proxy, LiteLLM, the Kubernetes
controller, and enabled chat or token relays. These services may hold credentials
because agent-generated code does not execute inside them and the agent cannot
read their Secrets, environments, filesystems, or service account tokens.

This is stricter than Hermes's standard
[gateway deployment checklist](https://hermes-agent.nousresearch.com/docs/user-guide/security#gateway-deployment-checklist),
which permits API keys in `~/.hermes/.env` with restrictive file permissions.
The sandbox in this design must not contain those keys at all; they belong in a
trusted service.

Generic authorized output is intentionally available to the agent. This includes
`kubectl get`, `kubectl logs`, ConfigMaps, resource specifications, and remote
command output even when an upstream application placed a credential in that
output. This is an accepted capability tradeoff, not a failure of the pod-level
credential boundary. Commands whose explicit purpose is credential disclosure
are still blocked as a practical guardrail.

[Hermes credential redaction](https://hermes-agent.nousresearch.com/docs/user-guide/security#credential-redaction)
provides defense in depth for MCP tool error messages by replacing recognized
GitHub PATs, OpenAI-style keys, bearer tokens, and common credential parameters
with `[REDACTED]`. It is not a guarantee that arbitrary successful CLI output is
sanitized.

## Design Goals

1. The sandbox has no direct access to platform-managed credential files,
   tokens, Secret values, or credential-producing identity.
2. The agent retains broad, non-interactive `kubectl`, `gcloud`, `gh`, and `git`
   functionality, including raw shell composition and `kubectl logs`.
3. The command transport remains generic: it forwards command text instead of
   hard-coding individual CLI operations.
4. The proxy starts with a platform-selected shell profile, including the
   Kubernetes context and cloud defaults, without command-specific handling.
5. Proxy failure fails closed. No native authenticated CLI or credential fallback
   appears in the sandbox.
6. The operator manages the sandbox and proxy as one logical lifecycle.
7. Credentialed non-CLI integrations forward opaque provider data and generic
   SDK calls without duplicating provider schemas in the relay.

## Trust Model

The agent and everything it can influence are untrusted with respect to
credential confidentiality. This includes prompts, retrieved content, skills,
repositories, MCP subprocesses, shell commands, and generated code.

Cluster-internal traffic is trusted. The proxy does not authenticate the calling
pod, originating user, or logical agent. Its ClusterIP Service is routing, not an
authorization boundary. Protecting against a compromised in-cluster workload is
outside this design.

LiteLLM and the controller are trusted workloads. Credentials or projected
service account tokens present inside those pods are outside the agent sandbox
and do not violate this design. The boundary requires that the agent cannot read
their Secrets or use Kubernetes exec to enter them.

Raw agent command text is also trusted for execution. The proxy runs the text in
a non-interactive shell after applying a blacklist. The blacklist is not a
sandbox, parser, or complete defense against deliberate obfuscation. Actual
authority is constrained by the proxy's Kubernetes RBAC, cloud IAM, GitHub
permissions, resource limits, and workload boundary.

## Architecture

### Agent Sandbox

The operator reconciles `Deployment/<agent>-sandbox` with:

- a dedicated `<agent>-sandbox` ServiceAccount;
- `automountServiceAccountToken: false`;
- removal of inherited `iam.gke.io/*` ServiceAccount annotations;
- no platform-managed Secret environment variables or Secret volumes for
  migrated integrations;
- no native `kubectl`, `gcloud`, `gh`, or `git` binaries;
- credential-free wrappers at `/opt/credential-proxy/bin`;
- a dedicated `<agent>-sandbox-data` PVC that never mounts legacy agent data;
- a non-secret API ingress sentinel for the trusted cluster transport;
- an explicit Kubernetes project, location, cluster, context, and default
  namespace as non-secret agent context;
- a metadata-deny egress NetworkPolicy.

Hermes and all agent-accessible tools remain in this pod. The sandbox cannot
install or modify software in the proxy.

### Credential Proxy

The operator reconciles `Deployment/<agent>-credential-proxy` and a matching
ClusterIP Service. The proxy:

- uses the platform's credential-bearing ServiceAccount and Workload Identity;
- contains the real `kubectl`, `gcloud`, `gh`, and `git` binaries;
- owns CLI homes, kubeconfigs, caches, and temporary state;
- initializes one trusted shell profile at startup with platform-provided
  configuration;
- accepts versioned JSON requests over the cluster network;
- treats every accepted command as generic shell text, without CLI-specific
  parsing or rewriting;
- executes commands with bounded request size, output size, and duration;
- logs request identifiers, policy decisions, exit codes, duration, and
  truncation without logging command output;
- runs Google Chat Pub/Sub and Slack Socket Mode ingestion plus authenticated
  chat API calls when those integrations are enabled.

The proxy has a private `emptyDir` state directory. It does not mount the
sandbox PVC, so commands cannot implicitly read sandbox files.

### Workload Lifecycle

The operator owns both Deployments, their ServiceAccounts, the proxy Service,
policy ConfigMap, and metadata NetworkPolicy. It reports the PlatformAgent ready
only when the expected workloads are ready. The proxy uses `Recreate` deployment
semantics so one logical proxy instance does not share mutable CLI state across
revisions.

Separate pods cannot start, stop, or restart atomically. The enforceable
property is fail-closed behavior: wrappers return an error while the proxy is
unavailable and never restore local authenticated execution.

## Routing Patterns

### Generic CLI Commands

Sandbox CLI names resolve to credential-free wrappers. A wrapper forwards the
shell command to `/v1/exec`; the proxy executes it in a normal trusted shell
whose image, identity, credentials, configuration, and CLI caches are managed by
the platform. Adding another CLI requires installing and configuring it in the
proxy image, but does not require a new executor code path.

The deployment may provide a generic startup command through
`CREDENTIAL_PROXY_BOOTSTRAP_COMMAND`. The current GKE deployment uses it to set
the default Google Cloud project, obtain the proxy kubeconfig, select the
platform context, and set the default namespace. Once startup completes, the
server does not inspect whether a request contains `kubectl`, `gcloud`, `gh`,
`git`, a future CLI, or an ordinary shell pipeline.

### Chat Messages

Long-lived event ingestion and provider APIs do not naturally fit a shell
request. Google Chat and Slack therefore use credential-free transport relays.
The proxy forwards opaque provider events, and Hermes passes generic SDK calls
back for authenticated execution. The relay does not interpret message, thread,
command, card, or action schemas.

## Command Protocol

### Raw Execution Request

`POST /v1/exec` accepts:

```json
{
  "requestId": "uuid",
  "command": "kubectl get pods -n team-a -o json | jq '.items[].metadata.name'",
  "stdin": "optional text"
}
```

The proxy does not rewrite `command`. It executes the original string with:

```text
/bin/bash --noprofile --norc -c <command>
```

This preserves quoting, pipelines, redirection, chaining, substitutions, and
normal CLI behavior. Direct executable wrappers receive an argument vector, so
they reconstruct a shell-equivalent command with safe quoting; byte-for-byte
preservation is available when raw text is submitted directly.

The initial transport is request/response. Interactive TTYs, arbitrary file
transfer, port forwarding, and indefinite streaming are not implemented.

### Trusted Shell Profile

Every command receives the same proxy-owned `HOME`, workspace, temporary
directory, XDG directories, `CLOUDSDK_CONFIG`, `GH_CONFIG_DIR`, and `KUBECONFIG`.
These paths are never mounted into the sandbox. Configuration behaves like a
normal shell: commands can read or modify the proxy's CLI state and can select a
different context if the proxy identity and network can reach it. That behavior
is intentional because agent commands are trusted for execution; IAM and RBAC
remain the authority boundary.

### Security Blacklist

Policy is stored in a ConfigMap and evaluated before shell execution. Current
rules block common forms of:

- `gcloud auth print-access-token` and identity-token disclosure;
- `gcloud config config-helper` credential disclosure;
- `gh auth token` and `gh auth status --show-token`;
- `kubectl create token` and raw kubeconfig disclosure;
- `git credential fill`;
- login, logout, identity replacement, component installation, and CLI extension
  modification.

A blocked request returns HTTP 403, `SECURITY_POLICY_BLOCKED`, a policy rule ID,
and a user-facing security message. Wrapper processes exit with status 126.

This is a narrow guardrail, not a complete semantic policy. Equivalent commands,
new CLI features, shell indirection, or authorized service output may disclose
sensitive data. Deliberate blacklist evasion is outside the threat model.

## Non-CLI Integration Protocols

Google Chat uses the credential proxy as a trusted relay:

- the proxy pulls and settles Pub/Sub events;
- the sandbox wraps unchanged Pub/Sub bytes and attributes in a
  Pub/Sub-compatible message object;
- Hermes's native Google Chat callback is the only event parser;
- a discovery-resource-shaped facade forwards generic Chat API resource,
  method, and argument values unchanged;
- Google credentials and Pub/Sub clients remain in the proxy;
- file-attachment OAuth setup is unavailable until per-user OAuth storage is
  moved out of the sandbox.

Slack uses the same boundary:

- the proxy owns the bot and app tokens and maintains the Socket Mode
  connection;
- the proxy forwards unchanged Socket Mode types and payloads to Slack Bolt;
- Hermes registers and runs its native message, command, action, plugin, and
  response handlers;
- a Slack SDK-shaped client forwards generic API method names and arguments;
- authenticated file downloads and bounded uploads pass through the proxy;
- provider-returned data follows the same authorized-output policy as CLI
  results.

Future chat integrations should forward opaque events to the provider's
official library and forward generic SDK calls to a trusted credentialed
executor.

## Limitations

- Legacy agent PVC data is not copied into the clean sandbox PVC automatically;
  any migration must explicitly exclude credential caches.
- The provisioner enables NetworkPolicy enforcement. Pre-existing or externally
  managed clusters must also enable it or the metadata deny object is inert.
- Slack relay requests use a bounded request size. Large uploads and
  dynamically installed Slack extensions remain unavailable.
- Arbitrary deployment environment variables, sidecars, and volumes can
  reintroduce credentials. Admission enforcement does not yet reject these
  configurations.
- The sandbox currently has broad network reachability where cluster egress
  policy is not enforced. Credential isolation therefore depends on removing
  identity as well as enforcing metadata blocking.
- Any trusted in-cluster workload can call the proxy Service and exercise the
  proxy's upstream authority. Caller authentication and per-user authorization
  are explicitly out of scope.
- The raw shell and blacklist maximize compatibility at the cost of precise
  operation-level authorization.
- Authorized output, application logs, ConfigMaps, resource specifications, and
  remote repository contents may contain secrets and are intentionally returned.
  Hermes redacts recognized credentials in MCP tool error messages; successful
  CLI output is not covered by that guarantee.
- Direct wrappers do not consume inherited stdin because it may carry an MCP or
  other stdio protocol. Submit pipelines and stdin through the raw request.
- `kubectl logs` works for bounded requests. Long-running `--follow`, terminal
  resize, cancellation propagation, and backpressure require a streaming
  transport.
- Files in the sandbox are not visible to the proxy. Commands using local
  manifests, request bodies, uploads, or repositories require stdin/text
  transport or a future file-transfer interface.
- The proxy HOME and workspace are shared by concurrent requests for one agent.
  Commands may race while changing contexts, CLI configuration, or Git working
  trees. State is lost when the proxy pod is replaced.
- The agent cannot install or upgrade proxy CLIs, plugins, packages, credential
  helpers, or system configuration. Platform deployment is required for new
  proxy capabilities.
- New chat providers still need a small transport adapter that maps receipts,
  raw event bytes, generic SDK calls, and explicit binary values.
- Two workloads consume more scheduling and namespace quota than the legacy
  single-pod deployment.

## Design Goal Assessment

| Goal                                                                                         | Assessment                             | Evidence and remaining work                                                                                                                                                                                                                            |
| -------------------------------------------------------------------------------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| No credentials or tokens accessible through the sandbox filesystem, environment, or identity | **Met**                                | The sandbox has no Secret env/volumes, service account token mount, Workload Identity annotation, cloud or CLI credential cache, or reachable metadata token endpoint. Trusted-service credentials remain outside the pod.                             |
| Generic and scalable architecture                                                            | **Partially met**                      | Raw commands and opaque chat events tolerate new CLI operations and provider schemas without relay changes. Capacity scales linearly because each agent has a dedicated long-lived proxy, and each new provider still needs a small transport adapter. |
| Preserve agent capability and avoid making the agent materially less effective               | **Mostly met**                         | Non-interactive CLI commands, shell composition, mutations, multiple repositories, and bounded `kubectl logs` work. Missing TTY/streaming/file transfer, unavailable Chat attachments, and inability to install proxy tooling are real restrictions.   |
| Platform-selected default shell context                                                      | **Met**                                | The proxy bootstrap prepares its Google Cloud project, kubeconfig, Kubernetes context, and namespace once. Requests stay generic and use that trusted shell profile. Commands can intentionally change mutable profile state.                          |
| Trusted cluster-internal command transport                                                   | **Met by design**                      | The ClusterIP endpoint has no caller authentication, matching the stated trust model. This is not safe if the threat model expands to malicious in-cluster workloads.                                                                                  |
| No native authenticated CLI or legacy fallback in the sandbox                                | **Met**                                | Sandbox CLI names resolve to proxy wrappers, native binaries are absent, and proxy failure fails closed. The clean sandbox PVC contains no cloud, Kubernetes, or GitHub credential cache.                                                              |
| Google Chat credentials isolated in the proxy                                                | **Met for text messaging**             | Pub/Sub ingestion and generic Chat API calls execute in the proxy. Raw events are parsed only by Hermes. Per-user attachment OAuth is deferred.                                                                                                        |
| Slack credentials isolated in the proxy                                                      | **Met for supported relay operations** | Bot and app tokens, Socket Mode, generic Web API calls, and authenticated downloads execute in the proxy. Raw events are dispatched only by Slack Bolt and Hermes. Large uploads remain restricted.                                                    |
| Block direct credential-disclosure commands                                                  | **Met as a guardrail**                 | Known commands return policy error and exit 126. The blacklist is intentionally not a complete defense against equivalent or indirect disclosure.                                                                                                      |
| Credential isolation for enabled PlatformAgent integrations                                  | **Met**                                | Model credentials remain in trusted LiteLLM; CLI, Google Chat, and Slack credentials remain in the proxy. Arbitrary user-supplied pod configuration can bypass the boundary until admission enforcement is added.                                      |

Overall, the two-workload architecture and generic command path meet the core
credential-isolation goal for `PlatformAgent`. Authorized output remains visible
by design, with Hermes MCP error redaction as an additional safeguard.

## Validation Criteria

The credential-isolation boundary should continue to be validated by checking:

1. Verify every sandbox PVC remains free of cloud, Kubernetes, GitHub, SSH, Chat
   OAuth, and credential-helper state.
2. Prove the sandbox cannot obtain a token from IPv4 or IPv6 metadata endpoints
   and that NetworkPolicy enforcement remains enabled.
3. Keep `automountServiceAccountToken: false`, remove Workload Identity
   annotations and bindings, and verify no projected token path exists.
4. Verify there are no Secret environment variables, Secret volumes, credential
   files, native authenticated CLIs, or credential helpers in the sandbox.
5. Verify every enabled `PlatformAgent` integration keeps credentials outside
   Hermes; leave integrations such as attachment OAuth disabled until migrated.
6. Add admission checks that reject sandbox Secret references, credential-bearing
   identity, native authenticated images, unsafe sidecars, and configuration
   paths that bypass the proxy.
7. Run positive CLI and Chat tests, negative credential-disclosure tests,
   shell-profile tests, proxy-unavailable tests, metadata tests, and
   persistent-filesystem scans against the deployed pods.
