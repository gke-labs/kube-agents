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

# The scenario driver for bench/tasks/ai-security-manual-names-reconciler.
#
# It plants ONE Deployment in ONE namespace on the host cluster: a model
# server on a floating tag (ai-security-audit SOP 3.6,
# `model-image-floating-tag`), with the two meta.helm.sh annotations a Helm
# install writes. The collector reads those annotations as the object's
# reconciler. Nothing declares the object, and no Pod runs to read a digest
# from, so the SOP makes the finding `manual`; `finish` then adds the
# reconciler note that the case grades.
#
# The finding is the only one on the stream: the eval fleet carries no other
# AI workload, so the ledger body renders it in full. On the obtainability
# stream the same note sits on one of about a hundred findings and the body
# cut leaves it out.
#
# Inert by design. spec.replicas is 0, so no Pod is scheduled and no image is
# pulled; the image is also on `registry.invalid`, a name that never
# resolves, so a scale-up by mistake pulls nothing either. No release record
# exists, so no Helm client and no controller acts on the annotations. The
# image path still matches the collector's AI_MODEL_IMAGE_RE (`/vllm/`),
# which is what makes the workload an AI workload. No env, no volume, no
# args: the other five checks have nothing to read.
#
# The credential-fetch and teardown structure is prebuilt/ai-model-defects'.
# test_collect.py's
# test_the_reconciled_model_plant_files_one_manual_candidate_with_its_reconciler
# holds the planted object as the collector reads it; change one and change
# both.

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
  ns       = var.defect_namespace
  workload = var.workload_name

  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
}

resource "null_resource" "defect" {
  triggers = {
    namespace     = local.ns
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id

    manifest = sha256(join("|", [
      local.ns,
      local.workload,
      var.model_image,
      var.helm_release_name,
      var.host_cluster_name,
    ]))
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      # Own kubeconfig, own credentials; the ambient context is some earlier
      # tofu task's cluster by the time this apply runs.
      kubeconfig_dir="$(mktemp -d)"
      trap 'rm -rf "$kubeconfig_dir"' EXIT
      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG

      project="${var.project_id}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      if [ -z "$project" ]; then
        echo "ERROR: no project id. Pass -var project_id=... or set a gcloud default project; this stack needs one to fetch credentials for ${var.host_cluster_name}." >&2
        exit 1
      fi

      gcloud container clusters get-credentials "${var.host_cluster_name}" \
        --location "${var.host_cluster_location}" --project "$project" --quiet

      # Destroy deletes the namespace with --wait=false, and the next
      # repetition can apply seconds later: applying into a namespace still
      # Terminating is refused, so wait the old one out first.
      if [ "$(kubectl get namespace "${local.ns}" -o jsonpath='{.status.phase}' 2>/dev/null)" = "Terminating" ]; then
        kubectl wait --for=delete "namespace/${local.ns}" --timeout=180s
      fi
      kubectl create namespace "${local.ns}" --dry-run=client -o yaml | kubectl apply -f -
      kubectl label namespace "${local.ns}" --overwrite \
        managed-by="${local.ci_labels["managed-by"]}" \
        build-id="${local.ci_labels["build-id"]}" \
        pull-number="${local.ci_labels["pull-number"]}"

      kubectl apply -f - <<'MANIFEST'
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: ${local.workload}
        namespace: ${local.ns}
        labels:
          app: ${local.workload}
        annotations:
          meta.helm.sh/release-name: ${var.helm_release_name}
          meta.helm.sh/release-namespace: ${local.ns}
      spec:
        replicas: 0
        selector:
          matchLabels:
            app: ${local.workload}
        template:
          metadata:
            labels:
              app: ${local.workload}
          spec:
            automountServiceAccountToken: false
            containers:
              - name: server
                image: ${var.model_image}
                resources:
                  requests:
                    cpu: 10m
                    memory: 16Mi
                  limits:
                    memory: 32Mi
      MANIFEST

      echo "Planted ${local.workload} (replicas 0, Helm release ${var.helm_release_name}) in ${local.ns} on ${var.host_cluster_name}."
    EOT
  }

  # Namespace-scoped teardown, own credentials, same shape and reasons as
  # prebuilt/autoops-incident's destroy provisioner.
  provisioner "local-exec" {
    when        = destroy
    on_failure  = continue
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
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

      kubectl delete namespace "${self.triggers.namespace}" --ignore-not-found --wait=false
    EOT
  }
}

# The subject cluster already exists; devops-bench reads these outputs
# unconditionally after up() and re-points the ambient kubeconfig at
# cluster_name — the cluster the task's safeguards read.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
