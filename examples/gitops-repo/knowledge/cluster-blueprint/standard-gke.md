---
type: cluster-blueprint
title: Standard GKE cluster blueprint
tags: [gke, baseline, cluster]
resource: container.googleapis.com/Cluster
timestamp: 2026-07-23T00:00:00Z
---

# Standard GKE cluster blueprint

The baseline configuration a new cluster starts from (06 §5, `cluster-blueprint`). Agents read this
for context when proposing a `provisioning/` change; it is guidance, not applied state.

## Baseline

- **Release channel:** regular; **min K8s:** ≥1.30 (for `ValidatingAdmissionPolicy` GA — 07 Phase 0).
- **Node security:** Workload Identity enabled; Shielded Nodes; no legacy metadata endpoints.
- **Network:** private nodes; default-deny `NetworkPolicy` per tenant namespace (Phase 3).
- **Admission:** the agent-read-only `ValidatingAdmissionPolicy` from [../../policy/README.md](../../policy/README.md) applied.
- **Identity:** per-agent read-only KSA/RBAC/WI pre-created (never controller-minted — 08 §4).

## Related

- Tenancy isolation standard: _`tenancy-model` entry (added when Phase 3 lands)._
- Back to the [OKF index](../index.md).
