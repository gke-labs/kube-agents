# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 18: GPU driver mismatch. A CUDA 13 PyTorch image on an L4 pool pinned to GKE's default driver, which the
# GPU how-to table lists as R535 on 1.31-1.33 (CUDA 13 needs >=580); a CUDA 12.4 image beside it is the
# control (R535 runs 12.x through minor-version compatibility). The pool then goes 1.32 -> 1.33 -> 1.34 and
# each step re-reads the driver and re-runs both probes. Time-sharing lets both probes share the one L4;
# maxSurge 0 is the usual GPU-pool setting and deletes the node before its replacement exists.
# First attempt: CUDA devel images on a 32 GB disk hit ephemeral-storage eviction, on 1.34 where the default is R580.
CHANNEL=EXTENDED; START=1.32; CREATE_FLAGS="--cluster-ipv4-cidr=/19"
POOL_FLAGS="--num-nodes 1 --machine-type g2-standard-4 --disk-size 200 --max-surge-upgrade 0 --max-unavailable-upgrade 1 --accelerator type=nvidia-l4,count=1,gpu-driver-version=default,gpu-sharing-strategy=time-sharing,max-shared-clients-per-gpu=2"
CU12_IMAGE=pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime
CU13_IMAGE=pytorch/pytorch:2.10.0-cuda13.0-cudnn9-runtime
GPU_WAIT=1200; PROBE_WAIT=1500; POLL=15
gpu_ready(){ [ -n "$(K get nodes -l role=work -o jsonpath='{.items[*].status.allocatable.nvidia\.com/gpu}' 2>/dev/null | tr -d ' 0')" ]; }
wait_gpu(){ local t=0; while ! gpu_ready && [ $t -lt $GPU_WAIT ]; do sleep $POLL; t=$((t+POLL)); done; note gpu-driver "GPU allocatable after ${t}s wait: $(K get nodes -l role=work -o jsonpath='{.items[*].status.allocatable.nvidia\.com/gpu}')"; }
torch_probe(){ # torch_probe <pod> <image>: print the node's driver, then exit 0 only if torch can open the GPU
  K -n scen delete pod "$1" --ignore-not-found >/dev/null
  K -n scen apply -f - <<Y
apiVersion: v1
kind: Pod
metadata: {name: $1, labels: {app: torch-probe}}
spec:
  restartPolicy: Never
  nodeSelector: {role: work}
  tolerations: [{key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}]
  containers:
    - name: probe
      image: $2
      command: ["sh", "-c", "echo driver=\$(/usr/local/nvidia/bin/nvidia-smi --query-gpu=driver_version --format=csv,noheader); python3 -c 'import torch,sys; ok=torch.cuda.is_available(); print(\"torch\", torch.__version__, \"cuda\", torch.version.cuda, \"available\", ok); sys.exit(0 if ok else 3)'"]
      resources: {limits: {nvidia.com/gpu: 1}}
Y
}
probe_done(){ case "$(K -n scen get pod "$1" -o jsonpath='{.status.phase}' 2>/dev/null)" in Succeeded|Failed) return 0;; *) return 1;; esac; }
probe_round(){ local tag=$1 t=0; wait_gpu; torch_probe cu124-$tag "$CU12_IMAGE"; torch_probe cu130-$tag "$CU13_IMAGE"
  while ! { probe_done cu124-$tag && probe_done cu130-$tag; } && [ $t -lt $PROBE_WAIT ]; do sleep $POLL; t=$((t+POLL)); done
  ev gpu-driver $tag-cu124 K -n scen logs cu124-$tag; ev gpu-driver $tag-cu130 K -n scen logs cu130-$tag
  ev gpu-driver $tag-pods K -n scen get pods -l app=torch-probe -o custom-columns='NAME:.metadata.name,NODE:.spec.nodeName,PHASE:.status.phase,EXIT:.status.containerStatuses[0].state.terminated.exitCode'
  ev gpu-driver $tag-node K get nodes -l role=work -o custom-columns='NAME:.metadata.name,VER:.status.nodeInfo.kubeletVersion,DRIVER_LABEL:.metadata.labels.cloud\.google\.com/gke-gpu-driver-version,GPU:.status.allocatable.nvidia\.com/gpu'
  ev gpu-driver $tag-pool G container node-pools describe work-pool --cluster "$CLUSTER" --zone "$ZONE" --format='value(version,config.accelerators[0].gpuDriverInstallationConfig.gpuDriverVersion)'
  ev gpu-driver $tag-installer sh -c "for p in \$(kubectl --context $CTX -n kube-system get pods -o name | grep nvidia-gpu-device-plugin); do kubectl --context $CTX -n kube-system logs \$p -c nvidia-driver-installer 2>/dev/null | grep -i 'driver version' | tail -2; done"; }
plant(){ wait_gpu; }
before(){ probe_round v132; }
break_it(){ local v; for m in 1.33 1.34; do v=$(newest_patch EXTENDED $m); upgrade_master "$v"; upgrade_pool work-pool "$v"; [ $m = 1.34 ] || probe_round v${m/./}; done; }
after(){ probe_round v134; ev gpu-driver pool-ops G container operations list --zone "$ZONE" --filter="targetLink~clusters/$CLUSTER/nodePools/work-pool AND operationType=UPGRADE_NODES" --format='table(name,status,startTime,endTime,statusMessage)'; }
