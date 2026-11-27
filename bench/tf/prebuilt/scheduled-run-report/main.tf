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

# A scenario driver for a case that asks about an audit run a scheduled task
# wrote to the broker report store (#6110).
#
# It plants, in the broker store, one run of var.audit_id an hour old with one
# critical finding whose cluster, project and namespace carry var.mark: what a
# scheduled run on a profile worker leaves there. A case then asks what the
# last audit found and checks that the reply names the mark. The shell sandbox
# reads the plant only when it reads the broker store, which is the change
# #6110 makes; on a sandbox that keeps its own file store the reply can't name
# it.
#
# scheduled_report.py does the work in the gateway pod's `platform-agent`
# container, which keeps the files the broker's store serves, with the same
# exec access the unchanged-finding-wording stack's broker teardown uses there.
# It writes the report with the function `finish` uses, and refuses unless its
# store root is the broker store's. The destroy removes what the seed wrote,
# by exact path, from the note the seed keeps beside the store, which it finds
# by var.mark without resolving the repository again, and puts back any
# `latest` the seed moved aside.

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
  python = "python3"
  # Every pod the operator runs for an agent carries this label; the gateway's
  # is the one with a `platform-agent` container.
  pod_selector      = "app.kubernetes.io/name=platform-agent"
  gateway_container = "platform-agent"
  script_b64        = base64encode(file("${path.module}/scheduled_report.py"))
  # How long an exec waits for a running pod; see
  # bench/tf/prebuilt/bootstrap-ranking/main.tf.
  pod_wait = 5
}

resource "null_resource" "scheduled_run_report" {
  triggers = {
    host_cluster      = var.host_cluster_name
    host_location     = var.host_cluster_location
    host_project      = var.project_id
    namespace         = var.agent_namespace
    pod_selector      = local.pod_selector
    gateway_container = local.gateway_container
    audit_id          = var.audit_id
    mark              = var.mark
    gitops_repo       = var.gitops_repo
    python            = local.python
    script_b64        = local.script_b64
    pod_wait          = local.pod_wait
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

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

      # The one running, not terminating, agent pod with a gateway container,
      # which holds the broker store's files.
      gateway=""
      while read -r running deleting; do
        [ -n "$running" ] && [ -z "$deleting" ] || continue
        names="$(kubectl get pod -n "${var.agent_namespace}" "$running" -o jsonpath='{.spec.containers[*].name}')"
        if [[ " $names " == *" ${local.gateway_container} "* ]]; then
          if [ -n "$gateway" ]; then
            echo "ERROR: more than one running agent pod has a ${local.gateway_container} container ($gateway, $running); the broker store's files are on one." >&2
            exit 1
          fi
          gateway="$running"
        fi
      done < <(kubectl get pods -n "${var.agent_namespace}" -l "${local.pod_selector}" \
        --field-selector=status.phase=Running \
        -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.deletionTimestamp}{"\n"}{end}')
      if [ -z "$gateway" ]; then
        echo "ERROR: no running agent pod with a ${local.gateway_container} container, which holds the broker store's files." >&2
        exit 1
      fi

      printf '%s' '${local.script_b64}' | base64 -d | \
        kubectl exec -i -n "${var.agent_namespace}" "$gateway" -c "${local.gateway_container}" \
          --pod-running-timeout=${local.pod_wait}s -- \
          ${local.python} - seed "${var.audit_id}" "${var.mark}" "${var.gitops_repo}" || {
        # A failed create taints the resource, and a tainted resource gets no
        # destroy-time provisioner, so the seed's own failure runs teardown
        # here. Teardown undoes whatever the seed's note records.
        status=$?
        printf '%s' '${local.script_b64}' | base64 -d | \
          kubectl exec -i -n "${var.agent_namespace}" "$gateway" -c "${local.gateway_container}" \
            --pod-running-timeout=${local.pod_wait}s -- \
            ${local.python} - teardown "${var.audit_id}" "${var.mark}" || true
        exit "$status"
      }
    EOT
  }

  # No on_failure = continue: a destroy that cannot remove the plant fails the
  # run, rather than leaving a report that Terraform records as removed.
  provisioner "local-exec" {
    when        = destroy
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

      gateway=""
      while read -r running deleting; do
        [ -n "$running" ] && [ -z "$deleting" ] || continue
        names="$(kubectl get pod -n "${self.triggers.namespace}" "$running" -o jsonpath='{.spec.containers[*].name}')"
        if [[ " $names " == *" ${self.triggers.gateway_container} "* ]]; then
          if [ -n "$gateway" ]; then
            echo "ERROR: more than one running agent pod has a ${self.triggers.gateway_container} container ($gateway, $running); the planted report stays in the broker store until one is left." >&2
            exit 1
          fi
          gateway="$running"
        fi
      done < <(kubectl get pods -n "${self.triggers.namespace}" -l "${self.triggers.pod_selector}" \
        --field-selector=status.phase=Running \
        -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.deletionTimestamp}{"\n"}{end}')
      if [ -z "$gateway" ]; then
        echo "ERROR: no running agent pod with a ${self.triggers.gateway_container} container; the planted report stays in the broker store." >&2
        exit 1
      fi

      printf '%s' '${self.triggers.script_b64}' | base64 -d | \
        kubectl exec -i -n "${self.triggers.namespace}" "$gateway" -c "${self.triggers.gateway_container}" \
          --pod-running-timeout=${self.triggers.pod_wait}s -- \
          ${self.triggers.python} - teardown "${self.triggers.audit_id}" "${self.triggers.mark}"
    EOT
  }
}

# Passed straight through: devops-bench reads these after apply and points the
# ambient kubeconfig at cluster_name.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
