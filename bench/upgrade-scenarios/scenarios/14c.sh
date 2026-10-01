# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 14c: the full cgroup v1 -> v2 path. A JVM that predates cgroup v2 support runs correctly on a v1 pool;
# the pool then moves to v2 (GKE's 1.35 upgrade, or the migration GKE requires first) and the same JVM
# sizes its heap from the host and is OOM-killed. Run as: CLUSTER=upg-14b bash run.sh 14c
CHANNEL=REGULAR; START=1.34; POOL_FLAGS=""
V1_POOL=v1-pool; LEGACY_IMAGE=eclipse-temurin:11.0.15_10-jdk; JVM_LIMIT=256Mi
plant(){ printf 'linuxConfig:\n  cgroupMode: CGROUP_MODE_V1\n' >"$EVID/cgroup-v1.yaml"; printf 'linuxConfig:\n  cgroupMode: CGROUP_MODE_V2\n' >"$EVID/cgroup-v2.yaml"
  pool_exists "$V1_POOL" || ev cgroup v1-pool G container node-pools create "$V1_POOL" --cluster "$CLUSTER" --zone "$ZONE" --num-nodes 1 --machine-type e2-standard-2 --disk-size "$NODE_DISK_GB" --node-labels=role=v1 --system-config-from-file "$EVID/cgroup-v1.yaml" --quiet
  K -n scen create configmap fill --from-literal=Fill.java='import java.util.*; public class Fill { public static void main(String[] a) throws Exception { System.out.println("max=" + Runtime.getRuntime().maxMemory()); List<byte[]> l = new ArrayList<>(); try { while (true) l.add(new byte[1<<23]); } catch (OutOfMemoryError e) { System.out.println("OOM caught"); } Thread.sleep(Long.MAX_VALUE); } }' --dry-run=client -o yaml | K -n scen apply -f - >/dev/null
  K -n scen apply -f - <<Y
apiVersion: apps/v1
kind: Deployment
metadata: {name: legacy-jvm-v1}
spec:
  replicas: 1
  selector: {matchLabels: {app: legacy-jvm-v1}}
  template:
    metadata: {labels: {app: legacy-jvm-v1}}
    spec:
      nodeSelector: {role: v1}
      volumes: [{name: src, configMap: {name: fill}}]
      containers:
        - name: jvm
          image: $LEGACY_IMAGE
          command: ["java", "/src/Fill.java"]
          volumeMounts: [{name: src, mountPath: /src}]
          resources: {requests: {cpu: 100m, memory: $JVM_LIMIT}, limits: {memory: $JVM_LIMIT}}
Y
}
jvm_state(){ ev cgroup "$1-mode" G container node-pools describe "$V1_POOL" --cluster "$CLUSTER" --zone "$ZONE" --format='value(version,config.effectiveCgroupMode)'
  ev cgroup "$1-pods" K -n scen get pods -l app=legacy-jvm-v1 -o custom-columns='NAME:.metadata.name,NODE:.spec.nodeName,PHASE:.status.phase,RESTARTS:.status.containerStatuses[0].restartCount,LAST:.status.containerStatuses[0].lastState.terminated.reason,EXIT:.status.containerStatuses[0].lastState.terminated.exitCode'
  ev cgroup "$1-log" K -n scen logs deploy/legacy-jvm-v1 --tail=3; }
before(){ sleep 60; jvm_state before; }
break_it(){ local V; V=$(newest_patch REGULAR 1.35)
  upgrade_master "$V"
  note cgroup "attempt the node upgrade of the cgroup v1 pool to $V"
  attempt_refusal cgroup ev cgroup pool-upgrade G container clusters upgrade "$CLUSTER" --node-pool "$V1_POOL" --cluster-version "$V" --zone "$ZONE" --quiet
  wait_ops; ev cgroup after-upgrade-attempt G container node-pools describe "$V1_POOL" --cluster "$CLUSTER" --zone "$ZONE" --format='value(version,config.effectiveCgroupMode)'
  # Key off the describe's exit status, not its text: a failed describe prints nothing, which would read as "not V1".
  local mode; mode=$(G container node-pools describe "$V1_POOL" --cluster "$CLUSTER" --zone "$ZONE" --format='value(config.effectiveCgroupMode)') ||
    { note cgroup "could not read the pool's cgroup mode; stopping"; exit 1; }
  if [[ $mode == *V1* ]]; then
    note cgroup "pool still cgroup v1: run the migration GKE asks for"
    retry_busy cgroup ev cgroup migrate G container node-pools update "$V1_POOL" --cluster "$CLUSTER" --zone "$ZONE" --system-config-from-file "$EVID/cgroup-v2.yaml" --quiet || exit 1
    wait_ops; fi
  sleep 180; }
after(){ jvm_state after; ev cgroup after-events K -n scen get events --field-selector involvedObject.kind=Pod --sort-by=.lastTimestamp -o custom-columns='T:.lastTimestamp,R:.reason,O:.involvedObject.name,M:.message'; }
