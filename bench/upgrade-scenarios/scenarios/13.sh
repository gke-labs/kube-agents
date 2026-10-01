# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 13: the container runtime changes with the node image, and a v1alpha2 CRI client breaks. This run found the newest
# 1.31 patch already on containerd 2.0 (the runtime moves inside the 1.31 patch line, not at 1.32); 13b stages the
# patch-only step that moves it, and the verdict rests on 13b.
CHANNEL=EXTENDED; START=1.31; POOL_FLAGS="--num-nodes 1 --machine-type e2-standard-2"
CLIENT_ROLLOUT_TIMEOUT=300s; CRICTL_VERSION=v1.22.0; CRICTL_URL=https://github.com/kubernetes-sigs/cri-tools/releases/download/$CRICTL_VERSION/crictl-$CRICTL_VERSION-linux-amd64.tar.gz
CRICTL_SHA256=45e0556c42616af60ebe93bf4691056338b3ea0001c0201a6a8ff8b1dbc0652a   # the release's published .sha256; the fetch runs as root beside the containerd socket, so it is checked before anything is unpacked
plant(){ K -n scen apply -f - <<Y
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
          command: ["sh", "-c", "curl -fsSL -o /tmp/crictl.tgz $CRICTL_URL && echo \"$CRICTL_SHA256  /tmp/crictl.tgz\" | sha256sum -c - && tar -xz -C /bin-out -f /tmp/crictl.tgz"]
          volumeMounts: [{name: bin, mountPath: /bin-out}]
      containers:
        - name: agent
          image: busybox:1.36
          securityContext: {runAsUser: 0}
          command: ["sh", "-c", "while true; do date -u; /opt/crictl -r unix:///run/containerd/containerd.sock version || echo CRICTL-FAILED; sleep 60; done"]
          volumeMounts: [{name: bin, mountPath: /opt}, {name: sock, mountPath: /run/containerd/containerd.sock}]
          resources: {requests: {cpu: 10m, memory: 16Mi}}
Y
  K -n scen rollout status ds/cri-v1alpha2-agent --timeout="$CLIENT_ROLLOUT_TIMEOUT"; }   # the init container must have fetched and verified crictl, or there is no client to break
before(){ sleep 60; ev runtime before-runtime K get nodes -l role=work -o custom-columns='NAME:.metadata.name,RUNTIME:.status.nodeInfo.containerRuntimeVersion'; ev runtime before-crictl K -n scen logs ds/cri-v1alpha2-agent --tail=4; }
break_it(){ V=$(newest_patch EXTENDED 1.32); upgrade_master "$V"; upgrade_pool work-pool "$V" runtime:scen:app=cri-agent; }
after(){ sleep 90; ev runtime after-runtime K get nodes -l role=work -o custom-columns='NAME:.metadata.name,RUNTIME:.status.nodeInfo.containerRuntimeVersion'; ev runtime after-crictl K -n scen logs ds/cri-v1alpha2-agent --tail=6; }
