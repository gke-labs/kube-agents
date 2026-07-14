# First-Time Environment Discovery & Inventory Scan (`bootstrap-inventory-scan`)

**Purpose:** Executes the background GKE environment discovery, topology inspection, and SRE workload audit on initial agent boot, generating the unified `/opt/data/INVENTORY.md` file.

---

## Pre-Execution Check

1. **Verify Bootstrap Status:** Inspect whether `/opt/data/BOOTSTRAP.md` exists in your root or `/opt/data` workspace.
   - If `/opt/data/BOOTSTRAP.md` does **NOT** exist (or if `/opt/data/.bootstrap_completed` is present), return strictly `[SILENT]` and do nothing.
   - If `/opt/data/BOOTSTRAP.md` **DOES** exist and `/opt/data/INVENTORY.md` is not built yet, proceed through the systematic discovery process below.

---

## Step 1: Environment Landscape & Fleet Discovery

Use native Google Cloud CLI (`gcloud`) and Kubernetes (`kubectl`) read-only commands to systematically map the project landscape:

1. **Identify GCP Project & Fleet Bounds:**
   - Run `gcloud config get-value project` and `gcloud container clusters list --project=<project-id>` to enumerate every active and stopped GKE cluster in the project.
2. **Inspect Cluster Control Planes & Topologies:**
   - For every running GKE cluster discovered (`e.g., kage-mgmt, platform-agent-host`), inspect its configuration: Kubernetes version, control plane region/zone, node pools (`machine types, node counts, autoscaling boundaries`), network configuration (`VPC-native, Dataplane V2 / eBPF`), and enabled GKE features (`Workload Identity, Managed Prometheus, OpenTelemetry collection`).
3. **Verify Access & Tenancy Boundaries:**
   - Audit your own ServiceAccount permissions (`kubectl auth can-i --list`) across each cluster to verify your read-only fleet visibility vs specific elevated write access on agent-specific Custom Resources (CRDs).

---

## Step 2: Workload & Service SRE Audit

For each running cluster discovered in Step 1, perform an SRE production-readiness audit across all namespaces and active workloads:

1. **Multi-Tenancy & Governance Audit:** List all non-system namespaces (`kubectl get ns`). Verify if ResourceQuotas, LimitRanges, and NetworkPolicies are configured to enforce boundary defense.
2. **Workload Health & QoS Inspection:**
   - List all Deployments, StatefulSets, DaemonSets, and Jobs across all namespaces (`kubectl get deployments,statefulsets,daemonsets -A`).
   - **Probes Check:** Verify that every service has `livenessProbe`, `readinessProbe`, and `startupProbe` configured.
   - **Resource Management Check:** Verify that containers define explicit `requests` and `limits` (check Quality of Service class: `Guaranteed`, `Burstable`, or `BestEffort`).
   - **Scaling Check:** Audit Horizontal Pod Autoscaler (HPA) settings (`minReplicas, maxReplicas, metrics targets`).
   - **Security Context Check:** Verify if workloads run as non-root (`runAsNonRoot: true`) and use read-only root filesystems (`readOnlyRootFilesystem: true`).
3. **Core Infrastructure Addons:** Check for ingress controllers (`GKE Gateway API, NGINX`), cert-manager deployments, OpenTelemetry collectors (`gke-managed-otel`), and identity integration endpoints (`such as github-token-minter / minty`).

---

## Step 3: Proactive GKE Infrastructure Improvement Analysis

Based on your discovery and engineering best practices (`use the developer_knowledge tool to query for up-to-date Google Cloud and GKE best practices when appropriate`), proactively evaluate gaps against modern GKE patterns:

### 1. Observability & Telemetry (`OpenTelemetry & Managed Prometheus`)
- Check if the GKE OpenTelemetry collector (`gke-managed-otel` namespace / `hermes_otel` plugin) is deployed and actively scraping workload traces/metrics. If absent, note a high-priority recommendation to enable OTel collection (`OTLP / Telemetry API`).
- Check if Google Cloud `Managed Service for Prometheus` (`gmp-system` / PodMonitoring CRDs) is enabled to eliminate manual Prometheus scraping overhead.

### 2. Alerting Hygiene & SLO Definition
- Evaluate whether alerting relies on Service Level Objectives (`SLOs`) and error budget burn rates rather than noisy, transient infrastructure thresholds (`such as CPU usage`).
- Identify missing standard SRE health alerts: `Pod CrashLoopBackOff / OOMKilled events`, `Control Plane API latency spikes`, `PersistentVolumeClaim exhaustion`, and `Workload probe failures`.

### 3. GKE Security Hardening & Workload Identity
- Verify whether pods accessing Google Cloud APIs (`e.g., Cloud KMS, Cloud Storage, BigQuery`) use **GKE Workload Identity** (`serviceAccountName` with `iam.gke.io/gcp-service-account` annotation) rather than static service accounts or JSON key files.
- For Standard mode clusters, evaluate adherence to baseline hardening: **Shielded GKE Nodes**, **Dataplane V2 (`eBPF`)**, **Node Auto-Upgrades**, and **Pod Security Admission (`PSA`)**.

---

## Step 4: Compile Master Inventory (`/opt/data/INVENTORY.md`)

Create and write the unified file `/opt/data/INVENTORY.md` clearly outlining all collected metrics across the environment:

1. **GKE Fleet Discovery Table:**
   | Cluster Name | GCP Region / Zone | Status | K8s Version | Node Pools / Machine Types | Workload Identity | Observability Stack | Deployment Toolchain |
   | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |

2. **Workloads Inventory Table:**
   | Cluster | Namespace | Workload Name | Kind | Replicas (`Ready/Total`) | Probes (`Live/Ready`) | Resource QoS (`Req/Lim`) | OTel / Telemetry | Security Context (`NonRoot`) |
   | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |

3. **Actionable Drift & Remediation Recommendations:**
   Summarize high-impact gaps grouped by priority (`Priority 1: Security & Identity Hardening`, `Priority 2: Workload Reliability & Probes`, `Priority 3: Observability & Telemetry`) for presentation during user interactions.

---

## Step 5: Post-Scan User Notification & Cleanup Check

Once `/opt/data/INVENTORY.md` is compiled:

1. Check whether the user alignment marker `/opt/data/.user_aligned` exists.
2. **If `/opt/data/.user_aligned` does NOT exist:**
   - The human team has not interacted with the agent or completed onboarding alignment yet. Return strictly `[SILENT]` and exit cleanly without removing any files or pushing unrequested messages.
3. **If `/opt/data/.user_aligned` DOES exist:**
   - The user initiated chat interactions while the scan was in progress and is waiting for completion. Your output **MUST NOT** be `[SILENT]`.
   - Read `/opt/data/INVENTORY.md` and present a comprehensive Markdown summary of the fleet clusters, workload audit highlights, and your prioritized SRE recommendations directly in the chat output.
   - Immediately run the cleanup routine to remove bootstrap artifacts (`BOOTSTRAP.md`, `INVENTORY.md`, `inventory.md`, and `.user_aligned`) and transition to standard daily operation:
     ```bash
     python3 /opt/data/scripts/bootstrap_cleanup.py
     ```
