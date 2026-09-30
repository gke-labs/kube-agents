#!/bin/bash
# hold.sh NN: after scenario NN's run, put its hazard back in its before-state on the same cluster, so the
# Recommender's next daily refresh (about 00:00Z) can see it. Evidence: evidence/NN-hold/.
# Run as: CLUSTER=upg-07 bash hold.sh 07
set -u; NN=${1:?scenario number}; TRACK=$NN-hold; CLUSTER=${CLUSTER:?cluster name}
# shellcheck source-path=SCRIPTDIR source=common.sh
. "$(dirname "$0")/common.sh"; require_scenario_cluster
G container clusters get-credentials "$CLUSTER" --zone "$ZONE" --quiet >/dev/null 2>&1
SCHEMA1_IMAGE=gcr.io/google_containers/busybox:1.24     # a Docker schema 1 manifest, which containerd 2.0 refuses
CD17_VERSION=1.31.14-gke.2704000                        # the newest 1.31 patch still on containerd 1.7
LEGACY_JVM=eclipse-temurin:11.0.15_10-jdk; JVM_LIMIT=256Mi
CU13_IMAGE=pytorch/pytorch:2.10.0-cuda13.0-cudnn9-runtime
SETTLE=180; GPU_SETTLE=900
pool_exists(){ G container node-pools describe "$1" --cluster "$CLUSTER" --zone "$ZONE" >/dev/null 2>&1; }
# 7: the fail-closed webhook back in place, its Service still without endpoints
hold_07(){ . "$H/scenarios/07.sh"; plant >/dev/null; sleep 10
  ev webhook config K get validatingwebhookconfiguration fail-closed-gate
  ev webhook no-endpoints K -n scen get endpointslices -l kubernetes.io/service-name=absent-hook
  ev webhook create-refused K -n scen run hold-probe --image=registry.k8s.io/pause:3.9 --restart=Never; }
# 13: a containerd 1.7 pool beside the 2.0 one; the v1alpha2 CRI client and a schema 1 image run on both
hold_13(){ pool_exists cd17-hold || ev runtime cd17-pool G container node-pools create cd17-hold --cluster "$CLUSTER" --zone "$ZONE" --node-version "$CD17_VERSION" --num-nodes 1 --machine-type e2-standard-2 --disk-size 32 --node-labels=role=work --quiet
  K -n scen apply -f - <<Y
apiVersion: apps/v1
kind: DaemonSet
metadata: {name: schema1-image}
spec:
  selector: {matchLabels: {app: schema1}}
  template:
    metadata: {labels: {app: schema1}}
    spec:
      nodeSelector: {role: work}
      containers: [{name: old, image: $SCHEMA1_IMAGE, command: ["sh", "-c", "while true; do sleep 3600; done"], resources: {requests: {cpu: 10m, memory: 16Mi}}}]
Y
  sleep $SETTLE
  ev runtime nodes K get nodes -l role=work -o custom-columns='NAME:.metadata.name,POOL:.metadata.labels.cloud\.google\.com/gke-nodepool,VER:.status.nodeInfo.kubeletVersion,RUNTIME:.status.nodeInfo.containerRuntimeVersion'
  ev runtime cri-agents K -n scen get pods -l app=cri-agent -o wide
  for p in $(K -n scen get pods -l app=cri-agent -o name); do ev runtime "crictl-${p##*/}" K -n scen logs "$p" -c agent --tail=3; done
  ev runtime schema1-pods K -n scen get pods -l app=schema1 -o wide
  ev runtime schema1-events K -n scen get events --field-selector reason=Failed -o custom-columns='T:.lastTimestamp,O:.involvedObject.name,M:.message'; }
