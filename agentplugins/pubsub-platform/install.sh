#!/bin/bash
set -euo pipefail

# Installation script for pubsub-platform extension module.
# Applies the AgentPlugin CRD and installs the pubsub-platform extension.
# Respects User Rule 15: Always uses a dedicated kubectl context.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

CONTEXT="${KUBECTL_CONTEXT:-$(kubectl config current-context 2>/dev/null || echo "")}"
NAMESPACE="${HERMES_NAMESPACE:-kubeagents-system}"

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

echo "============================================================"
echo "Installing PubSub Platform Extension"
echo "Kubectl Context: ${CONTEXT}"
echo "Namespace: ${NAMESPACE}"
echo "============================================================"

# Apply the AgentPlugin CRD
echo "Applying AgentPlugin CRD..."
kubectl --context="$CONTEXT" apply -f "${REPO_ROOT}/k8s-operator/config/crd/bases/kubeagents.x-k8s.io_agentplugins.yaml"

# Build and publish the OCI image
echo "Building and publishing pubsub-platform OCI image..."
# Published to the project being installed into, unless PLUGIN_IMAGE overrides it.
PROJECT_ID="${GCP_PROJECT_ID:-${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo "")}}"
if [ -z "${PLUGIN_IMAGE:-}" ] && [ -z "$PROJECT_ID" ]; then
    echo "Error: could not determine the GCP project for the image. Set GCP_PROJECT_ID or PLUGIN_IMAGE."
    exit 1
fi
IMAGE="${PLUGIN_IMAGE:-gcr.io/${PROJECT_ID}/pubsub-platform:latest}"
# PLUGIN_IMAGE names an image that already exists, so building is skipped. That is not
# just a convenience: Cloud Build is unavailable in some environments (corp credentials
# are rejected for its quota), and an installer that can only ever build its own image
# cannot be used there at all — nor in a pipeline that builds once and installs many times.
if [ -n "${PLUGIN_IMAGE:-}" ]; then
    echo "Using the prebuilt image ${IMAGE} (PLUGIN_IMAGE set); skipping the build."
else
    gcloud builds submit --project="$PROJECT_ID" --tag "$IMAGE" "$SCRIPT_DIR"
fi

# Deploy the chart directly. This used to call scripts/deploy_extension.sh, which is not
# in this repository — the installer could never have run as written.
echo "Deploying pubsub-platform extension via Helm chart..."
helm upgrade --install pubsubplatform "$SCRIPT_DIR" \
    --kube-context "$CONTEXT" \
    --namespace "$NAMESPACE" \
    --create-namespace \
    --set image="$IMAGE"

echo "Done! PubSub platform extension installed successfully."
