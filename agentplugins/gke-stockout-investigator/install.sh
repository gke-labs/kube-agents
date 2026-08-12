#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# GKE Stockout Investigator Extension Installation Script
# Ensures GCP APIs, IAM permissions (least privilege), PubSub topic/subscription,
# Log Sink, Platform Agent IAM bindings, and Helm AgentPlugin deployment.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# shellcheck source=../lib/plugin_image.sh
. "${REPO_ROOT}/agentplugins/lib/plugin_image.sh"

# Resolve GCP Project ID
PROJECT_ID="${GCP_PROJECT_ID:-${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo "")}}"
if [ -z "$PROJECT_ID" ]; then
    echo "Error: Could not determine GCP Project ID. Please set GCP_PROJECT_ID or PROJECT_ID environment variable."
    exit 1
fi

# Resolve Kubectl Context (User Rule 15: Always use dedicated kubectl context)
CONTEXT="${KUBECTL_CONTEXT:-$(kubectl config current-context 2>/dev/null || echo "")}"
if [ -z "$CONTEXT" ]; then
    echo "Error: No kubectl context found. Set KUBECTL_CONTEXT environment variable."
    exit 1
fi

# Overridable, and spelled the same way as in the pubsub-platform installer: the two are
# installed as a pair, and a namespace honoured by one of them puts the route and the
# adapter that serves it on different agents.
NAMESPACE="${HERMES_NAMESPACE:-kubeagents-system}"
# Helm release, AgentPlugin name and Hermes plugin module, all one identifier: the CRD
# pattern is ^[a-z][a-z0-9]*$, so the hyphenated chart name cannot be used here.
RELEASE="gkestockoutinvestigator"
# The PlatformAgent this plugin attaches to. One value drives every place the agent is
# named: the CR's agentRef (passed to helm in step 6, overriding values.yaml), the registry
# the image resolution copies from, the service account step 4 grants roles to, and the
# tuning patch in step 6b. The default matches values.yaml so an unset AGENT_REF changes
# nothing.
AGENT_REF="${AGENT_REF:-platform-agent}"
# Required: it is compiled into the alert filter, so a wrong value drops every alert
# without an error anywhere.
CLUSTER_NAME="${TARGET_CLUSTER_NAME:-}"
if [ -z "$CLUSTER_NAME" ]; then
    echo "Error: set TARGET_CLUSTER_NAME to the GKE cluster whose stockout alerts this should investigate."
    exit 1
fi
# Overridable so a second deployment in the same project — a demo fleet alongside a
# development one — gets its own ingress rather than sharing these. All three names must
# match the ones the chart templates into the subscription block, which is why the same
# variables are passed to helm below. The sink included: its startup presence check in the
# adapter is disabled for now, but the name can only ever come from here, so it is still
# passed through rather than left to be re-plumbed when the check comes back.
TOPIC="${STOCKOUT_TOPIC:-gke-stockout-alerts-topic}"
SUBSCRIPTION="${STOCKOUT_SUBSCRIPTION:-gke-stockout-alerts-sub}"
SINK_NAME="${STOCKOUT_SINK:-gke-stockout-alerts-sink}"
# Scoped to CLUSTER_NAME, and not only because it is tidier. A log sink is project-wide:
# an unscoped one exports every cluster's autoscaler failures to this topic, so two
# deployments in one project each receive the other's alerts. The adapter's own filter
# drops them, silently, which is the problem — the dedup registry and the delivery costs
# are already paid by then, and a misconfigured filter looks exactly like a quiet fleet.
#
# Both spellings of the label are matched. Real autoscaler entries carry the monitored
# resource, so the cluster name is at `resource.labels`; the synthetic `test-stockout`
# entries the scenarios write are `global`-resourced, and carry it inside the payload.
FILTER="(log_id(\"test-stockout\") OR log_id(\"container.googleapis.com/cluster-autoscaler-visibility\")) AND (resource.labels.cluster_name=\"${CLUSTER_NAME}\" OR jsonPayload.resource.labels.cluster_name=\"${CLUSTER_NAME}\") AND (jsonPayload.messageId:(\"scale.up.error.out.of.resources\" OR \"scale.up.error.quota.exceeded\" OR \"scale.up.error.ip.space.exhausted\" OR \"scale.up.no.scale.up\") OR jsonPayload.noDecisionStatus.noScaleUp:* OR jsonPayload.resultInfo.results.errorMsg.messageId:(\"scale.up.error.out.of.resources\" OR \"scale.up.error.quota.exceeded\" OR \"scale.up.error.ip.space.exhausted\" OR \"scale.up.no.scale.up\"))"

