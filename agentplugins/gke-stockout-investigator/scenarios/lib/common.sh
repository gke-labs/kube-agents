# shellcheck shell=bash
#
# Shared plumbing for the GKE Stockout Investigator scenarios.
#
# Each scenario is a thin script that declares what makes it distinct — the workload
# that is wedged, and the autoscaler reason the alert carries — and then calls
# scenario_main. Everything that is the same for every scenario lives here.
#
# A scenario does two things, and both matter:
#
#   1. Applies a real workload to the target cluster that genuinely cannot be
#      scheduled. The alert payload carries only the workload's identity; the
#      diagnosis comes from the agent inspecting live cluster state. Publishing an
#      alert for a workload that does not exist exercises the plumbing but produces a
#      "false signal" verdict, which is a valid scenario (10) and a useless one
#      everywhere else.
#
#   2. Publishes a Cloud Logging-shaped alert naming that workload, so the
#      investigation starts now instead of whenever the autoscaler next emits its
#      periodic noDecisionStatus log.
#
# Scenarios declare (see any NN-*.sh for a worked example):
#
#   SCENARIO_TITLE       one-line summary, printed in the header
#   SCENARIO_RULE        the SKILL.md rule this exercises, or a short label
#   SCENARIO_CONTROLLER  workload name; must match the manifest and the alert
#   SCENARIO_KIND        controller kind for the alert payload (default Deployment)
#   SCENARIO_MESSAGE_ID  autoscaler messageId (default scale.up.error.out.of.resources)
#   SCENARIO_PODS        totalPodCount reported in the alert (default 1)
#   scenario_manifest()  echoes the YAML to apply (optional; scenario 10 has none)
#   scenario_reasons()   echoes a JSON fragment of rejectedMigs/napFailureReasons
#                        (optional) — this is what steers the diagnosis
#   scenario_notes()     echoes free text shown after the run (optional)
#
# Each hook runs in a subshell, and scenario_manifest runs more than once per invocation,
# so nothing a hook assigns reaches the next one. A hook that resolves something at run
# time must wrap the resolver in scenario_memo (below) or it will resolve repeatedly, and
# may not resolve the same way twice.

set -euo pipefail

# --------------------------------------------------------------------- settings
#
# Every one of these is overridable from the environment. The defaults describe the
# development fleet; nothing here is baked into the plugin.

PROJECT_ID="${GCP_PROJECT_ID:-${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}}"
AGENT_NAMESPACE="${AGENT_NAMESPACE:-kubeagents-system}"

# The cluster under investigation. CLUSTER_NAME has to match the adapter's filter
# expression (`resource.labels.cluster_name == '<name>'`) or the alert is dropped
# before it reaches the agent — see templates/agentplugin.yaml.
# Required — validated in preflight rather than here so --help still works. It has to
# match the adapter's filter expression, so there is no safe guess: a wrong name means
# every alert is dropped and the run looks like the agent ignored it.
CLUSTER_NAME="${TARGET_CLUSTER_NAME:-}"
CLUSTER_LOCATION="${TARGET_CLUSTER_LOCATION:-us-east1}"

# Where the agent runs, and where the wedged workload goes. These are different
# clusters: the agent watches the fleet from the management cluster.
# Defaults to the active context, which is right only when you are already pointed at the
# management cluster. Preflight says so explicitly when it is not, because the failure
# otherwise reads as "the cluster is down" rather than "you are looking at the wrong one".
MGMT_CONTEXT_SOURCE="MGMT_CONTEXT"
if [ -z "${MGMT_CONTEXT:-}" ]; then
    MGMT_CONTEXT="$(kubectl config current-context 2>/dev/null || true)"
    MGMT_CONTEXT_SOURCE="the active kubectl context"
fi
PROD_CONTEXT="${PROD_CONTEXT:-gke_${PROJECT_ID}_${CLUSTER_LOCATION}_${CLUSTER_NAME}}"

TOPIC="${STOCKOUT_TOPIC:-gke-stockout-alerts-topic}"
# The adapter route these alerts arrive on. Task titles are "<route> alert <id>",
# which is how the watcher recognises a kanban-dispatched investigation.
ROUTE_NAME="${STOCKOUT_ROUTE:-gke_stockout_alerts}"

# A namespace of our own, created on demand. Not `default`: that one carries a
# `tenant-resource-limits` ResourceQuota capping the namespace at 4 vCPU of requests,
# and a quota rejection happens at pod *creation* — the ReplicaSet reports
# FailedCreate and no Pod object is ever made. That is not a stockout, it is the
# absence of one, and the agent would correctly find nothing to diagnose.
WORKLOAD_NAMESPACE="${WORKLOAD_NAMESPACE:-stockout-scenarios}"
# Only used to print where a remediation PR would land; unset simply drops that hint.
GITOPS_REPO="${GITOPS_REPO:-}"
PLUGIN_NAME="${PLUGIN_NAME:-gkestockoutinvestigator}"

