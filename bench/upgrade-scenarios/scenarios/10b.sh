# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 10b: the kubelet skew limit. upg-10 already runs a 1.34 control plane over 1.31 nodes (three minors,
# the upstream maximum); this asks GKE for a 1.35 control plane over the same nodes (four minors) and,
# if GKE allows it, checks what still works against the old kubelets. Run as: CLUSTER=upg-10 bash run.sh 10b
CHANNEL=EXTENDED; START=1.34; POOL_FLAGS=""; EXTENDS=10   # runs on scenario 10's cluster
[ "$(G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(resourceLabels.scenario)' 2>/dev/null)" = 10 ] && pool_exists work-pool ||
  { echo "10b extends scenario 10's cluster, and $CLUSTER is not it (no scenario=10 label or no work-pool); run as: CLUSTER=upg-10 bash run.sh 10b" >&2; exit 1; }
plant(){ pause_deploy steady 1; }
before(){ ev skew before G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(currentMasterVersion)'; ev skew pools-before G container node-pools list --cluster "$CLUSTER" --zone "$ZONE" --format='table(name,version,status)'; }
break_it(){ local V; V=$(newest_patch EXTENDED 1.35); require_version "$V"; note skew "attempt: master -> $V with the pools four minors behind"
  attempt_refusal skew ev skew four-behind-attempt G container clusters upgrade "$CLUSTER" --master --cluster-version "$V" --zone "$ZONE" --quiet --timeout "$MASTER_UPGRADE_TIMEOUT"; wait_ops; }
after(){ ev skew master-after G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(currentMasterVersion)'
  ev skew pools-after G container node-pools list --cluster "$CLUSTER" --zone "$ZONE" --format='table(name,version,status)'
  ev skew nodes K get nodes -o wide
  ev skew logs-through-kubelet K -n scen logs deploy/steady --tail=1
  K -n scen delete pod skew-probe --ignore-not-found --wait=true >/dev/null; ev skew new-pod K -n scen run skew-probe --image=busybox:1.36 --restart=Never --overrides='{"spec":{"nodeSelector":{"role":"work"}}}' -- sh -c 'echo ran-on-old-kubelet'
  sleep 45; ev skew new-pod-result K -n scen get pod skew-probe -o wide; ev skew new-pod-log K -n scen logs skew-probe; ev skew exec K -n scen exec deploy/steady -- /pause -v; }
