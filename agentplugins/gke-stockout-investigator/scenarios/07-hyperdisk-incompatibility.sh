#!/usr/bin/env bash
#
# Rule H — Hyperdisk Incompatibility with Older Generation Machines.
#
# The workload asks for Hyperdisk and the ComputeClass offers only machine families
# that predate it. Unlike scenario 06 this never partially works: no priority in the
# class can attach the volume, so the workload has never scheduled at all.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SCENARIO_TITLE="Hyperdisk volume on a ComputeClass offering only pre-Hyperdisk families"
SCENARIO_RULE="SKILL.md Rule H — Hyperdisk Incompatibility with Older Generations"
SCENARIO_CONTROLLER="primary-database"
SCENARIO_KIND="StatefulSet"
SCENARIO_PODS=1

scenario_manifest() {
    cat <<YAML
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: scenario-hyperdisk-extreme
  labels:
    scenario: 07-hyperdisk-incompatibility
provisioner: pd.csi.storage.gke.io
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
parameters:
  type: hyperdisk-extreme
  provisioned-throughput-on-create: "250Mi"
---
apiVersion: cloud.google.com/v1
kind: ComputeClass
metadata:
  name: db-legacy-class
  labels:
    scenario: 07-hyperdisk-incompatibility
spec:
  # Neither family supports Hyperdisk. There is no valid placement anywhere in the
  # class, which is what separates this from the partial failure in scenario 06.
  priorities:
    - machineFamily: n1
    - machineFamily: e2
  whenUnsatisfiable: DoNotScaleUp
  nodePoolAutoCreation:
    enabled: true
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: primary-database
  labels:
    scenario: 07-hyperdisk-incompatibility
spec:
  serviceName: primary-database
  replicas: 1
  selector:
    matchLabels: {app: primary-database}
  template:
    metadata:
      labels:
        app: primary-database
        scenario: 07-hyperdisk-incompatibility
    spec:
      nodeSelector:
        cloud.google.com/compute-class: db-legacy-class
      containers:
        - name: db
          image: registry.k8s.io/pause:3.9
          resources:
            requests:
              cpu: "8"
              memory: 32Gi
            limits:
              cpu: "8"
              memory: 32Gi
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
  volumeClaimTemplates:
    - metadata:
        name: data
        labels:
          scenario: 07-hyperdisk-incompatibility
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: scenario-hyperdisk-extreme
        resources:
          requests:
            storage: 500Gi
YAML
}

scenario_reasons() {
    cat <<JSON
"rejectedMigs": [
  {
    "mig": {"name": "gke-${CLUSTER_NAME}-n1-legacy", "nodepool": "db-legacy", "zone": "${ZONE_A}"},
    "reason": {
      "messageId": "no.scale.up.mig.failing.predicate",
      "parameters": ["VolumeBinding", "hyperdisk-extreme not supported on n1"]
    }
  },
  {
    "mig": {"name": "gke-${CLUSTER_NAME}-e2-legacy", "nodepool": "db-legacy", "zone": "${ZONE_B}"},
    "reason": {
      "messageId": "no.scale.up.mig.failing.predicate",
      "parameters": ["VolumeBinding", "hyperdisk-extreme not supported on e2"]
    }
  }
],
"napFailureReasons": [
  {
    "messageId": "no.scale.up.nap.pod.zonal.resource.pool.exhausted",
    "parameters": ["n1", "${CLUSTER_LOCATION}"]
  }
]
JSON
}

scenario_notes() {
    cat <<'TXT'
There is no capacity problem here at all, and both a quota check and a capacity check
will come back clean. The evidence is entirely in the predicate failure: every MIG is
rejected on VolumeBinding, never on Insufficient cpu or memory.

The expected proposal replaces the families with generations that support Hyperdisk —
c3, m3, or n4 — or moves the StorageClass to pd-balanced if the older families are a
hard requirement. A proposal that changes replica counts or adds zones has diagnosed
the wrong thing entirely, and this scenario is the sharpest test of that distinction.
TXT
}

scenario_main "$@"
