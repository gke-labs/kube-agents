# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 15: the group OOM kill (memory.oom.group from 1.28 on cgroup v2) against the singleProcessOomKill opt-out on a second pool
CHANNEL=REGULAR; START=1.35; POOL_FLAGS="--num-nodes 1 --machine-type e2-standard-2"
# e2-small leaves no room for a 256Mi pod after the system pods, so both pools are e2-standard-2.
forker(){ K -n scen delete pod "forker-$1" --ignore-not-found --wait=true >/dev/null; K -n scen apply -f - <<Y   # a re-run must not read the previous run's restart counter
apiVersion: v1
kind: Pod
metadata: {name: forker-$1, labels: {app: forker}}
spec:
  nodeSelector: {oompool: "$1"}
  restartPolicy: Always
  containers:
    - name: parent
      image: python:3.14-slim
      command: ["bash", "-c", "echo oom.group=\$(cat /sys/fs/cgroup/memory.oom.group); for i in 1 2 3; do python3 -c 'import time; b=bytearray(120*1024*1024); time.sleep(3600)' & done; while true; do sleep 10; echo children=\$(cat /proc/[0-9]*/comm 2>/dev/null | grep -cx python3); done"]
      resources: {requests: {cpu: 50m, memory: 256Mi}, limits: {memory: 256Mi}}
Y
}
plant(){ K label node -l role=work oompool=default --overwrite >/dev/null; forker default; }
before(){ sleep 90; ev group-oom default-pool-pod K -n scen get pod forker-default -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,RESTARTS:.status.containerStatuses[0].restartCount,LAST:.status.containerStatuses[0].lastState.terminated.reason'; ev group-oom default-pool-log K -n scen logs forker-default --previous --tail=6; }
break_it(){ printf 'kubeletConfig:\n  singleProcessOomKill: true\n' >"$EVID/single-process-oom-kill.yaml"
  pool_exists single-pool || retry_busy group-oom ev group-oom single-pool G container node-pools create single-pool --cluster "$CLUSTER" --zone "$ZONE" --num-nodes 1 --machine-type e2-standard-2 --disk-size "$NODE_DISK_GB" --node-labels=oompool=single --system-config-from-file "$EVID/single-process-oom-kill.yaml" --quiet ||
    { note final "precondition not met: single-pool was not created, so the opt-out half has nothing to run on; stopping"; exit 1; }
  forker single; sleep 120; }
after(){ ev group-oom single-pool-pod K -n scen get pod forker-single -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,RESTARTS:.status.containerStatuses[0].restartCount,LAST:.status.containerStatuses[0].lastState.terminated.reason'; ev group-oom single-pool-log K -n scen logs forker-single --tail=8; ev group-oom both K -n scen get pods -l app=forker -o custom-columns='NAME:.metadata.name,NODE:.spec.nodeName,RESTARTS:.status.containerStatuses[0].restartCount'; }