echo "============================================================"
echo "Installing GKE Stockout Investigator Extension Module"
echo "GCP Project ID:  ${PROJECT_ID}"
echo "Kubectl Context: ${CONTEXT}"
echo "Target Cluster:  ${CLUSTER_NAME}"
echo "Namespace:       ${NAMESPACE}"
echo "Agent:           ${AGENT_REF}"
echo "PubSub Topic:    ${TOPIC}"
echo "Subscription:    ${SUBSCRIPTION}"
echo "Sink:            ${SINK_NAME}"
echo "============================================================"

# Work the image reference out before step 1 provisions anything. This is where a missing
# project, an unreadable source tree or an absent image builder is caught. Left until
# step 5, those failures land after the APIs, the topic, the subscription, the log sink
# and four IAM bindings already exist, so the run has to be unpicked by hand. The context
# and namespace go in so the reference lands in the registry the agent already pulls
# from. It prints the reference it settled on — which is why it runs after the banner
# and not before it. See agentplugins/lib/plugin_image.sh.
plugin_image_resolve "gke-stockout-investigator" "$PROJECT_ID" "$SCRIPT_DIR" \
    "${SCRIPT_DIR}/files" "$CONTEXT" "$NAMESPACE" "$AGENT_REF"
IMAGE="$PLUGIN_IMAGE_REF"

# Step 1: Enable necessary GCP APIs (Least Privilege - specific to this extension)
echo "Step 1: Enabling required GCP APIs for GKE Stockout Investigator extension..."
gcloud services enable \
    pubsub.googleapis.com \
    logging.googleapis.com \
    container.googleapis.com \
    --project="$PROJECT_ID" --quiet

# Step 2: Ensure PubSub Topic & Subscription exist beforehand
echo "Step 2: Ensuring PubSub Topic '${TOPIC}' exists..."
if ! gcloud pubsub topics describe "$TOPIC" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud pubsub topics create "$TOPIC" --project="$PROJECT_ID" || true
else
    echo "Topic '${TOPIC}' already exists."
fi

echo "Step 2b: Ensuring PubSub Subscription '${SUBSCRIPTION}' exists on topic '${TOPIC}'..."
if ! gcloud pubsub subscriptions describe "$SUBSCRIPTION" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud pubsub subscriptions create "$SUBSCRIPTION" --topic="$TOPIC" --project="$PROJECT_ID" || true
else
    echo "Subscription '${SUBSCRIPTION}' already exists."
fi

# Confirm the subscription really is attached to the topic the sink writes to — whether it
# was just created or was already there. A subscription's topic is fixed at creation, so
# overriding STOCKOUT_TOPIC without also overriding STOCKOUT_SUBSCRIPTION leaves the
# default subscription attached to the default topic: the sink publishes to one queue and
# the adapter pulls another, so not one alert is ever seen. Nothing errors — the plugin
# installs, reports healthy, and investigates nothing. This also catches a `create` that
# failed above, whose exit status is deliberately ignored so a race does not abort the run.
ATTACHED_TOPIC="$(gcloud pubsub subscriptions describe "$SUBSCRIPTION" \
    --project="$PROJECT_ID" --format='value(topic)' 2>/dev/null || echo "")"
EXPECTED_TOPIC="projects/${PROJECT_ID}/topics/${TOPIC}"
if [ -z "$ATTACHED_TOPIC" ]; then
    echo "Error: subscription '${SUBSCRIPTION}' does not exist and could not be created in project '${PROJECT_ID}'." >&2
    exit 1
fi
if [ "$ATTACHED_TOPIC" != "$EXPECTED_TOPIC" ]; then
    echo "Error: subscription '${SUBSCRIPTION}' is attached to '${ATTACHED_TOPIC}', not '${EXPECTED_TOPIC}'." >&2
    echo "       A subscription's topic cannot be changed after it is created. Set" >&2
    echo "       STOCKOUT_SUBSCRIPTION to a name of its own for topic '${TOPIC}', or delete" >&2
    echo "       the existing subscription and re-run." >&2
    exit 1
fi

