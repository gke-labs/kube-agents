# Kube-Agent Security Configuration

This document defines the provider-neutral security configuration model for kube-agent. It records the security decisions that apply across supported deployments and distinguishes current behavior from planned capabilities.

## Why It Is Needed

Kube-agent supports deployments with different security and collaboration requirements. An industrial production system may require strict access boundaries, complete attribution, and approval-controlled changes, while a personal deployment may use a simpler policy, allow agents to autonomously perform mutative actions. Requests may come from one developer or from multiple users, systems, events, and scheduled jobs.

## How It Is Configured

The security model defines permission, interaction, and authorization as independent dimensions:

- **Permission**
  - **Read-only:** The agent may inspect approved target resources and operational data but may not create, update, patch, or delete those resources.
  - **Mutation-enabled:** The agent may perform only the explicitly configured mutation actions and target resource scopes. Designated actions may also require approval.
- **Interaction**
  - **Chatroom:** The agent accepts instructions and facts from multiple users and systems, including other agents, events, repositories, and scheduled jobs. Each initiating source must remain attributable.
  - **Private chat:** The agent accepts chat requests from one authenticated developer. Private chat controls who may initiate a request; it does not create a separate runtime or filesystem for each chat session.
- **Authorization**
  - **AgentSA-only:** The agent executes under its assigned service account (`AgentSA`), and access is limited by that identity's permissions.
  - **User-constrained:** The agent still executes under its AgentSA, but a user-initiated action must also be permitted for the authenticated user's service identity (`UserSA`). User authorization can further restrict, but cannot expand, the AgentSA's access.

## Implementation Details

### 1. Platform and Configuration Boundary

- An administrator declares an agent deployment through a `PlatformAgent` custom resource.
- The operator reconciles the workload and its supporting Kubernetes resources to the declared state.
- A deployment composes its permission, interaction, authorization, integration, identity, and resource-scope settings. Selecting one configuration dimension must not implicitly select another.
- Kubernetes permissions are enforced through RBAC. Infrastructure-provider permissions are enforced through the configured workload identity, managed identity, role, or service account.
- Integrations and default target provider accounts, projects or subscriptions, clusters, and namespaces are explicitly configured. Identity policy remains the enforcement boundary for accessible resources.

### 2. Identity and Authorization

- Every agent has an AgentSA. Kubernetes and infrastructure-provider operations execute as that identity. Integrations such as GitHub may use a dedicated, brokered service identity.
- In AgentSA-only authorization, a non-mutating preflight check evaluates the requested action as the AgentSA before execution.
- In user-constrained authorization, a user-initiated action requires two non-mutating preflight checks: one as the AgentSA and one as the UserSA. Both checks must authorize the same requested action.
- Scheduled, event-driven, and other autonomous actions have no UserSA context and are authorized only as the AgentSA, subject to any configured autonomous-action restrictions.
- Preflight checks evaluate current policy and do not copy, merge, or persist AgentSA or UserSA permissions. The actual operation continues to execute as the AgentSA.

AgentSA execution is current behavior. AgentSA and UserSA preflight authorization are planned capabilities.

Preflight authorization requires an integration-specific implementation. Kubernetes authorization reviews and provider permission-test APIs can support this model, but arbitrary CLI commands do not share a reliable dry-run interface.

### 3. Permission Enforcement

- Read-only and mutation-enabled permissions are configured independently of interaction and authorization.
- Mutation permissions are limited by action and resource scope.
- Any configured approval requirement is enforced in addition to authorization.
- Audit records distinguish reads from mutations.

The install supports a read-only and a custom Google Cloud permission set; the GKE-administrator set was removed because an IAM grant of `roles/container.admin` authorizes the agent independently of its Kubernetes RBAC. Kubernetes target-resource inspection is read-only; the agent also has narrowly scoped write permissions for its own leader election. Provider-neutral permission profiles, mutation classification in audit records, and per-action approval policy remain deployment-specific.

### 4. Interaction and Shared State

- Chatroom and private-chat configurations control accepted instruction sources; they do not change the agent's permission or authorization model.
- A `PlatformAgent` is an agent-level isolation boundary. Sessions handled by the same agent share its sandbox, PVC-backed agent home, skills, scripts, configuration, workspace files, and file-based memory.
- Hermes built-in memory and user-profile features are disabled by default and may be enabled through `PlatformAgent` configuration. Enabling them does not create per-session filesystem isolation.
- Operator-managed storage is created per `PlatformAgent`. Administrator-supplied volumes may be shared intentionally and remain outside this isolation guarantee.
- Where multi-user access is supported, telemetry combines the PlatformAgent identity with the user identity stored for the session.

