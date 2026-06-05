#!/usr/bin/env bash
# Helper script to provision LiteLLM gateway.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source vars if available and not already set
if [ -f "${PARENT_DIR}/vars.sh" ]; then
  source "${PARENT_DIR}/vars.sh"
fi

# Validation
if [ -z "${NAMESPACE}" ]; then
  echo "Error: NAMESPACE is not set." >&2
  exit 1
fi
if [ -z "${MODEL_PROVIDER}" ]; then
  echo "Error: MODEL_PROVIDER is not set." >&2
  exit 1
fi
if [ -z "${MODEL_DEFAULT_NAME}" ]; then
  echo "Error: MODEL_DEFAULT_NAME is not set." >&2
  exit 1
fi

verify() {
  echo "Verifying LiteLLM deployment..."
  kubectl get configmap litellm-config -n "${NAMESPACE}" >/dev/null 2>&1 && \
  kubectl get deployment litellm -n "${NAMESPACE}" >/dev/null 2>&1 && \
  kubectl get service litellm -n "${NAMESPACE}" >/dev/null 2>&1
}

deploy() {
  echo "Deploying LiteLLM gateway..."
  
  local tmp_dir
  tmp_dir=$(mktemp -d)
  
  # Ensure cleanup on exit safely
  trap '[[ -n "${tmp_dir}" ]] && rm -rf "${tmp_dir}"' EXIT
  
  # Process templates
  for template in "configmap.yaml.tmpl" "deployment.yaml.tmpl" "service.yaml.tmpl"; do
    local input="${SCRIPT_DIR}/${template}"
    local output="${tmp_dir}/${template%.tmpl}"
    
    if [ ! -f "${input}" ]; then
      echo "Error: Template file ${input} not found." >&2
      exit 1
    fi
    
    # Replace placeholders using envsubst
    envsubst '$NAMESPACE $MODEL_PROVIDER $MODEL_DEFAULT_NAME' < "${input}" > "${output}"
  done
  
  # Apply manifests
  kubectl apply -f "${tmp_dir}/configmap.yaml"
  kubectl apply -f "${tmp_dir}/deployment.yaml"
  kubectl apply -f "${tmp_dir}/service.yaml"
}

case "$1" in
  --verify)
    verify
    ;;
  --deploy)
    deploy
    ;;
  *)
    echo "Usage: $0 {--verify|--deploy}" >&2
    exit 1
    ;;
esac
