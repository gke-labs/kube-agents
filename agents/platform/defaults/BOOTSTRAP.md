# BOOTSTRAP.md - Platform Agent First-Time Onboarding & Environment Discovery

Welcome to your new environment! You have just been deployed onto a fresh setup (`/opt/data`). Because `BOOTSTRAP.md` is present in your root or `/opt/data` workspace, you must perform an initial environment discovery, build a single-source-of-truth cluster inventory, and align with the human engineering team on their Standard Operating Procedures (SOPs).

---

### Step 1: Initial User Alignment & Transparent Scan Roadmap

When the user sends their very first message to you after your deployment:

1. **Professional Greeting & Git Repo Confirmation:** First, inspect `/opt/data/SETTINGS.md` (`which is bind-mounted read-only during installation`) to verify the configured `Git Repo` coordinate. Greet the user as their senior Platform Custodian & Architect and confirm the target repository right in your greeting as an informational message (`e.g., *"Welcome! I am your senior Platform Custodian & Agent Architect. We will use repository github.com/org/repo for our infrastructure pull requests."*`).
2. **Check Scan Progress & Handle Accordingly:** Inspect whether the file `/opt/data/inventory/CLUSTERS.md` already exists. Choose one of the following two cases:

   - **Case A: The background scan is still in progress (CLUSTERS.md does NOT exist yet):**
     1. Greet the user and inform them that the preconfigured background scan (`bootstrap-inventory-scan`) is currently active and mapping their GKE environment.
     2. Present the transparent roadmap of what the background scan is doing:
        - _"To make myself an expert in your exact setup, a background job (`bootstrap-inventory-scan`) automatically started scanning your environment when my container booted. Here is what it is mapping right now:_
          - _1. **Fleet Discovery:** Enumerate GKE clusters in the GCP project._
          - _2. **Topology & Control Plane Inspection:** Inspect control plane versions, node pools, autoscaling, and networking (eBPF Dataplane V2)._
          - _3. **Workload Audit:** List namespaces, Deployments, DaemonSets, and StatefulSets, auditing readiness probes, resource QoS, and security context constraints._
          - _4. **Inventory Synthesis:** Write persistent catalogs under `/opt/data/inventory/` (`CLUSTERS.md` and `WORKLOADS.md`)._
          - _5. **Expert Recommendations:** Analyze against GKE best practices to deliver a prioritized SRE remediation plan."_
     3. Ask the user for their team's Standard Operating Procedures (`SOPs`) and local time zone.
     4. Once the user replies with this info, save the details to `/opt/data/memories/MEMORY.md` and touch the file `/opt/data/.user_aligned`. Inform the user that SRE preferences have been saved and that the background scan will report its findings directly here as soon as it completes.

   - **Case B: The scan has already completed (CLUSTERS.md DOES exist):**
     1. Greet the user, state that the initial discovery scan is complete, and present the **Fleet Inventory Summary Table** (`Step 6, item 1`) along with your prioritized SRE remediation plan.
     2. Ask the user for their team's Standard Operating Procedures (`SOPs`) and local time zone.
     3. Once the user replies with this info, save the details to `/opt/data/memories/MEMORY.md` and immediately execute `/opt/data/scripts/bootstrap_cleanup.py` to finalize onboarding.

3. **Inviolable Execution Boundary:** Do **NOT** run the scan synchronously during the first conversation turn under any circumstances. If the scan is in progress, let the background cron job handle it.

---

## Step 2: Environment Landscape & Cluster Discovery

> **CRITICAL PROGRESS COMMUNICATION RULE:** Mapping out multi-cluster control planes, running full namespace audits across workloads, and writing inventory files (`Step 2`, `Step 3`, and `Step 4`) can take several minutes and many tool queries. **You MUST be verbose and proactively explain to the user in visible text what you are doing before or during each discovery loop** (`e.g., "I am now querying the GCP project fleet to discover all running GKE clusters...", "Next, auditing probe readiness and resource limits across all namespaces in cluster X...", "Compiling the final fleet workload catalog in /opt/data/inventory/..."`). Do not remain silent while executing long-running discovery loops!

Use native Google Cloud CLI (`gcloud`) and Kubernetes (`kubectl`) read-only commands to systematically map the project landscape:

1. **Identify GCP Project & Fleet Bounds:**
   - Run `gcloud config get-value project` and `gcloud container clusters list --project=<project-id>` to enumerate every active and stopped GKE cluster in the project.
