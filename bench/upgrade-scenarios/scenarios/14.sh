# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 14: a runtime that cannot read cgroup v2 sizes its heap from the host; the symptom on a v2 node, legacy and fixed JVM side by side (14c runs the full v1 -> v2 path)
CHANNEL=REGULAR; START=1.34; POOL_FLAGS="--num-nodes 1 --machine-type e2-standard-2"
# e2-standard-2 (8 GB): a JVM that reads the host instead of the cgroup sizes its heap from 8 GB against a 256Mi limit.
plant(){ K -n scen create configmap fill --from-literal=Fill.java='import java.util.*; public class Fill { public static void main(String[] a) throws Exception { System.out.println("max=" + Runtime.getRuntime().maxMemory()); List<byte[]> l = new ArrayList<>(); try { while (true) l.add(new byte[1<<23]); } catch (OutOfMemoryError e) { System.out.println("OOM caught"); } Thread.sleep(Long.MAX_VALUE); } }' --dry-run=client -o yaml | K -n scen apply -f - >/dev/null
  for pair in "legacy-jvm:eclipse-temurin:11.0.15_10-jdk" "fixed-jvm:eclipse-temurin:11.0.16_8-jdk"; do name=${pair%%:*}; img=${pair#*:}; K -n scen apply -f - <<Y
apiVersion: apps/v1
kind: Deployment
metadata: {name: $name}
spec:
  replicas: 1
  selector: {matchLabels: {app: $name}}
  template:
    metadata: {labels: {app: $name}}
    spec:
      nodeSelector: {role: work}
      volumes: [{name: src, configMap: {name: fill}}]
      containers:
        - name: jvm
          image: $img
          command: ["java", "/src/Fill.java"]
          volumeMounts: [{name: src, mountPath: /src}]
          resources: {requests: {cpu: 100m, memory: 256Mi}, limits: {memory: 256Mi}}
Y
done; }
before(){ ev cgroup mode G container node-pools describe work-pool --cluster "$CLUSTER" --zone "$ZONE" --format='value(config.effectiveCgroupMode)'; }
break_it(){ note cgroup "no upgrade step: the node is already cgroup v2, which is the state a migrated pool reaches"; sleep 180; }
after(){ ev cgroup pods K -n scen get pods -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,RESTARTS:.status.containerStatuses[0].restartCount,LAST:.status.containerStatuses[0].lastState.terminated.reason,EXIT:.status.containerStatuses[0].lastState.terminated.exitCode'; ev cgroup legacy-log K -n scen logs deploy/legacy-jvm --tail=3; ev cgroup fixed-log K -n scen logs deploy/fixed-jvm --tail=3; }
