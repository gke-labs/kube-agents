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

# Family and zone are discovered rather than hardcoded. The scenario needs a machine
# family that the pinned zone does not offer; which families those are is a property of
# the region on the day, not a constant. c2d in us-east1-b was the original pinning, and
# every ordinary family is offered in every zone of that region — so the workload simply
# scheduled and no scale-up failure was ever emitted. Override with SCENARIO_FAMILY and
# SCENARIO_ZONE to force a pair.
SCENARIO_FAMILY="${SCENARIO_FAMILY:-}"
SCENARIO_ZONE="${SCENARIO_ZONE:-}"

_resolve_pinned_family() {
    [ -n "$SCENARIO_FAMILY" ] && [ -n "$SCENARIO_ZONE" ] && return 0

    local pair
    pair="$(gcloud compute machine-types list --project="$PROJECT_ID" \
                --filter="zone ~ ^${CLUSTER_LOCATION}-" \
                --format='csv[no-heading](name,zone)' 2>/dev/null |
        python3 -c '
import collections, csv, sys
# Ordinary compute families first: this scenario is about an over-narrow spec on a
# mundane workload, not about scarce hardware. Accelerator and TPU families are excluded
# outright — they need accelerator wiring Autopilot will not infer, and they belong to
# the GPU scenarios.
# Ordinary compute first; then the memory-optimised families, which Autopilot accepts
# and which do have zone gaps in some regions. Anything else is a last resort, since an
# exotic family may be refused at admission and never reach Pending.
PREFERRED = ("n2", "n2d", "c2", "c2d", "c3", "c3d", "c4", "n4", "e2", "t2d", "m1", "m2", "m3", "m4")
EXCLUDED = ("a2", "a3", "a4", "a4x", "g2", "g4", "h4d")
zones, fam = set(), collections.defaultdict(set)
for row in csv.reader(sys.stdin):
    if len(row) != 2:
        continue
    name, zone = row
    zones.add(zone)
    fam[name.split("-")[0]].add(zone)
def gaps(names):
    for f in names:
        missing = sorted(zones - fam.get(f, set()))
        if fam.get(f) and missing:
            return f, missing[0]
    return None
pick = gaps(PREFERRED) or gaps(sorted(
    f for f in fam
    if f not in EXCLUDED and not f.startswith(("ct", "tpu"))))
if pick:
    print(pick[0], pick[1])
' || true)"

    if [ -n "$pair" ]; then
        SCENARIO_FAMILY="${pair%% *}"
        SCENARIO_ZONE="${pair##* }"
    else
        SCENARIO_FAMILY="c2d"
        SCENARIO_ZONE="$ZONE_A"
        warn "no family with a zone gap in ${CLUSTER_LOCATION}; falling back to ${SCENARIO_FAMILY} in ${SCENARIO_ZONE}, which may simply schedule" >&2
    fi
}
SCENARIO_PODS=4

# Memoised: this runs from both hooks, and scenario_manifest itself runs twice, so an
# unmemoised resolver would query the API three times per run. The query returns the
# first PREFERRED family with a zone gap, and which one that is depends on what the
# region reports at the moment of the call — leaving the ComputeClass pinning one family
# and the alert naming another.
_pinned_family() {
    scenario_memo family _resolve_pinned_family SCENARIO_FAMILY SCENARIO_ZONE
}

scenario_manifest() {
    _pinned_family
    # stderr: this function's stdout is the manifest.
    info "pinning family ${SCENARIO_FAMILY} to ${SCENARIO_ZONE} (that family is not offered there)" >&2
    cat <<YAML
apiVersion: cloud.google.com/v1
kind: ComputeClass
metadata:
  name: frontend-pinned-class
  labels:
    scenario: 04-missing-zone-fallback
spec:
  priorities:
    - machineFamily: ${SCENARIO_FAMILY}
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
                    values: ["${SCENARIO_ZONE}"]
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
    # Resolved again here, not just in scenario_manifest. Both hooks run in a subshell —
    # scenario_manifest's stdout is piped into kubectl, this one's is captured into the
    # alert payload — so an assignment made in one is invisible to the other and to the
    # parent. Without this the published alert named an empty family and an empty zone,
    # which reads as a malformed autoscaler event rather than the over-narrow pinning.
    # Memoised, so this replays the manifest's answer instead of asking again.
    _pinned_family
    cat <<JSON
"rejectedMigs": [
  {
    "mig": {"name": "gke-${CLUSTER_NAME}-${SCENARIO_FAMILY}-pool", "nodepool": "frontend-pinned", "zone": "${SCENARIO_ZONE}"},
    "reason": {
      "messageId": "no.scale.up.mig.failing.predicate",
      "parameters": ["NodeAffinity"]
    }
  }
],
"napFailureReasons": [
  {
    "messageId": "no.scale.up.nap.pod.zonal.resource.pool.exhausted",
    "parameters": ["${SCENARIO_FAMILY}", "${SCENARIO_ZONE}"]
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
