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

# The scenario driver for bench/tasks/gitops-configsync-rootsync-stalled.
#
# It plants one Config Sync RootSync, `root-sync` in var.namespace, for one
# run, in one of the two states compliance check 2.17
# (`configsync-reconciler-stalled`) reads (var.reconciler_state):
#
#  - `stalled`, the default and the stack's first case: the sync object's
#    status reports Stalled.
#  - `out-of-memory`, for bench/tasks/gitops-configsync-rootsync-oomkilled:
#    the sync object reads healthy, and beside it a stand-in reconciler pod is
#    killed for memory at every start. That is a live reconciler loop at a
#    moment between two kills: each restart makes the Deployment available,
#    the object's Reconciling condition goes False, and only the pod says what
#    is wrong. The pod carries the labels Config Sync puts on a reconciler
#    pod, with the sync object it serves; its image is not Config Sync's.
#
# One stack for both, so that either case's plant clears the other's
# leftover: they share the namespace, the CRD and the label that owns them.
#
# No pool cluster runs Config Sync, so the stack installs the RootSync kind
# itself (rootsync_crd.sh beside this file): a minimal CRD in
# configsync.gke.io whose schema keeps every field, which is all the
# collector's read needs. It has no status subresource, so one create writes
# the object with its status, and this stack holds its manifest and nothing
# after it. No operator runs. The status is the one Config Sync v1.25.2 writes
# when the reconciler Deployment of `root-sync` passes its progress deadline
# (pkg/reconcilermanager/controllers/rootsync_controller.go, handleReconcileError;
# errors.go, ObjectReconcileError; pkg/rootsync/conditions.go, SetStalled):
# `Reconciling` True with the stage as its reason, and `Stalled` True with the
# failed object's kind as its reason, its kind, key and kstatus in the message,
# and the error summary for one error. The conditions are a copy of a live
# object: Config Sync v1.25.2 wrote them ten minutes after its reconciler
# became unschedulable. The status names no cause, and the fixture claims
# none.
#
# The spec is inert: a repository on a reserved `.invalid` host, no credential,
# and a directory no repository holds, so nothing that matches a RootSync's
# source against an install's repository finds this one.
#
# A host that runs Config Sync serves the kind and holds var.namespace
# already: the CRD step and the module each read that as foreign and stop the
# plant, and nothing of the product's is touched. One path is not guarded: the
# module owns a namespace by its label alone, so a destroy from a kept state,
# after Config Sync was applied over this stack's plant, deletes the
# namespace with what the product put in it. A run starts from an empty
# state, and the CRD step stops a plant over such a host before the module
# runs, so the lane does not reach it. The lease sweep
# (hack/ci_sweep_bench_plants.sh) also deletes the CRD by the bench label; it
# does not read the CRD's shape, and refuses one that holds an object without
# the label.
#
# Isolation class: one namespace, the namespaced-plant module's, and one
# cluster-scoped CRD with the module's guards: created only on a host that
# serves none, labelled with this stack, removed by a failed plant, and
# deleted on destroy only when it carries that label, still has this stack's
# shape, and no RootSync is left.

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
  stack       = "gitops-configsync-rootsync-stalled"
  api_version = "configsync.gke.io/v1beta1"
  crd_script  = "${path.module}/rootsync_crd.sh"
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }

  # The reconciler of `root-sync` is the Deployment `root-reconciler`
  # (pkg/core/names.go); any other name gets the sync's name as a suffix.
  default_sync_name = "root-sync"
  reconciler_prefix = "root-reconciler"
  reconciler        = var.target_resource_name == local.default_sync_name ? local.reconciler_prefix : "${local.reconciler_prefix}-${var.target_resource_name}"
  stalled_stage     = "Setup"
  stalled_reason    = "Deployment"
  stalled_message   = "Deployment (${var.namespace}/${local.reconciler}) Failed: Progress deadline exceeded"
  # The live object's own times: `Reconciling` was already True while the
  # Deployment was in progress, so it keeps that earlier transition time; the
  # rest carry the time of the Stalled write. No check reads them, and literal
  # times keep a second plan from replacing the plant.
  reconciling_since = "2026-11-20T20:56:06Z"
  stalled_at        = "2026-11-20T21:18:23Z"

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
    status:
      observedGeneration: 1
      conditions:
        - type: Reconciling
          status: "True"
          reason: "${local.stalled_stage}"
          message: "${local.stalled_stage} stalled"
          lastUpdateTime: "${local.stalled_at}"
          lastTransitionTime: "${local.reconciling_since}"
        - type: Stalled
          status: "True"
          reason: "${local.stalled_reason}"
          message: "${local.stalled_message}"
          errorSummary:
            totalCount: 1
            errorCountAfterTruncation: 1
          lastUpdateTime: "${local.stalled_at}"
          lastTransitionTime: "${local.stalled_at}"
  MANIFEST

  # The out-of-memory state. The sync object carries the one condition a live
  # object held between two kills of its reconciler: nothing in it says the
  # reconciler is down. Beside it, the stand-in reconciler: the labels of a
  # real reconciler pod, and a container of the reconciler's name that
  # allocates past its limit at every start. Its requests and limits are the
  # ones an Autopilot host wrote for it (read back from a live plant on GKE
  # 1.35), so the object is the same on either host type.
  out_of_memory          = var.reconciler_state == "out-of-memory"
  between_kills_at       = "2026-11-20T23:50:41Z"
  manifest_out_of_memory = <<-MANIFEST
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
    status:
      observedGeneration: 1
      conditions:
        - type: Reconciling
          status: "False"
          lastUpdateTime: "${local.between_kills_at}"
          lastTransitionTime: "${local.between_kills_at}"
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: ${local.reconciler}
      labels:
        managed-by: kube-agents-bench
        created-by-stack: ${local.stack}
    spec:
      replicas: 1
      selector:
        matchLabels:
          configsync.gke.io/deployment-name: ${local.reconciler}
      template:
        metadata:
          labels:
            app: reconciler
            managed-by: kube-agents-bench
            configsync.gke.io/deployment-name: ${local.reconciler}
            configsync.gke.io/sync-kind: RootSync
            configsync.gke.io/sync-name: "${var.target_resource_name}"
            configsync.gke.io/sync-namespace: ${var.namespace}
        spec:
          automountServiceAccountToken: false
          containers:
            - name: reconciler
              image: busybox:1.36
              command: ["sh", "-c", "dd if=/dev/zero of=/dev/null bs=${var.allocate_mib}M count=1 && sleep 3600"]
              resources:
                requests:
                  cpu: 50m
                  memory: ${var.memory_limit_mib}Mi
                  ephemeral-storage: 1Gi
                limits:
                  memory: ${var.memory_limit_mib}Mi
                  ephemeral-storage: 1Gi
  MANIFEST
}

