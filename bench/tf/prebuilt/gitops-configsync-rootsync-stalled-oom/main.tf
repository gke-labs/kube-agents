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

# The scenario driver for bench/tasks/gitops-configsync-rootsync-stalled-oom.
#
# It plants one Config Sync RootSync, `root-sync` in var.namespace, whose
# status reports Stalled, for the length of one run. Compliance check 2.17
# (`configsync-reconciler-stalled`) reads that condition.
#
# No pool cluster runs Config Sync, so the RootSync kind is installed here
# when the host does not already serve it: a minimal CRD in configsync.gke.io
# with a status subresource and a schema that keeps every field, which is all
# the collector's read needs. No operator runs, so this stack writes the
# status itself, with the conditions Config Sync v1.25.2 writes when the
# reconciler Deployment of `root-sync` fails to progress
# (pkg/reconcilermanager/controllers/rootsync_controller.go, handleReconcileError;
# errors.go, ObjectReconcileError; pkg/rootsync/conditions.go, SetStalled):
# `Reconciling` True with the stage as its reason, and `Stalled` True with the
# failed object's kind as its reason, its kind, key and kstatus in the message,
# and the error summary for one error. The status names no cause, and the
# fixture claims none.
#
# The spec is inert: a repository on a reserved `.invalid` host, no credential,
# and a directory no repository holds, so nothing that matches a RootSync's
# source against an install's repository finds this one.
#
# A host that runs Config Sync already holds var.namespace: the module reads
# it as foreign and stops the plant, and nothing of the product's is touched.
#
# Isolation class: one namespace, the namespaced-plant module's, and at most
# one cluster-scoped CRD, created only when absent, labelled with this stack,
# and deleted on destroy only when it carries that label and no RootSync is
# left anywhere on the cluster.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0.0"
    }
  }
}

locals {
  stack       = "gitops-configsync-rootsync-stalled-oom"
  crd_name    = "rootsyncs.configsync.gke.io"
  api_version = "configsync.gke.io/v1beta1"
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
  manifest = <<-MANIFEST
    apiVersion: ${local.api_version}
    kind: RootSync
    metadata:
      name: "${var.target_resource_name}"
      labels:
        managed-by: kube-agents-bench
        created-by-stack: ${local.stack}
    spec:
      sourceFormat: unstructured
      sourceType: git
      git:
        repo: https://config-sync-fixture.invalid/fleet-config.git
        branch: main
        dir: fixture-no-such-directory
        auth: none
  MANIFEST

  # The reconciler of `root-sync` is the Deployment `root-reconciler`
  # (pkg/core/names.go); any other name gets the sync's name as a suffix.
  default_sync_name = "root-sync"
  reconciler_prefix = "root-reconciler"
  reconciler        = var.target_resource_name == local.default_sync_name ? local.reconciler_prefix : "${local.reconciler_prefix}-${var.target_resource_name}"
  stalled_stage     = "Setup"
  stalled_reason    = "Deployment"
  stalled_message   = "Deployment (${var.namespace}/${local.reconciler}) Failed: Progress deadline exceeded"
  # The status as a merge patch. The placeholder becomes the time of the write.
  time_placeholder = "STAMP"
  status_patch = jsonencode({
    status = {
      observedGeneration = 1
      conditions = [
        {
          type               = "Reconciling"
          status             = "True"
          reason             = local.stalled_stage
          message            = "${local.stalled_stage} stalled"
          lastUpdateTime     = local.time_placeholder
          lastTransitionTime = local.time_placeholder
        },
        {
          type               = "Stalled"
          status             = "True"
          reason             = local.stalled_reason
          message            = local.stalled_message
          lastUpdateTime     = local.time_placeholder
          lastTransitionTime = local.time_placeholder
          errorSummary = {
            totalCount                = 1
            errorCountAfterTruncation = 1
          }
        },
      ]
    }
  })
}

