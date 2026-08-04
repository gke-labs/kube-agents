#!/usr/bin/env bash
#
# Rule A — Lack of Zone/Family Fallbacks.
#
# The plainest failure in the set: a perfectly ordinary workload, sized reasonably,
# that cannot schedule only because its ComputeClass names one machine family in one
# zone. Nothing is scarce and no quota is hit — the spec is simply too narrow.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SCENARIO_TITLE="Ordinary web workload pinned to one family in one zone"
SCENARIO_RULE="SKILL.md Rule A — Lack of Zone/Family Fallbacks"
SCENARIO_CONTROLLER="frontend-web-app"
SCENARIO_PODS=4

scenario_manifest() {
    cat <<YAML
apiVersion: cloud.google.com/v1
kind: ComputeClass
metadata:
  name: frontend-pinned-class
  labels:
    scenario: 04-missing-zone-fallback
spec:
  priorities:
    - machineFamily: c2d
  whenUnsatisfiable: DoNotScaleUp
  nodePoolAutoCreation:
    enabled: true
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: frontend-web-app
  labels:
    scenario: 04-missing-zone-fallback
spec:
  replicas: 4
  selector:
    matchLabels: {app: frontend-web-app}
  template:
    metadata:
      labels:
        app: frontend-web-app
        scenario: 04-missing-zone-fallback
    spec:
      nodeSelector:
        cloud.google.com/compute-class: frontend-pinned-class
      # The zone constraint is what turns a survivable narrow ComputeClass into an
      # outage: one family AND one zone leaves a single candidate pool.
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - key: topology.kubernetes.io/zone
                    operator: In
                    values: ["${ZONE_A}"]
      containers:
        - name: web
          image: registry.k8s.io/pause:3.9
          resources:
            requests:
              cpu: "2"
              memory: 4Gi
            limits:
              cpu: "2"
              memory: 4Gi
YAML
}

scenario_reasons() {
    cat <<JSON
"rejectedMigs": [
  {
    "mig": {"name": "gke-${CLUSTER_NAME}-c2d-pool", "nodepool": "frontend-pinned", "zone": "${ZONE_A}"},
    "reason": {
      "messageId": "no.scale.up.mig.failing.predicate",
      "parameters": ["NodeAffinity"]
    }
  }
],
"napFailureReasons": [
  {
    "messageId": "no.scale.up.nap.pod.zonal.resource.pool.exhausted",
    "parameters": ["c2d", "${ZONE_A}"]
  }
]
JSON
}

scenario_notes() {
    cat <<'TXT'
This is the scenario a good diagnosis should resolve fastest and most cheaply. A 2 vCPU
web pod has no special hardware needs, so the quota and capacity checks should both
come back clean and point at the spec rather than the region.

The expected proposal widens the ComputeClass to several families and drops the
single-zone nodeAffinity, leaving the workload able to land anywhere in the region.
Any answer that requests a quota increase here has misread the evidence.
TXT
}

scenario_main "$@"
