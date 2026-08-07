#!/usr/bin/env bash
#
# Signal quality — duplicate and stale alerts.
#
# The autoscaler re-logs the same unschedulable pod group every few minutes for as
# long as it stays unschedulable, so one incident produces a stream of identical
# alerts. Two things have to hold: the adapter's dedup registry drops the repeats, and
# when one does get through, the skill's own duplicate-PR check stops the agent from
# proposing the same remediation twice.
#
# This scenario deliberately does NOT clear the dedup registry between publishes.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SCENARIO_TITLE="The same alert three times — dedup and duplicate-PR suppression"
SCENARIO_RULE="Signal quality — repeated alerts for one ongoing incident"
SCENARIO_CONTROLLER="llm-inference-service"
SCENARIO_PODS=3

scenario_manifest() {
    cat <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: llm-inference-service
  labels:
    scenario: 09-duplicate-signal
spec:
  replicas: 3
  selector:
    matchLabels: {app: llm-inference-service}
  template:
    metadata:
      labels:
        app: llm-inference-service
        scenario: 09-duplicate-signal
    spec:
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
Expected: exactly one investigation, not three.

The second and third publishes should be dropped by the adapter before they ever reach
the agent, logged as a dedup hit on cluster + namespace + controller. Confirm with:

  kubectl logs -n kubeagents-system deployment/platform-agent-gateway \
    -c platform-agent --tail=200 | grep -i dedup

The dedup window is 24h, so this workload stays suppressed until the registry is
cleared. To test the second layer — the skill's duplicate-PR check — clear the registry
by hand and publish again while the first PR is still open; the agent should find the
existing PR and decline to open another rather than diagnosing from scratch.

  kubectl exec -n kubeagents-system deploy/platform-agent-gateway -c platform-agent \
    -- rm -f /opt/data/pubsub_registry.json
TXT
}

# Publish three times rather than once. Everything else — preflight, the workload, the
# watch — is the standard flow, so run it and then add the repeats.
#
# The registry is cleared as usual first, so publish 1 gets through and registers the
# key; publishes 2 and 3 then collide with the entry publish 1 just wrote. Suppressing
# the clear here instead would risk a leftover entry swallowing publish 1 as well,
# which looks identical to working dedup but proves nothing.
scenario_main "$@"

if [ "${DO_CLEANUP:-0}" -eq 0 ] && [ "${DRY_RUN:-0}" -eq 0 ]; then
    step "Publishing the same alert twice more"
    info "these should be dropped by the adapter, not investigated"
    for n in 2 3; do
        sleep 15
        RUN_ID="$(date +%s)"   # a fresh insertId; the dedup key is unchanged
        publish_alert
        dim "repeat ${n} sent"
    done

    step "Checking the gateway log for dedup decisions"
    kmgmt logs -n "$AGENT_NAMESPACE" deployment/platform-agent-gateway \
        -c platform-agent --tail=200 2>/dev/null \
        | grep -iE "dedup|duplicate" | tail -6 | sed 's/^/    /' \
        || warn "no dedup lines found — check whether the repeats were investigated"
fi
