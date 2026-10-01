# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 4: state on an emptyDir; the pod writes its creation stamp once and a rebuild starts a new pod. The stamp is
# read again after the control-plane step and before the pool upgrade, so the loss is pinned to the node rebuild.
CHANNEL=REGULAR; START=1.34; POOL_FLAGS="--num-nodes 1 --machine-type e2-standard-2"; CREATE_FLAGS="--cluster-ipv4-cidr=/19"
plant(){ K -n scen apply -f - <<'Y'
apiVersion: apps/v1
kind: Deployment
metadata: {name: scratch}
spec:
  replicas: 1
  selector: {matchLabels: {app: scratch}}
  template:
    metadata: {labels: {app: scratch}}
    spec:
      nodeSelector: {role: work}
      volumes: [{name: data, emptyDir: {}}]
      containers:
        - name: keeper
          image: busybox:1.36
          command: ["sh", "-c", "test -f /data/created || date -u > /data/created; while true; do sleep 3600; done"]
          volumeMounts: [{name: data, mountPath: /data}]
          resources: {requests: {cpu: 10m, memory: 16Mi}}
Y
}
before(){ sleep 20; ev node-data before K -n scen exec deploy/scratch -- cat /data/created; }
break_it(){ V=$(newest_patch REGULAR 1.35); upgrade_master "$V"; ev node-data before-pool K -n scen exec deploy/scratch -- cat /data/created
  ev node-data before-pool-pod K -n scen get pods -l app=scratch -o wide; upgrade_pool work-pool "$V" node-data:scen:app=scratch; }
after(){ sleep 30; ev node-data after K -n scen exec deploy/scratch -- cat /data/created; ev node-data pods K -n scen get pods -l app=scratch -o wide; }