resource "null_resource" "crd" {
  triggers = {
    stack         = local.stack
    crd           = local.crd_name
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      readonly CRD_ESTABLISH_TIMEOUT="60s"
      kubeconfig_dir="$(mktemp -d)"
      trap 'rm -rf "$kubeconfig_dir"' EXIT
      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG
      project="${var.project_id}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      if [ -z "$project" ]; then
        echo "ERROR: no project id. Pass -var project_id=... or set a gcloud default project." >&2
        exit 1
      fi
      gcloud container clusters get-credentials "${var.host_cluster_name}" \
        --location "${var.host_cluster_location}" --project "$project" --quiet

      # Empty output is absent (install it); anything else is served already,
      # by this stack's earlier run or by a real Config Sync, and is used as it
      # is. A read error exits non-zero and stops the plant.
      owner="$(kubectl get crd "${local.crd_name}" --ignore-not-found \
        -o jsonpath='{.metadata.name}/{.metadata.labels.created-by-stack}')"
      if [ -n "$owner" ]; then
        echo "${local.crd_name} is already served ($owner); using it."
        exit 0
      fi
      kubectl create -f - <<'MANIFEST'
      apiVersion: apiextensions.k8s.io/v1
      kind: CustomResourceDefinition
      metadata:
        name: ${local.crd_name}
        labels:
          managed-by: ${local.ci_labels["managed-by"]}
          build-id: "${local.ci_labels["build-id"]}"
          pull-number: "${local.ci_labels["pull-number"]}"
          created-by-stack: ${local.stack}
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
            subresources:
              status: {}
            schema:
              openAPIV3Schema:
                type: object
                x-kubernetes-preserve-unknown-fields: true
      MANIFEST
      kubectl wait --for condition=established --timeout="$CRD_ESTABLISH_TIMEOUT" "crd/${local.crd_name}"
      echo "Installed ${local.crd_name} on ${var.host_cluster_name}."
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    on_failure  = continue
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      # The namespace is deleted without waiting, so its RootSync can outlive
      # this step by a little; wait for it before the CRD goes.
      readonly DRAIN_ATTEMPTS=36
      readonly DRAIN_INTERVAL_S=5
      kubeconfig_dir="$(mktemp -d)"
      trap 'rm -rf "$kubeconfig_dir"' EXIT
      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG
      project="${self.triggers.host_project}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      gcloud container clusters get-credentials "${self.triggers.host_cluster}" \
        --location "${self.triggers.host_location}" --project "$project" --quiet
      owner="$(kubectl get crd "${self.triggers.crd}" --ignore-not-found \
        -o jsonpath='{.metadata.name}/{.metadata.labels.created-by-stack}')"
      if [ "$owner" != "${self.triggers.crd}/${self.triggers.stack}" ]; then
        echo "${self.triggers.crd} was not installed by this stack ($${owner:-absent}); left in place."
        exit 0
      fi
      # Deleting a CRD deletes every object of its kind. Only an empty one goes.
      for _ in $(seq "$DRAIN_ATTEMPTS"); do
        left="$(kubectl get "${self.triggers.crd}" -A -o name)"
        [ -z "$left" ] && break
        sleep "$DRAIN_INTERVAL_S"
      done
      if [ -n "$left" ]; then
        echo "WARNING: RootSyncs remain on ${self.triggers.host_cluster} ($left); ${self.triggers.crd} left in place. The next run reuses it." >&2
        exit 0
      fi
      kubectl delete crd "${self.triggers.crd}" --ignore-not-found
    EOT
  }
}

module "plant" {
  source = "../../modules/namespaced-plant"

  host_cluster_name     = var.host_cluster_name
  host_cluster_location = var.host_cluster_location
  project_id            = var.project_id
  namespace             = var.namespace
  stack                 = local.stack
  labels                = local.ci_labels
  manifest              = local.manifest

  depends_on = [null_resource.crd]
}

# The status, written after the object exists: a create ignores `status` on a
# kind with a status subresource. A failure here leaves the namespace to the
# module's destroy, which removes the object with it.
resource "null_resource" "status" {
  triggers = {
    namespace = var.namespace
    name      = var.target_resource_name
    patch     = sha256(local.status_patch)
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      readonly STALLED_STATUS_PATH='{.status.conditions[?(@.type=="Stalled")].status}'
      readonly STALLED_TRUE="True"
      readonly STAMP_FORMAT='+%Y-%m-%dT%H:%M:%SZ'
      kubeconfig_dir="$(mktemp -d)"
      trap 'rm -rf "$kubeconfig_dir"' EXIT
      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG
      project="${var.project_id}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      gcloud container clusters get-credentials "${var.host_cluster_name}" \
        --location "${var.host_cluster_location}" --project "$project" --quiet
      stamp="$(date -u "$STAMP_FORMAT")"
      patch="$${STATUS_PATCH//$TIME_PLACEHOLDER/$stamp}"
      kubectl --namespace "${var.namespace}" patch "${local.crd_name}" "${var.target_resource_name}" \
        --subresource=status --type=merge --patch "$patch"
      stalled="$(kubectl --namespace "${var.namespace}" get "${local.crd_name}" "${var.target_resource_name}" \
        -o jsonpath="$STALLED_STATUS_PATH")"
      if [ "$stalled" != "$STALLED_TRUE" ]; then
        echo "ERROR: ${var.target_resource_name} does not report Stalled after the status write (read: '$stalled')." >&2
        exit 1
      fi
      echo "Wrote the Stalled status of ${var.namespace}/${var.target_resource_name} on ${var.host_cluster_name}."
    EOT
    environment = {
      STATUS_PATCH     = local.status_patch
      TIME_PLACEHOLDER = local.time_placeholder
    }
  }

  depends_on = [module.plant]
}

output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
