# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 12: a nodeSelector on a label set by hand on the node, which the rebuilt node does not carry
CHANNEL=REGULAR; START=1.34; POOL_FLAGS="--num-nodes 1 --machine-type e2-standard-2"; CREATE_FLAGS="--cluster-ipv4-cidr=/19"
plant(){ N1=$(work_node 0); K label node "$N1" team=legacy --overwrite >/dev/null; pause_deploy label-pinned 1 "      nodeSelector: {team: legacy}"; }
before(){ ev label before K -n scen get pods -l app=label-pinned -o wide; ev label node-labels K get nodes -l role=work -L team; }
break_it(){ V=$(newest_patch REGULAR 1.35); upgrade_master "$V"; upgrade_pool work-pool "$V" label:scen:app=label-pinned; }
after(){ ev label after K -n scen get pods -l app=label-pinned -o wide; ev label after-node-labels K get nodes -l role=work -L team; ev label events K -n scen get events --field-selector reason=FailedScheduling -o custom-columns='T:.lastTimestamp,O:.involvedObject.name,M:.message'; }
