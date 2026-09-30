# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 13: the container runtime changes with the node image: containerd 1.7 on 1.31, 2.0 on 1.32; a v1alpha2 CRI client breaks
CHANNEL=EXTENDED; START=1.31; POOL_FLAGS="--num-nodes 1 --machine-type e2-standard-2"
plant(){ K -n scen apply -f - <<'Y'
apiVersion: apps/v1
kind: DaemonSet
metadata: {name: cri-v1alpha2-agent}
spec:
  selector: {matchLabels: {app: cri-agent}}
  template:
    metadata: {labels: {app: cri-agent}}
    spec:
      nodeSelector: {role: work}
      volumes:
        - {name: bin, emptyDir: {}}
        - {name: sock, hostPath: {path: /run/containerd/containerd.sock, type: Socket}}
      initContainers:
        - name: fetch
          image: curlimages/curl:8.10.1
          command: ["sh", "-c", "curl -sSL https://github.com/kubernetes-sigs/cri-tools/releases/download/v1.22.0/crictl-v1.22.0-linux-amd64.tar.gz | tar -xz -C /bin-out"]
          volumeMounts: [{name: bin, mountPath: /bin-out}]
      containers:
        - name: agent
          image: busybox:1.36
          securityContext: {runAsUser: 0}
          command: ["sh", "-c", "while true; do date -u; /opt/crictl -r unix:///run/containerd/containerd.sock version || echo CRICTL-FAILED; sleep 60; done"]
          volumeMounts: [{name: bin, mountPath: /opt}, {name: sock, mountPath: /run/containerd/containerd.sock}]
          resources: {requests: {cpu: 10m, memory: 16Mi}}
Y
}
before(){ sleep 60; ev runtime before-runtime K get nodes -l role=work -o custom-columns='NAME:.metadata.name,RUNTIME:.status.nodeInfo.containerRuntimeVersion'; ev runtime before-crictl K -n scen logs ds/cri-v1alpha2-agent --tail=4; }
break_it(){ V=$(newest_patch EXTENDED 1.32); upgrade_master "$V"; upgrade_pool work-pool "$V" runtime:scen:app=cri-agent; }
after(){ sleep 90; ev runtime after-runtime K get nodes -l role=work -o custom-columns='NAME:.metadata.name,RUNTIME:.status.nodeInfo.containerRuntimeVersion'; ev runtime after-crictl K -n scen logs ds/cri-v1alpha2-agent --tail=6; }
