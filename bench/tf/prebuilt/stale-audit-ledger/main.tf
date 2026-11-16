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

# The scenario driver for bench/tasks/fleet-audit-clean-close-reads-clean
# (ledger_kind "finding") and bench/tasks/fleet-audit-on-demand-run-reports
# (ledger_kind "gap"). The text below is about the first case. With "gap", the
# ledger has no finding and one coverage gap on a second invented cluster, so
# the clean run closes it with nothing new and nothing resolved (ledger.py says
# more).
#
# It opens ONE ledger issue for one audit stream, with one finding on a cluster
# that does not exist, and writes the stored record of that ledger. The run
# that follows audits a fleet where the stream finds nothing, so it closes the
# ledger as clean. The case grades what the closed ledger says.
#
# ledger.py does the work in the shell sandbox, where the audit worker runs
# audit_report.py: it imports audit_report and opens the issue and writes the
# record with the same functions `finish` uses, so the ledger is one `finish`
# wrote and the record is one `finish` trusts. The harness closes the stream's
# open ledger before each repetition (hack/ci_reset_audit_ledgers.py), before
# devops-bench applies this stack, so each repetition starts from the ledger
# this stack opens.
#
# The cluster, project and object names are invented names that only the two
# cases of this stack use. The destroy closes each open ledger of the stream whose number the
# seed wrote to the report store, or whose body names the invented cluster. A ledger that the
# run closed is already closed.

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
  # The ledger.py mode for each ledger_kind.
  seed_mode = var.ledger_kind == "gap" ? "seed-gap" : "seed"
}

resource "null_resource" "stale_ledger" {
  triggers = {
    host_cluster      = var.host_cluster_name
    host_location     = var.host_cluster_location
    host_project      = var.project_id
    namespace         = var.agent_namespace
    sandbox_selector  = var.sandbox_selector
    sandbox_container = var.sandbox_container
    audit_id          = var.audit_id
    gitops_repo       = var.gitops_repo
    seed_mode         = local.seed_mode
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
        echo "ERROR: expected one shell sandbox pod matching ${var.sandbox_selector} in ${var.agent_namespace}, found $#. The audit worker writes the ledger from the sandbox, so the stale ledger is opened there." >&2
        exit 1
      fi

      printf '%s' '${local.script_b64}' | base64 -d | \
        kubectl exec -i -n "${var.agent_namespace}" "$1" -c "${var.sandbox_container}" \
          --pod-running-timeout=${local.pod_wait}s -- \
          ${local.python} - ${local.seed_mode} "${var.audit_id}" ${var.gitops_repo}
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
        echo "ERROR: expected one shell sandbox pod matching ${self.triggers.sandbox_selector}, found $#; the stale ledger stays open until the next harness reset closes it." >&2
        exit 1
      fi

      printf '%s' '${self.triggers.script_b64}' | base64 -d | \
        kubectl exec -i -n "${self.triggers.namespace}" "$1" -c "${self.triggers.sandbox_container}" \
          --pod-running-timeout=${self.triggers.pod_wait}s -- \
          ${self.triggers.python} - teardown "${self.triggers.audit_id}" ${self.triggers.gitops_repo}
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