# Zones of the target region, used by scenarios that pin a workload to one zone.
ZONE_A="${CLUSTER_LOCATION}-b"
ZONE_B="${CLUSTER_LOCATION}-c"

# Defaults a scenario may override before calling scenario_main.
SCENARIO_KIND="${SCENARIO_KIND:-Deployment}"
SCENARIO_MESSAGE_ID="${SCENARIO_MESSAGE_ID:-scale.up.error.out.of.resources}"
SCENARIO_PODS="${SCENARIO_PODS:-1}"

# Flags, set by _parse_args.
DO_CLEANUP=0
DO_TEARDOWN_AFTER=0
SKIP_WORKLOAD=0
SKIP_ALERT=0
VIA_SINK=0
KEEP_DEDUP=0
DRY_RUN=0
EMIT_MANIFEST=""
WATCH_TIMEOUT="${WATCH_TIMEOUT:-600}"

SCENARIO_SLUG="$(basename "${BASH_SOURCE[1]:-scenario}" .sh)"
RUN_ID=""
PLATFORM_POD=""

# ----------------------------------------------------------------------- output

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_DIM=$'\033[2m'; C_RED=$'\033[31m'; C_GRN=$'\033[32m'
    C_YEL=$'\033[33m'; C_BLU=$'\033[34m'; C_OFF=$'\033[0m'
else
    C_DIM=""; C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_OFF=""
fi

step() { printf '\n%s==>%s %s\n' "$C_BLU" "$C_OFF" "$*"; }
info() { printf '    %s\n' "$*"; }
dim()  { printf '    %s%s%s\n' "$C_DIM" "$*" "$C_OFF"; }
ok()   { printf '    %s✓%s %s\n' "$C_GRN" "$C_OFF" "$*"; }
warn() { printf '    %s!%s %s\n' "$C_YEL" "$C_OFF" "$*"; }
die()  { printf '\n%serror:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

kmgmt() { kubectl --context="$MGMT_CONTEXT" --request-timeout=30s "$@"; }
kprod() { kubectl --context="$PROD_CONTEXT" --request-timeout=30s "$@"; }

# ------------------------------------------------------------- resolver memoisation
#
# Both scenario hooks run in subshells, and more than once: scenario_manifest is called
# separately for the infra and workload halves of the manifest, scenario_reasons once for
# the alert. So a variable a hook assigns is invisible to every other call, and a hook
# that resolves something by asking the API — which large shape is scarce, which family a
# zone does not offer — repeats the query each time.
#
# Repeating it is not merely wasteful. Two calls can answer differently, and then the
# ComputeClass pins one machine while the alert names another, or the workload's
# nodeSelector names a zone the ComputeClass was not built for. Both read as a malformed
# scenario rather than the capacity failure being staged.
#
# scenario_memo runs the resolver once per run and replays its answer to later calls.
#
#   scenario_memo <key> <resolver-fn> <var>...
#
# The named variables are what the resolver sets; they are cached after the first call
# and restored, unchanged, on every later one.
SCENARIO_RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/stockout-scenario.XXXXXX")"
trap 'rm -rf "$SCENARIO_RUN_DIR"' EXIT

scenario_memo() {
    local key="$1" resolver="$2"; shift 2
    local cache="${SCENARIO_RUN_DIR}/memo.${key}" var

    if [ -f "$cache" ]; then
        # shellcheck disable=SC1090 # generated by the write below, one VAR=value per line
        . "$cache"
        return 0
    fi

    "$resolver"

    # %q so a value with a space or a quote survives the round trip. Written last, and
    # only on success: a resolver that dies leaves no cache, so the next call retries
    # rather than replaying a half-built answer.
    : > "$cache"
    for var in "$@"; do
        printf '%s=%q\n' "$var" "${!var}" >> "$cache"
    done
}

# ------------------------------------------------------------------------- args

usage() {
    cat <<EOF
${SCENARIO_SLUG} — ${SCENARIO_TITLE:-stockout scenario}

  Deploys a workload that cannot be scheduled on ${CLUSTER_NAME:-<TARGET_CLUSTER_NAME>}, then publishes the
  matching scale-up failure alert so the Platform Agent investigates it now.

Usage: ./${SCENARIO_SLUG}.sh [options]

  --cleanup         Remove this scenario's workload and exit. Run this when done;
                    a wedged workload keeps generating real autoscaler alerts.
  --teardown        Run the scenario, then clean up once the watch finishes.
  --no-workload     Publish the alert only, without deploying anything. The agent
                    should conclude "false signal" — that is scenario 10's whole point.
  --no-alert        Deploy the workload but publish nothing: wait for the autoscaler's
                    own alert. Slower, and the only mode that shows whether one real
                    incident produces exactly one investigation — an injected alert and
                    an organic one describe the same failure differently (Deployment vs
                    ReplicaSet controller name) and so do not deduplicate against
                    each other.
  --no-wait         Publish and exit without watching for the investigation.
  --via-sink        Publish through Cloud Logging (log_id "test-stockout") so the real
                    log sink routes it, instead of writing to the topic directly.
                    Slower and needs the sink to exist, but exercises one more hop.
  --keep-dedup      Do not clear the adapter's dedup registry first. Without this the
                    registry is cleared, because a repeat run for the same
                    cluster+namespace+controller is otherwise silently dropped.
  --watch-timeout N Seconds to watch for the investigation (default ${WATCH_TIMEOUT}).
  --emit-manifest F Write the scenario's manifest to F ("-" for stdout) and exit, ready
                    for `kubectl apply -f`. Includes the namespace and pins the workload
                    to it, so the file stands alone — useful for launching a scenario by
                    hand, or for reading what a scenario actually deploys.
  --dry-run         Print the manifest and the alert payload; change nothing.
  -h, --help        This.

Environment: GCP_PROJECT_ID, TARGET_CLUSTER_NAME, TARGET_CLUSTER_LOCATION,
MGMT_CONTEXT, PROD_CONTEXT, AGENT_NAMESPACE, WORKLOAD_NAMESPACE, STOCKOUT_TOPIC.
EOF
}

_parse_args() {
    local no_wait=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --cleanup)       DO_CLEANUP=1 ;;
            --teardown)      DO_TEARDOWN_AFTER=1 ;;
            --no-workload)   SKIP_WORKLOAD=1 ;;
            --no-alert)      SKIP_ALERT=1 ;;
            --no-wait)       no_wait=1 ;;
            --via-sink)      VIA_SINK=1 ;;
            --keep-dedup)    KEEP_DEDUP=1 ;;
            --watch-timeout) shift; WATCH_TIMEOUT="${1:?--watch-timeout needs a value}" ;;
            --emit-manifest) shift; EMIT_MANIFEST="${1:?--emit-manifest needs a path, or - for stdout}" ;;
            --dry-run)       DRY_RUN=1 ;;
            -h|--help)       usage; exit 0 ;;
            *)               usage >&2; die "unknown option: $1" ;;
        esac
        shift
    done
    [ "$no_wait" -eq 1 ] && WATCH_TIMEOUT=0
    return 0
}

