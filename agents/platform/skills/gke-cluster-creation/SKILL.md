---
name: gke-cluster-creation
description: >-
  Plans GKE cluster creation and provisioning as a pull request in the
  GitOps repository's format, and audits production readiness, using
  pre-defined templates (Autopilot, Standard Regional,
  GPU/AI Inference, AI Hypercompute). Use when creating GKE clusters,
  provisioning GKE environments, selecting cluster modes, or auditing GKE
  clusters. Don't use for application onboarding or deployment configuration
  (use gke-app-onboarding instead).
metadata:
  category: Containers
---

# GKE Cluster Creation

This reference guides creating Google Kubernetes Engine (GKE) clusters by
providing a set of best-practice templates and guiding through mode selection
and customization. The **golden path Autopilot** configuration is the default
for a new cluster whose workload states no requirement it cannot meet.

> **MCP Tools:** `list_clusters`, `get_cluster`,
> `list_operations`, `get_operation`

## Workflow

1.  **Discover context**: Use `list_clusters` to see existing clusters. Use
    `gcloud config get-value project` if project unknown.
2.  **Gather inputs**: `project_id`, `location` (region or zone),
    `cluster_name`, environment type. If missing essential details, ask the user
    before taking action.
3.  **Select mode & explain trade-offs**: When the request names a workload
    the cluster is for ("with Agent Substrate installed"), read that
    workload's install requirements before choosing anything (`SOUL.md` §3,
    item 4.1): the Developer Knowledge MCP for what Google documents, the
    project's own repository (**inspect-repository**) otherwise. A
    requirement it states (Standard mode, a machine series, beta APIs, a node
    label, a bucket or IAM binding it expects) overrides every template
    default below, and the pull request body lists each one with its source.
    Search-result snippets are not requirements. If the user hasn't specified
    a template or mode, present the available templates (e.g., Autopilot,
    Standard Regional, GPU Inference, AI Hypercompute) and explain key
    trade-offs (Cost vs. Availability, Autopilot vs. Standard node
    management).
4.  **Configure networking**: auto-create subnet (default) or bring-your-own.
5.  **Review golden path settings**: present the default configuration block
    (the template's settings, with step 3's workload requirements applied over
    them) as the configuration the pull request will carry.
6.  **Propose**: Write the configuration as a pull request in the format the
    GitOps repository's `provisioning/` path already holds: Config Connector
    YAML through the `gcp-config-connector` skill. A Terraform repository, an
    empty `provisioning/` path, or no usable repository is a stated stop
    (`SOUL.md` §3, items 6 and 7). Never create the cluster directly: the
    `gke` MCP server offers no create tool, and the broker refuses
    `gcloud container clusters create`.
7.  **Track**: Once the pull request merges and the reconciler starts the
    create, use `get_operation` to monitor its progress.
8.  **Verify**: Use `get_cluster` with `readMask="*"` to confirm the
    settings the pull request carried applied.

## Mode Selection

| Criteria           | Autopilot (Golden Path)   | Standard                  |
| ------------------ | ------------------------- | ------------------------- |
| Node management    | Google-managed            | Self-managed              |
| Pricing            | Pay per pod resource      | Pay per node (VM)         |
:                    : request                   :                           :
| Node customization | Via ComputeClasses        | Full control              |
| DaemonSets         | Allowed (with             | Full control              |
:                    : restrictions)             :                           :
| GPU/TPU            | Supported via             | Supported via node pools  |
:                    : ComputeClasses            :                           :
| Best for           | Most production workloads | Kernel tuning, custom OS, |
:                    :                           : privileged workloads      :

> **Rule**: Default to Autopilot unless the customer, or the workload the
> cluster is for, has a requirement Autopilot cannot satisfy. A workload's
> stated requirement (from step 3) wins over this default, and over every
> template value below: a template default applies only where the workload
> sets nothing.

## Best Practices

When guiding the user or generating configurations, adhere to these GKE best
practices:

### Security & Networking

1.  **Private Clusters**: Default to private clusters (`enablePrivateNodes:
    true`) with a private control plane and restricted public endpoints
    (`enable-master-authorized-networks`) to minimize attack surface.
2.  **VPC-Native Networking**: Use VPC-native clusters (`useIpAliases: true` /
    `--enable-ip-alias`) to enable alias IP ranges and pod-level firewall rules.
3.  **Workload Identity**: Prefer Workload Identity (`workloadPool:
    <PROJECT_ID>.svc.id.goog`) for securely granting GKE workloads access to
    Google Cloud services instead of static service account keys.
4.  **Shielded GKE Nodes**: Enable Shielded GKE Nodes
    (`--enable-shielded-nodes`, `--enable-secure-boot`) against rootkits and
    bootkits.
5.  **Least Privilege (RBAC)**: Institute strict Role-Based Access Control
    limits (`scoped-rbs-bindings`).

