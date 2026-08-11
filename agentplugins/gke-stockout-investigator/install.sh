#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# GKE Stockout Investigator Extension Installation Script
# Ensures GCP APIs, IAM permissions (least privilege), PubSub topic/subscription,
# Log Sink, Platform Agent IAM bindings, and Helm AgentPlugin deployment.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

NAMESPACE="kubeagents-system"
# Helm release, AgentPlugin name and Hermes plugin module, all one identifier: the CRD
# pattern is ^[a-z][a-z0-9]*$, so the hyphenated chart name cannot be used here.
RELEASE="gkestockoutinvestigator"
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
echo "PubSub Topic:    ${TOPIC}"
echo "Subscription:    ${SUBSCRIPTION}"
echo "============================================================"

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

echo "Step 2b: Ensuring PubSub Subscription '${SUBSCRIPTION}' exists..."
if ! gcloud pubsub subscriptions describe "$SUBSCRIPTION" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud pubsub subscriptions create "$SUBSCRIPTION" --topic="$TOPIC" --project="$PROJECT_ID" || true
else
    echo "Subscription '${SUBSCRIPTION}' already exists."
fi

# Step 3: Ensure GCP Cloud Logging Sink exists & grant least privilege publisher role
echo "Step 3: Ensuring Cloud Logging Sink '${SINK_NAME}' exists..."
TOPIC_PATH="pubsub.googleapis.com/projects/${PROJECT_ID}/topics/${TOPIC}"
if ! gcloud logging sinks describe "$SINK_NAME" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud logging sinks create "$SINK_NAME" "$TOPIC_PATH" \
        --log-filter="$FILTER" \
        --project="$PROJECT_ID" || true
else
    echo "Updating existing log sink '${SINK_NAME}' filter..."
    gcloud logging sinks update "$SINK_NAME" "$TOPIC_PATH" \
        --log-filter="$FILTER" \
        --project="$PROJECT_ID" || true
fi

WRITER_IDENTITY="$(gcloud logging sinks describe "$SINK_NAME" --project="$PROJECT_ID" --format='value(writerIdentity)')"
echo "Granting roles/pubsub.publisher (least privilege) to log sink identity '${WRITER_IDENTITY}' on topic '${TOPIC}'..."
gcloud pubsub topics add-iam-policy-binding "$TOPIC" \
    --member="$WRITER_IDENTITY" \
    --role="roles/pubsub.publisher" \
    --project="$PROJECT_ID" --quiet >/dev/null

# Step 4: Verify & Grant Platform Agent Service Account PubSub and Skill Command Permissions
echo "Step 4: Analyzing skill commands & checking Platform Agent GCP Service Account permissions..."

# Detect Platform Agent GCP Service Account from K8s SA annotation or default convention
GSA_EMAIL="$(kubectl --context="$CONTEXT" get sa kubeagents-platform-agent -n "$NAMESPACE" -o jsonpath='{.metadata.annotations.iam\.gke\.io/gcp-service-account}' 2>/dev/null || echo "")"
if [ -z "$GSA_EMAIL" ]; then
    GSA_EMAIL="kubeagents-platform-gsa@${PROJECT_ID}.iam.gserviceaccount.com"
fi

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
echo "Step 5: Building and publishing GKE Stockout Investigator OCI image..."
# Published to the project being installed into, unless PLUGIN_IMAGE overrides it.
IMAGE="${PLUGIN_IMAGE:-gcr.io/${PROJECT_ID}/gke-stockout-investigator:latest}"
# PLUGIN_IMAGE names an image that already exists, so building is skipped. That is not
# just a convenience: Cloud Build is unavailable in some environments (corp credentials
# are rejected for its quota), and an installer that can only ever build its own image
# cannot be used there at all — nor in a pipeline that builds once and installs many times.
if [ -n "${PLUGIN_IMAGE:-}" ]; then
    echo "Using the prebuilt image ${IMAGE} (PLUGIN_IMAGE set); skipping the build."
else
    gcloud builds submit --project="$PROJECT_ID" --tag "$IMAGE" "$SCRIPT_DIR"
fi

# Step 6: Deploy AgentPlugin via Helm
echo "Step 6: Deploying GKE Stockout Investigator AgentPlugin via Helm..."
helm upgrade --install "$RELEASE" "$SCRIPT_DIR" \
    --kube-context "$CONTEXT" \
    --namespace "$NAMESPACE" \
    --create-namespace \
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
    echo "Step 6b: Applying execution limits to PlatformAgent '${AGENT_REF:-platform-agent}'..."
    if kubectl --context="$CONTEXT" -n "$NAMESPACE" patch platformagent "${AGENT_REF:-platform-agent}" \
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
