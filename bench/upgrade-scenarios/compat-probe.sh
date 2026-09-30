#!/usr/bin/env bash
# compat-probe.sh <tag>: scenario 18's forward-compatibility probe, run beside a 18x track (TRACK, CLUSTER, ZONE
# from the environment). NVIDIA's forward-compatibility libraries (cuda-compat, /usr/local/cuda-*/compat in the
# nvidia/cuda images) let a newer CUDA run on an older driver, and are supported only on older drivers. Each pod
# copies them from the nvidia/cuda base image in an init container and puts them first on LD_LIBRARY_PATH, as an
# image does when it forces forward compatibility, then asks torch to open the GPU. Evidence: <track>/compat.txt.
set -uo pipefail
H=$(cd "$(dirname "$0")" && pwd); . "$H/common.sh"
TAG=$1; WAIT=900; POLL=10
CU12_BASE=nvidia/cuda:12.4.1-base-ubuntu22.04; CU12_TORCH=pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime
CU13_BASE=nvidia/cuda:13.0.0-base-ubuntu24.04; CU13_TORCH=pytorch/pytorch:2.10.0-cuda13.0-cudnn9-runtime
compat_pod(){ # compat_pod <name> <base image> <torch image>
  K -n scen delete pod "$1" --ignore-not-found >/dev/null
  K -n scen apply -f - <<Y
apiVersion: v1
kind: Pod
metadata: {name: $1, labels: {app: compat-probe}}
spec:
  restartPolicy: Never
  nodeSelector: {role: work}
  tolerations: [{key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}]
  volumes: [{name: compat, emptyDir: {}}]
  initContainers:
    - name: copy-compat
      image: $2
      command: ["sh", "-c", "cp -a /usr/local/cuda/compat/. /compat/ && ls /compat"]
      volumeMounts: [{name: compat, mountPath: /compat}]
  containers:
    - name: probe
      image: $3
      env: [{name: LD_LIBRARY_PATH, value: "/compat:/usr/local/nvidia/lib64:/usr/local/nvidia/lib"}]
      command: ["sh", "-c", "echo driver=\$(/usr/local/nvidia/bin/nvidia-smi --query-gpu=driver_version --format=csv,noheader) compat=\$(ls /compat | grep -o 'libcuda.so.[0-9.]*' | sort -u | tail -1); python3 -c 'import torch,sys; ok=torch.cuda.is_available(); print(\"torch\", torch.__version__, \"cuda\", torch.version.cuda, \"available\", ok); sys.exit(0 if ok else 3)'"]
      volumeMounts: [{name: compat, mountPath: /compat}]
      resources: {limits: {nvidia.com/gpu: 1}}
Y
}
done_(){ case "$(K -n scen get pod "$1" -o jsonpath='{.status.phase}' 2>/dev/null)" in Succeeded|Failed) return 0;; *) return 1;; esac; }
compat_pod compat-cu124-$TAG $CU12_BASE $CU12_TORCH; compat_pod compat-cu130-$TAG $CU13_BASE $CU13_TORCH
t=0; while ! { done_ compat-cu124-$TAG && done_ compat-cu130-$TAG; } && [ $t -lt $WAIT ]; do sleep $POLL; t=$((t+POLL)); done
ev compat $TAG-cu124-init K -n scen logs compat-cu124-$TAG -c copy-compat; ev compat $TAG-cu124 K -n scen logs compat-cu124-$TAG
ev compat $TAG-cu130-init K -n scen logs compat-cu130-$TAG -c copy-compat; ev compat $TAG-cu130 K -n scen logs compat-cu130-$TAG
ev compat $TAG-pods K -n scen get pods -l app=compat-probe -o custom-columns='NAME:.metadata.name,NODE:.spec.nodeName,PHASE:.status.phase,EXIT:.status.containerStatuses[0].state.terminated.exitCode'
ev compat $TAG-node K get nodes -l role=work -o custom-columns='NAME:.metadata.name,VER:.status.nodeInfo.kubeletVersion,GPU:.status.allocatable.nvidia\.com/gpu'
