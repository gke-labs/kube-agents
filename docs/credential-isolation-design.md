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

The design limits credentials and credential-producing identity made directly
available to the agent through:

- environment variables and process environments;
- Secret, service account, and credential volume mounts;
- container image contents and writable filesystems;
- persistent volumes and CLI credential caches;
- cloud metadata and Workload Identity endpoints;
- inherited arguments, stdin, sockets, and file descriptors.

The design does not guarantee that authorized command output is free of secrets.
For example, `kubectl logs` may return a credential written by an application.
Returning credentials as ordinary authorized output is outside this design's
confidentiality guarantee. Commands whose explicit purpose is credential
disclosure are nevertheless blocked as a practical guardrail.

## Design Goals

1. The sandbox has no direct access to platform-managed credential files,
   tokens, Secret values, or credential-producing identity.
2. The agent retains broad, non-interactive `kubectl`, `gcloud`, `gh`, and `git`
   functionality, including raw shell composition and `kubectl logs`.
3. The command transport remains generic: it forwards command text instead of
   hard-coding individual CLI operations.
4. Every kubectl execution carries an explicit cluster context selected by the
   platform, not ambient sandbox configuration.
5. Proxy failure fails closed. No native authenticated CLI or credential fallback
   appears in the sandbox.
6. The operator manages the sandbox and proxy as one logical lifecycle.
7. Credentialed non-CLI integrations can use the same separation through typed,
   credential-free protocols.

## Trust Model

The agent and everything it can influence are untrusted with respect to
credential confidentiality. This includes prompts, retrieved content, skills,
repositories, MCP subprocesses, shell commands, and generated code.

Cluster-internal traffic is trusted. The proxy does not authenticate the calling
pod, originating user, or logical agent. Its ClusterIP Service is routing, not an
authorization boundary. Protecting against a compromised in-cluster workload is
outside this design.

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
- a non-secret API ingress sentinel for the trusted cluster transport;
- an explicit Kubernetes project, location, cluster, context, and default
  namespace;
- a metadata-deny egress NetworkPolicy.

Hermes and all agent-accessible tools remain in this pod. The sandbox cannot
install or modify software in the proxy.

### Credential Proxy

The operator reconciles `Deployment/<agent>-credential-proxy` and a matching
ClusterIP Service. The proxy:

- uses the platform's credential-bearing ServiceAccount and Workload Identity;
- contains the real `kubectl`, `gcloud`, `gh`, and `git` binaries;
- owns CLI homes, kubeconfigs, caches, and temporary state;
- accepts versioned JSON requests over the cluster network;
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

## Command Protocol

### Raw Execution Request

`POST /v1/exec` accepts:

