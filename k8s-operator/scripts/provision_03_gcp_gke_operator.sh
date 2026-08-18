#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 3: Deploy Kubernetes Operator (CRDs & Controller Manager)
# ==============================================================================
# Idempotent script that installs the CRDs and deploys the operator to the cluster.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$SCRIPT_DIR" == */scripts ]]; then
  OPERATOR_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  OPERATOR_DIR="${SCRIPT_DIR}"
fi
VARS_FILE="${SCRIPT_DIR}/vars.sh"

source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud" "kubectl" "make" "jq"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State for Operator Deployment"
# This step deploys the operator image from this repo, so it needs a tag.
REQUIRES_IMAGE_TAG=1
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "$DEFAULT_REGION" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "$DEFAULT_CLUSTER_NAME" "Enter GKE Cluster Name"

DEFAULT_OPERATOR_IMAGE="$(registry_prefix)/k8s-operator"
init_var "OPERATOR_IMAGE" "$DEFAULT_OPERATOR_IMAGE" "Enter Operator Image Path"
warn_on_registry_prefix_mismatch "OPERATOR_IMAGE"
# This step forwards these two to the operator as well, so a saved value left
# behind in another registry is just as misleading here as OPERATOR_IMAGE is.
# Both are unset on a stock install, and the check is a no-op when empty.
warn_on_registry_prefix_mismatch "PLATFORM_AGENT_IMAGE"
warn_on_registry_prefix_mismatch "CREDENTIAL_PROXY_IMAGE"

# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Connect kubectl
verify_kubeconfig() {
  local current_ctx
  current_ctx=$(kubectl config current-context 2>/dev/null || echo "")
  [[ "$current_ctx" == *"${PROJECT_ID}"* && "$current_ctx" == *"${CLUSTER_NAME}"* ]] && \
  (kubectl get ns "${NAMESPACE}" >/dev/null 2>&1 || kubectl get ns default >/dev/null 2>&1)
}
execute_kubeconfig() {
  connect_cluster
}

# Apply the cert-manager release manifest, rewriting its images onto the
# third-party mirror when one is configured. cert-manager ships as a single
# manifest with the images baked in, so a prefix alone cannot redirect it —
# without the rewrite this step pulls quay.io/jetstack/* and fails on a cluster
# that may only pull from an approved registry. The jetstack images are already
# named cert-manager-*, so swapping the registry path is enough to match the
# flat layout scripts/mirror_images.sh writes.
apply_cert_manager_manifest() {
  local source prefix version
  if [ -n "${CERT_MANAGER_MANIFEST:-}" ]; then
    source="$CERT_MANAGER_MANIFEST"
  else
    version="$(cert_manager_version)" || return 1
    source="https://github.com/cert-manager/cert-manager/releases/download/${version}/cert-manager.yaml"
  fi
  prefix="$(third_party_registry_prefix)"

  if [ -z "$prefix" ]; then
    print_info "Applying cert-manager from ${source}..."
    kubectl apply -f "$source"
    return
  fi

  print_info "Applying cert-manager from ${source} with images rewritten to ${prefix}..."
  local manifest
  case "$source" in
    http://* | https://*)
      # kubectl can fetch a URL itself, but not while the images are being
      # rewritten, so this branch — and only this branch — needs a fetcher.
      if ! command -v curl >/dev/null 2>&1; then
        print_error "curl is required to rewrite cert-manager images onto '${prefix}'. Install curl, or set CERT_MANAGER_MANIFEST to a local file."
        return 1
      fi
      manifest="$(curl -fsSL "$source")" || return 1
      ;;
    *)
      manifest="$(cat "$source")" || return 1
      ;;
  esac
  printf '%s\n' "$manifest" | sed "s#quay\.io/jetstack/#${prefix}/#g" | kubectl apply -f -
}

# The cert-manager version is pinned once, in images.json, alongside the images
# `make mirror-images` copies — a manifest URL and a mirror on different
# versions is a pull failure at apply time.
cert_manager_version() {
  local version
  version="$(jq -r '.images[] | select(.name == "cert-manager-controller") | .tag' "$IMAGES_JSON" 2>/dev/null)"
  if [ -z "$version" ] || [ "$version" = "null" ]; then
    print_error "No cert-manager-controller pin in ${IMAGES_JSON}; cannot build the manifest URL."
    return 1
  fi
  echo "$version"
}

