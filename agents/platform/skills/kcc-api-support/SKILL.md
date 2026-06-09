---
name: kcc-api-support
description: Audit and discover Google Cloud resources supported by Kubernetes Config Connector (KCC). Use when validating if a GCP resource, API version, or field can be managed using KCC custom resource manifests.
---

# kcc-api-support - Config Connector Supported Resource Audit

This skill enables you to autonomously verify which GCP resource types and API groups are supported by the latest releases of Kubernetes Config Connector (KCC).

## When to Use

- **Config Connector Coverage Check**: Triggered when validating if a specific GCP resource type, field, or API group is supported for declarative management by KCC.

## Execution Instructions

Follow these steps to search the KCC Custom Resource Definitions (CRDs) for supported resources:

### Step 1: Query the CRD File Names

To check if KCC supports a service or resource, list the files under KCC's CRD configuration directory.

#### Option A: Using the GitHub Contents API (Remote/Default)
Query the official repository via the GitHub REST API:
```bash
curl -s https://api.github.com/repos/GoogleCloudPlatform/k8s-config-connector/contents/config/crds/resources | jq '.[] | .name' | grep -i "<QUERY>"
```
*(Replace `<QUERY>` with the service/resource, e.g., `redis` or `alloydb`)*

#### Option B: Using a Local Repository Checkout (If Available)
If the environment variable `KCC_REPO_PATH` is defined, look directly at the local filesystem:
```bash
ls $KCC_REPO_PATH/config/crds/resources | grep -i "<QUERY>"
```

### Step 2: Analyze CRD Name Mappings

CRD files follow the format `apiextensions.k8s.io_v1_customresourcedefinition_<plural>.<service>.cnrm.cloud.google.com.yaml`.

* If resources are returned (e.g. `redisinstances.redis` for `redis`), those resources are supported.
* The portion before the first dot (e.g. `redisinstances`) is the plural resource name.
* The portion after the first dot (e.g. `redis`) is the KCC group name.
* If no resources are found, it indicates KCC cannot currently manage that resource. Search the open/closed GitHub issues on `GoogleCloudPlatform/k8s-config-connector` for future milestones or planned support.

### Step 3: Audit CRD Field Coverage

To verify if a specific field is supported by KCC for a given custom resource, inspect the OpenAPI v3 schema properties inside the CRD YAML file:

1. Locate the spec properties in the YAML definition (under `spec.versions[].schema.openAPIV3Schema.properties.spec.properties`).
2. Search the file for the field name or nested property key:
   ```bash
   grep -n -C 5 -i "<FIELD_NAME>" <CRD_FILE_PATH>
   ```
3. Verify the nesting hierarchy of the field matching the GCP REST API path (e.g. confirming `dataCacheCount` is nested inside `ephemeralStorageLocalSsdConfig` under the `nodeConfig` property).

