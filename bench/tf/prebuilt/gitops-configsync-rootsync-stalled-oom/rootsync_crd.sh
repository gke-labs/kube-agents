#!/usr/bin/env bash
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

# The cluster-scoped half of the stalled-RootSync plant: `plant` installs a
# minimal RootSync CRD on the host cluster, `destroy` removes it. It keeps the
# guards of bench/tf/modules/namespaced-plant for a cluster-scoped object
# (bench/README.md, "Validated Capabilities suite", rule 6): one ownership read
# that never takes an error for "absent", a pre-clear that removes this
# stack's leftover and stops on a foreign owner, `create` and never `apply`,
# a failed plant that removes what it created, and a destroy that acts on
# the label match alone. Inputs arrive as RC_* variables from main.tf beside
# this file. Run against stub kubectl and gcloud by
# tests/test_configsync_stalled_stack.py.
set -euo pipefail

readonly RC_CRD="rootsyncs.configsync.gke.io"
readonly RC_LABEL_OWNER="created-by-stack"
readonly RC_ESTABLISH_TIMEOUT="60s"
readonly RC_LEFTOVER_DELETE_TIMEOUT="180s"
readonly RC_DRAIN_ATTEMPTS=36
readonly RC_DRAIN_INTERVAL_S=5
readonly RC_EXIT_SIGNALLED=143
readonly RC_EXIT_USAGE=2

rc_require() { # <name>: the variable must be set and non-empty
  if [ -z "${!1:-}" ]; then
    echo "ERROR: $1 is not set; the stack's provisioner passes it." >&2
    exit 1
  fi
}

rc_credentials() {
  local project="${RC_PROJECT:-}"
  if [ -z "$project" ]; then
    project="$(gcloud config get-value project 2>/dev/null || true)"
  fi
  if [ -z "$project" ]; then
    echo "ERROR: no project id. Pass project_id or set a gcloud default project." >&2
    exit 1
  fi
  gcloud container clusters get-credentials "$RC_HOST_CLUSTER" \
    --location "$RC_HOST_LOCATION" --project "$project" --quiet
}

# The CRD's owner, read once: prints "" when the CRD is absent, "<name>/<label>"
# when present (the label part empty when it carries none). A read error exits
# non-zero through errexit; it is never read as "absent".
rc_owner() {
  kubectl get crd "$RC_CRD" --ignore-not-found \
    -o jsonpath="{.metadata.name}/{.metadata.labels.${RC_LABEL_OWNER}}"
}

# The RootSyncs this stack did not create, by name. Deleting a CRD deletes
# every object of its kind, so the CRD goes only when this prints nothing.
rc_foreign_objects() {
  kubectl get "$RC_CRD" --all-namespaces -l "${RC_LABEL_OWNER}!=${RC_STACK}" -o name
}

