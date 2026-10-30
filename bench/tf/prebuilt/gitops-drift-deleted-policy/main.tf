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

# The scenario driver for bench/tasks/gitops-drift-deleted-policy-triage.
#
# A person deletes the NetworkPolicy protecting a namespace and never puts it
# back. This stack plants that -- a workload, the policy that guarded it,
# applied by a GitOps field manager, then the policy deleted -- and hands the
# Session KV daemon the audit record the deletion would have produced. The
# daemon claims its alert quota, posts the chat alert and starts the front-door
# turn that files the triage card; the task's prompt reads that card off the
# board.
#
# WHY THIS SCENARIO. It is the shape CUJ 1 of docs/designs/drift-detection.md
# names first ("a NetworkPolicy is deleted"), and the one an operator meets
# most: the policy is deleted to find out whether it is what is blocking a
# connection, the answer is no, and nothing puts it back. The sibling stack
# ../gitops-drift carries a replica count, benign by construction; this one
# carries a change with a security consequence, which is what CUJ 1 asks the
# report to weigh.
#
# WHY THE RECORD IS PLANTED RATHER THAN OBSERVED. The sibling stack's header
# carries the full argument and it applies here unchanged: the detector's
# classifier drops automation principals before anything reaches the daemon and
# every principal a bench fixture can authenticate as is one, and the audit
# route's sink costs minutes of export lag on top of the run's budget. Posting
# the record directly is not a way around the classifier; it is the only way to
# hand the daemon the kind of record a person makes.
#
# WHAT THE AGENT CAN AND CANNOT LEARN, WHICH IS THE POINT OF THIS CASE.
#
# `join: no_object` below is not a defect being papered over. A delete leaves
# nothing to read managedFields from, and the detector says so itself
# (k8s-operator/cmd/drift-detector/join.go: "joinNoObject is a record there is
# no object to fetch for -- a delete [...] Neither is a failure: the audited
# change still stands on its own"). So the card renders "Field ownership: not
# read" and instructs the agent to say so rather than treat the absence as
# evidence that nothing manages the namespace.
#
# What is left is enough, and it is a harder read than the sibling's. The
# record names who, what, which object and when; the cluster says the policy is
# still gone. The impact the report has to weigh cannot be lifted off a changed
# field, because the changed thing no longer exists -- it has to come from what
# the namespace looks like NOW: ${var.workload_replicas} pods and no
# NetworkPolicy at all, so every one of them accepts traffic from anywhere in
# the cluster. That is CUJ 1's "weighs the security impact" clause with nothing
# to transcribe.
#
# The MOTIVE is not recoverable and the case does not ask for it. A
# NetworkPolicy refusing a connection produces no Kubernetes event -- the
# caller just times out -- so the debugging session that plausibly prompted
# this leaves no trace anywhere the agent can reach. The task's expected_output
# therefore requires no reason, and a report that supplies none is correct.
#
# Enforcement is real on this cluster, so the deletion is not a fiction:
# terraform/modules/gke-cluster sets datapath_provider = "ADVANCED_DATAPATH"
# (main.tf), and Dataplane V2 enforces NetworkPolicy without the legacy addon.
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
  policy   = var.policy_name
  workload = var.workload_name

  # The object path the daemon renders from the payload's `resource` block
  # (_drift_resource_path in agents/platform/scripts/session_kv_server.py:
  # namespace/resource/name). Composed here so the summary this stack writes
  # and the card the daemon renders name the object the same way.
  resource_path = "${var.drift_namespace}/networkpolicies/${var.policy_name}"

  # Seconds. Hoisted out of the script because each is a judgement about a
  # different failure, and inline they read as one arbitrary number repeated.
  #
  # leftover_delete_timeout: clear of the ~35s a namespace holding one busybox
  # Deployment takes to go, and short enough that a namespace wedged on a
  # finalizer fails here naming the real problem rather than later as a
  # mystery.
  #
  # workload_ready_timeout: the workload only has to be Running for the
  # impact sentence to be true, and a busybox pod on a warm node is Ready in
  # well under a minute. A pool with no room is the failure this bounds.
  #
  # card_wait_timeout: the front-door turn is one kanban_create, so the card
  # appears in seconds when the daemon is working and never when it is not.
  # Anything past a minute is already the failure; the ceiling only decides
  # how long the run pays before saying so.
  leftover_delete_timeout = 180
  workload_ready_timeout  = 180
  card_wait_timeout       = 300
  card_poll_interval      = 10

  # What the failure dump prints before tearing the namespace down. The
  # NetworkPolicy is the object this scenario is about, so omitting it would
  # leave the dump silent on the only thing worth seeing.
  failure_resources = "deployments,networkpolicies,pods"

  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "case"        = "gitops-drift-deleted-policy-triage"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
}

