#!/bin/bash
# Shared helpers for the upgrade-scenario tracks. Every observation goes through ev(), which
# records the command, its output, its exit code and a UTC timestamp under evidence/<track>/.
DEFAULT_ZONE=us-central1-a
: "${PROJECT:?set PROJECT to the GCP project the scenario clusters go in}" "${ZONE:=$DEFAULT_ZONE}" "${TRACK:?TRACK unset}" "${CLUSTER:?CLUSTER unset}"
H=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd); EVID="$H/evidence/$TRACK"; mkdir -p "$EVID"
CTX="gke_${PROJECT}_${ZONE}_${CLUSTER}"
# One kubeconfig file per cluster: parallel gcloud writers racing on the shared ~/.kube/config corrupted it once.
KCFG_DIR="$H/.kubeconfigs"; mkdir -p "$KCFG_DIR"; export KUBECONFIG="$KCFG_DIR/$CLUSTER"
# run.sh labels every cluster it creates purpose=$SCENARIO_LABEL; every script that plants or upgrades refuses any other.
SCENARIO_LABEL=upgrade-scenarios
require_scenario_cluster(){ local p; p=$(G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(resourceLabels.purpose)' 2>/dev/null)
  [ "$p" = "$SCENARIO_LABEL" ] || { echo "refusing: $CLUSTER in $ZONE is not labelled purpose=$SCENARIO_LABEL (label: '$p')" >&2; exit 1; }; }
ts(){ date -u +%Y-%m-%dT%H:%M:%SZ; }
in_days(){ date -u -v+"$1"d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "+$1 days" +%Y-%m-%dT%H:%M:%SZ; }   # BSD date, then GNU
ev(){ local sc=$1 st=$2; shift 2; local f="$EVID/$sc.txt"; { echo; echo "## $(ts) [$sc/$st] $*"; "$@" 2>&1; echo "## exit $?"; } | tee -a "$f"; }
note(){ local sc=$1; shift; echo "## $(ts) [$sc/note] $*" | tee -a "$EVID/$sc.txt"; }
K(){ kubectl --context "$CTX" --request-timeout=30s "$@"; }
G(){ gcloud "$@" --project "$PROJECT"; }
newest_patch(){ G container get-server-config --location "$ZONE" --format=json | python3 -c "import json,sys;d=json.load(sys.stdin);ch=[c for c in d['channels'] if c['channel']=='$1'][0];print([v for v in ch['validVersions'] if v.startswith('$2.')][0])"; }
ops_running(){ G container operations list --zone "$ZONE" --filter="(targetLink~clusters/${CLUSTER}\$ OR targetLink~clusters/${CLUSTER}/) AND status=RUNNING" --format='value(operationType,name)'; }
wait_ops(){ local n=0; while [ -n "$(ops_running)" ]; do sleep 30; n=$((n+1)); [ $((n%10)) -eq 0 ] && echo "# $(ts) still running: $(ops_running | tr '\n' ' ')"; done; }
# await_op: the pollers start before the upgrade call returns an operation, so each one first waits
# (up to OP_APPEAR_TIMEOUT seconds) for an operation to appear; checking only "while running" ends at once.
OP_APPEAR_TIMEOUT=120
await_op(){ local t=0; while [ -z "$(ops_running)" ] && [ $t -lt $OP_APPEAR_TIMEOUT ]; do sleep 5; t=$((t+5)); done; }
# poll_avail <scenario> <namespace> <label>: every 10 s while a node-pool operation runs, record each pod's
# node, phase, readiness and deletion stamp (node/phase/ready/deleting), so a moment with zero serving
# replicas is on record even if it lasts 20 s. A terminating pod can still report Ready=True, but a
# Service has already dropped it, so "serving" is Ready=True with an empty deletion stamp.
poll_avail(){ local sc=$1 ns=$2 sel=$3; local f="$EVID/$sc-availability.txt"; echo "# $(ts) poll start $ns $sel" >>"$f"; await_op
  while [ -n "$(ops_running)" ]; do echo "$(ts) $(K -n "$ns" get pods -l "$sel" -o jsonpath='{range .items[*]}{.spec.nodeName}/{.status.phase}/{.status.conditions[?(@.type=="Ready")].status}/{.metadata.deletionTimestamp} {end}' 2>&1)" >>"$f"; sleep 10; done; echo "# $(ts) poll end" >>"$f"; }
# poll_api <scenario> <stop-file>: every 5 s until <stop-file> exists, record whether the API server answers (entry 11).
# upgrade_master creates the stop file once its blocking upgrade call returns, so another operation on the cluster
# can neither end the poll before the control-plane upgrade nor keep it running after.
poll_api(){ local sc=$1 stop=$2; local f="$EVID/$sc-api.txt"; echo "# $(ts) api poll start" >>"$f"
  until [ -e "$stop" ]; do if K get --raw /version >/dev/null 2>&1; then echo "$(ts) up" >>"$f"; else echo "$(ts) DOWN" >>"$f"; fi; sleep 5; done; echo "# $(ts) api poll end" >>"$f"; }
BUSY_RETRIES=5; BUSY_WAIT=30
# GKE refuses an upgrade while any other operation runs on the cluster ("incompatible operation"): wait for it, retry.
retry_busy(){ local f=$1 i; shift; for i in $(seq 1 $BUSY_RETRIES); do wait_ops; "$@"; tail -4 "$f" | grep -q "incompatible operation" || return 0; note upgrade "refused while another operation ran; retry $i"; sleep $BUSY_WAIT; done; }
upgrade_master(){ local v=$1 stop="$KCFG_DIR/$CLUSTER.master-done"; rm -f "$stop"; note upgrade "master -> $v"; poll_api zonal-api "$stop" & local p=$!; retry_busy "$EVID/upgrade.txt" ev upgrade master-$v G container clusters upgrade "$CLUSTER" --master --cluster-version "$v" --zone "$ZONE" --quiet; touch "$stop"; wait $p; rm -f "$stop"; wait_ops; ev upgrade master-$v-version G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(currentMasterVersion)'; }
upgrade_pool(){ local pool=$1 v=$2; shift 2; note upgrade "pool $pool -> $v"; for sc_ns_sel in "$@"; do IFS=: read -r sc ns sel <<<"$sc_ns_sel"; poll_avail "$sc" "$ns" "$sel" & done; retry_busy "$EVID/upgrade.txt" ev upgrade pool-$pool-$v G container clusters upgrade "$CLUSTER" --node-pool "$pool" --cluster-version "$v" --zone "$ZONE" --quiet --async; sleep 20; wait_ops; wait; ev upgrade pool-$pool-$v-nodes K get nodes -o wide; }
# --- shared plants -------------------------------------------------------------------------------
pause_deploy(){ # pause_deploy <name> <replicas> <pod-spec placement line, indented 6>; the default pins to the work pool
  local placement="${3-}"; [ -n "$placement" ] || placement="      nodeSelector: {role: work}"
  K -n scen apply -f - <<Y
apiVersion: apps/v1
kind: Deployment
metadata: {name: $1}
spec:
  replicas: $2
  selector: {matchLabels: {app: $1}}
  template:
    metadata: {labels: {app: $1}}
    spec:
$placement
      containers: [{name: web, image: registry.k8s.io/pause:3.9, resources: {requests: {cpu: 10m, memory: 16Mi}}}]
Y
}
work_node(){ K get nodes -l role=work -o jsonpath="{.items[${1:-0}].metadata.name}"; }
# serving_summary <scenario>: from poll_avail's file, the samples taken and those with no serving replica.
serving_summary(){ local f="$EVID/$1-availability.txt"; local n z; n=$(grep -vc '^#' "$f"); z=$(grep -v '^#' "$f" | grep -Evc '/True/( |$)')
  echo "samples=$n zero_serving=$z"; grep -v '^#' "$f" | grep -Ev '/True/( |$)' | head -5; }
