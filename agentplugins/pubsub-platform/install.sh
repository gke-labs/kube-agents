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
IMAGE="gcr.io/tomeklipski-izrhgv/pubsub-platform:latest"
gcloud builds submit --tag "$IMAGE" "$SCRIPT_DIR"

# Deploy extension via deploy_extension.sh
echo "Deploying pubsub-platform extension via Helm chart..."
bash "${REPO_ROOT}/scripts/deploy_extension.sh" \
    --extension pubsub-platform \
    --release-name pubsubplatform \
    --context "$CONTEXT" \
    --namespace "$NAMESPACE"

echo "Done! PubSub platform extension installed successfully."