# -------------------------------------------------------------------- preflight

# Resolve the gateway pod once; several steps need to exec into it.
_resolve_platform_pod() {
    PLATFORM_POD="$(kmgmt get pods -n "$AGENT_NAMESPACE" \
        -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
        | grep '^platform-agent-gateway-' | head -1 || true)"
    [ -n "$PLATFORM_POD" ] || die "no platform-agent-gateway pod in ${AGENT_NAMESPACE} on ${MGMT_CONTEXT}"
}

# The failure modes below are the ones that have actually cost time: an alert that is
# filtered out, a plugin that mounted but never loaded, and a dedup hit that swallows
# the alert with nothing in the logs to say so.
preflight() {
    step "Preflight"

    [ -n "$PROJECT_ID" ] || die "no GCP project; set GCP_PROJECT_ID"
    [ -n "$CLUSTER_NAME" ] || die "set TARGET_CLUSTER_NAME — it must match the cluster the plugin was installed for, or every alert is filtered out"
    [ -n "$MGMT_CONTEXT" ] || die "no kubectl context; set MGMT_CONTEXT to the cluster running the agent"
    command -v gcloud >/dev/null || die "gcloud not on PATH"
    command -v kubectl >/dev/null || die "kubectl not on PATH"
    command -v python3 >/dev/null || die "python3 not on PATH"
    dim "project ${PROJECT_ID}, cluster ${CLUSTER_NAME} (${CLUSTER_LOCATION})"

    kmgmt get ns "$AGENT_NAMESPACE" >/dev/null 2>&1 || die \
"no namespace ${AGENT_NAMESPACE} in context ${MGMT_CONTEXT} (from ${MGMT_CONTEXT_SOURCE}).
       The agent runs on the MANAGEMENT cluster, which is usually not the cluster under
       investigation — set MGMT_CONTEXT to it."
    ok "management cluster reachable"

    if [ "$SKIP_WORKLOAD" -eq 0 ]; then
        # The namespace itself is created later; this only proves the cluster answers.
        kprod get ns >/dev/null 2>&1 \
            || die "cannot reach ${PROD_CONTEXT}"
        ok "target cluster reachable"
    fi

    # A plugin can be mounted and still not be loaded by the profile that runs the
    # skill. Phase Ready plus a resolvable skill name is what actually matters.
    local phase
    phase="$(kmgmt get agentplugin "$PLUGIN_NAME" -n "$AGENT_NAMESPACE" \
        -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    [ "$phase" = "Ready" ] || die "AgentPlugin ${PLUGIN_NAME} is '${phase:-missing}', not Ready"
    ok "AgentPlugin ${PLUGIN_NAME} is Ready"

    _resolve_platform_pod
    dim "gateway pod ${PLATFORM_POD}"

    # $HERMES_HOME is /opt/data in the shipped image, but read it rather than assume:
    # a plugin that mounted at the wrong path is exactly the failure this catches.
    if kmgmt exec -i=false -n "$AGENT_NAMESPACE" "$PLATFORM_POD" -c platform-agent -- \
        sh -c 'test -f "${HERMES_HOME:-/opt/data}/profiles/platform/plugins/'"${PLUGIN_NAME}"'/skills/gke-stockout-investigator/SKILL.md"' 2>/dev/null
    then
        ok "skill resolves in the platform profile"
    else
        warn "SKILL.md not found under the platform profile's plugins directory"
        warn "the investigation would run without it — check the plugin mount"
    fi

    gcloud pubsub topics describe "$TOPIC" --project="$PROJECT_ID" >/dev/null 2>&1 \
        || die "Pub/Sub topic ${TOPIC} not found in ${PROJECT_ID}"
    ok "topic ${TOPIC} exists"
}

