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

# The shape and zone are discovered, not hardcoded. A shape is only scarce while it is
# scarce: c3-standard-176 was pinned here originally, and on a healthy day the autoscaler
# simply provisions one — the workload schedules, no scale-up failure is emitted, and the
# run waits for an alert that will never come. What stays true is that large shapes are
# not offered in every zone, so this asks the API which large shape is missing from which
# zone of the target region and pins that pair. Override with SCENARIO_MACHINE and
# SCENARIO_ZONE to force a specific one.
SCENARIO_MACHINE="${SCENARIO_MACHINE:-}"
SCENARIO_ZONE="${SCENARIO_ZONE:-}"

_resolve_scarce_shape() {
    [ -n "$SCENARIO_MACHINE" ] && [ -n "$SCENARIO_ZONE" ] && return 0

    local pair
    pair="$(gcloud compute machine-types list --project="$PROJECT_ID" \
                --filter="zone ~ ^${CLUSTER_LOCATION}- AND guestCpus>=96" \
                --format='csv[no-heading](name,zone,guestCpus)' 2>/dev/null |
        python3 -c '
import collections, csv, sys
# Accelerator and TPU shapes belong to other scenarios, and -lssd variants need local SSD
# wiring Autopilot will not infer, so this looks only at large general-purpose families.
FAMILIES = ("c3-", "c3d-", "c4-", "c4a-", "m3-", "m4-", "n2-", "n4-")
zones, offered, cpus = set(), collections.defaultdict(set), {}
for row in csv.reader(sys.stdin):
    if len(row) != 3:
        continue
    name, zone, cpu = row
    zones.add(zone)
    if not name.startswith(FAMILIES) or name.endswith("-lssd"):
        continue
    offered[name].add(zone)
    cpus[name] = int(cpu)
best = None
for name, has in offered.items():
    missing = sorted(zones - has)
    if missing and (best is None or cpus[name] > best[0]):
        best = (cpus[name], name, missing[0])
if best:
    print(best[1], best[2])
' || true)"

    if [ -n "$pair" ]; then
        SCENARIO_MACHINE="${pair%% *}"
        SCENARIO_ZONE="${pair##* }"
    else
        # No gap found, or the query failed: fall back to the original pinning. The run
        # may then schedule successfully, which the watch will report as no investigation.
        SCENARIO_MACHINE="c3-standard-176"
        SCENARIO_ZONE="$ZONE_A"
        warn "could not find a large shape missing from a zone; falling back to ${SCENARIO_MACHINE} in ${SCENARIO_ZONE}" >&2
    fi
}

scenario_manifest() {
    _resolve_scarce_shape
    # stderr, not stdout: this function's stdout is piped straight into kubectl.
    info "pinning ${SCENARIO_MACHINE} in ${SCENARIO_ZONE} (that shape is not offered there)" >&2
    cat <<YAML
apiVersion: cloud.google.com/v1
kind: ComputeClass
metadata:
  name: analytics-xl-class
  labels:
    scenario: 03-large-vm-shape-scarcity
spec:
  # One shape, no fallback, in a zone that does not offer it. DoNotScaleUp means the
  # autoscaler reports failure rather than quietly downgrading to something available.
  priorities:
    - machineType: ${SCENARIO_MACHINE}
      spot: false
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
        topology.kubernetes.io/zone: ${SCENARIO_ZONE}
      containers:
        - name: worker
          image: registry.k8s.io/pause:3.9
          # Sized to Autopilot's per-pod ceiling (30 vCPU / 110Gi), not to the node.
          # A larger request is rejected at admission and the pod never reaches
          # Pending, so there would be no scale-up failure to investigate. Scarcity
          # here comes from the ComputeClass pinning one large shape in a zone without it, not
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
    "mig": {"name": "gke-${CLUSTER_NAME}-xl", "nodepool": "analytics-xl", "zone": "${SCENARIO_ZONE}"},
    "reason": {
      "messageId": "no.scale.up.mig.failing.predicate",
      "parameters": ["Insufficient cpu", "Insufficient memory"]
    }
  }
],
"napFailureReasons": [
  {
    "messageId": "no.scale.up.nap.pod.zonal.resource.pool.exhausted",
    "parameters": ["${SCENARIO_MACHINE}", "${SCENARIO_ZONE}"]
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
