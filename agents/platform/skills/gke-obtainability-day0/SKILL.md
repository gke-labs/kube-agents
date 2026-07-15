---
name: gke-obtainability-day0
description: Guidance and design workflows for proactively architecting GKE workloads and compute configurations for capacity obtainability before deployment (Day 0). Use when designing ComputeClasses, node selection strategies, Spot fallback rules, and capacity reservations to prevent stockouts.
---

# GKE Obtainability: Day 0 (Proactive Architecture & Design)

This skill guides you through designing high-obtainability Google Kubernetes Engine (GKE) workloads and infrastructure specifications before deployment. Proactive Day 0 architecture ensures workloads can reliably acquire physical compute resources (CPUs, Memory, GPUs, TPUs, and Spot VMs) across Google Cloud zones without encountering `FailedScheduling` bottlenecks or `ZONE_RESOURCE_POOL_EXHAUSTED` stockouts.

## Core Design Principles

### 1. Multi-Zonal Placement & Targeting

Never restrict workloads or `ComputeClass` specifications to a single zone unless strict data residency or physical hardware constraints mandate it. Single-zone placement creates a single point of failure for capacity obtainability.

- **Rule:** Always list multiple zones within the target region when defining `location.zones` in a `ComputeClass`.
- **Example:**
  ```yaml
  location:
    zones:
      - us-central1-a
      - us-central1-b
      - us-central1-c
  ```

### 2. Multi-Tiered ComputeClass Priorities (Spot with On-Demand Fallback)

When utilizing Spot VMs (`spot: true`) for cost optimization, always define a secondary priority level targeting standard On-Demand VMs (`spot: false`) within the same or alternative machine families.

- **Rule:** Priority 1 requests Spot capacity; Priority 2 provides the On-Demand safety net.
- **Example:**
  ```yaml
  apiVersion: cloud.google.com/v1
  kind: ComputeClass
  metadata:
    name: spot-obtainable-pool
  spec:
    nodePoolAutoCreation:
      enabled: true
    priorities:
      # Priority 1: High-priority Spot VM request
      - machineFamily: n4
        spot: true
      # Priority 2: Automatic On-Demand fallback if Spot is stocked out
      - machineFamily: n4
        spot: false
  ```

### 3. Active Migration for Cost Recovery

When workloads fall back to On-Demand VMs during a Spot stockout, enable active migration so GKE automatically returns the pods to Spot instances as soon as capacity recovers.

- **Rule:** Set `spec.activeMigration.optimizeRulePriority: true` in your `ComputeClass`.
- **Example:**
  ```yaml
  spec:
    activeMigration:
      optimizeRulePriority: true
  ```

### 4. Guaranteed Capacity with Reservation Affinity

For mission-critical workloads or specialized hardware (such as `nvidia-l4`, `nvidia-h100`, or Cloud TPUs), design manifests to explicitly consume pre-purchased Google Cloud Capacity Reservations or Future Reservations (DASP).

- **Rule:** Configure `reservationAffinity` in the `ComputeClass` priorities or node configuration.
- **Example (`SPECIFIC_RESERVATION`):**
  ```yaml
  priorities:
    - machineFamily: g2
      gpu:
        type: nvidia-l4
        count: 1
      reservationAffinity:
        consumeReservationType: SPECIFIC_RESERVATION
        key: compute.googleapis.com/reservation-name
        values:
          - projects/my-project/reservations/prod-l4-gpu-pool
  ```

### 5. Anti-Rigidity & Scheduling Boundaries

Avoid hardcoded pod `nodeSelector` definitions that lock workloads to specific hostnames or rigid zonal labels (`kubernetes.io/hostname` or a single `topology.kubernetes.io/zone`).

- **Rule:** Use `ComputeClass` tolerations and selectors (`cloud.google.com/compute-class: "<class-name>"`) alongside `HorizontalPodAutoscaler` (HPA) specifications for all deployments exceeding 3 replicas.

---

## Complete Day 0 Reference Template

Use this copy-pasteable template when generating high-obtainability `ComputeClass` manifests:

```yaml
apiVersion: cloud.google.com/v1
kind: ComputeClass
metadata:
  name: obtainable-ai-workload
  namespace: default
spec:
  nodePoolAutoCreation:
    enabled: true
  activeMigration:
    optimizeRulePriority: true
  priorities:
    # Priority 1: Target primary Spot GPU instances across 3 zones
    - machineFamily: g2
      spot: true
      gpu:
        type: nvidia-l4
        count: 1
      location:
        zones:
          - us-central1-a
          - us-central1-b
          - us-central1-c
    # Priority 2: Fallback to On-Demand GPU instances across 3 zones
    - machineFamily: g2
      spot: false
      gpu:
        type: nvidia-l4
        count: 1
      location:
        zones:
          - us-central1-a
          - us-central1-b
          - us-central1-c
```

## Checklist Before Day 1 Hand-Off

1. [ ] Does the `ComputeClass` list at least 2 or 3 availability zones?
2. [ ] Is every `spot: true` rule paired with a `spot: false` fallback (or an alternative machine family)?
3. [ ] Is `activeMigration.optimizeRulePriority: true` enabled?
4. [ ] If using specialized GPUs/TPUs, is `reservationAffinity` configured to consume dedicated reservations?
5. [ ] Do all target Deployments have an associated `HorizontalPodAutoscaler` (HPA)?