# The adapter dedups on cluster + namespace + controller for 24h. Re-running a
# scenario is the normal case here, so clear the registry unless asked not to.
clear_dedup() {
    [ "$KEEP_DEDUP" -eq 1 ] && { dim "keeping dedup registry (--keep-dedup)"; return 0; }
    kmgmt exec -i=false -n "$AGENT_NAMESPACE" "$PLATFORM_POD" -c platform-agent -- \
        rm -f /opt/data/pubsub_registry.json >/dev/null 2>&1 || true
    ok "cleared the adapter dedup registry"
}

# ------------------------------------------------------------------- the payload

_iso_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# The inner document: what the autoscaler itself logs. When published through the
# sink this becomes jsonPayload verbatim, which is what the adapter's dedup paths
# (jsonPayload.noDecisionStatus...) and filter clause 2 are written against.
_inner_payload() {
    local reasons="" now
    now="$(_iso_now)"
    if declare -F scenario_reasons >/dev/null; then
        reasons="$(scenario_reasons)"
    fi
    cat <<JSON
{
  "messageId": "${SCENARIO_MESSAGE_ID}",
  "noDecisionStatus": {
    "measureTime": "${now}",
    "noScaleUp": {
      "unhandledPodGroupsTotalCount": ${SCENARIO_PODS},
      "unhandledPodGroups": [
        {
          "podGroup": {
            "samplePod": {
              "name": "${SCENARIO_CONTROLLER}-${RUN_ID}",
              "namespace": "${WORKLOAD_NAMESPACE}",
              "controller": {
                "apiVersion": "apps/v1",
                "kind": "${SCENARIO_KIND}",
                "name": "${SCENARIO_CONTROLLER}"
              }
            },
            "totalPodCount": ${SCENARIO_PODS}
          }${reasons:+,
          ${reasons}}
        }
      ]
    }
  },
  "resource": {
    "type": "k8s_cluster",
    "labels": {
      "cluster_name": "${CLUSTER_NAME}",
      "location": "${CLUSTER_LOCATION}",
      "project_id": "${PROJECT_ID}"
    }
  }
}
JSON
}

# The full LogEntry, as the sink would deliver it to the topic. Used for direct
# publishing so that top-level resource.labels.cluster_name — filter clause 1 and the
# first dedup field — resolves the same way it does in production.
# The payload arrives as argv, not stdin: a `python3 - <<PY` heredoc is itself read
# from stdin, so anything piped in would be swallowed by the interpreter.
_log_entry() {
    python3 -c '
import json, sys
inner = json.loads(sys.argv[1])
cluster, location, project, run_id, slug = sys.argv[2:7]
print(json.dumps({
    "insertId": f"{slug}-{run_id}",
    "logName": f"projects/{project}/logs/test-stockout",
    "timestamp": inner["noDecisionStatus"]["measureTime"],
    "severity": "WARNING",
    "resource": {
        "type": "k8s_cluster",
        "labels": {"cluster_name": cluster, "location": location, "project_id": project},
    },
    "jsonPayload": inner,
}, indent=2))
' "$1" "$CLUSTER_NAME" "$CLUSTER_LOCATION" "$PROJECT_ID" "$RUN_ID" "$SCENARIO_SLUG"
}

