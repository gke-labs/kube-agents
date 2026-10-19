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

# The scenario driver for bench/tasks/gitops-drift-out-of-band-triage.
#
# It plants a real out-of-band change on the host cluster and then hands the
# Session KV daemon the audit record that change would have produced, as a
# `gitops-drift` inject. The daemon claims its alert quota, writes its ledger
# row, posts the chat alert and starts the front-door turn that files the triage
# card; the task's prompt then reads that card off the board. What is under test
# is the daemon end of the chain and everything downstream of it.
#
# WHY THE RECORD IS PLANTED RATHER THAN OBSERVED. In production the record
# arrives by a longer route: the API server writes an audit entry, a logging
# sink exports it to Pub/Sub, and the drift-detector process pulls it, joins the
# live object's field ownership onto it and posts it here. This stack starts at
# the last step for two reasons, and each of them is a reason the longer route
# could not be driven from a bench fixture at all.
#
# The first is the detector's own classifier. It drops principals it judges to
# be automation before anything reaches the daemon, and every principal a bench
# fixture can authenticate as is a *.gserviceaccount.com one -- the runner's, or
# the node's. A change this stack made with kubectl would be classified as
# automation and correctly dropped, so the scenario would plant a defect the
# pipeline is designed never to report and then time out waiting for a card
# nobody filed. Posting the record directly is not a way around the classifier;
# it is the only way to hand the daemon the kind of record a person makes.
#
# The second is the sink. The audit route needs a logging sink, a Pub/Sub topic
# and a subscription on the leased project, none of which the eval install
# provisions, and an export lag measured in minutes on top of the run's budget.
#
# So the planted change and the planted record are two halves of one fixture and
# they have to agree: step 1 makes the change real -- the Deployment is applied
# by one field manager and its replica count taken by another, in managedFields,
# on the live object -- and step 2 sends the record that change would have
# produced. The card tells the agent to read the live object before concluding
# anything, and when it does it finds exactly what the record describes.
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
  # Kept in one place because the plant, the poll and the teardown all name
  # them, and a rename that reaches two of the three fails as a timeout rather
  # than as an error.
  ns       = var.drift_namespace
  workload = var.workload_name

  # The object path the daemon renders from the payload's `resource` block
  # (_drift_resource_path in agents/platform/scripts/session_kv_server.py:
  # namespace/resource/name). Composed here so the summary this stack writes
  # and the card the daemon renders name the object the same way.
  resource_path = "${var.drift_namespace}/deployments/${var.workload_name}"

  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }

  # Named here rather than written into the shared scaffolding, because each
  # drift stack waits for something different: this one plants an apply, a
  # patch and two reads, so it needs no observation window at all. The sibling
  # stacks set their own. They were literals inside the provisioner until the
  # scaffolding moved out, and the module takes them as values.
  leftover_delete_timeout = 180
  card_wait_timeout       = 300
  poll_interval           = 10

  # What the failure dump prints before tearing the namespace down. A replica
  # count is a Deployment fact, so the Deployment is the whole story here; the
  # deleted-policy stack asks for networkpolicies instead.
  failure_resources = "deployments"
}

