# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 8: a default that flips in the new minor: the kubelet refuses gitRepo volumes from 1.33
CHANNEL=EXTENDED; START=1.32; POOL_FLAGS="--num-nodes 1 --machine-type e2-standard-2"
plant(){ K -n scen apply -f - <<'Y'
apiVersion: apps/v1
kind: Deployment
metadata: {name: gitrepo-legacy}
spec:
  replicas: 1
  selector: {matchLabels: {app: gitrepo}}
  template:
    metadata: {labels: {app: gitrepo}}
    spec:
      nodeSelector: {role: work}
      volumes: [{name: repo, gitRepo: {repository: "https://github.com/kubernetes/examples.git"}}]
      containers:
        - name: reader
          image: busybox:1.36
          command: ["sh", "-c", "ls /repo | head -3; while true; do sleep 3600; done"]
          volumeMounts: [{name: repo, mountPath: /repo}]
          resources: {requests: {cpu: 10m, memory: 16Mi}}
Y
}
before(){ sleep 30; ev default-change before K -n scen get pods -l app=gitrepo -o wide; ev default-change before-events K -n scen get events --sort-by=.lastTimestamp -o custom-columns='T:.lastTimestamp,R:.reason,M:.message'; }
break_it(){ V=$(newest_patch EXTENDED 1.33); upgrade_master "$V"; upgrade_pool work-pool "$V" default-change:scen:app=gitrepo; }
after(){ sleep 30; ev default-change after K -n scen get pods -l app=gitrepo -o wide; ev default-change after-events K -n scen get events --sort-by=.lastTimestamp -o custom-columns='T:.lastTimestamp,R:.reason,M:.message'; }
