---
name: gke-quota-and-limits-checker
description: Workflows for auditing namespace ResourceQuotas, LimitRanges, and unconstrained container CPU/memory resource limits across GKE.
---

# GKE Quota & Resource Limits Checker

This skill provides diagnostic workflows for verifying that containers specify explicit CPU and memory resource requests/limits, preventing node starvation and OOM evictions.

## Workflows

### 1. Audit Unconstrained Containers (Missing Resource Limits)

Identify pods running without explicit memory limits or CPU limits.

**Command:**

```bash
kubectl get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{.spec.containers[*].resources.limits}{"\n"}{end}'
```

### 2. Inspect Namespace ResourceQuota Exhaustion

Check active ResourceQuotas across namespaces to detect quota starvation.

**Command:**

```bash
kubectl get resourcequotas -A
```

### 3. Verify Default LimitRanges

Inspect LimitRanges in non-system namespaces to verify fallback resource boundaries.

**Command:**

```bash
kubectl get limitranges -A
```
