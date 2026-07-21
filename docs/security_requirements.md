# Security Requirements for Kube Agents Onboarding

This document outlines the security, architecture, and compliance requirements for onboarding to the Kube Agents platform, specifically addressing concerns regarding access, data retention, and operational mechanisms in non-production (e.g., Staging) environments.

## 1. Architecture & Operations

### Operational Model
The kube-agents system utilizes the standard Kubernetes Controller Pattern. It acts as an intent-driven operator within the GKE cluster, not as a standalone "black box" background process.
*   **Intent-Driven:** The agent only acts when an administrator submits a `PlatformAgent` Custom Resource Definition (CRD) manifest.
*   **Reconcile Loop:** The `k8s-operator` (running in the management cluster) continuously watches for changes to the `PlatformAgent` resource. It reconciles the cluster resources (Deployments, PVCs, ConfigMaps) to match the desired state defined in the CRD.
*   **Background Execution:** The agent pod runs as a managed workload to maintain state and process tasks. It does not perform autonomous actions outside the scope of its defined skills and the permissions granted to its ServiceAccount.

### Log Processing
The agent processes standard Kubernetes container logs and events produced by cluster workloads. These logs are routed through a managed `fluent-bit` sidecar/config and ingested into the existing GKE Managed Logging pipeline (Google Cloud Logging).

## 2. Security & Compliance

### Data Retention
*   **No Long-Term Proprietary Database:** The platform does not implement a custom long-term data retention database.
*   **Stateful Persistence:** All agentic state, session history, and skill data are stored in cluster-local PersistentVolumeClaims (PVCs) bound specifically to the agent identity.
*   **Lifecycle Cleanup:** Data is retained only as long as the `PlatformAgent` CRD exists. Upon deletion of the CRD, the operator automatically triggers a cleanup of the associated PVCs and secrets, ensuring no data residue remains.

### Data Access and Encryption
*   **Access Control:** The PVC data store is protected by standard Kubernetes Volume Access Policies. Only the Pod associated with the `PlatformAgent` has read/write access to this volume.
*   **Encryption at Rest:** Because the volume is managed via GKE, data benefits from underlying GCE disk encryption at rest by default.

## 3. Identity & Access Management (IAM)

The agent’s permissions are granular, transparent, and defined declaratively in the cluster.

### Kubernetes RBAC (In-Cluster)
*   **Least Privilege:** The agent's capabilities are strictly limited by a `ClusterRole`. By default, it requires `get`, `list`, and `watch` permissions for cluster observability.
*   **Read-Only Profile:** An "SRE/Troubleshooter" profile can be utilized, which has a limited set of get/list permissions with **no mutations allowed**. The output is a set of recommendations surfaced as pull requests for human-in-the-loop approvals.
*   **Explicit Mutation Control:** Any ability to create, patch, or delete resources must be explicitly defined in the `ClusterRole` manifest.

### GCP IAM (External Access)
*   **Workload Identity:** Access to Google Cloud resources is governed via GKE Workload Identity. The agent does not use static, long-lived service account keys stored in the cluster.
*   **Granular Access:** The agent's Kubernetes ServiceAccount must be linked to a Google Cloud IAM Service Account (GSA). This GSA should be granted only the minimum necessary roles (e.g., `roles/logging.viewer`, `roles/monitoring.viewer`).

## 4. Integrations & Data Sources

*   **Configured Access:** The agent accesses data sources and external platforms strictly based on its configuration manifest (`PlatformAgent` spec). It does not "crawl" the cloud environment blindly.
*   **Cloud Logs:** Read access to GKE logs and metrics is achieved through standard Kubernetes ServiceAccount token injection and GKE Workload Identity.
*   **External APIs:** The agent accesses external platforms (e.g., GitHub, Slack) only using secrets injected into the pod from the Kubernetes Secret store.

## 5. Security Summary

| Capability | Security Mechanism |
| :--- | :--- |
| **Mutation Control** | Defined via standard Kubernetes RBAC verbs in `role.yaml`. |
| **Authentication** | Google Cloud Workload Identity (No static secret keys). |
| **Data Storage** | Local Kubernetes PVCs, encrypted at rest by GKE. |
| **Auditability** | All agent actions are traceable via Kubernetes API Audit Logs. |
| **Configuration** | Fully declarative (GitOps-ready); no manual cluster mutation allowed. |

## 6. Onboarding Success Criteria & Evaluation

### Goals
To transition GKE cluster management from reactive, manual operations to proactive, intent-driven operations, acting as a force multiplier for the SRE team.

### Evaluation Benchmarks
1.  **Agent Observability Integration:** Effectiveness of ingesting unique log telemetry and monitoring data.
2.  **Skill Performance:** Accuracy and relevance of automated analysis and suggestions against actual cluster incidents.
3.  **Security & Compliance Audit:** Verification that permissions (RBAC/IAM) strictly adhere to "Least Privilege" policies and that the agent operates effectively without excessive rights.

### Success Metrics
*   **MTTD Reduction:** Measurable decrease in Time to Detect anomalies.
*   **Manual Task Offloading:** Number of operational "toil" tasks successfully handled by the agent.
*   **Operational Confidence:** Reaching a "trust threshold" to move from recommendations to autonomous execution for low-risk workflows.

### Required Resource Allocation (Partner Team)
*   **Permissions Provisioning:** IAM/Security engineer assistance to set up GKE Workload Identity bindings.
*   **Environment Access:** Provisioning of a non-production GKE environment (staging/dev) for evaluation.
*   **Operational Partnership:** SRE/Developer contact (approx. 1 hour/week) to review recommendations and provide feedback.
*   **Observability Feedback:** Access to logging/tracing dashboards to verify telemetry visibility.