rc_plant() {
  rc_require RC_HOST_CLUSTER; rc_require RC_HOST_LOCATION; rc_require RC_STACK
  rc_require RC_MANAGED_BY; rc_require RC_BUILD_ID; rc_require RC_PULL_NUMBER
  # Not locals: the EXIT trap runs after this function has returned.
  rc_kubeconfig_dir="$(mktemp -d)"
  rc_planted=""
  # A plant that fails after the CRD exists removes it on the way out:
  # Terraform taints the resource and skips its destroy provisioner. The flag
  # is set before the create is sent, and the trap deletes only a CRD that
  # carries this stack's label, so a refused create over a foreign CRD
  # deletes nothing.
  rc_on_exit() {
    local status=$?
    set +e
    if [ "$status" -ne 0 ] && [ -n "$rc_planted" ]; then
      local owner
      owner="$(rc_owner 2>/dev/null)"
      if [ "$owner" = "$RC_CRD/$RC_STACK" ]; then
        echo "Plant failed (exit $status); deleting $RC_CRD so the next run starts clean." >&2
        kubectl delete crd "$RC_CRD" --ignore-not-found --wait=false >&2
      fi
    fi
    rm -rf "$rc_kubeconfig_dir"
  }
  trap rc_on_exit EXIT
  trap 'exit "$RC_EXIT_SIGNALLED"' TERM INT
  KUBECONFIG="$rc_kubeconfig_dir/config"
  export KUBECONFIG
  rc_credentials

  # The pre-clear: one read tells the three states apart. A CRD of another
  # owner is a real Config Sync, or someone's own: the plant stops, and the
  # case does not run on that host.
  local owner
  owner="$(rc_owner)"
  if [ -n "$owner" ]; then
    if [ "${owner#*/}" != "$RC_STACK" ]; then
      echo "ERROR: $RC_CRD is already served on $RC_HOST_CLUSTER and was not installed by this stack ($RC_LABEL_OWNER='${owner#*/}'); it will not be touched. A host that runs Config Sync cannot take this plant." >&2
      exit 1
    fi
    local foreign
    foreign="$(rc_foreign_objects)"
    if [ -n "$foreign" ]; then
      echo "ERROR: a leftover $RC_CRD of this stack holds RootSyncs it did not create ($foreign); it will not be deleted. Remove them by hand." >&2
      exit 1
    fi
    echo "Found a leftover $RC_CRD from an earlier run; deleting it before planting."
    if ! kubectl delete crd "$RC_CRD" --ignore-not-found --wait=true --timeout="$RC_LEFTOVER_DELETE_TIMEOUT"; then
      echo "ERROR: could not clear the leftover $RC_CRD within $RC_LEFTOVER_DELETE_TIMEOUT." >&2
      exit 1
    fi
  fi

  # A schema that keeps every field, and no status subresource: the plant
  # writes the object with its status in one create, and the collector reads
  # the object's JSON, which is the same either way.
  rc_planted=1
  kubectl create -f - <<MANIFEST
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: $RC_CRD
  labels:
    managed-by: "$RC_MANAGED_BY"
    build-id: "$RC_BUILD_ID"
    pull-number: "$RC_PULL_NUMBER"
    $RC_LABEL_OWNER: "$RC_STACK"
spec:
  group: configsync.gke.io
  scope: Namespaced
  names:
    kind: RootSync
    listKind: RootSyncList
    plural: rootsyncs
    singular: rootsync
  versions:
    - name: v1beta1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
MANIFEST
  kubectl wait --for condition=established --timeout="$RC_ESTABLISH_TIMEOUT" "crd/$RC_CRD"
  echo "Installed $RC_CRD on $RC_HOST_CLUSTER."
}

rc_destroy() {
  rc_require RC_HOST_CLUSTER; rc_require RC_HOST_LOCATION; rc_require RC_STACK
  rc_kubeconfig_dir="$(mktemp -d)"
  trap 'rm -rf "$rc_kubeconfig_dir"' EXIT
  KUBECONFIG="$rc_kubeconfig_dir/config"
  export KUBECONFIG
  rc_credentials
  local owner
  owner="$(rc_owner)"
  if [ -z "$owner" ]; then
    echo "$RC_CRD is already gone."
    return 0
  fi
  if [ "${owner#*/}" != "$RC_STACK" ]; then
    echo "$RC_CRD was not installed by this stack ($RC_LABEL_OWNER='${owner#*/}'); leaving it in place."
    return 0
  fi
  # The namespace is deleted without waiting, so its RootSync can outlive the
  # module's destroy by a little; wait for it before the CRD goes.
  local left="" attempt=0
  while [ "$attempt" -lt "$RC_DRAIN_ATTEMPTS" ]; do
    attempt=$((attempt + 1))
    left="$(kubectl get "$RC_CRD" --all-namespaces -o name)"
    [ -z "$left" ] && break
    sleep "$RC_DRAIN_INTERVAL_S"
  done
  if [ -n "$left" ]; then
    echo "WARNING: RootSyncs remain on $RC_HOST_CLUSTER ($left); $RC_CRD left in place. The next plant clears it." >&2
    return 0
  fi
  kubectl delete crd "$RC_CRD" --ignore-not-found
}

case "${1:-}" in
  plant) rc_plant ;;
  destroy) rc_destroy ;;
  *) echo "usage: $0 plant|destroy" >&2; exit "$RC_EXIT_USAGE" ;;
esac
