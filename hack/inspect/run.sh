#!/usr/bin/env bash
# Builds the images the tasks run in, loads them into the cluster if it is a kind
# cluster, then runs `inspect eval` with the arguments given.
#
#   ./run.sh clusters_from_memory.py --model google/gemini-3.7-flash -T harness=hermes
set -euo pipefail

cd "$(dirname "$0")"
REPO_ROOT="$(git rev-parse --show-toplevel)"
CONTEXT="${INSPECT_CONTEXT:-kind-kube-agents}"
PLATFORM="linux/$(docker version --format '{{.Server.Arch}}')"
HERMES_AGENT_TAG="$(sed -n 's/^HERMES_AGENT_TAG=//p' "${REPO_ROOT}/tags.env")"

docker build --platform "${PLATFORM}" --build-arg HERMES_AGENT_TAG="${HERMES_AGENT_TAG}" \
  --target platform -t kube-agents-inspect-platform:dev -f "${REPO_ROOT}/deploy/docker/Dockerfile" "${REPO_ROOT}"
docker build --platform "${PLATFORM}" -t kube-agents-inspect-kubectl:v1.33.1 environments/kubectl

if [[ "${CONTEXT}" == kind-* ]]; then
  kind load docker-image kube-agents-inspect-platform:dev kube-agents-inspect-kubectl:v1.33.1 --name "${CONTEXT#kind-}"
fi

exec uv run inspect eval "$@"
