---
name: gke-cost-optimization
description: Guidance on optimizing costs for Google Kubernetes Engine (GKE) clusters.
---

# GKE Cost Optimization

This skill provides guidance on optimizing costs for Google Kubernetes Engine (GKE) clusters.

## Overview

Cost optimization in GKE involves tracking costs, setting limits to prevent waste, and rightsizing workloads to match actual usage.

## Workflows

### 1. Enable GKE Cost Allocation

GKE cost allocation allows you to see the cost of your GKE resources in Cloud Billing, broken down by namespace and cluster labels.

**Steps:**

1. Enable GKE cost allocation in the cluster settings.

**Command:**

```bash
gcloud container clusters update <cluster-name> \
    --enable-cost-allocation \
    --region <region>
```

2. View costs in the Cloud Billing reports by grouping by namespace or labels.

### 2. Configure Resource Quotas

Resource quotas restrict the total resource consumption in a namespace, preventing any single tenant from consuming all cluster resources.

**Example ResourceQuota Manifest:**

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: compute-quota
  namespace: my-namespace
spec:
  hard:
    requests.cpu: "4"
    requests.memory: 16Gi
    limits.cpu: "8"
    limits.memory: 32Gi
```

### 3. Rightsizing Strategies

Rightsizing involves adjusting the requested resources of your workloads to match their actual utilization.

- **Use VPA in Recommender Mode**: Let VPA observe usage and recommend CPU and memory requests.
- **Use MPA**: Reconcile HPA and VPA to avoid conflicts.
- **Review Cost Recommendations**: Check the Google Cloud Console for GKE cost optimization recommendations.

## Best Practices

1. **Enable Cost Allocation**: Always enable GKE cost allocation to understand where your money is going.
2. **Use Resource Quotas**: Enforce resource quotas in multi-tenant clusters to prevent cost runaways.
3. **Leverage Spot VMs**: Use Spot VMs for fault-tolerant, stateless workloads to save up to 91%.
4. **Automate Scaling**: Use Cluster Autoscaler and HPA/VPA to ensure you only pay for what you need.