Google Chat and Slack currently support user allowlists. A one-user private-chat constraint can be expressed through those allowlists, but the operator does not expose a provider-neutral interaction-mode field.

### 5. Action Sources and Attribution

Required attribution depends on the source of an operation:

- **Autonomous action:** Records the trigger type, event or job identifier, trace ID, and session ID when one exists.
- **Direct user instruction:** Records the authenticated requester, chat or session ID, trace ID, and resulting tool call.
- **Skill-, script-, or repository-mediated action:** Records the initiating user or autonomous trigger, session and trace IDs, and the available automation identifier.

Google Chat session and requester attribution is implemented. Complete autonomous-trigger attribution, immutable skill, script, and workflow versions, and a version-controlled automation changelog are planned capabilities.

See [Google Chat Session Metadata Data Flow](designs/gchat-session-metadata-data-flow.md) for the implemented Google Chat session-to-requester correlation path.

### 6. Credential Isolation

- The operator-generated agent sandbox must not receive API keys, access tokens, refresh tokens, private keys, or Kubernetes ServiceAccount tokens through its environment or filesystem. Administrator-supplied containers, volumes, and mounts are outside this guarantee. The one operator-managed exception is the audience-bound projected ServiceAccount token the `platform-agent` container presents to the credential broker; see the discussion below.
- Credentialed commands execute in the credential broker Pod, not in the agent sandbox.
- The credential broker receives the AgentSA token and integration secrets required by configured services.
- Provider access uses workload identity or short-lived credentials rather than static keys in the sandbox.
- GitHub access uses short-lived, repository-scoped installation tokens.
- Chat and source-control credentials remain behind explicitly configured relay or command interfaces.
- The current command proxy supports `gcloud`, `kubectl`, `gh`, and `git`. Additional CLIs require explicit proxy support.
- A configuration file the sandbox supplies to a credentialed command selects a target; it does not supply content. The proxy must not run a credentialed command against a document the sandbox authored, because such a document can direct execution, redirect the minted token, or name a file to disclose — none of which the argument-vector deny policy can see. Kubeconfigs are regenerated in the broker for this reason.

The sandbox and the credential runtime must not share a process namespace, and must not run as the
same user, while the credential runtime holds credentials: either one exposes its environment
variables through `/proc`. They now share neither, because they no longer share a Pod. The
credential broker is a Deployment of its own behind a ClusterIP Service on TCP 8765, the shell
sandbox is a StatefulSet of its own, and the agent gateway is a third. `shareProcessNamespace` is
unset in every configuration.

The broker authenticates every caller. A caller presents an audience-bound projected ServiceAccount
token (audience `kubeagents-credential-proxy`, one hour) as a bearer header, and the broker verifies
it with a `TokenReview` before serving any path but `/healthz`; `CREDENTIAL_PROXY_ALLOWED_CALLERS`
names the two ServiceAccounts allowed to call. Three properties do not follow from that. The
allowlist names the gateway's ServiceAccount and the sandbox's, and no policy varies on which one
presented the token, so the check is a multi-tenancy control rather than an agent-containment one.
The token crosses the cluster network in cleartext. And the sandbox holds that token, so it holds a
credential where the design would prefer it held none — short-lived, audience-bound and revocable,
but not non-exportable.

One sharing is left, and it is the gateway's rather than the sandbox's. The broker runs under
`spec.security.serviceAccountName`, defaulting to the PlatformAgent's own name, which is the
ServiceAccount the Workload Identity binding annotates — and that is also the gateway Pod's
ServiceAccount. So the gateway retains an ambient cloud identity from the metadata server. Only
`<agent>-shell`, the Pod that runs model-authored code, runs under a ServiceAccount bound to no
Google service account. Closing the gateway gap needs either `spec.security.workloadIdentityFederation`,
which moves the broker onto a federated credential and lets the annotation come off, or a separate
ServiceAccount for the broker. Neither is on by default.

`spec.security.egressPolicy: Allowlist` renders a default-deny egress NetworkPolicy over the gateway
Pod with the link-local metadata server's credential API left off the allowlist (port 53 to that
address stays, because under Cloud DNS for GKE it is the Pod's resolver). It no longer has a
prerequisite — the broker has left that Pod — and its one refusal, `EgressAllowlistRefused`, is
about the allowlist's own contents.

