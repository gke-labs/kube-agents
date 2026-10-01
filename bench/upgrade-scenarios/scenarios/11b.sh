# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 11b: the zonal control plane during its own upgrade, probed with a read AND a write in a loop that sleeps one
# second between samples; each sample is two API calls under a 3 s timeout, so the recorded gap is about 2.6 to 3 s.
# Run 11 polled only reads (/version), about every 2.6 s because each loop also asked gcloud for the
# operation, and saw no failure. This run keeps gcloud out of the probe loop and adds a write: a zonal
# control plane that serves reads from a cache but refuses changes is still an outage for a deploy.
CHANNEL=REGULAR; START=1.34; CREATE_FLAGS="--cluster-ipv4-cidr=/19"
PROBE_TIMEOUT=3s
probe_loop(){ local stop=$1 f="$EVID/zonal-probe.txt" i=0 run; run=$(date -u +%H%M%S)-$$   # names unique to this run: a ConfigMap a timed-out delete left behind must not read as a refused write next time
  echo "# $(ts) probe start (read=/version, write=create+delete ConfigMap, timeout $PROBE_TIMEOUT)" >>"$f"   # append: a re-run adds a block, it does not replace the checked-in one
  until [ -e "$stop" ] || ! kill -0 $$ 2>/dev/null; do i=$((i+1))   # $$ is run.sh: an interrupted run leaves no prober
    if K --request-timeout=$PROBE_TIMEOUT get --raw /version >/dev/null 2>&1; then r=up; else r=DOWN; fi
    if K --request-timeout=$PROBE_TIMEOUT -n scen create configmap "probe-$run-$i" --from-literal=t="$(ts)" >/dev/null 2>&1; then w=up; K --request-timeout=$PROBE_TIMEOUT -n scen delete configmap "probe-$run-$i" --wait=false >/dev/null 2>&1; else w=DOWN; fi
    echo "$(ts) read=$r write=$w" >>"$f"; sleep 1; done; echo "# $(ts) probe end" >>"$f"; }
plant(){ pause_deploy bystander 1 "      nodeSelector: {}"; }
before(){ ev zonal before K get --raw /version; ev zonal endpoint G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(endpoint,location,locations)'; }
break_it(){ V=$(newest_patch REGULAR 1.35); require_version "$V"; local stop="$KCFG_DIR/$CLUSTER.$TRACK.zonal-done"; rm -f "$stop"; probe_loop "$stop" & local P=$!; sleep 10
  retry_busy zonal ev zonal upgrade G container clusters upgrade "$CLUSTER" --master --cluster-version "$V" --zone "$ZONE" --quiet --timeout "$MASTER_UPGRADE_TIMEOUT" ||
    { touch "$stop"; wait $P; rm -f "$stop"; exit 1; }   # a probe of an untouched control plane would read as "not reproduced"
  wait_ops; sleep 30; touch "$stop"; wait $P; rm -f "$stop"; }
last_probe(){ since_last_start "$EVID/zonal-probe.txt"; }   # the run that just ended, not every run appended to the file
probe_summary(){ echo "samples=$(last_probe | grep -vc '^#')"; echo "read_down=$(last_probe | grep -c read=DOWN)"; echo "write_down=$(last_probe | grep -c write=DOWN)"; last_probe | grep DOWN | head -3; last_probe | grep DOWN | tail -2; }
after(){ ev zonal summary probe_summary
  ev zonal version G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(currentMasterVersion)'; ev zonal op G container operations list --zone "$ZONE" --filter="targetLink~clusters/$CLUSTER\$ AND operationType=UPGRADE_MASTER" --format='table(operationType,status,startTime,endTime)'; }