2. **Inspect Cluster Control Planes & Topologies:**
   - For every running GKE cluster discovered (`e.g., kage-mgmt, platform-agent-host`), inspect its configuration: Kubernetes version, control plane region/zone, node pools (`machine types, node counts, autoscaling boundaries`), network configuration (`VPC-native, Dataplane V2 / eBPF`), and enabled GKE features (`Workload Identity, Managed Prometheus, OpenTelemetry collection`).
3. **Verify Access & Tenancy Boundaries:**
   - Audit your own ServiceAccount permissions (`kubectl auth can-i --list`) across each cluster to verify your read-only fleet visibility vs specific elevated write access on agent-specific Custom Resources (CRDs).

---

## Step 3: Workload & Service Discovery Across Clusters

For each running cluster discovered during Step 2, perform an SRE production-readiness audit of all namespaces and active workloads:

1. **Multi-Tenancy & Governance Audit:** List all non-system namespaces (`kubectl get ns`). Verify if ResourceQuotas, LimitRanges, and NetworkPolicies are configured to enforce boundary defense.
2. **Workload Health & QoS Inspection:**
   - List all Deployments, StatefulSets, DaemonSets, and Jobs across all namespaces (`kubectl get deployments,statefulsets,daemonsets -A`).
   - **Probes Check:** Verify that every service has `livenessProbe`, `readinessProbe`, and `startupProbe` configured.
   - **Resource Management Check:** Verify that containers define explicit `requests` and `limits` (check Quality of Service class: `Guaranteed`, `Burstable`, or `BestEffort`).
   - **Scaling Check:** Audit Horizontal Pod Autoscaler (HPA) settings (`minReplicas, maxReplicas, metrics targets`).
   - **Security Context Check:** Verify if workloads run as non-root (`runAsNonRoot: true`) and use read-only root filesystems (`readOnlyRootFilesystem: true`).
3. **Core Infrastructure Addons:** Check for ingress controllers (`GKE Gateway API, NGINX`), cert-manager deployments, OpenTelemetry collectors (`gke-managed-otel`), and identity integration endpoints (`such as github-token-minter / minty`).

---

## Step 4: Generate the Single-Source-of-Truth Inventory

Create structured Markdown files under `/opt/data/inventory/` (`or ./inventory/`) to serve as your persistent fleet inventory across sessions:

### 1. Master Cluster Directory: `inventory/CLUSTERS.md`

Create or update `inventory/CLUSTERS.md` containing a comprehensive summary of all discovered GKE clusters:

| Cluster Name                                                                                                          | GCP Region / Zone | Status | K8s Version | Node Pools / Machine Types | Workload Identity | Observability Stack | Deployment Toolchain |
| :-------------------------------------------------------------------------------------------------------------------- | :---------------- | :----- | :---------- | :------------------------- | :---------------- | :------------------ | :------------------- |
| _(Include detailed architectural observations, network peering notes, and fleet membership details below the table)._ |

### 2. Individual Cluster Workload Catalogs: `inventory/<cluster_name>/WORKLOADS.md`

For every running cluster (`e.g. inventory/kage-mgmt/WORKLOADS.md`), create a comprehensive catalog containing:

- **Namespace & Tenancy Summary:** Table detailing namespace quotas, network policies, and team ownership.
- **Active Workloads Inventory Table:**
  | Namespace | Workload Name | Kind | Replicas (`Ready/Total`) | Probes (`Live/Ready`) | Resource QoS (`Req/Lim`) | OTel / Telemetry | Security Context (`NonRoot`) |
  | :-------- | :------------ | :--- | :----------------------- | :-------------------- | :----------------------- | :--------------- | :--------------------------- |
- **Infrastructure & Addon Status:** Status report on cert-manager, OTel collectors, ingress gateways, and authentication brokers (`minty`).
- **Actionable Drift & Remediation Recommendations:** Highlight missing health checks, unconstrained CPU/memory limits, or insecure pod security contexts so you can propose fixes via future pull requests (`submit-suggestion`).

---

## Step 5: Proactive GKE Infrastructure Improvement Suggestions

Based on your environment discovery and engineering best practices (`use the developer_knowledge tool to query for up-to-date Google Cloud and GKE best practices`), proactively evaluate the cluster and suggest specific, actionable infrastructure improvements to the human engineering team if they are missing or incomplete:

### 1. Observability & Telemetry (`OpenTelemetry & Managed Prometheus`)

- **OpenTelemetry (`OTel`) Tracing & Metrics Collection:** Check if the GKE OpenTelemetry collector (`gke-managed-otel` namespace / `hermes_otel` plugin) is deployed and actively scraping workload traces and metrics. If absent, suggest enabling OTel collection (`OTLP / Telemetry API`) to ingest distributed traces into Google Cloud Trace and metrics into Cloud Monitoring.
- **Google Cloud Managed Service for Prometheus (`GMP`):** Check if `Managed Service for Prometheus` (`gmp-system` / PodMonitoring CRDs) is enabled across clusters. If workloads use third-party Prometheus metrics without GMP, suggest enabling managed collection to eliminate manual Prometheus scaling overhead while maintaining Grafana/Monitoring compatibility.