# Step 3: Ensure GCP Cloud Logging Sink exists & grant least privilege publisher role
echo "Step 3: Ensuring Cloud Logging Sink '${SINK_NAME}' exists..."
TOPIC_PATH="pubsub.googleapis.com/projects/${PROJECT_ID}/topics/${TOPIC}"
EXISTING_DESTINATION="$(gcloud logging sinks describe "$SINK_NAME" \
    --project="$PROJECT_ID" --format='value(destination)' 2>/dev/null || echo "")"
if [ -z "$EXISTING_DESTINATION" ]; then
    gcloud logging sinks create "$SINK_NAME" "$TOPIC_PATH" \
        --log-filter="$FILTER" \
        --project="$PROJECT_ID" || true
elif [ "$EXISTING_DESTINATION" != "$TOPIC_PATH" ]; then
    # Refused, not repointed — the same answer step 2b gives for a subscription attached
    # to the wrong topic, and for the same reason. A sink is project-wide and shared: an
    # `update` here would silently take the sink some other deployment depends on and
    # aim it at this topic, and the only symptom over there is that alerts stop arriving.
    # It reads as a quiet fleet. Nothing warns, because from this side the update
    # succeeded and the verification below would happily confirm the new destination.
    #
    # A test run is the likely way in: STOCKOUT_TOPIC on its own is enough to hijack the
    # default sink, which is why STOCKOUT_SINK exists and why they are usually set together.
    echo "Error: log sink '${SINK_NAME}' already exports to '${EXISTING_DESTINATION}', not to '${TOPIC_PATH}'." >&2
    echo "       Repointing it would stop alerts reaching whatever consumes that topic," >&2
    echo "       with no error on either side. Set STOCKOUT_SINK to a sink name of its" >&2
    echo "       own for topic '${TOPIC}', or delete the existing sink and re-run." >&2
    exit 1
else
    # Destination unchanged; this is here for the filter, which legitimately moves when
    # TARGET_CLUSTER_NAME changes or the matched message set is edited.
    echo "Updating existing log sink '${SINK_NAME}' filter..."
    gcloud logging sinks update "$SINK_NAME" "$TOPIC_PATH" \
        --log-filter="$FILTER" \
        --project="$PROJECT_ID" || true
fi

# Confirm the sink really writes to this topic, and really filters for this cluster.
#
# The create and the update above are tolerated so a race does not abort the run, and that
# tolerance is exactly what needs checking: an `update` that failed leaves the sink
# exporting to the topic and filter it had BEFORE — a previous STOCKOUT_TOPIC, or a
# previous TARGET_CLUSTER_NAME. `describe` still returns a writerIdentity, the grant below
# still succeeds, and the install reports done while every alert goes to a queue nobody
# reads, or none is matched at all. This is the same silent failure step 2b catches one
# hop downstream, and it is worth catching at both ends of the pipe.
SINK_DESTINATION="$(gcloud logging sinks describe "$SINK_NAME" --project="$PROJECT_ID" --format='value(destination)' 2>/dev/null || echo "")"
if [ "$SINK_DESTINATION" != "$TOPIC_PATH" ]; then
    echo "Error: log sink '${SINK_NAME}' exports to '${SINK_DESTINATION:-<nothing>}', not to '${TOPIC_PATH}'." >&2
    echo "       The create or update above did not take. Re-run, or point STOCKOUT_SINK at" >&2
    echo "       a sink name of its own for topic '${TOPIC}'." >&2
    exit 1
fi
# One predicate out of the expression, not the expression itself. The cluster name is the
# part whose staleness is silent — a sink still scoped to the cluster of a previous install
# exports nothing this deployment will ever act on — whereas the rest of FILTER changes
# spelling whenever the matched message set is edited, so comparing the whole string would
# report a mismatch every time this script is edited rather than every time the update
# failed.
#
# It is still a byte comparison, of a substring: the Logging API stores the filter as it
# was given, so `cluster_name="<name>"` comes back the way it went in. If that ever stops
# being true the comparison fails closed — a loud false alarm on a correct install, not a
# silent pass on a stale sink — and the fix is to compare against a re-normalised filter
# here, not to drop the check.
SINK_FILTER="$(gcloud logging sinks describe "$SINK_NAME" --project="$PROJECT_ID" --format='value(filter)' 2>/dev/null || echo "")"
case "$SINK_FILTER" in
    *"cluster_name=\"${CLUSTER_NAME}\""*) ;;
    *)
        echo "Error: log sink '${SINK_NAME}' does not filter for cluster '${CLUSTER_NAME}'." >&2
        echo "       Its filter is still: ${SINK_FILTER:-<empty>}" >&2
        echo "       The update above did not take, so the sink is scoped to a different" >&2
        echo "       cluster and no alert from '${CLUSTER_NAME}' would ever reach the topic." >&2
        exit 1
        ;;
