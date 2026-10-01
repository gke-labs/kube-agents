# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 19: an in-tree gcePersistentDisk PersistentVolume, served through CSI migration, on a cluster whose PD CSI
# driver add-on is off. Run 1 found GKE enables the driver even when --addons omits it, so the driver is
# disabled explicitly; the running pod keeps its mount, and the break comes when the pool upgrade moves it.
# Run 19b's disable ended in a Compute Engine stockout (GCE_STOCKOUT) with the driver still on, and the run
# upgraded anyway; the disable is now retried and the run stops before the upgrade if the driver stays on.
CHANNEL=REGULAR; START=1.34; POOL_FLAGS="--num-nodes 1 --machine-type e2-standard-2"
DISK="$CLUSTER-intree"
DISABLE_TRIES=4; DISABLE_WAIT=120; ROLLOUT_TIMEOUT=300s
csi_state(){ G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(addonsConfig.gcePersistentDiskCsiDriverConfig)'; }
pd_events(){ K -n scen get events --field-selector involvedObject.kind=Pod -o custom-columns='T:.lastTimestamp,R:.reason,O:.involvedObject.name,M:.message' | grep -v "Pulling\|Pulled\|Created\|Started" | tail -8; }
plant(){ describe_exists "disk $DISK" G compute disks describe "$DISK" --zone "$ZONE" || G compute disks create "$DISK" --zone "$ZONE" --size 10GB --type pd-balanced --quiet >/dev/null; K apply -f - <<Y
apiVersion: v1
kind: PersistentVolume
metadata: {name: intree-pd}
spec:
  capacity: {storage: 10Gi}
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: intree
  gcePersistentDisk: {pdName: $DISK, fsType: ext4}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: intree-pd, namespace: scen}
spec: {accessModes: [ReadWriteOnce], storageClassName: intree, resources: {requests: {storage: 10Gi}}, volumeName: intree-pd}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: pd-user, namespace: scen}
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector: {matchLabels: {app: pd-user}}
  template:
    metadata: {labels: {app: pd-user}}
    spec:
      nodeSelector: {role: work}
      volumes: [{name: data, persistentVolumeClaim: {claimName: intree-pd}}]
      containers: [{name: c, image: busybox:1.36, command: ["sh", "-c", "date -u >> /data/log; while true; do sleep 3600; done"], volumeMounts: [{name: data, mountPath: /data}], resources: {requests: {cpu: 10m, memory: 16Mi}}}]
Y
  K -n scen rollout status deploy/pd-user --timeout="$ROLLOUT_TIMEOUT"; }   # the pod must have mounted the disk before the driver goes off
before(){ ev csi addon-default csi_state; sleep 60; ev csi pod-with-driver K -n scen get pods -l app=pd-user -o wide
  disable_driver || { note csi "precondition not met: the PD CSI driver is not confirmed off after $DISABLE_TRIES tries; stopping before the upgrade"; exit 1; }
  sleep 60; ev csi addon-disabled csi_state; ev csi pod-still-running K -n scen get pods -l app=pd-user -o wide; }
# An empty state reads as off, but only from a describe that succeeded: a failed one also prints nothing.
disable_driver(){ local i s; for i in $(seq 1 $DISABLE_TRIES); do
    ev csi disable-driver G container clusters update "$CLUSTER" --zone "$ZONE" --update-addons=GcePersistentDiskCsiDriver=DISABLED --quiet; wait_ops
    if s=$(csi_state); then case $s in *enabled=True*) note csi "driver still on after try $i; retrying in ${DISABLE_WAIT}s" ;; *) return 0 ;; esac
    else note csi "could not read the add-on state after try $i; retrying in ${DISABLE_WAIT}s"; fi; sleep $DISABLE_WAIT; done; return 1; }
break_it(){ V=$(newest_patch REGULAR 1.35); upgrade_master "$V"; upgrade_pool work-pool "$V" csi:scen:app=pd-user; }
after(){ sleep 120; ev csi pod-after-upgrade K -n scen get pods -l app=pd-user -o wide; ev csi events-after-upgrade pd_events
  note csi "fix: enable the PD CSI driver add-on"; retry_busy csi ev csi enable-driver G container clusters update "$CLUSTER" --zone "$ZONE" --update-addons=GcePersistentDiskCsiDriver=ENABLED --quiet || { note final "the PD CSI driver was not re-enabled; the pod is left Pending"; exit 1; }; wait_ops; sleep 180
  ev csi pod-after-fix K -n scen get pods -l app=pd-user -o wide; ev csi events-after-fix pd_events; }
