# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 13b: the container runtime changes with the node image, here inside one minor; a v1alpha2 CRI client breaks
# Run 13 found the newest 1.31 patch (1.31.14-gke.2759000) already on containerd 2.0.10, so the client
# was broken before any upgrade. This run starts the work pool on 1.31.14-gke.2704000 (containerd 1.7.34,
# measured on gemma-gpu) and the break is a patch-only node upgrade inside 1.31.
CHANNEL=EXTENDED; START=1.31; POOL_FLAGS=""
CLIENT_ROLLOUT_TIMEOUT=300s; CRICTL_VERSION=v1.22.0; CRICTL_URL=https://github.com/kubernetes-sigs/cri-tools/releases/download/$CRICTL_VERSION/crictl-$CRICTL_VERSION-linux-amd64.tar.gz
CRICTL_SHA256=45e0556c42616af60ebe93bf4691056338b3ea0001c0201a6a8ff8b1dbc0652a   # the release's published .sha256; the fetch runs as root beside the containerd socket, so it is checked before anything is unpacked
OLD_NODE_VERSION=1.31.14-gke.2704000
AUTO_HOLD_DAYS=2; WORK_MACHINE=e2-small   # POOL_START is read from the pool in plant and used by break_it
plant(){ has_exclusion hold-auto || retry_busy cluster ev cluster hold G container clusters update "$CLUSTER" --zone "$ZONE" --add-maintenance-exclusion-name hold-auto --add-maintenance-exclusion-start "$(ts)" --add-maintenance-exclusion-end "$(in_days "$AUTO_HOLD_DAYS")" --add-maintenance-exclusion-scope no_upgrades --quiet
  pool_exists work-pool || retry_busy cluster ev cluster work-pool G container node-pools create work-pool --cluster "$CLUSTER" --zone "$ZONE" --node-version "$OLD_NODE_VERSION" --node-labels=role=work --disk-size "$NODE_DISK_GB" --num-nodes 1 --machine-type "$WORK_MACHINE" --quiet
  # The experiment is the patch step off this exact version; a pool already past it (a re-run, or scenario 13's cluster) has nothing to show.
  POOL_START=$(G container node-pools describe work-pool --cluster "$CLUSTER" --zone "$ZONE" --format='value(version)') || { note final "could not read work-pool's version; stopping"; exit 1; }
  [ "$POOL_START" = "$OLD_NODE_VERSION" ] || { note final "precondition not met: work-pool is on $POOL_START, not $OLD_NODE_VERSION, so the patch-only step has already happened or this is not 13b's pool; stopping before the upgrade"; exit 1; }
  K -n scen apply -f - <<Y
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
break_it(){ V=$(newest_patch EXTENDED 1.31); note runtime "patch-only node upgrade $POOL_START -> $V"; upgrade_pool work-pool "$V" runtime:scen:app=cri-agent; }
after(){ sleep 90; ev runtime after-runtime K get nodes -l role=work -o custom-columns='NAME:.metadata.name,RUNTIME:.status.nodeInfo.containerRuntimeVersion'; ev runtime after-crictl K -n scen logs ds/cri-v1alpha2-agent --tail=6; }