# Step 2: Ensure cert-manager is installed
verify_cert_manager() {
  local avail
  avail=$(kubectl get deployment cert-manager-webhook -n cert-manager -o jsonpath='{.status.availableReplicas}' 2>/dev/null || echo 0)
  [ "${avail:-0}" -ge 1 ]
}
execute_cert_manager() {
  if is_truthy "${SKIP_CERT_MANAGER:-}"; then
    # For clusters where the platform team installs cert-manager themselves, or
    # installs it under names this script's checks do not recognise. The
    # operator's admission webhooks need it, so say what was skipped rather
    # than reporting a clean step.
    print_warning "SKIP_CERT_MANAGER is set — not installing cert-manager. The operator's webhooks need it; ensure it is present."
    return 0
  fi

  print_info "cert-manager not found. Installing cert-manager..."

  # Check if the cluster is a GKE Autopilot cluster
  local is_autopilot
  is_autopilot=$(kubectl get nodes -o jsonpath='{.items[*].spec.providerID}' 2>/dev/null | grep -q "gce://.*/gk3-" && echo "true" || echo "false")

  if [ "$is_autopilot" = "true" ]; then
    print_info "GKE Autopilot cluster detected. Deploying cert-manager with leader-election disabled..."
  else
    print_info "Standard cluster detected. Installing standard cert-manager..."
  fi

  apply_cert_manager_manifest || return 1

  # Wait for the deployments to be created by the API server
  ensure_k8s_resource_exists "deployment/cert-manager-cainjector" "cert-manager" || return 1
  ensure_k8s_resource_exists "deployment/cert-manager" "cert-manager" || return 1
  ensure_k8s_resource_exists "deployment/cert-manager-webhook" "cert-manager" || return 1

  if [ "$is_autopilot" = "true" ]; then
    # Patch deployments to disable leader election due to Autopilot kube-system namespace restrictions
    print_info "Patching cert-manager cainjector and controller arguments..."
    kubectl patch deployment cert-manager-cainjector -n cert-manager --type='json' -p='[{"op": "replace", "path": "/spec/template/spec/containers/0/args/1", "value": "--leader-elect=false"}]' || return 1
    kubectl patch deployment cert-manager -n cert-manager --type='json' -p='[{"op": "replace", "path": "/spec/template/spec/containers/0/args/2", "value": "--leader-elect=false"}]' || return 1
  fi

  print_info "Patching cert-manager resources to comply with baseline quotas..."
  local resources_patch='[{"op": "add", "path": "/spec/template/spec/containers/0/resources", "value": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"cpu": "100m", "memory": "128Mi"}}}]'
  kubectl patch deployment cert-manager -n cert-manager --type='json' -p="${resources_patch}" || return 1
  kubectl patch deployment cert-manager-cainjector -n cert-manager --type='json' -p="${resources_patch}" || return 1
  kubectl patch deployment cert-manager-webhook -n cert-manager --type='json' -p="${resources_patch}" || return 1

  # Wait for cert-manager pods to become healthy
  wait_for_k8s_resource "deployment/cert-manager" "cert-manager" "Available" "120s" || return 1
  wait_for_k8s_resource "deployment/cert-manager-cainjector" "cert-manager" "Available" "120s" || return 1
  wait_for_k8s_resource "deployment/cert-manager-webhook" "cert-manager" "Available" "120s" || return 1
}

