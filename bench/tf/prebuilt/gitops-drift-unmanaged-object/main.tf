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

# The scenario driver for bench/tasks/gitops-drift-unmanaged-object-triage.
#
# A person loosens the Pod Security level of a namespace nobody ever put in
# git. This stack plants that -- a Namespace created by hand with `kubectl
# create`, enforcing `restricted`, then relabelled `privileged` with `kubectl
# label` -- and hands the Session KV daemon the audit record the relabel would
# have produced. The daemon posts the chat alert and starts the front-door turn
# that files the triage card; the task's prompt reads that card off the board.
#
# WHY THIS SCENARIO. The detector forwards every successful human write to
# declarative state and never consults git, so an object git does not declare
# reaches the same card as one it does. docs/designs/drift-detection.md says
# what is different about it ("Resources not in Git at all"): there is no
# desired state to revert to, so the triage never proposes one. The sibling stacks
# (../gitops-drift, ../gitops-drift-deleted-policy, ../gitops-drift-load-bearing)
# all apply their objects under a GitOps field manager, so none of them asks
# the agent to notice that git declares nothing.
#
# WHAT MARKS THE OBJECT AS NOT IN GIT, which is what the agent has to read off
# the cluster -- the Cluster Agent has no repository to check (see
# agents/cluster/skills/gke-load-bearing-change/SKILL.md). An object a GitOps
# tool syncs carries a GitOps manager in managedFields or a tool's tracking
# label or annotation, and the skill lists them. This plant carries none, and
# step 1 asserts that before the drift is made: the only managers are
# `kubectl-create` and, after step 2, `kubectl-label`. It also carries no
# `kubectl.kubernetes.io/last-applied-configuration`, which a client-side
# apply leaves -- a person's and Argo CD's client-side sync alike, which is why the
# skill reads a `kubectl-client-side-apply` manager as ambiguous rather than
# as git.
#
# The bench's own `managed-by` / `case` / `build-id` / `pull-number` labels are
# on the namespace because drift_clear_namespace requires them to delete a
# leftover. `managed-by: kube-agents-bench` is the closest thing to a
# management marker the object carries, and it names a test harness, not git;
# it is not Helm's `app.kubernetes.io/managed-by`, which the skill reads as
# ambiguous.
#
# WHY THE RECORD IS PLANTED RATHER THAN OBSERVED: ../gitops-drift's header
# carries the argument and it applies unchanged.
#
# Isolation class: namespace-scoped. Eligible tier: nightly.

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
  ns = var.drift_namespace

  # The label the scenario is about. Kept in one place because the plant, the
  # assertion, the payload and the task's safeguard all name it.
  enforce_label = "pod-security.kubernetes.io/enforce"

  # The field managers kubectl records for the two writes below. Not chosen by
  # this stack -- kubectl picks them per subcommand -- which is why step 1 and
  # step 2 assert them rather than trusting them.
  create_manager = "kubectl-create"
  label_manager  = "kubectl-label"

  # The object path the daemon renders for a cluster-scoped object
  # (_drift_resource_path: no namespace segment).
  resource_path = "namespaces/${var.drift_namespace}"

  # Seconds. Same judgements as ../gitops-drift-deleted-policy: an empty
  # namespace goes in seconds, and the card appears in seconds when the daemon
  # is working.
  leftover_delete_timeout = 180
  card_wait_timeout       = 300
  card_poll_interval      = 10

  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "case"        = "gitops-drift-unmanaged-object-triage"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
}

