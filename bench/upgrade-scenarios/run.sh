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
G container clusters describe "$CLUSTER" --zone "$ZONE" >/dev/null 2>&1 || ev cluster create G container clusters create "$CLUSTER" --zone "$ZONE" --release-channel "$(echo $CHANNEL | tr A-Z a-z)" --cluster-version "$START_VERSION" --num-nodes 1 --machine-type "$DEFAULT_POOL_MACHINE" --disk-size "$NODE_DISK_GB" --workload-pool="$PROJECT.svc.id.goog" --labels=purpose=upgrade-scenarios,scenario=$NN --quiet ${CREATE_FLAGS:-}
if [ -n "${POOL_FLAGS:-}" ]; then G container node-pools describe work-pool --cluster "$CLUSTER" --zone "$ZONE" >/dev/null 2>&1 || ev cluster work-pool G container node-pools create work-pool --cluster "$CLUSTER" --zone "$ZONE" --node-version "$START_VERSION" --node-labels=role=work --disk-size "$NODE_DISK_GB" --quiet ${POOL_FLAGS}; fi
G container clusters get-credentials "$CLUSTER" --zone "$ZONE" --quiet >/dev/null 2>&1
ev baseline nodes K get nodes -o custom-columns='NAME:.metadata.name,VER:.status.nodeInfo.kubeletVersion,RUNTIME:.status.nodeInfo.containerRuntimeVersion,POOL:.metadata.labels.cloud\.google\.com/gke-nodepool'
K create ns scen --dry-run=client -o yaml | K apply -f - >/dev/null
plant; sleep "$PLANT_SETTLE"; ev plant pods K -n scen get pods -o wide; before; break_it; after
ev final pods K -n scen get pods -o wide; ev final events K -n scen get events --sort-by=.lastTimestamp -o custom-columns='T:.lastTimestamp,R:.reason,O:.involvedObject.name,M:.message'; note final "scenario $NN done"
