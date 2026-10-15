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

# The scenario driver for bench/tasks/fleet-audit-unchanged-finding-keeps-wording:
# plant a previous run of the fleet-consistency-drift stream in the report store
# of the install under test, and remove it again on destroy.
#
# plant.py runs in the container that runs `audit_report.py finish`, because the
# store is on that container's volume: the shell sandbox's `shell` container when
# the sandbox is on, else the gateway's `platform-agent` container. The same order
# as `make fleet-audit-view` (scripts/fleet_audit_status_view.py).
#
# The planted run has the evidence and severity that `finish` gives each drift
# candidate on the unchanged seeded fleet, and wording that starts with
# var.mark. The planted envelope names no issue, so it does not change the delta
# of the next run.

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
  # Every pod the operator runs for an agent carries this label.
  pod_selector = "app.kubernetes.io/name=platform-agent"
  # In this order: the sandbox first, then the gateway.
  store_containers = "shell platform-agent"
  scripts_dir      = "/opt/data/skills/fleet-audit/scripts"
  script_b64       = base64encode(file("${path.module}/plant.py"))
}

resource "null_resource" "previous_run" {
  triggers = {
    host_cluster     = var.host_cluster_name
    host_location    = var.host_cluster_location
    host_project     = var.project_id
    namespace        = var.agent_namespace
    mark             = var.mark
    pod_selector     = local.pod_selector
    store_containers = local.store_containers
    scripts_dir      = local.scripts_dir
    script_b64       = local.script_b64
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      kubeconfig_dir="$(mktemp -d)"
      planted=""
      pod=""
      container=""
      plant_store() {
        printf '%s' '${local.script_b64}' | base64 -d | \
          kubectl exec -i -n "${var.agent_namespace}" "$pod" -c "$container" -- \
            python3 - "$1" "${local.scripts_dir}" "${var.mark}"
      }
      on_exit() {
        status=$?
        trap '' TERM INT
        set +e
        if [ "$status" -ne 0 ] && [ -n "$planted" ]; then
          echo "Plant failed (exit $status); removing the envelopes it may have planted." >&2
          plant_store teardown >&2 || echo "Teardown failed too; the next apply's plant overwrites the mark and its destroy removes it." >&2
        fi
        rm -rf "$kubeconfig_dir"
      }
      trap on_exit EXIT
      trap 'exit 143' TERM INT
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

      target=""
      for candidate in ${local.store_containers}; do
        for running in $(kubectl get pods -n "${var.agent_namespace}" -l "${local.pod_selector}" \
            --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}'); do
          names="$(kubectl get pod -n "${var.agent_namespace}" "$running" -o jsonpath='{.spec.containers[*].name}')"
          if [[ " $names " == *" $candidate "* ]]; then
            target="$running $candidate"
            break 2
          fi
        done
      done
      if [ -z "$target" ]; then
        echo "ERROR: no running agent pod with a store container (${local.store_containers})." >&2
        exit 1
      fi
      read -r pod container <<<"$target"
      planted=1
      plant_store plant
    EOT
  }

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

      for container in ${self.triggers.store_containers}; do
        for pod in $(kubectl get pods -n "${self.triggers.namespace}" -l "${self.triggers.pod_selector}" \
            --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}'); do
          names="$(kubectl get pod -n "${self.triggers.namespace}" "$pod" -o jsonpath='{.spec.containers[*].name}')"
          if [[ " $names " == *" $container "* ]]; then
            printf '%s' '${self.triggers.script_b64}' | base64 -d | \
              kubectl exec -i -n "${self.triggers.namespace}" "$pod" -c "$container" -- \
                python3 - teardown "${self.triggers.scripts_dir}" "${self.triggers.mark}"
            exit 0
          fi
        done
      done
      echo "ERROR: no running agent pod with a store container; the planted wording may stay in the store." >&2
      exit 1
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