esac

WRITER_IDENTITY="$(gcloud logging sinks describe "$SINK_NAME" --project="$PROJECT_ID" --format='value(writerIdentity)')"
echo "Granting roles/pubsub.publisher (least privilege) to log sink identity '${WRITER_IDENTITY}' on topic '${TOPIC}'..."
gcloud pubsub topics add-iam-policy-binding "$TOPIC" \
    --member="$WRITER_IDENTITY" \
    --role="roles/pubsub.publisher" \
    --project="$PROJECT_ID" --quiet >/dev/null

# Step 4: Verify & Grant Platform Agent Service Account PubSub and Skill Command Permissions
echo "Step 4: Analyzing skill commands & checking Platform Agent GCP Service Account permissions..."

# Detect Platform Agent GCP Service Account from K8s SA annotation or default convention.
#
# The KSA is read off the agent rather than hardcoded. Its name is spec.security.
# serviceAccountName, which is per-agent, so a fleet with a second PlatformAgent runs under
# a KSA of its own — and looking up a fixed `kubeagents-platform-agent` there finds either
# nothing or, worse, the OTHER agent's identity, and grants this plugin's roles to it.
# Nothing errors: the alerts arrive, the skill runs, and its gcloud calls are denied.
KSA_NAME="$(kubectl --context="$CONTEXT" -n "$NAMESPACE" get platformagent "$AGENT_REF" \
    -o jsonpath='{.spec.security.serviceAccountName}' 2>/dev/null || echo "")"
# The operator's own default when the field is unset
# (k8s-operator/internal/controller/platformagent_manifests.go:1158).
KSA_NAME="${KSA_NAME:-$AGENT_REF}"

GSA_EMAIL="$(kubectl --context="$CONTEXT" get sa "$KSA_NAME" -n "$NAMESPACE" -o jsonpath='{.metadata.annotations.iam\.gke\.io/gcp-service-account}' 2>/dev/null || echo "")"
if [ -z "$GSA_EMAIL" ]; then
    # A guess, and said out loud as one. Reached when the KSA carries no Workload Identity
    # annotation, which is also what a wrong AGENT_REF or namespace looks like from here.
    # Granting roles to an account the agent does not use is invisible until the skill's
    # first gcloud call is denied, long after this script reported success.
    GSA_EMAIL="kubeagents-platform-gsa@${PROJECT_ID}.iam.gserviceaccount.com"
    echo "  WARN: ServiceAccount '${KSA_NAME}' in '${NAMESPACE}' carries no" >&2
    echo "        iam.gke.io/gcp-service-account annotation, so the GSA below is a guess" >&2
    echo "        from the default naming convention. If the agent runs as a different" >&2
    echo "        account, the roles granted next land on the wrong one." >&2
fi

echo "Platform Agent Kubernetes ServiceAccount: ${KSA_NAME}"
echo "Platform Agent GCP Service Account identified: ${GSA_EMAIL}"

# 4a: Check & Grant PubSub topic & subscription permissions to Platform Agent GSA
echo "Checking PubSub topic/subscription access for Platform Agent GSA (${GSA_EMAIL})..."
gcloud pubsub topics add-iam-policy-binding "$TOPIC" \
    --member="serviceAccount:${GSA_EMAIL}" \
    --role="roles/pubsub.subscriber" \
    --project="$PROJECT_ID" --quiet >/dev/null

gcloud pubsub subscriptions add-iam-policy-binding "$SUBSCRIPTION" \
    --member="serviceAccount:${GSA_EMAIL}" \
    --role="roles/pubsub.subscriber" \
    --project="$PROJECT_ID" --quiet >/dev/null