publish_alert() {
    step "Publishing the alert"

    local inner entry
    inner="$(_inner_payload)"
    # Fail here rather than let gcloud reject a malformed message with a vaguer error.
    echo "$inner" | python3 -c 'import json,sys; json.load(sys.stdin)' \
        || die "scenario produced invalid JSON (check scenario_reasons)"

    if [ "$VIA_SINK" -eq 1 ]; then
        dim "via Cloud Logging → sink → ${TOPIC}"
        if [ "$DRY_RUN" -eq 1 ]; then
            printf '%s\n' "$inner"; return 0
        fi
        gcloud logging write test-stockout "$inner" \
            --payload-type=json --project="$PROJECT_ID" --severity=WARNING >/dev/null
        ok "written to log test-stockout; the sink forwards within ~30s"
    else
        entry="$(_log_entry "$inner")"
        dim "directly to topic ${TOPIC}"
        if [ "$DRY_RUN" -eq 1 ]; then
            printf '%s\n' "$entry"; return 0
        fi
        gcloud pubsub topics publish "$TOPIC" \
            --project="$PROJECT_ID" --message="$entry" >/dev/null
        ok "published as insertId ${SCENARIO_SLUG}-${RUN_ID}"
    fi
}

# ------------------------------------------------------------------- the workload

# Cluster-scoped prerequisites go in before the workload that references them: on
# Autopilot the admission webhook rejects a pod naming a ComputeClass that is not yet
# in its allowed list, and that list lags creation by a few seconds.
_manifest_part() {
    scenario_manifest | python3 -c '
import sys, yaml
want = sys.argv[1]
INFRA = {"ComputeClass", "StorageClass", "PriorityClass", "ResourceQuota"}
docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
sel = [d for d in docs if (d.get("kind") in INFRA) == (want == "infra")]
if sel:
    print(yaml.safe_dump_all(sel, sort_keys=False), end="")
' "$1"
}


emit_manifest() {
    # A standalone, appliable file: namespace first, then the cluster-scoped infra, then
    # the workload with its namespace stamped in. `--dry-run` prints the same YAML but
    # interleaved with the harness's own banners, which is fine to read and useless to
    # pipe.
    declare -F scenario_manifest >/dev/null || die "this scenario has no workload to emit"

    local out="$EMIT_MANIFEST"
    {
        printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: %s\n' "$WORKLOAD_NAMESPACE"
        local infra work
        infra="$(_manifest_part infra)"
        # printf '%s\n', not '%s': $( ) strips the trailing newline, and without it the
        # next separator lands on the last line as `enabled: true---`, which YAML reads as
        # a scalar and silently swallows the following document.
        [ -n "$infra" ] && { printf -- '---\n'; printf '%s\n' "$infra"; }
        work="$(_manifest_part workload)"
        [ -n "$work" ] && {
            printf -- '---\n'
            printf '%s\n' "$work" | python3 -c '
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
for d in docs:
    d.setdefault("metadata", {})["namespace"] = sys.argv[1]
print(yaml.safe_dump_all(docs, sort_keys=False), end="")
' "$WORKLOAD_NAMESPACE"
        }
    } > >(if [ "$out" = "-" ]; then cat; else cat > "$out"; fi)
    wait

    if [ "$out" != "-" ]; then
        ok "wrote ${out}"
        dim "apply it with:   kubectl --context=${PROD_CONTEXT} apply -f ${out}"
        dim "remove it with:  kubectl --context=${PROD_CONTEXT} delete -f ${out}"
        dim "the alert follows on its own once the pods sit Pending, or publish one with --no-workload"
    fi
}