# Step 3: Deploy Operator (CRDs & Controller manager)
verify_operator() {
  # Always return false to ensure operator updates/re-deployments are applied
  return 1
}
execute_operator() {
  print_info "Installing Custom Resource Definitions (CRDs)..."
  make -C "$OPERATOR_DIR" install || return 1
  local operator_image_ref
  operator_image_ref="$(qualify_image_ref "$OPERATOR_IMAGE")" || return 1
  print_info "Deploying Operator Controller Manager (${operator_image_ref}) to the GKE cluster..."
  make -C "$OPERATOR_DIR" deploy IMG="${IMG:-$operator_image_ref}" || return 1

  # Propagate image overrides to the operator so PlatformAgent CRs created
  # without an explicit spec.deployment.image also pull from the custom
  # registry (see PLATFORM_AGENT_IMAGE et al. in config/manager/manager.yaml).
  # Precedence: explicit PLATFORM_AGENT_IMAGE > custom AGENT_IMAGE > custom
  # REGISTRY_PREFIX. Nothing is set for a default install so the operator's
  # compiled-in default stays authoritative.
  #
  # Every reference goes through qualify_image_ref: the saved *_IMAGE values
  # are bare repository paths (IMAGE_TAG is per-run and never persisted), and
  # an untagged value reaches the operator as ':latest', which no step of this
  # provisioner pushes. FLUENT_BIT_IMAGE is exempt — it names an upstream
  # fluent/fluent-bit release whose tag has nothing to do with IMAGE_TAG.
  #
  # CREDENTIAL_PROXY_IMAGE is unset unless a user pins the sidecar by hand:
  # install.sh deliberately does not write it, because the operator derives the
  # sidecar from each CR's own agent image and an env override wins over that
  # derivation for every CR in the cluster (resolveCredentialProxyImage).
  # Each reference is qualified into a variable first: a command substitution
  # inside an array element discards the helper's exit status, so a failure
  # would otherwise be forwarded to the operator as an empty override.
  local env_overrides=() agent_image_ref="" proxy_image_ref=""
  if [ -n "${PLATFORM_AGENT_IMAGE:-}" ]; then
    agent_image_ref="$(qualify_image_ref "$PLATFORM_AGENT_IMAGE")" || return 1
  elif [ -n "${AGENT_IMAGE:-}" ] && [ "${AGENT_IMAGE}" != "$(registry_prefix)/platform-agent" ]; then
    # A custom AGENT_IMAGE feeds the CR rendered in provision_08; mirror it to
    # the operator so hand-written CRs that omit spec.deployment.image pull
    # from the same place.
    agent_image_ref="$(qualify_image_ref "$AGENT_IMAGE")" || return 1
  elif [ "$(registry_prefix)" != "$DEFAULT_REGISTRY_PREFIX" ]; then
    agent_image_ref="$(qualify_image_ref "$(registry_prefix)/platform-agent")" || return 1
  fi
  if [ -n "$agent_image_ref" ]; then
    env_overrides+=("PLATFORM_AGENT_IMAGE=${agent_image_ref}")
  fi
  # No derived default for the credential proxy: the operator already builds
  # the sidecar reference from the agent image by swapping the last path
  # element (resolveCredentialProxyImage), which lands on the mirror as soon as
  # PLATFORM_AGENT_IMAGE above does. Only an explicit override needs passing.
  if [ -n "${CREDENTIAL_PROXY_IMAGE:-}" ]; then
    proxy_image_ref="$(qualify_image_ref "$CREDENTIAL_PROXY_IMAGE")" || return 1
    env_overrides+=("CREDENTIAL_PROXY_IMAGE=${proxy_image_ref}")
  fi
  # fluent-bit has no such derivation — the operator's only knob is this env
  # var — so a mirrored install has to be told, or every agent pod keeps
  # pulling the logging sidecar from Docker Hub.
  if [ -n "${FLUENT_BIT_IMAGE:-}" ]; then
    env_overrides+=("FLUENT_BIT_IMAGE=${FLUENT_BIT_IMAGE}")
  elif [ -n "$(third_party_registry_prefix)" ]; then
    # Assign first: inside a command substitution the failure status is
    # discarded, and run_step calls this function from an `if`, which suspends
    # set -e. An unresolvable entry would then set FLUENT_BIT_IMAGE= empty,
    # which the operator reads as unset and answers with Docker Hub — the
    # exact pull this branch exists to prevent, reported as a successful step.
    local fluent_bit
    fluent_bit="$(third_party_image fluent-bit)" || return 1
    env_overrides+=("FLUENT_BIT_IMAGE=${fluent_bit}")
  fi
  if [ ${#env_overrides[@]} -gt 0 ]; then
    print_info "Setting operator image overrides: ${env_overrides[*]}"
    kubectl set env deployment/kubeagents-controller-manager -n "${NAMESPACE:-kubeagents-system}" "${env_overrides[@]}" || return 1
  fi

  wait_for_k8s_resource "deployment/kubeagents-controller-manager" "${NAMESPACE:-kubeagents-system}" "Available" "180s" || return 1
}

# Step 1b: Ensure Filestore CSI Driver is enabled for RWX storage
verify_filestore_addon() {
  local enabled
  enabled=$(gcloud container clusters describe "$CLUSTER_NAME" --location="$REGION" --project="$PROJECT_ID" --format="value(addonsConfig.gcpFilestoreCsiDriverConfig.enabled)" 2>/dev/null || echo "false")
  [ "$enabled" = "True" ] || [ "$enabled" = "true" ]
}
execute_filestore_addon() {
  print_info "Enabling GKE Filestore CSI Driver for RWX storage support..."
  local active_op
  active_op=$(gcloud container operations list --location="$REGION" --project="$PROJECT_ID" --filter="targetLink:$CLUSTER_NAME AND status=RUNNING" --format="value(name)" 2>/dev/null | head -n1)
  if [ -n "$active_op" ]; then
    print_info "Waiting for ongoing cluster operation $active_op to complete..."
    gcloud container operations wait "$active_op" --location="$REGION" --project="$PROJECT_ID" || true
  fi

  gcloud container clusters update "$CLUSTER_NAME" \
      --location "$REGION" \
      --update-addons GcpFilestoreCsiDriver=ENABLED \
      --project "$PROJECT_ID" || return 1
}

# Step 1c: Ensure GKE Dataplane V2 is enabled for built-in NetworkPolicy isolation
verify_networkpolicy_addon() {
  local dp_provider
  dp_provider=$(gcloud container clusters describe "$CLUSTER_NAME" --region="$REGION" --project="$PROJECT_ID" --format="value(networkConfig.datapathProvider)" 2>/dev/null || echo "")
  if [ "$dp_provider" = "ADVANCED_DATAPATH" ]; then
    print_info "GKE Datapath V2 (ADVANCED_DATAPATH) is enabled; Kubernetes NetworkPolicy enforcement is built-in by default."
    return 0
  fi

  local legacy_np
  legacy_np=$(gcloud container clusters describe "$CLUSTER_NAME" --region="$REGION" --project="$PROJECT_ID" --format="value(networkPolicy.enabled)" 2>/dev/null || echo "False")
  if [ "$legacy_np" = "True" ] || [ "$legacy_np" = "true" ]; then
    print_info "Legacy GKE Network Policy (Calico) is already enabled."
    return 0
  fi

  print_info "Neither GKE Dataplane V2 nor Legacy Network Policy is enabled on cluster $CLUSTER_NAME."
  return 1
}
execute_networkpolicy_addon() {
  if verify_networkpolicy_addon; then
    return 0
  fi

  print_info "GKE Dataplane V2 is not enabled. Falling back to enabling Legacy GKE Network Policy (Calico)..."

  confirm_action "Enabling NetworkPolicy on existing cluster '$CLUSTER_NAME' will trigger a rolling restart of all cluster node pools." \
    "Cluster:$CLUSTER_NAME" \
    "Region:$REGION"

  local active_op
  active_op=$(gcloud container operations list --region="$REGION" --project="$PROJECT_ID" --filter="targetLink:$CLUSTER_NAME AND status=RUNNING" --format="value(name)" 2>/dev/null | head -n1)
  if [ -n "$active_op" ]; then
    print_info "Waiting for ongoing cluster operation $active_op to complete before updating network policy addon..."
    gcloud container operations wait "$active_op" --region="$REGION" --project="$PROJECT_ID" || print_warning "Operation wait returned non-zero (operation may have completed between list and wait); proceeding to cluster update..."
  fi

  print_info "Enabling NetworkPolicy addon on the cluster master..."
  gcloud container clusters update "$CLUSTER_NAME" \
      --region "$REGION" \
      --update-addons NetworkPolicy=ENABLED \
      --project "$PROJECT_ID" \
      --quiet || return 1

  active_op=$(gcloud container operations list --region="$REGION" --project="$PROJECT_ID" --filter="targetLink:$CLUSTER_NAME AND status=RUNNING" --format="value(name)" 2>/dev/null | head -n1)
  if [ -n "$active_op" ]; then
    print_info "Waiting for ongoing cluster operation $active_op to complete before enabling network policy enforcement..."
    gcloud container operations wait "$active_op" --region="$REGION" --project="$PROJECT_ID" || print_warning "Operation wait returned non-zero (operation may have completed between list and wait); proceeding to cluster update..."
  fi

  print_info "Enabling NetworkPolicy enforcement on the cluster nodes..."
  gcloud container clusters update "$CLUSTER_NAME" \
      --region "$REGION" \
      --enable-network-policy \
      --project "$PROJECT_ID" \
      --quiet || return 1

  active_op=$(gcloud container operations list --region="$REGION" --project="$PROJECT_ID" --filter="targetLink:$CLUSTER_NAME AND status=RUNNING" --format="value(name)" 2>/dev/null | head -n1)
  if [ -n "$active_op" ]; then
    print_info "Waiting for node pool rollout operation $active_op to complete..."
    gcloud container operations wait "$active_op" --region="$REGION" --project="$PROJECT_ID" || print_warning "Operation wait returned non-zero (operation may have completed between list and wait); proceeding..."
  fi

  print_warning "Legacy Network Policy enabled. Note that advanced FQDN-based NetworkPolicies will NOT be supported without Dataplane V2."
  return 0
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_deploy_step "1b. Ensure Filestore CSI Driver" verify_filestore_addon execute_filestore_addon 5
run_deploy_step "1c. Ensure NetworkPolicy Addon" verify_networkpolicy_addon execute_networkpolicy_addon 5
run_deploy_step "2. Ensure cert-manager" verify_cert_manager execute_cert_manager 5
run_deploy_step "3. Deploy Kubernetes Operator" verify_operator execute_operator 0

print_success "Kubernetes Operator deployed successfully!"
