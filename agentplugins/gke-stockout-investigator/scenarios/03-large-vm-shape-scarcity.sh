#!/usr/bin/env bash
#
# Rule B — Large VM Shape Scarcity (>32 vCPUs).
#
# Very large machine shapes are scarce even in healthy regions, and a workload that
# demands one specific large shape has a much thinner supply than the same total
# capacity spread over smaller nodes. This is the scenario where "ask for less per
# node" is the right answer rather than "ask for more nodes".
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SCENARIO_TITLE="Analytics workload demands a single very large machine shape"
SCENARIO_RULE="SKILL.md Rule B — Large VM Shape Scarcity (>32 vCPUs)"
SCENARIO_CONTROLLER="data-warehouse-analytics"
SCENARIO_PODS=2

scenario_manifest() {
    cat <<YAML
apiVersion: cloud.google.com/v1
kind: ComputeClass
metadata:
  name: analytics-xl-class
  labels:
    scenario: 03-large-vm-shape-scarcity
spec:
  # One shape, no fallback. c3-standard-176 is the scarcest thing in the family, and
  # DoNotScaleUp means the autoscaler reports failure rather than quietly downgrading.
  priorities:
    - machineType: c3-standard-176
  whenUnsatisfiable: DoNotScaleUp
  nodePoolAutoCreation:
    enabled: true
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: data-warehouse-analytics
  labels:
    scenario: 03-large-vm-shape-scarcity
spec:
  replicas: 2
  selector:
    matchLabels: {app: data-warehouse-analytics}
  template:
    metadata:
      labels:
        app: data-warehouse-analytics
        scenario: 03-large-vm-shape-scarcity
    spec:
      nodeSelector:
        cloud.google.com/compute-class: analytics-xl-class
      containers:
        - name: worker
          image: registry.k8s.io/pause:3.9
          # Sized to Autopilot's per-pod ceiling (30 vCPU / 110Gi), not to the node.
          # A larger request is rejected at admission and the pod never reaches
          # Pending, so there would be no scale-up failure to investigate. Scarcity
          # here comes from the ComputeClass pinning c3-standard-176, not from the
          # pod being enormous.
          resources:
            requests:
              cpu: "28"
              memory: 100Gi
            limits:
              cpu: "28"
              memory: 100Gi
YAML
}

scenario_reasons() {
    cat <<JSON
"rejectedMigs": [
  {
    "mig": {"name": "gke-${CLUSTER_NAME}-c3-176", "nodepool": "analytics-xl", "zone": "${ZONE_A}"},
    "reason": {
      "messageId": "no.scale.up.mig.failing.predicate",
      "parameters": ["Insufficient cpu", "Insufficient memory"]
    }
  }
],
"napFailureReasons": [
  {
    "messageId": "no.scale.up.nap.pod.zonal.resource.pool.exhausted",
    "parameters": ["c3-standard-176", "${CLUSTER_LOCATION}"]
  }
]
JSON
}

scenario_notes() {
    cat <<'TXT'
The trap is treating this as plain scarcity and proposing more zones. Every zone in the
region can offer c3-standard-176 in principle; the point is that a 176 vCPU shape is
rare everywhere, and the ComputeClass names it with nothing behind it.

The expected proposal adds smaller shapes and adjacent families as lower priorities, so
scheduling succeeds on whatever is actually available. The pods ask for 28 vCPU, which
fits comfortably on a c3-standard-88 or even a -44 — a good diagnosis should notice
that the pinned shape is far larger than the workload needs and say so.
TXT
}

scenario_main "$@"