# 14: a cgroup v1 pool on 1.34 (the last minor that upgrades one) with the legacy JVM on it
hold_14(){ local v; v=$(newest_patch REGULAR 1.34); printf 'linuxConfig:\n  cgroupMode: CGROUP_MODE_V1\n' >"$EVID/cgroup-v1.yaml"
  pool_exists v1-hold || ev cgroup v1-hold-pool G container node-pools create v1-hold --cluster "$CLUSTER" --zone "$ZONE" --node-version "$v" --num-nodes 1 --machine-type e2-standard-2 --disk-size 32 --node-labels=role=v1hold --system-config-from-file "$EVID/cgroup-v1.yaml" --quiet
  K -n scen apply -f - <<Y
apiVersion: apps/v1
kind: Deployment
metadata: {name: legacy-jvm-hold}
spec:
  replicas: 1
  selector: {matchLabels: {app: legacy-jvm-hold}}
  template:
    metadata: {labels: {app: legacy-jvm-hold}}
    spec:
      nodeSelector: {role: v1hold}
      volumes: [{name: src, configMap: {name: fill}}]
      containers:
        - name: jvm
          image: $LEGACY_JVM
          command: ["java", "/src/Fill.java"]
          volumeMounts: [{name: src, mountPath: /src}]
          resources: {requests: {cpu: 100m, memory: $JVM_LIMIT}, limits: {memory: $JVM_LIMIT}}
Y
  sleep $SETTLE
  ev cgroup v1-hold-mode G container node-pools describe v1-hold --cluster "$CLUSTER" --zone "$ZONE" --format='value(version,config.effectiveCgroupMode)'
  ev cgroup v1-hold-pods K -n scen get pods -l app=legacy-jvm-hold -o custom-columns='NAME:.metadata.name,NODE:.spec.nodeName,PHASE:.status.phase,RESTARTS:.status.containerStatuses[0].restartCount'
  ev cgroup v1-hold-log K -n scen logs deploy/legacy-jvm-hold --tail=3; }
# 18: a 1.33 L4 pool on the default driver running a CUDA 13 build, which crash-loops until the driver moves
hold_18(){ local v; v=$(newest_patch EXTENDED 1.33)
  pool_exists gpu-hold || ev gpu-driver gpu-hold-pool G container node-pools create gpu-hold --cluster "$CLUSTER" --zone "$ZONE" --node-version "$v" --num-nodes 1 --machine-type g2-standard-4 --disk-size 200 --accelerator type=nvidia-l4,count=1,gpu-driver-version=default --node-labels=role=gpuhold --quiet
  K -n scen apply -f - <<Y
apiVersion: apps/v1
kind: Deployment
metadata: {name: cuda13-hold}
spec:
  replicas: 1
  selector: {matchLabels: {app: cuda13-hold}}
  template:
    metadata: {labels: {app: cuda13-hold}}
    spec:
      nodeSelector: {role: gpuhold}
      tolerations: [{key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}]
      containers:
        - name: probe
          image: $CU13_IMAGE
          command: ["sh", "-c", "echo driver=\$(/usr/local/nvidia/bin/nvidia-smi --query-gpu=driver_version --format=csv,noheader); python3 -c 'import torch; print(\"cuda\", torch.version.cuda, \"available\", torch.cuda.is_available()); assert torch.cuda.is_available()' && exec sleep infinity"]
          resources: {limits: {nvidia.com/gpu: 1}}
Y
  sleep $GPU_SETTLE
  ev gpu-driver gpu-hold-pods K -n scen get pods -l app=cuda13-hold -o wide
  ev gpu-driver gpu-hold-log K -n scen logs deploy/cuda13-hold --tail=4
  ev gpu-driver gpu-hold-node K get nodes -l role=gpuhold -o custom-columns='NAME:.metadata.name,VER:.status.nodeInfo.kubeletVersion,DRIVER_LABEL:.metadata.labels.cloud\.google\.com/gke-gpu-driver-version'; }
# 19: the PD CSI driver add-on off again, with the in-tree PersistentVolume still bound
hold_19(){ . "$H/scenarios/19.sh"
  ev csi disable-driver G container clusters update "$CLUSTER" --zone "$ZONE" --update-addons=GcePersistentDiskCsiDriver=DISABLED --quiet; wait_ops
  ev csi addon-state csi_state; ev csi pv K get pv intree-pd; ev csi pod K -n scen get pods -l app=pd-user -o wide; }
note hold "re-planting scenario $NN's hazard on $CLUSTER for the Recommender"; "hold_$NN"; note hold "scenario $NN hold done"
