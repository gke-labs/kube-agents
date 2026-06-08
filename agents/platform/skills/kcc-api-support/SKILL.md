---
name: kcc-api-support
description: Audit and discover Google Cloud resources supported by Kubernetes Config Connector (KCC). Use when validating if a GCP resource, API version, or field can be managed using KCC custom resource manifests.
---

# KCC Supported API Discovery

This skill enables you to autonomously verify which GCP resource types and API groups are supported by the latest releases of Kubernetes Config Connector (KCC).

## When to Use

- **Config Connector Coverage Check**: Triggered when validating if a specific GCP resource type, field, or API group is supported for declarative management by KCC.

## Discovery Workflow

When you need to verify if KCC can manage a specific GCP resource or service:

### Step 1: Search for KCC Support
Use the bundled `discover_kcc.cjs` script to search the KCC Custom Resource Definitions (CRDs).
```bash
node scripts/discover_kcc.cjs --search <gcp_service_or_resource>
```

**Example Output Analysis:**
* If resources are returned (e.g., `redisclusters`, `redisinstances` for the query `redis`), those resources are supported by KCC.
* If no resources are found, it indicates KCC cannot currently manage that specific resource. You should verify if it's undocumented or search open/closed GitHub issues on `GoogleCloudPlatform/k8s-config-connector` for future milestones or planned support.

### Step 2: Source of Truth
* **Local Repo**: The script automatically checks for a local checkout of the KCC source code specified by the `KCC_REPO_PATH` environment variable.
* **GitHub Repository Fallback**: If the environment variable is not defined, it fetches the file listing directly from the official repository `GoogleCloudPlatform/k8s-config-connector` on GitHub (under the `config/crds/resources/` directory) to query the latest release capabilities.