**It blocks nothing at all today.** Adding a NetworkPolicy is monotone: policies selecting one Pod
are unioned, the API has no deny rule, and the agent Pod is already selected for egress by
`<agent>-gateway-netpol`, which the operator renders whenever `spec.networkPolicy.enabled` is left at
its default (set it to `false` and the gateway policy is withheld instead — on a Helm install the
allowlist is then the Pod's only policy and really does default-deny on an enforcing CNI; a Kustomize
install still carries the static `platform-agent-core-egress` set over the same Pod). So enabling
`egressPolicy: Allowlist` leaves the Pod's permitted egress a strict superset of what it was — wider
by the credential broker on TCP 8765, and wider also by the collector namespace on 4317/4318 when the
agent is not exporting telemetry, since the gateway policy drops its own OTel rule in that case. It
cannot take a destination away. The gateway policy permits `169.254.169.254/32` on TCP 80 and on port
53, plus the discovered metadata-daemon port (`988` by default) to both link-local metadata
addresses, so the metadata path stays open, and it permits TCP 443 to `0.0.0.0/0` minus the private
ranges unless FQDNNetworkPolicy is enabled, so the exfiltration half stays open too. A Kustomize
install adds `platform-agent-core-egress`, which permits the same metadata path; it changes nothing
either way.

The field is therefore a rendered, auditable statement of the destinations the agent is supposed to need, plus the refusal rules and the reconcile behaviour that a real control will need — not a control. Narrowing `<agent>-gateway-netpol`, which still permits the metadata path, is what turns it into one. Two conditions the operator will not be able to enforce even then: the policy does nothing on a cluster whose CNI does not enforce NetworkPolicy, and any other policy an administrator adds re-opens whatever it permits. The capability cost — the agent's DuckDuckGo web search, the `browser` toolset, the `gke` and `developer_knowledge` MCP servers, and direct `github.com` access from the sandbox — falls due at that point and not before; none of it is lost today, because the gateway policy still permits every one of those destinations.

Removing the `iam.gke.io/gcp-service-account` annotation from the gateway Pod's ServiceAccount, once the broker has one of its own or is federated, is the complementary control: it takes the identity away rather than the route, and it does not depend on the CNI. It is separate, planned work.

Credential values deliberately returned by an approved command or integration response are outside the filesystem and environment isolation scope.

See [Credential Isolation Design](credential-isolation-design.md) for the credential-proxy architecture, sandbox boundary, command paths, and known limitations.

### 7. Audit and Git Attribution

- Tool calls and approval decisions emit structured application audit records.
- Direct chat actions include the authenticated user and session context when the integration provides them.
- Autonomous actions identify their event, system, or scheduled trigger when that context is available.
- Kubernetes and infrastructure-provider audit logs remain authoritative for API activity.
- OpenTelemetry trace and session identifiers correlate application telemetry with platform audit records when both systems propagate those identifiers.
- Pull requests created through the GitHub integration identify the configured GitHub App as the authoring automation. Initiating-user, session, trace, and automation-version metadata are planned provenance capabilities.

Complete correlation from every proxied CLI operation to its corresponding provider audit record is a planned capability.

### 8. Acceptance Criteria

The selected configuration is accepted when:

1. the configured permission scope is enforced independently of the interaction and authorization choices;
2. Kubernetes and infrastructure-provider operations execute as the AgentSA;
3. the required AgentSA preflight, and optional UserSA preflight, authorize an operation before it executes;
4. operator-managed persisted state is scoped to its `PlatformAgent`;
5. the operator-generated agent sandbox receives no credentials or Kubernetes ServiceAccount tokens through environment variables or mounted filesystems. This holds fully for the `<agent>-shell` Pod, which runs model-authored code. The gateway's `platform-agent` container is the one exception: it mounts an audience-bound projected ServiceAccount token so it can authenticate to the broker across the network — a deliberate trade described in section 6;
6. direct, autonomous, and automation-mediated actions remain distinguishable in telemetry; and
7. the configured chat access policy accepts only authorized initiators.

Acceptance criterion 3 and the complete source-attribution portions of criterion 6 depend on planned capabilities. Implementation status for the remaining criteria is stated in the corresponding sections above.