apply_workload() {
    declare -F scenario_manifest >/dev/null || return 0
    [ "$SKIP_WORKLOAD" -eq 1 ] && { dim "skipping workload (--no-workload)"; return 0; }

    step "Deploying the wedged workload"
    if [ "$DRY_RUN" -eq 1 ]; then
        scenario_manifest; return 0
    fi

    local infra work attempt waited=0
    # A namespace that is still Terminating from the previous scenario's cleanup cannot be
    # recreated, and every object applied into it is rejected — which surfaces much later
    # as "namespaces not found" after the admission retries have burned themselves out.
    # Running the scenarios back to back is the normal case, so wait for the old one to go.
    while kprod get ns "$WORKLOAD_NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null | grep -q Terminating; do
        [ "$waited" -eq 0 ] && dim "waiting for the previous ${WORKLOAD_NAMESPACE} namespace to finish terminating"
        sleep 5
        waited=$((waited + 5))
        [ "$waited" -ge 180 ] && die "namespace ${WORKLOAD_NAMESPACE} stuck Terminating after ${waited}s"
    done
    kprod create namespace "$WORKLOAD_NAMESPACE" --dry-run=client -o yaml \
        | kprod apply -f - >/dev/null 2>&1 || true

    infra="$(_manifest_part infra)"
    if [ -n "$infra" ]; then
        printf '%s' "$infra" | kprod apply -f - \
            || die "could not apply the scenario's ComputeClass/StorageClass"
        sleep 5
    fi

    # Retry rather than fail: the webhook's view of a just-created ComputeClass can
    # take a few seconds to catch up, and that shows up as an admission denial.
    work="$(_manifest_part workload)"
    for attempt in 1 2 3 4 5; do
        if printf '%s' "$work" | kprod apply -n "$WORKLOAD_NAMESPACE" -f - 2>/dev/null; then
            break
        fi
        [ "$attempt" -eq 5 ] && {
            printf '%s' "$work" | kprod apply -n "$WORKLOAD_NAMESPACE" -f - || true
            die "could not apply the scenario workload"
        }
        dim "admission not ready yet, retrying (${attempt}/5)"
        sleep 10
    done

    # Give the scheduler long enough to admit defeat, so the agent has events to read
    # by the time it looks. Autopilot marks the pod Pending and records the
    # provisioning failure within a few tens of seconds.
    info "waiting up to 90s for the pods to go Pending with a scheduling failure"
    local i pending blocked
    for i in $(seq 1 18); do
        pending="$(kprod get pods -n "$WORKLOAD_NAMESPACE" \
            -l "scenario=${SCENARIO_SLUG}" \
            -o jsonpath='{range .items[?(@.status.phase=="Pending")]}{.metadata.name}{"\n"}{end}' \
            2>/dev/null | grep -c . || true)"
        if [ "${pending:-0}" -gt 0 ]; then
            ok "${pending} pod(s) Pending — the cluster has a real failure to diagnose"
            kprod get events -n "$WORKLOAD_NAMESPACE" \
                --field-selector reason=FailedScheduling 2>/dev/null \
                | tail -3 | sed 's/^/    /' || true
            return 0
        fi

        # No Pending pod can mean two very different things. If the ReplicaSet cannot
        # even create the Pod — a quota or admission rejection — there is nothing
        # unschedulable and therefore nothing for the agent to find. Say so, rather
        # than let it look like a slow scheduler.
        blocked="$(kprod get events -n "$WORKLOAD_NAMESPACE" \
            --field-selector reason=FailedCreate -o jsonpath='{.items[-1:].message}' \
            2>/dev/null || true)"
        if [ -n "$blocked" ]; then
            warn "pods are being rejected at admission, not left Pending:"
            printf '      %s\n' "$(printf '%s' "$blocked" | cut -c1-160)"
            die "no unschedulable pod will exist, so there is no stockout to investigate"
        fi
        sleep 5
    done
    warn "no Pending pods after 90s; publishing anyway — the agent may call it a false signal"
}

cleanup_workload() {
    step "Cleaning up"
    if ! declare -F scenario_manifest >/dev/null; then
        dim "nothing to remove"; return 0
    fi
    # Reverse of apply: the workload goes first, so nothing still references the
    # ComputeClass when it is removed.
    local infra work
    work="$(_manifest_part workload)"
    infra="$(_manifest_part infra)"
    [ -n "$work" ] && printf '%s' "$work" \
        | kprod delete -n "$WORKLOAD_NAMESPACE" -f - --ignore-not-found --wait=false 2>/dev/null || true
    [ -n "$infra" ] && printf '%s' "$infra" \
        | kprod delete -f - --ignore-not-found --wait=false 2>/dev/null || true

    # StatefulSet PVCs outlive the set, and a Hyperdisk left behind is a running cost.
    kprod delete pvc -n "$WORKLOAD_NAMESPACE" -l "scenario=${SCENARIO_SLUG}" \
        --ignore-not-found --wait=false 2>/dev/null || true

    ok "removed ${SCENARIO_SLUG} resources from ${CLUSTER_NAME}"

    # Drop the namespace once the last scenario has left it, but never touch one we
    # did not create — someone may have pointed WORKLOAD_NAMESPACE at a real one.
    if [ "$WORKLOAD_NAMESPACE" = "stockout-scenarios" ]; then
        # Count controllers, not `all`: the delete above does not wait, so pods and
        # replica sets from the scenario just removed are still terminating and would
        # make an empty namespace look occupied.
        local left i
        for i in 1 2 3; do
            left="$(kprod get deploy,statefulset,job -n "$WORKLOAD_NAMESPACE" \
                -o name 2>/dev/null | grep -c . || true)"
            [ "${left:-0}" -eq 0 ] && break
            sleep 3
        done
        if [ "${left:-0}" -eq 0 ]; then
            if kprod get namespace "$WORKLOAD_NAMESPACE" >/dev/null 2>&1; then
                kprod delete namespace "$WORKLOAD_NAMESPACE" --ignore-not-found \
                    --wait=false >/dev/null 2>&1 || true
                dim "removed the now-empty ${WORKLOAD_NAMESPACE} namespace"
            fi
        else
            dim "${left} workload(s) from other scenarios remain; keeping ${WORKLOAD_NAMESPACE}"
        fi
    fi
    dim "the cluster scales back to zero on its own"
}

