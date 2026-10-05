# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 17: a per-node agent that a rebuild leaves behind: it runs only where a hand-set label is, and a client on the node depends on it
# (busybox httpd on the host network; busybox nc has no -q, which kept the first attempt's agent from listening)
CHANNEL=REGULAR; START=1.34; POOL_FLAGS="--num-nodes 1 --machine-type e2-standard-2"; CREATE_FLAGS="--cluster-ipv4-cidr=/19"
plant(){ N1=$(work_node 0); K label node "$N1" team=legacy --overwrite >/dev/null; K -n scen apply -f - <<'Y'
apiVersion: apps/v1
kind: DaemonSet
metadata: {name: node-agent}
spec:
  selector: {matchLabels: {app: node-agent}}
  template:
    metadata: {labels: {app: node-agent}}
    spec:
      nodeSelector: {team: legacy}
      hostNetwork: true
      containers: [{name: agent, image: busybox:1.36, command: ["sh", "-c", "mkdir -p /www && echo agent-ok > /www/index.html && exec httpd -f -p 18080 -h /www"], resources: {requests: {cpu: 10m, memory: 16Mi}}}]
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: consumer}
spec:
  replicas: 1
  selector: {matchLabels: {app: consumer}}
  template:
    metadata: {labels: {app: consumer}}
    spec:
      nodeSelector: {role: work}
      containers:
        - name: c
          image: busybox:1.36
          env: [{name: NODE_IP, valueFrom: {fieldRef: {fieldPath: status.hostIP}}}]
          command: ["sh", "-c", "while true; do wget -qO- --timeout=3 http://$NODE_IP:18080 || echo AGENT-UNREACHABLE; sleep 30; done"]
          resources: {requests: {cpu: 10m, memory: 16Mi}}
Y
sleep 60; }
before(){ ev node-agent before-ds K -n scen get ds node-agent; ev node-agent before-consumer K -n scen logs deploy/consumer --tail=2; }
break_it(){ V=$(newest_patch REGULAR 1.35); upgrade_master "$V"; upgrade_pool work-pool "$V" node-agent:scen:app=node-agent; }
after(){ sleep 60; ev node-agent after-ds K -n scen get ds node-agent; ev node-agent after-consumer K -n scen logs deploy/consumer --tail=3; ev node-agent node-labels K get nodes -l role=work -L team; }
