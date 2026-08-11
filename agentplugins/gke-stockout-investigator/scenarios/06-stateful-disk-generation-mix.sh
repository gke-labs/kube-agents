#!/usr/bin/env bash
#
# Rule C — Stateful Disk Generation Mix.
#
# A StatefulSet's volume was provisioned against one machine generation, and the
# ComputeClass now offers a mixture of generations. Replicas land on a generation that
# cannot attach the existing disk, so the set wedges partway through — the kind of
# failure that only shows up on rescheduling, long after the config was written.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SCENARIO_TITLE="StatefulSet volume incompatible with the offered machine generations"
SCENARIO_RULE="SKILL.md Rule C — Stateful Disk Generation Mix"
SCENARIO_CONTROLLER="transactional-db-primary"
SCENARIO_KIND="StatefulSet"
SCENARIO_PODS=3

scenario_manifest() {
    cat <<YAML
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: scenario-hyperdisk-balanced
  labels:
    scenario: 06-stateful-disk-generation-mix
provisioner: pd.csi.storage.gke.io
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
parameters:
  type: hyperdisk-balanced
---
apiVersion: cloud.google.com/v1
kind: ComputeClass
metadata:
  name: db-mixed-generation-class
  labels:
    scenario: 06-stateful-disk-generation-mix
spec:
  # n1 and n2 predate Hyperdisk; c3 supports it. A StatefulSet spread across this
  # class attaches on some nodes and fails on others, which is why the mix itself is
  # the defect rather than any single entry.
  priorities:
    - machineFamily: n1
    - machineFamily: n2
    - machineFamily: c3
  whenUnsatisfiable: DoNotScaleUp
  nodePoolAutoCreation:
    enabled: true
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: transactional-db-primary
  labels:
    scenario: 06-stateful-disk-generation-mix
spec:
  serviceName: transactional-db-primary
  replicas: 3
  selector:
    matchLabels: {app: transactional-db-primary}
  template:
    metadata:
      labels:
        app: transactional-db-primary
        scenario: 06-stateful-disk-generation-mix
    spec:
      nodeSelector:
        cloud.google.com/compute-class: db-mixed-generation-class
      containers:
        - name: db
          image: registry.k8s.io/pause:3.9
          resources:
            requests:
              cpu: "4"
              memory: 16Gi
            limits:
              cpu: "4"
              memory: 16Gi
          volumeMounts:
            - name: data
              mountPath: /var/lib/data
  volumeClaimTemplates:
    - metadata:
        name: data
        labels:
          scenario: 06-stateful-disk-generation-mix
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: scenario-hyperdisk-balanced
        resources:
          requests:
            storage: 100Gi
YAML
}

scenario_reasons() {
    cat <<JSON
"rejectedMigs": [
  {
    "mig": {"name": "gke-${CLUSTER_NAME}-n1-pool", "nodepool": "db-mixed-generation", "zone": "${ZONE_A}"},
    "reason": {
      "messageId": "no.scale.up.mig.failing.predicate",
      "parameters": ["VolumeBinding", "node does not support hyperdisk-balanced"]
    }
  }
],
"napFailureReasons": [
  {
    "messageId": "no.scale.up.nap.pod.zonal.resource.pool.exhausted",
    "parameters": ["n1", "${ZONE_A}"]
  }
]
JSON
}

scenario_notes() {
    cat <<'TXT'
The tell is that some replicas run and others do not, with a VolumeBinding predicate
failure rather than an Insufficient-resources one. A diagnosis that stops at "not
enough capacity" has missed it — the region has plenty of n1.

The expected proposal removes the generations that cannot attach the volume type, so
every priority in the class can serve every replica. Scenario 07 is the same family of
mistake stated the other way round: there, the disk is the newer half of the pair.
TXT
}

scenario_main "$@"