```json
{
  "requestId": "uuid",
  "command": "kubectl get pods -n team-a -o json | jq '.items[].metadata.name'",
  "stdin": "optional text",
  "context": {
    "kubernetes": {
      "contextName": "gke_project_location_cluster",
      "projectId": "project",
      "location": "us-east4",
      "clusterName": "cluster",
      "defaultNamespace": "team-a"
    }
  }
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

### Mandatory Kubernetes Context

Every sandbox wrapper request includes the platform-provided Kubernetes context.
A direct request containing kubectl without `context.kubernetes` is rejected
with `EXECUTION_CONTEXT_REQUIRED`.

The proxy creates or reuses a kubeconfig for the declared project, location, and
cluster, verifies the requested context name, and gives each command a temporary
copy. The raw command may contain `--context`, but it cannot select a cluster not
present in that isolated kubeconfig.

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
- the sandbox receives credential-free event envelopes;
- the sandbox sends create, patch, and delete message requests to the proxy;
- Google credentials and Pub/Sub clients remain in the proxy;
- file-attachment OAuth setup is unavailable until per-user OAuth storage is
  moved out of the sandbox.

Slack uses the same boundary:

- the proxy owns the bot and app tokens and maintains the Socket Mode
  connection;
- the sandbox receives credential-free event envelopes and retains Hermes's
  message, thread, command, approval, and response handling;
- an allowlisted Slack SDK-shaped protocol sends message, conversation, user,
  file, reaction, and assistant operations to the proxy;
- authenticated file downloads and bounded uploads pass through the proxy;
- credential-management API families are not exposed to the sandbox.

Future chat integrations should use the same pattern: the sandbox sends typed
provider operations, while a trusted adapter applies credentials at the
authenticated upstream boundary.

## Limitations

- The live sandbox PVC may retain credential files created by the legacy
  single-pod deployment. Moving identity to the proxy does not erase persistent
  files. A one-time migration and a startup/conformance check are required.
- A metadata NetworkPolicy protects the sandbox only when the cluster CNI
  enforces Kubernetes NetworkPolicy. Merely creating the object is insufficient.
- Slack relay requests use the command transport's request-size bound. Large
  uploads, provider features not represented by the relay, and dynamically
  installed Slack extensions are unavailable.
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
- Authorized output, application logs, error messages, and remote repository
  contents may contain secrets. Output classification and redaction are outside
  scope.
- Direct wrappers do not consume inherited stdin because it may carry an MCP or
  other stdio protocol. Submit pipelines and stdin through the raw request.
- `kubectl logs` works for bounded requests. Long-running `--follow`, terminal
  resize, cancellation propagation, and backpressure require a streaming
  transport.
- Files in the sandbox are not visible to the proxy. Commands using local
  manifests, request bodies, uploads, or repositories require stdin/text
  transport or a future file-transfer interface.
- The proxy HOME and workspace are shared by concurrent requests for one agent.
  Commands may contend on CLI configuration or Git working trees. State is lost
  when the proxy pod is replaced.
- The agent cannot install or upgrade proxy CLIs, plugins, packages, credential
  helpers, or system configuration. Platform deployment is required for new
  proxy capabilities.
- Two workloads consume more scheduling and namespace quota than the legacy
  single-pod deployment.

## Design Goal Assessment

| Goal                                                                                         | Assessment                                  | Evidence and remaining work                                                                                                                                                                                                                                                                                                                                           |
| -------------------------------------------------------------------------------------------- | ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| No credentials or tokens accessible through the sandbox filesystem, environment, or identity | **Not met**                                 | The pod spec has no Secret env/volumes, no service account token mount, and no Workload Identity annotation. However, the persistent HOME still contains legacy `gcloud` credential databases and kubeconfig/auth-plugin cache files, and the live metadata token endpoint remains reachable. Clean the PVC and enforce metadata isolation before claiming this goal. |
| Generic and scalable architecture                                                            | **Partially met**                           | The raw text protocol, wrappers, image split, and operator reconciliation generalize across CLIs and agents. Capacity scales linearly because each agent has a dedicated long-lived proxy, and mutable proxy state limits safe concurrency. Non-CLI services still require typed adapters.                                                                            |
| Preserve agent capability and avoid making the agent materially less effective               | **Mostly met**                              | Non-interactive CLI commands, shell composition, mutations, multiple repositories, and bounded `kubectl logs` work. Missing TTY/streaming/file transfer, unavailable Chat attachments, and inability to install proxy tooling are real restrictions.                                                                                                                  |
| Explicit context on every kubectl request                                                    | **Met**                                     | Wrappers attach the platform context, the proxy rejects missing context, and kubectl receives an isolated per-request kubeconfig.                                                                                                                                                                                                                                     |
| Trusted cluster-internal command transport                                                   | **Met by design**                           | The ClusterIP endpoint has no caller authentication, matching the stated trust model. This is not safe if the threat model expands to malicious in-cluster workloads.                                                                                                                                                                                                 |
| No native authenticated CLI or legacy fallback in the sandbox                                | **Met for executables; cleanup incomplete** | Sandbox CLI names resolve to proxy wrappers and native binaries are absent. Proxy failure fails closed. Legacy credential files on the PVC still violate the broader credential-clean objective.                                                                                                                                                                      |
| Google Chat credentials isolated in the proxy                                                | **Met for text messaging**                  | Pub/Sub ingestion and Chat create/patch/delete execute in the proxy over a credential-free protocol. Per-user attachment OAuth is deferred.                                                                                                                                                                                                                           |
| Slack credentials isolated in the proxy                                                      | **Met for supported relay operations**      | Bot and app tokens, Socket Mode, Web API calls, and authenticated downloads execute in the proxy. The sandbox receives events and invokes an allowlisted credential-free API. Large uploads and unrepresented provider features remain restricted.                                                                                                                    |
| Block direct credential-disclosure commands                                                  | **Met as a guardrail**                      | Known commands return policy error and exit 126. The blacklist is intentionally not a complete defense against equivalent or indirect disclosure.                                                                                                                                                                                                                     |
| Complete credential isolation for every supported integration                                | **Not met**                                 | Arbitrary user-supplied pod configuration and deferred per-user attachment OAuth can still place credentials in the sandbox. Admission enforcement and OAuth migration are required.                                                                                                                                                                                  |

Overall, the two-workload architecture and generic command path meet the core
structural direction, but the running system must not yet be described as a
credential-clean sandbox.

## Required Closure Criteria

The credential-isolation goal is complete only when all of the following pass:

1. Remove legacy cloud, Kubernetes, GitHub, SSH, Chat OAuth, and credential-helper
   state from every sandbox PVC without deleting unrelated agent state.
2. Prove the sandbox cannot obtain a token from IPv4 or IPv6 metadata endpoints;
   enable an enforcing CNI or use an equivalent infrastructure control.
3. Keep `automountServiceAccountToken: false`, remove Workload Identity
   annotations and bindings, and verify no projected token path exists.
4. Verify there are no Secret environment variables, Secret volumes, credential
   files, native authenticated CLIs, or credential helpers in the sandbox.
5. Migrate or disable every integration that still injects credentials into
   Hermes, including attachment OAuth.
6. Add admission checks that reject sandbox Secret references, credential-bearing
   identity, native authenticated images, unsafe sidecars, and configuration
   paths that bypass the proxy.
7. Run positive CLI and Chat tests, negative credential-disclosure tests,
   mandatory-context tests, proxy-unavailable tests, metadata tests, and
   persistent-filesystem scans against the deployed pods.

Until these criteria pass, documentation and status reporting must distinguish
"credential proxy deployed" from "sandbox credential isolation achieved."
