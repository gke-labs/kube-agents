# Kube-Agent Security Configuration

<!-- prettier-ignore-start -->

## Feature Summary

This document defines the kube-agent security configuration model. It records current behavior, supported configuration choices, the recommended initial configuration, and the target authorization model.

The objective is to move GKE operations from reactive manual work toward proactive, intent-driven assistance while preserving least privilege, audit attribution, and human control.

Security is configured along three independent axes:

| Aspect | Option A | Option B | Recommended initial configuration | Target configuration |
| --- | --- | --- | --- | --- |
| Capability | **Read-only:** inspect resources, logs, metrics, and other approved operational data | **Mutation-enabled:** allow explicitly selected create, update, patch, or delete actions | Read-only | Read-only by default, with optional scoped mutations and approvals |
| Interaction | **Chatroom:** interact with multiple people and systems; instructions and facts may come from users, agents, events, repositories, or cron jobs | **Private chat:** interact with one developer; retain memory only across that developer's sessions | Private chat | Both are supported; chatroom authorization must preserve each initiating user's identity |
| Authorization | **Agent service account:** each agent has fixed IAM independent of the user | **Agent service account + user IAM inheritance:** execute as the agent while limiting effective access to the intersection of agent policy and the initiating user's IAM | One dedicated read-only service account per agent | Agent identity with user-IAM-derived limits and finer resource controls |

These axes can be combined independently. For example, a private assistant may remain read-only or receive selected mutation permissions, and either interaction model may use fixed agent IAM or user-IAM-derived authorization.

The recommended initial configuration is **private chat + read-only + one dedicated service account per agent**. It provides deterministic identity, authorization, memory, and audit boundaries.

## 1. Current Platform

- An administrator creates a `PlatformAgent` custom resource to declare an agent deployment.
- The Kubernetes operator continuously reconciles Deployments or StatefulSets, PVCs, ConfigMaps, and related resources to the declared state.
- The long-running workload processes configured chat requests, events, scheduled jobs, and skills. The custom resource controls deployment lifecycle; it is not the only source of agent actions.
- Kubernetes access is declared through RBAC. The read-only profile uses `get`, `list`, and `watch`; mutations require explicit grants.
- Google Cloud access uses GKE Workload Identity and an assigned cloud service account rather than static service-account keys.
- Integrations and target projects, clusters, and namespaces are explicitly configured in the `PlatformAgent` specification; the agent does not indiscriminately discover cloud resources.
- Container logs and Kubernetes events flow through the configured logging pipeline, including `fluent-bit` and Google Cloud Logging on GKE.
- Agent state, session history, and skill data use cluster-local PVCs rather than a proprietary long-term database. GKE-managed disks provide encryption at rest, and Kubernetes volume controls restrict attachment.

## 2. Recommended Initial Configuration

Each developer receives one private assistant with:

- one authenticated user and one dedicated Kubernetes and cloud service account;
- explicit project, cluster, and namespace scope;
- private sessions and memory shared only across that developer's sessions;
- read-only permissions;
- independent configuration for optional mutations; and
- telemetry correlating user, agent, session, instruction source, tool call, and platform audit record.

This is comparable to a private development assistant while retaining a distinct identity for execution and auditing.

### Authorization Evolution

| State | Authorization |
| --- | --- |
| Current | Each agent uses declarative Kubernetes RBAC and Workload Identity |
| Recommended initial configuration | Each private assistant has one dedicated service account with only the read permissions needed for approved resources and data |
| Target | The agent retains its own identity, while effective access is the intersection of agent policy and the initiating user's current IAM, further restricted by project, cluster, namespace, resource, and action |

User-IAM-derived policy must refresh when user IAM changes. It must not retain revoked access or exceed the user's current authority. This prevents access from crossing user, project, or cluster boundaries and lets resource owners retain decentralized control.

Recommendations and proposed changes are delivered through pull requests for human approval.

### Optional Mutations

Mutation permissions:

- are absent by default and configured per assistant;
- are limited by action and resource scope;
- do not exceed the paired user's authority;
- require approval for designated sensitive actions; and
- are distinguishable from reads in audit records.

## 3. Action Sources and Attribution

Every action executes as the agent service account. Telemetry also records why the action occurred.