resource "null_resource" "deleted_policy" {
  triggers = {
    # Destroy-time provisioners may read only `self`, so everything the
    # teardown names is copied in here -- including the cluster coordinates,
    # because the destroy fetches its own credentials for the same reason
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
      local.policy,
      local.workload,
      tostring(var.workload_replicas),
      var.gitops_field_manager,
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
      # scenario: the plant below, the deletion, and the payload's values.
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
      drift_poll_interval=${local.card_poll_interval}

      # networkpolicies, not the default: the object whose absence IS this
      # scenario would otherwise be the one thing the failure dump omits.
      drift_failure_resources="${local.failure_resources}"

      source "${path.module}/../../modules/drift-inject/drift_inject.sh"

      drift_init
      drift_clear_namespace
      drift_require_daemon

      # ---- 1. Plant the protected namespace ---------------------------------
      # Both objects are applied server-side under ${var.gitops_field_manager},
      # which is what a GitOps controller reconciling this directory would look
      # like in managedFields. The policy's ownership is not observable once
      # step 2 removes it -- see this file's header -- but applying it under
      # that manager is what makes the scenario coherent rather than merely
      # asserted by the record.
      #
      # Set before the create, not after: a create that half-succeeds, or a
      # label call that fails behind one that did not, still leaves a namespace
      # this run is responsible for removing.
      drift_planted_ns=1
      kubectl create namespace "${local.ns}" --dry-run=client -o yaml | kubectl apply -f -
      kubectl label namespace "${local.ns}" --overwrite \
        managed-by="${local.ci_labels["managed-by"]}" \
        case="${local.ci_labels["case"]}" \
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
        replicas: ${var.workload_replicas}
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

      # A default-deny on ingress: an empty podSelector selects every pod in the
      # namespace, and naming Ingress in policyTypes with no ingress rules
      # denies all of it. This is the shape whose removal has a consequence
      # worth reporting -- a policy with allow rules would leave the question
      # "allowing what?" unanswerable once it is deleted, and the impact
      # sentence vague.
      kubectl apply --server-side --field-manager="${var.gitops_field_manager}" -f - <<'POLICY'
      apiVersion: networking.k8s.io/v1
      kind: NetworkPolicy
      metadata:
        name: ${local.policy}
        namespace: ${local.ns}
      spec:
        podSelector: {}
        policyTypes:
          - Ingress
      POLICY

      # Asserted before the deletion, because afterwards nothing can check it.
      # A server-side apply that silently fell back to a different manager
      # would leave the scenario claiming a GitOps-owned policy was removed when
      # no GitOps manager ever owned one.
      #
      # Every manager is listed and matched rather than reading
      # managedFields[0]: the array is not ordered by who matters, so any
      # second entry the API server sorts ahead of the GitOps one -- a
      # controller touching annotations, say -- would make index 0 the wrong
      # manager and hard-exit this plant against a scenario that is in fact
      # correct. The sibling stacks use the same form.
      # Not swallowed with `2>/dev/null || true`: a failing read would then
      # produce an empty list and send the script into the branch below, which
      # blames the server-side apply for a transport error that has nothing to
      # do with it. Under errexit a failing kubectl aborts here with its own
      # stderr, which is the honest failure.
      policy_managers="$(kubectl get networkpolicy "${local.policy}" -n "${local.ns}" \
        -o jsonpath='{range .metadata.managedFields[*]}{.manager}{"\n"}{end}')"
      if ! printf '%s\n' "$policy_managers" | grep -qxF "${var.gitops_field_manager}"; then
        echo "ERROR: ${local.policy} does not record field manager '${var.gitops_field_manager}', so the policy this run is about to delete was never owned by the GitOps manager and the scenario would be a fiction. Managers found:" >&2
        printf '%s\n' "$policy_managers" >&2
        exit 1
      fi

      # The pods have to be up before the policy goes, because the impact the
      # report weighs is that THESE pods are now reachable. A namespace whose
      # workload never scheduled would make that sentence true of nothing.
      if ! kubectl rollout status deployment/"${local.workload}" -n "${local.ns}" \
        --timeout=${local.workload_ready_timeout}s; then
        echo "ERROR: ${local.workload} did not become available within ${local.workload_ready_timeout}s, so the namespace holds no running pods for the deleted policy to have been protecting. This is the cluster, not the agent." >&2
        kubectl get pods -n "${local.ns}" -o wide >&2 || true
        exit 1
      fi

      # ---- 2. Make the deletion real ----------------------------------------
      # A plain delete, with no --field-manager, because a deletion claims no
      # field: the verb is the whole change. This is also why the record in
      # step 3 carries `join: no_object` rather than an ownership block.
      kubectl delete networkpolicy "${local.policy}" -n "${local.ns}" --wait=true

      # The namespace must hold NO policy, not merely not this one. The report's
      # impact sentence -- every pod here now accepts traffic from anywhere in
      # the cluster -- is only true if nothing else is still selecting them, and
      # the task's safeguard asserts the same emptiness at the end of the run.
      # stderr is kept for the same reason the read above keeps it: kubectl
      # writes "No resources found" there and exits zero, which is the
      # expected case, while a transport failure exits non-zero and aborts the
      # pipeline with a line saying what broke rather than with a bare trap.
      remaining="$(kubectl get networkpolicies -n "${local.ns}" \
        --no-headers | wc -l | tr -d ' ')"
      if [ "$remaining" != "0" ]; then
        echo "ERROR: ${local.ns} still holds $remaining NetworkPolicy object(s) after the deletion, so the namespace is not open and the impact the card asks the agent to weigh is not the one this stack planted." >&2
        kubectl get networkpolicies -n "${local.ns}" >&2 || true
        exit 1
      fi
      echo "Planted ${local.resource_path}: applied by ${var.gitops_field_manager}, deleted out of band, ${var.workload_replicas} pods now unprotected."

      # ---- 3. Hand the daemon the record ------------------------------------
      # Minted before the payload so the id the record carries and the id the
      # poll matches are the same value by construction.
      drift_mint_insert_id eval-drift-policy

      # The record the detector would have built for a DELETE, which differs
      # from the sibling stacks' in four places and each is forced:
      #
      #   verb/method_name name a deletion, which is what the audit entry a
      #   `delete` call produces carries;
      #
      #   `join: no_object` is what the detector's joiner returns for one --
      #   `if record.Verb == deleteVerb || record.Resource.Name == ""`
      #   (join.go) -- because there is no object left to read managedFields
      #   from. The daemon renders that as "Field ownership: not read" and
      #   tells the agent to say so;
      #
      #   `owners` is ABSENT, not empty. payloadForEvent builds it by appending
      #   over event.Owners, which a delete leaves nil, and the field is
      #   `json:"owners,omitempty"`, so the detector emits no key at all.
      #   Sending `[]` would be the shape that does not occur. Passing no owner
      #   argument below is what produces that, and
      #   bench/tests/test_drift_inject_payload.py pins it -- the daemon only
      #   declines to read the key because _drift_ownership_block returns
      #   early on a non-enriched join, so an empty list here would be one
      #   reordering away from a card claiming the live object records no
      #   managedFields at all;
      #
      #   the summary carries driftSummary's suffix for a non-enriched
      #   outcome, "(field ownership not read: <outcome>)". Written out rather
      #   than left to the daemon's fallback, because a real record always
      #   carries one and the fallback renders a different sentence.
      drift_build_payload \
        "${var.drift_principal} deleted ${local.resource_path} on cluster ${var.host_cluster_name} (field ownership not read: no_object)" \
        "${var.host_cluster_name}" \
        "$drift_project" \
        "${var.host_cluster_location}" \
        "${var.drift_principal}" \
        "kubectl/v1.31.0 (linux/amd64)" \
        "delete" \
        "io.k8s.networking.v1.networkpolicies.delete" \
        "$drift_changed_at" \
        "$drift_insert_id" \
        "networking.k8s.io" "v1" "${local.ns}" "networkpolicies" "${local.policy}" \
        "no_object" "false"

      drift_inject_record

      # ---- 4. Wait for the card ---------------------------------------------
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
  # the previous task left selected.
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
