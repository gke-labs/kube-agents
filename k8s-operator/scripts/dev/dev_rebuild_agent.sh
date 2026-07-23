#!/usr/bin/env bash
# ==============================================================================
# 🛠️ Fast Local Development: Rebuild & Redeploy Agent
# ==============================================================================
# Script to build, push, and redeploy the platform agent image
# for fast local iteration and testing.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$SCRIPT_DIR" == */scripts/dev ]]; then
  SCRIPTS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
  OPERATOR_DIR="$(cd "${SCRIPTS_DIR}/.." && pwd)"
  REPO_ROOT="$(cd "${OPERATOR_DIR}/.." && pwd)"
elif [[ "$SCRIPT_DIR" == */scripts ]]; then
  SCRIPTS_DIR="${SCRIPT_DIR}"
  OPERATOR_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
  REPO_ROOT="$(cd "${OPERATOR_DIR}/.." && pwd)"
else
  SCRIPTS_DIR="${SCRIPT_DIR}"
  OPERATOR_DIR="${SCRIPT_DIR}"
  REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
fi
VARS_FILE="${SCRIPTS_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPTS_DIR}/common.sh" "$@"

# ─── Argument Parsing ─────────────────────────────────────────────────────────
USE_LOCAL_BUILD=0
SELECTED_AGENT=""
for arg in "$@"; do
  case $arg in
    --local) USE_LOCAL_BUILD=1 ;;
    platform) SELECTED_AGENT="$arg" ;;
    *) ;;
  esac
done

if [ -z "$SELECTED_AGENT" ]; then
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    SELECTED_AGENT="platform"
  else
    echo -ne "  ${C_CYAN}Select agent to rebuild (platform) [${C_WHITE}platform${C_CYAN}]: ${C_RESET}"
    read -r input_val
    SELECTED_AGENT="${input_val:-platform}"
  fi
fi

SELECTED_AGENT=$(echo "$SELECTED_AGENT" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
if [[ ! "$SELECTED_AGENT" =~ ^(platform)$ ]]; then
  print_error "Invalid agent '$SELECTED_AGENT'. Must be one of: platform."
  exit 1
fi

case "$SELECTED_AGENT" in
  platform)
    AGENT_TARGET="platform"
    IMAGE_NAME="platform-agent"
    CR_KIND="PlatformAgent"
    CR_RESOURCE="platformagents.kubeagents.x-k8s.io"
    ;;
esac

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
if [ "$USE_LOCAL_BUILD" -eq 1 ]; then
  check_prereqs "gcloud" "kubectl" "docker"
else
  check_prereqs "gcloud" "kubectl"
fi

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State for Dev Rebuild"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "us-east4" "Enter GCP Region for Artifact Registry & GKE"
init_var "CLUSTER_NAME" "platform-agent-host" "Enter Host GKE Cluster Name"
init_var "GCP_ARTIFACT_REGISTRY_REPO_NAME" "${GCP_ARTIFACT_REGISTRY_REPO_NAME:-${REPO_NAME:-kube-agents}}" "Enter Artifact Registry Repository Name"

# Resolve HERMES_AGENT_TAG from tags.env
HERMES_AGENT_TAG=""
if [ -f "${REPO_ROOT}/tags.env" ]; then
  HERMES_AGENT_TAG=$(grep '^HERMES_AGENT_TAG=' "${REPO_ROOT}/tags.env" | cut -d'=' -f2 | tr -d '\r"' | tr -d "'")
fi
if [ -z "$HERMES_AGENT_TAG" ]; then
  print_error "Could not resolve HERMES_AGENT_TAG from ${REPO_ROOT}/tags.env"
  exit 1
fi

DEV_TAG="dev-$(date +%Y%m%d-%H%M%S)"
IMAGE_BASE="$REGION-docker.pkg.dev/$PROJECT_ID/$GCP_ARTIFACT_REGISTRY_REPO_NAME/$IMAGE_NAME"
IMAGE_URI="$IMAGE_BASE:$DEV_TAG"
IMAGE_URI_LATEST="$IMAGE_BASE:latest"
PROXY_IMAGE_BASE="$REGION-docker.pkg.dev/$PROJECT_ID/$GCP_ARTIFACT_REGISTRY_REPO_NAME/credential-proxy"
PROXY_IMAGE_URI="$PROXY_IMAGE_BASE:$DEV_TAG"
PROXY_IMAGE_URI_LATEST="$PROXY_IMAGE_BASE:latest"

# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Verify / Create Artifact Registry Repository
verify_registry() {
  gcloud artifacts repositories describe "$GCP_ARTIFACT_REGISTRY_REPO_NAME" --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1
}
execute_registry() {
  print_info "Creating Artifact Registry repository '$GCP_ARTIFACT_REGISTRY_REPO_NAME' in location '$REGION'..."
  gcloud artifacts repositories create "$GCP_ARTIFACT_REGISTRY_REPO_NAME" \
      --repository-format=docker \
      --location="$REGION" \
      --project="$PROJECT_ID" \
      --description="Kubernetes Agentic Harness repository for local development"
}

# Step 2: Build & Push Image
verify_image_build() {
  # Always return 1 so local changes are always rebuilt when running this dev tool
  return 1
}
execute_image_build() {
  if [ "$USE_LOCAL_BUILD" -eq 1 ]; then
    print_info "Building '$AGENT_TARGET' agent locally using Docker..."
    docker pull "$IMAGE_URI_LATEST" 2>/dev/null || true
    DOCKER_BUILDKIT=1 docker build --cache-from "$IMAGE_URI_LATEST" --build-arg BUILDKIT_INLINE_CACHE=1 --build-arg HERMES_AGENT_TAG="$HERMES_AGENT_TAG" --target "$AGENT_TARGET" -t "$IMAGE_URI" -t "$IMAGE_URI_LATEST" -f "${REPO_ROOT}/deploy/docker/Dockerfile" "${REPO_ROOT}" || return 1
    print_info "Pushing images to Artifact Registry ($IMAGE_BASE)..."
    docker push "$IMAGE_URI" || return 1
    docker push "$IMAGE_URI_LATEST" || return 1
    DOCKER_BUILDKIT=1 docker build --cache-from "$PROXY_IMAGE_URI_LATEST" --build-arg BUILDKIT_INLINE_CACHE=1 --build-arg HERMES_AGENT_TAG="$HERMES_AGENT_TAG" --target credential-proxy -t "$PROXY_IMAGE_URI" -t "$PROXY_IMAGE_URI_LATEST" -f "${REPO_ROOT}/deploy/docker/Dockerfile" "${REPO_ROOT}" || return 1
    docker push "$PROXY_IMAGE_URI" || return 1
    docker push "$PROXY_IMAGE_URI_LATEST" || return 1
  else
    print_info "Submitting build for '$AGENT_TARGET' agent to Google Cloud Build..."
    print_info "Target Images: $IMAGE_URI and $IMAGE_URI_LATEST"
    (
      cd "${REPO_ROOT}"
      gcloud builds submit \
          --config="deploy/docker/cloudbuild.yaml" \
          --substitutions="_IMAGE_URI=${IMAGE_URI},_IMAGE_URI_LATEST=${IMAGE_URI_LATEST},_TARGET=${AGENT_TARGET},_HERMES_AGENT_TAG=${HERMES_AGENT_TAG}" \
          --project="${PROJECT_ID}" \
          .
    ) || return 1
    (
      cd "${REPO_ROOT}"
      gcloud builds submit \
          --config="deploy/docker/cloudbuild.yaml" \
          --substitutions="_IMAGE_URI=${PROXY_IMAGE_URI},_IMAGE_URI_LATEST=${PROXY_IMAGE_URI_LATEST},_TARGET=credential-proxy,_HERMES_AGENT_TAG=${HERMES_AGENT_TAG}" \
          --project="${PROJECT_ID}" \
          .
    ) || return 1
  fi
}

# Step 3: Connect to Host GKE Cluster
verify_kubeconfig() {
  local current_ctx
  current_ctx=$(kubectl config current-context 2>/dev/null || echo "")
  [[ "$current_ctx" == *"${PROJECT_ID}"* && "$current_ctx" == *"${CLUSTER_NAME}"* ]]
}
execute_kubeconfig() {
  connect_cluster
}

