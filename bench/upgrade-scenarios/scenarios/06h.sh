# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 06h: scenario 6's hazard held in its before-state (1.31 control plane, a flowcontrol/v1beta3 caller) through the
# Recommender's daily refresh, with no upgrade; a no-upgrades exclusion keeps GKE from moving it meanwhile.
. "$H/scenarios/06.sh"; CREATE_FLAGS="--cluster-ipv4-cidr=/19"
break_it(){ hold_break; }
after(){ :; }
