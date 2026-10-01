# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 2: no spare capacity, on a pool configured to allow unavailability (maxSurge 0 / maxUnavailable 1, one node)
CHANNEL=REGULAR; START=1.34; POOL_FLAGS="--num-nodes 1 --machine-type e2-standard-2 --max-surge-upgrade 0 --max-unavailable-upgrade 1"
plant(){ pause_deploy lonely 1; }
before(){ ev capacity before-pool G container node-pools describe work-pool --cluster "$CLUSTER" --zone "$ZONE" --format='value(upgradeSettings)'; ev capacity before K -n scen get pods -l app=lonely -o wide; }
break_it(){ V=$(newest_patch REGULAR 1.35); upgrade_master "$V"; upgrade_pool work-pool "$V" capacity:scen:app=lonely; }
after(){ ev capacity after K -n scen get pods -l app=lonely -o wide; ev capacity pending-events K -n scen get events --field-selector reason=FailedScheduling -o custom-columns='T:.lastTimestamp,O:.involvedObject.name,M:.message'; ev capacity op-timeline G container operations list --zone "$ZONE" --filter="targetLink~clusters/$CLUSTER/nodePools" --format='table(operationType,status,startTime,endTime)'; }
