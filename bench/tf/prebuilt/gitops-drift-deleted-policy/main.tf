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
  # Every kubectl below runs against a kubeconfig this stack fetches for itself
  # in step 0, never the ambient one -- see that step for why the ambient
  # current-context is the wrong cluster by the time this apply runs.
  kubectl = "kubectl --namespace=${var.drift_namespace}"

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
    # step 0 does and cannot reach `var`.
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

      # ---- 0. Point kubectl at the cluster the agent is on ------------------
      # Fetched here rather than inherited, because the ambient current-context
      # is NOT the host cluster by the time this apply runs: devops-bench calls
      # deployer.get_cluster_info() unconditionally after up(), which shells
      # `gcloud container clusters get-credentials` with no --kubeconfig, so any
      # deployer: tofu task earlier in the matrix leaves its own per-run cluster
      # selected. ../gitops-drift step 0 carries the argument in full.
      #
      # A directory, not `mktemp` on its own: get-credentials refuses to load an
      # existing empty file, warns, and writes a stray dated .backup beside it.
      kubeconfig_dir="$(mktemp -d)"

      # A plant that fails must not leave ${local.ns} behind, because the
      # leftover is what breaks the NEXT run -- see step 0b. The teardown at the
      # bottom of this file cannot be what prevents it: Terraform taints a
      # resource whose create-time provisioner failed and skips destroy-time
      # provisioners on a tainted resource, so the destroy reports "1 destroyed"
      # without running a line of it.
      #
      # `set +e` is load-bearing: errexit stays in force inside a trap, so the
      # first command that failed would abort it and skip the delete. Capturing
      # `status` first keeps the script's own exit code intact.
      planted_ns=""
      on_exit() {
        status=$?
        set +e
        if [ "$status" -ne 0 ] && [ -n "$planted_ns" ]; then
          echo "Plant failed (exit $status). State of ${local.ns} before cleanup:" >&2
          ${local.kubectl} get deployments,networkpolicies,pods -o wide >&2
          echo "Deleting ${local.ns} so the next run starts from a clean namespace." >&2
          kubectl delete namespace "${local.ns}" --ignore-not-found --wait=false >&2
        fi
        rm -rf "$kubeconfig_dir"
      }
      trap on_exit EXIT

      # A Prow deadline delivers SIGTERM, and bash does not run an EXIT trap
      # when the shell dies from an untrapped signal -- so without this the
      # deadline kill leaks the namespace exactly as a failed plant would.
      trap 'exit 143' TERM INT

      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG

      project="${var.project_id}"
      if [ -z "$project" ]; then
        # GCPProvider.resolve_variables injects project_id for every task, so
        # this is the local-run path rather than the CI one.
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      if [ -z "$project" ]; then
        echo "ERROR: no project id. Pass -var project_id=... or set a gcloud default project; this stack needs one to fetch credentials for ${var.host_cluster_name}." >&2
        exit 1
      fi

      gcloud container clusters get-credentials "${var.host_cluster_name}" \
        --location "${var.host_cluster_location}" --project "$project" --quiet

      # ---- 0b. Clear what an earlier run left behind ------------------------
      # Step 1 is idempotent, so against a namespace that already holds this
      # scenario the applies report no change and the assertions below pass on
      # the previous run's objects rather than on anything this one planted.
      #
      # The policy itself is not the exposure -- step 1 re-creates it before
      # step 2 deletes it, so a leftover namespace whose policy is already
      # gone still gets a fresh one to delete. What survives is everything
      # step 1 does NOT rewrite. Its label call is `--overwrite`, so the
      # labels are not it; what survives is the workload's pods, which keep
      # the ages and restart history of whichever run created them, and the
      # managedFields the ownership assertion reads, written by a
      # server-side apply that may have happened hours ago. Deleting first
      # is what makes the case recover on its own, and it is also the only
      # thing that can: the run that leaked the namespace is over, and nothing
      # else visits this cluster between runs.
      #
      # Gated on the label step 1 writes, not on the name alone. This is the
      # only unconditional namespace delete in the stack and it runs against the
      # shared cluster the Platform Agent install lives on, so it has to be able
      # to say the namespace is ours. A namespace of this name that we did not
      # plant is a stop, not a target.
      if kubectl get namespace "${local.ns}" >/dev/null 2>&1; then
        # Not swallowed: the enclosing `if` has already proved the namespace
        # exists, so a failing read here is a transport or RBAC failure and
        # not an absent label. Swallowed, it becomes an empty value that the
        # branch below reports as "this stack did not create it" -- a refusal
        # with the wrong reason, and indistinguishable from a namespace that
        # genuinely carries no label.
        leftover_owner="$(kubectl get namespace "${local.ns}" \
          -o jsonpath='{.metadata.labels.managed-by}')"
        if [ "$leftover_owner" != "${local.ci_labels["managed-by"]}" ]; then
          echo "ERROR: ${local.ns} already exists on ${var.host_cluster_name} but is not labelled managed-by=${local.ci_labels["managed-by"]} (found '$leftover_owner'). This stack did not create it, so it will not delete it. Remove it by hand if it is stale." >&2
          exit 1
        fi
        echo "Found a leftover ${local.ns} from an earlier run; deleting it before planting."
        # --ignore-not-found because the destroy provisioner deletes with
        # --wait=false, so a namespace can be mid-deletion when the get above
        # sees it and gone by the time this runs.
        if ! kubectl delete namespace "${local.ns}" --ignore-not-found --wait=true --timeout=${local.leftover_delete_timeout}s; then
          echo "ERROR: could not clear the leftover ${local.ns} within ${local.leftover_delete_timeout}s, so this run cannot plant a fresh deletion. A namespace wedged on a finalizer is the usual cause; an RBAC or API failure lands here too. Its current state follows." >&2
          kubectl get namespace "${local.ns}" -o yaml >&2 || true
          exit 1
        fi
      fi

      # ---- 0c. Refuse a daemon that cannot take the record ------------------
      # A daemon predating the `gitops-drift` dispatch takes the payload down
      # its event path instead, grades it as a Warning Pod alert for `default/`,
      # answers 200, and files a card that has nothing to do with drift. Every
      # objective would then fail against a card that looks plausible in the
      # transcript, which is the worst way for a fixture to be wrong.
      #
      # /healthz is unauthenticated, so this runs before the key is needed and
      # separates "the install is too old" from "the key is wrong".
      #
      # BEFORE THE PLANT, like the load-bearing sibling. It reads only the
      # kubeconfig from step 0 and four plain variables, and this stack's
      # plant is a leftover delete, a namespace create, two server-side
      # applies and a rollout wait. Probing afterwards would spend all of
      # that, then let the exit trap delete the namespace it just built,
      # before reporting a condition knowable in ten seconds.
      #
      # The exec's status and curl's are both tested below, separately from
      # the advertised list: a call that failed is a different diagnosis from
      # a daemon that answered and advertised the wrong kinds.
      pod="deployment/${var.agent_deployment}"
      exec_in_pod=(kubectl exec "$pod" -n "${var.agent_namespace}" -c "${var.agent_container}" --)
      exec_in_pod_stdin=(kubectl exec -i "$pod" -n "${var.agent_namespace}" -c "${var.agent_container}" --)

      # jq runs inside the pod rather than on the runner, here and in the calls
      # below. The agent image installs it; the Prow runner has no jq in any of
      # hack/'s scripts, so assuming one would be a dependency nothing else in
      # bench/tf takes.
      #
      # `curl -fsS`, and the body captured before jq sees it. Without -f, curl
      # prints an error body and exits 0 on a 404; and inside `sh -c 'curl |
      # jq'` the pipeline's status is jq's, which is 0 on empty input -- so a
      # connection refused, a wrong daemon_url or a timeout produced an empty
      # `kinds` with a zero status, and the branch below blamed the install's
      # age for a daemon that was simply down. Measured: that pipeline exits 0
      # with curl failing.
      kinds_rc=0
      kinds="$("$${exec_in_pod[@]}" sh -c 'body="$(curl -fsS --max-time 10 "$1/healthz")" || exit $?; printf "%s" "$body" | jq -r ".inject_kinds[]?"' sh "${var.daemon_url}")" || kinds_rc=$?
      if [ "$kinds_rc" -ne 0 ]; then
        echo "ERROR: could not read ${var.daemon_url}/healthz in ${var.agent_container} (exit $kinds_rc); kubectl's or curl's own error is above. This says nothing about which inject kinds the daemon advertises -- it is an exec, RBAC, connection or HTTP failure, and the install may be perfectly current." >&2
        exit 1
      fi
      if ! printf '%s\n' "$kinds" | grep -qxF 'gitops-drift'; then
        echo "ERROR: the Session KV daemon at ${var.daemon_url} does not advertise the 'gitops-drift' inject kind (it advertises: $kinds). This install predates the drift dispatch, so a record sent to it would be graded as a Warning Pod event and reported back as delivered. Redeploy the agent image before running this case." >&2
        exit 1
      fi


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
      planted_ns=1
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
      # The key is read from the pod's own environment inside the exec, never
      # passed in: SESSION_KV_API_KEY is on this container and a value that
      # crossed the runner would be one argv away from a CI log.
      session_id="$("$${exec_in_pod[@]}" sh -c \
        'curl -sS --max-time 10 -X POST -H "Authorization: Bearer $SESSION_KV_API_KEY" "$1/sessions" | jq -r ".sessionID // empty"' \
        sh "${var.daemon_url}" || true)"
      if [ -z "$session_id" ]; then
        echo "ERROR: the daemon returned no sessionID. A 401 here means SESSION_KV_API_KEY on ${var.agent_container} does not match what the daemon expects; a 503 means it is unset." >&2
        exit 1
      fi

      # Minted per apply, and the card poll in step 4 matches on it. That is
      # what makes the poll run-scoped without a per-run object name: the card
      # body renders `insertId=<this>`, so a card left on the board by an
      # earlier run against this same namespace cannot satisfy it.
      insert_id="eval-drift-policy-$(date -u +%s)-$RANDOM"
      changed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

      # The payload the detector would have built (payloadForEvent in
      # k8s-operator/cmd/drift-detector/inject.go) for a delete.
      #
      # Three fields differ from the sibling stack's and each is forced:
      #
      #   verb/method_name name a deletion, which is what the audit entry a
      #   `delete` call produces carries;
      #
      #   `join: no_object` is what the detector's joiner returns for one --
      #   `if record.Verb == deleteVerb || record.Resource.Name == ""`
      #   (join.go) -- because there is no object left to read managedFields
      #   from. The daemon renders that as "Field ownership: not read" and tells
      #   the agent to say so;
      #
      #   `owners` is ABSENT, not empty, for the same reason. payloadForEvent
      #   builds it by appending over event.Owners, which a delete leaves nil,
      #   and the field is `json:"owners,omitempty"` -- so the detector emits
      #   no key at all. Sending `[]` would be the shape that does not occur.
      #   (Either way the daemon never reads it on this branch:
      #   _drift_ownership_block returns before it does.)
      #
      #   the summary carries driftSummary's suffix for a non-enriched
      #   outcome, "(field ownership not read: <outcome>)", which it appends
      #   ahead of the reconciled and owner-manager clauses. Written out
      #   rather than left to the daemon's fallback, because a real record
      #   always carries one and the fallback renders a different sentence.
      payload_file="$kubeconfig_dir/payload.json"
      cat > "$payload_file" <<PAYLOAD
      {
        "kind": "gitops-drift",
        "summary": "${var.drift_principal} deleted ${local.resource_path} on cluster ${var.host_cluster_name} (field ownership not read: no_object)",
        "cluster": "${var.host_cluster_name}",
        "project": "$project",
        "location": "${var.host_cluster_location}",
        "principal": "${var.drift_principal}",
        "user_agent": "kubectl/v1.31.0 (linux/amd64)",
        "verb": "delete",
        "method_name": "io.k8s.networking.v1.networkpolicies.delete",
        "timestamp": "$changed_at",
        "insert_id": "$insert_id",
        "resource": {
          "group": "networking.k8s.io",
          "version": "v1",
          "namespace": "${local.ns}",
          "resource": "networkpolicies",
          "name": "${local.policy}"
        },
        "join": "no_object",
        "reconciled": false
      }
      PAYLOAD

      # The daemon's envelope carries the payload as a JSON *string*, not as a
      # nested object (injectMessageRequest in the detector's inject.go). Built
      # with python rather than by hand so the escaping is the language's
      # problem and not a quoting bug that surfaces as a 400.
      envelope_file="$kubeconfig_dir/envelope.json"
      python3 -c 'import json,sys; sys.stdout.write(json.dumps({"message": open(sys.argv[1]).read()}))' \
        "$payload_file" > "$envelope_file"

      inject_status="$("$${exec_in_pod_stdin[@]}" sh -c \
        'curl -sS --max-time 30 -X POST -H "Authorization: Bearer $SESSION_KV_API_KEY" -H "Content-Type: application/json" --data-binary @- "$1" | jq -r ".status // empty"' \
        sh "${var.daemon_url}/sessions/$session_id/inject" < "$envelope_file" || true)"

      # `suppressed` is a 200. The daemon accepted the record, spent nothing on
      # it and told nobody, because the day's drift alert ceiling is already
      # gone -- so no card is ever filed and the case would time out looking
      # for one.
      #
      # This does NOT bite in CI: hack/ci-deploy.sh sets
      # EVAL_ALERT_DAILY_LIMIT_DRIFT=0 and _alert_daily_limit treats 0 as off,
      # so an eval install has no drift ceiling at all (the sibling case
      # gitops-drift-noise-filtered-triage carries the same note). It bites on
      # a DEV install carrying the shipped default, where this case shares
      # the bucket with every other drift case -- which is the install the
      # eval-driven loop runs against, and the one the message below is for.
      if [ "$inject_status" = "suppressed" ]; then
        echo "ERROR: the daemon suppressed this record against its daily drift alert ceiling, so no triage card will be filed. This install has already taken its drift alerts for the day -- the other gitops-drift cases spend from the same bucket. Raise ALERT_DAILY_LIMIT_DRIFT or run against a fresh install." >&2
        exit 1
      fi
      if [ "$inject_status" != "injected" ]; then
        echo "ERROR: the daemon answered '$inject_status' rather than 'injected'. An empty status means the request did not reach the drift route at all." >&2
        exit 1
      fi
      echo "Daemon accepted the drift record (insert_id=$insert_id)."

      # ---- 4. Wait for the card ---------------------------------------------
      # The inject returns as soon as the daemon has queued the front-door turn,
      # and that turn -- one kanban_create against the cluster agent scoped to
      # ${var.host_cluster_name} -- is what puts the card on the board. Without
      # this wait the agent turn starts first, finds an empty board, and the
      # case grades a correct agent on a fixture that had not finished.
      #
      # Read out of the board's SQLite file rather than from a log line, which
      # is the one place this stack is coupled to an internal path. There is no
      # external seam: the daemon logs to a file inside the pod rather than to
      # stdout, and the front door's kanban_create produces no line of its own,
      # so the alternatives were this or a fixed sleep, and a sleep cannot say
      # what was missing when it is wrong.
      #
      # Matched on the run's own insert id, which _drift_task_body renders into
      # the card body verbatim as `insertId=<id>` -- and which survives that
      # rendering, because the defanging it passes through removes only
      # backticks and newlines (_DRIFT_UNSAFE_CHARS_RE). Not on the title: the
      # front door composes that and could reword it, and it carries no id.
      #
      # The probe searches every text column rather than naming one, because
      # the board's schema is upstream Hermes' and is not in this repository to
      # check a column name against. A column rename upstream would turn a
      # named query into a poll that never matches and reports as a timeout; a
      # rename cannot hide the string from this one.
      board_probe="$kubeconfig_dir/board_probe.py"
      cat > "$board_probe" <<'PROBE'
      import sqlite3
      import sys

      BOARD = "file:/opt/data/kanban.db?mode=ro"

      # Read-only URI so the probe cannot create the file, add a journal beside
      # it, or block the agent writing to it. A board that is not there yet is
      # a 0, not a traceback: the caller polls.
      try:
          conn = sqlite3.connect(BOARD, uri=True)
          columns = [
              row[1]
              for row in conn.execute("PRAGMA table_info(tasks)")
              if (row[2] or "").upper().startswith("TEXT")
          ]
          if not columns:
              print(0)
              raise SystemExit(0)
          needle = "%" + sys.argv[1] + "%"
          where = " OR ".join(f'"{c}" LIKE ?' for c in columns)
          found = conn.execute(
              f"SELECT COUNT(*) FROM tasks WHERE {where}", [needle] * len(columns)
          ).fetchone()[0]
          print(found)
      except sqlite3.Error:
          print(0)
      PROBE

      # Resolved rather than assumed: the daemon runs under the Hermes venv's
      # interpreter, and whether a bare `python3` is also on PATH in this
      # container is an image detail that should not be what this case fails on.
      pod_python=""
      # `import sqlite3`, not `pass`: the probe's own import sits outside its
      # try block, so an interpreter that satisfies `pass` but has no
      # _sqlite3 module would be picked, fail on every poll, and surface
      # 300 seconds later as "the front-door turn filed nothing".
      for candidate in python3 /opt/hermes/.venv/bin/python3; do
        if "$${exec_in_pod[@]}" "$candidate" -c 'import sqlite3' >/dev/null 2>&1; then
          pod_python="$candidate"
          break
        fi
      done
      if [ -z "$pod_python" ]; then
        echo "ERROR: no python interpreter found in ${var.agent_container} to read the kanban board with." >&2
        exit 1
      fi

      # The count is read into a variable and checked for shape before `-ge`
      # sees it. `[ "" -ge 1 ]` is a syntax error under errexit, and a kubectl
      # exec that fails halfway -- an evicted pod, an API blip -- can return
      # partial output as easily as none, so a bare command substitution in the
      # test would turn a transport failure into an abort with no message about
      # what was being waited for.
      elapsed=0
      while :; do
        found="$("$${exec_in_pod_stdin[@]}" "$pod_python" - "$insert_id" < "$board_probe" 2>/dev/null || true)"
        case "$found" in
          "" | *[!0-9]*) found=0 ;;
        esac
        if [ "$found" -ge 1 ]; then
          break
        fi
        if [ "$elapsed" -ge ${local.card_wait_timeout} ]; then
          echo "ERROR: the daemon accepted the record but no kanban card carrying insert_id=$insert_id appeared within $${elapsed}s. The front-door turn the daemon queued either never ran or filed nothing, so there is no triage for the agent to read." >&2
          exit 1
        fi
        sleep ${local.card_poll_interval}
        elapsed=$((elapsed + ${local.card_poll_interval}))
      done
      echo "Triage card for $insert_id appeared after $${elapsed}s."

      # The card then takes minutes to run. Waiting for THAT here would spend
      # the wait outside the agent turn for no gain; the task's prompt polls the
      # board with kanban_show instead, which is the read path the product
      # actually offers.
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
