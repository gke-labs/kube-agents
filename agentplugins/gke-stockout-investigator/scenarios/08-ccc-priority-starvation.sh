#!/usr/bin/env bash
#
# Rule G — CCC Priority Starvation & Reset Loop.
#
# The ComputeClass lists many narrow, granular machine types in priority order. The
# autoscaler works down the list, fails on each, and restarts from the top rather than
# settling — so the workload never schedules even though a shape further down would
# have worked. The symptom is repetition, which a single alert does not show.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SCENARIO_TITLE="Over-granular priority list makes the autoscaler loop instead of settle"
SCENARIO_RULE="SKILL.md Rule G — CCC Priority Starvation & Reset Loop"
SCENARIO_CONTROLLER="transactional-db"
SCENARIO_PODS=5

scenario_manifest() {
    cat <<YAML
apiVersion: cloud.google.com/v1
kind: ComputeClass
metadata:
  name: db-granular-class
  labels:
    scenario: 08-ccc-priority-starvation
spec:
  # Eight exact machineType entries, each a narrow target, ordered by preference. Any
  # one of them being unavailable is unremarkable; the list being built this way is
  # the defect, because the autoscaler re-walks it from the top on every attempt.
  priorities:
    - machineType: c3-standard-4
    - machineType: c3-standard-8
    - machineType: c3-highmem-4
    - machineType: c3-highmem-8
    - machineType: c2d-standard-4
    - machineType: c2d-standard-8
    - machineType: c2d-highmem-4
    - machineType: c2d-highmem-8
  whenUnsatisfiable: DoNotScaleUp
  nodePoolAutoCreation:
    enabled: true
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: transactional-db
  labels:
    scenario: 08-ccc-priority-starvation
spec:
  replicas: 5
  selector:
    matchLabels: {app: transactional-db}
  template:
    metadata:
      labels:
        app: transactional-db
        scenario: 08-ccc-priority-starvation
    spec:
      nodeSelector:
        cloud.google.com/compute-class: db-granular-class
      # Larger than every shape in the list above, so each priority is tried and
      # rejected in turn and the walk restarts.
      containers:
        - name: db
          image: registry.k8s.io/pause:3.9
          resources:
            requests:
              cpu: "14"
              memory: 56Gi
            limits:
              cpu: "14"
              memory: 56Gi
YAML
}

scenario_reasons() {
    cat <<JSON
"rejectedMigs": [
  {
    "mig": {"name": "gke-${CLUSTER_NAME}-c3-std-4", "nodepool": "db-granular", "zone": "${ZONE_A}"},
    "reason": {
      "messageId": "no.scale.up.mig.failing.predicate",
      "parameters": ["Insufficient cpu"]
    }
  },
  {
    "mig": {"name": "gke-${CLUSTER_NAME}-c3-std-8", "nodepool": "db-granular", "zone": "${ZONE_A}"},
    "reason": {
      "messageId": "no.scale.up.mig.failing.predicate",
      "parameters": ["Insufficient cpu"]
    }
  },
  {
    "mig": {"name": "gke-${CLUSTER_NAME}-c2d-hm-8", "nodepool": "db-granular", "zone": "${ZONE_B}"},
    "reason": {
      "messageId": "no.scale.up.mig.failing.predicate",
      "parameters": ["Insufficient cpu"]
    }
  }
],
"napFailureReasons": [
  {
    "messageId": "no.scale.up.nap.pod.zonal.resource.pool.exhausted",
    "parameters": ["c3", "${ZONE_A}"]
  }
]
JSON
}

scenario_notes() {
    cat <<'TXT'
Publish this one two or three times a few minutes apart, with --keep-dedup on the
repeats, to make the loop visible. A single alert looks like ordinary scarcity; the
repetition across the same priority list is the actual signal, and it is what
distinguishes Rule G from Rule B.

The expected proposal collapses the eight exact machineType entries into a couple of
machineFamily entries, which lets the autoscaler pick a shape that fits instead of
rejecting each candidate in turn. Note that the 14 vCPU request exceeds every listed
shape, so the diagnosis should also say the list could never have satisfied this pod.
TXT
}

scenario_main "$@"
