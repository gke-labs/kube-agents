#!/bin/bash
# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# The namespaced-plant module's shell: `plant` creates the namespace this
# stack owns and the objects the case plants in it; `destroy` removes the
# namespace, and with it everything planted. Inputs arrive as NP_* variables
# from the module's provisioners (main.tf beside this file). Run against stub
# kubectl and gcloud by tests/test_namespaced_plant.py.
set -euo pipefail

readonly NP_LABEL_OWNER="created-by-stack"
readonly NP_LABEL_MANAGED_BY="managed-by"
readonly NP_LABEL_BUILD_ID="build-id"
readonly NP_LABEL_PULL_NUMBER="pull-number"
readonly NP_LEFTOVER_DELETE_TIMEOUT="180s"
readonly NP_EXIT_SIGNALLED=143
readonly NP_EXIT_USAGE=2

np_require() { # <name>: the variable must be set and non-empty
  if [ -z "${!1:-}" ]; then
    echo "ERROR: $1 is not set; the module's provisioner passes it." >&2
    exit 1
  fi
}

np_credentials() {
  local project="${NP_PROJECT:-}"
  if [ -z "$project" ]; then
    project="$(gcloud config get-value project 2>/dev/null || true)"
  fi
  if [ -z "$project" ]; then
    echo "ERROR: no project id. Pass project_id or set a gcloud default project." >&2
    exit 1
  fi
  gcloud container clusters get-credentials "$NP_HOST_CLUSTER" \
    --location "$NP_HOST_LOCATION" --project "$project" --quiet
}

# The namespace's owner, read once: prints "" when the namespace is absent,
# "<name>/<label>" when present (the label part empty when it carries none).
# A read error (no credentials, no kubectl, an API error) exits non-zero
# through errexit; it is never read as "absent".
np_owner() {
  kubectl get namespace "$NP_NAMESPACE" --ignore-not-found \
    -o jsonpath="{.metadata.name}/{.metadata.labels.${NP_LABEL_OWNER}}"
}

np_plant() {
  np_require NP_HOST_CLUSTER; np_require NP_HOST_LOCATION; np_require NP_NAMESPACE
  np_require NP_STACK; np_require NP_MANAGED_BY; np_require NP_BUILD_ID
  np_require NP_PULL_NUMBER; np_require NP_MANIFEST
  # Not locals: the EXIT trap runs after this function has returned, and
  # under `set -u` a local would be unbound there.
  np_kubeconfig_dir="$(mktemp -d)"
  np_planted_ns=""
  # A plant that fails after the namespace exists removes it on the way out:
  # Terraform taints the resource and skips its destroy provisioner, so
  # nothing else would. np_planted_ns is set before the create is sent: a
  # signal during the create (bash runs the trap once the child exits) or a
  # lost response can leave the namespace committed with no success seen.
  # The trap therefore reads the owner label and deletes only a namespace
  # this stack created, so a refused create over a foreign namespace deletes
  # nothing. `set +e` inside the trap: errexit stays in force there, and the
  # first failing command would otherwise skip the delete.
  np_on_exit() {
    local status=$?
    set +e
    if [ "$status" -ne 0 ] && [ -n "$np_planted_ns" ]; then
      local owner
      owner="$(np_owner 2>/dev/null)"
      if [ "$owner" = "$NP_NAMESPACE/$NP_STACK" ]; then
        echo "Plant failed (exit $status); deleting $NP_NAMESPACE so the next run starts clean." >&2
        kubectl delete namespace "$NP_NAMESPACE" --ignore-not-found --wait=false >&2
      fi
    fi
    rm -rf "$np_kubeconfig_dir"
  }
  trap np_on_exit EXIT
  trap 'exit "$NP_EXIT_SIGNALLED"' TERM INT
  KUBECONFIG="$np_kubeconfig_dir/config"
  export KUBECONFIG
  np_credentials

  # The pre-clear: one read tells the three states apart.
  local owner
  owner="$(np_owner)"
  if [ -n "$owner" ]; then
    if [ "${owner#*/}" != "$NP_STACK" ]; then
      echo "ERROR: $NP_NAMESPACE already exists on $NP_HOST_CLUSTER and was not created by this stack ($NP_LABEL_OWNER='${owner#*/}'); it will not be deleted. Remove it by hand if it is stale." >&2
      exit 1
    fi
    echo "Found a leftover $NP_NAMESPACE from an earlier run; deleting it before planting."
    if ! kubectl delete namespace "$NP_NAMESPACE" --ignore-not-found --wait=true --timeout="$NP_LEFTOVER_DELETE_TIMEOUT"; then
      echo "ERROR: could not clear the leftover $NP_NAMESPACE within $NP_LEFTOVER_DELETE_TIMEOUT; a namespace wedged on a finalizer is the usual cause." >&2
      exit 1
    fi
  fi

  # Created with its labels in one request; `create` stops on AlreadyExists
  # rather than adopting a namespace the pre-clear did not see.
  np_planted_ns=1
  kubectl create -f - <<MANIFEST
apiVersion: v1
kind: Namespace
metadata:
  name: "$NP_NAMESPACE"
  labels:
    $NP_LABEL_MANAGED_BY: "$NP_MANAGED_BY"
    $NP_LABEL_BUILD_ID: "$NP_BUILD_ID"
    $NP_LABEL_PULL_NUMBER: "$NP_PULL_NUMBER"
    $NP_LABEL_OWNER: "$NP_STACK"
MANIFEST
  printf '%s\n' "$NP_MANIFEST" | kubectl --namespace "$NP_NAMESPACE" create -f -
  echo "Planted $NP_STACK in $NP_NAMESPACE on $NP_HOST_CLUSTER."
}

np_destroy() {
  np_require NP_HOST_CLUSTER; np_require NP_HOST_LOCATION; np_require NP_NAMESPACE; np_require NP_STACK
  np_kubeconfig_dir="$(mktemp -d)"
  trap 'rm -rf "$np_kubeconfig_dir"' EXIT
  KUBECONFIG="$np_kubeconfig_dir/config"
  export KUBECONFIG
  np_credentials
  local owner
  owner="$(np_owner)"
  if [ -z "$owner" ]; then
    echo "Namespace $NP_NAMESPACE is already gone."
    return 0
  fi
  if [ "${owner#*/}" = "$NP_STACK" ]; then
    # The namespace alone: the delete cascades to everything planted in it.
    kubectl delete namespace "$NP_NAMESPACE" --ignore-not-found --wait=false
  else
    echo "Namespace $NP_NAMESPACE was not created by this stack ($NP_LABEL_OWNER='${owner#*/}'); leaving it in place."
  fi
}

case "${1:-}" in
  plant) np_plant ;;
  destroy) np_destroy ;;
  *) echo "usage: $0 plant|destroy" >&2; exit "$NP_EXIT_USAGE" ;;
esac
