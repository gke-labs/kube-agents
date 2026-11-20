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

# The scenario driver for bench/tasks/security-serviceentry-host-collision.
#
# It plants four Istio ServiceEntries in four namespaces. Two declare
# var.colliding_host, exported to every namespace, with different addresses:
# any namespace that declares neither sees both, and Istio routes it by its own
# pick. Two declare var.scoped_host, each exported to its own namespace only:
# no namespace sees both, so the check must stay silent on that host.
#
# No pool cluster runs a mesh, so the ServiceEntry kind is installed here when
# the host does not already serve it: a minimal CRD in networking.istio.io
# whose schema keeps every field, which is all the collector's read needs.
# On a host that already serves the kind (a real Istio install) the stack
# uses it and never touches it. The objects are inert either way: with no
# mesh nothing reads them, and with one, `.example` is a reserved name nothing
# resolves and 192.0.2.0/24 a documentation range nothing routes.
#
# Isolation class: four namespaces, each the namespaced-plant module's, and at
# most one cluster-scoped CRD, created only when absent, labelled with this
# stack, and deleted on destroy only when it carries that label and no
# ServiceEntry is left anywhere on the cluster.

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
  stack    = "security-serviceentry-host-collision"
  crd_name = "serviceentries.networking.istio.io"
  # The version both the minimal CRD and every Istio release since 1.5 serve.
  api_version = "networking.istio.io/v1beta1"
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
  # namespace => [entry name, host, exportTo, address]
  entries = {
    "se-team-a" = ["external-payments", var.colliding_host, "*", "192.0.2.10"]
    "se-team-b" = ["external-payments", var.colliding_host, "*", "192.0.2.20"]
    "se-team-c" = ["ledger-api", var.scoped_host, ".", "192.0.2.30"]
    "se-team-d" = ["ledger-api", var.scoped_host, ".", "192.0.2.40"]
  }
  manifests = {
    for ns, e in local.entries : ns => <<-MANIFEST
      apiVersion: ${local.api_version}
      kind: ServiceEntry
      metadata:
        name: "${e[0]}"
        labels:
          managed-by: kube-agents-bench
          created-by-stack: ${local.stack}
      spec:
        hosts: ["${e[1]}"]
        exportTo: ["${e[2]}"]
        addresses: ["${e[3]}"]
        location: MESH_EXTERNAL
        resolution: STATIC
        ports:
          - number: 443
            name: tls
            protocol: TLS
        endpoints:
          - address: "${e[3]}"
    MANIFEST
  }
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
      # by this stack's earlier run or by a real Istio, and is used as it is.
      # A read error exits non-zero and stops the plant.
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
        group: networking.istio.io
        scope: Namespaced
        names:
          kind: ServiceEntry
          listKind: ServiceEntryList
          plural: serviceentries
          singular: serviceentry
          shortNames: ["se"]
        versions:
          - name: v1beta1
            served: true
            storage: true
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
      # The namespaces are deleted without waiting, so their ServiceEntries
      # can outlive this step by a little; wait for them before the CRD goes.
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
        echo "WARNING: ServiceEntries remain on ${self.triggers.host_cluster} ($left); ${self.triggers.crd} left in place. The next run reuses it." >&2
        exit 0
      fi
      kubectl delete crd "${self.triggers.crd}" --ignore-not-found
    EOT
  }
}

# One module per namespace, chained rather than for_each: each plant runs its
# own `gcloud container clusters get-credentials`, and four at once crash
# gcloud's credential store on a workstation ("[SSL] PEM lib", FileNotFoundError).
module "plant_a" {
  source = "../../modules/namespaced-plant"

  host_cluster_name     = var.host_cluster_name
  host_cluster_location = var.host_cluster_location
  project_id            = var.project_id
  namespace             = "se-team-a"
  stack                 = local.stack
  labels                = local.ci_labels
  manifest              = local.manifests["se-team-a"]

  depends_on = [null_resource.crd]
}

module "plant_b" {
  source = "../../modules/namespaced-plant"

  host_cluster_name     = var.host_cluster_name
  host_cluster_location = var.host_cluster_location
  project_id            = var.project_id
  namespace             = "se-team-b"
  stack                 = local.stack
  labels                = local.ci_labels
  manifest              = local.manifests["se-team-b"]

  depends_on = [module.plant_a]
}

module "plant_c" {
  source = "../../modules/namespaced-plant"

  host_cluster_name     = var.host_cluster_name
  host_cluster_location = var.host_cluster_location
  project_id            = var.project_id
  namespace             = "se-team-c"
  stack                 = local.stack
  labels                = local.ci_labels
  manifest              = local.manifests["se-team-c"]

  depends_on = [module.plant_b]
}

module "plant_d" {
  source = "../../modules/namespaced-plant"

  host_cluster_name     = var.host_cluster_name
  host_cluster_location = var.host_cluster_location
  project_id            = var.project_id
  namespace             = "se-team-d"
  stack                 = local.stack
  labels                = local.ci_labels
  manifest              = local.manifests["se-team-d"]

  depends_on = [module.plant_c]
}

output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
