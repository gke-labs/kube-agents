# First-Time Environment Discovery & Inventory Scan (`bootstrap-inventory-scan`)

**Purpose:** Executes rapid background GKE environment discovery and initial fleet status audit on agent boot (< 2 minutes), generating the unified `/opt/data/INVENTORY.md` report.

---

## Pre-Execution Check

1. **Verify Status:** Check directly via terminal command (`test -e /opt/data/INVENTORY.md`).
   - If `/opt/data/INVENTORY.md` is already built on disk, return strictly `[SILENT]` immediately and do nothing.
   - If `/opt/data/INVENTORY.md` is absent, proceed through the rapid discovery process below (limit total tool executions to 3–5 commands max to guarantee completion in under 2 minutes).

---

## Step 1: Rapid Fleet Topology Discovery (< 60 seconds)

Use fast, aggregated CLI commands to map the project landscape in 2–3 commands:

1. **GCP Project & Clusters:** Run `gcloud container clusters list` to list all GKE clusters, regions, versions, and status.
2. **Cluster Node & Pod Overview:** Run `kubectl get nodes` and `kubectl get pods -A --field-selector=status.phase!=Running` to quickly capture node capacity and identify any active failing/unhealthy workloads.

---

## Step 2: Strategic Summary Audit

Synthesize findings into high-level status metrics:
- Active cluster count, Kubernetes control plane versions, node pool counts.
- Summary of healthy vs degraded workloads across namespaces.
- Immediate high-priority SRE observations (e.g. failing pods, missing Workload Identity, unmonitored namespaces).

---

## Step 3: Write Master Inventory (`/opt/data/INVENTORY.md`)

Write `/opt/data/INVENTORY.md` directly. Keep it concise, complete, and presentation-ready:

```markdown
# GKE Environment Discovery Report

*Initial automated fleet scan performed on agent boot.*

### 🛸 GKE Fleet Overview
| Cluster Name | Region / Zone | Status | K8s Version | Node Count | Observability |
| :--- | :--- | :--- | :--- | :--- | :--- |
| <cluster_name> | <region> | Running | <version> | <nodes> | Enabled |

### ⚠️ Immediate Workload Status & Issues
- **Unhealthy / Failing Pods:** <list_failing_pods_or_none>
- **Active Namespaces:** `<namespaces_summary>`

### 🎯 Key Recommendations
1. **Security & Identity:** Ensure Workload Identity is enabled for Cloud API access.
2. **Workload Reliability:** Monitor failing pods and ensure liveness/readiness probes are configured.
3. **Observability:** Track OpenTelemetry collector metrics and Managed Service for Prometheus.
```

---

## Step 4: Post-Scan Completion & Silent Exit

Once `/opt/data/INVENTORY.md` is written, return strictly `[SILENT]` immediately. Delivery to chat is handled separately by the delivery daemon.
