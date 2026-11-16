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

# The scenario driver for bench/tasks/fleet-audit-unchanged-manifest-skips-synthesis:
# plant the previous scheduled run of the fleet-consistency-drift stream, then
# fire the stream's real scheduled run and wait for it to end, so the case can
# read what that run stored. Everything is undone on destroy.
#
# Three execs, in two containers:
#   - the agent container of the gateway (var.agent_container) holds the scope
#     snapshot the collectors need as arguments on an install that declares a
#     scope (the platform_control fleet_scope tool's collector_args), and the
#     cron roster arm.py fires the job from;
#   - the container that runs `audit_report.py finish` holds the report store
#     and the scratch directory: the shell sandbox's `shell` container when the
#     sandbox is on, else the gateway's `platform-agent` container, in the same
#     order as `make fleet-audit-view`. plant.py runs there.

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
  home             = "/opt/data"
  python           = "/opt/hermes/.venv/bin/python3"
  audit_job        = "fleet-consistency-drift"
  pod_wait         = 5
  plant_b64        = base64encode(file("${path.module}/plant.py"))
  arm_b64          = base64encode(file("${path.module}/arm.py"))
  # The one line the fleet_scope tool hands the agent, read from the same
  # snapshot in the agent container; empty on an install with no declared scope.
  scope_script = <<-PY
    import sys
    sys.path.insert(0, "/opt/defaults/scripts")
    try:
        import fleet_scope_targets
        targets = fleet_scope_targets.declared_scope_targets()
    except Exception as exc:  # noqa: BLE001 - an unreadable scope is reported, and the collector refuses on its own
        print(f"scope: {exc}", file=sys.stderr)
        targets = None
    print(targets.collector_args() if targets else "")
  PY
  scope_b64    = base64encode(local.scope_script)
}

resource "null_resource" "previous_scheduled_run" {
  triggers = {
    host_cluster     = var.host_cluster_name
    host_location    = var.host_cluster_location
    host_project     = var.project_id
    namespace        = var.agent_namespace
    deployment       = var.agent_deployment
    container        = var.agent_container
    mark             = var.mark
    pod_selector     = local.pod_selector
    store_containers = local.store_containers
    scripts_dir      = local.scripts_dir
    home             = local.home
    python           = local.python
    audit_job        = local.audit_job
    pod_wait         = local.pod_wait
    plant_b64        = local.plant_b64
    arm_b64          = local.arm_b64
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      kubeconfig_dir="$(mktemp -d)"
      planted=""
      pod=""
      container=""
      arm() {
        printf '%s' '${local.arm_b64}' | base64 -d | \
          kubectl exec -i -n "${var.agent_namespace}" "deployment/${var.agent_deployment}" \
            -c "${var.agent_container}" --pod-running-timeout=${local.pod_wait}s -- \
            ${local.python} - "$@"
      }
      plant_store() {
        printf '%s' '${local.plant_b64}' | base64 -d | \
          kubectl exec -i -n "${var.agent_namespace}" "$pod" -c "$container" -- \
            env PYTHONDONTWRITEBYTECODE=1 python3 - "$@"
      }
      on_exit() {
        status=$?
        trap '' TERM INT
        set +e
        if [ "$status" -ne 0 ]; then
          echo "Apply failed (exit $status); undoing what it did." >&2
          arm clean "${local.home}" >&2 || echo "Could not clean the arm state; the next apply removes it first." >&2
          if [ -n "$planted" ]; then
            plant_store teardown "${local.scripts_dir}" "${var.mark}" >&2 \
              || echo "Teardown failed too; the next apply's plant overwrites the mark and its destroy removes it." >&2
          fi
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

      # The collector's scope arguments, as the agent would paste them.
      collector_args="$(printf '%s' '${local.scope_b64}' | base64 -d | \
        kubectl exec -i -n "${var.agent_namespace}" "deployment/${var.agent_deployment}" \
          -c "${var.agent_container}" --pod-running-timeout=${local.pod_wait}s -- \
          ${local.python} - | tail -n 1)"
      echo "collector arguments: '$collector_args'"

      arm clean "${local.home}"
      arm idle "${local.home}" "${local.audit_job}" "${var.own_wait}"
      planted=1
      # Unquoted on purpose: the arguments are a space-separated flag list.
      # shellcheck disable=SC2086
      plant_store plant "${local.scripts_dir}" "${var.mark}" $collector_args
      arm fire "${local.home}" "${local.python}" "${local.audit_job}"
      arm wait "${local.home}" "${local.audit_job}" "${var.run_wait}"
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    on_failure  = continue
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -uo pipefail
      kubeconfig_dir="$(mktemp -d)"
      trap 'rm -rf "$kubeconfig_dir"' EXIT
      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG

      project="${self.triggers.host_project}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      gcloud container clusters get-credentials "${self.triggers.host_cluster}" \
        --location "${self.triggers.host_location}" --project "$project" --quiet \
        || echo "WARNING: could not fetch credentials for ${self.triggers.host_cluster}." >&2

      printf '%s' '${self.triggers.arm_b64}' | base64 -d \
        | kubectl exec -i -n "${self.triggers.namespace}" "deployment/${self.triggers.deployment}" \
          -c "${self.triggers.container}" --pod-running-timeout=${self.triggers.pod_wait}s -- \
          ${self.triggers.python} - clean "${self.triggers.home}" \
        || echo "WARNING: could not clean the arm state; the next apply removes it first." >&2

      for container in ${self.triggers.store_containers}; do
        for pod in $(kubectl get pods -n "${self.triggers.namespace}" -l "${self.triggers.pod_selector}" \
            --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}'); do
          names="$(kubectl get pod -n "${self.triggers.namespace}" "$pod" -o jsonpath='{.spec.containers[*].name}')"
          if [[ " $names " == *" $container "* ]]; then
            printf '%s' '${self.triggers.plant_b64}' | base64 -d | \
              kubectl exec -i -n "${self.triggers.namespace}" "$pod" -c "$container" -- \
                env PYTHONDONTWRITEBYTECODE=1 python3 - teardown "${self.triggers.scripts_dir}" "${self.triggers.mark}"
            exit 0
          fi
        done
      done
      echo "ERROR: no running agent pod with a store container; the planted run may stay in the store." >&2
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