# Step 4: Trigger Redeployment in GKE
verify_redeploy() {
  # Always return 1 to force redeployment of the updated image
  return 1
}
execute_redeploy() {
  print_info "Searching for running instances of ${CR_KIND} across cluster..."
  local cr_found=0
  if kubectl get crd "${CR_RESOURCE}" >/dev/null 2>&1; then
    local instances
    instances=$(kubectl get "${CR_RESOURCE}" -A -o jsonpath='{range .items[*]}{.metadata.namespace}:{.metadata.name}{"\n"}{end}' 2>/dev/null || echo "")
    if [ -n "$instances" ]; then
      for inst in $instances; do
        local ns="${inst%%:*}"
        local name="${inst#*:}"
        print_info "Updating Custom Resource '${name}' (${CR_KIND}) in namespace '${ns}' to use image '${IMAGE_BASE}' tag '${DEV_TAG}'..."
        kubectl patch "${CR_RESOURCE}" "${name}" -n "${ns}" --type='merge' -p '{"spec":{"deployment":{"image":"'"${IMAGE_BASE}"'","tag":"'"${DEV_TAG}"'"}}}' || return 1
        cr_found=1
      done
    fi
  fi

  if [ "$cr_found" -eq 0 ]; then
    print_warning "No active ${CR_KIND} custom resources found in cluster."
  fi

  print_info "Searching for Kubernetes Deployments matching '${AGENT_TARGET}' across cluster..."
  local deployments
  deployments=$(kubectl get deployments -A -o jsonpath='{range .items[*]}{.metadata.namespace}:{.metadata.name}{"\n"}{end}' 2>/dev/null || echo "")
  local dep_found=0
  if [ -n "$deployments" ]; then
    for dep_entry in $deployments; do
      local ns="${dep_entry%%:*}"
      local dep="${dep_entry#*:}"
      if [[ "$dep" == *"${AGENT_TARGET}"* && "$dep" != *"credential-proxy"* ]]; then
        print_info "Triggering rolling update for Deployment '${dep}' in namespace '${ns}'..."
        # Set image in case it's a standalone deployment not managed by a CR
        local container_name
        local container_names
        container_names=$(kubectl get deployment "${dep}" -n "${ns}" -o jsonpath='{range .spec.template.spec.containers[*]}{.name}{"\n"}{end}' 2>/dev/null || true)
        container_name=$(echo "$container_names" | grep -E "agent|${AGENT_TARGET}" | head -n 1)
        if [ -n "$container_name" ]; then
          local image_updates=("${container_name}=${IMAGE_URI}")
          if echo "$container_names" | grep -Fxq "envoy-credential-proxy"; then
            image_updates+=("envoy-credential-proxy=${PROXY_IMAGE_URI}")
          fi
          kubectl set image "deployment/${dep}" -n "${ns}" "${image_updates[@]}" 2>/dev/null || true
        else
          kubectl set image "deployment/${dep}" -n "${ns}" "${AGENT_TARGET}=${IMAGE_URI}" 2>/dev/null || true
        fi
        dep_found=1
      fi
    done
  fi

  if [ "$dep_found" -eq 0 ] && [ "$cr_found" -eq 0 ]; then
    print_warning "Could not locate matching running workloads for '${SELECTED_AGENT}'. Please verify your cluster deployment state."
  else
    print_success "Redeployment triggered successfully for '${SELECTED_AGENT}'!"
  fi
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Verify/Create Artifact Registry Repository" verify_registry execute_registry 0
save_var "DEV_ARTIFACT_REGISTRY_CREATED" "true"
run_step "2. Build & Push Agent Image (${SELECTED_AGENT})" verify_image_build execute_image_build 0
run_step "3. Connect to Host GKE Cluster" verify_kubeconfig execute_kubeconfig 0
run_step "4. Trigger Redeployment in GKE" verify_redeploy execute_redeploy 0

echo -e "\n${C_GREEN}${C_BOLD}🚀 Fast iteration update complete for ${SELECTED_AGENT}!${C_RESET}"
echo -e "  ${C_CYAN}New sandbox image deployed:${C_RESET} ${IMAGE_URI}"
echo -e "  ${C_CYAN}New credential proxy image deployed:${C_RESET} ${PROXY_IMAGE_URI}"
echo ""
