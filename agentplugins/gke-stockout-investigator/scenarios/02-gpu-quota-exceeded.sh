#!/usr/bin/env bash
#
# Rule F — Regional Quota Exceeded Violation.
#
# The region has capacity; the project is not allowed to use it. The workload asks for
# more GPUs in total than the project's regional quota permits, so no amount of
# retrying or zone-widening helps. The remediation is to make the request fit, or to
# raise the quota — and the agent has to say which.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SCENARIO_TITLE="Workload requests more GPUs than the regional quota allows"
SCENARIO_RULE="SKILL.md Rule F — Regional Quota Exceeded Violation"
SCENARIO_CONTROLLER="ml-training-job-gpu-2"
SCENARIO_MESSAGE_ID="scale.up.error.quota.exceeded"
SCENARIO_PODS=8

scenario_manifest() {
    cat <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ml-training-job-gpu-2
  labels:
    scenario: 02-gpu-quota-exceeded
spec:
  # 8 replicas x 4 GPUs = 32 L4s. Regional NVIDIA_L4_GPUS quota on a default project
  # is well below that, so the request is refused before capacity is ever consulted.
  replicas: 8
  selector:
    matchLabels: {app: ml-training-job-gpu-2}
  template:
    metadata:
      labels:
        app: ml-training-job-gpu-2
        scenario: 02-gpu-quota-exceeded
    spec:
      nodeSelector:
        cloud.google.com/gke-accelerator: nvidia-l4
      containers:
        - name: trainer
          image: registry.k8s.io/pause:3.9
          resources:
            requests:
              cpu: "12"
              memory: 48Gi
              nvidia.com/gpu: "4"
            limits:
              cpu: "12"
              memory: 48Gi
              nvidia.com/gpu: "4"
YAML
}

scenario_reasons() {
    cat <<JSON
"rejectedMigs": [
  {
    "mig": {"name": "gke-${CLUSTER_NAME}-l4-pool", "nodepool": "l4-pool", "zone": "${ZONE_A}"},
    "reason": {
      "messageId": "no.scale.up.mig.failing.predicate",
      "parameters": ["Insufficient nvidia.com/gpu"]
    }
  }
],
"napFailureReasons": [
  {
    "messageId": "no.scale.up.nap.pod.zonal.quota.exceeded",
    "parameters": ["NVIDIA_L4_GPUS", "${CLUSTER_LOCATION}", "32", "24"]
  }
]
JSON
}

scenario_notes() {
    cat <<'TXT'
The distinguishing evidence is the quota check. The agent must run
`gcloud compute regions describe <region> --format="json(quotas.filter(metric=NVIDIA_L4_GPUS))"`
and compare the limit against the 32 GPUs this deployment asks for.

Naming it a capacity stockout here would be wrong, and so would proposing extra zone
fallbacks: every zone in the region draws on the same regional quota. The expected
proposal caps total requested GPUs to fit the limit — for example dropping to 6
replicas — and says plainly that a quota increase is the alternative.
TXT
}

scenario_main "$@"