| Source | Example | Required attribution |
| --- | --- | --- |
| Autonomous | Investigate an issue detected at 04:00 | Agent identity, trigger type, event or job ID, trace ID, and session ID when present |
| Direct user instruction | Find all underutilized clusters | Agent identity, authenticated user, chat/session ID, trace ID, and tool call |
| Skill, script, or repository workflow | Run a performance test from an approved skill | Agent identity, user or autonomous trigger, session/trace ID, and automation identifier |

The target provenance model also records the immutable version or commit of executed skills and scripts and maintains a version-controlled changelog for approved automation.

## 4. Session, Memory, and Retention

- One assistant must not read another assistant's sessions, memory, cache, or persisted data.
- Storage and retrieval must carry user and assistant boundaries. Application filtering must not be the only control where workload or storage isolation is available.
- Retention and deletion periods must be explicit. By default, persisted data is retained only for the lifetime of its `PlatformAgent`.
- Deleting a `PlatformAgent` must trigger or document cleanup of its PVCs and associated secrets; deleting only the workload must not be assumed to remove persistent data.
- Evaluation must verify that the cleanup workflow leaves no residual data beyond the configured retention policy.

Automated, verified lifecycle cleanup is the target state.

## 5. Credentials and Command Execution

All supported `gcloud`, `kubectl`, `gh`, and `git` operations pass through a controlled tool or credential-proxy path.

- The agent sandbox must not receive API keys, access tokens, refresh tokens, private keys, or Kubernetes ServiceAccount tokens through its environment or filesystem.
- Credentialed commands execute under the assistant's assigned service identity.
- Cloud commands use short-lived credentials or service-account impersonation, not static keys.
- Repository credentials are short-lived and repository-scoped.
- GitHub, Google Chat, Slack, and other external services are available only through explicitly configured integrations.

## 6. Audit and Git Attribution

Every supported `gcloud` and `kubectl` invocation must emit a structured tool-call record through OpenTelemetry or the configured logging pipeline. The record includes timestamp, outcome, agent identity, executable, arguments, target scope, instruction source, trace ID, session ID, and authenticated requester when applicable.

Cloud and Kubernetes audit logs remain authoritative for API activity. Trace and session IDs correlate tool telemetry with those records. Data Access audit logs must be enabled when required reads are not logged by default.

Records are available through the configured log and trace backend, including Google Cloud Trace Explorer. Direct chat actions expose the authenticated user and session. Autonomous actions identify their event, system, or scheduled trigger.

Pull requests created by kube-agent identify the agent as the authoring automation. PR metadata records the developer or autonomous trigger, assistant identity, session and trace IDs, and automation version when available.

## 7. Acceptance and Evaluation

The feature is accepted when:

1. the assistant starts with read-only Kubernetes and cloud permissions;
2. it cannot access resources outside its configured effective scope and, when user IAM inheritance is enabled, cannot exceed the initiating user's authority;
3. another assistant cannot read its sessions or memory;
4. its sandbox has no credentials or ServiceAccount tokens in its environment or mounted filesystems;
5. direct, autonomous, and skill-mediated actions are distinguishable;
6. a `gcloud` or `kubectl` call can be followed from tool telemetry to the corresponding platform audit record;
7. direct chat actions correlate to their user and session in Trace Explorer;
8. mutation permissions are absent by default and constrained when enabled;
9. agent-authored pull requests preserve agent and initiating context; and
10. lifecycle cleanup satisfies the configured retention policy.

Evaluation covers:

- **Observability integration:** log, event, metric, and trace ingestion.
- **Skill performance:** accuracy and relevance during real incidents.
- **Security and compliance:** demonstrated least privilege without preventing required read-only workflows.

Success is measured by reduced mean time to detect anomalies, increased safe offloading of operational toil, and sufficient confidence to enable approved low-risk mutations.

## 8. Onboarding Dependencies

- IAM or security engineering support for Workload Identity and service-account impersonation.
- A GKE project and cluster for evaluation.
- An SRE or developer partner, approximately one hour per week during evaluation, to review recommendations and provide feedback.
- Access to logging and tracing dashboards for attribution verification.

<!-- prettier-ignore-end -->
