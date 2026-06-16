#!/bin/bash
# Script to manually build and push the k8s-operator image to GHCR.
# Ensure you are logged in to GHCR first: echo $PAT | docker login ghcr.io -u USERNAME --password-stdin

set -e

# Configuration (Override by setting env variables)
OWNER="${OWNER:-gke-agentic}"
REPO="${REPO:-kube-agents}"
TAG="${TAG:-latest}"

IMAGE="ghcr.io/${OWNER}/${REPO}/k8s-operator:${TAG}"

echo "Building image: ${IMAGE}..."
docker build -t "${IMAGE}" -f k8s-operator/Dockerfile k8s-operator

echo "Pushing image: ${IMAGE}..."
docker push "${IMAGE}"

echo "Done!"