resource "null_resource" "drift" {
  triggers = {
    # Destroy-time provisioners may read only `self`, so everything the
    # teardown needs is copied in here -- including the cluster coordinates,
    # because the destroy has to fetch its own credentials for the same reason
    # drift_init does and cannot reach `var`.
    namespace     = local.ns
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id

    # Re-plant when any of it changes. Without this the resource is inert after
    # the first apply, and a local run that edited the scenario would keep
    # scoring the old one. Inert in CI, where state starts fresh every run.
    scenario = sha256(join("|", [
      local.ns,
      local.workload,
      tostring(var.declared_replicas),
      tostring(var.live_replicas),
      var.gitops_field_manager,
      var.drift_field_manager,
      var.drift_principal,
      var.host_cluster_name,
    ]))
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      # The scaffolding every gitops-drift fixture shares -- the kubeconfig
      # fetch, the labelled-leftover guard, the healthz probe, the session and
      # inject exchange and the card poll -- is one file, and the three stacks
      # that used to carry their own copy now source it. What stays here is the
      # scenario: the plant below, its assertions, and the payload's values.
      # The module's header documents every variable set here.
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
      drift_poll_interval=${local.poll_interval}
      drift_failure_resources="${local.failure_resources}"

      source "${path.module}/../../modules/drift-inject/drift_inject.sh"

      # Fetches this stack's own kubeconfig, arms the failure-path cleanup and
      # the SIGTERM trap, and resolves the project. Never the ambient
      # current-context: devops-bench calls deployer.get_cluster_info() after
      # every up(), which re-points it at whichever cluster the previous
      # deployer: tofu task built.
      drift_init

      # Clears a namespace an earlier run left behind, and refuses one this
      # stack did not label.
      drift_clear_namespace

      # Before the plant, where it used to run after. The probe reads nothing
      # the plant produces, and a daemon that cannot take the record is worth
      # learning in ten seconds rather than after a namespace has been created
      # and has to be torn back down. The sibling stacks already probed here.
      drift_require_daemon

      # ---- 1. Make the change real ------------------------------------------
      # Two writes, deliberately by two field managers.
      #
      # The first is a server-side apply as ${var.gitops_field_manager}, which
      # is what a GitOps controller reconciling this manifest would look like in
      # managedFields: that manager owns the whole declared spec, replica count
      # included.
      #
      # The second takes `.spec.replicas` away from it under
      # ${var.drift_field_manager}. That transfer of ownership IS the drift --
      # it is the thing the detector's join reads off the live object and the
      # thing the report has to weigh -- and it is why the replica count is
      # changed with an explicit --field-manager rather than with `kubectl
      # scale`, whose manager name is a kubectl implementation detail this stack
      # would then be asserting on.
      #
      # Set before the create, not after: a create that half-succeeds, or a
      # label call that fails behind one that did not, still leaves a namespace
      # this run is responsible for removing.
      drift_planted_ns=1
      kubectl create namespace "${local.ns}" --dry-run=client -o yaml | kubectl apply -f -
      kubectl label namespace "${local.ns}" --overwrite \
        managed-by="${local.ci_labels["managed-by"]}" \
        build-id="${local.ci_labels["build-id"]}" \
        pull-number="${local.ci_labels["pull-number"]}"

      kubectl apply --server-side --field-manager="${var.gitops_field_manager}" -f - <<'MANIFEST'
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: ${local.workload}
        namespace: ${local.ns}
        labels:
          app: ${local.workload}
      spec:
        replicas: ${var.declared_replicas}
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
              - name: web
                image: busybox:1.36
                command: ["sh", "-c", "sleep 3600"]
                resources:
                  requests:
                    cpu: 10m
                    memory: 16Mi
                  limits:
                    memory: 32Mi
      MANIFEST

      kubectl patch deployment "${local.workload}" -n "${local.ns}" \
        --field-manager="${var.drift_field_manager}" --type=merge \
        -p '{"spec":{"replicas":${var.live_replicas}}}'

      # The record this stack is about to send claims both managers hold fields
      # on this object. Asserted rather than assumed, because a server-side
      # apply that silently fell back to a different manager, or a patch that
      # did not register one, would leave the card describing ownership the
      # agent cannot find when it reads the live object -- the one failure that
      # makes this case grade a report against a fiction, and the one that would
      # otherwise surface as a judge disagreeing with a correct answer.
      managers="$(kubectl get deployment "${local.workload}" -n "${local.ns}" \
        -o jsonpath='{range .metadata.managedFields[*]}{.manager}{"\n"}{end}')"
      for expected in "${var.gitops_field_manager}" "${var.drift_field_manager}"; do
        if ! printf '%s\n' "$managers" | grep -qxF "$expected"; then
          echo "ERROR: ${local.workload} does not record field manager '$expected' after the plant, so the ownership the drift record claims is not on the live object. Managers found:" >&2
          printf '%s\n' "$managers" >&2
          exit 1
        fi
      done

      observed_replicas="$(kubectl get deployment "${local.workload}" -n "${local.ns}" \
        -o jsonpath='{.spec.replicas}')"
      if [ "$observed_replicas" != "${var.live_replicas}" ]; then
        echo "ERROR: ${local.workload} is at $observed_replicas replicas, not the ${var.live_replicas} the out-of-band edit should have left. The task's safeguards assert ${var.live_replicas}, so this run would red on a change the agent never made." >&2
        exit 1
      fi
      echo "Planted ${local.resource_path}: declared ${var.declared_replicas} by ${var.gitops_field_manager}, live ${var.live_replicas} under ${var.drift_field_manager}."

      # ---- 2. Hand the daemon the record ------------------------------------
      # Minted before the payload so the id the record carries and the id the
      # poll matches are the same value by construction. A mismatch here could
      # only ever surface as a card-wait timeout.
      drift_mint_insert_id eval-drift

      # The record the detector would have built, including the one-line
      # `summary` it composes -- written out rather than left to the daemon's
      # fallback, because a real record always carries one and the fallback
      # renders a different sentence.
      #
      # `enriched` and the two owners are the claim step 1 asserted: one
      # manager holding the declared spec and one holding the replica count it
      # took. Every value crosses as an argument, so none of them is
      # interpolated into JSON text.
      drift_build_payload \
        "${var.drift_principal} patched ${local.resource_path} on cluster ${var.host_cluster_name}, fields owned by ${var.gitops_field_manager}, ${var.drift_field_manager}" \
        "${var.host_cluster_name}" \
        "$drift_project" \
        "${var.host_cluster_location}" \
        "${var.drift_principal}" \
        "kubectl/v1.31.0 (linux/amd64)" \
        "patch" \
        "io.k8s.apps.v1.deployments.patch" \
        "$drift_changed_at" \
        "$drift_insert_id" \
        "apps" "v1" "${local.ns}" "deployments" "${local.workload}" \
        "enriched" "false" \
        "${var.gitops_field_manager}|Apply|.spec.template.spec.containers,.spec.selector.matchLabels,.metadata.labels.app" \
        "${var.drift_field_manager}|Update|.spec.replicas"

      drift_inject_record

      # ---- 3. Wait for the card ---------------------------------------------
      # The inject returns as soon as the daemon has queued the front-door turn;
      # the card appears when that turn runs. Without this wait the agent turn
      # starts first, finds an empty board, and the case grades a correct agent
      # on a fixture that had not finished.
      drift_wait_for_card
    EOT
  }

  # Namespace-scoped by design, and this is the half that keeps it that way on
  # the success path. It does NOT cover the failure path, which is why the plant
  # above cleans up after itself: Terraform taints a resource whose create-time
  # provisioner failed and skips destroy-time provisioners on a tainted
  # resource, so `teardown: true` reaches a `tofu destroy` that reports
  # "1 destroyed" without running a line of this.
  #
  # It fetches its own credentials because get_cluster_info() runs after up()
  # and can itself fail; on that path the create-time provisioner succeeded, so
  # the resource is not tainted and this block does run, with whatever cluster
  # the previous task left selected. Deleting a namespace by name on the wrong
  # cluster is the kind of thing --ignore-not-found makes survivable rather than
  # safe.
  #
  # The drift record itself needs no teardown. It is a row in
  # `intercepted_events` and a card on the board, both of which belong to the
  # install rather than to the cluster, and both of which the next run's insert
  # id makes invisible to it.
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

# Passed straight through: the subject cluster already exists and this stack is
# not building one. devops-bench calls deployer.get_cluster_info()
# unconditionally after up() for any non-noop deployer, and TFDeployer reads
# these two outputs and hands them to GCPProvider.ensure_cluster_credentials --
# omitting them raises ConfigError. Here that call is wanted rather than merely
# tolerated: it leaves the ambient kubeconfig pointed at the host cluster, which
# is the cluster the task's safeguards read.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
