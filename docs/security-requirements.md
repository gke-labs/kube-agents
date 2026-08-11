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

The current provisioner supports read-only, GKE administrator, and custom Google Cloud permission sets. Kubernetes target-resource inspection is read-only; the agent also has narrowly scoped write permissions for its own leader election. Provider-neutral permission profiles, mutation classification in audit records, and per-action approval policy remain deployment-specific.

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

- The operator-generated agent sandbox must not receive API keys, access tokens, refresh tokens, private keys, or Kubernetes ServiceAccount tokens through its environment or filesystem. Administrator-supplied containers, volumes, and mounts are outside this guarantee.
- Credentialed commands execute in the credential sidecar, not in the agent sandbox.
- The credential sidecar receives the AgentSA token and integration secrets required by configured services.
- Provider access uses workload identity or short-lived credentials rather than static keys in the sandbox.
- GitHub access uses short-lived, repository-scoped installation tokens.
- Chat and source-control credentials remain behind explicitly configured relay or command interfaces.
- The current command proxy supports `gcloud`, `kubectl`, `gh`, and `git`. Additional CLIs require explicit proxy support.
- A configuration file the sandbox supplies to a credentialed command selects a target; it does not supply content. The proxy must not run a credentialed command against a document the sandbox authored, because such a document can direct execution, redirect the minted token, or name a file to disclose — none of which the argument-vector deny policy can see. Kubeconfigs are regenerated in the sidecar for this reason.

The sandbox and credential sidecar must not share a process namespace while the sidecar holds credentials. The current dashboard-enabled Pod configuration shares the process namespace and runs both containers as the same user, which may expose sidecar environment variables through `/proc`. This must be removed or otherwise isolated before the credential-isolation requirement is satisfied.

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
5. the operator-generated agent sandbox receives no credentials or Kubernetes ServiceAccount tokens through environment variables or mounted filesystems;
6. direct, autonomous, and automation-mediated actions remain distinguishable in telemetry; and
7. the configured chat access policy accepts only authorized initiators.

Acceptance criterion 3 and the complete source-attribution portions of criterion 6 depend on planned capabilities. Criterion 5 is not satisfied while the sandbox shares a process namespace with the credential sidecar. Implementation status for the remaining criteria is stated in the corresponding sections above.
