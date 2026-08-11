#!/usr/bin/env bash
#
# Rule E — Regional Scarcity (specialized hardware).
#
# An inference deployment pins itself to one GPU type in one zone. When that zone has
# no L4 capacity there is nowhere else for the pods to go, because nothing in the spec
# offers an alternative. This is the most common real stockout and the one the skill
# was written against first.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SCENARIO_TITLE="GPU capacity exhausted in the only zone the workload accepts"
SCENARIO_RULE="SKILL.md Rule E — Regional Scarcity (specialized hardware)"
SCENARIO_CONTROLLER="llm-inference-service"
SCENARIO_PODS=3

scenario_manifest() {
    cat <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: llm-inference-service
  labels:
    scenario: 01-gpu-regional-scarcity
spec:
  replicas: 3
  selector:
    matchLabels: {app: llm-inference-service}
  template:
    metadata:
      labels:
        app: llm-inference-service
        scenario: 01-gpu-regional-scarcity
    spec:
      # A single accelerator in a single zone: no fallback of any kind. The fix the
      # agent should propose is a ComputeClass that widens both axes.
      nodeSelector:
        cloud.google.com/gke-accelerator: nvidia-l4
        topology.kubernetes.io/zone: ${ZONE_A}
      containers:
        - name: vllm
          image: registry.k8s.io/pause:3.9
          resources:
            requests:
              cpu: "8"
              memory: 32Gi
              nvidia.com/gpu: "2"
            limits:
              cpu: "8"
              memory: 32Gi
              nvidia.com/gpu: "2"
YAML
}

scenario_reasons() {
    cat <<JSON
"rejectedMigs": [
  {
    "mig": {"name": "gke-${CLUSTER_NAME}-l4-pool", "nodepool": "l4-pool", "zone": "${ZONE_A}"},
    "reason": {
      "messageId": "no.scale.up.mig.failing.predicate",
      "parameters": ["NodeAffinity", "Insufficient nvidia.com/gpu"]
    }
  }
],
"napFailureReasons": [
  {
    "messageId": "no.scale.up.nap.pod.zonal.resource.pool.exhausted",
    "parameters": ["nvidia-l4", "${ZONE_A}"]
  }
]
JSON
}

scenario_notes() {
    cat <<'TXT'
The agent should find three Pending pods whose only candidate zone has no L4 capacity,
confirm against `gcloud compute regions describe` that this is scarcity rather than a
quota cap, and propose a ComputeClass with zone and accelerator fallbacks rather than
simply asking for more of the same GPU.

A correct diagnosis distinguishes this from scenario 02: the quota limit is not the
binding constraint here, availability is.
TXT
}

scenario_main "$@"
