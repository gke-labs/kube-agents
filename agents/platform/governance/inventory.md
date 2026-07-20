# First-Time Environment Discovery & Inventory Scan (`bootstrap-inventory-scan`)

**Purpose:** Executes the background GKE environment discovery, topology inspection, and SRE workload audit on initial agent boot, generating the unified `/opt/data/INVENTORY.md` file.

---

## Pre-Execution Check

1. **Verify Status:** Check directly via terminal command (`test -e /opt/data/INVENTORY.md`) or directly inspect exact absolute file paths using `read_file` on `/opt/data/INVENTORY.md`. **Do not run relative directory search patterns (`search_files`) since your active working directory (`cwd`) resides inside a subfolder where `/opt/data/` markers won't be listed.**
   - If `/opt/data/INVENTORY.md` is already built on disk, return strictly `[SILENT]` immediately and do nothing.
   - If `/opt/data/INVENTORY.md` is confirmed absent, proceed through the systematic technical discovery process below.

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

Write the unified file `/opt/data/INVENTORY.md`. **This file is delivered to the user verbatim — no agent edits, reformats, or summarizes it afterward — so it must be complete, self-contained, and presentation-ready.** Write in clean Markdown that reads well in a chat client. Do not leave placeholders, "TODO", or truncated tables; fill in every value you discovered (use `n/a` only when a value genuinely does not apply).

Structure the file in this order:

1. **Greeting Header:** A short, friendly heading and one or two sentences framing the report — e.g. a title like `# GKE Environment Discovery Report`, and a line noting this is the first-time environment scan for the project.

2. **GKE Fleet Discovery Table:** One row per discovered cluster.

   | Cluster Name | GCP Region / Zone | Status | K8s Version | Node Pools / Machine Types | Workload Identity | Observability Stack | Deployment Toolchain |
   | :----------- | :---------------- | :----- | :---------- | :------------------------- | :---------------- | :------------------ | :------------------- |

3. **Workloads Inventory Table:** One row per workload discovered across clusters.

   | Cluster | Namespace | Workload Name | Kind | Replicas (`Ready/Total`) | Probes (`Live/Ready`) | Resource QoS (`Req/Lim`) | OTel / Telemetry | Security Context (`NonRoot`) |
   | :------ | :-------- | :------------ | :--- | :----------------------- | :-------------------- | :----------------------- | :--------------- | :--------------------------- |

4. **Prioritized SRE Remediation Plan:** The full set of high-impact recommendations, grouped by priority — not just headings, but a concrete, actionable list under each:
   - **Priority 1 — Security & Identity Hardening** (Workload Identity, Shielded Nodes, Dataplane V2, Pod Security Admission, non-root/read-only filesystems).
   - **Priority 2 — Workload Reliability & Probes** (missing liveness/readiness/startup probes, resource requests/limits and QoS, HPA coverage).
   - **Priority 3 — Observability & Telemetry** (OpenTelemetry collection, Managed Service for Prometheus, SLO/error-budget alerting, missing standard SRE alerts).

   For each item, name the affected cluster/namespace/workload where applicable and state the recommended action concisely, so the reader can act on it directly.

---

## Step 5: Post-Scan Completion & Silent Exit

Once `/opt/data/INVENTORY.md` is fully written and confirmed on disk, return strictly `[SILENT]` immediately without running any further terminal commands. Delivery to chat is handled separately by the `bootstrap-inventory-delivery` job — do not attempt to send the report yourself.