resource "null_resource" "unmanaged_object" {
  triggers = {
    # Destroy-time provisioners may read only `self`.
    namespace     = local.ns
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id

    # Re-plant when the scenario changes; inert in CI, where state starts fresh.
    scenario = sha256(join("|", [
      local.ns,
      var.declared_enforce_level,
      var.drifted_enforce_level,
      var.drift_principal,
      var.host_cluster_name,
    ]))
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      # ../../modules/drift-inject/drift_inject.sh documents every variable set here.
      drift_ns="${local.ns}"
      drift_cluster="${var.host_cluster_name}"
      drift_location="${var.host_cluster_location}"
      drift_project_id="${var.project_id}"
      drift_managed_by="${local.ci_labels["managed-by"]}"
      drift_leftover_timeout=${local.leftover_delete_timeout}
      drift_agent_namespace="${var.agent_namespace}"
      drift_agent_container="${var.agent_container}"
      drift_agent_deployment="${var.agent_deployment}"
      drift_daemon_url="${var.daemon_url}"
      drift_card_wait_timeout=${local.card_wait_timeout}
      drift_poll_interval=${local.card_poll_interval}

      source "${path.module}/../../modules/drift-inject/drift_inject.sh"

      drift_init
      drift_clear_namespace
      drift_require_daemon

      # ---- 1. Create the namespace by hand ----------------------------------
      # `kubectl create`, not `apply`: a client-side apply would write
      # last-applied-configuration, which is one of the markers this scenario
      # exists to leave out, and a server-side apply would record an Apply
      # operation that reads like a tool.
      drift_planted_ns=1
      kubectl create -f - <<'MANIFEST'
      apiVersion: v1
      kind: Namespace
      metadata:
        name: ${local.ns}
        labels:
          managed-by: ${local.ci_labels["managed-by"]}
          case: ${local.ci_labels["case"]}
          build-id: "${local.ci_labels["build-id"]}"
          pull-number: "${local.ci_labels["pull-number"]}"
          ${local.enforce_label}: ${var.declared_enforce_level}
      MANIFEST

      # The scenario is a lie if the object carries a GitOps marker, so check
      # before making the drift. Every manager is listed rather than reading
      # managedFields[0], which is not ordered by who matters.
      managers="$(kubectl get namespace "${local.ns}" \
        -o jsonpath='{range .metadata.managedFields[*]}{.manager}{"\n"}{end}' | sort -u)"
      if [ "$managers" != "${local.create_manager}" ]; then
        echo "ERROR: ${local.ns} was created by '${local.create_manager}' alone, but managedFields records:" >&2
        printf '%s\n' "$managers" >&2
        echo "Another manager on a hand-created namespace means something on this cluster adopts namespaces, and the scenario (an object no tool manages) would be false." >&2
        exit 1
      fi
      last_applied="$(kubectl get namespace "${local.ns}" \
        -o jsonpath='{.metadata.annotations.kubectl\.kubernetes\.io/last-applied-configuration}')"
      if [ -n "$last_applied" ]; then
        echo "ERROR: ${local.ns} carries last-applied-configuration, which marks a client-side apply; the scenario needs an object nothing applied." >&2
        exit 1
      fi

      # ---- 2. Make the drift real --------------------------------------------
      kubectl label namespace "${local.ns}" --overwrite \
        "${local.enforce_label}=${var.drifted_enforce_level}"

      live_level="$(kubectl get namespace "${local.ns}" \
        -o jsonpath='{.metadata.labels.pod-security\.kubernetes\.io/enforce}')"
      if [ "$live_level" != "${var.drifted_enforce_level}" ]; then
        echo "ERROR: ${local.ns} enforces '$live_level' after the relabel, not '${var.drifted_enforce_level}'." >&2
        exit 1
      fi
      managers="$(kubectl get namespace "${local.ns}" \
        -o jsonpath='{range .metadata.managedFields[*]}{.manager}{"\n"}{end}' | sort -u | tr '\n' ' ')"
      if [ "$managers" != "${local.create_manager} ${local.label_manager} " ]; then
        echo "ERROR: after the relabel ${local.ns}'s managers are '$managers', not '${local.create_manager} ${local.label_manager}', so the ownership the record below claims is not the cluster's." >&2
        exit 1
      fi
      echo "Planted ${local.resource_path}: created by hand enforcing ${var.declared_enforce_level}, relabelled ${var.drifted_enforce_level} out of band, no GitOps marker."

      # ---- 3. Hand the daemon the record ------------------------------------
      drift_mint_insert_id eval-drift-unmanaged

      # A patch to a cluster-scoped core object: empty group (the detector maps
      # Cloud Audit Logs' `core` to it, resourcename.go) and empty namespace
      # (the namespace object itself, not a scope). The owners are what step 2
      # asserted, with the paths the detector renders from FieldsV1
      # (ownership.go joinPath: no leading dot), checked against a live
      # namespace planted this way: the create manager owns the labels map
      # itself (the "." marker, which walkFields renders as `metadata.labels`)
      # and `kubernetes.io/metadata.name`, which the API server adds to every
      # namespace at create.
      drift_build_payload \
        "${var.drift_principal} patched ${local.resource_path} on cluster ${var.host_cluster_name}, fields owned by ${local.create_manager}, ${local.label_manager}" \
        "${var.host_cluster_name}" \
        "$drift_project" \
        "${var.host_cluster_location}" \
        "${var.drift_principal}" \
        "kubectl/v1.31.0 (linux/amd64)" \
        "patch" \
        "io.k8s.core.v1.namespaces.patch" \
        "$drift_changed_at" \
        "$drift_insert_id" \
        "" "v1" "" "namespaces" "${local.ns}" \
        "enriched" "false" \
        "${local.create_manager}|Update|metadata.labels,metadata.labels.build-id,metadata.labels.case,metadata.labels.kubernetes.io/metadata.name,metadata.labels.managed-by,metadata.labels.pull-number" \
        "${local.label_manager}|Update|metadata.labels.${local.enforce_label}"

      drift_inject_record

      # ---- 4. Wait for the card ---------------------------------------------
      drift_wait_for_card
    EOT
  }

  # The success-path teardown. The failure path is the plant's own EXIT trap
  # (drift_planted_ns=1), because Terraform skips destroy-time provisioners on
  # a resource whose create failed; ../gitops-drift-deleted-policy says more.
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

# Passed straight through: devops-bench calls get_cluster_info() after up() for
# any non-noop deployer, and it leaves the ambient kubeconfig on the host
# cluster, which is the cluster the task's safeguard reads.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
