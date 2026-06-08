---
name: gcp-api-discovery
description: Audit and discover Google Cloud REST APIs across alpha, beta, and GA release stages. Use when analyzing GCP resource management schemas, mapping API resource trees (e.g. compute, container), or checking KCC custom resource compatibility.
---

# gcp-api-discovery - Google Cloud API Discovery & Schema Auditing

This skill equips the Platform Agent to autonomously discover, audit, and analyze Google Cloud REST API schemas across all release stages (Alpha, Beta, GA) for resource management tasks.

## When to Use

- **API Auditing & Schema Mapping**: Triggered when analyzing a GCP resource's REST specification, endpoints, or release stages (alpha/beta/GA).
- **KCC Compatibility Preparation**: Triggered when planning KCC manifest configurations and translating REST names to KCC singular Kind names.

## Execution Instructions

Follow these steps to query the Google API Discovery Service and analyze the resources:

### Step 1: List and Filter Available APIs

Query the Google API Discovery Directory using a secure HTTPS call or `curl` to find the exact service name and its available versions:

```bash
curl -s https://www.googleapis.com/discovery/v1/apis | jq '.items[] | select((.name | test("<SERVICE_QUERY>"; "i")) or (.title | test("<SERVICE_QUERY>"; "i")) or (.description | test("<SERVICE_QUERY>"; "i")))'
```
*(Replace `<SERVICE_QUERY>` with the service name, e.g. `compute`, `container`, `spanner`)*

**Analysis of List Output:**
* Look for the exact service prefix (e.g., `compute`, `container`, `pubsub`).
* Identify the available versions (`v1alpha1`, `v1beta1`, `v1`, etc.).
* Identify which version is marked as preferred (`"preferred": true` represents the preferred release version).

### Step 2: Fetch Resource Details & Methods

Fetch the full REST schema documentation for the target API ID (e.g., `container:v1` or `compute:beta`):

1. From the list output, extract the `discoveryRestUrl` (e.g. `https://container.googleapis.com/$discovery/rest?version=v1`).
2. Query that URL using `curl`:
   ```bash
   curl -s <discoveryRestUrl> > api_schema.json
   ```
3. Extract the resource tree hierarchy and their supported HTTP methods (methods like `list`, `get`, `create`/`insert`, `patch`/`update`, `delete`):
   ```bash
   jq '.resources | to_entries[] | {resource: .key, methods: (.value.methods | keys)}' api_schema.json
   ```

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

When designing or debugging Config Connector (KCC) manifests, map Google REST API resources to their Kubernetes counterparts:

1. **Service Prefix**: The first part of the API ID (e.g., `compute` in `compute:v1`) maps to the KCC API group (e.g. `compute.cnrm.cloud.google.com`).
2. **Resource Name**: Convert plural REST resource names to singular camel case for KCC resource Kind names:
   * REST resource `instances` $\rightarrow$ KCC Kind `ComputeInstance`
   * REST resource `nodePools` $\rightarrow$ KCC Kind `ContainerNodePool`
   * REST resource `subnetworks` $\rightarrow$ KCC Kind `ComputeSubnetwork`
3. **API Version Mapping**:
   * GA APIs typically map to KCC `v1beta1` or `v1alpha1` versions depending on KCC's maturity path.
