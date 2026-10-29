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

# The scenario driver for bench/tasks/fleet-audit-unread-target-keeps-finding.
#
# It opens ONE fleet-consistency-drift ledger with one finding on the install's
# host cluster, under a drift facet (`label-keys`), and writes the stored record
# of that ledger. The host cluster has no environment label, so the drift
# collector compares it on no facet: the run that follows has a coverage gap on
# the host and does not check the facet there. The case grades whether the
# finding stays on the ledger that the run writes.
#
# ledger.py does the work in the shell sandbox, where the audit worker runs
# audit_report.py: it imports audit_report and opens the issue and writes the
# record with the same functions `finish` uses, so the ledger is one `finish`
# wrote and the record is one `finish` trusts. The harness closes the stream's
# open ledger before each repetition (hack/ci_reset_audit_ledgers.py), before
# devops-bench applies this stack, so each repetition starts from the ledger
# this stack opens.
#
# The finding's object is an invented name that only this case uses. The
# seed writes the issue number in the stream's report store directory in the
# sandbox, and the destroy closes that issue first: the label-filtered issue
# list can omit it for a short time. Then the destroy closes each other open
# drift ledger: the seed refuses when one is open, so an open one at destroy
# is the one the seed opened, whatever the run wrote in its body. A seed that fails after it opens the issue closes the issue
# itself, because a failed apply runs no destroy.

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
  python     = "python3"
  script_b64 = base64encode(file("${path.module}/ledger.py"))
  # How long an exec into the sandbox waits for a running pod; see
  # bench/tf/prebuilt/bootstrap-ranking/main.tf.
  pod_wait = 5
}

resource "null_resource" "unread_target_ledger" {
  triggers = {
    host_cluster      = var.host_cluster_name
    host_location     = var.host_cluster_location
    host_project      = var.project_id
    namespace         = var.agent_namespace
    sandbox_selector  = var.sandbox_selector
    sandbox_container = var.sandbox_container
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

      sandbox_pods="$(kubectl get pods -n "${var.agent_namespace}" -l "${var.sandbox_selector}" -o name)"
      set -- $sandbox_pods
      if [ "$#" -ne 1 ]; then
        echo "ERROR: expected one shell sandbox pod matching ${var.sandbox_selector} in ${var.agent_namespace}, found $#. The audit worker writes the ledger from the sandbox, so the seeded ledger is opened there." >&2
        exit 1
      fi

      printf '%s' '${local.script_b64}' | base64 -d | \
        kubectl exec -i -n "${var.agent_namespace}" "$1" -c "${var.sandbox_container}" \
          --pod-running-timeout=${local.pod_wait}s -- \
          ${local.python} - seed "$project" "${var.host_cluster_location}" "${var.host_cluster_name}" ${var.gitops_repo}
    EOT
  }

  # No on_failure = continue: a destroy that cannot close the ledger fails the
  # run, rather than leaving an open ledger that Terraform records as removed.
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

      sandbox_pods="$(kubectl get pods -n "${self.triggers.namespace}" -l "${self.triggers.sandbox_selector}" -o name)"
      set -- $sandbox_pods
      if [ "$#" -ne 1 ]; then
        echo "ERROR: expected one shell sandbox pod matching ${self.triggers.sandbox_selector}, found $#; the seeded ledger stays open until the next harness reset closes it." >&2
        exit 1
      fi

      printf '%s' '${self.triggers.script_b64}' | base64 -d | \
        kubectl exec -i -n "${self.triggers.namespace}" "$1" -c "${self.triggers.sandbox_container}" \
          --pod-running-timeout=${self.triggers.pod_wait}s -- \
          ${self.triggers.python} - teardown ${self.triggers.gitops_repo}
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
