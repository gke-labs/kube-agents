# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 11b: the zonal control plane during its own upgrade, probed every second with a read AND a write.
# Run 11 polled only reads (/version), about every 2.6 s because each loop also asked gcloud for the
# operation, and saw no failure. This run keeps gcloud out of the probe loop and adds a write: a zonal
# control plane that serves reads from a cache but refuses changes is still an outage for a deploy.
CHANNEL=REGULAR; START=1.34; CREATE_FLAGS="--cluster-ipv4-cidr=/19"
PROBE_TIMEOUT=3s
probe_loop(){ local f="$EVID/zonal-probe.txt" i=0; echo "# $(ts) probe start (read=/version, write=create+delete ConfigMap, timeout $PROBE_TIMEOUT)" >"$f"
  while [ -f "$EVID/probing" ]; do i=$((i+1))
    if K --request-timeout=$PROBE_TIMEOUT get --raw /version >/dev/null 2>&1; then r=up; else r=DOWN; fi
    if K --request-timeout=$PROBE_TIMEOUT -n scen create configmap "probe-$i" --from-literal=t="$(ts)" >/dev/null 2>&1; then w=up; K --request-timeout=$PROBE_TIMEOUT -n scen delete configmap "probe-$i" --wait=false >/dev/null 2>&1; else w=DOWN; fi
    echo "$(ts) read=$r write=$w" >>"$f"; sleep 1; done; echo "# $(ts) probe end" >>"$f"; }
plant(){ pause_deploy bystander 1 "      nodeSelector: {}"; }
before(){ ev zonal before K get --raw /version; ev zonal endpoint G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(endpoint,location,locations)'; }
break_it(){ V=$(newest_patch REGULAR 1.35); touch "$EVID/probing"; probe_loop & local P=$!; sleep 10
  ev zonal upgrade G container clusters upgrade "$CLUSTER" --master --cluster-version "$V" --zone "$ZONE" --quiet; wait_ops; sleep 30; rm -f "$EVID/probing"; wait $P; }
after(){ ev zonal summary sh -c "f='$EVID/zonal-probe.txt'; echo samples=\$(grep -c read= \$f); echo read_down=\$(grep -c read=DOWN \$f); echo write_down=\$(grep -c write=DOWN \$f); grep DOWN \$f | head -3; grep DOWN \$f | tail -2"
  ev zonal version G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(currentMasterVersion)'; ev zonal op G container operations list --zone "$ZONE" --filter="targetLink~clusters/$CLUSTER\$ AND operationType=UPGRADE_MASTER" --format='table(operationType,status,startTime,endTime)'; }
