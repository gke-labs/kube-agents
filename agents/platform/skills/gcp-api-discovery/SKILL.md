---
name: gcp-api-discovery
description: Audit and discover Google Cloud REST APIs across alpha, beta, and GA release stages. Use when analyzing GCP resource management schemas, mapping API resource trees (e.g. compute, container), or checking KCC custom resource compatibility.
---

# Google Cloud API Discovery

This skill enables you to autonomously discover, audit, and analyze Google Cloud REST API schemas across all release stages (Alpha, Beta, GA) for resource management tasks.

## When to Use

- **API Auditing & Schema Mapping**: Triggered when analyzing a GCP resource's REST specification, endpoints, or release stages (alpha/beta/GA).
- **KCC Compatibility Preparation**: Triggered when planning KCC manifest configurations and translating REST names to KCC singular Kind names.

## Discovery Workflow

When you need to discover APIs, locate resource endpoints, or check schema differences between release versions, follow these steps:

### Step 1: List and Filter Available APIs
Use the bundled `discover_apis.cjs` script to search the Google API Discovery Directory.
```bash
node scripts/discover_apis.cjs --list --search <service_name>
```

**Example Output Analysis:**
* Look for the exact service prefix (e.g., `compute`, `container`, `spanner`, `pubsub`).
* Identify the available versions (`v1alpha1`, `v1beta1`, `v1`, etc.).
* Identify which version is marked as preferred (`preferred: true` is typically the latest GA version).

### Step 2: Fetch Resource Details & Methods
Once you have the API ID (e.g., `compute:beta` or `container:v1`), inspect the exposed resource paths and supported HTTP methods:
```bash
node scripts/discover_apis.cjs --details <api_id>
```

**What to verify in the details output:**
* **Resource Path**: The hierarchical structure (e.g. `projects.zones.clusters.nodePools`).
* **Supported Methods**: The verbs available on that resource (e.g. `list`, `get`, `create`/`insert`, `patch`/`update`, `delete`).

---

## Release Stage Heuristics

Understand the lifecycle and stability of the APIs you discover:

| Stage | Version Pattern | Stability / Support | Best For |
|---|---|---|---|
| **Alpha** | `*alpha*` (e.g. `v1alpha1`, `alpha`) | Early access, experimental. Breaking changes can occur without notice. | Testing cutting-edge GCP features before broad release. |
| **Beta** | `*beta*` (e.g. `v1beta1`, `beta`) | Feature-complete but still testing. Changes are minimized but possible. | Pre-production staging and early adoption. |
| **GA** (GA) | `vX` (e.g. `v1`, `v2`, `v3`) | Production ready, fully supported, and backward-compatible. | Production workloads. |

---

## Mapping GCP Resources to KCC (Kubernetes Config Connector)

When designing or debugging Config Connector (KCC) manifests, you need to map Google REST API resources to their Kubernetes counterparts:

1. **Service Prefix**: The first part of the API ID (e.g., `compute` in `compute:v1`) maps to the KCC API group (e.g. `compute.cnrm.cloud.google.com`).
2. **Resource Name**: Convert plural REST resource names to singular camel case for KCC resource Kind names:
   * REST resource `instances` $\rightarrow$ KCC Kind `ComputeInstance`
   * REST resource `nodePools` $\rightarrow$ KCC Kind `ContainerNodePool`
   * REST resource `subnetworks` $\rightarrow$ KCC Kind `ComputeSubnetwork`
3. **API Version Mapping**:
   * GA APIs typically map to KCC `v1beta1` or `v1alpha1` versions depending on KCC's maturity path.
   * Alpha/Beta GCP features require matching KCC alpha/beta features, which are often configured via resource spec options or might not be supported yet (see KCC unsupported lists).
