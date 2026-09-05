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

# The Spot-only class only demonstrates anything if its Spot tiers are genuinely
# unobtainable. The original pinning (c3-standard-88, then the c2 and e2 families) could
# not do that: `gcloud beta compute advice capacity` puts Spot obtainability for those at
# 0.5-0.9 in a healthy region, so the class was always satisfiable, the pods scheduled,
# and no scale-up failure was ever emitted.
#
# What is deterministic is that some families do not offer Spot *at all* in a region --
# the advisor rejects them with "Machine specification is not supported in locations"
# rather than scoring them. That is the ideal Rule D trigger, because the same shape is
# still available on demand: appending a non-Spot tier is a real fix, not a workaround.
# In us-east1 the H4D family is the only non-metal family with that property (metal
# shapes reject Spot too, but NAP cannot provision them for unrelated reasons, which
# would blame the wrong thing). Override with SCENARIO_MACHINES to force a list.
SCENARIO_MACHINES="${SCENARIO_MACHINES:-}"

# Preference order for the probe. Non-metal only, and no accelerator shapes: those belong
# to scenarios 01 and 02, and their scarcity has a different remediation.
SPOT_CANDIDATES=(h4d-standard-192 h4d-highmem-192)

_spot_unsupported() {
    # True when the region offers no Spot for this shape at all, as opposed to offering it
    # and being short today. The advisor errors in the first case and scores in the second.
    ! gcloud beta compute advice capacity --project="$PROJECT_ID" --region="$CLUSTER_LOCATION" \
        --provisioning-model=SPOT --size=1 --instance-selection-machine-types="$1" \
        --target-distribution-shape=any-single-zone >/dev/null 2>&1
}

_beta_missing_message="scenario 05 probes Spot obtainability with 'gcloud beta compute advice capacity', and the beta component is not installed. Install it (gcloud components install beta) or set SCENARIO_MACHINES to pin the shapes directly."

# Runs at the end of preflight(), before clear_dedup and before any workload exists.
# The probe below reads "the advisor rejected this shape" as "the region offers no Spot
# for it", and a missing `beta` component is an indistinguishable rejection -- every
# candidate would be classified unobtainable on a check that never ran. Refusing here
# rather than at first use is the difference between declining to start and dying with
# the dedup registry already cleared and a namespace already created.
scenario_preflight() {
    [ -n "$SCENARIO_MACHINES" ] && return 0
    gcloud beta --help >/dev/null 2>&1 || die "$_beta_missing_message"
}

_resolve_spot_only_shapes() {
    [ -n "$SCENARIO_MACHINES" ] && return 0

    # --cleanup renders this manifest only to name what to delete, and every object in it
    # has a static name -- the probe's answer changes nothing. Skip it, so a --cleanup on
    # a machine without the beta component still removes the workload instead of dying
    # with a 6-replica Deployment left running. (--teardown is a different flag: it runs
    # the whole scenario first, so it needs the real shapes and is not exempted.)
    if [ "${DO_CLEANUP:-0}" -eq 1 ]; then
        SCENARIO_MACHINES="c3-standard-88"
        return 0
    fi

    # Backstop for the paths that never reach preflight -- --emit-manifest is the one
    # that still needs real shapes.
    gcloud beta --help >/dev/null 2>&1 || die "$_beta_missing_message"

    local found=() mt
    for mt in "${SPOT_CANDIDATES[@]}"; do
        # Only consider shapes the region actually offers on demand: a shape absent from
        # the region has no Spot either, but then "add an on-demand tier" fixes nothing
        # and the scenario would be teaching the wrong lesson.
        gcloud compute machine-types list --project="$PROJECT_ID" \
            --filter="name=${mt} AND zone ~ ^${CLUSTER_LOCATION}-" --format='value(name)' 2>/dev/null |
            grep -q . || continue
        _spot_unsupported "$mt" && found+=("$mt")
    done

    if [ ${#found[@]} -gt 0 ]; then
        SCENARIO_MACHINES="${found[*]}"
    else
        # Every candidate can be had on Spot today. Fall back to the original pinning; the
        # run will most likely schedule, which the watch reports as no investigation.
        SCENARIO_MACHINES="c3-standard-88"
        warn "no shape found whose Spot is unsupported in ${CLUSTER_LOCATION}; falling back to ${SCENARIO_MACHINES}" >&2
    fi
}

# Memoised: this runs from both hooks, and scenario_manifest itself runs twice, so an
# unmemoised resolver would query the API up to six times per run.
_spot_only_shapes() {
    scenario_memo spot _resolve_spot_only_shapes SCENARIO_MACHINES
}

scenario_manifest() {
    _spot_only_shapes
    # stderr, not stdout: this function's stdout is piped straight into kubectl.
    # On --cleanup the shapes are the unprobed placeholder, so claiming their Spot is
    # unsupported would state the opposite of what this file documents about c3.
    if [ "${DO_CLEANUP:-0}" -eq 1 ]; then
        info "Tiers: ${SCENARIO_MACHINES// /, } (placeholder; naming objects to delete, not probed)" >&2
    else
        info "Spot-only tiers: ${SCENARIO_MACHINES// /, } (Spot unsupported in ${CLUSTER_LOCATION}; on demand available)" >&2
    fi
    local priorities="" mt
    for mt in $SCENARIO_MACHINES; do
        priorities+="    - machineType: ${mt}"$'\n'"      spot: true"$'\n'
    done
    cat <<YAML
apiVersion: cloud.google.com/v1
kind: ComputeClass
metadata:
  name: batch-spot-only-class
  labels:
    scenario: 05-missing-ondemand-floor
spec:
  # Every priority is Spot, and every one of them names a shape this region does not
  # offer on Spot. There is no further tier, so the class cannot be satisfied at any
  # price -- even though the same shapes are sitting there available on demand.
  priorities:
${priorities%$'\n'}
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
    _spot_only_shapes
    local primary="${SCENARIO_MACHINES%% *}"
    cat <<JSON
"rejectedMigs": [
  {
    "mig": {"name": "gke-${CLUSTER_NAME}-spot-${primary%%-*}", "nodepool": "batch-spot-only", "zone": "${ZONE_A}"},
    "reason": {
      "messageId": "no.scale.up.mig.failing.predicate",
      "parameters": ["Insufficient cpu"]
    }
  },
  {
    "mig": {"name": "gke-${CLUSTER_NAME}-spot-${primary%%-*}", "nodepool": "batch-spot-only", "zone": "${ZONE_B}"},
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
    _spot_only_shapes
    # Quoted heredoc below, and printf here: the prose is full of backticked command
    # names, and an unquoted heredoc runs every one of them as a command substitution.
    printf '%s\n\n' "The Spot tiers here are unobtainable by construction, not by luck: \
${SCENARIO_MACHINES// /, } is offered in ${CLUSTER_LOCATION} on demand but not on Spot in
any of its zones. That is what makes this a clean Rule D case rather than a shape-scarcity
one -- the capacity exists, and only the provisioning model puts it out of reach. An
investigation that blames regional scarcity has misread it."
    cat <<'TXT'
This is also the scenario that should exercise the Spot capacity advisor. The skill mandates
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