### Cost Optimization

1.  **Autoscaling**: Enable Cluster Autoscaler and Horizontal/Vertical Pod
    Autoscaler (`--enable-autoscaling`, `--enable-vertical-pod-autoscaling`) to
    adjust resources based on demand.
2.  **Right-Sizing & Spot VMs**: Choose appropriate machine types and node
    counts. Consider Spot VMs (`--spot`) for fault-tolerant, non-critical batch
    or inference workloads.

### High Availability & Reliability

1.  **Regional Clusters**: Use Regional Clusters for production environments to
    ensure control plane replication across multiple zones (`--region` instead
    of `--zone`). *Note: Standard regional creates nodes across 3 zones by
    default.*
2.  **Pod Disruption Budgets**: Recommend setting Pod Disruption Budgets for
    application stability during node maintenance.
3.  **Release Channels**: Subscribe to a release channel (`REGULAR` or `STABLE`)
    for automated, safer cluster upgrades.

## Templates

### 1. Golden Path Autopilot (Production)

This is the default. All settings match
`../gke-golden-path/assets/golden-path-autopilot.yaml`.

**Settings, as API fields (map them onto the repository's own format; never send this to a create call):**

```json
{
  "parent": "projects/<PROJECT_ID>/locations/<REGION>",
  "cluster": {
    "name": "<CLUSTER_NAME>",
    "autopilot": { "enabled": true },
    "privateClusterConfig": { "enablePrivateNodes": true },
    "masterAuthorizedNetworksConfig": {
      "privateEndpointEnforcementEnabled": true
    },
    "releaseChannel": { "channel": "REGULAR" },
    "secretManagerConfig": {
      "enabled": true,
      "rotationConfig": { "enabled": true, "rotationInterval": "120s" }
    },
    "rbacBindingConfig": {
      "enableInsecureBindingSystemAuthenticated": false,
      "enableInsecureBindingSystemUnauthenticated": false
    }
  }
}
```

### 2. Autopilot Dev/Test

Relaxes some golden path defaults for cost savings and easier access in
non-production.

**Settings, as API fields (map them onto the repository's own format; never send this to a create call):**

```json
{
  "parent": "projects/<PROJECT_ID>/locations/<REGION>",
  "cluster": {
    "name": "<CLUSTER_NAME>",
    "autopilot": { "enabled": true },
    "releaseChannel": { "channel": "RAPID" }
  }
}
```

> **Warning**: This does not apply golden path security hardening. Suitable for
> dev/test only.

### 3. Standard Regional (High Availability / Custom Requirements)

Best when Autopilot cannot be used (e.g., custom kernel tuning, specific node OS
requirements). Creates 3 nodes across zones by default.

**Settings, as API fields (map them onto the repository's own format; never send this to a create call):**

```json
{
  "parent": "projects/<PROJECT_ID>/locations/<REGION>",
  "cluster": {
    "name": "<CLUSTER_NAME>",
    "initialNodeCount": 3,
    "nodeConfig": {
      "machineType": "e2-standard-4",
      "diskType": "pd-balanced",
      "diskSizeGb": 100,
      "oauthScopes": ["https://www.googleapis.com/auth/cloud-platform"],
      "shieldedInstanceConfig": {
        "enableSecureBoot": true,
        "enableIntegrityMonitoring": true
      },
      "workloadMetadataConfig": {
        "mode": "GKE_METADATA"
      }
    },
    "privateClusterConfig": { "enablePrivateNodes": true },
    "releaseChannel": { "channel": "REGULAR" },
    "workloadIdentityConfig": {
      "workloadPool": "<PROJECT_ID>.svc.id.goog"
    }
  }
}
```

### 4. GPU Inference & AI Workloads (L4 / ComputeClass)

Best for: AI/ML Inference, small model serving. Can be provisioned via
Autopilot + ComputeClass or via Standard node pool with `g2-standard-4`
(`nvidia-l4`). *Note: Requires `g2-standard-4` quota.*

**Autopilot ComputeClass / GIQ approach:** the cluster is template 1. The manifests below go into the repository through a pull request; the agent never runs `kubectl apply`.

```bash
# ComputeClass manifest (see gke-compute-classes.md)
kubectl apply -f gpu-compute-class.yaml

# Or render the GIQ inference manifest (see gke-inference.md)
gcloud container ai profiles manifests create \
  --model=gemma-2-9b-it --model-server=vllm --accelerator-type=nvidia-l4 --quiet > inference.yaml
kubectl apply -f inference.yaml
```

**Standard node pool approach, as API fields (map them onto the repository's own format; never send this to a create call):**

```json
{
  "parent": "projects/<PROJECT_ID>/locations/<REGION>",
  "cluster": {
    "name": "<CLUSTER_NAME>",
    "initialNodeCount": 1,
    "nodeConfig": {
      "machineType": "g2-standard-4",
      "accelerators": [
        {
          "acceleratorCount": "1",
          "acceleratorType": "nvidia-l4"
        }
      ],
      "diskSizeGb": 100,
      "oauthScopes": ["https://www.googleapis.com/auth/cloud-platform"]
    }
  }
}
```

### 5. AI Hypercompute (A3 HighGPU / Large Model Serving)

Best for: Large-scale LLM / AI model training and hypercompute inference. *Note:
High hourly cost and strict quota requirements (`a3-highgpu-8g` /
`nvidia-h100-80gb-hbm3`).*

**Settings, as API fields (map them onto the repository's own format; never send this to a create call):**

```json
{
  "parent": "projects/<PROJECT_ID>/locations/<REGION>",
  "cluster": {
    "name": "<CLUSTER_NAME>",
    "initialNodeCount": 1,
    "nodeConfig": {
      "machineType": "a3-highgpu-8g",
      "accelerators": [
        {
          "acceleratorCount": "8",
          "acceleratorType": "nvidia-h100-80gb-hbm3"
        }
      ],
      "diskSizeGb": 200,
      "oauthScopes": ["https://www.googleapis.com/auth/cloud-platform"]
    }
  }
}
```

## Instructions

-   **ALWAYS** ask for `project_id` if not in context.
-   **ALWAYS** ask for `region` (or location).
-   **ALWAYS** ask for a unique `cluster_name`.
-   **DEFAULT** to golden path Autopilot unless customer specifies otherwise or
    has custom node/kernel/hypercompute requirements.
-   **ALWAYS WARN** when deviating to GKE Standard, highlighting that it
    deviates from the golden path and explaining the added
    operational/management overhead (manually managing node pools, upgrades, and
    autoscaling).
-   **EXPLAIN TRADE-OFFS** when presenting templates or mode choices to the user
    if they haven't specified one (e.g., Autopilot vs Standard, Cost vs
    Availability).
-   **PRESENT THE CONFIGURATION** in the pull request body, setting by
    setting, so the reviewer approves the shape along with the change.
-   **WARN** about Day-0 decisions (networking, private nodes) that are hard to
    change later.
-   **WARN** explicitly about cost and quota requirements when the user selects
    GPU (`g2-standard-4`, `a3-highgpu-8g`), TPU, or multi-region/regional
    clusters (`--region` defaults to 3 zones).
-   A cluster's name is the **short name** (e.g., `my-cluster`), not the full
    resource path
    (`projects/<PROJECT_ID>/locations/<REGION>/clusters/<CLUSTER_NAME>`).

<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->

## Required final step: provision the Cluster Agent profile

Creating a cluster is **not complete** until it has a Cluster Agent. A managed cluster and its
Cluster Agent profile are **created together** — never leave a newly created cluster without a
profile. Once the merged change has produced the cluster and it is reachable, create its
dedicated **Cluster Agent** profile (this is what makes the cluster delegable for runtime
debugging). Use the [cluster-agent-lifecycle](../cluster-agent-lifecycle/SKILL.md) skill:

```bash
python3 /opt/data/scripts/cluster_agent_profile.py create \
  --project "<project>" --cluster "<cluster>" --location "<location>"
```

The command is idempotent, so it is safe to re-run. This gives the new cluster an agent
immediately. (The `cluster-agent-reconcile` cron would also pick it up on its next run — it
manages every cluster in every project in scope, so no labeling is required.)

## Cluster Agent Profile Teardown

A managed cluster and its Cluster Agent profile are **deleted together**. When a cluster is
decommissioned/deleted, also remove its dedicated **Cluster Agent** profile (created at onboarding).
Use the [cluster-agent-lifecycle](../cluster-agent-lifecycle/SKILL.md) skill:

```bash
python3 /opt/data/scripts/cluster_agent_profile.py delete \
  --project "<project>" --cluster "<cluster>" --location "<location>"
```

Do not delete a Cluster Agent profile while its cluster still exists.

Deleting the profile here is the immediate, preferred path. As a backstop, the hourly
`cluster-agent-reconcile` job auto-prunes any profile whose cluster is definitively gone, so a
profile missed during teardown is cleaned up on the next reconcile cycle.

## Before recommending GPU/TPU or large-shape capacity

Before recommending capacity for a GPU/TPU or large-shape design, load the
[capacity-obtainability](../capacity-obtainability/SKILL.md) skill and run its diagnostics:
verify the regional quota for the exact accelerator metric (e.g. `NVIDIA_A100_GPUS`), then gather
capacity obtainability advice (`gcloud beta compute advice capacity`) for the requested machine
shape and count across the region's zones, for the Spot and Flex-Start provisioning models the
advice API accepts. That skill owns the rules for what to probe and how to report it; follow it
rather than restating them here.
