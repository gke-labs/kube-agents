# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 18k: the 1.33 -> 1.34 leg of scenario 18 on a T4 (launch with ZONE=us-central1-b), with the forward-compatibility
# probe in each round. 18i lost its only node when its 1.34 replacement hit a T4 stockout (maxSurge 0), so this
# leg surges: the 1.34 node is created before the 1.33 node is drained, and a stockout stalls the upgrade instead
# of emptying the pool. 1.33 is the last minor on GKE's R535 default and 1.34 the first on R580, so this one step
# is the driver change. 18m runs the same leg in us-west1-a.
. "$H/scenarios/18.sh"
START=1.33
POOL_FLAGS="--num-nodes 1 --machine-type n1-standard-4 --disk-size 200 --max-surge-upgrade 1 --max-unavailable-upgrade 0 --accelerator type=nvidia-tesla-t4,count=1,gpu-driver-version=default,gpu-sharing-strategy=time-sharing,max-shared-clients-per-gpu=2"
compat_round(){ TRACK=$TRACK CLUSTER=$CLUSTER ZONE=$ZONE bash "$H/compat-probe.sh" "$1" || { note final "precondition not met: the compatibility probe $1 did not run; stopping"; exit 1; }; }
before(){ probe_round v133; compat_round v133; }
break_it(){ local v; v=$(newest_patch EXTENDED 1.34); upgrade_master "$v"; upgrade_pool work-pool "$v"; }
after(){ probe_round v134; compat_round v134; ev gpu-driver pool-ops G container operations list --zone "$ZONE" --filter="targetLink~clusters/$CLUSTER/nodePools/work-pool AND operationType=UPGRADE_NODES" --format='table(name,status,startTime,endTime,statusMessage)'; }
