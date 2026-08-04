#!/usr/bin/env bash
#
# Rule D — Missing On-Demand Floor.
#
# Every priority in the ComputeClass is Spot. That is cheap right up until Spot
# capacity disappears, at which point the workload has no on-demand tier to fall back
# to and simply stops scheduling. The failure looks like a stockout; the cause is a
# cost decision with no floor under it.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SCENARIO_TITLE="Spot-only ComputeClass with no on-demand tier to fall back to"
SCENARIO_RULE="SKILL.md Rule D — Missing On-Demand Floor"
SCENARIO_CONTROLLER="batch-processing-job"
SCENARIO_PODS=6

scenario_manifest() {
    cat <<YAML
apiVersion: cloud.google.com/v1
kind: ComputeClass
metadata:
  name: batch-spot-only-class
  labels:
    scenario: 05-missing-ondemand-floor
spec:
  # Three priorities, all Spot. When Spot is unavailable across the region there is
  # no fourth option, so the class cannot be satisfied at any price.
  priorities:
    - machineType: c3-standard-88
      spot: true
    - machineFamily: c2
      spot: true
    - machineFamily: e2
      spot: true
  whenUnsatisfiable: DoNotScaleUp
  nodePoolAutoCreation:
    enabled: true
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: batch-processing-job
  labels:
    scenario: 05-missing-ondemand-floor
spec:
  replicas: 6
  selector:
    matchLabels: {app: batch-processing-job}
  template:
    metadata:
      labels:
        app: batch-processing-job
        scenario: 05-missing-ondemand-floor
    spec:
      nodeSelector:
        cloud.google.com/compute-class: batch-spot-only-class
      tolerations:
        - key: cloud.google.com/gke-spot
          operator: Equal
          value: "true"
          effect: NoSchedule
      containers:
        - name: batch
          image: registry.k8s.io/pause:3.9
          resources:
            requests:
              cpu: "16"
              memory: 64Gi
            limits:
              cpu: "16"
              memory: 64Gi
YAML
}

scenario_reasons() {
    cat <<JSON
"rejectedMigs": [
  {
    "mig": {"name": "gke-${CLUSTER_NAME}-spot-c3", "nodepool": "batch-spot-only", "zone": "${ZONE_A}"},
    "reason": {
      "messageId": "no.scale.up.mig.failing.predicate",
      "parameters": ["Insufficient cpu"]
    }
  },
  {
    "mig": {"name": "gke-${CLUSTER_NAME}-spot-e2", "nodepool": "batch-spot-only", "zone": "${ZONE_B}"},
    "reason": {
      "messageId": "no.scale.up.mig.failing.predicate",
      "parameters": ["Insufficient cpu"]
    }
  }
],
"napFailureReasons": [
  {
    "messageId": "no.scale.up.nap.pod.zonal.resource.pool.exhausted",
    "parameters": ["spot", "${CLUSTER_LOCATION}"]
  }
]
JSON
}

scenario_notes() {
    cat <<'TXT'
This is the scenario that should exercise the Spot capacity advisor. The skill mandates
`gcloud beta compute advice capacity` and `capacity-history`, and this is the case where
that output changes the recommendation rather than decorating it: the advisor says
which shapes and zones currently have Spot capacity, and whether the shortage looks
transient or sustained.

The expected proposal appends a non-Spot priority as the last tier so the workload
degrades to paying full price instead of not running, and keeps the Spot tiers ahead of
it so the cost saving survives. Removing Spot altogether would be an overcorrection
worth flagging.
TXT
}

scenario_main "$@"
