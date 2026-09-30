#!/bin/bash
# run.sh NN: build cluster upg-NN for scenario NN, plant its defect, record the before-state, break it
# (usually an upgrade), record the after-state, and leave the cluster up for the Recommender's next
# daily refresh. Each scenario is one file in scenarios/ defining CHANNEL, START (minor), optional
# CREATE_FLAGS and POOL_FLAGS, and the functions plant, before, break_it, after. Evidence: evidence/NN/.
DEFAULT_POOL_MACHINE=e2-small; NODE_DISK_GB=32; PLANT_SETTLE=60   # the default pool only runs system pods; scenarios add a work-pool
set -u; NN=${1:?scenario number, two digits}; TRACK=$NN; CLUSTER=${CLUSTER:-upg-$NN}
# shellcheck source-path=SCRIPTDIR source=common.sh
. "$(dirname "$0")/common.sh"; . "$H/scenarios/$NN.sh"
START_VERSION=$(newest_patch "$CHANNEL" "$START"); note cluster "scenario $NN on $CLUSTER: $CHANNEL $START_VERSION"
G container clusters describe "$CLUSTER" --zone "$ZONE" >/dev/null 2>&1 || ev cluster create G container clusters create "$CLUSTER" --zone "$ZONE" --release-channel "$(echo $CHANNEL | tr A-Z a-z)" --cluster-version "$START_VERSION" --num-nodes 1 --machine-type "$DEFAULT_POOL_MACHINE" --disk-size "$NODE_DISK_GB" --workload-pool="$PROJECT.svc.id.goog" --labels=purpose=$SCENARIO_LABEL,scenario=$NN --quiet ${CREATE_FLAGS:-}
require_scenario_cluster
# And the scenario's own cluster: the label run.sh set at creation must be NN, or share NN's number as the base a lettered
# re-run extends (CLUSTER=upg-10 bash run.sh 10b; CLUSTER=upg-14b bash run.sh 14c). A hold cluster (label ending in h) is
# accepted only by its own scenario, since an upgrade there ends the multi-day hold the Recommender is waiting on.
LABEL=$(G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(resourceLabels.scenario)'); BASE=${NN%%[a-z]*}
[ "$LABEL" = "$NN" ] || { [ "${LABEL%%[a-z]*}" = "$BASE" ] && [[ $LABEL != *h ]]; } ||
  { echo "refusing: $CLUSTER was built for scenario '$LABEL', which is not $NN, a base it extends, or the same number without a hold suffix" >&2; exit 1; }
if [ -n "${POOL_FLAGS:-}" ]; then pool_exists work-pool || ev cluster work-pool G container node-pools create work-pool --cluster "$CLUSTER" --zone "$ZONE" --node-version "$START_VERSION" --node-labels=role=work --disk-size "$NODE_DISK_GB" --quiet ${POOL_FLAGS} ||
  { note final "precondition not met: work-pool was not created; stopping before the plant"; exit 1; }; fi
G container clusters get-credentials "$CLUSTER" --zone "$ZONE" --quiet >/dev/null 2>&1
ev baseline nodes K get nodes -o custom-columns='NAME:.metadata.name,VER:.status.nodeInfo.kubeletVersion,RUNTIME:.status.nodeInfo.containerRuntimeVersion,POOL:.metadata.labels.cloud\.google\.com/gke-nodepool'
K create ns scen --dry-run=client -o yaml | K apply -f - >/dev/null
# Any failing step inside plant (a manifest that does not apply, a create that fails) marks the run, and it stops
# before the upgrade rather than recording an after-state for a hazard that was never there.
run_plant plant
[ "$PLANT_FAILED" -eq 0 ] || { note final "precondition not met: a step in plant failed (see the console); stopping before the upgrade"; exit 1; }
sleep "$PLANT_SETTLE"; ev plant pods K -n scen get pods -o wide; before; break_it; after
ev final pods K -n scen get pods -o wide; ev final events K -n scen get events --sort-by=.lastTimestamp -o custom-columns='T:.lastTimestamp,R:.reason,O:.involvedObject.name,M:.message'; note final "scenario $NN done"
