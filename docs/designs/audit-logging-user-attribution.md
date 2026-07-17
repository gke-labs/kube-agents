# Design: Audit Logging and User Attribution for kube-agents

**Status:** Draft for review

**Priority:** P0 — addresses external concern (Waze #2) and the internal "Audit log all agents"
objective

---

## TL;DR

Kubernetes and Cloud audit logs identify the agent ServiceAccount, but that identity alone does
not identify the human who requested an action. kube-agents can close most of that gap by adding
the authenticated requester and trace or session ID to telemetry records it already produces.

The current runtime enriches Hermes spans with the Google Chat sender and session. This change
wires the Platform Agent Deployment to the GKE Managed OpenTelemetry endpoint, adds stable agent
resource attributes, documents the correlation contract, and provides a reference Kubernetes
audit policy. LLM-request attribution and cluster-object annotations remain runtime follow-ups.

---

## 1. Problem

An investigation should be able to connect these identities:

| Boundary                     | Identity available at that boundary       |
| ---------------------------- | ----------------------------------------- |
| Human to Platform Agent      | Authenticated chat sender                 |
| Platform Agent to Kubernetes | Kubernetes ServiceAccount                 |
| Platform Agent to Cloud APIs | Google service account                    |
| Platform Agent to LiteLLM    | Agent or workload identity, but not human |

Chat history cannot be the only join:

- Messages can age out, spaces can be deleted, and retention differs by platform.
- A chat message does not inherently carry the trace ID of the work it initiated.
- Kubernetes and Cloud audit entries name the workload identity, not the chat sender.

### Goals

- Provide durable, queryable records for agent traces and Kubernetes API mutations.
- Carry an authenticated requester identifier through the records where the runtime can do so
  reliably.
- Correlate records using trace and Hermes session IDs.
- Reuse Managed OpenTelemetry, Cloud Logging, LiteLLM, and Kubernetes audit infrastructure.

### Non-goals

- Cryptographic non-repudiation of the human identity.
- Per-action authorization or policy enforcement.
- Treating a model-generated field as trusted identity.
- Claiming that an object annotation alone attributes every update or deletion.

---

## 2. Existing Components

| Component                            | Current behavior                                                                                              |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| **Managed OpenTelemetry for GKE**    | Provides an in-cluster OTLP endpoint and exports accepted signals to Google Cloud Observability               |
| **Hermes OTel and session plugins**  | Add `hermes.sender.id`, `user.id`, and `session.id` to spans using authenticated gateway session metadata     |
| **LiteLLM proxy**                    | Receives agent LLM calls and exports telemetry, but requester fields are a follow-up                          |
| **Dedicated agent ServiceAccount**   | Gives Kubernetes audit entries a stable workload actor                                                        |
| **Chat and tool audit records**      | Write structured records to standard output for collection by the platform logging agent                      |
| **Kubernetes API-server audit logs** | Record the requesting ServiceAccount, verb, target resource, and timestamp independently of workload metadata |

The expensive storage and query pipelines already exist. The remaining work is to use consistent
identity and correlation fields at each boundary.

---

## 3. Attribution Contract

| Plane                       | Carrier                                                                                               | Status            |
| --------------------------- | ----------------------------------------------------------------------------------------------------- | ----------------- |
| **Agent traces**            | `hermes.sender.id=<email>`, `user.id=<platform>:<user>`, and `session.id=<Hermes session>` span attrs | Implemented       |
| **Kubernetes actions**      | API audit record naming the agent ServiceAccount                                                      | Implemented       |
| **LLM calls**               | OpenAI `user` and `metadata.requested_by`                                                             | Runtime follow-up |
| **Created cluster objects** | `kubeagents.x-k8s.io/requested-by` and optional `request-id` annotations                              | Runtime follow-up |

The operator also adds process-level resource identity:

```text
service.name=<agent-name>-gateway
service.namespace=<namespace>
k8s.namespace.name=<namespace>
kubeagents.agent_type=platform
kubeagents.agent_name=<agent-name>
```

Object identity must use an annotation rather than a label. The current requester identifier is
an email address, and `@` is not valid in a Kubernetes label value. An annotation accepts the
identity without lossy encoding and makes clear that this is correlation metadata, not a selector
or authorization input.

### Correlation

- Start from a person: filter spans by `hermes.sender.id`, then follow the trace and session IDs.
- Start from a Kubernetes mutation: inspect the audit entry for the agent ServiceAccount, then
  correlate by time and target object. When present, the object's requester and request-ID
  annotations narrow the join.
- Start from a chat log: use its `session_id` to find spans with the same `session.id`.
- Start from an LLM call after the follow-up lands: use `requested_by` or its trace ID.

The [operational runbook](../attribution.md) contains concrete queries.

---

## 4. Trust and Security Model

- **Workload actor:** The Kubernetes API server generates the audit entry. A workload-supplied
  annotation cannot change the ServiceAccount recorded by the API server.
- **Human requester:** The gateway/runtime asserts the requester from authenticated ingress
  metadata. The model must not supply or rewrite this value.
- **Object annotation:** This is supporting evidence only. It can be changed, might not exist on
  updates, and can disappear with the object. It must not be used for authorization.
- **Audit-log isolation:** Server-generated records are tamper-resistant only if the agent cannot
  modify their sink or retention. The standard provisioning grants read-only log access. Higher
  assurance deployments should export an immutable copy to a separate security project.
- **Data minimization:** Emails, prompts, outputs, chat messages, and tool inputs can be sensitive.
  Apply least-privilege access and retention, and scrub secrets before export. The reference audit
  policy records only metadata for arbitrary mutations so Secret bodies are not captured.
- **Dedicated identity:** Each Platform Agent uses a dedicated ServiceAccount rather than the
  namespace's `default` ServiceAccount.

---

## 5. Delivery Plan

| Step                             | Work                                                                                              | State             |
| -------------------------------- | ------------------------------------------------------------------------------------------------- | ----------------- |
| **Session and trace identity**   | Persist authenticated session metadata and attach the fixed identity allowlist to Hermes spans    | Implemented       |
| **Agent telemetry export**       | Set OTLP endpoint, protocol, service name, namespace, and agent resource attributes               | This change       |
| **Server-side action ledger**    | Document GKE behavior and provide a reference audit policy for self-managed clusters              | This change       |
| **LLM requester fields**         | Set `user` and `metadata.requested_by` deterministically on LiteLLM requests                      | Runtime follow-up |
| **Cluster object annotations**   | Stamp `requested-by` and `request-id` on objects created through trusted runtime paths            | Runtime follow-up |
| **End-to-end correlation tests** | Verify a chat request, trace, LLM record, and Kubernetes mutation can be joined in a test cluster | Follow-up         |

---

## 6. Stronger Guarantees

If gateway-asserted attribution is insufficient for compliance or policy decisions, add one or
more of these controls:

- An admission component that stamps immutable request metadata from authenticated context.
- Signed requester assertions verified before a tool or API call executes.
- A request record written by deterministic gateway code and protected from agent mutation.
- A cross-project log sink with retention lock and access controlled by the security team.

These controls are intentionally outside the first version, but the trace and session join keys
remain useful if they are added later.
