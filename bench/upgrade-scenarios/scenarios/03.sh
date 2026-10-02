# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 3: two replicas that must share a node (required podAffinity on the hostname), so rebuilding that node
# takes both at once; a Deployment spread across nodes on the same pool is the control. A 30 s readiness
# delay stands in for an application's start-up, so a moment with nothing serving shows in 10 s samples.
CHANNEL=REGULAR; START=1.34; POOL_FLAGS="--num-nodes 2 --machine-type e2-standard-2"; CREATE_FLAGS="--cluster-ipv4-cidr=/19"
READY_DELAY=30
web(){ # web <name> <placement block, indented 6>: two busybox web servers with a delayed readiness probe
  K -n scen apply -f - <<Y
apiVersion: apps/v1
kind: Deployment
metadata: {name: $1}
spec:
  replicas: 2
  selector: {matchLabels: {app: $1}}
  template:
    metadata: {labels: {app: $1}}
    spec:
      nodeSelector: {role: work}
$2
      containers:
        - name: web
          image: busybox:1.36
          command: ["sh", "-c", "mkdir -p /www && echo ok > /www/index.html && exec httpd -f -p 8080 -h /www"]
          readinessProbe: {tcpSocket: {port: 8080}, initialDelaySeconds: $READY_DELAY, periodSeconds: 5}
          resources: {requests: {cpu: 10m, memory: 16Mi}}
Y
}
plant(){ web same-node "      affinity:
        podAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - {labelSelector: {matchLabels: {app: same-node}}, topologyKey: kubernetes.io/hostname}"
  web spread "      topologySpreadConstraints:
        - {maxSkew: 1, topologyKey: kubernetes.io/hostname, whenUnsatisfiable: DoNotSchedule, labelSelector: {matchLabels: {app: spread}}}"; }
before(){ ev placement before K -n scen get pods -l 'app in (same-node,spread)' -o wide; ev placement before-ready K -n scen get deploy same-node spread; }
break_it(){ V=$(newest_patch REGULAR 1.35); upgrade_master "$V"; ev placement before-pool K -n scen get pods -l 'app in (same-node,spread)' -o wide
  upgrade_pool work-pool "$V" placement:scen:app=same-node spread:scen:app=spread; }
after(){ ev placement summary-same-node serving_summary placement; ev placement summary-spread serving_summary spread
  ev placement after K -n scen get pods -l 'app in (same-node,spread)' -o wide; ev placement after-ready K -n scen get deploy same-node spread
  ev placement killing-events K -n scen get events --field-selector reason=Killing -o custom-columns='T:.lastTimestamp,O:.involvedObject.name,M:.message'; }
