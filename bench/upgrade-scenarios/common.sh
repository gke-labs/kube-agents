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
require_scenario_cluster(){ local p; p=$(G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(resourceLabels.purpose)' 2>/dev/null) ||
    { echo "refusing: cannot describe $CLUSTER in $ZONE (missing, or its creation failed)" >&2; exit 1; }
  [ "$p" = "$SCENARIO_LABEL" ] || { echo "refusing: $CLUSTER in $ZONE is not labelled purpose=$SCENARIO_LABEL (label: '$p')" >&2; exit 1; }; }
ts(){ date -u +%Y-%m-%dT%H:%M:%SZ; }
in_days(){ date -u -v+"$1"d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "+$1 days" +%Y-%m-%dT%H:%M:%SZ; }   # BSD date, then GNU
# ev returns the command's own exit status, not tee's, so a caller can stop on a failed step.
ev(){ local sc=$1 st=$2; shift 2; local f="$EVID/$sc.txt"; ( echo; echo "## $(ts) [$sc/$st] $*"; "$@" 2>&1; rc=$?; echo "## exit $rc"; exit "$rc" ) | tee -a "$f"; return "${PIPESTATUS[0]}"; }
note(){ local sc=$1; shift; echo "## $(ts) [$sc/note] $*" | tee -a "$EVID/$sc.txt"; }
K(){ kubectl --context "$CTX" --request-timeout=30s "$@"; }
G(){ gcloud "$@" --project "$PROJECT"; }
newest_patch(){ G container get-server-config --location "$ZONE" --format=json | python3 -c "import json,sys;d=json.load(sys.stdin);ch=[c for c in d['channels'] if c['channel']=='$1'][0];print([v for v in ch['validVersions'] if v.startswith('$2.')][0])"; }
# ops_running prints the operations running on the cluster, or the word "unknown" when the list itself failed (a 429, an
# expired token), so no caller reads a failed read as an idle cluster: wait_ops keeps waiting through it, and after
# LIST_FAILURES_MAX failed reads in a row it stops the run rather than guess.
LIST_FAILURES_MAX=40   # 20 minutes of failed reads at wait_ops's 30 s cadence
ops_running(){ local out; out=$(G container operations list --zone "$ZONE" --filter="(targetLink~clusters/${CLUSTER}\$ OR targetLink~clusters/${CLUSTER}/) AND status=RUNNING" --format='value(operationType,name)' 2>/dev/null) || { echo unknown; return; }; echo "$out"; }
wait_ops(){ local n=0 f=0 o; while o=$(ops_running); [ -n "$o" ]; do
    if [ "$o" = unknown ]; then f=$((f+1)); echo "# $(ts) operations list failed ($f); treating the cluster as busy"; [ $f -lt $LIST_FAILURES_MAX ] || { note final "operations list failed $f times in a row; stopping rather than guess whether the upgrade ended"; exit 1; }; else f=0; fi
    sleep 30; n=$((n+1)); [ $((n%10)) -eq 0 ] && echo "# $(ts) still running: $(echo "$o" | tr '\n' ' ')"; done; }
# await_op: an --async upgrade can return before its operation is listed; wait up to OP_APPEAR_TIMEOUT seconds for one.
OP_APPEAR_TIMEOUT=120
await_op(){ local t=0; while [ -z "$(ops_running)" ] && [ $t -lt $OP_APPEAR_TIMEOUT ]; do sleep 5; t=$((t+5)); done; }
# poll_avail <scenario> <namespace> <label> <stop-file>: every 10 s until <stop-file> exists, record each pod's
# node, phase, readiness and deletion stamp (node/phase/ready/deleting), so a moment with zero serving
# replicas is on record even if it lasts 20 s. A terminating pod can still report Ready=True, but a
# Service has already dropped it, so "serving" is Ready=True with an empty deletion stamp. upgrade_pool creates
# the stop file once the pool upgrade and every other operation on the cluster have ended, so an operation that
# was already running (GKE's own default-pool upgrade, say) cannot end the poll before the pool upgrade starts.
poll_avail(){ local sc=$1 ns=$2 sel=$3 stop=$4; local f="$EVID/$sc-availability.txt"; echo "# $(ts) poll start $ns $sel" >>"$f"
  until [ -e "$stop" ] || ! kill -0 $$ 2>/dev/null; do echo "$(ts) $(K -n "$ns" get pods -l "$sel" -o jsonpath='{range .items[*]}{.spec.nodeName}/{.status.phase}/{.status.conditions[?(@.type=="Ready")].status}/{.metadata.deletionTimestamp} {end}' 2>&1)" >>"$f"; sleep 10; done; echo "# $(ts) poll end" >>"$f"; }
# poll_api <scenario> <stop-file>: every 5 s until <stop-file> exists, record whether the API server answers (entry 11).
# Each probe gives up after API_PROBE_TIMEOUT, not K's 30 s, so a control plane that hangs rather than refuses costs one
# short sample instead of stretching the interval; the stamp is taken when the probe returns, so a DOWN line is at most
# that timeout late.
API_PROBE_TIMEOUT=3s
# upgrade_master creates the stop file once its blocking upgrade call returns, so another operation on the cluster
# can neither end the poll before the control-plane upgrade nor keep it running after. It also stops when the
# script that started it is gone ($$ is the script's PID even in this background subshell), so an interrupted run
# leaves no poller behind.
poll_api(){ local sc=$1 stop=$2; local f="$EVID/$sc-api.txt"; echo "# $(ts) api poll start" >>"$f"
  until [ -e "$stop" ] || ! kill -0 $$ 2>/dev/null; do if K --request-timeout=$API_PROBE_TIMEOUT get --raw /version >/dev/null 2>&1; then echo "$(ts) up" >>"$f"; else echo "$(ts) DOWN" >>"$f"; fi; sleep 5; done; echo "# $(ts) api poll end" >>"$f"; }
BUSY_RETRIES=5; BUSY_WAIT=30
MASTER_UPGRADE_TIMEOUT=10800   # seconds; gcloud's own default for a blocking upgrade is 3600, which a slow one can pass
# retry_busy <track> <ev command...>: GKE refuses a cluster change while any other operation runs on the cluster
# ("incompatible operation"): wait for it, retry. <track> is the ev track the command writes to, where the notes go too.
# Returns 1 when the command fails for any other reason, or once every retry was refused, so the caller stops instead
# of recording a change that never happened.
retry_busy(){ local sc=$1 i; shift; for i in $(seq 1 $BUSY_RETRIES); do wait_ops; "$@" && return 0
  tail -4 "$EVID/$sc.txt" | grep -q "incompatible operation" || { note "$sc" "the command failed (see $sc.txt); stopping"; return 1; }; note "$sc" "refused while another operation ran; retry $i"; sleep $BUSY_WAIT; done; note "$sc" "gave up after $BUSY_RETRIES refusals; the command did not run"; return 1; }
upgrade_master(){ local v=$1 stop="$KCFG_DIR/$CLUSTER.$TRACK.master-done"; rm -f "$stop"; note upgrade "master -> $v"; poll_api zonal-api "$stop" & local p=$!; retry_busy upgrade ev upgrade "master-$v" G container clusters upgrade "$CLUSTER" --master --cluster-version "$v" --zone "$ZONE" --quiet --timeout "$MASTER_UPGRADE_TIMEOUT"; local rc=$?; touch "$stop"; wait $p; rm -f "$stop"; [ $rc -eq 0 ] || exit 1; wait_ops; ev upgrade "master-$v-version" G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(currentMasterVersion)'; }
upgrade_pool(){ local pool=$1 v=$2 stop="$KCFG_DIR/$CLUSTER.$TRACK.pool-done"; shift 2; rm -f "$stop"; note upgrade "pool $pool -> $v"; for sc_ns_sel in "$@"; do IFS=: read -r sc ns sel <<<"$sc_ns_sel"; poll_avail "$sc" "$ns" "$sel" "$stop" & done; retry_busy upgrade ev upgrade "pool-$pool-$v" G container clusters upgrade "$CLUSTER" --node-pool "$pool" --cluster-version "$v" --zone "$ZONE" --quiet --async || { touch "$stop"; wait; rm -f "$stop"; exit 1; }; sleep 20; wait_ops; touch "$stop"; wait; rm -f "$stop"
  # The operation's own verdict: GKE reports DONE with the error in statusMessage when a node could not be recreated (a stockout), so the pool can be short a node while the upgrade reads finished.
  ev upgrade "pool-$pool-$v-operation" G container operations list --zone "$ZONE" --filter="targetLink~clusters/$CLUSTER/nodePools/$pool AND operationType=UPGRADE_NODES" --sort-by=~startTime --limit 1 --format='value(status,startTime,endTime,statusMessage)'
  ev upgrade "pool-$pool-$v-nodes" K get nodes -o wide; }
# hold_exclusion: the held scenarios (06h, 08h, 16h) keep GKE from upgrading the cluster while the hazard waits for the
# Recommender. Without the exclusion GKE may upgrade the cluster first, so a failure to add it stops the run.
HOLD_DAYS=2
hold_exclusion(){ has_exclusion hold-recommender && return; retry_busy hold ev hold exclusion G container clusters update "$CLUSTER" --zone "$ZONE" --add-maintenance-exclusion-name hold-recommender --add-maintenance-exclusion-start "$(ts)" --add-maintenance-exclusion-end "$(in_days "$HOLD_DAYS")" --add-maintenance-exclusion-scope no_upgrades --quiet ||
  { note final "precondition not met: the maintenance exclusion was not added, so GKE may upgrade the cluster before the Recommender reads it"; exit 1; }; }
pool_exists(){ G container node-pools describe "$1" --cluster "$CLUSTER" --zone "$ZONE" >/dev/null 2>&1; }
# has_exclusion <name>: the cluster already carries a maintenance exclusion of that name, which GKE refuses to add twice.
has_exclusion(){ G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(maintenancePolicy.window.maintenanceExclusions)' | grep -Eq "(^|;)$1="; }
# run_plant <fn>: run a planting function and set PLANT_FAILED=1 if any command in it fails. The function still runs to
# its end. Bash does not fire the ERR trap inside a function called on the left of || or &&, so call run_plant on a
# line of its own and test PLANT_FAILED after it.
# shellcheck disable=SC2034  # PLANT_FAILED is read by the caller
run_plant(){ PLANT_FAILED=0; set -E; trap 'PLANT_FAILED=1' ERR; "$@"; trap - ERR; set +E; }
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
# since_last_start <file>: the lines after the file's last "# ... start" marker. Every poller appends, so a file carries
# one block per run and a summary must read only the run that just ended.
since_last_start(){ awk '/^# .* start( |$)/{buf=""; next} {buf=buf $0 "\n"} END{printf "%s", buf}' "$1"; }
# serving_summary <scenario>: from poll_avail's file, the samples taken and those with no serving replica. A sample
# is a stamp followed by pod records (node/phase/ready/deleting); a line whose kubectl call failed carries an error
# message instead and is counted as an error, not as a moment with nothing serving.
serving_summary(){ local f="$EVID/$1-availability.txt"; local n z e; n=$(since_last_start "$f" | grep -vc '^#'); e=$(since_last_start "$f" | grep -v '^#' | grep -Evc '^[0-9TZ:-]+( [^ /]*/[^ ]*)* *$')
  z=$(since_last_start "$f" | grep -v '^#' | grep -E '^[0-9TZ:-]+( [^ /]*/[^ ]*)* *$' | grep -Evc '/True/( |$)'); echo "samples=$n zero_serving=$z errors=$e"; since_last_start "$f" | grep -v '^#' | grep -E '^[0-9TZ:-]+( [^ /]*/[^ ]*)* *$' | grep -Ev '/True/( |$)' | head -5; }