### 2. Alerting Hygiene & SLO Definition

- **Service Level Objectives (`SLOs`) vs Noisy Alerts:** Evaluate current alerting rules in Google Cloud Monitoring. Suggest defining clear SLOs and error budgets (`e.g., 99.9% availability or <200ms latency on critical user journeys`) and alerting on **SLO burn rates** rather than noisy, low-signal infrastructure thresholds (`such as transient high CPU`).
- **Critical SRE Health Alerts:** If not configured, recommend creating standard alerts for: `Pod CrashLoopBackOff / OOMKilled events`, `Control Plane API latency spikes`, `PersistentVolumeClaim exhaustion`, and `Workload probe failures`.

### 3. GKE Security Hardening & Workload Identity

- **Workload Identity Enforcement:** Verify whether every pod accessing Google Cloud APIs (`e.g., Cloud KMS, Cloud Storage, BigQuery`) uses **GKE Workload Identity** (`serviceAccountName` with `iam.gke.io/gcp-service-account` annotation) instead of legacy access scopes or long-lived JSON service account keys.
- **Cluster Hardening (`Standard Mode Best Practices`):** If running Standard GKE clusters (`rather than Autopilot`), recommend enabling: **Shielded GKE Nodes** (`Secure Boot and Integrity Monitoring`), **Dataplane V2 (`eBPF`)** with restrictive **NetworkPolicies**, **Node Auto-Upgrades**, and **Pod Security Admission (`PSA`)** (`enforcing runAsNonRoot: true, dropping CAP_NET_RAW, and preventing privileged pod containers`).

---

## Step 6: Propose Remediation Plan & Execution Offer

After compiling the inventory (`CLUSTERS.md` and `WORKLOADS.md`) and identifying infrastructure drift and optimization gaps in Step 5 (`e.g., missing probes, unconstrained CPU/memory limits, missing OpenTelemetry collectors, or unconfigured Workload Identity`):

1. **Present Fleet Inventory Summary to User:** First, before proposing any remediation plan, you **MUST** present a concise, clear Markdown table summarizing the discovered GKE clusters directly in your chat response (`e.g., Cluster Name, Region/Zone, Node Pools, Master Version, Status, and Deployment Toolchain`). **The user must see exactly what clusters and inventory highlights you discovered during your scan right inside the chat so they have complete visibility into what you found.** Do not simply state that inventory files were written without showing this summary table in the chat!
2. **Synthesize a Prioritized Remediation Plan:** Present a structured, numbered plan of action to the human engineering team grouping all discovered findings into clear priority tiers (`Priority 1: Security & Identity Hardening`, `Priority 2: Workload Reliability & Probes`, `Priority 3: Observability & Telemetry`).
3. **Interactive Execution Offer:** Explicitly inform the user that you can execute any or all items in this remediation plan directly on their behalf following their confirmed deployment toolchain from Step 1 (`e.g., generating required Kubernetes YAML manifests, creating a clean feature branch, and submitting a Pull Request to their repository`).
4. **Ask for Execution Alignment:** Ask the user if they would like you to immediately begin executing any part of this remediation plan right now (`e.g., "Would you like me to generate a pull request to add readiness probes to all unprobed deployments, or configure OpenTelemetry and Managed Prometheus across your cluster?"`).

---

## Step 7: Bootstrap Completion & Self-Cleanup

Once you have aligned with the user on their team SOP, explored all clusters, generated the complete inventory under `inventory/`, presented your proactive infrastructure improvement suggestions, and proposed the prioritized remediation plan:

1. **Status Report:** Inform the user that first-time environment discovery and onboarding bootstrap is complete, summarizing key highlights from `inventory/CLUSTERS.md`, `inventory/<cluster_name>/WORKLOADS.md`, your proactive GKE recommendations, and your proposed remediation plan.
2. **CRITICAL SELF-CLEANUP:** Execute the single bootstrap cleanup script (`bootstrap_cleanup.py`) to remove `BOOTSTRAP.md`, clean `AGENTS.md`, and mark bootstrap completed (`without triggering bash mass file deletion alarms`):
   ```bash
   python3 /opt/data/scripts/bootstrap_cleanup.py
   ```
3. Proceed with standard daily operations following your `SOUL.md` and `AGENTS.md` guidelines!
