# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 16: the dataplane's policy behaviour changes on a node rebuild. A default-deny NetworkPolicy sits unenforced;
# switching enforcement on changes nothing until the nodes are recreated, which the next node upgrade does.
# Run 1 found that enabling enforcement did not recreate the node (no calico-node, traffic still allowed),
# so the break is the pool upgrade that follows the config change.
CHANNEL=REGULAR; START=1.34; POOL_FLAGS="--num-nodes 1 --machine-type e2-standard-2"
plant(){ K -n scen apply -f - <<'Y'
apiVersion: apps/v1
kind: Deployment
metadata: {name: server}
spec:
  replicas: 1
  selector: {matchLabels: {app: server}}
  template:
    metadata: {labels: {app: server}}
    spec: {nodeSelector: {role: work}, containers: [{name: web, image: nginx:1.27-alpine, ports: [{containerPort: 80}], resources: {requests: {cpu: 10m, memory: 32Mi}}}]}
---
apiVersion: v1
kind: Service
metadata: {name: server}
spec: {selector: {app: server}, ports: [{port: 80}]}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: client}
spec:
  replicas: 1
  selector: {matchLabels: {app: client}}
  template:
    metadata: {labels: {app: client}}
    spec: {nodeSelector: {role: work}, containers: [{name: c, image: busybox:1.36, command: ["sh", "-c", "while true; do sleep 3600; done"], resources: {requests: {cpu: 10m, memory: 16Mi}}}]}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: default-deny-ingress}
spec: {podSelector: {}, policyTypes: [Ingress]}
Y
sleep 30; }
connect(){ K -n scen exec deploy/client -- wget -qO- --timeout=5 http://server 2>&1 | head -4; }
calico(){ K -n kube-system get pods -l k8s-app=calico-node -o wide; }
before(){ ev dataplane enforcement-before G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(networkPolicy,addonsConfig.networkPolicyConfig,networkConfig.datapathProvider)'; ev dataplane connect-before connect
  retry_busy dataplane ev dataplane enable-addon G container clusters update "$CLUSTER" --zone "$ZONE" --update-addons=NetworkPolicy=ENABLED --quiet || { note final "precondition not met: the NetworkPolicy add-on was not enabled; stopping before the upgrade"; exit 1; }; wait_ops
  retry_busy dataplane ev dataplane enable-enforcement G container clusters update "$CLUSTER" --zone "$ZONE" --enable-network-policy --quiet || { note final "precondition not met: policy enforcement was not enabled; stopping before the upgrade"; exit 1; }; wait_ops; sleep 120
  ev dataplane enforcement-configured G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(networkPolicy,addonsConfig.networkPolicyConfig)'; ev dataplane calico-before calico; ev dataplane connect-dormant connect; }
break_it(){ V=$(newest_patch REGULAR 1.35); upgrade_master "$V"; upgrade_pool work-pool "$V" dataplane:scen:app=server; }
after(){ sleep 60; ev dataplane nodes K get nodes -o wide; ev dataplane calico-after calico; ev dataplane connect-after connect; }
