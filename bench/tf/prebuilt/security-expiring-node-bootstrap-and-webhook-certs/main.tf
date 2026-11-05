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

# The scenario driver for bench/tasks/security-expiring-node-bootstrap-and-webhook-certs.
#
# It plants one ValidatingWebhookConfiguration whose caBundle is a committed,
# already-expired self-signed certificate (expired-ca.pem beside this file;
# a certificate only, no key). The webhook is inert: failurePolicy Ignore, one
# rule on an API group nothing serves, the system namespaces excluded, and no
# backend. No compliance check reads a CA bundle's notAfter today; the case
# carries expected_fail: true and the planted object is what a fix would have
# to name. A TLS Secret would be the other half of the scenario, but the
# agent's read role reaches no Secret, so a fixture there could never flip.
#
# Isolation class: one cluster-scoped object, removed by this stack's destroy.
# The teardown sweep selects webhook configurations by the chart's own label,
# not this one, so a leftover from a killed run is removed by the next plant
# (which deletes its own name first) or by hand.

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
  name      = var.target_resource_name
  ca_bundle = filebase64("${path.module}/expired-ca.pem")
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
}

resource "null_resource" "scenario" {
  triggers = {
    name          = local.name
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      kubeconfig_dir="$(mktemp -d)"
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

      # A plant that fails must not leave the configuration behind: Terraform
      # taints a resource whose create-time provisioner failed and skips the
      # destroy-time provisioner on a tainted resource. `planted` is set only
      # after a successful create, so the trap removes only what this run made;
      # `set +e` keeps errexit from aborting the trap at its first failing
      # command, and the TERM/INT trap makes a Prow deadline run it too.
      planted=""
      on_exit() {
        status=$?
        set +e
        if [ "$status" -ne 0 ] && [ -n "$planted" ]; then
          echo "Plant failed (exit $status); deleting ${local.name} so the next run starts clean." >&2
          kubectl delete validatingwebhookconfiguration "${local.name}" --ignore-not-found >&2
        fi
        rm -rf "$kubeconfig_dir"
      }
      trap on_exit EXIT
      trap 'exit 143' TERM INT

      # One read tells the three states apart with no stderr grammar: empty
      # output is absent (plant), "<name>/<stack>" is this stack's leftover
      # (delete it, then plant), any other owner is a stop, and a non-zero exit
      # is a read error, which is a stop too.
      leftover="$(kubectl get validatingwebhookconfiguration "${local.name}" --ignore-not-found \
        -o jsonpath='{.metadata.name}/{.metadata.labels.created-by-stack}')"
      if [ -n "$leftover" ]; then
        if [ "$leftover" != "${local.name}/security-expiring-node-bootstrap-and-webhook-certs" ]; then
          echo "ERROR: ValidatingWebhookConfiguration ${local.name} already exists on ${var.host_cluster_name} and was not created by this stack ($leftover); it will not be changed. Remove it by hand if it is stale." >&2
          exit 1
        fi
        echo "Found a leftover ${local.name} from an earlier run; deleting it before planting."
        kubectl delete validatingwebhookconfiguration "${local.name}" --ignore-not-found
      fi
      # `create`, not `apply`: a configuration that appears between the read
      # and the write (AlreadyExists) stops the plant instead of being adopted.
      kubectl create -f - <<'MANIFEST'
      apiVersion: admissionregistration.k8s.io/v1
      kind: ValidatingWebhookConfiguration
      metadata:
        name: ${local.name}
        labels:
          managed-by: ${local.ci_labels["managed-by"]}
          build-id: "${local.ci_labels["build-id"]}"
          pull-number: "${local.ci_labels["pull-number"]}"
          created-by-stack: security-expiring-node-bootstrap-and-webhook-certs
      webhooks:
        - name: expired-ca.bench.kube-agents.dev
          admissionReviewVersions: ["v1"]
          sideEffects: None
          failurePolicy: Ignore
          rules:
            - apiGroups: ["bench.kube-agents.dev"]
              apiVersions: ["v1"]
              resources: ["probes"]
              operations: ["CREATE"]
          namespaceSelector:
            matchExpressions:
              - key: kubernetes.io/metadata.name
                operator: NotIn
                values: ["kube-system", "config-management-system", "kubeagents-system"]
          clientConfig:
            service:
              namespace: seeded-admission
              name: strict-policy-webhook
              path: /validate
              port: 443
            caBundle: ${local.ca_bundle}
      MANIFEST
      planted=1
      echo "Planted scenario security-expiring-node-bootstrap-and-webhook-certs on ${var.host_cluster_name}."
    EOT
  }

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
      # The same one read as the plant: delete only on a label match; a read
      # error exits non-zero so the leftover is reported, never skipped.
      owner="$(kubectl get validatingwebhookconfiguration "${self.triggers.name}" --ignore-not-found \
        -o jsonpath='{.metadata.name}/{.metadata.labels.created-by-stack}')"
      if [ "$owner" = "${self.triggers.name}/security-expiring-node-bootstrap-and-webhook-certs" ]; then
        kubectl delete validatingwebhookconfiguration "${self.triggers.name}" --ignore-not-found
      elif [ -n "$owner" ]; then
        echo "WARNING: ValidatingWebhookConfiguration ${self.triggers.name} on ${self.triggers.host_cluster} was not created by this stack ($owner); left in place." >&2
      fi
    EOT
  }
}

output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
