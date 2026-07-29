---
name: gke-cluster-lifecycle
description: Guidance on managing the lifecycle and upgrades of Google Kubernetes Engine (GKE) clusters.
---

# GKE Cluster Lifecycle and Upgrades

This skill provides guidance on managing the lifecycle and upgrades of Google Kubernetes Engine (GKE) clusters.

## Overview

Managing cluster upgrades is crucial for security and access to new features. GKE provides automated upgrades, but they must be configured to minimize disruption.

## Workflows

### 1. Select Release Channels

Release channels allow you to choose the balance between stability and feature availability.

- **Rapid**: Newest features, less tested.
- **Regular** (Default): Good balance.
- **Stable**: Most tested, best for critical production workloads.

**Command to set release channel:**

```bash
gcloud container clusters update <cluster-name> \
    --release-channel=stable \
    --region <region>
```

### 2. Configure Surge Upgrades

Surge upgrades allow you to specify how many nodes can be created above the target size during an upgrade, minimizing disruption.

**Example configuration:**

```bash
gcloud container node-pools update <pool-name> \
    --cluster=<cluster-name> \
    --max-surge-upgrade=2 \
    --max-unavailable-upgrade=0 \
    --region <region>
```

Setting `max-unavailable-upgrade=0` ensures that no nodes are taken offline before new ones are ready.

### 3. Implement Blue/Green Node Pool Upgrades

For high-risk upgrades, you can create a new node pool (Green) with the new version, test it, and then migrate workloads from the old node pool (Blue).

**Steps:**

1. Create a new node pool with the new version and appropriate taints (using --node-taints).
2. Cordon and drain the old node pool gradually.
3. Delete the old node pool once empty.

## Best Practices

1. **Use Release Channels**: Always enroll production clusters in a release channel (preferably `Stable` or `Regular`).
2. **Configure Surge Upgrades**: Use `max-surge-upgrade` to ensure availability during upgrades.
3. **Use Maintenance Windows**: Configure maintenance windows to ensure upgrades only happen during off-peak hours (reliability is the Cluster Agent's `gke-reliability` domain).
4. **Test in Non-Prod**: Always test upgrades in a staging environment before applying them to production.

<!-- kube-agents: cluster-agent coupling (auto-injected by sync-upstream-skills.py) -->

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