# 4b: Skill Command Permission Analysis:
# Skill SKILL.md executes:
#   - `gcloud compute regions describe ...`
#   - `gcloud compute reservations list ...`
#   - `gcloud beta compute advice capacity ...`
#   - `gcloud beta compute advice capacity-history ...`
# Permission Required: `roles/compute.viewer` (Least privilege for GCP compute & capacity advice inspection)
echo "Skill Command Analysis: 'SKILL.md' executes gcloud compute & advice queries ('gcloud compute regions describe', 'gcloud beta compute advice ...')."
echo "Ensuring least-privilege IAM role 'roles/compute.viewer' is granted to '${GSA_EMAIL}'..."
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${GSA_EMAIL}" \
    --role="roles/compute.viewer" \
    --condition=None \
    --project="$PROJECT_ID" --quiet >/dev/null

# Step 5: Build and Publish Plugin OCI Image
#
# Built locally — by docker where a daemon is running, by crane where none is — and
# pushed to the Artifact Registry the agent's own image is in, which is usually but not
# always this project; the resolve step above says so when it is not. PLUGIN_IMAGE names
# an image that already exists and skips the build entirely, for a pipeline that builds
# once and installs many times. The reference itself was settled before step 1.
echo "Step 5: Building and publishing GKE Stockout Investigator OCI image..."
plugin_image_publish "$SCRIPT_DIR" "${SCRIPT_DIR}/files"

# Step 6: Deploy AgentPlugin via Helm
# agentRef is passed, not left to values.yaml. The operator ignores a plugin whose
# agentRef names no PlatformAgent in the namespace, so an AGENT_REF honoured by the image
# discovery and by the tuning patch below, but not here, would install a plugin that
# attaches to nothing, report success, and investigate no alert ever — with the tuning
# landing on the agent that was never given the skill.
echo "Step 6: Deploying GKE Stockout Investigator AgentPlugin via Helm..."
helm upgrade --install "$RELEASE" "$SCRIPT_DIR" \
    --kube-context "$CONTEXT" \
    --namespace "$NAMESPACE" \
    --create-namespace \
    --set agentRef="$AGENT_REF" \
    --set image="$IMAGE" \
    --set clusterName="$CLUSTER_NAME" \
    --set pubsub.topic="$TOPIC" \
    --set pubsub.subscription="$SUBSCRIPTION" \
    --set pubsub.sink="$SINK_NAME"

# Step 6b: Apply the execution limits this workload needs.
#
# These live on the PlatformAgent, which this chart does not own, so they are patched
# rather than templated. They are deliberately NOT kube-agents defaults — a vanilla
# deployment runs Hermes' own values, and stockout remediation needs more because it is
# long-running and quota-hungry. See tuning.yaml for the reasoning behind each number.
#
# The switch is this environment variable, and only this one. There is no Helm value for
# it: no template reads `.Values.tuning`, so a `--set tuning.apply=false` would be
# accepted by helm and change nothing, and the operator who used it to keep their own
# limits would get them overwritten with no indication the opt-out was ignored.
#
# Rejecting anything but true/false for the same reason, in the other direction:
# `APPLY_TUNING=0` or `=False` reads as an opt-out to a human and takes the else branch
# here silently, so the limits the plugin needs never land and the first long run dies as
# a "protocol violation".
APPLY_TUNING="${APPLY_TUNING:-true}"
if [ "$APPLY_TUNING" != "true" ] && [ "$APPLY_TUNING" != "false" ]; then
    echo "Error: APPLY_TUNING must be 'true' or 'false', got '${APPLY_TUNING}'." >&2
    exit 1
fi
if [ "$APPLY_TUNING" = "true" ]; then
    echo "Step 6b: Applying execution limits to PlatformAgent '${AGENT_REF}'..."
    if kubectl --context="$CONTEXT" -n "$NAMESPACE" patch platformagent "$AGENT_REF" \
        --type=merge --patch-file "$SCRIPT_DIR/tuning.yaml"; then
        echo "  Applied. The agent pod will roll to pick the new limits up."
    else
        echo "  WARN: could not patch the PlatformAgent. The plugin will still install, but" >&2
        echo "        remediation runs may hit Hermes' default limits and stop mid-task with" >&2
        echo "        a misleading 'protocol violation'. See tuning.yaml." >&2
    fi
else
    echo "Step 6b: Skipped (APPLY_TUNING=false). Apply tuning.yaml yourself, or the agent"
    echo "         runs on Hermes defaults — see tuning.yaml for why that may not suffice."
fi

echo "Step 7: Verifying AgentPlugin status in cluster..."
kubectl --context="$CONTEXT" get agentplugin "$RELEASE" -n "$NAMESPACE"

echo "============================================================"
echo "GKE Stockout Investigator Extension installation complete!"
echo "============================================================"