# ---------------------------------------------------------------------- watching

_sessions_json() {
    kmgmt exec -i=false -n "$AGENT_NAMESPACE" "$PLATFORM_POD" -c platform-agent -- \
        python3 -c "
import os, requests
key = os.environ.get('API_SERVER_KEY', '')
print(requests.get('http://127.0.0.1:8642/api/sessions?limit=50',
                   headers={'Authorization': 'Bearer ' + key}, timeout=10).text)
" 2>/dev/null || echo '{}'
}

# Sessions started before we published are from earlier runs; only a newer one is ours.
_task_after() {
    # The kanban twin of _session_after, for routes with `dispatch: kanban`. Those file
    # the alert as a task owned by the specialist profile and never create a gateway
    # session, so watching only for sessions reports "nothing happened" while the
    # investigation runs to completion.
    local since="$1"
    kmgmt exec -i=false -n "$AGENT_NAMESPACE" "$PLATFORM_POD" -c platform-agent -- \
        env HOME=/tmp hermes kanban ls --json 2>/dev/null | python3 -c "
import json, sys
since = float('$since')
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
tasks = data.get('tasks') if isinstance(data, dict) else data
for t in sorted(tasks or [], key=lambda x: x.get('created_at') or 0, reverse=True):
    if (t.get('created_at') or 0) >= since and str(t.get('title','')).startswith('${ROUTE_NAME}'):
        print('%s\t%s\t%s' % (t.get('id',''), t.get('status') or 'running',
                              (t.get('title') or '')[:70]))
        break
"
}

_session_after() {
    local since="$1"
    _sessions_json | python3 -c "
import json, sys
since = float('$since')
try:
    data = json.load(sys.stdin).get('data', [])
except Exception:
    sys.exit(0)
for s in data:
    if (s.get('started_at') or 0) >= since:
        print('%s\t%s\t%s' % (s.get('id',''), s.get('end_reason') or 'running',
                              (s.get('title') or '')[:70]))
        break
"
}


_workload_still_pending() {
    # Did the workload schedule after all? Capacity is a moving target: a shape that was
    # unobtainable when the run started can appear minutes later, and then the autoscaler
    # has nothing to complain about and no alert is ever emitted. Without this check the
    # run waits out its whole timeout and reports "no investigation", which reads as a
    # broken agent rather than a cluster that healed itself.
    [ "$SKIP_WORKLOAD" -eq 1 ] && return 0
    # Unscheduled, not merely Pending: a pod keeps phase Pending after it lands on a node,
    # while the image pulls or a volume attaches. Counting those as "still stuck" is how a
    # successful scale-up looked like an ongoing stockout. The distinction is spec.nodeName.
    local unscheduled
    unscheduled="$(kprod get pods -n "$WORKLOAD_NAMESPACE" -l "scenario=${SCENARIO_SLUG}" \
        -o jsonpath='{range .items[?(@.status.phase=="Pending")]}{.spec.nodeName}{"\n"}{end}' \
        2>/dev/null | grep -c '^$' || true)"
    [ "${unscheduled:-0}" -gt 0 ]
}