resource "null_resource" "crd" {
  triggers = {
    stack         = local.stack
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id
    script        = filesha256(local.crd_script)
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "bash '${local.crd_script}' plant"
    environment = {
      RC_HOST_CLUSTER  = var.host_cluster_name
      RC_HOST_LOCATION = var.host_cluster_location
      RC_PROJECT       = var.project_id
      RC_STACK         = local.stack
      RC_MANAGED_BY    = local.ci_labels["managed-by"]
      RC_BUILD_ID      = local.ci_labels["build-id"]
      RC_PULL_NUMBER   = local.ci_labels["pull-number"]
    }
  }

  provisioner "local-exec" {
    when        = destroy
    on_failure  = continue
    interpreter = ["/bin/bash", "-c"]
    command     = "bash \"$RC_SCRIPT\" destroy"
    environment = {
      RC_SCRIPT        = "${path.module}/rootsync_crd.sh"
      RC_HOST_CLUSTER  = self.triggers.host_cluster
      RC_HOST_LOCATION = self.triggers.host_location
      RC_PROJECT       = self.triggers.host_project
      RC_STACK         = self.triggers.stack
    }
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
  manifest              = local.out_of_memory ? local.manifest_out_of_memory : local.manifest

  depends_on = [null_resource.crd]
}

output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
