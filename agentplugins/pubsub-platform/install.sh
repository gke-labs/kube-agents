#!/usr/bin/env bash
# env bash, not /bin/bash: on macOS the latter is 3.2, and the shared image library this
# sources is written to run there but has no reason to be held to it forever.
set -euo pipefail

# Installation script for pubsub-platform extension module.
# Applies the AgentPlugin CRD and installs the pubsub-platform extension.
# Respects User Rule 15: Always uses a dedicated kubectl context.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# shellcheck source=../lib/plugin_image.sh
. "${REPO_ROOT}/agentplugins/lib/plugin_image.sh"

CONTEXT="${KUBECTL_CONTEXT:-$(kubectl config current-context 2>/dev/null || echo "")}"
NAMESPACE="${HERMES_NAMESPACE:-kubeagents-system}"
# The PlatformAgent this plugin attaches to. One value drives both places the agent is
# named: the CR's agentRef (passed to helm below, overriding values.yaml) and the registry
# the image resolution copies from. The default matches values.yaml so an unset AGENT_REF
# changes nothing.
AGENT_REF="${AGENT_REF:-platform-agent}"

usage() {
    echo "Usage: $0 [--context <kubectl-context>] [--namespace <namespace>]"
    echo "Example: $0 --context kind-kind --namespace kubeagents-system"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --context)
            CONTEXT="$2"
            shift 2
            ;;
        --namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown argument: $1"
            usage
            ;;
    esac
done

if [ -z "$CONTEXT" ]; then
    echo "Error: No kubectl context specified via --context or KUBECTL_CONTEXT environment variable."
    exit 1
fi

PROJECT_ID="${GCP_PROJECT_ID:-${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo "")}}"

echo "============================================================"
echo "Installing PubSub Platform Extension"
echo "Kubectl Context: ${CONTEXT}"
echo "Namespace: ${NAMESPACE}"
echo "Agent: ${AGENT_REF}"
echo "============================================================"

# Work the image reference out before anything is applied. This is where a missing
# project, an unreadable source tree or an absent image builder is caught, and catching
# them here costs nothing — caught after the CRD is applied, they leave the cluster
# half-changed. The context and namespace go in so the reference lands in the registry
# the agent is already pulling from. It prints the reference it settled on — which is why
# it runs after the banner and not before it. See agentplugins/lib/plugin_image.sh.
plugin_image_resolve "pubsub-platform" "$PROJECT_ID" "$SCRIPT_DIR" \
    "${SCRIPT_DIR}/files/platforms/pubsub" "$CONTEXT" "$NAMESPACE" "$AGENT_REF"
IMAGE="$PLUGIN_IMAGE_REF"

# Apply the AgentPlugin CRD
echo "Applying AgentPlugin CRD..."
kubectl --context="$CONTEXT" apply -f "${REPO_ROOT}/k8s-operator/config/crd/bases/kubeagents.x-k8s.io_agentplugins.yaml"

# Build and publish the OCI image
#
# Built locally — by docker where a daemon is running, by crane where none is — and
# pushed to the Artifact Registry the agent's own image is in, which is usually but not
# always this project; the resolve step above says so when it is not. PLUGIN_IMAGE names
# an image that already exists and skips the build entirely, for a pipeline that builds
# once and installs many times.
echo "Building and publishing the pubsub-platform OCI image..."
plugin_image_publish "$SCRIPT_DIR" "${SCRIPT_DIR}/files/platforms/pubsub"

# Deploy the chart directly. This used to call scripts/deploy_extension.sh, which is not
# in this repository — the installer could never have run as written.
echo "Deploying pubsub-platform extension via Helm chart..."
# agentRef is passed, not left to values.yaml. The operator ignores a plugin whose
# agentRef names no PlatformAgent in the namespace, so an AGENT_REF honoured by the image
# discovery above but not here would install a plugin that attaches to nothing, report
# success, and leave the alert path dead with no error anywhere.
helm upgrade --install pubsubplatform "$SCRIPT_DIR" \
    --kube-context "$CONTEXT" \
    --namespace "$NAMESPACE" \
    --create-namespace \
    --set agentRef="$AGENT_REF" \
    --set image="$IMAGE"

echo "Done! PubSub platform extension installed successfully."