watch_investigation() {
    local since="$1"
    [ "${WATCH_TIMEOUT:-0}" -eq 0 ] && { step "Not waiting (--no-wait)"; return 0; }

    step "Watching for the investigation (up to ${WATCH_TIMEOUT}s)"
    info "the agent notifies chat, checks for duplicate PRs, then diagnoses"

    local waited=0 found="" line kind
    while [ "$waited" -lt "$WATCH_TIMEOUT" ]; do
        # Either dispatch mode counts: a gateway session (`dispatch: api`) or a board task
        # (`dispatch: kanban`). Checking both keeps one watcher honest for both routes.
        kind="session"
        line="$(_session_after "$since" || true)"
        if [ -z "$line" ]; then
            kind="task"
            line="$(_task_after "$since" || true)"
        fi
        if [ -n "$line" ]; then
            if [ -z "$found" ]; then
                found=1
                ok "investigation started: $(printf '%s' "$line" | cut -f3)"
                dim "$kind $(printf '%s' "$line" | cut -f1)"
            fi
            case "$(printf '%s' "$line" | cut -f2)" in
                running|ready|claimed) ;;
                *)  ok "$kind finished ($(printf '%s' "$line" | cut -f2))"
                    break ;;
            esac
        fi
        # Only worth checking while nothing has started yet: once an investigation is
        # under way it should finish even if the workload schedules in the meantime.
        if [ -z "$found" ] && ! _workload_still_pending; then
            printf '                    \r'
            warn "the workload scheduled after all — no scale-up failure, so no alert and no investigation"
            dim "capacity for this shape appeared while the run was waiting; that is a property of"
            dim "the region today, not a fault in the agent. Re-run later, or pick a scarcer shape."
            return 0
        fi
        sleep 10
        waited=$((waited + 10))
        printf '    %s%ds%s\r' "$C_DIM" "$waited" "$C_OFF"
    done
    printf '                    \r'

    [ -n "$found" ] || warn "no new session or board task after ${WATCH_TIMEOUT}s — see the hints below"
}

# ------------------------------------------------------------------------ report

_report() {
    step "Where to look"
    cat <<EOF
    Gateway log (alert ingress, filter and dedup decisions):
      kubectl --context=${MGMT_CONTEXT} logs -n ${AGENT_NAMESPACE} \\
        deployment/platform-agent-gateway -c platform-agent --tail=100 | grep gke_stockout

    Pending pods and their scheduling failures:
      kubectl --context=${PROD_CONTEXT} get pods -n ${WORKLOAD_NAMESPACE} -l scenario=${SCENARIO_SLUG}
      kubectl --context=${PROD_CONTEXT} get events -n ${WORKLOAD_NAMESPACE} --field-selector reason=FailedScheduling

    The remediation PR, if the agent proposed one:
      gh pr list --repo ${GITOPS_REPO:-<your GitOps repo>} --limit 5

    Tear this scenario down when you are finished:
      ./${SCENARIO_SLUG}.sh --cleanup
EOF
    if declare -F scenario_notes >/dev/null; then
        step "What this scenario is testing"
        scenario_notes | sed 's/^/    /'
    fi
}

# -------------------------------------------------------------------------- main

scenario_main() {
    _parse_args "$@"
    RUN_ID="$(date +%s)"

    if [ -n "$EMIT_MANIFEST" ]; then
        # Before the cleanup and dry-run branches, and before preflight: writing the
        # manifest out needs the project (the shape discovery queries it) but never
        # touches the cluster.
        [ -n "$PROJECT_ID" ] || die "no GCP project; set GCP_PROJECT_ID"
        [ -n "$CLUSTER_NAME" ] || die "set TARGET_CLUSTER_NAME"
        emit_manifest
        exit 0
    fi


    printf '%s\n' "════════════════════════════════════════════════════════════════════"
    printf ' %s\n' "${SCENARIO_SLUG}: ${SCENARIO_TITLE}"
    printf ' %s%s%s\n' "$C_DIM" "${SCENARIO_RULE}" "$C_OFF"
    printf '%s\n' "════════════════════════════════════════════════════════════════════"

    if [ "$DO_CLEANUP" -eq 1 ]; then
        [ -n "$PROJECT_ID" ] || die "no GCP project; set GCP_PROJECT_ID"
        [ -n "$CLUSTER_NAME" ] || die "set TARGET_CLUSTER_NAME — it must match the cluster the plugin was installed for, or every alert is filtered out"
        [ -n "$MGMT_CONTEXT" ] || die "no kubectl context; set MGMT_CONTEXT to the cluster running the agent"
        cleanup_workload
        exit 0
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        step "Manifest"
        declare -F scenario_manifest >/dev/null && scenario_manifest || dim "none"
        publish_alert
        exit 0
    fi

    preflight
    clear_dedup
    apply_workload

    local since
    since="$(date +%s)"
    if [ "$SKIP_ALERT" -eq 1 ]; then
        step "Not publishing an alert (--no-alert)"
        dim "the wedged workload makes the autoscaler emit the real thing; this waits for it"
        dim "rather than injecting one, so the run exercises exactly what production sees —"
        dim "including whether one incident produces exactly one investigation"
    else
        publish_alert
    fi
    watch_investigation "$since"
    _report

    [ "$DO_TEARDOWN_AFTER" -eq 1 ] && cleanup_workload
    return 0
}
