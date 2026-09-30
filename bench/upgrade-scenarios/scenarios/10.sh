# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 10: version skew: the pool stays at 1.31 while the master climbs; what GKE allows, and an old kubectl
CHANNEL=EXTENDED; START=1.31; POOL_FLAGS="--num-nodes 1 --machine-type e2-standard-2"
plant(){ pause_deploy steady 1; }
before(){ ev skew before G container node-pools list --cluster "$CLUSTER" --zone "$ZONE" --format='table(name,version)'; }
break_it(){ upgrade_master "$(newest_patch EXTENDED 1.32)"; upgrade_master "$(newest_patch EXTENDED 1.33)"; ev skew two-behind G container node-pools list --cluster "$CLUSTER" --zone "$ZONE" --format='table(name,version,status)'; note skew "attempt: master -> 1.34 with the pools three minors behind"; ev skew three-behind-attempt G container clusters upgrade "$CLUSTER" --master --cluster-version "$(newest_patch EXTENDED 1.34)" --zone "$ZONE" --quiet; wait_ops; }
after(){ ev skew after G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(currentMasterVersion)'; ev skew pools-after G container node-pools list --cluster "$CLUSTER" --zone "$ZONE" --format='table(name,version,status)'; curl -sSL -o /tmp/kubectl-1.29 https://dl.k8s.io/release/v1.29.0/bin/darwin/arm64/kubectl && chmod +x /tmp/kubectl-1.29 && ev skew old-kubectl /tmp/kubectl-1.29 --context "$CTX" get nodes; ev skew steady K -n scen get pods -o wide; }
